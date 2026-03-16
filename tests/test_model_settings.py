import os
import pytest
from settings import Settings
from model import ProjectStore, ProjectsWatcher


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
