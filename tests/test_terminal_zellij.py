# tests/test_terminal_zellij.py
"""
Tests for TerminalView's zellij detach detection.
These tests call _on_child_exited directly with mocked session_exists,
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

    with patch('terminal.zellij.session_exists', return_value=True):
        tv._on_child_exited(tv._terminal, 0)

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

    with patch('terminal.zellij.session_exists', return_value=False):
        tv._on_child_exited(tv._terminal, 0)

    assert detached_fired == []
    assert exited_fired == [True]


def test_child_exited_non_zellij_always_emits_exited():
    tv = _make_tv()
    tv._child_pid = 99
    tv._is_zellij = False

    exited_fired = []
    tv.connect('process-exited', lambda t, s: exited_fired.append(True))

    tv._on_child_exited(tv._terminal, 0)
    assert exited_fired == [True]
