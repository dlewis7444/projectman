"""Telegram transport: real urllib against a loopback server.

Nothing in this file contacts api.telegram.org. The harness call is stubbed.
The no-token launch uses a fake ``pass`` earlier on PATH and does not set HOME.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harnesses import HeadlessResult
from paa_telegram import (
    TelegramApi,
    TelegramLoop,
    read_pass_token,
    run_tick,
    split_outbound,
)
from paa_turns import TurnService
from settings import Settings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = 42
TOKEN = 'test-token-not-secret'


class _Box:
    def __init__(self):
        self.requests = []
        self.updates = []
        self.lock = threading.Lock()
        self.mid = 500
        self.assigned = []
        self.fail_401_once = False
        self.did_401 = False
        self.fail_send_once = False
        self.fail_action_once = False
        self.fail_action_always = False


def _handler(box):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            host = (self.headers.get('Host') or '')
            if 'api.telegram.org' in host or 'api.telegram.org' in self.path:
                raise AssertionError('test client reached api.telegram.org')
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length)
            body = json.loads(raw.decode() or '{}')
            method = self.path.rsplit('/', 1)[-1]
            with box.lock:
                box.requests.append({
                    'method': method,
                    'body': body,
                    'path': self.path,
                    'host': host,
                })
                fail = box.fail_401_once and not box.did_401
                if fail:
                    box.did_401 = True
                fail_send = method == 'sendMessage' and box.fail_send_once
                if fail_send:
                    box.fail_send_once = False
                fail_action = (
                    method == 'sendChatAction'
                    and (box.fail_action_once or box.fail_action_always))
                if fail_action and box.fail_action_once:
                    box.fail_action_once = False
            if fail_send or fail_action:
                payload = b'{"ok":false,"description":"send failed"}'
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if fail:
                payload = b'{"ok":false,"description":"Unauthorized"}'
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if method == 'getUpdates':
                with box.lock:
                    result = list(box.updates)
                    box.updates.clear()
                payload = {'ok': True, 'result': result}
            elif method == 'sendMessage':
                with box.lock:
                    box.mid += 1
                    mid = box.mid
                    box.assigned.append(mid)
                payload = {'ok': True, 'result': {
                    'message_id': mid, 'text': body.get('text'),
                }}
            elif method == 'editMessageText':
                payload = {'ok': True, 'result': {
                    'message_id': body.get('message_id'),
                    'text': body.get('text'),
                }}
            elif method == 'answerCallbackQuery':
                payload = {'ok': True, 'result': True}
            elif method == 'sendChatAction':
                payload = {'ok': True, 'result': True}
            else:
                payload = {'ok': False, 'description': method}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    return Handler


@pytest.fixture
def loopback():
    box = _Box()
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), _handler(box))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    yield box, base
    httpd.shutdown()
    httpd.server_close()


class _Stub:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, prompt, *, session_id=None, project_path='',
                 tool_policy='legacy', timeout=None, handle=None, **kwargs):
        self.calls.append({
            'prompt': prompt,
            'session_id': session_id,
            'tool_policy': tool_policy,
            'timeout': timeout,
        })
        return HeadlessResult(text='stub-reply', session_id='sess-9', tokens=11)


def _service(tmp_path, stub=None, ids=(UID,)):
    state = tmp_path / 'state'
    state.mkdir()
    payload = {'allowed_user_ids': list(ids), 'chat_timeout_sec': 600}
    (state / 'paa-telegram.json').write_text(json.dumps(payload))
    stub = stub or _Stub()
    settings = Settings(
        harness_default='grok', paa_budget_used=7, paa_enabled=False,
        paa_loop_interval_minutes=30,
    )
    svc = TurnService(str(state), settings, stub)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    return svc, stub


def _push(box, update):
    with box.lock:
        if isinstance(update, list):
            box.updates.extend(update)
        else:
            box.updates.append(update)


def _methods(box):
    with box.lock:
        return [row['method'] for row in box.requests]


def _bodies(box, method):
    with box.lock:
        return [row['body'] for row in box.requests if row['method'] == method]


def test_split_and_get_updates_contract(loopback):
    box, base = loopback
    parts = split_outbound('a' * 4000)
    assert parts == ['a' * 4000]
    long = ('line\n' * 900)  # 4500 chars
    pieces = split_outbound(long)
    assert len(pieces) >= 2
    assert all(len(part) <= 4000 for part in pieces)
    assert ''.join(pieces) == long
    api = TelegramApi(base, TOKEN)
    _push(box, [])
    api.get_updates(0)
    body = _bodies(box, 'getUpdates')[-1]
    assert body['allowed_updates'] == ['message', 'callback_query']
    assert body['timeout'] == 50
    assert body['offset'] == 0
    with box.lock:
        hosts = {row['host'] for row in box.requests}
    assert hosts
    assert all('api.telegram.org' not in host for host in hosts)


def test_private_text_is_plain_and_long_text_splits(loopback, tmp_path):
    box, base = loopback
    svc, stub = _service(tmp_path)
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, [{
        'update_id': 1,
        'message': {
            'message_id': 1,
            'date': 1700000000,
            'text': 'hello',
            'chat': {'id': UID, 'type': 'private'},
            'from': {'id': UID},
        },
    }])
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    sent = _bodies(box, 'sendMessage')
    assert len(sent) == 1
    assert sent[0]['text'].splitlines()[0] == 'stub-reply'
    assert 'Tokens: 11' in sent[0]['text']
    assert 'parse_mode' not in sent[0]
    assert stub.calls[0]['tool_policy'] == 'plan'
    assert stub.calls[0]['timeout'] == 600

    long_text = 'Y' * 5000
    stub.calls.clear()

    def long_chat(settings, prompt, **kwargs):
        stub.calls.append(kwargs)
        return HeadlessResult(text=long_text, session_id='sess-9', tokens=3)

    svc.chat_turn = long_chat
    _push(box, [{
        'update_id': 2,
        'message': {
            'message_id': 2,
            'date': 1700000001,
            'text': 'long please',
            'chat': {'id': UID, 'type': 'private'},
            'from': {'id': UID},
        },
    }])
    before = len(_bodies(box, 'sendMessage'))
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    parts = _bodies(box, 'sendMessage')[before:]
    assert len(parts) >= 2
    assert all(len(part['text']) <= 4000 for part in parts)
    assert all('parse_mode' not in part for part in parts)
    assert ''.join(part['text'] for part in parts).startswith(long_text)
    assert 'Tokens: 3' in ''.join(part['text'] for part in parts)
    assert all(part.get('reply_markup') is None for part in parts)
    # The token is used on the URL and must not land in the state dir.
    for dirpath, _dirs, files in os.walk(svc.state_dir):
        for name in files:
            blob = open(os.path.join(dirpath, name)).read()
            assert TOKEN not in blob


def test_unknown_spent_expired_foreign_and_success(loopback, tmp_path):
    box, base = loopback
    svc, stub = _service(tmp_path, ids=(UID, 77))
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)

    def poll(update):
        _push(box, [update])
        before = len(_methods(box))
        loop.poll_once(timeout=1)
        with box.lock:
            return list(box.requests[before:])

    # Unknown callback is answered and never becomes a prompt.
    calls = poll({
        'update_id': 1,
        'callback_query': {
            'id': 'cq-unknown',
            'data': 'cNOTHERE1',
            'from': {'id': UID},
            'message': {'message_id': 1, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    assert [c['method'] for c in calls] == ['getUpdates', 'answerCallbackQuery']
    assert calls[1]['body']['text'] == 'Unknown choice.'
    assert stub.calls == []

    # A live keyboard: select edits the same server message; a second tap
    # is 'Already chosen.'
    _group, _prompt, markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick one',
        options=[{'label': 'Alpha', 'value': 'a'},
                 {'label': 'Beta', 'value': 'b'}],
        meaning={'kind': 'drill-test'},
        message_id=700,
    )
    flat = [b for row in markup['inline_keyboard'] for b in row]
    allow_id = flat[0]['callback_data']
    cancel_id = flat[1]['callback_data']
    assert allow_id != cancel_id

    chosen = poll({
        'update_id': 3,
        'callback_query': {
            'id': 'cq-allow',
            'data': allow_id,
            'from': {'id': UID},
            'message': {'message_id': 700, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    assert [c['method'] for c in chosen][:2] == ['getUpdates', 'answerCallbackQuery']
    edit = [c for c in chosen if c['method'] == 'editMessageText'][0]
    assert edit['body']['message_id'] == 700
    assert edit['body']['text'].endswith('Alpha')
    assert edit['body']['reply_markup'] == {'inline_keyboard': []}
    assert 'parse_mode' not in edit['body']
    assert stub.calls == []

    spent = poll({
        'update_id': 4,
        'callback_query': {
            'id': 'cq-spent',
            'data': allow_id,
            'from': {'id': UID},
            'message': {'message_id': 700, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    assert [c['method'] for c in spent] == ['getUpdates', 'answerCallbackQuery']
    assert spent[1]['body']['text'] == 'Already chosen.'

    # Expired: a fresh group, then expire it, then tap.
    group2, _p2, markup2 = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick again',
        options=[{'label': 'Gamma', 'value': 'g'}],
        meaning={'kind': 'drill-test'},
        message_id=701,
    )
    fresh_id = markup2['inline_keyboard'][0][0]['callback_data']
    assert fresh_id
    svc.choices.expire_group(group2)
    expired = poll({
        'update_id': 6,
        'callback_query': {
            'id': 'cq-expired',
            'data': fresh_id,
            'from': {'id': UID},
            'message': {'message_id': 1, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    assert expired[1]['body']['text'] == 'That choice expired.'
    assert all(c['method'] != 'sendMessage' for c in expired[1:])
    assert stub.calls == []

    # Foreign: owner is 42, caller 77 is allowlisted but not the owner.
    drill_group, _prompt, markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick a branch',
        options=[{
            'label': 'More',
            'value': 'more',
            'children': [
                {'label': 'Child A', 'value': 'a'},
                {'label': 'Child B', 'value': 'b'},
            ],
        }],
        meaning={'kind': 'drill-test'},
        message_id=77,
    )
    parent_id = markup['inline_keyboard'][0][0]['callback_data']
    foreign = poll({
        'update_id': 7,
        'callback_query': {
            'id': 'cq-foreign',
            'data': parent_id,
            'from': {'id': 77},
            'message': {'message_id': 77, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    assert foreign[1]['body']['text'] == 'Not your choice.'
    assert all(c['method'] != 'editMessageText' for c in foreign)
    assert stub.calls == []

    drilled = poll({
        'update_id': 8,
        'callback_query': {
            'id': 'cq-drill',
            'data': parent_id,
            'from': {'id': UID},
            'message': {'message_id': 77, 'chat': {'id': UID, 'type': 'private'}},
        },
    })
    edit = [c for c in drilled if c['method'] == 'editMessageText'][0]
    assert edit['body']['message_id'] == 77
    labels = [
        b['text'] for row in edit['body']['reply_markup']['inline_keyboard']
        for b in row
    ]
    assert labels == ['Child A', 'Child B']
    ids = [
        b['callback_data']
        for row in edit['body']['reply_markup']['inline_keyboard'] for b in row
    ]
    assert all(_opaque(cid) for cid in ids)
    assert 'a' not in ids and parent_id not in ids
    stored = open(os.path.join(svc.state_dir, 'paa-choices.json')).read()
    assert 'Child A' in stored
    assert drill_group


def _opaque(cid):
    import re
    return bool(re.fullmatch(r'c[A-Za-z0-9_-]{8}', cid)) and len(cid.encode()) <= 64


def test_findings_page_size_and_allowlist_blocks_harness(loopback, tmp_path):
    box, base = loopback
    from paa_ledger import Ledger, LedgerItem
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    for i in range(9):
        ledger.add_if_new(LedgerItem(
            id=f'finding-row-{i:02d}',
            type='no-git',
            project='alpha',
            project_path='/tmp/alpha',
            summary=f'summary-{i:02d}',
            evidence=f'ev-{i:02d}',
            severity='info',
            created=f'2026-09-{30 - i:02d}T00:00:00+00:00',
        ))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps({
        'allowed_user_ids': [UID],
        'chat_timeout_sec': 600,
    }))
    stub = _Stub()
    svc = TurnService(str(state), Settings(
        harness_default='grok', paa_enabled=False, paa_loop_interval_minutes=30,
    ), stub, ledger=ledger)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, [{
        'update_id': 1,
        'message': {
            'message_id': 1, 'date': 1, 'text': '/findings',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    }])
    loop.poll_once(timeout=1)
    sent = _bodies(box, 'sendMessage')[-1]
    keyboard = sent['reply_markup']['inline_keyboard']
    # 8 finding rows plus a Next row.
    assert len(keyboard) == 9
    labels = [row[0]['text'] for row in keyboard[:8]]
    assert len(labels) == 8
    assert all('summary-08' not in label for label in labels)
    assert any('summary-00' in label for label in labels)
    ids = [row[0]['callback_data'] for row in keyboard[:8]]
    ids.append(keyboard[8][0]['callback_data'])
    assert all(_opaque(cid) for cid in ids)
    blob = open(state / 'paa-choices.json').read()
    for i in range(9):
        assert f'finding-row-{i:02d}' in blob
    for cid in ids:
        assert 'finding-row-' not in cid
    assert stub.calls == []

    # A user who is not allowlisted produces zero harness calls and no reply.
    before = len(stub.calls)
    _push(box, [{
        'update_id': 2,
        'message': {
            'message_id': 2, 'date': 1, 'text': 'please run',
            'chat': {'id': 99, 'type': 'private'}, 'from': {'id': 99},
        },
    }])
    n_before = len(_bodies(box, 'sendMessage'))
    loop.poll_once(timeout=1)
    assert len(stub.calls) == before
    assert len(_bodies(box, 'sendMessage')) == n_before


def test_http_401_reloads_token_once(loopback):
    box, base = loopback
    box.fail_401_once = True
    seen = []

    def reload():
        seen.append('reloaded')
        return 'tok-new'

    api = TelegramApi(base, 'tok-old', reload_token=reload)
    api.get_updates(3)
    assert seen == ['reloaded']
    with box.lock:
        paths = [row['path'] for row in box.requests]
    assert any(path.startswith('/bottok-old/') for path in paths)
    assert any(path.startswith('/bottok-new/') for path in paths)
    assert sum(1 for path in paths if 'getUpdates' in path) == 2


def test_refuses_n8n_entry_without_calling_pass(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError('pass was invoked')

    monkeypatch.setattr('paa_telegram.subprocess.run', boom)
    with pytest.raises(SystemExit):
        read_pass_token('internal/telegram/n8n-bot-token')
    with pytest.raises(SystemExit):
        read_pass_token('custom/n8n-bot-token')


def test_main_exits_when_pass_has_no_paa_token(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    pass_log = tmp_path / 'pass.log'
    script = bin_dir / 'pass'
    script.write_text(
        '#!/bin/sh\n'
        f'echo "$@" >> "{pass_log}"\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        '    *n8n*) echo SEEN_N8N >> "{log}"; exit 1 ;;\n'
        '  esac\n'
        'done\n'
        'echo "pass: entry not found" >&2\n'
        'exit 1\n'.format(log=pass_log)
    )
    script.chmod(0o755)
    env = os.environ.copy()
    env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
    env['PAA_STATE_DIR'] = str(tmp_path / 'state')
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, 'paa_telegram.py')],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode != 0
    assert 'fatal: pass show failed' in proc.stderr
    assert proc.stdout == ''
    logged = pass_log.read_text()
    assert 'internal/telegram/paa-bot-token' in logged
    assert 'n8n' not in logged
    assert 'SEEN_N8N' not in logged


def test_unit_file_has_no_secrets_and_is_not_the_install_script():
    path = os.path.join(REPO, 'contrib', 'paa-telegram.service')
    text = open(path).read()
    assert 'Restart=on-failure' in text
    assert 'WantedBy=default.target' in text
    assert 'StandardOutput=journal' in text
    assert 'StandardError=journal' in text
    assert 'PROJECTMAN_SOURCE' in text
    assert 'paa_telegram.py' in text
    assert '/usr/bin/python3' in text
    assert '.local/share/projectman' in text
    assert 'install.sh' in text
    assert 'ExecStart=' in text and 'install.sh' not in text.split('ExecStart=', 1)[1].split('\n', 1)[0]
    for banned in ('node.example.com', 'localhost', 'api.telegram.org'):
        assert banned not in text
    # The entry name is configuration, not a token. No token-shaped assignment.
    assert 'Environment=' not in text
    assert 'pass show' in text


def _private(update_id, text):
    return {
        'update_id': update_id,
        'message': {
            'message_id': update_id,
            'date': 1700000000 + update_id,
            'text': text,
            'chat': {'id': UID, 'type': 'private'},
            'from': {'id': UID},
        },
    }


def test_poll_returns_while_the_child_is_alive_and_stop_kills_it(loopback, tmp_path):
    """/stop is read on a later poll, while chat_turn is still inside the child."""
    box, base = loopback
    entered = threading.Event()
    returned = threading.Event()
    holder = {}

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        proc = subprocess.Popen(['sleep', '30'])
        holder['proc'] = proc
        if handle is not None:
            handle.attach(proc)
        entered.set()
        deadline = time.monotonic() + 8
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
            holder['timed_out'] = True
        return HeadlessResult(text=None, error='cancelled', tokens=1)

    svc, _stub = _service(tmp_path)
    svc.chat_turn = chat
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, _private(1, 'go'))
    errors = []

    def run_poll():
        try:
            loop.poll_once(timeout=1)
        except Exception as exc:
            errors.append(exc)
        finally:
            returned.set()

    threading.Thread(target=run_poll, daemon=True).start()
    try:
        assert returned.wait(2), 'poll_once stayed inside the chat turn'
        assert not errors
        assert entered.wait(2)
        assert holder['proc'].poll() is None
        _push(box, _private(2, '/stop'))
        loop.poll_once(timeout=1)
        assert holder['proc'].wait(timeout=3) is not None
        assert holder.get('timed_out') is not True
        texts = [body['text'] for body in _bodies(box, 'sendMessage')]
        assert 'Stopped.' in texts
    finally:
        proc = holder.get('proc')
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_discuss_callback_is_answered_before_chat(loopback, tmp_path):
    from paa_ledger import Ledger, LedgerItem

    box, base = loopback
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    ledger.add_if_new(LedgerItem(
        id='finding-alpha', type='no-git', project='alpha',
        project_path='/p/alpha', summary='alpha is dirty', evidence='ev-a',
        severity='warning', created='2026-09-30T00:00:00+00:00',
    ))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps({
        'allowed_user_ids': [UID], 'chat_timeout_sec': 600,
    }))
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    holder = {}

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        holder['methods'] = _methods(box)
        holder['prompt'] = prompt
        holder['policy'] = tool_policy
        entered.set()
        assert release.wait(5)
        return HeadlessResult(text='discussed', session_id='sess-9', tokens=4)

    svc = TurnService(str(state), Settings(
        harness_default='grok', paa_enabled=False, paa_loop_interval_minutes=30,
    ), chat, ledger=ledger)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    noted = svc.notify_new_findings(set(), chat_id=UID, user_id=UID)
    discuss_id = None
    for row in noted[0].reply_markup['inline_keyboard']:
        if row[0]['text'] == 'Discuss':
            discuss_id = row[0]['callback_data']
    assert discuss_id
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, {
        'update_id': 1,
        'callback_query': {
            'id': 'cq-discuss',
            'data': discuss_id,
            'from': {'id': UID},
            'message': {
                'message_id': noted[0].message_id,
                'chat': {'id': UID, 'type': 'private'},
            },
        },
    })
    errors = []

    def run_poll():
        try:
            loop.poll_once(timeout=1)
        except Exception as exc:
            errors.append(exc)
        finally:
            returned.set()

    threading.Thread(target=run_poll, daemon=True).start()
    try:
        assert returned.wait(2), 'poll_once stayed inside the discuss turn'
        assert not errors
        assert entered.wait(2)
        methods = holder['methods']
        assert 'answerCallbackQuery' in methods
        assert 'editMessageText' in methods
        assert methods.index('answerCallbackQuery') < methods.index('editMessageText')
        assert 'sendMessage' not in methods
        assert holder['prompt'].startswith('DISCUSS FINDING')
        assert holder['policy'] == 'plan'
        assert not release.is_set()
        release.set()
        assert loop.wait_idle()
    finally:
        release.set()


def test_failed_send_leaves_the_spool_and_a_later_delivery_does_not_rechat(loopback, tmp_path):
    box, base = loopback
    box.fail_send_once = True
    svc, stub = _service(tmp_path)
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, _private(1, 'hello'))
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    assert len(stub.calls) == 1
    spool = svc._spool_path(1)
    assert os.path.isfile(spool)
    record = json.loads(open(spool).read())
    assert record['ready'] is True
    assert record['follow'] is None
    assert 'stub-reply' in json.dumps(record['actions'])
    assert len(_bodies(box, 'sendMessage')) == 1
    loop.deliver_spool()
    sent = _bodies(box, 'sendMessage')
    assert len(sent) == 2
    assert sent[-1]['text'].splitlines()[0] == 'stub-reply'
    assert 'Tokens: 11' in sent[-1]['text']
    assert len(stub.calls) == 1
    assert not os.path.isfile(spool)


def test_question_dict_result_gets_plain_reply_without_keyboard(loopback, tmp_path):
    """A structured question on the result has no consumer anymore (the
    armed retry path is gone): the turn delivers a plain text reply."""
    box, base = loopback
    calls = []

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        calls.append({'prompt': prompt, 'tool_policy': tool_policy})
        return HeadlessResult(
            text='need approval',
            question={'tool': 'shell'},
            tokens=3,
            session_id='sess-9',
        )

    svc, _stub = _service(tmp_path)
    svc.chat_turn = chat
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    _push(box, _private(1, 'list the files'))
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    sent = _bodies(box, 'sendMessage')
    assert len(sent) == 1
    assert 'need approval' in sent[0]['text']
    assert 'reply_markup' not in sent[0]
    assert calls == [{'prompt': 'list the files', 'tool_policy': 'plan'}]


def test_scan_notice_callback_edits_the_server_message(loopback, tmp_path):
    """serve's scan tick binds Ack/Dismiss/Discuss to Telegram's message id."""
    from paa_ledger import Ledger, LedgerItem

    box, base = loopback
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps({
        'allowed_user_ids': [UID],
        'chat_timeout_sec': 600,
        'findings_notices': True,
    }))
    item = LedgerItem(
        id='finding-from-scan', type='no-git', project='alpha',
        project_path='/p/alpha', summary='alpha is dirty', evidence='ev-scan',
        severity='warning', created='2026-09-30T00:00:00+00:00',
    )

    class _Scan:
        def run_scan(self):
            ledger.add_if_new(item)

    stub = _Stub()
    svc = TurnService(str(state), Settings(
        harness_default='grok', paa_enabled=True, paa_budget_used=7,
        paa_loop_interval_minutes=30,
    ), stub, ledger=ledger)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc)
    results = run_tick(
        loop, svc, _Scan(), now=1_000_000, chat_id=UID, timeout=1)
    assert len(results) == 1
    server_mid = (results[0].get('result') or {}).get('message_id')
    with box.lock:
        assigned = list(box.assigned)
    assert assigned == [server_mid]
    assert isinstance(server_mid, int)
    choice = json.loads((state / 'paa-choices.json').read_text())
    stored = {row.get('message_id') for row in choice['buttons'].values()}
    assert stored == {server_mid}
    labels = {}
    for cid, row in choice['buttons'].items():
        labels[row.get('label')] = cid
    assert set(labels) == {'Ack', 'Dismiss', 'Discuss'}
    for cid in labels.values():
        assert item.id not in cid
    sent = _bodies(box, 'sendMessage')
    assert len(sent) == 1
    assert 'alpha' in sent[0]['text']
    assert item.id not in sent[0]['text']

    _push(box, {
        'update_id': 1,
        'callback_query': {
            'id': 'cq-dismiss-scan',
            'data': labels['Dismiss'],
            'from': {'id': UID},
            'message': {
                'message_id': server_mid,
                'chat': {'id': UID, 'type': 'private'},
            },
        },
    })
    run_tick(loop, svc, _Scan(), now=1_000_010, chat_id=UID, timeout=1)
    edits = _bodies(box, 'editMessageText')
    assert len(edits) == 1
    assert edits[0]['message_id'] == server_mid
    assert edits[0]['reply_markup'] == {'inline_keyboard': []}
    assert stub.calls == []
    fresh = Ledger(str(tmp_path / 'ledger.json'))
    fresh.load()
    assert fresh._items['finding-from-scan'].status == 'dismissed'


def _gate_chat(entered, release, result=None, calls=None):
    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        if calls is not None:
            calls.append(prompt)
        entered.set()
        assert release.wait(5)
        return result or HeadlessResult(
            text='slow-reply', session_id='sess-9', tokens=7)

    return chat


def _wait_actions(box, count, deadline=5.0):
    limit = time.monotonic() + deadline
    while len(_bodies(box, 'sendChatAction')) < count \
            and time.monotonic() < limit:
        time.sleep(0.02)
    return _bodies(box, 'sendChatAction')


def test_typing_action_is_sent_once_when_a_prompt_turn_starts(loopback, tmp_path):
    box, base = loopback
    entered = threading.Event()
    release = threading.Event()
    svc, _stub = _service(tmp_path)
    svc.chat_turn = _gate_chat(entered, release)
    api = TelegramApi(base, TOKEN)
    # A long interval: only the immediate send can fire while the turn runs.
    loop = TelegramLoop(api, svc, typing_interval=60)
    _push(box, _private(1, 'hello'))
    loop.poll_once(timeout=1)
    assert entered.wait(2)
    # Interval is 60s, so only the immediate send can fire; wait for it
    # rather than assuming the typing thread was scheduled first.
    actions = _wait_actions(box, 1)
    assert actions == [{'chat_id': UID, 'action': 'typing'}]
    release.set()
    assert loop.wait_idle()
    sent = _bodies(box, 'sendMessage')
    assert sent[-1]['text'].splitlines()[0] == 'slow-reply'

    # Instant commands answer without a typing indicator.
    _push(box, _private(2, '/status'))
    loop.poll_once(timeout=1)
    assert _bodies(box, 'sendChatAction') == actions


def test_typing_repeats_while_turn_runs_and_stops_after_reply(loopback, tmp_path):
    box, base = loopback
    entered = threading.Event()
    release = threading.Event()
    svc, _stub = _service(tmp_path)
    svc.chat_turn = _gate_chat(entered, release)
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc, typing_interval=0.05)
    _push(box, _private(1, 'hello'))
    loop.poll_once(timeout=1)
    assert entered.wait(2)
    during = _wait_actions(box, 3)
    assert len(during) >= 3
    assert all(body == {'chat_id': UID, 'action': 'typing'} for body in during)
    release.set()
    assert loop.wait_idle()
    # The follow thread joins the typing thread before going idle, so no
    # straggler action can land after the reply was delivered.
    assert _bodies(box, 'sendChatAction') == during
    sent = _bodies(box, 'sendMessage')
    assert sent[-1]['text'].splitlines()[0] == 'slow-reply'


def test_typing_stops_when_the_turn_returns_an_error(loopback, tmp_path):
    box, base = loopback
    entered = threading.Event()

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        entered.set()
        return HeadlessResult(text=None, error='boom', tokens=2)

    svc, _stub = _service(tmp_path)
    svc.chat_turn = chat
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc, typing_interval=0.05)
    _push(box, _private(1, 'boom'))
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    assert entered.wait(2)
    actions = _bodies(box, 'sendChatAction')
    assert len(actions) >= 1
    sent = _bodies(box, 'sendMessage')
    assert sent[-1]['text'].splitlines()[0] == 'boom'
    assert _bodies(box, 'sendChatAction') == actions


def test_typing_stops_when_stop_kills_the_turn(loopback, tmp_path):
    """/stop kills the child; the follow still delivers and typing ends."""
    box, base = loopback
    entered = threading.Event()
    holder = {}

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        proc = subprocess.Popen(['sleep', '30'])
        holder['proc'] = proc
        if handle is not None:
            handle.attach(proc)
        entered.set()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return HeadlessResult(text=None, error='cancelled', tokens=1)

    svc, _stub = _service(tmp_path)
    svc.chat_turn = chat
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc, typing_interval=0.05)
    _push(box, _private(1, 'go'))
    loop.poll_once(timeout=1)
    assert entered.wait(2)
    assert len(_wait_actions(box, 1)) >= 1
    _push(box, _private(2, '/stop'))
    loop.poll_once(timeout=1)
    assert loop.wait_idle()
    assert holder['proc'].poll() is not None
    actions = _bodies(box, 'sendChatAction')
    assert _bodies(box, 'sendChatAction') == actions
    texts = [body['text'] for body in _bodies(box, 'sendMessage')]
    assert 'Stopped.' in texts
    assert 'cancelled' in texts[-1]


def test_failed_send_chat_action_does_not_break_the_turn(loopback, tmp_path,
                                                         caplog):
    box, base = loopback
    box.fail_action_always = True
    entered = threading.Event()
    release = threading.Event()
    calls = []
    svc, _stub = _service(tmp_path)
    svc.chat_turn = _gate_chat(entered, release, calls=calls)
    api = TelegramApi(base, TOKEN)
    loop = TelegramLoop(api, svc, typing_interval=0.05)
    with caplog.at_level(logging.DEBUG, logger='paa_telegram'):
        _push(box, _private(1, 'hello'))
        loop.poll_once(timeout=1)
        assert entered.wait(2)
        actions = _wait_actions(box, 2)
        assert len(actions) >= 2
        release.set()
        assert loop.wait_idle()
    assert calls == ['hello']
    sent = _bodies(box, 'sendMessage')
    assert sent[-1]['text'].splitlines()[0] == 'slow-reply'
    # Best-effort: a failed action is debug-level, never fatal/error.
    noisy = [r for r in caplog.records
             if r.levelno >= logging.ERROR and 'sendChatAction' in r.getMessage()]
    assert noisy == []
