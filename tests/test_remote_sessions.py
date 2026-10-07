"""Remote session listing helpers + zellij-mode restore path notes (no GTK)."""

from types import SimpleNamespace

from hosts import HostProfile
from model import Project
from remote_sessions import (
    match_session_project_path,
    parse_claude_history_text,
    parse_opencode_sessions_remote,
    list_remote_claude_sessions,
    list_remote_cli_sessions,
    list_remote_kimi_sessions,
    is_remote_project,
)
from harnesses import SessionRef, parse_grok_session_list


def _remote_proj(name='demo', hid='h1'):
    return Project(
        name=name,
        path=f'ssh:{hid}:{name}',
        host_id=hid,
        remote_cwd=f'~/.ProjectMan/projects/{name}',
    )


def test_is_remote_project():
    assert is_remote_project(_remote_proj()) is True
    assert is_remote_project(Project(name='x', path='/tmp/x')) is False


def test_match_session_project_path_by_basename():
    p = _remote_proj('demo')
    assert match_session_project_path('/home/u/.ProjectMan/projects/demo', p)
    assert match_session_project_path('/other/demo', p)
    assert not match_session_project_path('/home/u/.ProjectMan/projects/other', p)


def test_parse_claude_history_text_filters_project():
    text = '\n'.join([
        '{"sessionId":"s1","project":"/home/u/.ProjectMan/projects/demo","timestamp":10,"display":"hi"}',
        '{"sessionId":"s2","project":"/home/u/.ProjectMan/projects/other","timestamp":20,"display":"no"}',
        '{"sessionId":"s1","project":"/home/u/.ProjectMan/projects/demo","timestamp":30,"display":"hi2"}',
    ])
    refs = parse_claude_history_text(text, _remote_proj('demo'))
    assert len(refs) == 1
    assert refs[0].id == 's1'
    assert refs[0].last_active == 30


def test_list_remote_claude_sessions_via_mock_ssh():
    history = (
        '{"sessionId":"abc","project":"/home/x/.ProjectMan/projects/demo",'
        '"timestamp":5,"display":"t"}\n'
    )
    calls = []

    def run_ssh(argv, timeout=12):
        calls.append(argv)
        return 0, history, ''

    prof = HostProfile(id='h1', ssh_target='localhost')
    refs = list_remote_claude_sessions(prof, _remote_proj(), run_ssh_fn=run_ssh)
    assert len(refs) == 1 and refs[0].id == 'abc'
    assert calls and 'ssh' in calls[0][0]


def test_list_remote_grok_cli_sessions():
    sample = (
        'SESSION ID                            CREATED     UPDATED     STATUS      SUMMARY\n'
        '019eb297-fa74-7741-863e-d8aa822ac7bf  2026-06-10  2026-06-10  local  Hello remote\n'
    )

    def run_ssh(argv, timeout=12):
        return 0, sample, ''

    prof = HostProfile(id='h1', ssh_target='localhost')
    refs = list_remote_cli_sessions(
        prof, _remote_proj(),
        ['grok', 'sessions', 'list', '-n', '50'],
        parse_grok_session_list,
        run_ssh_fn=run_ssh,
    )
    assert len(refs) == 1
    assert refs[0].id.startswith('019eb297')


def test_parse_opencode_sessions_remote():
    import json
    data = [
        {'id': 'ses_1', 'directory': '/home/u/.ProjectMan/projects/demo',
         'title': 'A', 'updated': 100},
        {'id': 'ses_2', 'directory': '/home/u/.ProjectMan/projects/other',
         'title': 'B', 'updated': 200},
    ]
    refs = parse_opencode_sessions_remote(json.dumps(data), _remote_proj('demo'))
    assert len(refs) == 1 and refs[0].id == 'ses_1'


def test_list_remote_kimi_index():
    text = (
        '{"sessionId":"k1","sessionDir":"/x","workDir":"/home/u/.ProjectMan/projects/demo"}\n'
        '{"sessionId":"k2","sessionDir":"/y","workDir":"/home/u/.ProjectMan/projects/other"}\n'
    )

    def run_ssh(argv, timeout=12):
        return 0, text, ''

    prof = HostProfile(id='h1', ssh_target='localhost')
    refs = list_remote_kimi_sessions(prof, _remote_proj(), run_ssh_fn=run_ssh)
    assert len(refs) == 1 and refs[0].id == 'k1'


def test_claude_adapter_routes_remote(monkeypatch):
    from harnesses import ClaudeAdapter
    from settings import Settings

    called = {}

    def fake_list(prof, project, **kw):
        called['yes'] = True
        return [SessionRef(id='r1', title='t', last_active=1)]

    monkeypatch.setattr(
        'remote_sessions.list_remote_claude_sessions', fake_list)
    # host_profiles
    s = Settings(hosts={
        'h1': {
            'id': 'h1', 'ssh_target': 'localhost',
            'remote_projects_dir': '~/.ProjectMan/projects',
            'binaries': {},
        },
    })
    a = ClaudeAdapter()
    refs = a.list_sessions(_remote_proj(), s)
    assert called.get('yes')
    assert refs and refs[0].id == 'r1'
