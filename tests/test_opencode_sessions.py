"""OpencodeAdapter — spawn argv/model + session-list parser (P2 Part B, B1).

Headless; no GTK, no real opencode process. The session parsers are exercised
against recorded fixtures under tests/fixtures/opencode/ (shapes captured from a
real `opencode session list`, marked to-be-verified on the VM gate — the parser
is layered and defensive precisely because opencode's CLI/storage shapes drift
across versions).
"""
import os
import json

import pytest

import agents
from settings import Settings


FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'opencode')
ALPHA = '/home/u/.ProjectMan/projects/alpha'
BETA = '/home/u/.ProjectMan/projects/beta'


def _fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def _project(path):
    import types
    return types.SimpleNamespace(name=os.path.basename(path), path=path)


# Identity realpath so fixture paths (which don't exist on disk) compare equal.
def _ident(p):
    return p


# ── adapter identity + caps ───────────────────────────────────────────────────

def test_opencode_registered_and_full_caps():
    a = agents.get_adapter('opencode')
    assert a.id == 'opencode'
    assert a.display_name == 'opencode'
    caps = a.caps
    assert caps.continue_ and caps.resume_by_id and caps.sessions
    assert caps.model_select and caps.rich_status and caps.headless_json


def test_resolve_opencode_is_not_a_miss():
    adapter, miss = agents.resolve_adapter('opencode')
    assert adapter.id == 'opencode'
    assert miss is None


# ── spawn argv: fresh / continue / resume, with and without a model ───────────

def test_fresh_argv_no_model():
    a = agents.OpencodeAdapter()
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'fresh')
    assert plan.argv == ['opencode']
    assert plan.env is None
    assert plan.fallback_reason is None


def test_fresh_argv_with_model():
    a = agents.OpencodeAdapter()
    s = Settings(agent_default='opencode', model_overrides={'/p/a': 'ollama/qwen3.5:cloud'})
    plan = a.spawn_plan(s, _project('/p/a'), 'fresh')
    assert plan.argv == ['opencode', '-m', 'ollama/qwen3.5:cloud']


def test_resume_argv_uses_dash_s():
    a = agents.OpencodeAdapter()
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'resume', session_id='ses_abc')
    assert plan.argv == ['opencode', '-s', 'ses_abc']


def test_resume_argv_with_model():
    a = agents.OpencodeAdapter()
    s = Settings(model_overrides={'/p/a': 'ollama/qwen3.5:cloud'})
    plan = a.spawn_plan(s, _project('/p/a'), 'resume', session_id='ses_abc')
    assert plan.argv == ['opencode', '-m', 'ollama/qwen3.5:cloud', '-s', 'ses_abc']


def test_resume_requires_session_id():
    a = agents.OpencodeAdapter()
    with pytest.raises(ValueError):
        a.spawn_plan(Settings(), _project('/p/a'), 'resume')


def test_continue_argv_folds_model_into_both_halves():
    """review n3 / A3: the continue wrapper's fallback must ALSO carry -m so the
    bare-opencode fallback targets the same provider/model."""
    a = agents.OpencodeAdapter()
    s = Settings(model_overrides={'/p/a': 'ollama/qwen3.5:cloud'})
    plan = a.spawn_plan(s, _project('/p/a'), 'continue')
    # bash -c "...; opencode -m M -c; ...; exec opencode -m M"
    script = plan.argv[2]
    assert plan.argv[0] == 'bash' and plan.argv[1] == '-c'
    assert 'opencode -m ollama/qwen3.5:cloud -c' in script
    assert 'exec opencode -m ollama/qwen3.5:cloud' in script


def test_continue_argv_no_model_is_plain():
    a = agents.OpencodeAdapter()
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'continue')
    script = plan.argv[2]
    assert 'opencode -c' in script
    assert 'exec opencode' in script
    assert '-m' not in script


def test_custom_binary_resolved():
    a = agents.OpencodeAdapter()
    s = Settings(agents={'opencode': {'binary': '/opt/oc/opencode'}})
    plan = a.spawn_plan(s, _project('/p/a'), 'fresh')
    assert plan.argv == ['/opt/oc/opencode']


def test_spawn_plan_never_injects_env():
    """opencode is natively multi-provider — no ccr, no env override, ever."""
    a = agents.OpencodeAdapter()
    s = Settings(model_overrides={'/p/a': 'ollama/qwen3.5:cloud'})
    for mode, kw in (('fresh', {}), ('continue', {}), ('resume', {'session_id': 's'})):
        plan = a.spawn_plan(s, _project('/p/a'), mode, **kw)
        assert plan.env is None
        assert plan.fallback_reason is None


# ── zellij ────────────────────────────────────────────────────────────────────

def test_zellij_spawn_env_is_none():
    a = agents.OpencodeAdapter()
    assert a.zellij_spawn_env(Settings(), _project('/p/a')) == (None, None)


def test_zellij_continue_command_folds_model():
    a = agents.OpencodeAdapter()
    s = Settings(model_overrides={'/p/a': 'ollama/qwen3.5:cloud'})
    cmd = a.zellij_continue_command(s, _project('/p/a'))
    assert cmd == ('opencode -m ollama/qwen3.5:cloud -c '
                   '|| opencode -m ollama/qwen3.5:cloud')


def test_zellij_continue_command_no_model():
    a = agents.OpencodeAdapter()
    cmd = a.zellij_continue_command(Settings(), _project('/p/a'))
    assert cmd == 'opencode -c || opencode'


# ── JSON parser (primary) — fixture-driven ────────────────────────────────────

def test_parse_json_filters_by_directory_newest_first():
    text = _fixture('session_list.json')
    refs = agents.parse_session_list_json(text, ALPHA, realpath=_ident)
    # Only alpha's three sessions, newest-first by `updated`.
    assert [r.id for r in refs] == [
        'ses_7a1f9c2dffeAAbbCCddEEff01',   # updated 1775478796232
        'ses_7a205e10ffeBBccDDeeFFgg02',   # updated 1775470000000
        'ses_7a40bba2ffeDDeeFFggHHii04',   # updated 1775400000000
    ]
    assert refs[0].title == 'Wire the opencode adapter into the seam'
    assert refs[0].last_active == 1775478796232
    assert all(isinstance(r, agents.SessionRef) for r in refs)


def test_parse_json_other_project():
    text = _fixture('session_list.json')
    refs = agents.parse_session_list_json(text, BETA, realpath=_ident)
    assert [r.id for r in refs] == ['ses_7a30aa91ffeCCddEEffGGhh03']


def test_parse_json_caps_at_seven():
    rows = [
        {'id': f'ses_{i}', 'title': f't{i}', 'updated': i, 'directory': ALPHA}
        for i in range(20)
    ]
    refs = agents.parse_session_list_json(json.dumps(rows), ALPHA, realpath=_ident)
    assert len(refs) == 7
    assert refs[0].id == 'ses_19'   # newest


def test_parse_json_defensive_on_garbage():
    assert agents.parse_session_list_json('not json', ALPHA) == []
    assert agents.parse_session_list_json('{"not": "a list"}', ALPHA) == []
    assert agents.parse_session_list_json('[]', ALPHA) == []
    # Entries missing id or directory are skipped, not fatal.
    rows = [{'title': 'no id', 'directory': ALPHA},
            {'id': 'ses_x', 'title': 'no dir'},
            {'id': 'ses_ok', 'title': 'ok', 'updated': 5, 'directory': ALPHA}]
    refs = agents.parse_session_list_json(json.dumps(rows), ALPHA, realpath=_ident)
    assert [r.id for r in refs] == ['ses_ok']


def test_parse_json_missing_updated_falls_back_to_created():
    rows = [{'id': 'ses_c', 'title': 't', 'created': 42, 'directory': ALPHA}]
    refs = agents.parse_session_list_json(json.dumps(rows), ALPHA, realpath=_ident)
    assert refs[0].last_active == 42


# ── table parser (fallback diagnostic; cannot filter by project) ──────────────

def test_parse_table_extracts_id_and_title():
    text = _fixture('session_list.table.txt')
    rows = agents.parse_session_list_table(text)
    assert rows[0] == ('ses_7a1f9c2dffeAAbbCCddEEff01',
                       'Wire the opencode adapter into the seam                                              7:33 AM · 4/6/2026')
    # Header + separator are skipped.
    assert all(sid.startswith('ses_') for sid, _ in rows)
    assert len(rows) == 3


def test_parse_table_handles_empty():
    assert agents.parse_session_list_table('') == []
    assert agents.parse_session_list_table('Session ID  Title  Updated\n') == []


# ── storage-scan fallback — fixture-driven ────────────────────────────────────

def test_scan_storage_maps_worktree_and_reads_sessions():
    storage = os.path.join(FIXTURES, 'storage')
    refs = agents.scan_storage_sessions(storage, ALPHA, realpath=_ident)
    # alpha → proj_alpha → two session files, newest-first.
    assert [r.id for r in refs] == [
        'ses_7a1f9c2dffeAAbbCCddEEff01',
        'ses_7a40bba2ffeDDeeFFggHHii04',
    ]
    assert refs[0].last_active == 1775478796232


def test_scan_storage_unknown_project_returns_empty():
    storage = os.path.join(FIXTURES, 'storage')
    refs = agents.scan_storage_sessions(storage, '/not/a/known/worktree', realpath=_ident)
    assert refs == []


def test_scan_storage_missing_dir_returns_empty():
    refs = agents.scan_storage_sessions('/no/such/storage', ALPHA)
    assert refs == []


# ── list_sessions integration: layering (CLI primary → storage fallback) ──────
# run_fn contract is (argv, cwd) — cwd is load-bearing: `opencode session list`
# is CWD-SCOPED (P2 VM gate finding: invoked from the wrong dir it returns 0
# rows while the same command from the project dir returns the real sessions).

def test_list_sessions_uses_cli_json_when_available():
    text = _fixture('session_list.json')
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, text)

    # realpath identity via monkeypatching not needed: fixture dirs compared
    # through os.path.realpath, which is a no-op for non-existent absolute paths.
    a = agents.OpencodeAdapter(run_fn=fake_run)
    refs = a.list_sessions(_project(ALPHA), Settings())
    assert [r.id for r in refs] == [
        'ses_7a1f9c2dffeAAbbCCddEEff01',
        'ses_7a205e10ffeBBccDDeeFFgg02',
        'ses_7a40bba2ffeDDeeFFggHHii04',
    ]
    # It asked for JSON.
    argv0, cwd0 = calls[0]
    assert '--format' in argv0 and 'json' in argv0


def test_list_sessions_runs_cli_in_project_dir():
    """The CLI MUST execute with cwd = the project's path (gate defect 2i):
    `opencode session list` scopes its output to the invoking directory, so a
    run from PM's own cwd silently lists the wrong project's sessions."""
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, '[]')

    a = agents.OpencodeAdapter(run_fn=fake_run, storage_dir='/no/such')
    a.list_sessions(_project(ALPHA), Settings())
    assert calls, 'CLI was never invoked'
    assert calls[0][1] == ALPHA


def test_list_sessions_falls_back_to_storage_when_cli_fails():
    storage = os.path.join(FIXTURES, 'storage')

    def fake_run(argv, cwd):
        return None   # CLI unavailable / errored

    a = agents.OpencodeAdapter(run_fn=fake_run, storage_dir=storage)
    refs = a.list_sessions(_project(ALPHA), Settings())
    assert [r.id for r in refs] == [
        'ses_7a1f9c2dffeAAbbCCddEEff01',
        'ses_7a40bba2ffeDDeeFFggHHii04',
    ]


def test_list_sessions_falls_back_when_cli_nonzero():
    storage = os.path.join(FIXTURES, 'storage')

    def fake_run(argv, cwd):
        return (1, '')   # rc != 0

    a = agents.OpencodeAdapter(run_fn=fake_run, storage_dir=storage)
    refs = a.list_sessions(_project(ALPHA), Settings())
    assert [r.id for r in refs] == [
        'ses_7a1f9c2dffeAAbbCCddEEff01',
        'ses_7a40bba2ffeDDeeFFggHHii04',
    ]


def test_list_sessions_total_failure_returns_empty():
    def fake_run(argv, cwd):
        return None
    a = agents.OpencodeAdapter(run_fn=fake_run, storage_dir='/no/such/storage')
    assert a.list_sessions(_project(ALPHA), Settings()) == []


def test_list_sessions_custom_binary_used_in_cli():
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, '[]')

    a = agents.OpencodeAdapter(run_fn=fake_run, storage_dir='/no/such')
    s = Settings(agents={'opencode': {'binary': '/opt/oc/opencode'}})
    a.list_sessions(_project(ALPHA), s)
    assert calls[0][0][0] == '/opt/oc/opencode'
