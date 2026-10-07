"""paa_mcp: the bot-owned MCP server (NDJSON JSON-RPC over stdio).

In-process tests drive ``handle_message``/``serve`` with a fake broker on a
scratch unix socket. No live model, no network, no real execution — the
canary tests prove the server never runs anything locally. The install
helpers (ensure_mcp_config / ensure_trusted_folder) run against tmp paths
only; no live grok config is ever touched here.
"""
import io
import json
import os
import socket
import threading

import pytest

import paa_mcp
from paa_mcp import (
    DEFAULT_DEADLINE_SEC,
    ENV_DEADLINE,
    ENV_SOCK,
    ENV_TOKEN,
    OUTPUT_MAX,
    TOOL_NAME,
    REVIEW_UNAVAILABLE,
    ensure_mcp_config,
    ensure_trusted_folder,
    handle_message,
    mcp_config_block,
    mcp_config_path,
    serve,
    server_file_path,
    tool_descriptor,
)

SOCK = None  # set by fixture


@pytest.fixture
def broker_sock(tmp_path):
    """Fake ApprovalBroker: records review requests, replies a canned
    decision (allow with output, or deny with reason, or sleeps)."""
    class FakeBroker:
        def __init__(self):
            self.requests = []
            self.reply = {'decision': 'allow', 'output': 'ok-output'}
            self.delay = 0.0
            self.hang = False

    fake = FakeBroker()
    path = str(tmp_path / 'paa-approve.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(8)
    stop = threading.Event()

    def _serve():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            data = b''
            while not data.endswith(b'\n'):
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                data += chunk
            if data.strip():
                try:
                    fake.requests.append(json.loads(data.decode()))
                except ValueError:
                    pass
                if fake.hang:
                    threading.Event().wait(30)
                    continue
                if fake.delay:
                    threading.Event().wait(fake.delay)
                try:
                    conn.sendall((json.dumps(fake.reply) + '\n').encode())
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    fake.path = path
    fake.stop = lambda: (stop.set(), listener.close())
    yield fake
    fake.stop()


@pytest.fixture
def live_env(broker_sock, monkeypatch):
    """The gate halves exported, as the gate argv would for a rw turn."""
    monkeypatch.setenv(ENV_SOCK, broker_sock.path)
    monkeypatch.setenv(ENV_TOKEN, 'tok-live')
    return broker_sock


def _call(name, arguments=None, msg_id=7):
    params = {'name': name}
    if arguments is not None:
        params['arguments'] = arguments
    return handle_message({'jsonrpc': '2.0', 'id': msg_id,
                           'method': 'tools/call', 'params': params})


# -- handshake ------------------------------------------------------------------


class TestHandshake:
    def test_initialize_echoes_client_protocol_version(self):
        # 2025-11-25 is the protocolVersion grok 1.0.44 sent in the
        # 2026-10-01 live capture; echoing is the negotiated contract.
        response = handle_message({
            'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'grok-cli', 'version': '1.0.44'}}})
        assert response['id'] == 0
        result = response['result']
        assert result['protocolVersion'] == '2025-11-25'
        assert result['capabilities'] == {'tools': {}}
        assert result['serverInfo']['name'] == 'paa-shell'
        assert result['serverInfo']['version']

    def test_initialize_defaults_protocol_version(self):
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        assert response['result']['protocolVersion'] == \
            paa_mcp.DEFAULT_PROTOCOL_VERSION

    def test_initialized_notification_gets_no_response(self):
        assert handle_message(
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None

    def test_unknown_notification_gets_no_response(self):
        assert handle_message(
            {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}) is None

    def test_ping(self):
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 9, 'method': 'ping'})
        assert response == {'jsonrpc': '2.0', 'id': 9, 'result': {}}

    def test_unknown_method_is_jsonrpc_32601(self):
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 3, 'method': 'resources/list'})
        assert response['error']['code'] == -32601
        assert 'resources/list' in response['error']['message']
        assert response['id'] == 3

    def test_non_object_request_is_invalid(self):
        assert handle_message(['not', 'an', 'object'])['error']['code'] == -32600


# -- inertness guard ---------------------------------------------------------------


class TestInertnessGuard:
    def test_tools_list_empty_without_env(self, monkeypatch):
        monkeypatch.delenv(ENV_SOCK, raising=False)
        monkeypatch.delenv(ENV_TOKEN, raising=False)
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response['result']['tools'] == []

    def test_tools_list_empty_with_dead_socket(self, monkeypatch, tmp_path):
        # Both halves set but nothing accepts on the socket: the channel
        # is down, so the tool must not exist.
        monkeypatch.setenv(ENV_SOCK, str(tmp_path / 'nobody-home.sock'))
        monkeypatch.setenv(ENV_TOKEN, 'tok')
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response['result']['tools'] == []

    def test_tools_list_empty_with_half_env(self, broker_sock, monkeypatch):
        monkeypatch.setenv(ENV_SOCK, broker_sock.path)
        monkeypatch.delenv(ENV_TOKEN, raising=False)
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response['result']['tools'] == []

    def test_tools_list_exposes_run_command_when_live(self, live_env):
        response = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        tools = response['result']['tools']
        assert len(tools) == 1
        tool = tools[0]
        assert tool['name'] == TOOL_NAME
        schema = tool['inputSchema']
        assert schema['required'] == ['command', 'reason']
        assert set(schema['properties']) == {'command', 'reason'}

    def test_tool_descriptor_describes_review(self):
        assert 'only way to execute commands' in tool_descriptor()['description']
        assert 'verdict is' in tool_descriptor()['description']


# -- tools/call round trips -----------------------------------------------------------


class TestToolsCall:
    def test_allow_round_trip_returns_output(self, live_env):
        live_env.reply = {'decision': 'allow', 'output': 'total 42\n'}
        response = _call(TOOL_NAME, {'command': 'ls -la',
                                     'reason': 'inspect the directory'})
        result = response['result']
        assert result['isError'] is False
        assert result['content'][0]['text'] == 'total 42\n'
        request = live_env.requests[0]
        assert request['kind'] == 'review'
        assert request['token'] == 'tok-live'
        assert request['tool'] == TOOL_NAME
        assert request['command'] == 'ls -la'
        assert request['reason'] == 'inspect the directory'

    def test_deny_round_trip_is_error_with_reason(self, live_env):
        live_env.reply = {'decision': 'deny',
                          'reason': 'EXCEEDS — user did not ask for this'}
        response = _call(TOOL_NAME, {'command': 'rm -rf build',
                                     'reason': 'cleanup'})
        result = response['result']
        assert result['isError'] is True
        text = result['content'][0]['text']
        assert 'EXCEEDS' in text
        assert 'rm -rf build' not in text  # the echo is the reason, not re-run data

    def test_call_with_dead_channel_is_error(self, monkeypatch, tmp_path):
        # Belt-and-suspenders: even if a stale catalog offered the tool,
        # a dead channel fails closed.
        monkeypatch.setenv(ENV_SOCK, str(tmp_path / 'gone.sock'))
        monkeypatch.setenv(ENV_TOKEN, 'tok')
        response = _call(TOOL_NAME, {'command': 'id', 'reason': 'x'})
        assert response['result']['isError'] is True
        assert REVIEW_UNAVAILABLE in response['result']['content'][0]['text']

    def test_deadline_maps_to_review_unavailable(self, live_env, monkeypatch):
        live_env.hang = True  # broker accepts, never answers
        monkeypatch.setenv(ENV_DEADLINE, '0.3')
        started = __import__('time').monotonic()
        response = _call(TOOL_NAME, {'command': 'id', 'reason': 'x'})
        elapsed = __import__('time').monotonic() - started
        assert elapsed < 10, 'deadline must bound the wait'
        assert response['result']['isError'] is True
        assert REVIEW_UNAVAILABLE in response['result']['content'][0]['text']

    def test_broker_refusal_transport_maps_to_review_unavailable(
            self, live_env):
        live_env.stop()  # nothing left to accept
        response = _call(TOOL_NAME, {'command': 'id', 'reason': 'x'})
        assert response['result']['isError'] is True
        assert REVIEW_UNAVAILABLE in response['result']['content'][0]['text']

    def test_output_truncated_to_15k(self, live_env):
        live_env.reply = {'decision': 'allow',
                          'output': 'x' * (OUTPUT_MAX * 3)}
        response = _call(TOOL_NAME, {'command': 'big', 'reason': 'x'})
        text = response['result']['content'][0]['text']
        assert len(text) <= OUTPUT_MAX
        assert 'xxx' in text

    def test_unknown_tool_is_jsonrpc_32602(self, live_env):
        response = _call('other_tool', {'command': 'id'})
        assert response['error']['code'] == -32602

    @pytest.mark.parametrize('args', [
        {},
        {'command': ''},
        {'command': 'id'},           # reason missing
        {'reason': 'x'},             # command missing
        {'command': '   '},
    ])
    def test_missing_arguments_are_jsonrpc_32602(self, live_env, args):
        response = _call(TOOL_NAME, args)
        assert response['error']['code'] == -32602

    def test_never_executes_locally_on_allow(self, live_env, tmp_path):
        # The canary must not exist even when the broker allows — the
        # server has no execution path at all.
        canary = tmp_path / 'mcp-canary-MUST-NOT-EXIST'
        live_env.reply = {'decision': 'allow', 'output': ''}
        response = _call(TOOL_NAME, {
            'command': f'touch {canary}', 'reason': 'canary test'})
        assert response['result']['isError'] is False
        assert not canary.exists()

    def test_never_executes_locally_on_deny(self, live_env, tmp_path):
        canary = tmp_path / 'mcp-canary-MUST-NOT-EXIST'
        live_env.reply = {'decision': 'deny', 'reason': 'denied'}
        response = _call(TOOL_NAME, {
            'command': f'touch {canary}', 'reason': 'canary test'})
        assert response['result']['isError'] is True
        assert not canary.exists()


# -- stdio plumbing ---------------------------------------------------------------------


class TestServeLoop:
    def _run(self, lines, env):
        old = dict(os.environ)
        os.environ.update(env)
        try:
            stdin = io.BytesIO(b''.join(lines))
            stdout = io.BytesIO()
            rc = serve(stdin, stdout)
            return rc, stdout.getvalue().decode()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_eof_exits_zero(self):
        rc, _ = self._run([], {})
        assert rc == 0

    def test_request_response_pairs(self):
        rc, out = self._run([
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n',
        ], {})
        assert rc == 0
        responses = [json.loads(l) for l in out.splitlines()]
        assert [r['id'] for r in responses] == [1, 2]

    def test_malformed_ndjson_is_ignored(self):
        rc, out = self._run([
            b'this is not json\n',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b'\n',
            b'{"jsonrpc":"2.0","id":5,"method":"ping"}\n',
        ], {})
        assert rc == 0
        responses = [json.loads(l) for l in out.splitlines()]
        assert responses == [{'jsonrpc': '2.0', 'id': 5, 'result': {}}]

    def test_full_session_over_stdio_with_live_channel(self, broker_sock):
        env = {ENV_SOCK: broker_sock.path, ENV_TOKEN: 'tok-live'}
        script = b''.join([
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":'
            b'{"protocolVersion":"2025-11-25"}}\n',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            (b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
             b'{"name":"run_command","arguments":{"command":"echo hi",'
             b'"reason":"probe"}}}\n'),
        ])
        rc, out = self._run([script], env)
        assert rc == 0
        by_id = {json.loads(l)['id']: json.loads(l) for l in out.splitlines()}
        assert by_id[0]['result']['protocolVersion'] == '2025-11-25'
        assert by_id[1]['result']['tools'][0]['name'] == TOOL_NAME
        assert by_id[2]['result']['isError'] is False
        assert broker_sock.requests[0]['command'] == 'echo hi'


# -- serve()-time install helpers ----------------------------------------------------------


class TestMcpConfigInstall:
    def test_block_points_at_this_tree(self):
        block = mcp_config_block('/x/paa_mcp.py', '/usr/bin/python3')
        assert '[mcp_servers.paa-shell]' in block
        assert 'command = "/usr/bin/python3"' in block
        assert 'args = ["/x/paa_mcp.py"]' in block
        assert '${PAA_APPROVAL_SOCK}' in block
        assert '${PAA_APPROVAL_TOKEN}' in block
        assert 'startup_timeout_sec = 15' in block
        assert 'tool_timeout_sec = 180' in block

    def test_creates_config_in_paa_cwd(self, tmp_path):
        path, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is True
        assert path == mcp_config_path(str(tmp_path))
        assert 'paa-shell' in open(path).read()
        again, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is False, 'content-identical rewrite is a no-op'

    def test_preserves_unrelated_content(self, tmp_path):
        grok_dir = tmp_path / '.grok'
        grok_dir.mkdir()
        cfg = grok_dir / 'config.toml'
        cfg.write_text(
            '# my note\n[mcp_servers.cc-vision]\ncommand = "npx"\n')
        path, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is True
        text = open(path).read()
        assert '# my note' in text
        assert '[mcp_servers.cc-vision]' in text
        assert '[mcp_servers.paa-shell]' in text
        _, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is False, 'merge must converge'

    def test_refreshes_stale_own_block(self, tmp_path):
        ensure_mcp_config(str(tmp_path), server_path='/old/paa_mcp.py',
                          python='/usr/bin/python3')
        path, changed = ensure_mcp_config(
            str(tmp_path), server_path='/new/paa_mcp.py', python='/usr/bin/python3')
        assert changed is True
        text = open(path).read()
        assert '/new/paa_mcp.py' in text
        assert '/old/paa_mcp.py' not in text

    def test_replaces_inline_definition(self, tmp_path):
        grok_dir = tmp_path / '.grok'
        grok_dir.mkdir()
        (grok_dir / 'config.toml').write_text(
            '[mcp_servers]\npaa-shell = { command = "python3" }\n')
        path, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is True
        text = open(path).read()
        # No TOML duplicate key may survive; tomllib must parse it.
        import tomllib
        parsed = tomllib.loads(text)
        assert parsed['mcp_servers']['paa-shell']['args'] == ['/x/paa_mcp.py']

    def test_refuses_unmergeable_inline_definition(self, tmp_path, caplog):
        grok_dir = tmp_path / '.grok'
        grok_dir.mkdir()
        # Multi-line inline table we cannot safely strip by line.
        (grok_dir / 'config.toml').write_text(
            '[mcp_servers]\npaa-shell = { command = "python3",\n'
            '                args = ["x"] }\n')
        path, changed = ensure_mcp_config(
            str(tmp_path), server_path='/x/paa_mcp.py', python='/usr/bin/python3')
        assert changed is False, 'fail closed: leave the file untouched'
        assert 'paa-shell = {' in open(path).read()


class TestTrustedFolderInstall:
    def test_appends_and_is_idempotent(self, tmp_path):
        db = str(tmp_path / 'trusted_folders.toml')
        p1, c1 = ensure_trusted_folder('/tmp/some-cwd', trusted_path=db)
        p2, c2 = ensure_trusted_folder('/tmp/some-cwd', trusted_path=db)
        assert (c1, c2) == (True, False)
        import tomllib
        parsed = tomllib.loads(open(db).read())
        entry = parsed['folders']['/tmp/some-cwd']
        assert entry['trusted'] is True
        assert entry['decided_at']

    def test_preserves_existing_entries(self, tmp_path):
        db = str(tmp_path / 'trusted_folders.toml')
        with open(db, 'w') as fh:
            fh.write('[folders."/home/u/proj"]\ntrusted = true\n'
                     'decided_at = 1\n')
        _, changed = ensure_trusted_folder('/home/u/other', trusted_path=db)
        assert changed is True
        text = open(db).read()
        assert '[folders."/home/u/proj"]' in text
        assert '[folders."/home/u/other"]' in text
        import tomllib
        parsed = tomllib.loads(text)
        assert len(parsed['folders']) == 2

    def test_upgrades_untrusted_entry(self, tmp_path):
        db = str(tmp_path / 'trusted_folders.toml')
        with open(db, 'w') as fh:
            fh.write('[folders."/home/u/proj"]\ntrusted = false\n'
                     'decided_at = 1\n')
        _, changed = ensure_trusted_folder('/home/u/proj', trusted_path=db)
        assert changed is True
        import tomllib
        parsed = tomllib.loads(open(db).read())
        assert parsed['folders']['/home/u/proj']['trusted'] is True
        assert parsed['folders']['/home/u/proj']['decided_at'] == 1

    def test_no_duplicate_sections_after_rewrite(self, tmp_path):
        # Regression: a missed "already present" lookup appends a second
        # [folders."x"] table, which is a TOML parse error that kills the
        # WHOLE trust store for grok (seen live 2026-10-01).
        db = str(tmp_path / 'trusted_folders.toml')
        ensure_trusted_folder('/home/u/proj', trusted_path=db)
        ensure_trusted_folder('/home/u/proj', trusted_path=db)
        ensure_trusted_folder('/home/u/proj', trusted_path=db)
        text = open(db).read()
        assert text.count('[folders."/home/u/proj"]') == 1

    def test_refuses_broken_store_and_keeps_bytes(self, tmp_path, caplog):
        # L1: a store that does not parse must not be appended to — the
        # pre-write bytes survive verbatim and the failure is reported.
        db = tmp_path / 'trusted_folders.toml'
        broken = '[folders /unquoted-path]\ntrusted = true\n'
        db.write_text(broken)
        path, changed = ensure_trusted_folder('/home/u/proj',
                                              trusted_path=str(db))
        assert changed is False
        assert db.read_text() == broken, 'original bytes intact'
        assert any('invalid TOML' in r.getMessage()
                   for r in caplog.records), 'failure logged'

    def test_refuses_broken_store_without_trashing_others(self, tmp_path):
        # A store broken by SOMEONE ELSE'S entry: our folder is still not
        # there, the merge would not parse — leave everything untouched.
        db = tmp_path / 'trusted_folders.toml'
        broken = ('[folders."/home/u/good"]\ntrusted = true\ndecided_at = 1\n'
                  '[folders /unquoted]\ntrusted = true\n')
        db.write_text(broken)
        path, changed = ensure_trusted_folder('/home/u/proj',
                                              trusted_path=str(db))
        assert changed is False
        assert db.read_text() == broken

    def test_valid_store_still_writes(self, tmp_path):
        db = str(tmp_path / 'trusted_folders.toml')
        _, changed = ensure_trusted_folder('/home/u/proj', trusted_path=db)
        assert changed is True


def test_server_file_path_is_this_tree():
    assert server_file_path().endswith(os.path.join('paa_mcp.py'))
    assert os.path.isfile(server_file_path())
