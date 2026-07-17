"""Remote project groups — argv builders, parse, fetch/push (mocked SSH)."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from hosts import HostProfile
from project_groups import (
    GroupForest,
    GroupNode,
    empty_forest,
    forest_to_dict,
)
from remote_groups import fetch_project_groups, push_project_groups
from ssh_transport import (
    REMOTE_GROUPS_MAX_BYTES,
    REMOTE_GROUPS_REL,
    build_fetch_project_groups_argv,
    build_push_project_groups_argv,
    parse_fetch_groups_stdout,
)


def _remote_script_body(argv):
    token = argv[-1]
    assert token.startswith('bash -lc '), token
    import shlex
    parts = shlex.split(token)
    assert parts[0] == 'bash' and parts[1] == '-lc'
    return parts[2]


def _profile(target: str = 'box') -> HostProfile:
    return HostProfile(id='host1', ssh_target=target, display_name='Box')


# ── argv builders ─────────────────────────────────────────────────────────────

def test_fetch_argv_batch_and_path():
    argv = build_fetch_project_groups_argv('user@host')
    assert argv[0] == 'ssh'
    assert 'BatchMode=yes' in argv
    assert argv[-2] == 'user@host'
    assert argv[-1].startswith('bash -lc ')
    script = _remote_script_body(argv)
    assert REMOTE_GROUPS_REL in script or 'project_groups.json' in script
    assert '$HOME' in script
    # Missing → early exit 0; present → head (bounded); no force exit 0 after read.
    assert 'if [ ! -f "$f" ]; then exit 0; fi' in script
    assert f'head -c {REMOTE_GROUPS_MAX_BYTES + 1}' in script
    assert not script.rstrip().endswith('exit 0')
    assert 'fi; exit 0' not in script
    assert 'fi;exit 0' not in script.replace(' ', '')


def test_push_argv_base64_safety_and_atomic():
    payload = '{"version":1,"groups":[],"membership":{}}\n'
    # Include characters that would break an unquoted shell if raw JSON leaked.
    evil = '{"a":"$(rm -rf /)","b":"`id`","c":"\'\\"}\n'
    for text in (payload, evil):
        argv = build_push_project_groups_argv('box', text)
        assert argv[0] == 'ssh'
        assert 'BatchMode=yes' in argv
        assert argv[-2] == 'box'
        script = _remote_script_body(argv)
        assert 'mkdir -p' in script
        assert 'mktemp' in script
        assert 'base64 -d' in script
        assert 'mv -f' in script
        assert 'project_groups.json' in script
        assert 'rm -f' in script  # cleanup temp on decode fail
        # Raw JSON must not appear unquoted in the remote script.
        assert '"version":1' not in script
        assert '$(rm -rf /)' not in script
        assert '`id`' not in script
        # Base64 of the payload is embedded (quoted).
        b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
        assert b64 in script


def test_push_argv_rejects_oversized():
    huge = 'x' * (REMOTE_GROUPS_MAX_BYTES + 1)
    with pytest.raises(ValueError, match='too large'):
        build_push_project_groups_argv('box', huge)
    # Exactly at cap is allowed
    at_cap = 'x' * REMOTE_GROUPS_MAX_BYTES
    argv = build_push_project_groups_argv('box', at_cap)
    assert argv[0] == 'ssh'


def test_push_argv_roundtrip_decode():
    """Remote script's base64 payload decodes back to the input JSON."""
    text = json.dumps({'version': 1, 'groups': [{'id': 'g1', 'name': 'A'}]})
    argv = build_push_project_groups_argv('h', text)
    script = _remote_script_body(argv)
    # Extract the quoted base64 blob after echo
    import shlex
    # script contains: echo 'BASE64' | base64 -d
    assert 'echo ' in script
    # Find base64 segment: after "echo " take next shlex token
    idx = script.index('echo ')
    rest = script[idx + len('echo '):]
    token = shlex.split(rest)[0]
    assert base64.b64decode(token).decode('utf-8') == text


# ── parse_fetch_groups_stdout ─────────────────────────────────────────────────

def test_parse_empty_and_whitespace():
    assert parse_fetch_groups_stdout('') == (None, None)
    assert parse_fetch_groups_stdout('   \n\t  ') == (None, None)


def test_parse_valid_object():
    data, err = parse_fetch_groups_stdout(
        '{"version":1,"groups":[],"membership":{}}'
    )
    assert err is None
    assert data == {'version': 1, 'groups': [], 'membership': {}}


def test_parse_invalid_json():
    data, err = parse_fetch_groups_stdout('{not json')
    assert data is None
    assert err is not None
    assert err.startswith('invalid json:')


def test_parse_non_object():
    data, err = parse_fetch_groups_stdout('[1,2,3]')
    assert data is None
    assert err == 'invalid top-level type'
    data2, err2 = parse_fetch_groups_stdout('"string"')
    assert data2 is None
    assert err2 == 'invalid top-level type'


def test_parse_oversized():
    body = '{' + ('"k":' + '"x"' * 100 + ',') * 50 + '"z":1}'
    # Force small cap
    data, err = parse_fetch_groups_stdout(body, max_bytes=10)
    assert data is None
    assert err == 'too large'
    # Default / explicit cap: just under limit is fine if valid JSON object
    small = '{"a":1}'
    data2, err2 = parse_fetch_groups_stdout(small, max_bytes=REMOTE_GROUPS_MAX_BYTES)
    assert err2 is None
    assert data2 == {'a': 1}
    # max+1 bytes (as head would emit for oversize remote file)
    over = 'x' * (REMOTE_GROUPS_MAX_BYTES + 1)
    data3, err3 = parse_fetch_groups_stdout(over)
    assert data3 is None
    assert err3 == 'too large'


# ── fetch / push orchestration (mocked run_ssh) ───────────────────────────────

def test_fetch_success():
    body = json.dumps({
        'version': 1,
        'groups': [
            {'id': 'g1', 'name': 'Team', 'parent_id': None, 'expanded': True},
        ],
        'membership': {'ssh:host1:proj': 'g1'},
    })
    with patch('remote_groups.run_ssh', return_value=(0, body, '')) as run:
        forest, err, status = fetch_project_groups(_profile())
    assert err is None
    assert status == 'ok'
    assert 'g1' in forest.groups
    assert forest.groups['g1'].name == 'Team'
    assert forest.membership['ssh:host1:proj'] == 'g1'
    run.assert_called_once()
    argv = run.call_args[0][0]
    assert 'box' in argv or argv[-2] == 'box'


def test_fetch_missing_file():
    with patch('remote_groups.run_ssh', return_value=(0, '', '')):
        forest, err, status = fetch_project_groups(_profile())
    assert err is None
    assert status == 'missing'
    assert forest.groups == {}
    assert forest.membership == {}


def test_fetch_ssh_fail():
    with patch(
        'remote_groups.run_ssh',
        return_value=(255, '', 'Connection refused'),
    ):
        forest, err, status = fetch_project_groups(_profile())
    assert forest.groups == {}
    assert err is not None
    assert status == 'error'
    assert 'Connection refused' in err or 'ssh failed' in err


def test_fetch_rc_nonzero_empty_stdout_is_error():
    """Unreadable/cat fail: non-zero rc must not be treated as missing file."""
    with patch('remote_groups.run_ssh', return_value=(1, '', 'Permission denied')):
        forest, err, status = fetch_project_groups(_profile())
    assert forest.groups == {}
    assert err is not None
    assert status == 'error'
    assert 'Permission denied' in err or 'ssh failed' in err


def test_fetch_oversized_surfaces_error():
    body = 'x' * (REMOTE_GROUPS_MAX_BYTES + 1)
    with patch('remote_groups.run_ssh', return_value=(0, body, '')):
        forest, err, status = fetch_project_groups(_profile())
    assert forest.groups == {}
    assert err == 'too large'
    assert status == 'invalid'


def test_fetch_invalid_json_surfaces_error():
    with patch('remote_groups.run_ssh', return_value=(0, '{bad', '')):
        forest, err, status = fetch_project_groups(_profile())
    assert forest.groups == {}
    assert err is not None
    assert status == 'invalid'
    assert 'invalid json' in err


def test_push_success():
    forest = GroupForest(
        groups={
            'g1': GroupNode(id='g1', name='A', parent_id=None, expanded=True),
        },
        membership={},
    )
    with patch('remote_groups.run_ssh', return_value=(0, '', '')) as run:
        ok, err = push_project_groups(_profile('cage'), forest)
    assert ok is True
    assert err is None
    run.assert_called_once()
    argv = run.call_args[0][0]
    assert argv[-2] == 'cage'
    script = _remote_script_body(argv)
    # Payload is forest_to_dict as JSON, base64-encoded
    expected = json.dumps(forest_to_dict(forest), indent=2) + '\n'
    b64 = base64.b64encode(expected.encode('utf-8')).decode('ascii')
    assert b64 in script


def test_push_fail():
    with patch(
        'remote_groups.run_ssh',
        return_value=(1, '', 'disk full'),
    ):
        ok, err = push_project_groups(_profile(), empty_forest())
    assert ok is False
    assert err is not None
    assert 'disk full' in err


def test_push_too_large_no_ssh():
    huge = 'x' * (REMOTE_GROUPS_MAX_BYTES + 1)
    with patch('remote_groups.json.dumps', return_value=huge), patch(
        'remote_groups.run_ssh',
    ) as run:
        ok, err = push_project_groups(_profile(), empty_forest())
    assert ok is False
    assert err == 'too large'
    run.assert_not_called()


def test_fetch_timeout_kw():
    with patch('remote_groups.run_ssh', return_value=(0, '', '')) as run:
        fetch_project_groups(_profile(), timeout=7)
    assert run.call_args.kwargs.get('timeout') == 7 or (
        len(run.call_args[0]) > 1 and run.call_args[0][1] == 7
    ) or run.call_args[1].get('timeout') == 7
