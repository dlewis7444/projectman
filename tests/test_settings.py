import os
import json
import pytest
from settings import Settings, DEFAULT_SETTINGS_PATH


def test_defaults():
    s = Settings()
    assert s.projects_dir == '~/.ProjectMan/Projects'
    assert s.claude_binary == ''
    assert s.resume_projects is True
    assert 'resume_last_project' not in Settings.__dataclass_fields__
    assert s.font_size == 11
    assert s.scrollback_lines == 10000
    assert s.audible_bell is False
    assert s.multiplexer == 'none'


def test_resolved_projects_dir():
    s = Settings()
    assert s.resolved_projects_dir == os.path.expanduser('~/.ProjectMan/Projects')


def test_resolved_claude_binary_empty():
    s = Settings()
    assert s.resolved_claude_binary == 'claude'


def test_resolved_claude_binary_whitespace():
    s = Settings(claude_binary='   ')
    assert s.resolved_claude_binary == 'claude'


def test_resolved_claude_binary_set():
    s = Settings(claude_binary='/usr/local/bin/claude')
    assert s.resolved_claude_binary == '/usr/local/bin/claude'


def test_load_missing_file(tmp_path):
    path = str(tmp_path / 'nonexistent.json')
    s = Settings.load(path)
    assert s.font_size == 11  # defaults returned


def test_load_corrupt_json(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('not json!')
    s = Settings.load(str(path))
    assert s.font_size == 11  # defaults on parse error


def test_load_partial_file(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 14}))
    s = Settings.load(str(path))
    assert s.font_size == 14
    assert s.scrollback_lines == 10000  # default for missing field


def test_load_ignores_unknown_keys(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'font_size': 14, 'unknown_key': 'value'}))
    s = Settings.load(str(path))
    assert s.font_size == 14  # must not raise TypeError


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings(font_size=14, multiplexer='tmux')
    s.save(path)
    s2 = Settings.load(path)
    assert s2.font_size == 14
    assert s2.multiplexer == 'tmux'


def test_save_atomic_no_temp_files(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings()
    s.save(path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == 'settings.json'


def test_save_creates_directory(tmp_path):
    path = str(tmp_path / 'subdir' / 'settings.json')
    s = Settings()
    s.save(path)
    assert os.path.exists(path)


def test_default_settings_path_constant():
    assert DEFAULT_SETTINGS_PATH == os.path.expanduser('~/.ProjectMan/settings.json')


def test_load_ignores_old_resume_last_project_key(tmp_path):
    """Old settings.json with resume_last_project is silently upgraded."""
    path = tmp_path / 'settings.json'
    path.write_text('{"resume_last_project": false}')
    s = Settings.load(str(path))
    # Old key is ignored; new field uses dataclass default (True)
    assert s.resume_projects is True
