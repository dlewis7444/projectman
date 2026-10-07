"""GTK-free headless harness runner for PAA.

Phase A of the PAA headless harness axis (see
``docs/paa-headless-harness-plan.md``):

* ``run_headless`` — subprocess execution factored out of the old
  ``paa_haiku._run_scan_model`` (timeout / OSError / nonzero / bad-JSON
  failure mapping preserved).
* ``resolve_headless_adapter`` — same harness axis as the main UI
  (``settings.effective_harness``), with explicit claude fallback when the
  resolved harness lacks a real headless plan implementation.
* ``chat_turn`` — resumable one-shot chat primitive for Phase B.

No GTK, no live LLM in unit tests. Scans rewire through ``paa_haiku`` so
callers keep their existing ``(text|None, tokens)`` contract.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace

from harnesses import (
    ADAPTERS,
    DEFAULT_HARNESS,
    HeadlessPlan,
    HeadlessPolicyError,
    HeadlessResult,
    adapter_implements_headless,
    with_harness_path,
)

log = logging.getLogger(__name__)


def default_paa_cwd(settings) -> str:
    """Isolated cwd for headless runs: ``<projects_dir>/.project-admin-agent``.

    Matches the historical scan path so headless claude does not pollute real
    project session histories. Created if missing.
    """
    paa_dir = os.path.join(settings.resolved_projects_dir, '.project-admin-agent')
    os.makedirs(paa_dir, exist_ok=True)
    return paa_dir


# Repo paa/ package → destination under the PAA cwd (item 5, 2026-10-01
# hardening): the deployed persona docs are stale regular-file copies, so
# serve() syncs them from the repo on every startup.
PERSONA_MAP = (
    ('AGENTS.md', 'AGENTS.md'),
    ('AGENTS-SUPPLEMENT.md', os.path.join('.system', 'AGENTS-SUPPLEMENT.md')),
)


def sync_paa_persona(paa_cwd: str, repo_dir: str | None = None) -> list:
    """Sync the repo's persona docs into the PAA cwd; idempotent.

    Content-diff + atomic tmp+os.replace, like the MCP config installer:
    a destination already byte-identical to the repo source is left
    untouched. Missing repo sources are skipped (nothing to sync FROM
    must not erase a working deployment). Returns a list of
    ``(dest_name, changed)`` pairs. Only ever writes inside *paa_cwd*.
    """
    if repo_dir is None:
        repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'paa')
    results = []
    # A deployed CLAUDE.md is a stale pre-rewrite artifact (2026-10-01,
    # review F2): grok's compat.claude.agents scan loads it alongside
    # AGENTS.md, feeding the model contradictory persona text. Project
    # rules live only in AGENTS.md — remove the leftover, never create one.
    stale_claude = os.path.join(paa_cwd, 'CLAUDE.md')
    if os.path.isfile(stale_claude):
        try:
            os.unlink(stale_claude)
            results.append(('CLAUDE.md', True))
        except OSError:
            log.warning('paa persona: could not remove stale %s',
                        stale_claude)
    for source_name, dest_rel in PERSONA_MAP:
        source = os.path.join(repo_dir, source_name)
        dest = os.path.join(paa_cwd, dest_rel)
        try:
            with open(source, 'r') as fh:
                desired = fh.read()
        except OSError:
            log.warning('paa persona: repo source %s missing; skipping',
                        source)
            continue
        try:
            with open(dest, 'r') as fh:
                current = fh.read()
        except OSError:
            current = None
        if current == desired:
            results.append((dest_rel, False))
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(desired)
        os.replace(tmp, dest)
        results.append((dest_rel, True))
    return results


def claude_binary_resolvable(settings) -> bool:
    """True when the configured claude binary resolves on PATH (or abs path)."""
    binary = getattr(settings, 'resolved_claude_binary', None) or 'claude'
    binary = str(binary).strip() or 'claude'
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    env = with_harness_path()
    return shutil.which(binary, path=env.get('PATH')) is not None


def resolve_headless_adapter(settings, project_path=''):
    """Resolve the harness adapter for a headless scan/chat turn.

    Returns ``(adapter, harness_id, fallback_reason)``:

    1. ``hid = settings.effective_harness(project_path or '')``
    2. If that adapter implements real headless (``caps.headless_json`` and a
       non-default ``headless_plan``) → use it, ``fallback_reason=None``.
    3. Else fall back to claude with a human-readable reason. If claude's
       binary is unresolvable → ``(None, hid, reason)`` so callers no-op
       exactly as scan failures do today.

    Design choice (W5 deferred): non-Claude builtins still advertise
    ``headless_json=True`` but their ``headless_plan`` is the default stub
    (returns ``None``). Resolution requires ``adapter_implements_headless``,
    so they fall back to claude until W5 wires verified argv.
    """
    hid = settings.effective_harness(project_path or '')
    adapter = ADAPTERS.get(hid)

    if adapter_implements_headless(adapter):
        return (adapter, hid, None)

    # --- fallback to claude ------------------------------------------------
    claude = ADAPTERS.get(DEFAULT_HARNESS) or ADAPTERS.get('claude')
    if claude is None or not claude_binary_resolvable(settings):
        if adapter is None:
            reason = (
                f"harness {hid!r} unknown and claude binary unresolvable; "
                "headless unavailable"
            )
        elif not getattr(getattr(adapter, 'caps', None), 'headless_json', False):
            reason = (
                f"{hid} lacks headless_json and claude binary unresolvable; "
                "headless unavailable"
            )
        else:
            reason = (
                f"{hid} has no headless plan implementation and claude binary "
                "unresolvable; headless unavailable"
            )
        return (None, hid, reason)

    if adapter is None:
        reason = f"unknown harness {hid!r}; used claude"
    elif not getattr(getattr(adapter, 'caps', None), 'headless_json', False):
        reason = f"{hid} lacks headless_json; used claude"
    else:
        reason = f"{hid} has no headless plan implementation; used claude"
    return (claude, hid, reason)


class HeadlessHandle:
    """Mutable kill switch for one cancellable headless child.

    ``kill`` is safe to call before the process exists and from another
    thread. The runner attaches the ``Popen`` and observes ``killed``.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self.killed = False

    def attach(self, proc):
        with self._lock:
            self._proc = proc
            already = self.killed
        if already and proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def kill(self):
        with self._lock:
            self.killed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def clear(self):
        with self._lock:
            self._proc = None


def _prepare_headless(plan, parse_fn, settings):
    """Shared cwd / early-error setup for the blocking and cancellable runners.

    Returns ``(cwd, error_result)``. ``error_result`` is set when the plan
    cannot run; ``cwd`` is set otherwise.
    """
    if plan is None:
        return None, HeadlessResult(text=None, error='no headless plan')
    if not callable(parse_fn):
        return None, HeadlessResult(text=None, error='no parse function')

    if plan.cwd is None:
        if settings is None:
            return None, HeadlessResult(
                text=None,
                error=(
                    'plan.cwd is None and no settings provided; '
                    'refuse to inherit process cwd (pass settings or set plan.cwd)'
                ),
            )
        cwd = default_paa_cwd(settings)
    else:
        cwd = plan.cwd
        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError:
            pass
    return cwd, None


def _log_headless_launch(plan, settings, cwd):
    try:
        from debug_log import debug_log, format_argv_for_debug
        fb = getattr(plan, 'fallback_reason', None)
        bits = [
            'session launch',
            'kind=headless',
            f'timeout={getattr(plan, "timeout", "?")}',
            f'cwd={cwd}',
        ]
        if fb:
            bits.append(f'fallback={fb}')
        bits.append(f'argv={format_argv_for_debug(plan.argv)}')
        debug_log(settings, ' '.join(bits))
    except Exception:
        pass


def _result_from_completed(stdout, returncode, parse_fn):
    if returncode != 0:
        return HeadlessResult(text=None, error=f'exit {returncode}')
    try:
        parsed = parse_fn(stdout)
    except Exception as exc:  # noqa: BLE001 — never let parse crash the scan
        return HeadlessResult(text=None, error=f'parse error: {exc}')
    if isinstance(parsed, HeadlessResult):
        return parsed
    return HeadlessResult(text=None, error='parse returned non-HeadlessResult')


def run_headless(plan: HeadlessPlan | None, parse_fn, *, settings=None) -> HeadlessResult:
    """Execute a ``HeadlessPlan`` and normalize via *parse_fn*.

    Subprocess shape matches the historical scan runner: ``capture_output``,
    ``text=True``, ``stdin=DEVNULL``, ``timeout=plan.timeout``, env passthrough
    (``None`` = inherit). When ``plan.cwd`` is None, cwd is always
    ``default_paa_cwd(settings)`` — *settings* is required in that case so the
    process cwd is never inherited silently (PAA session isolation). Explicit
    ``plan.cwd`` is created if missing.

    Failure mapping (parity with old ``_run_scan_model``):
    timeout / OSError / nonzero exit / parse returning text=None →
    ``HeadlessResult(text=None, error=...)``.
    """
    cwd, early = _prepare_headless(plan, parse_fn, settings)
    if early is not None:
        return early
    _log_headless_launch(plan, settings, cwd)

    try:
        result = subprocess.run(
            plan.argv,
            capture_output=True,
            text=True,
            timeout=plan.timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            env=plan.env,
        )
    except subprocess.TimeoutExpired:
        return HeadlessResult(text=None, error='timeout')
    except FileNotFoundError as exc:
        return HeadlessResult(text=None, error=f'not found: {exc}')
    except OSError as exc:
        return HeadlessResult(text=None, error=f'OSError: {exc}')

    return _result_from_completed(result.stdout, result.returncode, parse_fn)


def run_headless_cancellable(plan: HeadlessPlan | None, parse_fn, *,
                             settings=None, cancel_event=None,
                             handle: HeadlessHandle | None = None) -> HeadlessResult:
    """Like :func:`run_headless`, but the caller can kill the child.

    Same cwd, env, stdin, timeout, and parse failures. ``handle.kill()`` or a
    set ``cancel_event`` stops the process and returns an error result.
    Scans keep :func:`run_headless`.
    """
    cwd, early = _prepare_headless(plan, parse_fn, settings)
    if early is not None:
        return early
    _log_headless_launch(plan, settings, cwd)

    try:
        proc = subprocess.Popen(
            plan.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            env=plan.env,
        )
    except FileNotFoundError as exc:
        return HeadlessResult(text=None, error=f'not found: {exc}')
    except OSError as exc:
        return HeadlessResult(text=None, error=f'OSError: {exc}')

    if handle is not None:
        handle.attach(proc)

    deadline = time.monotonic() + (plan.timeout if plan.timeout is not None else 30)
    cancelled = False
    timed_out = False
    stdout = ''
    try:
        while True:
            if handle is not None and handle.killed:
                cancelled = True
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                if handle is not None:
                    handle.kill()
                elif proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                break
            try:
                stdout, _stderr = proc.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
        if cancelled or timed_out:
            try:
                out, _err = proc.communicate(timeout=5)
                if out:
                    stdout = out
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if handle is not None:
            handle.clear()

    if cancelled or (handle is not None and handle.killed):
        return HeadlessResult(text=None, error='cancelled')
    if timed_out:
        return HeadlessResult(text=None, error='timeout')
    return _result_from_completed(stdout, proc.returncode, parse_fn)


def _claude_fallback_adapter(settings):
    """Return the claude adapter when its binary resolves; else None."""
    claude = ADAPTERS.get(DEFAULT_HARNESS) or ADAPTERS.get('claude')
    if claude is None or not claude_binary_resolvable(settings):
        return None
    return claude


_UNCONSTRAINED = ('opencode', 'kimi')


def _invoke_headless_plan(adapter, prompt, settings, project_path, *,
                          session_id=None, model=None,
                          tool_policy='legacy', timeout=None,
                          approval_sock=None, approval_token=None,
                          reasoning_effort=None):
    """Call ``headless_plan``, passing policy/timeout only if it accepts them.

    Older test doubles and custom adapters keep the original keyword set.
    A non-legacy policy is never forwarded to an adapter that cannot say so,
    because that would build unconstrained argv. *approval_sock* /
    *approval_token* (the two halves of the gate review channel) and
    *reasoning_effort* (effort tier: reviewer_effort for review calls,
    chat_effort for the user's turns) are forwarded only when the
    signature accepts them.
    """
    fn = adapter.headless_plan
    kwargs = {'session_id': session_id, 'model': model}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = None
    if params is not None:
        var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        accepts_policy = var_kw or 'tool_policy' in params
        accepts_timeout = var_kw or 'timeout' in params
        accepts_sock = var_kw or 'approval_sock' in params
        accepts_token = var_kw or 'approval_token' in params
        accepts_effort = var_kw or 'reasoning_effort' in params
        if tool_policy not in (None, 'legacy'):
            if not accepts_policy:
                return None
            kwargs['tool_policy'] = tool_policy
        if timeout is not None and accepts_timeout:
            kwargs['timeout'] = timeout
        if approval_sock is not None and accepts_sock:
            kwargs['approval_sock'] = approval_sock
        if approval_token is not None and accepts_token:
            kwargs['approval_token'] = approval_token
        if reasoning_effort is not None and accepts_effort:
            kwargs['reasoning_effort'] = reasoning_effort
    elif tool_policy not in (None, 'legacy'):
        return None
    return fn(prompt, settings, project_path, **kwargs)


def plan_with_none_fallback(adapter, hid, settings, prompt, project_path, *,
                            session_id=None, model=None, claude_model=None,
                            tool_policy='legacy', timeout=None,
                            approval_sock=None, approval_token=None,
                            reasoning_effort=None):
    """Build a headless plan; if the adapter returns None, fall back to claude.

    Adapters that claim to implement headless can still return None for a given
    call (W5 safety). Treat that as missing capability: try claude (with a
    reason), never silently proceed with no plan.

    *claude_model* is used only on the claude fallback path (e.g. scan tier
    ``paa_scan_model``). When omitted, falls back to *model*, then
    ``paa_chat_model`` / ``sonnet``. *approval_sock* / *approval_token* are
    the two halves of the gate review channel and only mean anything for
    adapters with a ``gate`` policy. *reasoning_effort* (the effort tier:
    ``reviewer_effort`` for review calls, ``chat_effort`` for the user's
    turns) is forwarded to adapters whose plan accepts it (grok).

    Returns ``(adapter, plan, fallback_reason)``. ``plan`` may still be None
    when claude is unavailable or also returns None; ``adapter`` may become
    claude or None.
    """
    try:
        plan = _invoke_headless_plan(
            adapter, prompt, settings, project_path,
            session_id=session_id, model=model,
            tool_policy=tool_policy, timeout=timeout,
            approval_sock=approval_sock, approval_token=approval_token,
            reasoning_effort=reasoning_effort,
        )
    except HeadlessPolicyError:
        raise
    if plan is not None:
        return (adapter, plan, None)

    adapter_id = getattr(adapter, 'id', hid)
    if tool_policy not in (None, 'legacy') and adapter_id in _UNCONSTRAINED:
        raise HeadlessPolicyError(
            f'the bot will not run {adapter_id} unconstrained')
    if adapter_id == 'claude' or adapter is ADAPTERS.get('claude'):
        return (adapter, None, f'{adapter_id} headless_plan returned None')

    claude = _claude_fallback_adapter(settings)
    if claude is None:
        return (
            None,
            None,
            f'{adapter_id} headless_plan returned None and claude binary '
            'unresolvable; headless unavailable',
        )

    # Prefer explicit claude_model (scan path), then caller's model, then chat tier
    fb_model = claude_model if claude_model is not None else model
    if fb_model is None:
        tier = (getattr(settings, 'paa_chat_model', None) or 'sonnet')
        if isinstance(tier, str):
            fb_model = tier.strip() or 'sonnet'
        else:
            fb_model = 'sonnet'

    reason = f'{adapter_id} headless_plan returned None; used claude'
    try:
        plan = _invoke_headless_plan(
            claude, prompt, settings, project_path,
            session_id=session_id, model=fb_model,
            tool_policy=tool_policy, timeout=timeout,
            approval_sock=approval_sock, approval_token=approval_token,
            reasoning_effort=reasoning_effort,
        )
    except HeadlessPolicyError:
        raise
    return (claude, plan, reason)


def _stamp_fallback(result: HeadlessResult, reason: str | None) -> HeadlessResult:
    """Attach a harness fallback where the phone can see it.

    When the result has text, ``error`` stays empty — the fallback is not a
    failure. A text-less failure keeps its own ``error``.
    """
    if not reason:
        return result
    result.fallback_reason = reason
    if result.text:
        result.error = None
    return result


def chat_turn(settings, prompt, *, session_id=None, project_path='',
              model=None, tool_policy='legacy', timeout=None,
              cancel_event=None, handle: HeadlessHandle | None = None,
              approval_sock=None, approval_token=None,
              reasoning_effort=None,
              ) -> HeadlessResult:
    """One headless chat turn (new session or resume).

    Resolves the harness via :func:`resolve_headless_adapter`. When resume
    (``session_id`` set) is requested and the resolved adapter lacks
    ``caps.headless_chat``, applies the same claude fallback policy; if claude
    also cannot chat headlessly, returns an error result.

    If the resolved adapter's ``headless_plan`` returns None, falls back to
    claude (W5 safety) rather than a silent no-op.

    *approval_sock* / *approval_token* are the two halves of the gate review
    channel (a unix socket path plus the per-turn token the broker mints;
    the bot-owned MCP server presents them to the broker on every
    ``run_command`` call). Required by adapters for
    ``tool_policy='gate'``; they raise :class:`HeadlessPolicyError` when
    either half is missing, surfaced here as an error result. Ignored by
    other policies.

    *reasoning_effort* (item 4, 2026-10-01) is the effort tier for
    this turn ('none'/'minimal'/'low'/'medium'/'high'); harnesses that
    support it (grok ``--reasoning-effort``) get it, others ignore it.
    The bot sources it from ``reviewer_effort`` (review calls) or
    ``chat_effort`` (the user's turns).

    Session-id **persistence is the caller's problem** — this primitive only
    returns the (possibly new) ``session_id`` from the harness JSON.

    **Kimi resume cwd:** when the resolved harness is kimi, ``session_id`` is
    only valid for sessions created under the runner cwd (PAA isolates under
    ``.project-admin-agent`` via :func:`default_paa_cwd`). Do not resume an
    interactive project-cwd kimi session here — kimi enforces workDir match.
    """
    adapter, hid, fallback_reason = resolve_headless_adapter(
        settings, project_path or '')
    if adapter is None:
        return HeadlessResult(
            text=None,
            error=fallback_reason or 'no headless adapter available',
            fallback_reason=fallback_reason,
        )
    if tool_policy not in ('legacy', 'plan', 'armed', 'gate'):
        return HeadlessResult(
            text=None, error=f'unknown tool_policy {tool_policy!r}')

    # Resume needs headless_chat. If the resolved adapter can't, try claude.
    if session_id and not getattr(adapter.caps, 'headless_chat', False):
        claude = _claude_fallback_adapter(settings)
        if (claude is not None
                and getattr(claude.caps, 'headless_chat', False)
                and adapter is not claude):
            fallback_reason = (
                f"{getattr(adapter, 'id', hid)} lacks headless_chat; used claude"
            )
            adapter = claude
        else:
            return HeadlessResult(
                text=None,
                error=(
                    f"{getattr(adapter, 'id', hid)} lacks headless_chat; "
                    "cannot resume headless session"
                ),
            )

    # New chat also prefers headless_chat when available; scans use headless_json
    # only. For a plain new turn without resume, headless_json + plan is enough,
    # but if the adapter claims no headless_chat and we want chat semantics,
    # still allow a stateless one-shot via headless_plan (same as a scan).
    if fallback_reason:
        log.info('paa headless chat fallback: %s', fallback_reason)

    # Claude-axis tier for chat when model not supplied and adapter is claude
    if model is None and getattr(adapter, 'id', None) == 'claude':
        tier = (getattr(settings, 'paa_chat_model', None) or 'sonnet')
        if isinstance(tier, str):
            tier = tier.strip() or 'sonnet'
        else:
            tier = 'sonnet'
        model = tier

    # OpenCode and Kimi have no verified read-only flag. Refuse before any
    # argv is built, including a claude fallback that would run unconstrained.
    aid = getattr(adapter, 'id', hid)
    if tool_policy != 'legacy' and aid in _UNCONSTRAINED:
        return HeadlessResult(
            text=None,
            error=f'the bot will not run {aid} unconstrained',
        )

    resolve_path = project_path if project_path is not None else ''
    try:
        adapter, plan, none_reason = plan_with_none_fallback(
            adapter, hid, settings, prompt, resolve_path,
            session_id=session_id, model=model,
            tool_policy=tool_policy, timeout=timeout,
            approval_sock=approval_sock, approval_token=approval_token,
            reasoning_effort=reasoning_effort,
        )
    except HeadlessPolicyError as exc:
        return HeadlessResult(text=None, error=str(exc))
    if none_reason and plan is not None:
        log.info('paa headless chat fallback: %s', none_reason)
    if plan is None:
        return HeadlessResult(
            text=None,
            error=none_reason or (
                f"{getattr(adapter, 'id', hid) if adapter else hid} "
                "headless_plan returned None"
            ),
            fallback_reason=none_reason or fallback_reason,
        )

    if timeout is not None and plan.timeout != int(timeout):
        plan = replace(plan, timeout=int(timeout))

    parse_fn = getattr(adapter, 'parse_headless_output', None)
    reason = none_reason or fallback_reason
    if handle is not None or cancel_event is not None:
        result = run_headless_cancellable(
            plan, parse_fn, settings=settings,
            cancel_event=cancel_event, handle=handle,
        )
    else:
        result = run_headless(plan, parse_fn, settings=settings)
    return _stamp_fallback(result, reason)
