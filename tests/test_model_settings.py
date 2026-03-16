import os
import json
import time
import pytest
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib
from settings import Settings
from model import ProjectStore, ProjectsWatcher, StatusWatcher, Project


def test_project_store_uses_settings_dir(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'myproject').mkdir()
    projects = store.load_projects()
    assert any(p.name == 'myproject' for p in projects)


def test_project_store_excludes_dotfiles(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / '.archive').mkdir()
    (tmp_path / 'realproject').mkdir()
    projects = store.load_projects()
    names = [p.name for p in projects]
    assert 'realproject' in names
    assert '.archive' not in names


def test_project_store_archive_uses_settings_dir(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    proj_dir = tmp_path / 'myproject'
    proj_dir.mkdir()
    projects = store.load_projects()
    store.archive(projects[0])
    archive_path = tmp_path / '.archive' / 'myproject'
    assert archive_path.exists()


def test_project_store_restore(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    archive_dir = tmp_path / '.archive'
    archive_dir.mkdir()
    (archive_dir / 'myproject').mkdir()
    archived = store.load_archived()
    store.restore(archived[0])
    assert (tmp_path / 'myproject').exists()
    assert not (archive_dir / 'myproject').exists()


def test_projects_watcher_start_accepts_path(tmp_path):
    # ProjectsWatcher.start() must accept an explicit path argument
    watcher = ProjectsWatcher()
    watcher.start(str(tmp_path))  # must not raise
    watcher._monitor.cancel()


def test_projects_watcher_restart(tmp_path):
    watcher = ProjectsWatcher()
    watcher.start(str(tmp_path))
    new_path = str(tmp_path)
    watcher.restart(new_path)   # must not raise; monitor should still be active
    watcher._monitor.cancel()


# --- StatusWatcher tests ---

def test_status_watcher_initial_status_is_empty_dict():
    w = StatusWatcher()
    assert w._status == {}


def test_status_watcher_reload_empty_dir(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    w = StatusWatcher()
    w._reload()
    assert w._status == {}


def test_status_watcher_reload_valid_file(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/myproject'
    (tmp_path / 'tmp-myproject.json').write_text(json.dumps({
        'state': 'working',
        'event': 'PreToolUse',
        'cwd': proj_path,
        'ts': 1000,
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    key = os.path.realpath(proj_path)
    assert key in w._status
    assert w._status[key].state == 'working'


def test_status_watcher_reload_invalid_json_skipped(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    (tmp_path / 'bad.json').write_text('not json')
    w = StatusWatcher()
    w._reload()   # must not raise
    assert w._status == {}


def test_status_watcher_reload_missing_cwd_skipped(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    (tmp_path / 'no-cwd.json').write_text(json.dumps({'state': 'done', 'cwd': ''}))
    w = StatusWatcher()
    w._reload()
    assert w._status == {}


def test_status_watcher_reload_old_format_defaults_state_to_done(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/oldproject'
    (tmp_path / 'tmp-oldproject.json').write_text(json.dumps({
        'event': 'Stop',    # no 'state' field — old format
        'cwd': proj_path,
        'ts': 1000,
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    key = os.path.realpath(proj_path)
    assert w._status[key].state == 'done'


def test_status_watcher_get_project_status_returns_snapshot_state(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_dir = tmp_path / 'myproject'
    proj_dir.mkdir()
    proj_path = str(proj_dir)
    slug = proj_path.replace('/', '-').replace('.', '-').lstrip('-')
    (tmp_path / f'{slug}.json').write_text(json.dumps({
        'state': 'waiting',
        'event': 'Notification',
        'cwd': proj_path,
        'ts': int(time.time()),
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='myproject', path=os.path.realpath(proj_path))
    assert w.get_project_status(proj) == 'waiting'


def test_status_watcher_get_project_status_returns_idle_when_not_found(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='missing', path='/no/such/project')
    assert w.get_project_status(proj) == 'idle'


def test_status_watcher_reload_replaces_dict_atomically(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/atomicproject'
    (tmp_path / 'tmp-atomicproject.json').write_text(json.dumps({
        'state': 'working', 'event': 'PreToolUse',
        'cwd': proj_path, 'ts': 1000, 'session': 'x',
    }))
    w = StatusWatcher()
    w._reload()
    # Remove the file and reload — entry should disappear
    (tmp_path / 'tmp-atomicproject.json').unlink()
    w._reload()
    assert os.path.realpath(proj_path) not in w._status
