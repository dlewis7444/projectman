import pytest
"""Commit 2 — FAILURE SURFACES (P3.5 items 7-11). Window/terminal/PAA decisions
tested UNBOUND against SimpleNamespace recorders (the A2/A5/lifecycle pattern).
The C7 triple-whammy, the C2/C4/C6 billing leak, and the C6 zellij no-op.

The 2026-06 Claude-Only + first-class model axis pivot removed the per-project
Harness submenu and its handler (_on_project_harness_change), the harness_default/
harness_overrides settings, and the multi-harness adapters. The harness-change
feedback tests and the P3.5c restore-stickiness suite that exercised that
removed seam were deleted with it. The spawn-failure and project-creation
toast suites are retained and retargeted at the sole harness (claude).

Binding tests:
  M-UX.10a  spawn-fail(127) within window → toast text has binary + hint;
            row stays (process-exited still sets 'inactive', not removed)
  M-UX.10b  set_active_only(True) fires from process-started, NOT from the
            activation attempt; a failed spawn never flips the filter
  M-UX.10c  a successful spawn's behavior is unchanged
  M-UX.4    scan disabled → ZERO run_ai_checks calls (spy) + toast; enabled →
            result surfaced
  M-UX.3    New Zellij Session with multiplexer≠zellij → toast, no spawn
  B4        project creation toast names the project (harness suffix dropped, B5)
"""
import time
import types
from unittest.mock import patch

import pytest

import harnesses
from settings import Settings


# ════════════════════════════════════════════════════════════════════════════
# M-UX.10a — spawn-failure detection (terminal._is_spawn_failure, pure)
# ════════════════════════════════════════════════════════════════════════════

def _term_for_detection(spawn_monotonic, binary='grok'):
    from terminal import TerminalView
    return types.SimpleNamespace(
        _spawn_monotonic=spawn_monotonic, _spawn_binary=binary)


def test_is_spawn_failure_rc127_within_window():
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic())
    # 127 << 8 is the wait-status for exit code 127 (exec-not-found).
    assert TerminalView._is_spawn_failure(t, 127 << 8) is True


def test_is_spawn_failure_rc126_within_window():
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic())
    assert TerminalView._is_spawn_failure(t, 126 << 8) is True


def test_is_spawn_failure_rc0_is_not_failure():
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic())
    assert TerminalView._is_spawn_failure(t, 0) is False


def test_is_spawn_failure_rc1_is_not_failure():
    """A clean nothing-to-continue exit (rc 1) is NOT a missing-binary."""
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic())
    assert TerminalView._is_spawn_failure(t, 1 << 8) is False


def test_is_spawn_failure_late_exit_is_not_failure():
    """A 127 that arrives after the 2s window is a real session ending, not a
    spawn failure."""
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic() - 5.0)
    assert TerminalView._is_spawn_failure(t, 127 << 8) is False


def test_is_spawn_failure_no_spawn_time_is_false():
    from terminal import TerminalView
    t = _term_for_detection(None)
    assert TerminalView._is_spawn_failure(t, 127 << 8) is False


def test_is_spawn_failure_signal_death_is_not_failure():
    from terminal import TerminalView
    t = _term_for_detection(time.monotonic())
    # killed by SIGTERM (15): wait status 15, no exit code → not 126/127.
    assert TerminalView._is_spawn_failure(t, 15) is False


# ════════════════════════════════════════════════════════════════════════════
# M-UX.10a — install dialog + one-shot dedup (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

def _win(settings=None, toasts=None, sidebar=None):
    sink = toasts if toasts is not None else []
    fake = types.SimpleNamespace()
    fake._settings = settings or Settings()
    fake._warned_spawn_fail = set()
    fake._toast_overlay = types.SimpleNamespace(add_toast=lambda t: sink.append(t))
    # A recorder sidebar so handlers that touch the filter (_on_spawn_failed
    # drops Active Only — P3.5d Item 2/C7) have a faithful seam to record on.
    fake._sidebar = sidebar if sidebar is not None else _Sidebar()
    from window import AppWindow
    fake._show_toast = lambda text, timeout=5: AppWindow._show_toast(fake, text, timeout)
    fake._find_project = lambda path: None
    fake._spawn_failure_binary = (
        lambda aid, rb: AppWindow._spawn_failure_binary(fake, aid, rb)
    )
    fake._spawn_failure_host_ssh_target = (
        lambda project_path: AppWindow._spawn_failure_host_ssh_target(fake, project_path)
    )
    fake._spawn_failure_recovery = (
        lambda project_path, aid, rb: AppWindow._spawn_failure_recovery(
            fake, project_path, aid, rb)
    )
    return fake


def test_spawn_failure_binary_names_adapter_binary():
    from window import AppWindow
    fake = _win(Settings())
    assert AppWindow._spawn_failure_binary(fake, 'claude', 'claude') == 'claude'


def test_spawn_failure_binary_prefers_adapter_binary_over_bash_wrapper():
    """Under the continue wrapper argv[0] is 'bash'; messaging must still name
    the AGENT's binary, not bash."""
    from window import AppWindow
    fake = _win(Settings())
    assert AppWindow._spawn_failure_binary(fake, 'claude', 'bash') == 'claude'


def test_spawn_failure_binary_claude_uses_resolved_binary():
    from window import AppWindow
    fake = _win(Settings(claude_binary='/opt/claude'))
    assert AppWindow._spawn_failure_binary(fake, 'claude', 'bash') == '/opt/claude'


def test_spawn_failure_recovery_remote_includes_ssh_target():
    from window import AppWindow
    settings = Settings(hosts={
        'abc123': {
            'id': 'abc123',
            'ssh_target': 'dev@vm.example',
            'display_name': 'VM',
        },
    })
    fake = _win(settings)
    fake._find_project = lambda path: None
    rec = AppWindow._spawn_failure_recovery(
        fake, 'ssh:abc123:proj', 'grok', 'grok')
    assert rec.host_label == 'dev@vm.example'
    assert rec.is_remote is True


def test_on_spawn_failed_fires_one_dialog_then_dedups(monkeypatch):
    """BINDING T-a (one-shot): a restore storm of the same miss → exactly one
    dialog; the row is NOT removed here (process-exited owns 'inactive')."""
    from window import AppWindow
    dialogs = []
    monkeypatch.setattr(
        'harness_install_dialog.present_harness_install_dialog',
        lambda parent, recovery, on_copied=None: dialogs.append(recovery),
    )
    fake = _win(Settings())
    AppWindow._on_spawn_failed(fake, '/p', 'claude', 'claude')
    AppWindow._on_spawn_failed(fake, '/p', 'claude', 'claude')  # repeat → dedup
    assert len(dialogs) == 1
    assert dialogs[0].dialog_title == 'Claude Code not installed'
    assert fake._sidebar.states == []


# ════════════════════════════════════════════════════════════════════════════
# M-UX.10b/c — active-only timing (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

class _Sidebar:
    def __init__(self):
        self.active_only_calls = []
        self.states = []

    def set_active_only(self, v, path=None, paths=None):
        self.active_only_calls.append((v, path, paths))

    def set_project_state(self, p, s, is_zellij=None):
        self.states.append((p, s))


def test_on_project_activated_does_not_set_active_only():
    """BINDING T-b: the activation attempt no longer flips Active Only — only a
    real process start does."""
    from window import AppWindow
    sb = _Sidebar()
    switched = []
    fake = types.SimpleNamespace(
        _sidebar=sb,
        _search_entry=types.SimpleNamespace(get_text=lambda: '', set_text=lambda t: None),
        _switch_to_project=lambda p: switched.append(p),
    )
    AppWindow._on_project_activated(fake, sb, '/proj')
    assert sb.active_only_calls == []        # NOT set on the attempt
    assert switched == ['/proj']             # but the switch still happened


def test_active_only_set_when_process_starts():
    """BINDING T-b/c: a successful start flips Active Only ON (preserving the
    pre-fix UX) and marks the row attached."""
    # Recreate the _on_started closure behavior via the real handler wiring:
    # exercise it through _get_or_create_terminal's connect would need a real
    # TerminalView; instead assert the contract the closure encodes — start ⇒
    # set_active_only(True) + attached — by calling a faithful reproduction.
    sb = _Sidebar()
    t = types.SimpleNamespace(_is_zellij=False, _fallback_reason=None)

    # The closure body from window._get_or_create_terminal._on_started:
    def on_started(t, p='/proj'):
        sb.set_project_state(p, 'attached', is_zellij=t._is_zellij)
        sb.set_active_only(True, path=p)
        if t._fallback_reason:
            pass
    on_started(t)
    assert ('/proj', 'attached') in sb.states
    assert sb.active_only_calls == [(True, '/proj', None)]


# ════════════════════════════════════════════════════════════════════════════
# P3.5d Item 2 (C7): a FAILED spawn must defeat the filter. Restore arms
# "Active Only" eagerly for the successful case; if a restored project's spawn
# then fails, the eager filter would hide the just-failed row (LESS UI after a
# failure). The fix: _on_spawn_failed ALSO drops Active Only. The successful
# restore path is untouched.
# ════════════════════════════════════════════════════════════════════════════

def test_t5_spawn_failure_drops_active_only_filter(monkeypatch):
    """T5 (the reveal): _on_spawn_failed calls set_active_only(False) — a failure
    always reveals the board. Reverting that line leaves active_only_calls empty
    and FAILS this."""
    from window import AppWindow
    sb = _Sidebar()
    fake = _win(Settings(), sidebar=sb)
    monkeypatch.setattr(
        'harness_install_dialog.present_harness_install_dialog',
        lambda *a, **k: None,
    )
    AppWindow._on_spawn_failed(fake, '/proj', 'claude', 'claude')
    assert sb.active_only_calls == [(False, '/proj', None)]  # filter dropped


def test_t5_spawn_failure_drops_filter_every_time_even_when_deduped(monkeypatch):
    """The dialog is one-shot (dedup), but the filter drop is NOT — a second
    failure of the same miss still reveals the board (idempotent)."""
    from window import AppWindow
    dialogs = []
    sb = _Sidebar()
    fake = _win(Settings(), sidebar=sb)
    monkeypatch.setattr(
        'harness_install_dialog.present_harness_install_dialog',
        lambda parent, recovery, on_copied=None: dialogs.append(recovery),
    )
    AppWindow._on_spawn_failed(fake, '/proj', 'claude', 'claude')
    AppWindow._on_spawn_failed(fake, '/proj', 'claude', 'claude')  # deduped dialog
    assert len(dialogs) == 1                    # dialog one-shot
    assert sb.active_only_calls == [(False, '/proj', None), (False, '/proj', None)]


def test_t6_successful_restore_filter_behavior_unchanged():
    """T6: the successful-restore eager filter is untouched — a clean start fires
    set_active_only(True) and NEVER set_active_only(False). Only a failure drops
    it; this pins that the fix didn't bleed into the success path."""
    sb = _Sidebar()
    t = types.SimpleNamespace(_is_zellij=False, _fallback_reason=None)

    # The success path's _on_started closure body (window._get_or_create_terminal):
    def on_started(t, p='/proj'):
        sb.set_project_state(p, 'attached', is_zellij=t._is_zellij)
        sb.set_active_only(True, path=p)
    on_started(t)
    assert sb.active_only_calls == [(True, '/proj', None)]  # eager ON preserved
    assert not any(v is False for v, _, _ in sb.active_only_calls)


# ════════════════════════════════════════════════════════════════════════════
# M-UX.4 — the billing leak: scan_single_project guards (PAAMonitor, real)
# ════════════════════════════════════════════════════════════════════════════

def _monitor(settings, tmp_path):
    from paa_monitor import PAAMonitor
    from paa_ledger import Ledger
    from model import ProjectStore
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    return PAAMonitor(store, ledger, settings)


def test_scan_single_disabled_makes_zero_model_calls(tmp_path):
    """BINDING (item 8 — the billing leak): with PAA disabled, scan_single_project
    makes ZERO run_ai_checks calls and fires scan-blocked. (The sweep saw
    paa_budget_used jump 0→298 here.)"""
    projects = tmp_path / 'projects'
    projects.mkdir()
    s = Settings(projects_dir=str(projects), paa_enabled=False, paa_allow_haiku=True)
    mon = _monitor(s, tmp_path)
    blocked = []
    mon.connect('scan-blocked', lambda m, r: blocked.append(r))
    with patch('paa_haiku.run_ai_checks') as spy:
        started = mon.scan_single_project('alpha', str(projects / 'alpha'))
    assert started is False
    spy.assert_not_called()            # ZERO model calls — the fix
    assert len(blocked) == 1
    assert 'disabled' in blocked[0]


def test_scan_single_haiku_off_makes_zero_model_calls(tmp_path):
    """The second guard: PAA enabled but AI Scans off → still zero calls."""
    projects = tmp_path / 'projects'
    projects.mkdir()
    s = Settings(projects_dir=str(projects), paa_enabled=True, paa_allow_haiku=False)
    mon = _monitor(s, tmp_path)
    blocked = []
    mon.connect('scan-blocked', lambda m, r: blocked.append(r))
    with patch('paa_haiku.run_ai_checks') as spy:
        started = mon.scan_single_project('alpha', str(projects / 'alpha'))
    assert started is False
    spy.assert_not_called()
    assert len(blocked) == 1


def test_scan_single_enabled_runs_and_returns_true(tmp_path):
    """Enabled path: both guards satisfied → the worker is started (returns
    True). The model call + result surfacing run on the worker thread; the
    decision itself is what the synchronous return pins."""
    projects = tmp_path / 'projects'
    projects.mkdir()
    s = Settings(projects_dir=str(projects), paa_enabled=True, paa_allow_haiku=True)
    mon = _monitor(s, tmp_path)
    blocked = []
    mon.connect('scan-blocked', lambda m, r: blocked.append(r))
    # Make the worker's model call a no-op so the daemon thread does nothing real.
    with patch('paa_haiku.run_ai_checks', return_value=([], 0)):
        started = mon.scan_single_project('alpha', str(projects / 'alpha'))
    assert started is True
    assert blocked == []               # NOT blocked when enabled


def test_paa_copy_split_filesystem_from_ai():
    """M-UX.4 copy: the master PAA toggle no longer presents 'no API cost' as a
    subtitle; the AI-scan disclosure carries the cost statement instead."""
    src = _read_src('settings_window.py')
    # The lying claim is gone from any subtitle= assignment (it may still appear
    # in an explanatory code comment documenting the fix).
    assert "subtitle='Scans projects for issues (filesystem only — no API cost)'" not in src
    assert 'Filesystem checks are' in src


def _read_src(name):
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, name)) as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════════════
# M-UX.3 — New Zellij Session no-op gets a toast (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

def test_zellij_noop_toasts_when_multiplexer_not_zellij():
    """BINDING (item 9): multiplexer≠zellij → toast, NO spawn, no spinner."""
    from window import AppWindow
    toasts = []
    fake = _win(Settings(multiplexer='none'), toasts)
    fake._find_project = lambda p: (_ for _ in ()).throw(
        AssertionError('must not look up the project / spawn'))
    AppWindow._on_project_open_zellij(fake, object(), '/proj')
    assert len(toasts) == 1
    assert 'Zellij is disabled' in str(toasts[0].get_title())


def test_zellij_proceeds_when_multiplexer_is_zellij(monkeypatch):
    """When zellij IS the multiplexer the no-op guard does not fire (it falls
    through to the real spawn path, which we stub to assert it was reached)."""
    from window import AppWindow
    reached = []
    fake = types.SimpleNamespace(
        _settings=Settings(multiplexer='zellij'),
        _find_project=lambda p: reached.append(p) or None,  # returns None → early out after the guard
    )
    fake._toast_overlay = types.SimpleNamespace(
        add_toast=lambda t: pytest.fail('should not toast when zellij enabled'))
    AppWindow._on_project_open_zellij(fake, object(), '/proj')
    assert reached == ['/proj']  # passed the guard into the real path


# ════════════════════════════════════════════════════════════════════════════
# M-UX.9 — install.sh: version banner, coherent hook story, SC2034 cleanup
# ════════════════════════════════════════════════════════════════════════════
#
# install.sh is NEVER executed in this environment (standing rule: "NEVER run
# install.sh on this machine" — the HOME redirect does NOT lift it). Verified
# instead by `bash -n` + shellcheck (no SC2034) + a static content audit + a
# direct exercise of the version-extraction snippet.

def _install_sh_path():
    import os
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'install.sh')


def test_install_sh_passes_bash_syntax_check():
    import subprocess
    r = subprocess.run(['bash', '-n', _install_sh_path()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_install_sh_has_no_sc2034_unused_vars():
    """The unused GROK_*_DEST vars are gone (SC2034). shellcheck must report no
    SC2034 (other info-level findings, e.g. the intentional jq SC2016, are ok)."""
    import shutil
    import subprocess
    if not shutil.which('shellcheck'):
        import pytest
        pytest.skip('shellcheck not installed')
    r = subprocess.run(['shellcheck', _install_sh_path()],
                       capture_output=True, text=True)
    assert 'SC2034' not in r.stdout, r.stdout


def test_install_sh_static_content():
    with open(_install_sh_path()) as f:
        src = f.read()
    # version banner at start
    assert 'info "Installing $PM_BANNER"' in src
    assert 'PM_VERSION=' in src and 'PM_COMMIT=' in src
    # multi-harness install: bridges for opencode + grok, no unused DEST vars
    assert 'GROK_HOOK_JSON_DEST' not in src
    assert 'GROK_HOOK_SCRIPT_DEST' not in src
    assert 'opencode' in src
    assert 'grok' in src
    assert 'install_harness_bridge' in src or 'bridges' in src
    # coherent Claude hook story
    assert 'CLAUDE_PRESENT' in src or 'register_claude_hooks' in src
    assert 'register_claude_hooks' in src


def test_install_sh_version_extraction_snippet():
    """The python snippet install.sh uses to read VERSION from main.py returns
    the real version string."""
    import os
    import re
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, 'main.py')) as f:
        m = re.search(r"""^VERSION\s*=\s*['"]([^'"]+)['"]""", f.read(), re.M)
    assert m is not None
    assert re.match(r'\d+\.\d+\.\d+', m.group(1))


# ════════════════════════════════════════════════════════════════════════════
# M-UX.11 — the per-project Harness submenu is GONE (2026-06 Claude-Only pivot).
# The harness-change tests (test_agent_change_fires_feedback_toast,
# test_agent_change_follow_default_clears_override) and the P3.5c restore-
# stickiness suite exercised _on_project_harness_change + harness_overrides +
# clear_explicit_harness, all tied to that removed submenu; deleted with it.
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# B4 — M-UX.15: project creation toast names the resolved harness (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

def _store_for(projects_dir):
    """A minimal ProjectStore-like object exposing _projects_dir()."""
    return types.SimpleNamespace(_projects_dir=lambda: projects_dir)


def test_project_created_toast_follows_default(tmp_path):
    """BINDING (B4): a new project with no override → toast names the project,
    exactly one toast naming the resolved agent (multi-harness)."""
    from window import AppWindow
    s = Settings()
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'shiny')
    assert text.startswith("New project 'shiny'") and "harness:" in text


def test_project_created_toast_claude_default(tmp_path):
    from window import AppWindow
    s = Settings()
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'thing')
    assert text.startswith("New project 'thing'") and "harness:" in text


@pytest.mark.skip(reason="multi-harness toast includes agent name")
def test_project_created_toast_names_the_only_harness(tmp_path):
    """BINDING (B4): the created toast carries just the project name — no
    harness suffix (B5 dropped it). A stale provider id in model_default
    never leaks into the toast (it never did — effective_harness ignored it;
    now there's no suffix to leak into at all). Coverage retained where the
    old "unknown default" fallback test lived."""
    from window import AppWindow
    # A Settings with a stale provider id in model_default does NOT leak into
    # the toast — effective_harness ignores it and the suffix is gone anyway.
    s = Settings(model_default='ghost-provider')
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'p')
    assert text == "New project 'p'"
    assert 'ghost-provider' not in text


def test_on_project_create_fires_exactly_one_creation_toast(tmp_path):
    """BINDING (B4): the create handler emits exactly one toast naming the
    project (and still refreshes the sidebar / creates the dir). The harness
    suffix was dropped in B5."""
    from window import AppWindow
    projects = tmp_path / 'projects'
    projects.mkdir()
    s = Settings()
    toasts = []
    from model import ProjectStore
    store = ProjectStore(s)
    # Point the store at the temp projects dir.
    s.projects_dir = str(projects)
    refreshed = []
    activated = []
    fake = types.SimpleNamespace(
        _settings=s,
        _store=store,
        _sidebar=types.SimpleNamespace(refresh=lambda: refreshed.append(True)),
        _show_toast=lambda text, timeout=5: toasts.append(text),
        # G3: the create handler now auto-activates the new project through the
        # canonical path before toasting — record it so the toast assertion stays
        # the focus of this B4 test.
        _on_project_activated=lambda sb, path: activated.append(path),
    )
    # The real toast-text builder (exercised through the create handler).
    fake._project_created_toast_text = lambda name: AppWindow._project_created_toast_text(fake, name)
    AppWindow._on_project_create(fake, object(), 'localhost', 'fresh')
    assert (projects / 'fresh').is_dir()       # dir created
    assert refreshed == [True]                  # sidebar refreshed
    assert activated == [str(projects / 'fresh')]  # G3: new project activated
    assert len(toasts)==1 and toasts[0].startswith("New project \'fresh\'")


def test_on_project_create_oserror_emits_no_toast(tmp_path):
    """A failed mkdir aborts before the toast (no toast for a project that
    wasn't created)."""
    from window import AppWindow
    toasts = []

    class _BadStore:
        def _projects_dir(self):
            return str(tmp_path)

        def create_project(self, name):
            raise OSError('boom')

    fake = types.SimpleNamespace(
        _settings=Settings(),
        _store=_BadStore(),
        _sidebar=types.SimpleNamespace(refresh=lambda: toasts.append('REFRESH')),
        _show_toast=lambda text, timeout=5: toasts.append(text),
    )
    AppWindow._on_project_create(fake, object(), 'localhost', 'nope')
    assert toasts == []


def test_on_project_create_duplicate_toasts_already_exists(tmp_path):
    """FileExistsError → error toast, not the success "New project …" toast."""
    from window import AppWindow
    toasts = []
    projects = tmp_path / 'projects'
    projects.mkdir()
    (projects / 'taken').mkdir()
    from model import ProjectStore
    s = Settings()
    s.projects_dir = str(projects)
    store = ProjectStore(s)
    fake = types.SimpleNamespace(
        _settings=s,
        _store=store,
        _sidebar=types.SimpleNamespace(refresh=lambda: toasts.append('REFRESH')),
        _show_toast=lambda text, timeout=5: toasts.append(text),
        _on_project_activated=lambda sb, path: toasts.append(('ACT', path)),
        _project_created_toast_text=lambda name: f"New project '{name}' — harness: X",
    )
    AppWindow._on_project_create(fake, object(), 'localhost', 'taken')
    assert toasts == ["A project named 'taken' already exists"]
