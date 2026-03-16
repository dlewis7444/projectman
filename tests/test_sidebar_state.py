# tests/test_sidebar_state.py
import pytest
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

# Bootstrap GTK (required before instantiating widgets)
app = Adw.Application(application_id='com.test.pm')


def _make_row():
    from model import Project, HistoryReader, StatusWatcher
    from sidebar import ProjectRow
    proj = Project(name='test', path='/tmp/test')
    history = HistoryReader()
    watcher = StatusWatcher()
    return ProjectRow(proj, history, watcher)


def test_initial_state_is_inactive():
    row = _make_row()
    assert row._process_state == 'inactive'


def test_set_process_state_attached():
    row = _make_row()
    row.set_process_state('attached')
    assert row._process_state == 'attached'


def test_set_process_state_detached():
    row = _make_row()
    row.set_process_state('detached')
    assert row._process_state == 'detached'
    # name label should have detached CSS class
    assert row._name_label.has_css_class('project-row-detached')


def test_set_process_state_back_to_inactive_clears_css():
    row = _make_row()
    row.set_process_state('detached')
    row.set_process_state('inactive')
    assert not row._name_label.has_css_class('project-row-detached')


def test_deactivate_action_enabled_only_when_attached():
    # GLib booleans are not Python True/False by identity — use bool()
    row = _make_row()
    row.set_process_state('attached')
    assert bool(row._deactivate_action.get_enabled()) is True
    row.set_process_state('detached')
    assert bool(row._deactivate_action.get_enabled()) is False
    row.set_process_state('inactive')
    assert bool(row._deactivate_action.get_enabled()) is False


def test_update_status_attached_no_file_shows_done():
    """Attached session with no status file falls back to status-done (green)."""
    row = _make_row()
    # watcher has no status file — get_project_status returns 'idle'
    row.set_process_state('attached')
    assert row._status_dot.has_css_class('status-done')
    assert not row._status_dot.has_css_class('status-idle')
