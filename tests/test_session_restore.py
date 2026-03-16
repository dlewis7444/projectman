import json
import os
import types
import pytest
from session import save_session, load_session, filter_active_paths, collect_session_state


# ── save_session ──────────────────────────────────────────────────────────────

def test_save_writes_correct_json(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a', '/b'], '/a')
    data = json.loads(open(path).read())
    assert data['open_paths'] == ['/a', '/b']
    assert data['focused_path'] == '/a'


def test_save_null_focused_path(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], None)
    data = json.loads(open(path).read())
    assert data['focused_path'] is None


def test_save_empty_session(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, [], None)
    data = json.loads(open(path).read())
    assert data['open_paths'] == []
    assert data['focused_path'] is None


def test_save_creates_directory(tmp_path):
    path = str(tmp_path / 'nested' / 'dir' / 'session.json')
    save_session(path, ['/x'], '/x')
    assert os.path.exists(path)


def test_save_atomic_no_temp_files(tmp_path):
    """After a successful write only the final file remains, no .tmp leftovers."""
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], '/a')
    files = [f.name for f in tmp_path.iterdir()]
    assert files == ['session.json']


def test_save_swallows_write_error(tmp_path, capsys):
    """A permission error must not raise; error is printed to stderr."""
    path = str(tmp_path / 'session.json')
    os.chmod(tmp_path, 0o444)
    try:
        save_session(path, ['/a'], '/a')  # must not raise
    finally:
        os.chmod(tmp_path, 0o755)
    captured = capsys.readouterr()
    assert 'ProjectMan' in captured.err


# ── load_session ──────────────────────────────────────────────────────────────

def test_load_returns_empty_on_missing_file(tmp_path):
    paths, focused = load_session(str(tmp_path / 'nonexistent.json'))
    assert paths == []
    assert focused is None


def test_load_returns_empty_on_corrupt_json(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text('not json!!!')
    paths, focused = load_session(str(path))
    assert paths == []
    assert focused is None


def test_load_returns_correct_data(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b'], 'focused_path': '/a'}))
    paths, focused = load_session(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/a'


def test_load_deduplicates_paths(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b', '/a'], 'focused_path': '/a'}))
    paths, _ = load_session(str(path))
    assert paths == ['/a', '/b']


def test_load_returns_empty_on_non_list_open_paths(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': 'not-a-list', 'focused_path': None}))
    paths, focused = load_session(str(path))
    assert paths == []
    assert focused is None


def test_load_null_focused_path(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a'], 'focused_path': None}))
    _, focused = load_session(str(path))
    assert focused is None


def test_load_missing_focused_path_key(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a']}))
    paths, focused = load_session(str(path))
    assert paths == ['/a']
    assert focused is None


# ── filter_active_paths ───────────────────────────────────────────────────────

def _proj(path):
    """Minimal Project-like object."""
    p = types.SimpleNamespace()
    p.path = path
    p.name = os.path.basename(path)
    return p


def _tv(pid):
    """Minimal TerminalView stand-in."""
    return types.SimpleNamespace(_child_pid=pid)


def test_filter_returns_only_matching_active_projects():
    active = [_proj('/a'), _proj('/b')]
    result = filter_active_paths(['/a', '/b', '/c'], active)
    assert set(result.keys()) == {'/a', '/b'}


def test_filter_excludes_archived_paths():
    """Archived projects are not passed in; absent from result."""
    active = [_proj('/a')]          # /b is 'archived' — not in active list
    result = filter_active_paths(['/a', '/b'], active)
    assert '/b' not in result
    assert '/a' in result


def test_filter_excludes_deleted_paths():
    """Paths deleted since last save are absent from active list → excluded."""
    active = [_proj('/a')]
    result = filter_active_paths(['/a', '/deleted'], active)
    assert '/deleted' not in result


def test_filter_preserves_project_objects():
    proj_a = _proj('/a')
    result = filter_active_paths(['/a'], [proj_a])
    assert result['/a'] is proj_a


def test_filter_empty_open_paths():
    result = filter_active_paths([], [_proj('/a')])
    assert result == {}


def test_filter_empty_active_projects():
    result = filter_active_paths(['/a'], [])
    assert result == {}


# ── collect_session_state ─────────────────────────────────────────────────────

def test_collect_includes_only_running_terminals():
    terminals = {'/a': _tv(42), '/b': _tv(None)}
    paths, _ = collect_session_state(terminals, '/a')
    assert paths == ['/a']


def test_collect_focused_path_when_active_is_running():
    terminals = {'/a': _tv(1)}
    _, focused = collect_session_state(terminals, '/a')
    assert focused == '/a'


def test_collect_focused_null_when_active_has_no_process():
    terminals = {'/a': _tv(None), '/b': _tv(1)}
    _, focused = collect_session_state(terminals, '/a')
    assert focused is None


def test_collect_focused_null_when_active_path_is_none():
    terminals = {'/a': _tv(1)}
    _, focused = collect_session_state(terminals, None)
    assert focused is None


def test_collect_empty_when_no_terminals_running():
    terminals = {'/a': _tv(None), '/b': _tv(None)}
    paths, focused = collect_session_state(terminals, '/a')
    assert paths == []
    assert focused is None


def test_collect_empty_terminals():
    paths, focused = collect_session_state({}, None)
    assert paths == []
    assert focused is None
