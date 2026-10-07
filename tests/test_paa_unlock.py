"""Unlock window: /unlock grammar, opaque failures, the locked gate, rw mode.

State lives in a temp dir; HOME is left alone. The TOTP secret and the
clock are injected — no live ``pass``, no sleeping, no network.
"""
import json
import logging
import os
import socket
import threading
import time

import pytest

import paa_totp
import paa_turns
from harnesses import HeadlessResult
from paa_turns import TurnService, load_bot_config, parse_unlock_args
from settings import Settings

UID = 42
# RFC 6238 Appendix B SHA1 secret (ASCII "12345678901234567890").
SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class _Stub:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, prompt, *, session_id=None, project_path='',
                 tool_policy='legacy', timeout=None, handle=None,
                 approval_sock=None, approval_token=None, **kwargs):
        self.calls.append({
            'prompt': prompt,
            'tool_policy': tool_policy,
            'approval_sock': approval_sock,
            'approval_token': approval_token,
        })
        return HeadlessResult(text='stub-reply', session_id='sess-9', tokens=3)


def _broken_secret():
    raise RuntimeError('pass show failed: gpg: decryption failed')


def _settings(**kw):
    base = dict(
        harness_default='grok',
        paa_budget_used=0,
        paa_budget_tokens=100,
        paa_enabled=False,
        paa_loop_interval_minutes=30,
    )
    base.update(kw)
    return Settings(**base)


def _write_config(state, ids=(UID,), extra=None):
    os.makedirs(state, exist_ok=True)
    payload = {
        'allowed_user_ids': list(ids),
        'chat_timeout_sec': 600,
    }
    if extra:
        payload.update(extra)
    with open(os.path.join(state, 'paa-telegram.json'), 'w') as fh:
        json.dump(payload, fh)


def _msg(update_id, text, *, user=UID, chat=UID, chat_type='private'):
    body = {
        'message_id': update_id,
        'date': 1700000000,
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


_USE_PASS = object()


def _service(tmp_path, *, clock=None, secret=SECRET, provider=None,
             extra_config=None):
    state = str(tmp_path / 'state')
    _write_config(state, extra=extra_config)
    stub = _Stub()
    if secret is _USE_PASS:
        secret_fn = None  # exercise the real pass-reading path (stubbed)
    elif secret is None:
        secret_fn = _broken_secret
    else:
        secret_fn = (lambda: secret)
    svc = TurnService(
        state, _settings(), stub,
        now_fn=clock,
        approval_sock_provider=provider,
        unlock_secret_fn=secret_fn,
    )
    return svc, stub


def _code(clock):
    return paa_totp._hotp(SECRET, int(clock.t // 30))


def _unlock(clock, update_id=1, suffix=''):
    return f'/unlock {_code(clock)}{suffix}'


# -- grammar -------------------------------------------------------------


@pytest.mark.parametrize('arg,expected', [
    ('', None),
    ('   ', None),
    ('123456', ('123456', 'ro', 3600)),
    ('  123456   rw   999  ', ('123456', 'rw', 999)),
    ('123456 rw', ('123456', 'rw', 600)),
    ('123456 ro', ('123456', 'ro', 3600)),
    ('123456 rw 999', ('123456', 'rw', 999)),
    ('123456 ro 60', ('123456', 'ro', 60)),
    ('123456 rw 007', ('123456', 'rw', 7)),
    # range boundaries are accepted (ro max 999999, rw max 86400)
    ('123456 ro 1', ('123456', 'ro', 1)),
    ('123456 rw 86400', ('123456', 'rw', 86400)),
    ('123456 ro 86401', ('123456', 'ro', 86401)),
    ('123456 ro 999999', ('123456', 'ro', 999999)),
    # seconds without a mode are not accepted
    ('123456 999', None),
    # out-of-range seconds: below 1 or above the mode's max
    ('123456 rw 0', None),
    ('123456 ro 0', None),
    ('123456 rw 86401', None),
    ('123456 ro 1000000', None),
    ('123456 rw 999999999999', None),
    # unknown mode, wrong case, non-digit seconds, trailing junk
    ('123456 banana', None),
    ('123456 RW', None),
    ('123456 rw 12x', None),
    ('123456 rw 999 extra', None),
    ('123456 ro 60 70', None),
    (None, None),
])
def test_parse_unlock_args_matrix(arg, expected):
    assert parse_unlock_args(arg) == expected


# -- opaque failures -------------------------------------------------------


def test_wrong_code_malformed_and_empty_are_byte_identical(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    replies = []
    replies.append(svc.handle_update(_msg(1, '/unlock 000000'))[0].text)
    replies.append(svc.handle_update(_msg(2, '/unlock'))[0].text)
    replies.append(svc.handle_update(_msg(3, '/unlock 123456 999'))[0].text)
    replies.append(svc.handle_update(_msg(4, '/unlock abcdef rw'))[0].text)
    replies.append(svc.handle_update(_msg(5, '/unlock " "'))[0].text)
    assert replies == ['unable to unlock'] * len(replies)
    assert stub.calls == []
    # Still locked: a prompt is gated.
    assert svc.handle_update(_msg(6, 'hello'))[0].text == 'PAA is locked'
    assert stub.calls == []


def test_cooldown_blocks_even_a_correct_code(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    for i in range(1, 4):
        out = svc.handle_update(_msg(i, '/unlock 000000'))
        assert out[0].text == 'unable to unlock'
    # Correct code during cooldown: same opaque failure.
    out = svc.handle_update(_msg(4, _unlock(clock)))
    assert out[0].text == 'unable to unlock'
    assert stub.calls == []
    # After the cooldown a correct code unlocks.
    clock.t += 301
    out = svc.handle_update(_msg(5, _unlock(clock)))
    assert out[0].text == 'Unlocked ro for 3600s.'
    assert svc.handle_update(_msg(6, 'hello'))[0].text.splitlines()[0] \
        == 'stub-reply'


def test_secret_unreadable_every_unlock_fails_opaque(tmp_path, caplog):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock, secret=None)
    with caplog.at_level(logging.ERROR, logger='paa_turns'):
        out = svc.handle_update(_msg(1, _unlock(clock)))
    assert out[0].text == 'unable to unlock'
    assert stub.calls == []
    assert svc.handle_update(_msg(2, 'hello'))[0].text == 'PAA is locked'
    errors = [r for r in caplog.records
              if r.name == 'paa_turns' and r.levelno >= logging.ERROR]
    assert errors
    assert any('TOTP' in r.getMessage() for r in errors)
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()


# -- locked gate ------------------------------------------------------------


def test_locked_gates_message_command_and_status(tmp_path):
    svc, stub = _service(tmp_path, clock=FakeClock())
    assert svc.handle_update(_msg(1, 'hello'))[0].text == 'PAA is locked'
    assert svc.handle_update(_msg(2, '/status'))[0].text == 'PAA is locked'
    assert svc.handle_update(_msg(3, '/findings'))[0].text == 'PAA is locked'
    assert svc.handle_update(_msg(4, '/lock'))[0].text == 'PAA is locked'
    assert not os.path.exists(os.path.join(svc.state_dir, 'paa-choices.json'))
    assert stub.calls == []


def test_locked_callback_is_answered_and_never_resolved(tmp_path):
    svc, stub = _service(tmp_path, clock=FakeClock())
    # A live keyboard created while the service is locked (white-box: scan
    # notices can create keyboards while inbound is gated).
    group, _prompt, markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick',
        options=[{'label': 'A', 'value': 'a'}],
        meaning={'kind': 'drill-test'},
    )
    cid = markup['inline_keyboard'][0][0]['callback_data']
    outs = svc.handle_update(_callback(1, cid, cq_id='cq-locked'))
    assert len(outs) == 1
    assert outs[0].kind == 'answer_callback'
    assert outs[0].callback_query_id == 'cq-locked'
    assert outs[0].answer_text == 'PAA is locked'
    # Not resolved, not expired: the choice is still pending.
    assert svc.choices.group_outcome(group) is None
    assert stub.calls == []


# -- unlock, modes, turns ---------------------------------------------------


def test_unlock_ro_runs_plan_and_status_reports_window(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    out = svc.handle_update(_msg(1, _unlock(clock)))
    assert out[0].text == 'Unlocked ro for 3600s.'
    assert os.path.isfile(svc.unlock_path)
    status = svc.handle_update(_msg(2, '/status'))[0].text
    assert 'Lock: unlocked' in status
    assert 'Mode: ro' in status
    assert 'Seconds remaining: 3600' in status
    assert SECRET not in status
    reply = svc.handle_update(_msg(3, 'hello'))
    assert reply[0].text.splitlines()[0] == 'stub-reply'
    assert stub.calls[-1]['tool_policy'] == 'plan'


def test_unlock_rw_defaults_and_explicit_seconds(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    assert svc.handle_update(
        _msg(1, _unlock(clock, suffix=' rw')))[0].text == 'Unlocked rw for 600s.'
    # A fresh /unlock replaces the window.
    assert svc.handle_update(
        _msg(2, _unlock(clock, suffix=' rw 999')))[0].text == \
        'Unlocked rw for 999s.'
    status = svc.handle_update(_msg(3, '/status'))[0].text
    assert 'Mode: rw' in status
    assert 'Seconds remaining: 999' in status


def test_unlock_ro_mode_with_explicit_seconds(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    assert svc.handle_update(
        _msg(1, _unlock(clock, suffix=' ro 90')))[0].text == \
        'Unlocked ro for 90s.'
    clock.t += 45
    status = svc.handle_update(_msg(2, '/status'))[0].text
    assert 'Seconds remaining: 45' in status


def _live_unix_listener(tmp_path, name='broker.sock'):
    """A bound, listening AF_UNIX socket — what a real broker looks like."""
    path = str(tmp_path / name)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)
    return listener, path


def test_rw_turn_runs_gate_with_sock_from_provider(tmp_path):
    clock = FakeClock()
    seen = []
    listener, sock_path = _live_unix_listener(tmp_path)

    def provider():
        seen.append(1)
        return (sock_path, 'tok-1')

    try:
        svc, stub = _service(tmp_path, clock=clock, provider=provider)
        svc.handle_update(_msg(1, _unlock(clock, suffix=' rw')))
        svc.handle_update(_msg(2, 'edit the readme'))
        assert stub.calls[-1]['tool_policy'] == 'gate'
        assert stub.calls[-1]['approval_sock'] == sock_path
        assert stub.calls[-1]['approval_token'] == 'tok-1'
        assert seen
    finally:
        listener.close()


def test_rw_turn_refused_when_sock_path_is_dead(tmp_path):
    missing = str(tmp_path / 'missing.sock')
    regular = tmp_path / 'regular.sock'
    regular.write_text('not a socket')
    refusing_path = str(tmp_path / 'refusing.sock')
    refusing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    refusing.bind(refusing_path)  # bound but never listened on
    cases = {
        'nonexistent': lambda: (missing, 'tok'),
        'regular-file': lambda: (str(regular), 'tok'),
        'refusing-socket': lambda: (refusing_path, 'tok'),
        # Malformed channel shapes fail closed too.
        'bare-string': lambda: missing,
        'sock-only': lambda: (refusing_path,),
        'empty-token': lambda: (refusing_path, ''),
    }
    try:
        for name, provider in cases.items():
            clock = FakeClock()
            state = str(tmp_path / f'state-{name}')
            _write_config(state)
            stub = _Stub()
            svc = TurnService(state, _settings(), stub, now_fn=clock,
                              approval_sock_provider=provider,
                              unlock_secret_fn=lambda: SECRET)
            svc.handle_update(_msg(1, _unlock(clock, suffix=' rw')))
            out = svc.handle_update(_msg(2, 'edit the readme'))
            assert 'unavailable' in out[0].text, name
            assert 'rw' in out[0].text, name
            assert stub.calls == [], name
    finally:
        refusing.close()


def test_rw_turn_without_broker_is_refused_and_nothing_spawns(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock, provider=lambda: None)
    svc.handle_update(_msg(1, _unlock(clock, suffix=' rw')))
    out = svc.handle_update(_msg(2, 'edit the readme'))
    assert 'unavailable' in out[0].text
    assert 'rw' in out[0].text
    assert stub.calls == []


def test_rw_turn_broken_provider_fails_closed(tmp_path, caplog):
    clock = FakeClock()

    def provider():
        raise RuntimeError('broker blew up')

    svc, stub = _service(tmp_path, clock=clock, provider=provider)
    svc.handle_update(_msg(1, _unlock(clock, suffix=' rw')))
    with caplog.at_level(logging.ERROR, logger='paa_turns'):
        out = svc.handle_update(_msg(2, 'edit the readme'))
    assert 'unavailable' in out[0].text
    assert stub.calls == []
    assert any('approval socket provider' in r.getMessage()
               for r in caplog.records if r.name == 'paa_turns')


# -- expiry, /lock, persistence ---------------------------------------------


def test_window_expiry_relocks_by_injected_clock(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    svc.handle_update(_msg(1, _unlock(clock)))
    clock.t += 3599
    assert 'stub-reply' in svc.handle_update(_msg(2, 'ping'))[0].text
    clock.t += 2  # past 3600
    assert svc.handle_update(_msg(3, 'ping'))[0].text == 'PAA is locked'
    assert len(stub.calls) == 1
    # Expired state is cleared on disk at first observation.
    assert not os.path.exists(svc.unlock_path)


def test_window_survives_restart_until_expiry(tmp_path):
    clock = FakeClock()
    listener, sock_path = _live_unix_listener(tmp_path)
    try:
        svc, stub = _service(tmp_path, clock=clock,
                             provider=lambda: sock_path)
        svc.handle_update(_msg(1, _unlock(clock, suffix=' rw 999')))
        saved = json.loads(open(svc.unlock_path).read())
        assert saved['mode'] == 'rw'
        assert saved['expires_at'] == clock.t + 999

        # Fresh service on the same state: window still open, rw still gated.
        stub2 = _Stub()
        svc2 = TurnService(
            svc.state_dir, _settings(), stub2,
            now_fn=clock, approval_sock_provider=lambda: (sock_path, 'tok'),
            unlock_secret_fn=lambda: SECRET,
        )
        svc2.handle_update(_msg(2, 'edit again'))
        assert stub2.calls[-1]['tool_policy'] == 'gate'
        assert stub2.calls[-1]['approval_sock'] == sock_path
        assert stub2.calls[-1]['approval_token'] == 'tok'
    finally:
        listener.close()

    # Past expiry a fresh service loads as locked.
    clock.t += 1000
    stub3 = _Stub()
    svc3 = TurnService(
        svc.state_dir, _settings(), stub3,
        now_fn=clock, unlock_secret_fn=lambda: SECRET,
    )
    assert svc3.handle_update(_msg(3, 'hi'))[0].text == 'PAA is locked'
    assert stub3.calls == []


def test_lock_closes_early_and_expires_pending_keyboards(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    svc.handle_update(_msg(1, _unlock(clock)))
    group, _prompt, markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick',
        options=[{'label': 'A', 'value': 'a'}],
        meaning={'kind': 'drill-test'},
        message_id=600,
    )
    assert svc.choices.live_groups() == {group}
    out = svc.handle_update(_msg(3, '/lock'))
    assert out[0].text == 'PAA is locked'
    assert not os.path.exists(svc.unlock_path)
    assert svc.choices.live_groups() == set()
    assert svc.choices.group_outcome(group) == ('expired', None)
    # While locked the tap is gated like any other callback.
    cid = markup['inline_keyboard'][0][0]['callback_data']
    tapped = svc.handle_update(_callback(4, cid, cq_id='cq-stale'))
    assert tapped[0].kind == 'answer_callback'
    assert tapped[0].answer_text == 'PAA is locked'
    assert len(tapped) == 1
    assert stub.calls == []
    # Re-unlock: the pre-lock keyboard expired at /lock and cannot fire.
    svc.handle_update(_msg(5, _unlock(clock)))
    stale = svc.handle_update(_callback(6, cid, cq_id='cq-after-relock'))
    assert stale[0].answer_text == 'That choice expired.'
    assert len(stale) == 1
    assert stub.calls == []


def test_expire_pending_groups_on_expiry_transition(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    svc.handle_update(_msg(1, _unlock(clock)))
    group, _prompt, _markup = svc.choices.create(
        chat_id=UID, user_id=UID, prompt='Pick',
        options=[{'label': 'A', 'value': 'a'}],
        meaning={'kind': 'drill-test'},
    )
    clock.t += 4000  # window lapses with no traffic
    # The next inbound event observes the expiry and is gated.
    assert svc.handle_update(_msg(3, '/status'))[0].text == 'PAA is locked'
    assert svc.choices.group_outcome(group) == ('expired', None)


# -- config ------------------------------------------------------------------


def test_totp_pass_entry_config_default_and_override(tmp_path):
    path = str(tmp_path / 'paa-telegram.json')
    with open(path, 'w') as fh:
        json.dump({'allowed_user_ids': [UID]}, fh)
    cfg = load_bot_config(path)
    assert cfg['totp_pass_entry'] == 'external/telegram/paa-bot-totp'
    with open(path, 'w') as fh:
        json.dump({'allowed_user_ids': [UID],
                   'totp_pass_entry': 'custom/totp-entry'}, fh)
    cfg = load_bot_config(path)
    assert cfg['totp_pass_entry'] == 'custom/totp-entry'


def test_default_secret_reader_uses_configured_pass_entry(
        tmp_path, monkeypatch):
    clock = FakeClock()
    svc, stub = _service(
        tmp_path, clock=clock, secret=_USE_PASS,
        extra_config={'totp_pass_entry': 'custom/totp-entry'},
    )
    # No injected secret fn: the default reader shells out to pass. Stub
    # the pass layer itself and confirm the configured entry is requested.
    seen = []

    def fake_read(entry):
        seen.append(entry)
        return SECRET

    monkeypatch.setattr(paa_turns, '_read_pass_secret', fake_read)
    out = svc.handle_update(_msg(1, _unlock(clock)))
    assert out[0].text == 'Unlocked ro for 3600s.'
    assert seen == ['custom/totp-entry']
    assert stub.calls == []


# -- unlocked ro keeps today's behavior --------------------------------------


def test_unlocked_ro_findings_work_as_before(tmp_path):
    from paa_ledger import Ledger, LedgerItem

    clock = FakeClock()
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    ledger.add_if_new(LedgerItem(
        id='finding-alpha', type='no-git', project='alpha',
        project_path='/p/alpha', summary='alpha is dirty', evidence='ev-a',
        severity='warning', created='2026-09-30T00:00:00+00:00',
    ))
    state = str(tmp_path / 'state')
    _write_config(state)
    stub = _Stub()
    svc = TurnService(state, _settings(), stub,
                      now_fn=clock, unlock_secret_fn=lambda: SECRET,
                      ledger=ledger)
    svc.handle_update(_msg(1, _unlock(clock)))
    svc.handle_update(_msg(4, 'what is alpha?'))
    assert stub.calls[-1]['tool_policy'] == 'plan'
    findings = svc.handle_update(_msg(5, '/findings'))
    assert 'alpha is dirty' in findings[0].text


# -- run-time lock gate for deferred and spool-replayed follows (H1) ---------


def _commit(svc, update):
    while svc.unsent_actions(update):
        svc.mark_sent(update)
    svc.commit_delivery(update)


def test_deferred_follow_started_after_expiry_is_refused_and_dropped(
        tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    unlock_update = _msg(1, _unlock(clock))
    svc.handle_update(unlock_update)
    _commit(svc, unlock_update)
    # A prompt dispatched while unlocked, queued behind a running turn:
    # the follow cannot start until the worker is free.
    with svc._worker:
        _immediate, follow = svc.prepare_update(_msg(3, 'edit the readme'))
        assert follow is not None
        # The window expires while the follow waits on the worker.
        clock.t += 4000
    outs = svc.run_follow(follow)
    assert [o.text for o in outs] == ['PAA is locked']
    assert outs[0].chat_id == UID
    assert stub.calls == []
    # The follow is dropped, not stored.
    svc.append_ready(_msg(3, 'edit the readme'), outs)
    record = json.loads(open(svc._spool_path(3)).read())
    assert record['ready'] is True
    assert record['follow'] is None
    # After a re-unlock the dropped follow does not fire.
    assert svc.handle_update(_msg(4, _unlock(clock)))[0].text == \
        'Unlocked ro for 3600s.'
    svc.ensure_ready(_msg(3, 'edit the readme'))
    assert stub.calls == []
    svc.handle_update(_msg(5, 'edit the readme'))
    assert stub.calls[-1]['tool_policy'] == 'plan'


def test_queued_follow_lock_checked_after_worker_acquire(tmp_path):
    """R1: the run-time lock check must happen after the worker is won.

    A follow that passed dispatch while unlocked queues behind a running
    turn; the window expires with no inbound traffic while it waits. If
    the check ran before the worker acquire, the follow would start
    post-expiry with pre-expiry state.
    """
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    unlock_update = _msg(1, _unlock(clock))
    svc.handle_update(unlock_update)
    _commit(svc, unlock_update)
    _immediate, follow = svc.prepare_update(_msg(3, 'deploy the fix'))
    assert follow is not None

    started = threading.Event()
    result = []

    def run():
        started.set()
        result.extend(svc.run_follow(follow))

    with svc._worker:
        worker = threading.Thread(target=run)
        worker.start()
        assert started.wait(5)
        time.sleep(0.2)  # let the follow block on the worker acquire
        # The window expires while the follow waits, with no inbound traffic.
        clock.t += 4000
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert [o.text for o in result] == ['PAA is locked']
    assert stub.calls == []


def test_spool_replay_follow_after_expiry_is_refused_and_dropped(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    unlock_update = _msg(1, _unlock(clock))
    svc.handle_update(unlock_update)
    _commit(svc, unlock_update)
    # Crash window: the prompt is persisted with its follow, never run.
    _immediate, follow = svc.prepare_update(_msg(2, 'edit the readme'))
    assert follow is not None
    record = json.loads(open(svc._spool_path(2)).read())
    assert record['ready'] is False
    assert record['follow'] is not None

    # Restart after the window expired: the service loads locked.
    clock.t += 4000
    stub2 = _Stub()
    svc2 = TurnService(svc.state_dir, _settings(), stub2,
                       now_fn=clock, unlock_secret_fn=lambda: SECRET)
    # Fresh inbound is gated...
    fresh = _msg(3, 'hi')
    assert svc2.handle_update(fresh)[0].text == 'PAA is locked'
    assert stub2.calls == []
    _commit(svc2, fresh)
    # ...and the spooled follow is dropped with a locked reply, never run.
    replayed = svc2.replay_spool()
    assert [o.text for o in replayed] == ['PAA is locked']
    assert stub2.calls == []
    record = json.loads(open(svc2._spool_path(2)).read())
    assert record['ready'] is True
    assert record['follow'] is None
    # Re-unlock: the dropped follow must not fire on the next unlock, and a
    # second replay only resends the stored locked reply.
    relock = _msg(4, _unlock(clock))
    assert svc2.handle_update(relock)[0].text == 'Unlocked ro for 3600s.'
    _commit(svc2, relock)
    again = svc2.replay_spool()
    assert [o.text for o in again] == ['PAA is locked']
    assert stub2.calls == []


def test_legacy_armed_session_key_is_ignored(tmp_path):
    """Pre-upgrade session files persist an 'armed' key. It must be inert:
    the flag is gone, the next turn runs read-only plan."""
    state = str(tmp_path / 'state')
    _write_config(state)
    with open(os.path.join(state, 'paa-session.json'), 'w') as fh:
        json.dump({'session_id': None, 'harness_id': 'grok', 'armed': True,
                   'archive': [], 'updated': '0'}, fh)
    clock = FakeClock()
    stub = _Stub()
    svc = TurnService(state, _settings(), stub,
                      now_fn=clock, unlock_secret_fn=lambda: SECRET)
    assert not hasattr(svc, 'armed')
    svc.handle_update(_msg(1, _unlock(clock)))
    svc.handle_update(_msg(2, 'edit the readme'))
    assert stub.calls[-1]['tool_policy'] == 'plan'


# -- undecodable TOTP secret (H3) ---------------------------------------------


def test_undecodable_secret_every_unlock_fails_opaque_and_service_stays_up(
        tmp_path, caplog):
    clock = FakeClock()
    bad = '####not-base32####'
    svc, stub = _service(tmp_path, clock=clock, secret=bad)
    with caplog.at_level(logging.ERROR, logger='paa_turns'):
        out = svc.handle_update(_msg(1, _unlock(clock)))
    assert out[0].text == 'unable to unlock'
    assert stub.calls == []
    # Every further attempt is the same opaque failure (previously the
    # poll loop died here and systemd restarted it — a crash loop).
    out = svc.handle_update(_msg(2, _unlock(clock)))
    assert out[0].text == 'unable to unlock'
    assert svc.handle_update(_msg(3, 'hello'))[0].text == 'PAA is locked'
    assert stub.calls == []
    errors = [r for r in caplog.records
              if r.name == 'paa_turns' and r.levelno >= logging.ERROR]
    assert any('decode' in r.getMessage() for r in errors)
    for rec in caplog.records:
        assert bad not in rec.getMessage()


# -- cooldown does not extend on further attempts (M5) -------------------------


def test_cooldown_attempts_do_not_extend_cooldown(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    t0 = clock.t
    for i in range(1, 4):
        assert svc.handle_update(_msg(i, '/unlock 000000'))[0].text == \
            'unable to unlock'
    assert svc._throttle._failures[UID] == (3, t0 + 300)
    # Well-formed and malformed attempts during the cooldown — made late in
    # the window, so a recorded failure would push locked_until past t0+300.
    clock.t = t0 + 120
    assert svc.handle_update(_msg(4, _unlock(clock)))[0].text == \
        'unable to unlock'
    assert svc.handle_update(_msg(5, '/unlock 123456 999'))[0].text == \
        'unable to unlock'
    assert svc.handle_update(_msg(6, '/unlock'))[0].text == 'unable to unlock'
    assert svc._throttle._failures[UID] == (3, t0 + 300)
    assert stub.calls == []
    # At the original cooldown end a correct code unlocks.
    clock.t = t0 + 301
    assert svc.handle_update(_msg(7, _unlock(clock)))[0].text == \
        'Unlocked ro for 3600s.'


# -- bounded seconds (M6) -------------------------------------------------------


def test_unlock_out_of_range_seconds_fail_opaque(tmp_path):
    clock = FakeClock()
    svc, stub = _service(tmp_path, clock=clock)
    # Correct code, out-of-range seconds: opaque failure, still locked.
    assert svc.handle_update(
        _msg(1, _unlock(clock, suffix=' rw 0')))[0].text == 'unable to unlock'
    assert svc.handle_update(
        _msg(2, _unlock(clock, suffix=' rw 86401')))[0].text == \
        'unable to unlock'
    assert svc.handle_update(
        _msg(3, _unlock(clock, suffix=' ro 1000000')))[0].text == \
        'unable to unlock'
    assert svc.handle_update(_msg(4, 'hello'))[0].text == 'PAA is locked'
    assert stub.calls == []
    # ro allows long windows: 86401 (> rw max, < ro max) unlocks once the
    # throttle cooldown from the failures above expires.
    clock.t += 301
    assert svc.handle_update(
        _msg(5, _unlock(clock, suffix=' ro 86401')))[0].text == \
        'Unlocked ro for 86401s.'


# -- non-finite window state (M7) ------------------------------------------------


@pytest.mark.parametrize('literal', ['Infinity', '1e999'])
def test_unlock_window_non_finite_expires_at_loads_locked(tmp_path, literal):
    state = str(tmp_path / 'state')
    _write_config(state)
    with open(os.path.join(state, 'paa-unlock.json'), 'w') as fh:
        fh.write('{"mode": "ro", "expires_at": %s}' % literal)
    svc, stub = _service(tmp_path, clock=FakeClock())
    assert svc.handle_update(_msg(1, 'hello'))[0].text == 'PAA is locked'
    assert stub.calls == []


# -- failed pass read is not retried immediately (M4) -----------------------------


def test_failed_secret_read_is_not_retried_within_retry_window(tmp_path):
    clock = FakeClock()
    calls = []

    def failing_reader():
        calls.append(1)
        raise RuntimeError('gpg-agent blocked on pinentry')

    state = str(tmp_path / 'state')
    _write_config(state)
    stub = _Stub()
    svc = TurnService(state, _settings(), stub, now_fn=clock,
                      unlock_secret_fn=failing_reader)
    assert svc.handle_update(_msg(1, _unlock(clock)))[0].text == \
        'unable to unlock'
    assert len(calls) == 1
    # A second attempt inside the retry window must not re-stall the poll
    # thread with another synchronous pass show.
    assert svc.handle_update(_msg(2, _unlock(clock)))[0].text == \
        'unable to unlock'
    assert len(calls) == 1
    # Past the window the read is retried (and still fails opaquely).
    clock.t += 61
    assert svc.handle_update(_msg(3, _unlock(clock)))[0].text == \
        'unable to unlock'
    assert len(calls) == 2
    assert stub.calls == []
