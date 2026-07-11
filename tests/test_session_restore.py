import json
import os
import types
import pytest
from session import (
    save_session, load_session, load_hosts, filter_active_paths,
    collect_session_state, plan_restore,
)


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


# ── plan_restore ──────────────────────────────────────────────────────────────

def test_plan_restore_focused_in_active_set():
    active = {'/a': _proj('/a'), '/b': _proj('/b')}
    focused, bg = plan_restore(['/a', '/b'], '/a', active)
    assert focused == '/a'
    assert bg == ['/b']


def test_plan_restore_focused_remote_ssh_ref(tmp_path):
    """Remote ssh: paths stay in the restore plan when synthesized into active.

    Regression: focused remotes were dropped because activation used
    _find_project before the async remote list landed; background locals still
    restored. Session v3 must keep focused remote in the plan.
    """
    path = str(tmp_path / 'session.json')
    remote = 'ssh:abc123:myproj'
    local = '/home/u/projects/local'
    save_session(
        path, [local, remote], remote,
        harnesses={local: 'claude', remote: 'grok'},
        hosts={local: 'localhost', remote: 'abc123'},
    )
    open_paths, focused_path = load_session(path)
    assert focused_path == remote
    assert remote in open_paths
    assert load_hosts(path)[remote] == 'abc123'
    # Local-only filter would drop remote — synthesis adds it back (window).
    active = filter_active_paths(open_paths, [_proj(local)])
    assert remote not in active
    active[remote] = _proj(remote)  # synth stand-in
    focused, bg = plan_restore(open_paths, focused_path, active)
    assert focused == remote
    assert local in bg


def test_find_project_synth_does_not_recurse():
    """_find_project must not call _project_for_session_path which calls find.

    Regression 2026-07-10: maximum recursion depth exceeded on restore.
    """
    import types
    from window import AppWindow
    from hosts import HostProfile

    # Minimal self: store empty, sidebar no remotes, one host profile.
    class _Store:
        def load_projects(self):
            return []

        def load_archived(self):
            return []

    class _Sidebar:
        _remote_projects = {}

    class _Settings:
        def host_profiles(self):
            return {
                'abc123': HostProfile(
                    id='abc123', ssh_target='box', display_name='box',
                ),
            }

    fake = types.SimpleNamespace(
        _store=_Store(),
        _sidebar=_Sidebar(),
        _settings=_Settings(),
    )
    # Attach unbound methods so find → synth works without full AppWindow.
    fake._project_for_session_path = (
        lambda path, s=fake: AppWindow._project_for_session_path(s, path)
    )
    fake._find_project = (
        lambda path, s=fake: AppWindow._find_project(s, path)
    )
    path = 'ssh:abc123:myproj'
    proj = fake._project_for_session_path(path)
    assert proj is not None
    assert proj.name == 'myproj'
    assert proj.host_id == 'abc123'
    found = fake._find_project(path)
    assert found is not None
    assert found.path == path
    # Local path: synth returns None, find returns None
    assert fake._project_for_session_path('/local/p') is None
    assert fake._find_project('/local/p') is None


def test_plan_restore_focused_null_when_not_in_active():
    """focused_path is in open_paths but not in active (e.g. archived)."""
    active = {'/b': _proj('/b')}
    focused, bg = plan_restore(['/a', '/b'], '/a', active)
    assert focused is None
    assert bg == ['/b']


def test_plan_restore_focused_null_when_none():
    active = {'/a': _proj('/a')}
    focused, bg = plan_restore(['/a'], None, active)
    assert focused is None
    assert bg == ['/a']


def test_plan_restore_background_excludes_focused():
    active = {'/a': _proj('/a'), '/b': _proj('/b'), '/c': _proj('/c')}
    focused, bg = plan_restore(['/a', '/b', '/c'], '/b', active)
    assert focused == '/b'
    assert '/b' not in bg
    assert set(bg) == {'/a', '/c'}


def test_plan_restore_preserves_order():
    active = {'/a': _proj('/a'), '/b': _proj('/b'), '/c': _proj('/c')}
    focused, bg = plan_restore(['/a', '/b', '/c'], '/a', active)
    assert bg == ['/b', '/c']


def test_plan_restore_empty_active():
    focused, bg = plan_restore(['/a'], '/a', {})
    assert focused is None
    assert bg == []


def test_plan_restore_empty_open_paths():
    active = {'/a': _proj('/a')}
    focused, bg = plan_restore([], None, active)
    assert focused is None
    assert bg == []
