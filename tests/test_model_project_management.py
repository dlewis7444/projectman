import os
import pytest
from settings import Settings
from model import ProjectStore


def test_create_project(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    store.create_project('my-project')
    assert (tmp_path / 'my-project').is_dir()


def test_create_project_exist_ok(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'existing').mkdir()
    store.create_project('existing')  # must not raise
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
