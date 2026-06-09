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

pytestmark = pytest.mark.skipif(
    not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'),
    reason='requires a display (DISPLAY or WAYLAND_DISPLAY)'
)


def _make_tv(settings=None, path='/tmp/test'):
    from settings import Settings
    from model import Project
    from terminal import TerminalView
    proj = Project(name=os.path.basename(path), path=path)
    return TerminalView(proj, settings or Settings())


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
