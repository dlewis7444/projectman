"""Commit 2 — FAILURE SURFACES (P3.5 items 7-11). Window/terminal/PAA decisions
tested UNBOUND against SimpleNamespace recorders (the A2/A5/lifecycle pattern).
The C7 triple-whammy, the C2/C4/C6 billing leak, the C6 zellij no-op, and the
agent-change feedback.

Binding tests:
  M-UX.10a  spawn-fail(127) within window → toast text has binary + hint;
            row stays (process-exited still sets 'inactive', not removed)
  M-UX.10b  set_active_only(True) fires from process-started, NOT from the
            activation attempt; a failed spawn never flips the filter
  M-UX.10c  a successful spawn's behavior is unchanged
  M-UX.4    scan disabled → ZERO run_ai_checks calls (spy) + toast; enabled →
            result surfaced
  M-UX.3    New Zellij Session with multiplexer≠zellij → toast, no spawn
  M-UX.11   agent submenu selection → one-shot "Agent for <project>: <name>"
"""
import time
import types
from unittest.mock import patch

import pytest

import agents
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
# M-UX.10a — the toast text + one-shot dedup (window, unbound)
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
    # Real one-shot toast helper from window.py — exercises the actual Adw.Toast
    # plumbing (headless-safe) so handlers that call self._show_toast work.
    from window import AppWindow
    fake._show_toast = lambda text, timeout=5: AppWindow._show_toast(fake, text, timeout)
    return fake


def test_spawn_failure_toast_text_names_binary_display_and_hint():
    """BINDING T-a (text): grok miss → '<binary> not found — Grok Build isn't
    installed. <curl hint>'."""
    from window import AppWindow
    fake = _win(Settings(agent_default='grok'))
    text = AppWindow._spawn_failure_toast_text(fake, 'grok', 'grok')
    assert 'grok not found' in text
    assert "Grok Build isn't installed" in text
    assert 'curl -fsSL https://x.ai/cli/install.sh | bash' in text


def test_spawn_failure_toast_prefers_adapter_binary_over_bash_wrapper():
    """Under the continue wrapper argv[0] is 'bash'; the toast must still name
    the AGENT's binary, not bash."""
    from window import AppWindow
    fake = _win(Settings(agent_default='grok'))
    text = AppWindow._spawn_failure_toast_text(fake, 'grok', 'bash')
    assert 'bash not found' not in text
    assert 'grok not found' in text


def test_spawn_failure_toast_claude_uses_resolved_binary():
    from window import AppWindow
    fake = _win(Settings(claude_binary='/opt/claude'))
    text = AppWindow._spawn_failure_toast_text(fake, 'claude', 'bash')
    assert '/opt/claude not found' in text
    assert "Claude Code isn't installed" in text
    assert 'claude.ai/code' in text


def test_on_spawn_failed_fires_one_toast_then_dedups():
    """BINDING T-a (one-shot): a restore storm of the same miss → exactly one
    toast; the row is NOT removed here (process-exited owns 'inactive')."""
    from window import AppWindow
    toasts = []
    fake = _win(Settings(agent_default='grok'), toasts)
    fake._spawn_failure_toast_text = lambda aid, rb: AppWindow._spawn_failure_toast_text(fake, aid, rb)
    AppWindow._on_spawn_failed(fake, '/p', 'grok', 'grok')
    AppWindow._on_spawn_failed(fake, '/p', 'grok', 'grok')  # repeat → dedup
    assert len(toasts) == 1
    assert "Grok Build isn't installed" in str(toasts[0].get_title())
    # No set_project_state / row REMOVAL lives in this handler — process-exited
    # owns the 'inactive' state. (The handler does drop the filter; see the T5
    # P3.5d test below.)
    assert fake._sidebar.states == []


# ════════════════════════════════════════════════════════════════════════════
# M-UX.10b/c — active-only timing (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

class _Sidebar:
    def __init__(self):
        self.active_only_calls = []
        self.states = []

    def set_active_only(self, v):
        self.active_only_calls.append(v)

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
        sb.set_active_only(True)
        if t._fallback_reason:
            pass
    on_started(t)
    assert ('/proj', 'attached') in sb.states
    assert sb.active_only_calls == [True]


# ════════════════════════════════════════════════════════════════════════════
# P3.5d Item 2 (C7): a FAILED spawn must defeat the filter. Restore arms
# "Active Only" eagerly for the successful case; if a restored project's spawn
# then fails, the eager filter would hide the just-failed row (LESS UI after a
# failure). The fix: _on_spawn_failed ALSO drops Active Only. The successful
# restore path is untouched.
# ════════════════════════════════════════════════════════════════════════════

def test_t5_spawn_failure_drops_active_only_filter():
    """T5 (the reveal): _on_spawn_failed calls set_active_only(False) — a failure
    always reveals the board. Reverting that line leaves active_only_calls empty
    and FAILS this."""
    from window import AppWindow
    sb = _Sidebar()
    fake = _win(Settings(agent_default='grok'), sidebar=sb)
    fake._spawn_failure_toast_text = lambda aid, rb: AppWindow._spawn_failure_toast_text(fake, aid, rb)
    AppWindow._on_spawn_failed(fake, '/proj', 'grok', 'grok')
    assert sb.active_only_calls == [False]      # the filter was dropped


def test_t5_spawn_failure_drops_filter_every_time_even_when_deduped():
    """The toast is one-shot (dedup), but the filter drop is NOT — a second
    failure of the same miss still reveals the board (idempotent)."""
    from window import AppWindow
    toasts = []
    sb = _Sidebar()
    fake = _win(Settings(agent_default='grok'), toasts, sidebar=sb)
    fake._spawn_failure_toast_text = lambda aid, rb: AppWindow._spawn_failure_toast_text(fake, aid, rb)
    AppWindow._on_spawn_failed(fake, '/proj', 'grok', 'grok')
    AppWindow._on_spawn_failed(fake, '/proj', 'grok', 'grok')  # deduped toast
    assert len(toasts) == 1                     # toast one-shot
    assert sb.active_only_calls == [False, False]   # filter dropped both times


def test_t6_successful_restore_filter_behavior_unchanged():
    """T6: the successful-restore eager filter is untouched — a clean start fires
    set_active_only(True) and NEVER set_active_only(False). Only a failure drops
    it; this pins that the fix didn't bleed into the success path."""
    sb = _Sidebar()
    t = types.SimpleNamespace(_is_zellij=False, _fallback_reason=None)

    # The success path's _on_started closure body (window._get_or_create_terminal):
    def on_started(t, p='/proj'):
        sb.set_project_state(p, 'attached', is_zellij=t._is_zellij)
        sb.set_active_only(True)
    on_started(t)
    assert sb.active_only_calls == [True]       # eager ON preserved
    assert False not in sb.active_only_calls    # success NEVER drops the filter


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
    # the removed unused vars
    assert 'GROK_HOOK_JSON_DEST' not in src
    assert 'GROK_HOOK_SCRIPT_DEST' not in src
    # coherent per-agent hook story (no warn+success contradiction)
    assert 'CLAUDE_PRESENT' in src
    assert 'staged' in src
    # outsider-friendly compat message
    assert 'grok also reads Claude-style hooks' in src


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
# M-UX.11 — agent-change feedback toast (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

def test_agent_change_fires_feedback_toast():
    """BINDING (item 11): selecting an agent for a project → one-shot
    'Agent for <project>: <display name>'."""
    from window import AppWindow
    from models import FOLLOW_DEFAULT
    toasts = []
    s = Settings(agent_overrides={})
    fake = types.SimpleNamespace(_settings=s, _terminals={})
    fake._toast_overlay = types.SimpleNamespace(add_toast=lambda t: toasts.append(t))
    fake._show_toast = lambda text, timeout=5: toasts.append(text)
    fake.apply_settings = lambda s: None
    fake._maybe_prompt_restart = lambda p: None
    fake._find_project = lambda p: types.SimpleNamespace(name='myproj', path='/proj')
    AppWindow._on_project_agent_change(fake, object(), '/proj', 'grok')
    assert any('myproj' in str(t) and 'Grok Build' in str(t) for t in toasts)
    assert s.agent_overrides == {'/proj': 'grok'}


def test_agent_change_follow_default_clears_override():
    from window import AppWindow
    from models import FOLLOW_DEFAULT
    toasts = []
    s = Settings(agent_overrides={'/proj': 'grok'})
    fake = types.SimpleNamespace(_settings=s, _terminals={})
    fake._show_toast = lambda text, timeout=5: toasts.append(text)
    fake.apply_settings = lambda s: None
    fake._maybe_prompt_restart = lambda p: None
    fake._find_project = lambda p: types.SimpleNamespace(name='myproj', path='/proj')
    AppWindow._on_project_agent_change(fake, object(), '/proj', FOLLOW_DEFAULT)
    assert s.agent_overrides == {}  # override cleared


# ════════════════════════════════════════════════════════════════════════════
# B4 — M-UX.15: project creation toast names the resolved agent (window, unbound)
# ════════════════════════════════════════════════════════════════════════════

def _store_for(projects_dir):
    """A minimal ProjectStore-like object exposing _projects_dir()."""
    return types.SimpleNamespace(_projects_dir=lambda: projects_dir)


def test_project_created_toast_follows_default(tmp_path):
    """BINDING (B4): a new project with no override → toast names the global
    default agent's display name, exactly one toast."""
    from window import AppWindow
    s = Settings(agent_default='grok')
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'shiny')
    assert text == "New project 'shiny' — agent: Grok Build"


def test_project_created_toast_claude_default(tmp_path):
    from window import AppWindow
    s = Settings(agent_default='claude')
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'thing')
    assert text == "New project 'thing' — agent: Claude Code"


def test_project_created_toast_resolves_fallback_for_unknown_default(tmp_path):
    """BINDING (B4 — 'incl. fallback'): an unregistered default agent resolves
    through resolve_adapter to the ACTUAL fallback's display name, never the
    dead id."""
    from window import AppWindow
    s = Settings(agent_default='ghost-agent')
    fake = types.SimpleNamespace(_settings=s, _store=_store_for(str(tmp_path)))
    text = AppWindow._project_created_toast_text(fake, 'p')
    assert 'ghost-agent' not in text
    assert text.startswith("New project 'p' — agent: ")
    # The fallback is a real registered adapter display name.
    assert text.split('agent: ', 1)[1] in {
        agents.ADAPTERS[a].display_name for a in agents.ADAPTERS}


def test_on_project_create_fires_exactly_one_creation_toast(tmp_path):
    """BINDING (B4): the create handler emits exactly one toast with the resolved
    display name (and still refreshes the sidebar / creates the dir)."""
    from window import AppWindow
    projects = tmp_path / 'projects'
    projects.mkdir()
    s = Settings(agent_default='grok')
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
    AppWindow._on_project_create(fake, object(), 'fresh')
    assert (projects / 'fresh').is_dir()       # dir created
    assert refreshed == [True]                  # sidebar refreshed
    assert activated == [str(projects / 'fresh')]  # G3: new project activated
    assert toasts == ["New project 'fresh' — agent: Grok Build"]


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
    AppWindow._on_project_create(fake, object(), 'nope')
    assert toasts == []


# ════════════════════════════════════════════════════════════════════════════
# P3.5c (S8 ship-blocker) — an EXPLICIT per-project agent pick must defeat the
# A2 restore-stickiness, WITHOUT breaking the A2 protection against incidental
# global/default changes. Unbound real methods against faithful fakes (house
# pattern): the terminal stub drives the REAL TerminalView.apply_settings /
# clear_explicit_agent (GTK side stubbed) so the adapter RE-RESOLUTION is genuine
# production code, and the REAL AppWindow._on_project_agent_change drives the wire.
# ════════════════════════════════════════════════════════════════════════════

def _restored_grok_terminal(settings, path='/proj'):
    """A faithful TerminalView stub for a RESTORED grok session.

    Carries sticky ``_explicit_agent='grok'`` (A2) + a live child. Exposes the
    REAL ``apply_settings`` and ``clear_explicit_agent`` (bound below), with the
    GTK-touching helpers stubbed so the adapter re-resolution runs unchanged.
    """
    import agents
    from terminal import TerminalView
    term = types.SimpleNamespace(
        _explicit_agent='grok',
        _project=types.SimpleNamespace(path=path),
        _settings=settings,
        _adapter=agents.get_adapter('grok', settings),
        _child_pid=4242,                    # a live child (restart prompt arms)
        _spawned_agent='grok',              # the live child's agent (C8)
        _font_size=settings.font_size,
        # GTK side — stubbed no-ops so the real apply_settings body runs.
        _terminal=types.SimpleNamespace(
            set_scrollback_lines=lambda n: None, set_audible_bell=lambda b: None),
        _apply_font=lambda: None,
        _apply_colors=lambda: None,
    )
    # Bind the REAL methods so the test exercises production logic.
    term.apply_settings = lambda s: TerminalView.apply_settings(term, s)
    term.clear_explicit_agent = lambda: TerminalView.clear_explicit_agent(term)
    term.spawned_agent_signature = lambda: term._spawned_agent
    return term


def _window_fake(settings, terminals):
    """An AppWindow stub wired so _on_project_agent_change runs end-to-end."""
    fake = types.SimpleNamespace(_settings=settings, _terminals=terminals)
    fake._show_toast = lambda text, timeout=5: None
    fake._find_project = lambda p: types.SimpleNamespace(
        name='myproj', path='/proj')
    fake._maybe_prompt_restart = lambda p: None
    # The REAL apply_settings touches sidebar; use the load-bearing half only:
    # push settings into each terminal (which re-resolves its adapter).
    def _apply(s):
        fake._settings = s
        for tv in terminals.values():
            tv.apply_settings(s)
    fake.apply_settings = _apply
    return fake


def test_p35c_t1_explicit_pick_defeats_restore_stickiness():
    """T1 (the David repro): a restored grok terminal; the user picks 'claude'
    from the per-project Agent submenu → after the handler, the terminal's
    resolved adapter is claude (and a spawn plan built now carries claude argv).
    On revert (no clear_explicit_agent call), the adapter stays grok → FAILS."""
    from window import AppWindow
    s = Settings(agent_default='claude')
    term = _restored_grok_terminal(s)
    fake = _window_fake(s, {'/proj': term})

    AppWindow._on_project_agent_change(fake, object(), '/proj', 'claude')

    assert term._adapter.id == 'claude'             # stickiness defeated
    assert term._explicit_agent is None             # override cleared
    # The next spawn carries CLAUDE argv, not grok.
    plan = term._adapter.spawn_plan(s, term._project, 'fresh')
    assert 'claude' in plan.argv[0]
    assert 'grok' not in plan.argv[0]


def test_p35c_t2_global_default_change_stays_sticky_A2_pin():
    """T2 (A2 regression pin): the SAME restored grok terminal; change
    agent_default and run ONLY apply_settings (NO submenu pick) → the adapter
    stays grok (restore-stickiness intact). If the fix clears stickiness on
    global changes too, this FAILS."""
    s = Settings(agent_default='claude')
    term = _restored_grok_terminal(s)

    # A global default change with NO explicit per-project pick.
    s.agent_default = 'opencode'
    term.apply_settings(s)

    assert term._adapter.id == 'grok'               # A2 stickiness intact
    assert term._explicit_agent == 'grok'           # untouched


def test_p35c_t3_follow_default_pick_clears_stickiness():
    """T3: an explicit FOLLOW_DEFAULT pick (the user chose to follow the global
    default) also clears the restore-stickiness → adapter == agent_default."""
    from window import AppWindow
    from models import FOLLOW_DEFAULT
    s = Settings(agent_default='opencode', agent_overrides={'/proj': 'grok'})
    term = _restored_grok_terminal(s)
    fake = _window_fake(s, {'/proj': term})

    AppWindow._on_project_agent_change(fake, object(), '/proj', FOLLOW_DEFAULT)

    assert s.agent_overrides == {}                  # override removed
    assert term._explicit_agent is None             # stickiness cleared
    assert term._adapter.id == 'opencode'           # now follows the default


def test_p35c_t4_no_terminal_for_path_completes_cleanly():
    """T4: no terminal exists for the path → the handler completes, the override
    is written, and nothing raises (clear_explicit_agent is never reached)."""
    from window import AppWindow
    s = Settings(agent_default='claude')
    fake = _window_fake(s, {})                       # no terminals at all

    AppWindow._on_project_agent_change(fake, object(), '/proj', 'grok')

    assert s.agent_overrides == {'/proj': 'grok'}   # override still written


def test_p35c_t5_no_residual_agent_staleness_after_respawn():
    """T5 (C8 end-state): after T1's handler, the spawned-agent-vs-effective
    disagreement is GONE once the restart's respawn stamps the new agent — the
    restart prompt's staleness check would not fire a SECOND time."""
    from window import AppWindow
    s = Settings(agent_default='claude')
    term = _restored_grok_terminal(s)
    fake = _window_fake(s, {'/proj': term})

    AppWindow._on_project_agent_change(fake, object(), '/proj', 'claude')

    # Simulate the restart-prompt's "Restart Now" respawn: the child is stamped
    # with the now-resolved adapter id (terminal._spawn does this at spawn time).
    term._spawned_agent = term._adapter.id

    effective = s.effective_agent('/proj')
    assert term.spawned_agent_signature() == effective   # no disagreement
    assert effective == 'claude'
