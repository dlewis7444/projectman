"""Kimi Code session-index parsing + adapter spawn golden tests."""
import os
import shutil

import pytest

import harnesses
from settings import Settings

FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'kimi')


class _P:
    def __init__(self, path='/home/user/my-project'):
        self.path = path


# ── pure parsers ──────────────────────────────────────────────────────────────

def test_parse_kimi_session_index_lines_basic():
    with open(os.path.join(FIXDIR, 'session_index.jsonl')) as f:
        text = f.read()
    entries = harnesses.parse_kimi_session_index_lines(text)
    assert len(entries) == 4
    assert entries[0]['sessionId'].startswith('session_aaa')
    assert entries[1]['workDir'] == '/home/user/my-project'


def test_parse_kimi_session_index_defensive():
    assert harnesses.parse_kimi_session_index_lines('') == []
    assert harnesses.parse_kimi_session_index_lines('not json\n') == []
    assert harnesses.parse_kimi_session_index_lines('{"no":"id"}\n') == []
    assert harnesses.parse_kimi_session_index_lines(None) == []


def test_session_ref_from_kimi_state():
    with open(os.path.join(FIXDIR, 'state_new.json')) as f:
        import json
        state = json.load(f)
    ref = harnesses.session_ref_from_kimi_state(
        'session_ddd44444-4444-4444-4444-444444444444', state)
    assert ref.id.startswith('session_ddd')
    assert ref.title == 'newest session'
    assert ref.last_active > 0


def test_session_ref_fallback_title_and_created():
    ref = harnesses.session_ref_from_kimi_state('s1', {
        'lastPrompt': 'only prompt',
        'createdAt': '2026-01-01T00:00:00Z',
    })
    assert ref.title == 'only prompt'
    assert ref.last_active > 0


def test_session_ref_garbage_does_not_raise():
    ref = harnesses.session_ref_from_kimi_state('s', None)
    assert ref.id == 's'
    assert ref.last_active == 0


def test_list_kimi_sessions_from_home_filters_and_caps(tmp_path):
    """Build a mini ~/.kimi-code tree and verify workDir filter + newest-first."""
    home = tmp_path
    kc = home / '.kimi-code'
    # Session dirs with state
    states = {
        'session_bbb22222-2222-2222-2222-222222222222': 'state_mid.json',
        'session_ccc33333-3333-3333-3333-333333333333': 'state_old.json',
        'session_ddd44444-4444-4444-4444-444444444444': 'state_new.json',
        'session_aaa11111-1111-1111-1111-111111111111': 'state_old.json',
    }
    index_lines = []
    for sid, state_name in states.items():
        wd = ('/home/user/other-project' if sid.startswith('session_aaa')
              else '/home/user/my-project')
        sdir = kc / 'sessions' / f'wd_{sid[-8:]}' / sid
        sdir.mkdir(parents=True)
        shutil.copy(os.path.join(FIXDIR, state_name), sdir / 'state.json')
        # trailing slash on one entry to test normalize
        if sid.startswith('session_ccc'):
            wd_out = wd + '/'
        else:
            wd_out = wd
        index_lines.append(
            f'{{"sessionId":"{sid}","sessionDir":"{sdir}","workDir":"{wd_out}"}}'
        )
    (kc / 'session_index.jsonl').write_text('\n'.join(index_lines) + '\n')

    refs = harnesses.list_kimi_sessions_from_home(
        str(home), '/home/user/my-project',
        realpath=lambda p: p,  # keep synthetic paths as-is
    )
    assert len(refs) == 3
    # Newest first
    assert refs[0].title == 'newest session'
    assert refs[1].title == 'mid session'
    assert refs[2].title == 'old session'
    # Other project excluded
    assert all(not r.id.startswith('session_aaa') for r in refs)


def test_list_kimi_sessions_cap_7(tmp_path):
    home = tmp_path
    kc = home / '.kimi-code' / 'sessions'
    lines = []
    for i in range(10):
        sid = f'session_{i:04d}'
        sdir = kc / sid
        sdir.mkdir(parents=True)
        (sdir / 'state.json').write_text(
            f'{{"title":"t{i}","updatedAt":"2026-07-{10+i:02d}T00:00:00Z",'
            f'"workDir":"/p"}}'
        )
        lines.append(
            f'{{"sessionId":"{sid}","sessionDir":"{sdir}","workDir":"/p"}}'
        )
    (home / '.kimi-code' / 'session_index.jsonl').write_text('\n'.join(lines))
    refs = harnesses.list_kimi_sessions_from_home(
        str(home), '/p', realpath=lambda p: p)
    assert len(refs) == 7
    assert refs[0].title == 't9'  # newest


def test_list_kimi_sessions_missing_home(tmp_path):
    assert harnesses.list_kimi_sessions_from_home(str(tmp_path), '/p') == []


# ── adapter spawn goldens ─────────────────────────────────────────────────────

def test_kimi_adapter_registered():
    a = harnesses.get_adapter('kimi')
    assert a.id == 'kimi'
    assert a.display_name == 'Kimi Code'
    assert a.caps.continue_ is True
    assert a.caps.resume_by_id is True
    assert a.caps.sessions is True
    assert a.caps.rich_status is True
    assert a.caps.model_select is True
    assert a.caps.headless_json is True
    assert a.caps.continue_falls_back_to_fresh is False


def test_kimi_fresh_argv():
    a = harnesses.get_adapter('kimi')
    s = Settings()
    assert a.fresh_argv(s, _P()) == ['kimi']


def test_kimi_fresh_with_model():
    a = harnesses.get_adapter('kimi')
    s = Settings(model_pins={'/home/user/my-project': 'kimi-code/k3'})
    assert a.fresh_argv(s, _P()) == ['kimi', '-m', 'kimi-code/k3']


def test_kimi_continue_no_fallback_wrapper():
    """continue_falls_back_to_fresh=False → trap + exec only, no || fresh."""
    a = harnesses.get_adapter('kimi')
    s = Settings()
    plan = a.spawn_plan(s, _P(), 'continue')
    assert plan.argv[0] == 'bash'
    script = plan.argv[-1]
    assert "trap 'exit 143' TERM HUP;" in script
    assert 's=$?' not in script
    assert '||' not in script
    assert 'exec kimi -c' in script or script.endswith('exec kimi -c')
    assert plan.env is None


def test_kimi_continue_model_folded():
    a = harnesses.get_adapter('kimi')
    s = Settings(model_pins={'/home/user/my-project': 'kimi-code/k3'})
    plan = a.spawn_plan(s, _P(), 'continue')
    script = plan.argv[-1]
    assert 'kimi -m kimi-code/k3 -c' in script
    assert '||' not in script


def test_kimi_resume_uses_S_flag():
    a = harnesses.get_adapter('kimi')
    s = Settings()
    plan = a.spawn_plan(s, _P(), 'resume',
                        session_id='session_abc')
    assert plan.argv == ['kimi', '-S', 'session_abc']


def test_kimi_resume_with_model():
    a = harnesses.get_adapter('kimi')
    s = Settings(model_pins={'/home/user/my-project': 'kimi-code/k3'})
    plan = a.spawn_plan(s, _P(), 'resume', session_id='sid')
    assert plan.argv == ['kimi', '-m', 'kimi-code/k3', '-S', 'sid']


def test_kimi_zellij_continue_no_pipe():
    a = harnesses.get_adapter('kimi')
    s = Settings()
    cmd = a.zellij_continue_command(s, _P())
    assert cmd == 'kimi -c'
    assert '||' not in cmd


def test_kimi_zellij_continue_with_model():
    a = harnesses.get_adapter('kimi')
    s = Settings(model_pins={'/home/user/my-project': 'kimi-code/k3'})
    cmd = a.zellij_continue_command(s, _P())
    assert cmd == 'kimi -m kimi-code/k3 -c'
    assert '||' not in cmd


def test_kimi_zellij_spawn_env_none():
    a = harnesses.get_adapter('kimi')
    assert a.zellij_spawn_env(Settings(), _P()) == (None, None)


def test_kimi_custom_binary():
    a = harnesses.get_adapter('kimi')
    s = Settings(harnesses={'kimi': {'binary': '/opt/kimi/bin/kimi'}})
    assert a.fresh_argv(s, _P()) == ['/opt/kimi/bin/kimi']


def test_kimi_install_command():
    a = harnesses.get_adapter('kimi')
    assert 'code.kimi.com' in a.install_command
    assert harnesses.harness_install_command('kimi') == a.install_command


def test_kimi_list_sessions_via_adapter(tmp_path):
    home = tmp_path
    kc = home / '.kimi-code'
    sdir = kc / 'sessions' / 'wd' / 'session_x'
    sdir.mkdir(parents=True)
    shutil.copy(os.path.join(FIXDIR, 'state_new.json'), sdir / 'state.json')
    (kc / 'session_index.jsonl').write_text(
        f'{{"sessionId":"session_x","sessionDir":"{sdir}",'
        f'"workDir":"/home/user/my-project"}}\n'
    )
    a = harnesses.KimiAdapter(home=str(home))
    refs = a.list_sessions(_P(), Settings())
    assert len(refs) == 1
    assert refs[0].title == 'newest session'
