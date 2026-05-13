# tests/test_terminal_zellij.py
"""
Tests for TerminalView's zellij detach detection.
These tests call _fire_exit_if_current directly with mocked session_alive,
so they do NOT need a real display.  However, TerminalView.__init__ does
construct a Vte.Terminal widget, which requires a display.  Run with:
  DISPLAY=:0 pytest tests/test_terminal_zellij.py
or mark as integration tests if running in CI without a display.
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


def _make_tv():
    """Create a minimal TerminalView — requires a display."""
    from settings import Settings
    from model import Project
    from terminal import TerminalView
    proj = Project(name='test', path='/tmp/test')
    tv = TerminalView(proj, Settings())
    return tv


def test_child_exited_session_alive_emits_detached():
    tv = _make_tv()
    tv._child_pid = 99  # simulate was running
    tv._is_zellij = True
    tv._zellij_session = 'pm-test'

    detached_fired = []
    exited_fired = []
    tv.connect('process-detached', lambda t: detached_fired.append(True))
    tv.connect('process-exited',   lambda t, s: exited_fired.append(True))

    with patch('terminal.zellij.session_alive', return_value=True):
        tv._fire_exit_if_current(99, 0)

    assert detached_fired == [True]
    assert exited_fired == []


def test_child_exited_session_gone_emits_exited():
    tv = _make_tv()
    tv._child_pid = 99
    tv._is_zellij = True
    tv._zellij_session = 'pm-test'

    detached_fired = []
    exited_fired = []
    tv.connect('process-detached', lambda t: detached_fired.append(True))
    tv.connect('process-exited',   lambda t, s: exited_fired.append(True))

    with patch('terminal.zellij.session_alive', return_value=False):
        tv._fire_exit_if_current(99, 0)

    assert detached_fired == []
    assert exited_fired == [True]


def test_child_exited_non_zellij_always_emits_exited():
    tv = _make_tv()
    tv._child_pid = 99
    tv._is_zellij = False

    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))

    tv._fire_exit_if_current(99, 0)
    assert exited_fired == [True]


def test_fire_exit_if_current_stale_pid_is_silent():
    """If the watched pid no longer matches _child_pid (respawn happened),
    don't emit process-exited."""
    tv = _make_tv()
    tv._child_pid = 12345
    tv._is_zellij = False
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))
    tv._fire_exit_if_current(99, 0)   # stale pid
    assert exited_fired == []
    assert tv._child_pid == 12345    # unchanged


# ── check_child_alive (defensive sweep for missed VTE child-exited) ──────────

def _mock_proc_status(state_letter):
    """Return a context manager that fakes /proc/<pid>/status reads."""
    from io import StringIO
    return patch('builtins.open',
                 return_value=StringIO(f'Name:\tclaude\nState:\t{state_letter} (foo)\n'))


def test_check_child_alive_no_pid_is_noop():
    tv = _make_tv()
    tv._child_pid = None
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))
    tv.check_child_alive()
    assert exited_fired == []


def test_check_child_alive_running_does_not_fire():
    tv = _make_tv()
    tv._child_pid = os.getpid()  # this very process — definitely alive
    tv._is_zellij = False
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))
    tv.check_child_alive()
    assert exited_fired == []
    assert tv._child_pid == os.getpid()


def test_check_child_alive_zombie_fires_exited():
    tv = _make_tv()
    tv._child_pid = 999999
    tv._is_zellij = False
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))
    with _mock_proc_status('Z'), patch('os.waitpid', return_value=(999999, 0)):
        tv.check_child_alive()
    assert exited_fired == [True]
    assert tv._child_pid is None


def test_check_child_alive_proc_gone_fires_exited():
    tv = _make_tv()
    tv._child_pid = 999999
    tv._is_zellij = False
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))
    with patch('builtins.open', side_effect=FileNotFoundError), \
         patch('os.waitpid', side_effect=ChildProcessError):
        tv.check_child_alive()
    assert exited_fired == [True]
    assert tv._child_pid is None


def test_check_child_alive_respawn_race_does_not_double_fire():
    """If _child_pid changes during the check (a respawn races us), don't
    fire process-exited for the new PID."""
    tv = _make_tv()
    tv._child_pid = 999999
    tv._is_zellij = False
    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))

    def waitpid_swaps_pid(*args, **kwargs):
        tv._child_pid = 12345  # simulate respawn between read and emit
        return (999999, 0)

    with _mock_proc_status('Z'), patch('os.waitpid', side_effect=waitpid_swaps_pid):
        tv.check_child_alive()
    assert exited_fired == []
    assert tv._child_pid == 12345
