"""Turn owner: one session, one worker, spool, commands. No Telegram HTTP.

The harness call is the only stub. State lives in a temp dir, never
~/.ProjectMan. HOME is left alone.
"""
import json
import logging
import os
import subprocess
import sys
import threading

import pytest

from harnesses import HeadlessResult
from paa_turns import TurnService, load_bot_config
from settings import Settings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = 42
CANARY = 'CANARY9f3a-not-a-token'


def _settings(**kw):
    base = dict(
        harness_default='grok',
        paa_budget_used=42,
        paa_budget_tokens=100,
        paa_enabled=False,
        paa_loop_interval_minutes=30,
    )
    base.update(kw)
    settings = Settings(**base)
    settings.api_key_canary = CANARY
    return settings


def _write_config(state, ids=(UID,), timeout=600, extra=None):
    os.makedirs(state, exist_ok=True)
    payload = {
        'allowed_user_ids': list(ids),
        'chat_timeout_sec': timeout,
    }
    if extra:
        payload.update(extra)
    path = os.path.join(state, 'paa-telegram.json')
    with open(path, 'w') as fh:
        json.dump(payload, fh)
    return path


def _msg(update_id, text, *, user=UID, chat=UID, chat_type='private', date=1700000000):
    body = {
        'message_id': update_id,
        'date': date,
        'chat': {'id': chat, 'type': chat_type},
        'from': {'id': user},
    }
    if text is not None:
        body['text'] = text
    return {'update_id': update_id, 'message': body}


def _callback(update_id, data, *, user=UID, cq_id='cq', message_id=1):
    return {
        'update_id': update_id,
        'callback_query': {
            'id': cq_id,
            'data': data,
            'from': {'id': user},
            'message': {
                'message_id': message_id,
                'date': 1700000001,
                'chat': {'id': UID, 'type': 'private'},
            },
        },
    }


class _Stub:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.proc = None
        self.mode = 'fast'  # fast | block | killwait

    def __call__(self, settings, prompt, *, session_id=None, project_path='',
                 tool_policy='legacy', timeout=None, handle=None, **kwargs):
        self.calls.append({
            'prompt': prompt,
            'session_id': session_id,
            'tool_policy': tool_policy,
            'timeout': timeout,
            'project_path': project_path,
            'reasoning_effort': kwargs.get('reasoning_effort'),
        })
        if self.mode == 'block':
            proc = subprocess.Popen(['sleep', '30'])
            self.proc = proc
            if handle is not None:
                handle.attach(proc)
            self.started.set()
            self.release.wait(10)
            alive = proc.poll() is None
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if not alive:
                return HeadlessResult(
                    text=None, error='cancelled', session_id='inflight-sid',
                    tokens=1)
            return HeadlessResult(
                text='done', session_id='inflight-sid', tokens=4)
        if self.mode == 'killwait':
            proc = subprocess.Popen(['sleep', '30'])
            self.proc = proc
            if handle is not None:
                handle.attach(proc)
            self.started.set()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            return HeadlessResult(
                text=None, error='cancelled', session_id='should-not-matter',
                tokens=1)
        if prompt == 'boom':
            return HeadlessResult(text=None, error='boom', tokens=2)
        if prompt == 'fallback please':
            return HeadlessResult(
                text='hello from paa',
                session_id='sess-9',
                tokens=11,
                fallback_reason='fell back to grok',
                error=None,
            )
        return HeadlessResult(
            text='stub-reply', session_id='sess-9', tokens=11)


def _force_unlock(svc):
    """Open an ro window directly: the unlock gate now guards all inbound."""
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}


def _service(tmp_path, stub=None, **settings_kw):
    state = str(tmp_path / 'state')
    _write_config(state)
    stub = stub or _Stub()
    settings = _settings(**settings_kw)
    svc = TurnService(state, settings, stub)
    _force_unlock(svc)
    return svc, stub, settings


def test_config_ignores_token_and_defaults_timeout(tmp_path):
    path = _write_config(
        str(tmp_path / 'state'),
        extra={'token': 'LEAKED-TOKEN', 'pass_entry': 'internal/telegram/paa-bot-token'},
    )
    cfg = load_bot_config(path)
    assert cfg['chat_timeout_sec'] == 600
    assert cfg['allowed_user_ids'] == [UID]
    assert 'LEAKED-TOKEN' not in json.dumps(cfg)
    missing = load_bot_config(str(tmp_path / 'nope.json'))
    assert missing['chat_timeout_sec'] == 600
    assert missing['pass_entry'] == 'internal/telegram/paa-bot-token'


@pytest.mark.parametrize('raw,expected', [
    ('low', 'low'),
    ('HIGH', 'high'),          # case-insensitive
    (' none ', 'none'),
    ('minimal', 'minimal'),
    ('medium', 'medium'),
    ('banana', 'low'),         # invalid → fallback
    ('', 'low'),               # empty → fallback
    (123, 'low'),              # non-string → fallback
])
def test_reviewer_effort_config_values(tmp_path, raw, expected, caplog):
    path = _write_config(str(tmp_path / 'state'),
                         extra={'reviewer_effort': raw})
    with caplog.at_level(logging.WARNING, logger='paa_turns'):
        cfg = load_bot_config(path)
    assert cfg['reviewer_effort'] == expected
    if expected == 'low' and raw not in ('low',):
        assert any('reviewer_effort' in r.getMessage()
                   for r in caplog.records)


def test_reviewer_effort_defaults_low(tmp_path):
    path = _write_config(str(tmp_path / 'state'))
    assert load_bot_config(path)['reviewer_effort'] == 'low'


@pytest.mark.parametrize('raw,expected', [
    ('medium', 'medium'),
    ('HIGH', 'high'),          # case-insensitive
    (' none ', 'none'),
    ('minimal', 'minimal'),
    ('low', 'low'),
    ('banana', 'medium'),      # invalid → fallback
    ('', 'medium'),            # empty → fallback
    (123, 'medium'),           # non-string → fallback
])
def test_chat_effort_config_values(tmp_path, raw, expected, caplog):
    path = _write_config(str(tmp_path / 'state'),
                         extra={'chat_effort': raw})
    with caplog.at_level(logging.WARNING, logger='paa_turns'):
        cfg = load_bot_config(path)
    assert cfg['chat_effort'] == expected
    if expected == 'medium' and raw not in ('medium',):
        assert any('chat_effort' in r.getMessage()
                   for r in caplog.records)


def test_chat_effort_defaults_medium(tmp_path):
    path = _write_config(str(tmp_path / 'state'))
    assert load_bot_config(path)['chat_effort'] == 'medium'


def test_main_turn_carries_chat_effort(tmp_path):
    """chat_effort threads to the user's turn as reasoning_effort (the
    reviewer knob is independent — broker threading is pinned in
    test_paa_approve)."""
    svc, stub, _settings_obj = _service(tmp_path)
    state = str(tmp_path / 'state')
    _write_config(state, extra={'chat_effort': 'high'})
    svc.config = load_bot_config(os.path.join(state, 'paa-telegram.json'))
    svc.handle_update(_msg(1, 'hello'))
    assert stub.calls[-1]['reasoning_effort'] == 'high'


def test_main_turn_argv_golden_carries_reasoning_effort_medium(tmp_path):
    """End-to-end argv: TurnService → chat_turn → grok headless_plan.
    TurnService always passes a kill handle, so the cancellable runner
    (subprocess.Popen) is the spawn point."""
    from unittest.mock import patch

    from paa_headless import chat_turn as real_chat_turn

    payload = json.dumps({
        'text': 'ok', 'sessionId': 'sid-1',
        'usage': {'input_tokens': 1, 'output_tokens': 2},
    })

    class _FakeProc:
        def __init__(self, argv):
            self.argv = argv
            self.returncode = 0

        def communicate(self, timeout=None):
            return payload, ''

        def poll(self):
            return self.returncode

        def kill(self):
            pass

    svc, _stub, _settings_obj = _service(tmp_path)
    svc.chat_turn = real_chat_turn
    seen = {}

    def _popen(argv, **kwargs):
        seen['argv'] = list(argv)
        return _FakeProc(argv)

    with patch('subprocess.Popen', side_effect=_popen):
        outs = svc.handle_update(_msg(1, 'hello'))
    assert 'ok' in outs[0].text
    argv = seen['argv']
    assert argv[argv.index('--reasoning-effort') + 1] == 'medium'
    # ro default turn: plan policy, deny-guarded.
    assert argv[argv.index('--permission-mode') + 1] == 'plan'
    denials = [argv[i + 1] for i, a in enumerate(argv) if a == '--deny']
    assert denials == ['Edit', 'Write', 'Bash', 'MCPTool']


def test_armed_policy_request_fails_closed_to_plan(tmp_path, caplog):
    """No bot path may select tool_policy='armed' anymore; a stale request
    runs read-only 'plan' instead and logs."""
    svc, stub, _settings_obj = _service(tmp_path)
    with caplog.at_level(logging.WARNING, logger='paa_turns'):
        outs = svc._prompt_messages(UID, UID, 'edit the readme', policy='armed')
    assert 'stub-reply' in outs[0].text
    assert stub.calls[-1]['tool_policy'] == 'plan'
    assert any('armed turn requested' in r.getMessage()
               for r in caplog.records)


def test_one_turn_at_a_time(tmp_path):
    stub = _Stub()
    gate = threading.Event()
    entered = threading.Event()
    order = []

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        order.append(('enter', prompt))
        entered.set()
        assert gate.wait(5)
        order.append(('leave', prompt))
        return HeadlessResult(text='ok ' + prompt, session_id='s1', tokens=3)

    state = str(tmp_path / 'state')
    _write_config(state)
    svc = TurnService(state, _settings(), chat)
    _force_unlock(svc)
    first = {}

    def run_first():
        first['outs'] = svc.handle_update(_msg(1, 'one'))

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(3)
    started_second = threading.Event()

    def mark_and_chat(*args, **kwargs):
        started_second.set()
        return chat(*args, **kwargs)

    # The second message must wait until the first leaves chat_turn.
    svc.chat_turn = mark_and_chat
    second = {}

    def run_second():
        second['outs'] = svc.handle_update(_msg(2, 'two'))

    thread2 = threading.Thread(target=run_second)
    thread2.start()
    thread2.join(0.3)
    assert not started_second.is_set()
    gate.set()
    thread.join(5)
    thread2.join(5)
    assert order == [('enter', 'one'), ('leave', 'one'), ('enter', 'two'), ('leave', 'two')]
    assert first['outs'][0].text.startswith('ok one')
    assert second['outs'][0].text.startswith('ok two')


def test_spool_before_offset_and_replay_in_a_second_process(tmp_path):
    state = str(tmp_path / 'state')
    _write_config(state)
    stub = _Stub()
    svc = TurnService(state, _settings(), stub)
    _force_unlock(svc)
    update = _msg(7, 'hello from phone')
    seen = {}

    def before_ack():
        spool = svc._spool_path(7)
        seen['spool_exists'] = os.path.isfile(spool)
        seen['offset_memory'] = svc.offset
        seen['offset_disk'] = os.path.isfile(svc.offset_path)

    # Crash before dispatch: the spool is what a restarted process replays.
    svc.persist_update(update, before_ack=before_ack)
    assert seen['spool_exists'] is True
    assert seen['offset_memory'] == 0
    assert seen['offset_disk'] is False
    assert svc.offset == 8
    assert open(svc.offset_path).read().strip() == '8'
    assert os.path.isfile(svc._spool_path(7))

    script = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from harnesses import HeadlessResult
from paa_turns import TurnService

class S:
    paa_budget_used = 42
    paa_enabled = False
    paa_loop_interval_minutes = 30
    harness_default = "grok"
    def effective_harness(self, project_path="", host_id="localhost"):
        return "grok"

calls = {"n": 0}
def chat(settings, prompt, *, session_id=None, project_path="",
         tool_policy="legacy", timeout=None, handle=None, **kwargs):
    calls["n"] += 1
    return HeadlessResult(
        text="STUB-REPLY", session_id="sess-replay", tokens=11,
        fallback_reason="fell back to grok")

svc = TurnService(sys.argv[2], S(), chat)
svc._unlock_window = {"mode": "ro", "expires_at": svc._now() + 3600}
outs = svc.replay_spool()
again = svc.replay_spool()
# Delivery is a separate step. Leaving the spool here is the crash window.
print(json.dumps({
    "texts": [o.text for o in outs],
    "again": [o.text for o in again],
    "calls": calls["n"],
    "session_id": svc.session_id,
    "budget": svc.settings.paa_budget_used,
    "spool": __import__("os").path.isfile(svc._spool_path(7)),
}))
'''
    env = os.environ.copy()
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run(
        [sys.executable, '-c', script, REPO, state],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['calls'] == 1
    assert payload['again'] == payload['texts']
    assert len(payload['texts']) == 1
    assert payload['texts'][0].splitlines()[0] == 'fell back to grok'
    assert 'STUB-REPLY' in payload['texts'][0]
    assert 'Tokens: 11' in payload['texts'][0]
    assert payload['session_id'] == 'sess-replay'
    assert payload['budget'] == 42
    assert payload['spool'] is True
    assert os.path.isfile(svc._spool_path(7))
    # A fresh owner resends the stored reply and does not call the harness.
    fresh = _Stub()
    svc2 = TurnService(state, _settings(), fresh)
    _force_unlock(svc2)
    replayed = svc2.replay_spool()
    assert fresh.calls == []
    assert [o.text for o in replayed] == payload['texts']
    update = {'update_id': 7}
    svc2.commit_delivery(update)
    assert os.path.isfile(svc2._spool_path(7))
    while svc2.unsent_actions(update):
        svc2.mark_sent(update)
    svc2.commit_delivery(update)
    assert not os.path.isfile(svc2._spool_path(7))
    assert svc2.replay_spool() == []


def test_handle_update_keeps_spool_until_commit(tmp_path):
    svc, stub, settings = _service(tmp_path)
    seen = {}

    def before_ack():
        seen['spool'] = os.path.isfile(svc._spool_path(4))
        seen['offset'] = svc.offset

    update = _msg(4, 'ping')
    outs = svc.handle_update(update, before_ack=before_ack)
    assert seen['spool'] is True
    assert seen['offset'] == 0
    assert outs[0].text.splitlines()[0] == 'stub-reply'
    assert 'Tokens: 11' in outs[0].text
    assert os.path.isfile(svc._spool_path(4))
    svc.commit_delivery(update)
    assert os.path.isfile(svc._spool_path(4))
    while svc.unsent_actions(update):
        svc.mark_sent(update)
    svc.commit_delivery(update)
    assert not os.path.isfile(svc._spool_path(4))
    assert open(svc.offset_path).read().strip() == '5'
    assert stub.calls[0]['timeout'] == 600
    assert stub.calls[0]['session_id'] is None
    assert stub.calls[0]['tool_policy'] == 'plan'
    assert settings.paa_budget_used == 42
    assert svc.session_id == 'sess-9'


def test_new_does_not_kill_the_child_and_next_turn_has_no_session(tmp_path):
    stub = _Stub()
    stub.mode = 'block'
    svc, _, settings = _service(tmp_path, stub)
    svc.session_id = 'old-sid'
    svc._save_session()
    box = {}

    def run():
        box['outs'] = svc.handle_update(_msg(1, 'in flight'))

    thread = threading.Thread(target=run)
    thread.start()
    assert stub.started.wait(3)
    new_outs = svc.handle_update(_msg(2, '/new'))
    assert stub.proc is not None and stub.proc.poll() is None
    assert 'next turn' in new_outs[0].text.lower()
    assert svc.session_id is None
    stub.release.set()
    thread.join(5)
    assert svc.session_id is None
    saved = json.loads(open(svc.session_path).read())
    assert saved['session_id'] is None
    assert 'old-sid' in saved['archive']
    assert 'inflight-sid' not in saved['archive']
    stub.mode = 'fast'
    follow = svc.handle_update(_msg(3, 'after'))
    assert stub.calls[-1]['session_id'] is None
    assert 'stub-reply' in follow[0].text
    assert settings.paa_budget_used == 42


def test_stop_kills_the_child(tmp_path):
    stub = _Stub()
    stub.mode = 'killwait'
    svc, _, _settings_obj = _service(tmp_path, stub)
    box = {}

    def run():
        box['outs'] = svc.handle_update(_msg(1, 'go'))

    thread = threading.Thread(target=run)
    thread.start()
    assert stub.started.wait(3)
    stop_outs = svc.handle_update(_msg(2, '/stop'))
    thread.join(5)
    assert stop_outs[0].text == 'Stopped.'
    assert stub.proc.poll() is not None
    assert 'cancelled' in box['outs'][0].text
    idle = svc.handle_update(_msg(3, '/stop'))
    assert idle[0].text == 'No turn is running.'


def test_status_facts_and_no_secret(tmp_path):
    stub = _Stub()
    stub.mode = 'block'
    svc, _, settings = _service(tmp_path, stub)
    svc.session_id = 'sess-visible'
    svc._save_session()
    box = {}

    def run():
        box['outs'] = svc.handle_update(_msg(1, 'running'))

    thread = threading.Thread(target=run)
    thread.start()
    assert stub.started.wait(3)
    status = svc.handle_update(_msg(2, '/status'))
    text = status[0].text
    assert 'Harness: grok' in text
    assert 'Session: yes' in text
    assert 'Turn running: yes' in text
    assert 'Next turn armed' not in text
    assert 'paa-bot-token' not in text
    assert CANARY not in text
    assert settings.api_key_canary not in text
    stub.release.set()
    thread.join(5)


def test_reply_shows_fallback_then_tokens(tmp_path):
    svc, stub, settings = _service(tmp_path)
    outs = svc.handle_update(_msg(1, 'fallback please'))
    lines = outs[0].text.splitlines()
    assert lines[0] == 'fell back to grok'
    assert lines[1] == 'hello from paa'
    assert lines[-1] == 'Tokens: 11'
    assert settings.paa_budget_used == 42
    assert stub.calls[0]['timeout'] == 600


def test_empty_allowlist_logs_and_replies_to_nobody(tmp_path):
    state = str(tmp_path / 'state')
    _write_config(state, ids=())
    stub = _Stub()
    svc = TurnService(state, _settings(), stub)
    outs = svc.handle_update(_msg(1, 'hello', user=99, chat=99, date=1711111111))
    assert outs == []
    assert stub.calls == []
    log = open(svc.operator_log).read()
    assert 'user=99' in log
    assert 'chat=99' in log
    assert 'date=1711111111' in log
    assert svc.offset == 2
    cb = svc.handle_update(_callback(2, 'cAAAAAAAA', user=99))
    assert cb == []
    assert stub.calls == []
    log = open(svc.operator_log).read()
    assert log.count('user=99') >= 2


def test_non_private_and_non_text_and_unknown_slash(tmp_path):
    svc, stub, _settings_obj = _service(tmp_path)
    group = svc.handle_update(_msg(1, 'hi', chat_type='group'))
    assert group == []
    assert stub.calls == []
    stranger = svc.handle_update(_msg(2, 'hi', user=99))
    assert stranger == []
    assert stub.calls == []
    photo = _msg(3, None)
    photo['message']['photo'] = [{'file_id': 'x'}]
    only = svc.handle_update(photo)
    assert len(only) == 1
    assert only[0].text == 'Text only.'
    blank = svc.handle_update(_msg(4, '   '))
    assert blank[0].text == 'Text only.'
    assert stub.calls == []
    other = svc.handle_update(_msg(5, '/models'))
    assert stub.calls[-1]['prompt'] == '/models'
    assert 'stub-reply' in other[0].text


def test_callback_is_not_a_command(tmp_path):
    svc, stub, _settings_obj = _service(tmp_path)
    svc.session_id = 'keep-me'
    svc._save_session()
    outs = svc.handle_update(_callback(1, '/new', cq_id='cq-new'))
    assert outs[0].kind == 'answer_callback'
    assert outs[0].answer_text == 'Unknown choice.'
    assert len(outs) == 1
    assert svc.session_id == 'keep-me'
    assert stub.calls == []


def test_question_dict_gets_no_keyboard(tmp_path):
    """A structured question on the result has no consumer anymore (the
    armed retry path is gone): the reply is plain text, no keyboard."""
    calls = []

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        calls.append({'prompt': prompt, 'tool_policy': tool_policy})
        return HeadlessResult(
            text='cannot run that',
            question={'tool': 'shell'},
            tokens=2,
            session_id='sess-q',
        )

    state = str(tmp_path / 'state')
    _write_config(state)
    svc = TurnService(state, _settings(), chat)
    _force_unlock(svc)

    asked = svc.handle_update(_msg(1, 'needs a tool'))
    assert asked[0].reply_markup is None
    assert asked[0].text.splitlines()[0] == 'cannot run that'
    assert len(calls) == 1
    assert calls[0]['tool_policy'] == 'plan'


def test_second_tap_already_chosen(tmp_path):
    svc, _stub, _settings_obj = _service(tmp_path)
    group, _prompt, markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick',
        options=[{'label': 'A', 'value': 'a'}],
        meaning={'kind': 'drill-test'},
    )
    cid = markup['inline_keyboard'][0][0]['callback_data']
    svc.handle_update(_callback(1, cid, cq_id='once'))
    again = svc.handle_update(_callback(2, cid, cq_id='twice'))
    assert len(again) == 1
    assert again[0].kind == 'answer_callback'
    assert again[0].answer_text == 'Already chosen.'
