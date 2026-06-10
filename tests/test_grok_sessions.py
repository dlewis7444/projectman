"""GrokAdapter — spawn argv/model + `grok sessions list` parser (P3 Part B).

Headless; no GTK, no real grok process. The session parser is exercised against
fixtures under tests/fixtures/grok/ recorded from the REAL Part-0 bench probe of
`grok sessions list` (grok 0.2.39, bench-vm, 2026-06-10) — see that dir's
README for provenance. Binding tests T-B1 (spawn) and T-B2 (sessions).
"""
import os

import pytest

import agents
from settings import Settings


FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'grok')
# A real session UUID from the probe (also in raw/hooks.log).
PROBE_ID_NEWER = '019eb297-fa74-7741-863e-d8aa822ac7bf'
PROBE_ID_OLDER = '019eb291-3b96-7503-b462-7b92e11c1ebd'


def _fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def _project(path):
    import types
    return types.SimpleNamespace(name=os.path.basename(path), path=path)


# ── adapter identity + caps (registered as a builtin) ─────────────────────────

def test_grok_registered_and_full_caps():
    a = agents.get_adapter('grok')
    assert a.id == 'grok'
    assert a.display_name == 'Grok Build'
    caps = a.caps
    assert caps.continue_ and caps.resume_by_id and caps.sessions
    assert caps.model_select and caps.rich_status and caps.headless_json
    # F1: all six True INCLUDING the continue-fallback policy.
    assert caps.continue_falls_back_to_fresh is True


def test_grok_is_a_builtin():
    """Registered in ADAPTERS at import → BUILTIN_AGENT_IDS picks it up, so a
    custom adapter can never claim 'grok' (M-P3.5)."""
    assert 'grok' in agents.ADAPTERS
    assert 'grok' in agents.BUILTIN_AGENT_IDS
    with pytest.raises(ValueError):
        agents.register_adapter(type('X', (), {'id': 'grok'})())


def test_resolve_grok_is_not_a_miss():
    adapter, miss = agents.resolve_adapter('grok')
    assert adapter.id == 'grok'
    assert miss is None


# ── T-B1: spawn argv — fresh / continue / resume, with and without a model ────

def test_fresh_argv_no_model():
    a = agents.get_adapter('grok')
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'fresh')
    assert plan.argv == ['grok']
    assert plan.env is None
    assert plan.fallback_reason is None


def test_fresh_argv_with_model():
    a = agents.get_adapter('grok')
    s = Settings(agent_default='grok', model_overrides={'/p/a': 'pool-qwen'})
    plan = a.spawn_plan(s, _project('/p/a'), 'fresh')
    assert plan.argv == ['grok', '-m', 'pool-qwen']


def test_resume_argv_uses_dash_r():
    """T-B1: resume is `grok -r <id>` (UUIDv7)."""
    a = agents.get_adapter('grok')
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'resume', session_id=PROBE_ID_NEWER)
    assert plan.argv == ['grok', '-r', PROBE_ID_NEWER]


def test_resume_argv_with_model():
    a = agents.get_adapter('grok')
    s = Settings(model_overrides={'/p/a': 'pool-qwen'})
    plan = a.spawn_plan(s, _project('/p/a'), 'resume', session_id='abc')
    assert plan.argv == ['grok', '-m', 'pool-qwen', '-r', 'abc']


def test_resume_requires_session_id():
    a = agents.get_adapter('grok')
    with pytest.raises(ValueError):
        a.spawn_plan(Settings(), _project('/p/a'), 'resume')


def test_continue_argv_carries_grok_dash_c_via_policy():
    """T-B1: continue folds `grok -c || grok` shape via the fallback wrapper
    (F1: standard `-c || fresh`). Model-less form is plain."""
    a = agents.get_adapter('grok')
    plan = a.spawn_plan(Settings(), _project('/p/a'), 'continue')
    assert plan.argv[0] == 'bash' and plan.argv[1] == '-c'
    script = plan.argv[2]
    assert 'grok -c' in script
    assert 'exec grok' in script
    assert '-m' not in script


def test_continue_argv_folds_model_into_both_halves():
    """T-B1: the continue wrapper's fallback ALSO carries -m (mirrors opencode's
    review-n3 fix) so the bare-grok fallback targets the same model key."""
    a = agents.get_adapter('grok')
    s = Settings(model_overrides={'/p/a': 'pool-qwen'})
    plan = a.spawn_plan(s, _project('/p/a'), 'continue')
    script = plan.argv[2]
    assert 'grok -m pool-qwen -c' in script
    assert 'exec grok -m pool-qwen' in script


def test_custom_binary_resolved():
    a = agents.get_adapter('grok')
    s = Settings(agents={'grok': {'binary': '/opt/grok/grok'}})
    plan = a.spawn_plan(s, _project('/p/a'), 'fresh')
    assert plan.argv == ['/opt/grok/grok']


def test_spawn_plan_never_injects_env():
    """grok reaches the pool via its own config.toml — no ccr, no env override."""
    a = agents.get_adapter('grok')
    s = Settings(model_overrides={'/p/a': 'pool-qwen'})
    for mode, kw in (('fresh', {}), ('continue', {}), ('resume', {'session_id': 's'})):
        plan = a.spawn_plan(s, _project('/p/a'), mode, **kw)
        assert plan.env is None
        assert plan.fallback_reason is None


# ── zellij ────────────────────────────────────────────────────────────────────

def test_zellij_spawn_env_is_none():
    """B5/F5: no env injection — returns (None, None)."""
    a = agents.get_adapter('grok')
    assert a.zellij_spawn_env(Settings(), _project('/p/a')) == (None, None)


def test_zellij_continue_command_folds_model():
    a = agents.get_adapter('grok')
    s = Settings(model_overrides={'/p/a': 'pool-qwen'})
    cmd = a.zellij_continue_command(s, _project('/p/a'))
    assert cmd == 'grok -m pool-qwen -c || grok -m pool-qwen'


def test_zellij_continue_command_no_model():
    a = agents.get_adapter('grok')
    cmd = a.zellij_continue_command(Settings(), _project('/p/a'))
    assert cmd == 'grok -c || grok'


# ── T-B2: parser — fixture-driven (real probe capture) ────────────────────────

def test_parse_real_probe_capture_two_sessions():
    """The verbatim probe output (Q4): two real sessions, ids verbatim, the
    later-updated one first (CLI emits newest-first)."""
    text = _fixture('sessions_list.txt')
    refs = agents.parse_grok_session_list(text)
    assert [r.id for r in refs] == [PROBE_ID_NEWER, PROBE_ID_OLDER]
    # Summary (free text with spaces) is kept whole as the title.
    assert refs[0].title == 'Reply with exactly: hook-test-ok'
    assert refs[1].title == 'Reply with exactly: ok'
    assert all(isinstance(r, agents.SessionRef) for r in refs)
    # last_active is the UPDATED date parsed to an epoch (00:00 UTC).
    assert refs[0].last_active == 1781049600  # 2026-06-10T00:00:00Z


def test_parse_header_row_is_skipped():
    text = _fixture('sessions_list.txt')
    refs = agents.parse_grok_session_list(text)
    # No ref's id is the literal header token.
    assert all(r.id != 'SESSION' for r in refs)
    assert len(refs) == 2


def test_parse_newest_first_order_preserved_and_cap_7():
    """T-B2: order is newest-first (the CLI's emission order is preserved) and
    the result caps at 7 even when more rows exist."""
    text = _fixture('sessions_list_many.txt')
    refs = agents.parse_grok_session_list(text)
    assert len(refs) == 7  # 9 rows in fixture → capped
    # First seven, in CLI order (newest UPDATED first).
    assert refs[0].id == '019eb400-0000-7000-8000-000000000009'
    assert refs[0].title == 'newest by UPDATED'
    assert refs[6].id == '019eb400-0000-7000-8000-000000000003'
    # The two beyond cap 7 are dropped.
    ids = [r.id for r in refs]
    assert '019eb400-0000-7000-8000-000000000002' not in ids
    assert '019eb400-0000-7000-8000-000000000001' not in ids


def test_parse_summary_with_spaces_kept_whole():
    text = _fixture('sessions_list_many.txt')
    refs = agents.parse_grok_session_list(text)
    third = next(r for r in refs if r.id.endswith('07'))
    assert third.title == 'third with spaces in summary'


def test_parse_defensive_on_garbage():
    assert agents.parse_grok_session_list('') == []
    assert agents.parse_grok_session_list('   \n  \n') == []
    # A header with no rows → empty.
    assert agents.parse_grok_session_list(
        'SESSION ID  CREATED  UPDATED  STATUS  SUMMARY\n') == []
    # Non-UUID first token rows are ignored.
    assert agents.parse_grok_session_list('garbage line here\nmore noise\n') == []


def test_parse_row_missing_trailing_columns_no_crash():
    # A bare UUID with no following columns → empty title, no raise.
    refs = agents.parse_grok_session_list(PROBE_ID_NEWER + '\n')
    assert len(refs) == 1
    assert refs[0].id == PROBE_ID_NEWER
    assert refs[0].title == ''


# ── T-B2: list_sessions integration — run_fn cwd contract (P2 pattern) ────────

def test_list_sessions_uses_cli_when_available():
    text = _fixture('sessions_list.txt')
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, text)

    a = agents.GrokAdapter(run_fn=fake_run)
    refs = a.list_sessions(_project('/p/a'), Settings())
    assert [r.id for r in refs] == [PROBE_ID_NEWER, PROBE_ID_OLDER]
    # It ran `grok sessions list`.
    argv0, _ = calls[0]
    assert argv0[:3] == ['grok', 'sessions', 'list']


def test_list_sessions_runs_cli_in_project_dir():
    """T-B2: the CLI MUST execute with cwd = the project's path — `grok sessions
    list` is CWD-SCOPED (probe Q4). The run_fn contract is (argv, cwd), exactly
    like opencode's (the P2 cwd-contract test pattern)."""
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, '')

    a = agents.GrokAdapter(run_fn=fake_run)
    a.list_sessions(_project('/home/u/proj-x'), Settings())
    assert calls, 'CLI was never invoked'
    assert calls[0][1] == '/home/u/proj-x'


def test_list_sessions_custom_binary_used_in_cli():
    calls = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        return (0, '')

    a = agents.GrokAdapter(run_fn=fake_run)
    s = Settings(agents={'grok': {'binary': '/opt/grok/grok'}})
    a.list_sessions(_project('/p/a'), s)
    assert calls[0][0][0] == '/opt/grok/grok'


def test_list_sessions_cli_failure_returns_empty():
    def fake_run(argv, cwd):
        return None
    a = agents.GrokAdapter(run_fn=fake_run)
    assert a.list_sessions(_project('/p/a'), Settings()) == []


def test_list_sessions_cli_nonzero_returns_empty():
    def fake_run(argv, cwd):
        return (1, 'Error: No session found')
    a = agents.GrokAdapter(run_fn=fake_run)
    assert a.list_sessions(_project('/p/a'), Settings()) == []
