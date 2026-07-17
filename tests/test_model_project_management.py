import os
import pytest
from settings import Settings
from model import ProjectStore


def test_create_project(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    store.create_project('my-project')
    assert (tmp_path / 'my-project').is_dir()


def test_create_project_raises_if_exists(tmp_path):
    """Duplicate names must not silently succeed (false "New project" toast)."""
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'existing').mkdir()
    with pytest.raises(FileExistsError):
        store.create_project('existing')
    assert (tmp_path / 'existing').is_dir()


def test_create_project_appears_in_load(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    store.create_project('new-project')
    projects = store.load_projects()
    assert any(p.name == 'new-project' for p in projects)


def test_rename_project(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'old-name').mkdir()
    projects = store.load_projects()
    store.rename_project(projects[0], 'new-name')
    assert (tmp_path / 'new-name').is_dir()
    assert not (tmp_path / 'old-name').exists()


def test_rename_project_appears_in_load(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'myproject').mkdir()
    projects = store.load_projects()
    store.rename_project(projects[0], 'renamed')
    new_projects = store.load_projects()
    assert any(p.name == 'renamed' for p in new_projects)
    assert not any(p.name == 'myproject' for p in new_projects)


def test_create_project_rejects_unsafe_name(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    with pytest.raises(ValueError, match='invalid|cannot|/|Name'):
        store.create_project('$(whoami)')
    with pytest.raises(ValueError, match='cannot contain'):
        store.create_project('a/b')
    assert not (tmp_path / '$(whoami)').exists()


def test_rename_project_rejects_unsafe_name(tmp_path):
    """Rename must use the same shell-meta policy as create."""
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'safe').mkdir()
    projects = store.load_projects()
    with pytest.raises(ValueError, match='invalid|cannot|/|Name'):
        store.rename_project(projects[0], '$(whoami)')
    with pytest.raises(ValueError, match='cannot contain'):
        store.rename_project(projects[0], 'a/b')
    with pytest.raises(ValueError, match='start with'):
        store.rename_project(projects[0], '.hidden')
    assert (tmp_path / 'safe').is_dir()
    assert not (tmp_path / '$(whoami)').exists()


def test_rename_project_rejects_existing_destination(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'alpha').mkdir()
    (tmp_path / 'beta').mkdir()
    projects = {p.name: p for p in store.load_projects()}
    with pytest.raises(FileExistsError):
        store.rename_project(projects['alpha'], 'beta')
    assert (tmp_path / 'alpha').is_dir()

