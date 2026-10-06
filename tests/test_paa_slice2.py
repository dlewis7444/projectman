"""Slice 2: real fcntl locks, one scan, findings in the same chat.

Two ledger writers are real processes. The lock is not mocked. Scan and
chat go through the shipped functions. GTK is not clicked.
"""
import fcntl
import json
import os
import subprocess
import sys
import threading
import time

from unittest.mock import patch

from harnesses import HeadlessResult
from paa_findings import discuss_finding_prompt
from paa_headless import chat_turn
from paa_ledger import Ledger, LedgerFileRelay, LedgerItem
from paa_monitor import PAAMonitor
from paa_turns import TurnService
from model import ProjectStore
from settings import Settings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = 42


def _run(script, *args, timeout=20):
    env = os.environ.copy()
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run(
        [sys.executable, '-c', script, REPO, *map(str, args)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout,
    )


_ADD = r'''
import sys
sys.path.insert(0, sys.argv[1])
from paa_ledger import Ledger, LedgerItem
path, item_id, project = sys.argv[2], sys.argv[3], sys.argv[4]
ledger = Ledger(path)
ok = ledger.add_if_new(LedgerItem(
    id=item_id, type="no-git", project=project, project_path="/p/" + project,
    summary="row " + project, evidence="e-" + item_id, severity="warning",
    created="2026-09-01T00:00:00+00:00",
))
print("added" if ok else "skipped")
'''

_DISMISS = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from paa_ledger import Ledger
path, item_id, ready, go = sys.argv[2:6]
ledger = Ledger(path)
ledger.load()
open(ready, "w").write("ready")
while not os.path.exists(go):
    time.sleep(0.05)
ok = ledger.update_status(item_id, "dismissed")
print("dismissed" if ok else "missing")
'''

_HOLD = r'''
import fcntl, os, sys, time
lock_path, release, held = sys.argv[2], sys.argv[3], sys.argv[4]
fh = open(lock_path, "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
open(held, "w").write("1")
while not os.path.exists(release):
    time.sleep(0.05)
fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
fh.close()
print("released")
'''


def _item(item_id, project, summary, created, severity='warning'):
    return LedgerItem(
        id=item_id, type='no-git', project=project, project_path='/p/' + project,
        summary=summary, evidence='ev-' + item_id, severity=severity,
        created=created,
    )


def test_two_processes_keep_each_others_rows(tmp_path):
    path = str(tmp_path / 'ledger.json')
    first = _run(_ADD, path, 'row-alpha', 'alpha')
    assert first.returncode == 0, first.stderr
    # Overlap the second add with a dismiss that loaded before the add.
    ready = str(tmp_path / 'ready')
    go = str(tmp_path / 'go')
    env = os.environ.copy()
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    dismiss = subprocess.Popen(
        [sys.executable, '-c', _DISMISS, REPO, path, 'row-alpha', ready, go],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(50):
        if os.path.exists(ready):
            break
        time.sleep(0.05)
    assert os.path.exists(ready)
    second = _run(_ADD, path, 'row-beta', 'beta')
    assert second.returncode == 0, second.stderr
    open(go, 'w').write('go')
    out, err = dismiss.communicate(timeout=10)
    assert dismiss.returncode == 0, err
    assert 'dismissed' in out
    fresh = Ledger(path)
    fresh.load()
    assert fresh._items['row-alpha'].status == 'dismissed'
    assert fresh._items['row-beta'].status == 'pending'


def test_parallel_adds_both_survive(tmp_path):
    path = str(tmp_path / 'ledger.json')
    env = os.environ.copy()
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    procs = [
        subprocess.Popen(
            [sys.executable, '-c', _ADD, REPO, path, item_id, project],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for item_id, project in (('row-red', 'red'), ('row-blue', 'blue'))
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, err
        assert 'added' in out
    fresh = Ledger(path)
    fresh.load()
    assert set(fresh._items) == {'row-red', 'row-blue'}
    assert all(item.status == 'pending' for item in fresh._items.values())


def test_sweep_and_save_reload_under_the_lock(tmp_path):
    path = str(tmp_path / 'ledger.json')
    first = Ledger(path)
    alpha = _item('row-alpha', 'alpha', 'a', '2026-09-02T00:00:00+00:00')
    assert first.add_if_new(alpha) is True
    other = Ledger(path)
    beta = _item('row-beta', 'beta', 'b', '2026-09-03T00:00:00+00:00')
    assert other.add_if_new(beta) is True
    # first's memory does not yet contain beta. sweep must reload.
    first.sweep({'row-alpha', 'row-beta'})
    fresh = Ledger(path)
    fresh.load()
    assert set(fresh._items) == {'row-alpha', 'row-beta'}
    assert fresh._items['row-beta'].status == 'pending'
    # A later save of the stale object must not drop beta either.
    stale = Ledger(path)
    stale.load()
    assert 'row-beta' in stale._items
    extra = Ledger(path)
    gamma = _item('row-gamma', 'gamma', 'g', '2026-09-04T00:00:00+00:00')
    assert extra.add_if_new(gamma) is True
    stale.save()
    fresh.load()
    assert set(fresh._items) >= {'row-alpha', 'row-beta', 'row-gamma'}


def test_stale_save_keeps_a_dismiss_from_the_other_ledger(tmp_path):
    """The reproduced clobber: memory still says pending, disk says dismissed."""
    path = str(tmp_path / 'ledger.json')
    first = Ledger(path)
    assert first.add_if_new(
        _item('row-alpha', 'alpha', 'a', '2026-09-01T00:00:00+00:00')) is True
    assert first._items['row-alpha'].status == 'pending'
    other = Ledger(path)
    assert other.update_status('row-alpha', 'dismissed') is True
    assert first._items['row-alpha'].status == 'pending'
    first.save()
    fresh = Ledger(path)
    fresh.load()
    assert fresh._items['row-alpha'].status == 'dismissed'
    # A row that exists only in the stale object's memory is still added.
    first._items['row-only-memory'] = _item(
        'row-only-memory', 'mem', 'm', '2026-09-05T00:00:00+00:00')
    first.save()
    fresh.load()
    assert fresh._items['row-alpha'].status == 'dismissed'
    assert fresh._items['row-only-memory'].status == 'pending'


def test_ack_blocks_while_the_ledger_lock_is_held(tmp_path):
    path = str(tmp_path / 'ledger.json')
    ledger = Ledger(path)
    ledger.add_if_new(_item('row-alpha', 'alpha', 'a', '2026-09-01T00:00:00+00:00'))
    held = str(tmp_path / 'held')
    release = str(tmp_path / 'release')
    env = os.environ.copy()
    env['PYTHONPATH'] = REPO + os.pathsep + env.get('PYTHONPATH', '')
    holder = subprocess.Popen(
        [sys.executable, '-c', _HOLD, REPO, path + '.lock', release, held],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(50):
        if os.path.exists(held):
            break
        time.sleep(0.05)
    assert os.path.exists(held)
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps({
        'allowed_user_ids': [UID], 'chat_timeout_sec': 600,
    }))
    svc = TurnService(str(state), Settings(harness_default='grok'), lambda *a, **k: None, ledger=ledger)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    box = {}

    def ack():
        box['outs'] = svc.handle_update({
            'update_id': 1,
            'message': {
                'message_id': 1, 'date': 1, 'text': '/ack row-alpha',
                'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
            },
        })

    thread = threading.Thread(target=ack)
    thread.start()
    thread.join(0.4)
    assert thread.is_alive()
    open(release, 'w').write('1')
    thread.join(5)
    assert not thread.is_alive()
    assert 'Acknowledged row-alpha' in box['outs'][0].text
    holder.communicate(timeout=5)
    fresh = Ledger(path)
    fresh.load()
    assert fresh._items['row-alpha'].status == 'acknowledged'


def _monitor(tmp_path, **kw):
    projects = tmp_path / 'projects'
    projects.mkdir(exist_ok=True)
    (projects / 'alpha').mkdir(exist_ok=True)
    settings = Settings(
        projects_dir=str(projects),
        paa_enabled=kw.pop('paa_enabled', True),
        paa_allow_haiku=kw.pop('paa_allow_haiku', False),
        paa_loop_interval_minutes=kw.pop('paa_loop_interval_minutes', 30),
        paa_budget_tokens=kw.pop('paa_budget_tokens', 1000),
        paa_budget_used=kw.pop('paa_budget_used', 0),
        paa_budget_month=kw.pop('paa_budget_month', '2026-03'),
        **kw,
    )
    store = ProjectStore(settings)
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    return PAAMonitor(store, ledger, settings), ledger, settings, projects


def test_held_scan_lock_skips_run_scan_and_ai_scan(tmp_path):
    import paa_monitor
    # AI scans have to be switched on; otherwise the enablement guard
    # answers before the scan lock is consulted.
    mon, ledger, settings, projects = _monitor(
        tmp_path, paa_enabled=True, paa_allow_haiku=True)
    fh = open(paa_monitor.SCAN_LOCK_PATH, 'a+')
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        mon.run_scan()
        assert ledger.pending_count == 0
        blocked = []
        mon.connect('scan-blocked', lambda _m, reason: blocked.append(reason))
        with patch('paa_haiku.run_ai_checks') as spy:
            started = mon.scan_single_project('alpha', str(projects / 'alpha'))
        assert started is False
        spy.assert_not_called()
        assert blocked and 'The PAA service is scanning' in blocked[0]
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def test_disabled_ai_scan_still_names_the_setting(tmp_path):
    mon, _ledger, _settings, projects = _monitor(
        tmp_path, paa_enabled=False, paa_allow_haiku=True)
    blocked = []
    mon.connect('scan-blocked', lambda _m, reason: blocked.append(reason))
    with patch('paa_haiku.run_ai_checks') as spy:
        started = mon.scan_single_project('alpha', str(projects / 'alpha'))
    assert started is False
    spy.assert_not_called()
    assert blocked and 'disabled' in blocked[0]


def test_overlapping_scan_does_not_start(tmp_path):
    mon, _ledger, _settings, _projects = _monitor(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def load_projects():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return []

    mon._store.load_projects = load_projects
    thread = threading.Thread(target=mon.run_scan)
    thread.start()
    assert entered.wait(3)
    mon.run_scan()
    assert calls == [1]
    release.set()
    thread.join(5)
    assert calls == [1]


def test_filesystem_findings_when_budget_is_spent_and_chat_is_separate(tmp_path):
    mon, ledger, settings, _projects = _monitor(
        tmp_path,
        paa_allow_haiku=True,
        paa_budget_tokens=100,
        paa_budget_used=100,
        paa_budget_month='2026-03',
    )
    with patch('paa_monitor._current_month', return_value='2026-03'), \
         patch('paa_haiku.run_ai_checks') as spy:
        mon.run_scan()
    spy.assert_not_called()
    types = {item.type for item in ledger.pending_items()}
    assert 'missing-agents-md' in types or 'no-git' in types
    assert settings.paa_budget_used == 100

    chat_settings = Settings(paa_budget_used=42, paa_budget_tokens=100)
    completed = subprocess.CompletedProcess(
        args=['claude'], returncode=0,
        stdout=json.dumps({
            'type': 'result', 'subtype': 'success', 'is_error': False,
            'result': 'chat ok',
            'usage': {'input_tokens': 3, 'output_tokens': 4},
        }),
        stderr='',
    )
    with patch('subprocess.run', return_value=completed):
        result = chat_turn(chat_settings, 'hello from the phone')
    assert result.text == 'chat ok'
    assert chat_settings.paa_budget_used == 42


def test_ledger_file_relay_reloads_pending_row_and_count(tmp_path):
    path = str(tmp_path / 'ledger.json')
    viewer = Ledger(path)
    writer = Ledger(path)
    counts = []
    refreshes = []
    relay = LedgerFileRelay(
        viewer, counts.append, on_refresh=lambda: refreshes.append(1))
    assert relay.on_file_event('CHANGED') == 0
    writer.add_if_new(_item(
        'row-new', 'alpha', 'fresh summary', '2026-09-30T00:00:00+00:00'))
    assert relay.on_file_event('CHANGED') == 1
    pending = viewer.pending_items()
    assert [item.id for item in pending] == ['row-new']
    assert counts == [0, 1]
    assert refreshes == [1, 1]
    assert relay.on_file_event('not-a-ledger-event') is None
    import inspect
    import window
    source = inspect.getsource(window.AppWindow._start_ledger_watch)
    assert 'LedgerFileRelay' in source
    assert 'set_paa_pending_count' in source
    assert 'refresh_from_scan' in inspect.getsource(window.AppWindow._refresh_open_paa_cards)


def _turn(tmp_path, ledger, chat):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps({
        'allowed_user_ids': [UID], 'chat_timeout_sec': 600,
    }))
    settings = Settings(
        harness_default='grok', paa_enabled=True, paa_allow_haiku=False,
        paa_loop_interval_minutes=30, paa_budget_used=5,
    )
    svc = TurnService(str(state), settings, chat, ledger=ledger)
    svc._unlock_window = {'mode': 'ro', 'expires_at': svc._now() + 3600}
    return svc, settings


def test_findings_commands_and_discuss_use_one_session(tmp_path):
    import paa_card_window
    assert paa_card_window.discuss_finding_prompt is discuss_finding_prompt
    ledger = Ledger(path=str(tmp_path / 'ledger.json'))
    alpha = _item('finding-alpha', 'alpha', 'alpha is dirty', '2026-09-30T00:00:00+00:00')
    sibling = _item('finding-sibling', 'alpha', 'alpha also drifts', '2026-09-29T00:00:00+00:00')
    ledger.add_if_new(alpha)
    ledger.add_if_new(sibling)
    calls = []

    def chat(settings, prompt, *, session_id=None, project_path='',
             tool_policy='legacy', timeout=None, handle=None, **kwargs):
        calls.append({
            'prompt': prompt,
            'session_id': session_id,
            'tool_policy': tool_policy,
            'timeout': timeout,
        })
        return HeadlessResult(text='discussed', session_id=session_id or 'sess-9', tokens=6)

    svc, settings = _turn(tmp_path, ledger, chat)
    opened = svc.handle_update({
        'update_id': 1,
        'message': {
            'message_id': 1, 'date': 1, 'text': 'hello',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    })
    assert 'discussed' in opened[0].text
    assert svc.session_id == 'sess-9'
    assert calls[0]['session_id'] is None

    noted = svc.notify_new_findings(set(), chat_id=UID, user_id=UID)
    assert len(noted) == 2
    for msg, item in zip(noted, ledger.pending_items()):
        assert msg.text == f'{item.project} [{item.severity}] {item.summary}'
        labels = [
            b['text'] for row in msg.reply_markup['inline_keyboard'] for b in row
        ]
        assert labels == ['Ack', 'Dismiss', 'Discuss']
        ids = [
            b['callback_data']
            for row in msg.reply_markup['inline_keyboard'] for b in row
        ]
        assert all(len(cid) == 9 and cid.startswith('c') for cid in ids)
        blob = open(os.path.join(svc.state_dir, 'paa-choices.json')).read()
        assert item.id in blob
        for cid in ids:
            assert item.id not in cid

    # Nine pending rows page at 8 on one message.
    for i in range(7):
        ledger.add_if_new(_item(
            f'finding-extra-{i}', 'beta', f'extra-{i}',
            f'2026-09-{20 - i:02d}T00:00:00+00:00'))
    listed = svc.handle_update({
        'update_id': 2,
        'message': {
            'message_id': 2, 'date': 1, 'text': '/findings',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    })
    keyboard = listed[0].reply_markup['inline_keyboard']
    assert len(keyboard) == 9  # 8 rows + Next
    assert keyboard[-1][0]['text'] == 'Next'

    discussed = svc.handle_update({
        'update_id': 3,
        'message': {
            'message_id': 3, 'date': 1, 'text': '/discuss finding-alpha',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    })
    assert calls[-1]['session_id'] == 'sess-9'
    assert calls[-1]['tool_policy'] == 'plan'
    assert calls[-1]['timeout'] == 600
    prompt = calls[-1]['prompt']
    assert prompt.startswith('DISCUSS FINDING\n\n')
    assert 'OTHER PENDING FINDINGS FOR THIS PROJECT' in prompt
    assert 'alpha also drifts' in prompt
    ledger.load()
    assert prompt == discuss_finding_prompt(
        ledger._items['finding-alpha'], ledger.pending_items())
    assert 'discussed' in discussed[0].text
    assert svc.session_id == 'sess-9'
    assert len([c for c in calls if c['prompt'].startswith('DISCUSS FINDING')]) == 1

    # Discuss button uses the same session and the current policy (ro →
    # read-only plan; there is no armed path anymore).
    button = None
    for msg in noted:
        for row in msg.reply_markup['inline_keyboard']:
            for candidate in row:
                if candidate['text'] == 'Discuss':
                    button = candidate['callback_data']
    # The first notice follows pending order (newest first): finding-alpha.
    assert button
    svc.handle_update({
        'update_id': 5,
        'callback_query': {
            'id': 'cq-discuss',
            'data': button,
            'from': {'id': UID},
            'message': {'message_id': noted[0].message_id, 'chat': {'id': UID}},
        },
    })
    assert calls[-1]['tool_policy'] == 'plan'
    assert calls[-1]['session_id'] == 'sess-9'
    assert calls[-1]['prompt'].startswith('DISCUSS FINDING')
    assert settings.paa_budget_used == 5

    acked = svc.handle_update({
        'update_id': 6,
        'message': {
            'message_id': 6, 'date': 1, 'text': '/ack finding-sibling',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    })
    assert 'Acknowledged finding-sibling' in acked[0].text
    dismissed = svc.handle_update({
        'update_id': 7,
        'message': {
            'message_id': 7, 'date': 1, 'text': '/dismiss finding-extra-0',
            'chat': {'id': UID, 'type': 'private'}, 'from': {'id': UID},
        },
    })
    assert 'Dismissed finding-extra-0' in dismissed[0].text
    fresh = Ledger(ledger._path)
    fresh.load()
    assert fresh._items['finding-sibling'].status == 'acknowledged'
    assert fresh._items['finding-extra-0'].status == 'dismissed'
    assert fresh._items['finding-alpha'].status == 'pending'


def test_maybe_scan_posts_no_notices_by_default(tmp_path):
    mon, ledger, settings, _projects = _monitor(tmp_path, paa_enabled=True)
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps(
        {'allowed_user_ids': [UID]}))
    svc = TurnService(str(state), settings, lambda *a, **k: None,
                      ledger=ledger)
    outs = svc.maybe_scan(mon, now=1_000_000, chat_id=UID, user_id=UID)
    assert outs == []


def test_maybe_scan_uses_the_shared_scan_on_the_interval(tmp_path):
    mon, ledger, settings, _projects = _monitor(tmp_path, paa_enabled=True)
    calls = []
    orig = mon.run_scan

    def wrapped():
        calls.append(1)
        return orig()

    mon.run_scan = wrapped
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'paa-telegram.json').write_text(json.dumps(
        {'allowed_user_ids': [UID], 'findings_notices': True}))

    def chat(*args, **kwargs):
        raise AssertionError('scan must not chat')

    svc = TurnService(str(state), settings, chat, ledger=ledger)
    outs = svc.maybe_scan(mon, now=1_000_000, chat_id=UID, user_id=UID)
    assert calls == [1]
    assert outs
    assert all('alpha' in msg.text for msg in outs)
    again = svc.maybe_scan(mon, now=1_000_100, chat_id=UID, user_id=UID)
    assert again == []
    assert calls == [1]
    settings.paa_enabled = False
    skipped = svc.maybe_scan(mon, now=1_000_000 + 3600, chat_id=UID, user_id=UID)
    assert skipped == []
    assert calls == [1]
