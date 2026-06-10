"""Characterization + parity tests for session.json v2 (agent-aware).

v2 contract (spec P1):
  * Saved ``open_paths`` entries may be the legacy ``str`` form OR the new
    ``{"path": ..., "agent": ...}`` dict form.
  * ``load_session`` keeps its 2-tuple ``(open_paths: list[str], focused)``
    return — path strings only — so every existing caller and test is
    unaffected. Dict entries are transparently reduced to their ``path``.
  * A new ``load_agents(path) -> {path: agent}`` surfaces the per-project
    agent, defaulting to ``'claude'`` for legacy/missing entries.
  * ``save_session`` writes the legacy str form when no ``agents`` map is
    given (byte-compatible with v1), and the dict form when it is.
  * ``plan_restore`` output is unchanged for old data (covered in
    test_session_restore.py; re-pinned here against mixed input).
"""
import json
import types

from session import (
    save_session, load_session, load_agents, plan_restore, collect_agents_map,
)


# ── load_session back-compat: old str entries still load as path strings ──────

def test_load_old_str_entries_unchanged(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b'], 'focused_path': '/a'}))
    paths, focused = load_session(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/a'


def test_load_new_dict_entries_reduced_to_paths(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': [
            {'path': '/a', 'agent': 'claude'},
            {'path': '/b', 'agent': 'opencode'},
        ],
        'focused_path': '/b',
    }))
    paths, focused = load_session(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/b'


def test_load_mixed_old_and_new_entries(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': ['/a', {'path': '/b', 'agent': 'opencode'}],
        'focused_path': '/a',
    }))
    paths, focused = load_session(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/a'


def test_load_dedup_across_forms(tmp_path):
    """A path appearing as both str and dict dedups to one entry."""
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': ['/a', {'path': '/a', 'agent': 'opencode'}],
        'focused_path': '/a',
    }))
    paths, _ = load_session(str(path))
    assert paths == ['/a']


def test_load_dict_entry_missing_path_skipped(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': [{'agent': 'claude'}, {'path': '/b', 'agent': 'claude'}],
        'focused_path': None,
    }))
    paths, _ = load_session(str(path))
    assert paths == ['/b']


# ── load_agents: surfaces the per-project agent, defaults to claude ───────────

def test_load_agents_old_format_defaults_claude(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b'], 'focused_path': '/a'}))
    agents = load_agents(str(path))
    assert agents == {'/a': 'claude', '/b': 'claude'}


def test_load_agents_new_format(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': [
            {'path': '/a', 'agent': 'claude'},
            {'path': '/b', 'agent': 'opencode'},
        ],
        'focused_path': '/a',
    }))
    agents = load_agents(str(path))
    assert agents == {'/a': 'claude', '/b': 'opencode'}


def test_load_agents_dict_missing_agent_defaults_claude(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({
        'open_paths': [{'path': '/a'}],
        'focused_path': None,
    }))
    agents = load_agents(str(path))
    assert agents == {'/a': 'claude'}


def test_load_agents_missing_file_returns_empty(tmp_path):
    agents = load_agents(str(tmp_path / 'nope.json'))
    assert agents == {}


def test_load_agents_corrupt_returns_empty(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text('not json!!!')
    assert load_agents(str(path)) == {}


# ── save_session: legacy form by default, dict form when agents given ─────────

def test_save_without_agents_writes_legacy_str_form(tmp_path):
    """No agents map → byte-compatible with v1 (plain string entries)."""
    path = tmp_path / 'session.json'
    save_session(str(path), ['/a', '/b'], '/a')
    data = json.loads(path.read_text())
    assert data['open_paths'] == ['/a', '/b']
    assert data['focused_path'] == '/a'


def test_save_with_agents_writes_dict_form(tmp_path):
    path = tmp_path / 'session.json'
    save_session(str(path), ['/a', '/b'], '/a',
                 agents={'/a': 'claude', '/b': 'opencode'})
    data = json.loads(path.read_text())
    assert data['open_paths'] == [
        {'path': '/a', 'agent': 'claude'},
        {'path': '/b', 'agent': 'opencode'},
    ]
    assert data['focused_path'] == '/a'


def test_save_with_partial_agents_defaults_claude(tmp_path):
    """A path absent from the agents map saves as agent 'claude'."""
    path = tmp_path / 'session.json'
    save_session(str(path), ['/a', '/b'], None, agents={'/a': 'opencode'})
    data = json.loads(path.read_text())
    assert data['open_paths'] == [
        {'path': '/a', 'agent': 'opencode'},
        {'path': '/b', 'agent': 'claude'},
    ]


# ── round-trip: save(dict) → load_session + load_agents recover everything ────

def test_round_trip_new_form(tmp_path):
    path = tmp_path / 'session.json'
    save_session(str(path), ['/a', '/b'], '/b',
                 agents={'/a': 'claude', '/b': 'opencode'})
    paths, focused = load_session(str(path))
    agents = load_agents(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/b'
    assert agents == {'/a': 'claude', '/b': 'opencode'}


# ── plan_restore unchanged for old data (re-pinned via load+plan path) ────────

def _proj(path):
    import types
    p = types.SimpleNamespace()
    p.path = path
    p.name = path.rsplit('/', 1)[-1]
    return p


def test_plan_restore_unchanged_for_loaded_old_data(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b'], 'focused_path': '/a'}))
    open_paths, focused_path = load_session(str(path))
    active = {'/a': _proj('/a'), '/b': _proj('/b')}
    focused, bg = plan_restore(open_paths, focused_path, active)
    assert focused == '/a'
    assert bg == ['/b']


def test_plan_restore_unchanged_for_loaded_new_data(tmp_path):
    path = tmp_path / 'session.json'
    save_session(str(path), ['/a', '/b'], '/a',
                 agents={'/a': 'opencode', '/b': 'claude'})
    open_paths, focused_path = load_session(str(path))
    active = {'/a': _proj('/a'), '/b': _proj('/b')}
    focused, bg = plan_restore(open_paths, focused_path, active)
    assert focused == '/a'
    assert bg == ['/b']


# ── collect_agents_map: the save half of A2 records the RUNNING agent ─────────
#
# MAJOR-1 (P2 phase review): _save_session derived the agents map from
# settings.effective_agent, so a project restored under saved-agent-wins
# (running opencode while settings resolve claude) was re-saved as claude and
# the NEXT restore silently dropped the opencode session. The map must come
# from the live terminal's spawn-time signature.

def _term(agent):
    """Fake TerminalView: a live child + the spawn-time agent signature
    (mirrors the SimpleNamespace fakes in test_session_restore.py)."""
    t = types.SimpleNamespace(_child_pid=4242)
    t.spawned_agent_signature = lambda: agent
    return t


def test_collect_agents_map_records_running_agent_not_settings():
    """T1 — the reviewer's repro, pure: terminal runs opencode, settings
    resolve claude → the map records opencode."""
    terminals = {'/p': _term('opencode')}
    result = collect_agents_map(terminals, ['/p'], lambda p: 'claude')
    assert result == {'/p': 'opencode'}


def test_collect_agents_map_round_trip_keeps_saved_agent(tmp_path):
    """T2 — round-trip invariant: a session saved as opencode survives a full
    restore → re-save cycle with settings still resolving claude."""
    path = str(tmp_path / 'session.json')
    save_session(path, ['/p'], '/p', agents={'/p': 'opencode'})
    # Restore half (saved-agent-wins): the agent map drives the spawn.
    restored = load_agents(path)
    assert restored == {'/p': 'opencode'}
    # The terminal is spawned with the restored agent; settings still say
    # claude. The save half must echo the spawn-time truth.
    terminals = {'/p': _term(restored['/p'])}
    open_paths = ['/p']
    resaved = collect_agents_map(terminals, open_paths, lambda p: 'claude')
    save_session(path, open_paths, '/p', agents=resaved)
    assert load_agents(path) == {'/p': 'opencode'}


def test_collect_agents_map_spawn_truth_beats_midflight_settings_change():
    """T3 — settings flipped to opencode mid-flight; the live child is still
    the claude it was spawned as → the map records claude."""
    terminals = {'/p': _term('claude')}
    result = collect_agents_map(terminals, ['/p'], lambda p: 'opencode')
    assert result == {'/p': 'claude'}


def test_collect_agents_map_fallback_for_path_missing_from_terminals():
    """T4 — defensive leg: a path absent from terminals (normally impossible;
    open_paths is sourced from terminals) uses effective_agent_fn."""
    terminals = {'/p': _term('opencode')}
    result = collect_agents_map(terminals, ['/p', '/q'], lambda p: 'claude')
    assert result == {'/p': 'opencode', '/q': 'claude'}
