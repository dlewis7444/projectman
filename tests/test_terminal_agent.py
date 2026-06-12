# tests/test_terminal_agent.py
"""Consumption-seam tests: TerminalView routes spawns through its adapter.

Like test_terminal_zellij.py these construct a real Vte-backed TerminalView,
so they require a display and are skipped without one. They mock ``_spawn`` to
capture the argv/env the adapter produced — no fork, no real claude, no sleeps —
and assert the ``spawn_claude`` back-compat alias maps the old flags onto the
right spawn mode, byte-for-byte with the pre-seam behavior.
"""
import os
import pytest
from unittest.mock import patch
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
gi.require_version('GLib', '2.0')
from gi.repository import Gtk, GLib

from display_gate import requires_display

pytestmark = requires_display


def _make_tv(settings=None, path='/tmp/test', agent_id=None):
    from settings import Settings
    from model import Project
    from terminal import TerminalView
    proj = Project(name=os.path.basename(path), path=path)
    return TerminalView(proj, settings or Settings(), agent_id=agent_id)


# Goldens — identical to tests/test_agent_seam.py (the pre-seam argv).
GOLDEN_CONTINUE = [
    'bash', '-c',
    'trap \'exit 143\' TERM HUP; claude -c; s=$?; '
    '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec claude',
]


def test_adapter_constructed_for_project():
    tv = _make_tv()
    assert tv._adapter.id == 'claude'


def test_spawn_claude_continue_routes_through_adapter():
    """Default spawn_claude() → continue mode → the golden trap wrapper."""
    tv = _make_tv()
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv, env=env)):
        tv.spawn_claude(project_name='test')
    assert captured['argv'] == GOLDEN_CONTINUE
    assert captured['env'] is None  # native model → no env override


def test_spawn_claude_fresh_routes_to_fresh_mode():
    tv = _make_tv()
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_claude(fresh=True, project_name='test')
    assert captured['argv'] == ['claude']


def test_spawn_claude_resume_routes_to_resume_mode():
    tv = _make_tv()
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_claude(session_id='sess-xyz', project_name='test')
    assert captured['argv'] == ['claude', '--resume', 'sess-xyz']


def test_spawn_agent_sets_fallback_reason_from_plan():
    """A custom-model project with ccr missing → env None, fallback surfaced."""
    from settings import Settings
    s = Settings(
        providers={'ollama': {'name': 'O', 'base_url': 'http://h/v1',
                              'api_key': 'k', 'models': {'q': {'name': 'Q'}}}},
        model_default='ollama/q',
    )
    tv = _make_tv(settings=s, path='/tmp/custcommodel')
    captured = {}
    with patch('ccr.available', return_value=False), \
         patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv, env=env)):
        tv.spawn_agent('continue')
    assert captured['env'] is None
    assert tv._fallback_reason  # explanatory string set for window.py's toast


def test_spawn_claude_custom_binary_continue():
    from settings import Settings
    tv = _make_tv(settings=Settings(claude_binary='/opt/claude'), path='/tmp/cb')
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_claude(project_name='cb')
    assert captured['argv'] == [
        'bash', '-c',
        "trap 'exit 143' TERM HUP; /opt/claude -c; s=$?; "
        '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec /opt/claude',
    ]


def test_zellij_flag_file_contains_continue_command(tmp_path, monkeypatch):
    """spawn_zellij writes the adapter's continue command into the flag file
    (new session path), and the wrapper script is the generalized one."""
    import terminal
    # Redirect ~/.ProjectMan writes into tmp via HOME.
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.ProjectMan').mkdir(parents=True, exist_ok=True)
    tv = _make_tv(path='/tmp/zz')
    session = 'pm-zz'
    captured = {}
    with patch('terminal.zellij.session_alive', return_value=False), \
         patch('terminal.zellij.socket_dir', return_value=str(tmp_path / 'sock')), \
         patch.object(tv, '_spawn',
                      side_effect=lambda cmd, env=None: captured.update(cmd=cmd, env=env)):
        (tmp_path / 'sock').mkdir(parents=True, exist_ok=True)
        tv.spawn_zellij(session)
    flag = tmp_path / '.ProjectMan' / f'.zellij-init-{session}'
    assert flag.exists()
    assert flag.read_text() == 'claude -c || claude'
    wrapper = tmp_path / '.ProjectMan' / 'zellij-shell-init.sh'
    assert wrapper.exists()
    body = wrapper.read_text()
    assert 'eval "$CMD"' in body
    assert 'claude' not in body  # generalized — no hardcoded agent


# ── A2: explicit construction-time agent overrides effective_agent ────────────

class _FakeAdapter:
    """A stand-in second adapter so A2's override can be proven before opencode
    exists. Registered into agents.ADAPTERS for the duration of a test."""
    id = 'fake'
    display_name = 'Fake'

    def __init__(self):
        import agents
        self.caps = agents.AgentCaps(continue_=True, resume_by_id=True,
                                     sessions=True, model_select=True)

    def spawn_plan(self, settings, project, mode, session_id=None):
        import agents
        return agents.SpawnPlan(argv=['fake-agent'], env=None, fallback_reason=None)

    def zellij_continue_command(self, settings):
        return 'fake-agent -c || fake-agent'

    def zellij_spawn_env(self, settings, project):
        return (None, None)


def test_explicit_agent_overrides_settings(monkeypatch):
    """saved-agent-wins (A2): an explicit agent_id at construction beats
    settings.effective_agent (which says claude here)."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    s = Settings(agent_default='claude')   # settings say claude
    tv = _make_tv(settings=s, path='/tmp/restored', agent_id='fake')
    assert tv._adapter.id == 'fake'        # the saved agent won


def test_no_explicit_agent_follows_settings(monkeypatch):
    """settings-wins on a new activation (A2): no agent_id → effective_agent."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    s = Settings(agent_default='fake')     # settings default is the fake agent
    tv = _make_tv(settings=s, path='/tmp/new', agent_id=None)
    assert tv._adapter.id == 'fake'


def test_v1_session_no_agent_restores_claude():
    """A v1 (agent-less) restore yields agent_id='claude' upstream; constructing
    with that id resolves the claude adapter."""
    from settings import Settings
    tv = _make_tv(settings=Settings(agent_default='claude'),
                  path='/tmp/legacy', agent_id='claude')
    assert tv._adapter.id == 'claude'


def test_explicit_agent_sticky_across_apply_settings(monkeypatch):
    """A restored agent must not silently swap when settings change later."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    tv = _make_tv(settings=Settings(agent_default='claude'),
                  path='/tmp/sticky', agent_id='fake')
    assert tv._adapter.id == 'fake'
    # Settings now default to claude with no override for this path.
    tv.apply_settings(Settings(agent_default='claude'))
    assert tv._adapter.id == 'fake'       # still the restored agent


def test_explicit_agent_spawn_uses_that_adapter(monkeypatch):
    """The override actually drives the spawn argv (not just the stored id)."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    tv = _make_tv(settings=Settings(agent_default='claude'),
                  path='/tmp/spawn', agent_id='fake')
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_continue(project_name='spawn')
    assert captured['argv'] == ['fake-agent']


# ── FB-9 (P3.5e): sticky-agent lifetime = SESSION lifetime (the David repro) ───
# Construction-time _explicit_agent (A2) is cleared when the child TRULY ends and
# re-resolved at the next spawn, so a deactivate→reactivate honors a pending
# per-project override; a detach/reattach keeps it. Full round-trip here (real
# TerminalView, display-gated); the clear/preserve funnel is unit-tested headless
# in test_terminal_lifetime.py.

def test_fb9_deactivate_reactivate_honors_pending_override(monkeypatch):
    """T-a (THE DAVID REPRO): a restored-grok session (explicit agent 'fake')
    whose project is NOW overridden to claude, deactivated then reactivated, must
    spawn CLAUDE. Reverting the FB-9 clear leaves _explicit_agent='fake' and the
    reactivation re-spawns the fake agent → this FAILS with the verbatim repro."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    # Restored as the fake agent, but settings now OVERRIDE this project → claude.
    s = Settings(agent_default='claude', agent_overrides={'/tmp/react': 'claude'})
    tv = _make_tv(settings=s, path='/tmp/react', agent_id='fake')
    assert tv._adapter.id == 'fake'            # restored agent at construction
    assert s.effective_agent('/tmp/react') == 'claude'   # override is pending
    # --- deactivate: the child ends (SIGTERM → exit). Simulate the exit funnel.
    tv._child_pid = 4242
    tv._fire_exit_if_current(4242, 0)
    assert tv._explicit_agent is None          # FB-9: the dead session's agent dropped
    # --- reactivate: spawn_continue. It must re-resolve to the pending override.
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_continue(project_name='react')
    assert tv._adapter.id == 'claude'          # the override won
    assert captured['argv'] != ['fake-agent']  # NOT the dead restore agent


def test_fb9_detach_reattach_keeps_explicit_agent(monkeypatch):
    """T-b: a zellij DETACH preserves the restore agent — a reattach resumes the
    same agent. Over-clearing on detach FAILS this."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    s = Settings(agent_default='claude', agent_overrides={'/tmp/det': 'claude'})
    tv = _make_tv(settings=s, path='/tmp/det', agent_id='fake')
    tv._child_pid = 4242
    tv._is_zellij = True
    tv._zellij_session = 'pm-det'
    with patch('terminal.zellij.session_alive', return_value=True):
        tv._fire_exit_if_current(4242, 0)      # session still alive → DETACH
    assert tv._explicit_agent == 'fake'        # preserved through detach


def test_fb9_a2_restore_pin_unchanged(monkeypatch):
    """T-c: A2 is intact — a restored session that is NEVER ended keeps its
    explicit agent across an incidental settings change (the original A2 pin)."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    tv = _make_tv(settings=Settings(agent_default='claude'),
                  path='/tmp/a2', agent_id='fake')
    tv.apply_settings(Settings(agent_default='claude'))
    assert tv._adapter.id == 'fake'            # still sticky (no end occurred)
    # And a spawn (e.g. an explicit restart of the SAME restored session) still
    # uses the restored agent — A2 saved-agent-wins on restart.
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_fresh(project_name='a2')
    assert captured['argv'] == ['fake-agent']


def test_fb9_natural_exit_then_reactivate_follows_effective_agent(monkeypatch):
    """T-d: a natural exit (no override pending) → the next activation follows
    settings.effective_agent (here claude), proving the clear+re-resolve is the
    general behavior, not a special-case of the override path."""
    import agents
    from settings import Settings
    monkeypatch.setitem(agents.ADAPTERS, 'fake', _FakeAdapter())
    s = Settings(agent_default='claude')       # no per-project override
    tv = _make_tv(settings=s, path='/tmp/nat', agent_id='fake')
    tv._child_pid = 4242
    tv._fire_exit_if_current(4242, 0)          # natural exit
    assert tv._explicit_agent is None
    captured = {}
    with patch.object(tv, '_spawn',
                      side_effect=lambda argv, env=None: captured.update(argv=argv)):
        tv.spawn_continue(project_name='nat')
    assert tv._adapter.id == 'claude'          # follows effective_agent now


# ── A3: zellij env comes through the adapter, including custom-model fallback ──

def test_zellij_custom_model_ccr_missing_sets_fallback(tmp_path, monkeypatch):
    """spawn_zellij (create path) surfaces the adapter's ccr fallback reason —
    proving the env decision rides zellij_spawn_env, not a terminal-local call."""
    import terminal
    from settings import Settings
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.ProjectMan').mkdir(parents=True, exist_ok=True)
    s = Settings(
        providers={'ollama': {'name': 'O', 'base_url': 'http://h/v1',
                              'api_key': 'k', 'models': {'q': {'name': 'Q'}}}},
        model_default='ollama/q',
    )
    tv = _make_tv(settings=s, path='/tmp/zcustom')
    captured = {}
    with patch('terminal.zellij.session_alive', return_value=False), \
         patch('terminal.zellij.socket_dir', return_value=str(tmp_path / 'sock')), \
         patch('ccr.available', return_value=False), \
         patch.object(tv, '_spawn',
                      side_effect=lambda cmd, env=None: captured.update(cmd=cmd, env=env)):
        (tmp_path / 'sock').mkdir(parents=True, exist_ok=True)
        tv.spawn_zellij('pm-zcustom')
    # ccr missing → native env (None custom → inherits os.environ) BUT the
    # fallback reason is set for window.py's toast.
    assert tv._fallback_reason
    # The spawn still happened with a SHELL-wrapped env (zellij create path).
    assert captured['env'] is not None
    assert captured['env']['SHELL'].endswith('zellij-shell-init.sh')


def test_zellij_attach_path_clears_fallback(tmp_path, monkeypatch):
    """Attaching to a live session never consults the adapter env path, so a
    stale fallback reason must not survive into a healthy attach."""
    import terminal
    from settings import Settings
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.ProjectMan').mkdir(parents=True, exist_ok=True)
    tv = _make_tv(settings=Settings(), path='/tmp/zattach')
    tv._fallback_reason = 'stale reason from a prior failed spawn'
    captured = {}
    with patch('terminal.zellij.session_alive', return_value=True), \
         patch.object(tv, '_spawn',
                      side_effect=lambda cmd, env=None: captured.update(cmd=cmd, env=env)):
        tv.spawn_zellij('pm-zattach')
    assert tv._fallback_reason is None
    assert captured['cmd'] == ['zellij', 'attach', 'pm-zattach']
    assert captured['env'] is None
