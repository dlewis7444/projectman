"""Phase 2 SSH transport — pure builders, parse, classify (no network)."""
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ssh_transport import (
    HealthState,
    REMOTE_GROUPS_MAX_BYTES,
    REMOTE_GROUPS_REL,
    build_ensure_projects_dir_argv,
    build_fetch_project_groups_argv,
    build_list_projects_argv,
    build_mkdir_project_argv,
    build_push_project_groups_argv,
    build_remote_shell_command,
    build_rename_project_argv,
    build_rmdir_project_argv,
    build_ssh_base_argv,
    build_ssh_spawn_argv,
    classify_health,
    parse_fetch_groups_stdout,
    parse_ls_project_names,
    run_ssh,
)


# ── build_ssh_base_argv ───────────────────────────────────────────────────────

def test_base_argv_batch_default():
    argv = build_ssh_base_argv('user@host')
    assert argv[0] == 'ssh'
    assert '-o' in argv
    assert 'BatchMode=yes' in argv
    assert 'ConnectTimeout=5' in argv
    assert argv[-1] == 'user@host'
    # Never auto-accept host keys
    joined = ' '.join(argv)
    assert 'StrictHostKeyChecking=no' not in joined
    assert 'accept-new' not in joined


def test_base_argv_no_batch():
    argv = build_ssh_base_argv('box', batch=False, connect_timeout=12)
    assert 'BatchMode=yes' not in argv
    assert 'ConnectTimeout=12' in argv
    assert argv == ['ssh', '-o', 'ConnectTimeout=12', 'box']


# ── build_remote_shell_command ────────────────────────────────────────────────

def test_remote_shell_cd_and_exec():
    cmd = build_remote_shell_command('/home/u/proj', ['claude', '--resume'])
    assert 'cd -- /home/u/proj' in cmd
    assert 'export PATH=' in cmd  # non-interactive PATH bootstrap
    assert 'exec ' in cmd
    assert 'claude' in cmd
    assert '--resume' in cmd
    # Spaces in cwd must be quoted
    cmd2 = build_remote_shell_command('/path/with spaces', ['echo', 'hi'])
    assert "cd -- '/path/with spaces'" in cmd2 or 'cd -- /path/with\\ spaces' in cmd2


def test_remote_shell_exports_env():
    cmd = build_remote_shell_command(
        '/cwd', ['claude'], env={'ANTHROPIC_BASE_URL': 'http://x', 'FOO': 'bar'},
    )
    # filter happens at spawn argv layer; build_remote_shell_command exports what it gets
    assert 'export ANTHROPIC_BASE_URL=' in cmd or "export ANTHROPIC_BASE_URL=" in cmd
    assert 'cd ' in cmd
    assert cmd.index('export PATH') < cmd.index('cd')
    assert 'exec' in cmd


def test_remote_shell_no_env():
    cmd = build_remote_shell_command('/c', ['true'])
    # PATH bootstrap is always present; no arbitrary env exports.
    assert 'export PATH=' in cmd
    assert 'cd -- /c' in cmd
    assert 'GROK_WORKSPACE_ROOT' in cmd
    assert 'exec true' in cmd
    assert 'ANTHROPIC' not in cmd


# ── build_ssh_spawn_argv ──────────────────────────────────────────────────────

def test_spawn_argv_has_tt_and_bash_lc():
    argv = build_ssh_spawn_argv(
        'cage', '/remote/proj', ['claude', '-p', 'hi'],
        env={'ANTHROPIC_BASE_URL': 'http://x', 'HOME': '/home/wrong', 'A': 'b'},
    )
    assert argv[0] == 'ssh'
    assert '-tt' in argv
    # -tt before target
    assert argv.index('-tt') < argv.index('cage')
    # One remote command token after target (not bash / -lc / script).
    assert argv[-2] == 'cage'
    remote = argv[-1]
    assert remote.startswith('bash -lc ')
    assert 'cd ' in remote
    assert 'exec ' in remote
    # Only harness-relevant env exported — not laptop HOME or random keys.
    assert 'ANTHROPIC_BASE_URL' in remote
    assert 'HOME' not in remote or 'export HOME' not in remote
    assert "export A=" not in remote
    assert 'BatchMode=yes' in argv


def test_spawn_without_env():
    argv = build_ssh_spawn_argv('h', '/p', ['sh'])
    remote = argv[-1]
    assert 'export PATH=' in remote
    assert 'ANTHROPIC' not in remote


def _remote_script_body(argv):
    """Unwrap ``bash -lc '…'`` single remote token to the inner script."""
    token = argv[-1]
    assert token.startswith('bash -lc '), token
    # shlex-split the token: ['bash', '-lc', '<script>']
    import shlex
    parts = shlex.split(token)
    assert parts[0] == 'bash' and parts[1] == '-lc'
    return parts[2]


# ── list / ensure / mkdir / rename / rmdir ────────────────────────────────────

def test_list_projects_argv_tilde_and_ls():
    argv = build_list_projects_argv('box', '~/.ProjectMan/projects')
    assert argv[0] == 'ssh'
    # Single remote command after target
    assert argv[-2] == 'box'
    assert argv[-1].startswith('bash -lc ')
    script = _remote_script_body(argv)
    assert 'mkdir -p' in script
    assert 'ls -1A' in script
    # tilde expand via single $HOME/… expansion
    assert '$HOME/.ProjectMan/projects' in script or '"$HOME' in script
    assert 'BatchMode=yes' in argv


def test_list_projects_absolute_dir():
    argv = build_list_projects_argv('box', '/opt/projects')
    script = _remote_script_body(argv)
    assert '/opt/projects' in script
    assert 'ls -1A' in script


def test_ensure_projects_dir_argv():
    argv = build_ensure_projects_dir_argv('box', '~/proj')
    script = _remote_script_body(argv)
    assert 'mkdir -p' in script
    assert 'ls ' not in script  # ensure only, no list
    assert '$HOME/proj' in script or '"$HOME' in script


def test_mkdir_project_argv_safe():
    argv = build_mkdir_project_argv('box', '~/.ProjectMan/projects', 'my-app')
    script = _remote_script_body(argv)
    assert 'my-app' in script
    assert 'mkdir' in script


def test_mkdir_rejects_unsafe_names():
    for bad in ('', 'a/b', '..', '.', '.hidden', 'x\0y'):
        with pytest.raises(ValueError):
            build_mkdir_project_argv('box', '~/p', bad)


def test_rename_project_argv():
    argv = build_rename_project_argv('box', '/p', 'old', 'new')
    script = _remote_script_body(argv)
    assert 'mv' in script
    assert 'old' in script
    assert 'new' in script


def test_rename_rejects_unsafe():
    with pytest.raises(ValueError):
        build_rename_project_argv('box', '/p', 'ok', '../x')
    with pytest.raises(ValueError):
        build_rename_project_argv('box', '/p', 'a/b', 'ok')


def test_rmdir_project_argv_uses_rm_rf_on_safe_name():
    argv = build_rmdir_project_argv('box', '~/projects', 'doomed')
    script = _remote_script_body(argv)
    assert 'rm -rf' in script
    assert 'doomed' in script
    # must not be a free-form path; name is a segment under $dir
    assert '"$dir"' in script or "$dir" in script


def test_rmdir_rejects_unsafe():
    for bad in ('..', 'a/b', '.git', ''):
        with pytest.raises(ValueError):
            build_rmdir_project_argv('box', '/p', bad)


# ── project groups fetch/push argv ────────────────────────────────────────────

def test_fetch_project_groups_argv():
    argv = build_fetch_project_groups_argv('box')
    assert argv[-2] == 'box'
    script = _remote_script_body(argv)
    assert REMOTE_GROUPS_REL in script or 'project_groups.json' in script
    # Missing early exit 0; no trailing unconditional exit 0 after read.
    assert 'if [ ! -f "$f" ]; then exit 0; fi' in script
    assert f'head -c {REMOTE_GROUPS_MAX_BYTES + 1}' in script
    assert not script.rstrip().endswith('exit 0')
    assert 'BatchMode=yes' in argv


def test_push_project_groups_argv_no_raw_json():
    payload = '{"version":1,"groups":[]}'
    argv = build_push_project_groups_argv('box', payload)
    script = _remote_script_body(argv)
    assert 'base64 -d' in script
    assert 'mktemp' in script
    assert 'mv -f' in script
    assert '"version":1' not in script


def test_push_project_groups_argv_too_large():
    with pytest.raises(ValueError, match='too large'):
        build_push_project_groups_argv('box', 'y' * (REMOTE_GROUPS_MAX_BYTES + 1))


def test_parse_fetch_groups_stdout_matrix():
    assert parse_fetch_groups_stdout('') == (None, None)
    d, e = parse_fetch_groups_stdout('{"a":1}')
    assert e is None and d == {'a': 1}
    d, e = parse_fetch_groups_stdout('[1]')
    assert d is None and e == 'invalid top-level type'
    d, e = parse_fetch_groups_stdout('{', max_bytes=REMOTE_GROUPS_MAX_BYTES)
    assert d is None and e is not None and e.startswith('invalid json')
    d, e = parse_fetch_groups_stdout('x' * 100, max_bytes=10)
    assert d is None and e == 'too large'
    d, e = parse_fetch_groups_stdout('x' * (REMOTE_GROUPS_MAX_BYTES + 1))
    assert d is None and e == 'too large'


# ── parse_ls_project_names ────────────────────────────────────────────────────

def test_parse_ls_skips_dots_and_empty():
    raw = '\n'.join(['', '.', '..', '.hidden', 'foo', 'bar-baz', '  ', 'ok'])
    # strip of pure whitespace lines: '  ' is not empty after strip('\r') only —
    # implementation strips only \r; blank lines (empty after split) skipped.
    names = parse_ls_project_names(raw)
    assert 'foo' in names
    assert 'bar-baz' in names
    assert 'ok' in names
    assert '.' not in names
    assert '..' not in names
    assert '.hidden' not in names
    assert '' not in names


def test_parse_ls_empty_and_none_like():
    assert parse_ls_project_names('') == []
    assert parse_ls_project_names('only\n') == ['only']


def test_parse_ls_preserves_order():
    assert parse_ls_project_names('z\na\nm\n') == ['z', 'a', 'm']


# ── classify_health ───────────────────────────────────────────────────────────

def test_classify_health_matrix():
    assert classify_health(True, True, checks_enabled=True) == HealthState.GREEN
    assert classify_health(True, False, checks_enabled=True) == HealthState.YELLOW
    assert classify_health(False, True, checks_enabled=True) == HealthState.RED
    assert classify_health(False, False, checks_enabled=True) == HealthState.RED
    # checks off → grey regardless
    assert classify_health(True, True, checks_enabled=False) == HealthState.GREY
    assert classify_health(False, False, checks_enabled=False) == HealthState.GREY


def test_health_state_string_values():
    assert HealthState.GREY == 'grey'
    assert HealthState.GREEN == 'green'
    assert HealthState.YELLOW == 'yellow'
    assert HealthState.RED == 'red'


# ── run_ssh (mocked) ──────────────────────────────────────────────────────────

def test_run_ssh_success():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = 'out'
    fake.stderr = ''
    with patch('ssh_transport.subprocess.run', return_value=fake) as run:
        rc, out, err = run_ssh(['ssh', 'box', 'true'], timeout=3)
    assert (rc, out, err) == (0, 'out', '')
    run.assert_called_once()
    kwargs = run.call_args.kwargs
    assert kwargs['timeout'] == 3
    assert kwargs['capture_output'] is True
    assert kwargs['text'] is True


def test_run_ssh_timeout():
    with patch(
        'ssh_transport.subprocess.run',
        side_effect=subprocess.TimeoutExpired(cmd=['ssh'], timeout=1),
    ):
        rc, out, err = run_ssh(['ssh', 'box'], timeout=1)
    assert rc == 124
    assert 'timeout' in err.lower() or err == ''


def test_run_ssh_oserror():
    with patch(
        'ssh_transport.subprocess.run',
        side_effect=FileNotFoundError('ssh'),
    ):
        rc, out, err = run_ssh(['ssh', 'box'])
    assert rc == 127
    assert out == ''
    assert 'ssh' in err


# ── remote script quoting smoke (bash -n via local bash) ─────────────────────

def test_remote_scripts_are_valid_bash_syntax():
    """Builders produce scripts that bash -n accepts (no real SSH)."""
    scripts = [
        _remote_script_body(build_list_projects_argv('t', '~/.ProjectMan/projects')),
        _remote_script_body(build_ensure_projects_dir_argv('t', '/opt/p')),
        _remote_script_body(build_mkdir_project_argv('t', '~/p', 'proj1')),
        _remote_script_body(build_rename_project_argv('t', '~/p', 'a', 'b')),
        _remote_script_body(build_rmdir_project_argv('t', '~/p', 'a')),
        _remote_script_body(build_fetch_project_groups_argv('t')),
        _remote_script_body(
            build_push_project_groups_argv('t', '{"version":1,"groups":[]}')
        ),
        build_remote_shell_command(
            '/tmp/x', ['echo', 'hi there'], env={'E': "val'ue"},
        ),
    ]
    for script in scripts:
        r = subprocess.run(
            ['bash', '-n', '-c', script],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f'syntax error in {script!r}: {r.stderr}'


def test_spawn_remote_command_is_single_token_after_target():
    argv = build_ssh_spawn_argv('h', '/cwd', ['claude'])
    # structure: … -tt h 'bash -lc …'  — one remote token only
    assert argv[-2] == 'h'
    assert argv[-1].startswith('bash -lc ')
    assert argv.count('bash') == 0  # not a separate argv element
    assert '-lc' not in argv
