"""claude-code-router (ccr) management.

ProjectMan makes Claude Code model-agnostic by pointing the spawned ``claude``
at a local ccr service (an Anthropic Messages API <-> arbitrary-provider
gateway) via environment variables. This module owns ccr's config file and
service lifecycle.

Pure module — no GTK. Every function degrades gracefully: if ccr is not
installed, all of them are no-ops and the caller falls back to native Claude.
Nothing here raises into the GTK main loop.
"""

import os
import json
import time
import socket
import secrets
import tempfile
import subprocess

CCR_CONFIG_DIR = os.path.expanduser('~/.claude-code-router')
CCR_CONFIG_PATH = os.path.join(CCR_CONFIG_DIR, 'config.json')

_CMD_TIMEOUT = 10  # seconds for the run-and-wait commands: stop/status
_START_POLL_INTERVAL = 0.25  # seconds between is_running probes after start()

# Handle for the most recent detached `ccr start` (see start() for why it is
# detached). Kept so we can reap it without blocking; we never wait on it.
_started_proc = None

# Failed-start cooldown: monotonic deadline after which start retries are
# allowed again. Set when a full start+poll cycle ends in failure; cleared on
# any successful probe (a running server is never ignored), on config-write
# (user may have fixed the provider), or on expiry. Suppresses only start+poll
# — the probe still runs — so an N-project restore pays N cheap probes but
# only ONE start_wait budget on the GTK main loop, not N×start_wait.
_COOLDOWN_SECS = 30
_cooldown_deadline = None  # float | None; compared against monotonic_fn()


def _log(settings, msg):
    if getattr(settings, 'debug_logging', False):
        print(f'ProjectMan[ccr]: {msg}')


def available(settings):
    """True if the ccr binary is on PATH (or the configured override exists)."""
    import shutil
    return shutil.which(settings.resolved_ccr_binary) is not None


def ensure_api_key(settings):
    """Mint a ccr API key into ``settings.ccr_api_key`` if blank; return it.

    The caller is responsible for persisting via ``settings.save()``. The same
    value is used as ccr's ``APIKEY`` and as the auth token injected into the
    ``claude`` child, so they must match.
    """
    if not settings.ccr_api_key:
        settings.ccr_api_key = secrets.token_urlsafe(32)
    return settings.ccr_api_key


def _router_target(settings):
    """Pick a 'provider,model' string for ccr's Router (its required default).

    Prefers the global default model; falls back to any custom per-project
    override; finally the first model in any provider.
    """
    candidates = [settings.model_default]
    candidates.extend(settings.model_overrides.values())
    for model in candidates:
        if model and '/' in model and model.split('/', 1)[0] in settings.providers:
            return model.replace('/', ',', 1)
    for pid, prov in settings.providers.items():
        models = prov.get('models') if isinstance(prov, dict) else None
        if isinstance(models, dict) and models:
            return f'{pid},{next(iter(models))}'
    return ''


def render_config(settings):
    """Translate ``settings.providers`` + selection into a ccr config dict."""
    providers = []
    for pid, prov in settings.providers.items():
        if not isinstance(prov, dict):
            continue
        models = prov.get('models')
        entry = {
            'name': pid,
            'api_base_url': prov.get('base_url', ''),
            'api_key': prov.get('api_key', ''),
            'models': list(models.keys()) if isinstance(models, dict) else [],
        }
        transformer = prov.get('transformer')
        if transformer:
            entry['transformer'] = {'use': [transformer]}
        providers.append(entry)

    target = _router_target(settings)
    router = {k: target for k in ('default', 'background', 'think', 'longContext')}
    return {
        'HOST': settings.ccr_host,
        'PORT': settings.ccr_port,
        'APIKEY': settings.ccr_api_key,
        'Providers': providers,
        'Router': router,
    }


def write_config(settings):
    """Atomically write ccr's config.json from settings. Return True on success.

    The file holds provider API keys in cleartext (ccr has no keyring), so it
    is written 0600 inside a 0700 directory.

    A successful write also clears the failed-start cooldown: the user may have
    fixed the provider config, so the next spawn_env() should retry rather than
    short-circuit on the cooldown.
    """
    global _cooldown_deadline
    try:
        os.makedirs(CCR_CONFIG_DIR, exist_ok=True)
        try:
            os.chmod(CCR_CONFIG_DIR, 0o700)
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(dir=CCR_CONFIG_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(render_config(settings), f, indent=2)
            os.replace(tmp_path, CCR_CONFIG_PATH)
            try:
                os.chmod(CCR_CONFIG_PATH, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _cooldown_deadline = None   # config change → user may have fixed it; retry
        return True
    except Exception as e:
        _log(settings, f'write_config failed: {e}')
        return False


def config_differs(settings):
    """True if the on-disk ccr config differs from what settings would render.

    Only the keys ProjectMan manages are compared, so runtime keys ccr may add
    on its own do not force needless restarts.
    """
    rendered = render_config(settings)
    try:
        with open(CCR_CONFIG_PATH, 'r') as f:
            on_disk = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True
    return any(on_disk.get(k) != v for k, v in rendered.items())


def _run(settings, *args):
    """Run a ccr subcommand; return True on exit code 0. Never raises."""
    try:
        result = subprocess.run(
            [settings.resolved_ccr_binary, *args],
            capture_output=True, text=True, timeout=_CMD_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        _log(settings, f'ccr {" ".join(args)} -> {result.returncode}')
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(settings, f'ccr {" ".join(args)} failed: {e}')
        return False


def start(settings):
    """Launch the ccr service detached; return True iff the process was spawned.

    WHY detached (not _run): ccr v2.0.0 does NOT daemonize in a non-tty context
    (the desktop launch case) — the CLI process IS the foreground server and
    never exits on its own. ``subprocess.run(..., timeout=_CMD_TIMEOUT)`` would
    therefore wait the full timeout on a process that never returns and then
    KILL the very server it just started (verified on the test VM). So we spawn
    with ``Popen(..., start_new_session=True)`` and DEVNULL stdio: the child
    gets its own session (PM-exit signals don't reach it; ProjectMan stops it
    explicitly via ``stop()`` on close), and PM does not block on it. Readiness
    is decided solely by the port probe in ``spawn_env()`` / callers — start()'s
    return only reports whether the spawn syscall succeeded.
    """
    global _started_proc
    _reap_started()
    try:
        _started_proc = subprocess.Popen(
            [settings.resolved_ccr_binary, 'start'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log(settings, f'ccr start -> detached pid {_started_proc.pid}')
        return True
    except (FileNotFoundError, OSError) as e:
        _log(settings, f'ccr start (detached) failed: {e}')
        _started_proc = None
        return False


def stop(settings):
    # `ccr stop` is a normal run-and-exit CLI command — safe to wait on.
    return _run(settings, 'stop')


_RESTART_REAP_INTERVAL = 0.25  # seconds between reap polls in restart()
_RESTART_REAP_BUDGET = 2.0     # maximum total seconds to wait for old handle to exit


def restart(settings, *, sleep_fn=None):
    """Restart ccr: stop the old server, then spawn a fresh detached one.

    `ccr restart` itself would re-spawn the same non-daemonizing foreground
    server, so it has the same kill-on-timeout hazard as `start`. Compose it
    from a run-and-wait ``stop`` (the stop CLI exits promptly) followed by a
    detached ``start``; the result reflects whether the new server spawned.

    WHY bounded reap: after stop() returns the old `_started_proc` handle may
    still be mid-shutdown (poll() is None) — overwriting it immediately drops
    the handle unreaped. Poll at most ``_RESTART_REAP_BUDGET`` seconds before
    proceeding; never hang regardless of outcome (old process cleanup is best-
    effort). ``sleep_fn`` is injectable for tests (keeps them instant).
    """
    global _started_proc
    if sleep_fn is None:
        sleep_fn = time.sleep
    stop(settings)
    # Bounded wait for the old handle to exit before we overwrite it.
    if _started_proc is not None:
        steps = max(1, int(_RESTART_REAP_BUDGET / _RESTART_REAP_INTERVAL))
        for _ in range(steps):
            if _started_proc.poll() is not None:
                _started_proc = None
                break
            sleep_fn(_RESTART_REAP_INTERVAL)
        else:
            if _started_proc is not None:
                _log(settings, f'restart: old handle pid={_started_proc.pid} '
                               f'still live after reap budget — overwriting')
                _started_proc = None
    return start(settings)


def _reap_started(settings=None):
    """Non-blocking zombie hygiene for the detached ``start`` child.

    If the recorded handle's process has exited, clear it (so the OS can free
    the entry and we don't keep a stale handle). Never waits, never blocks —
    a still-running server (poll() is None) is left untouched. Called at the
    entry of ``spawn_env()`` and ``sync()``.
    """
    global _started_proc
    if _started_proc is not None and _started_proc.poll() is not None:
        _started_proc = None


def is_running(settings):
    """True if the ccr service is accepting connections on host:port."""
    try:
        with socket.create_connection(
                (settings.ccr_host, settings.ccr_port), timeout=0.5):
            return True
    except OSError:
        return False


def sync(settings):
    """Reconcile the ccr service with current settings.

    No-op if ccr is not installed. No-op beyond _reap_started() when
    ccr_managed is False — the user runs ccr themselves; ProjectMan must
    never start, stop, restart, or reconfigure it. Stops the service when no
    custom model is selected; otherwise writes a fresh config (if changed)
    and (re)starts.
    """
    global _cooldown_deadline
    _reap_started(settings)
    if not available(settings):
        _log(settings, 'ccr not installed — skipping sync')
        return
    if not settings.ccr_managed:
        _log(settings, 'ccr_managed=False — skipping sync (externally managed)')
        return
    if not settings.any_custom_model_active():
        if is_running(settings):
            stop(settings)
        return
    ensure_api_key(settings)
    if config_differs(settings):
        write_config(settings)
        # Config changed → user may have fixed the provider; clear the
        # failed-start cooldown so the next spawn_env() retries immediately.
        _cooldown_deadline = None
        if is_running(settings):
            restart(settings)
            return
    if not is_running(settings):
        start(settings)


def spawn_env(settings, project_path, *, probe=None, start_wait=4.0,
              sleep_fn=None, monotonic_fn=None):
    """Return (env_dict, fallback_reason) for spawning a claude child.

    Pure function — no GTK; injectable ``probe``/``sleep_fn``/``monotonic_fn``
    for tests (which never sleep and never use real time deltas).

    ``probe`` defaults to ``is_running``; ``sleep_fn`` to ``time.sleep``;
    ``monotonic_fn`` to ``time.monotonic`` (used only for cooldown checks).

    ``start()`` is fire-and-forget (it spawns the server detached and returns
    immediately — see start() for why), and ccr's node server doesn't bind the
    port until ~2s after launch, so an immediate re-probe would falsely fall
    back on every cold start. Instead the probe is re-polled up to
    ``start_wait`` seconds in _START_POLL_INTERVAL steps; the port probe is the
    sole readiness arbiter. The retry budget is count-based (no clock math), so
    tests inject a no-op ``sleep_fn`` and stay instant.

    When ccr_managed=False and ccr is not running, a DISTINCT reason is
    returned (mentions enabling 'Manage ccr' or starting manually). start() is
    never called when ccr_managed=False.

    The failed-start cooldown (``_cooldown_deadline``) suppresses the
    start+poll budget after a failed cycle: while active, a not-running probe
    returns immediately (no start, no poll, no sleeps) with the SAME failure
    reason string as the full cycle — byte-equal so the toast aggregator
    collapses a restore burst into one notice; the retry-suppression detail
    goes only to the debug log. The probe itself still runs FIRST, so a
    running server is never ignored — a successful probe clears the cooldown.
    An N-project restore with an unbindable ccr pays N probes (≤0.5s each)
    but only ONE start_wait budget. Also cleared by config writes
    (``write_config``/``sync``) and expiry.

    Return values:
      (dict, None)   — ccr is ready; inject these env vars into the child
      (None, None)   — native path; caller spawns without custom env
      (None, str)    — ccr should be in use but isn't available/startable;
                       caller falls back to native and shows the reason string
    """
    global _cooldown_deadline
    if probe is None:
        probe = is_running
    if sleep_fn is None:
        sleep_fn = time.sleep
    if monotonic_fn is None:
        monotonic_fn = time.monotonic

    _reap_started(settings)

    # Common path: project uses native Anthropic — skip every socket check.
    if not settings.uses_custom_model(project_path):
        return (None, None)

    if not available(settings):
        reason = (
            'ccr binary not found — spawning with native Anthropic. '
            'Install claude-code-router or clear the custom model setting.'
        )
        _log(settings, f'spawn_env: {reason}')
        return (None, reason)

    if not probe(settings):
        # ccr not running. Behaviour splits on whether ProjectMan manages ccr.
        if not settings.ccr_managed:
            # Running-externally path for managed=False is above (probe True);
            # not-running with managed=False → distinct reason, never start().
            reason = (
                f'ccr is not running on {settings.ccr_host}:{settings.ccr_port} '
                'and ProjectMan is not managing it — spawning with native '
                'Anthropic. Enable "Manage ccr" in Settings or start ccr manually.'
            )
            _log(settings, f'spawn_env: {reason}')
            return (None, reason)

        # ccr_managed=True with an active failed-start cooldown: what the
        # cooldown suppresses is the start+poll budget (the N×4s restore
        # multiplier), NOT the probe — each spawn during cooldown pays at most
        # the one probe above (a ≤0.5s connection-refused timeout). A running
        # server is never ignored: the probe-True path below clears the
        # cooldown (the "cleared by any successful probe" clause — e.g. the
        # user ran `ccr start` manually mid-cooldown). Also cleared by config
        # writes (write_config/sync) and natural expiry.
        now = monotonic_fn()
        if _cooldown_deadline is not None and now < _cooldown_deadline:
            # Byte-equal to the full-failure reason below: identical strings
            # are what let aggregate_fallback_notices() collapse a restore
            # burst (one full failure + N-1 suppressed) into ONE toast — a
            # per-spawn time-varying suffix split it (VM gate s2 finding).
            # Users care that ccr didn't start, not that the retry was
            # memoized; the suppression detail is debug-log-only.
            remaining = int(_cooldown_deadline - now)
            reason = (
                f'ccr did not start on {settings.ccr_host}:{settings.ccr_port} — '
                'spawning with native Anthropic.'
            )
            _log(settings, f'spawn_env: {reason} '
                           f'(retry suppressed {remaining}s after recent failure)')
            return (None, reason)

        # Not running — ensure exactly ONE start is in flight, then poll until
        # the server binds or the budget runs out. A detached start from
        # sync() (or a concurrent spawn) may still be binding its port; firing
        # another would race ccr's pidfile bookkeeping — the bind-race loser
        # clobbers then deletes the winner's pidfile on exit, leaving the
        # surviving server unstoppable by `ccr stop` (VM gate round-3
        # finding). The entry _reap_started() already cleared any finished
        # handle, so a live handle here means a start is genuinely pending.
        if _started_proc is None:
            start(settings)
        else:
            _log(settings, 'spawn_env: ccr start already in flight — polling')
        attempts = max(1, int(start_wait / _START_POLL_INTERVAL))
        for _ in range(attempts):
            sleep_fn(_START_POLL_INTERVAL)
            if probe(settings):
                break
        else:
            # Full poll budget exhausted without ccr binding its port. Record
            # the cooldown so subsequent spawns return immediately instead of
            # each waiting start_wait seconds (N-project restore penalty).
            _cooldown_deadline = monotonic_fn() + _COOLDOWN_SECS
            reason = (
                f'ccr did not start on {settings.ccr_host}:{settings.ccr_port} — '
                'spawning with native Anthropic.'
            )
            _log(settings, f'spawn_env: {reason}')
            return (None, reason)

    # ccr is reachable — this IS the successful-probe cooldown clear (the
    # initial probe or a poll-loop probe came back True) — build the env dict.
    _cooldown_deadline = None
    model = settings.effective_model(project_path).replace('/', ',', 1)
    env = dict(os.environ)
    env['ANTHROPIC_BASE_URL'] = f'http://{settings.ccr_host}:{settings.ccr_port}'
    env['ANTHROPIC_AUTH_TOKEN'] = settings.ccr_api_key or ''
    env['ANTHROPIC_API_KEY'] = settings.ccr_api_key or ''
    env['ANTHROPIC_MODEL'] = model
    return (env, None)


# ---------------------------------------------------------------------------
# Toast aggregation — pure helper (no GTK); tested in test_ccr_p15.py.
# ---------------------------------------------------------------------------

def aggregate_fallback_notices(events):
    """Collapse (project_name, reason) fallback events into display string(s).

    WHY: N failing projects → N identical toasts dismissed one-by-one is poor
    UX. This helper groups events by reason: a single event returns the verbatim
    P0 string; multiple events with the SAME reason collapse to one aggregate;
    events with DIFFERENT reasons produce separate strings (one per reason).

    Return value:
      ``None``/``''``  — empty input; caller shows nothing
      ``str``          — single reason (one or more projects, collapsed)
      ``list[str]``    — two or more distinct reasons (one string each)

    The single-project P0 format ``'ccr unavailable — running native Claude. <reason>'``
    is UNCHANGED for the single-project case (spec requirement: verbatim P0 string).
    """
    if not events:
        return None

    # Group by reason text, preserving insertion order.
    from collections import OrderedDict
    groups: dict = OrderedDict()
    for _name, reason in events:
        groups.setdefault(reason, 0)
        groups[reason] += 1

    def _format(reason, count):
        if count == 1:
            return f'ccr unavailable — running native Claude. {reason}'
        return (
            f'ccr unavailable — {count} projects running native Claude. {reason}'
        )

    strings = [_format(r, c) for r, c in groups.items()]
    if len(strings) == 1:
        return strings[0]
    return strings
