"""Unit tests for paa_choices — ChoiceBook locking + ChoiceWaiter primitive."""
import json
import queue
import threading
import time

import pytest

import paa_choices
from paa_choices import ChoiceBook, waiter


@pytest.fixture
def book(tmp_path):
    return ChoiceBook(str(tmp_path / 'choices.json'))


@pytest.fixture(autouse=True)
def _fresh_waiter_registry():
    saved_w, saved_o = paa_choices._WAITERS, paa_choices._OUTCOMES
    paa_choices._WAITERS, paa_choices._OUTCOMES = {}, {}
    yield
    paa_choices._WAITERS, paa_choices._OUTCOMES = saved_w, saved_o


class FakeClock:
    def __init__(self, t=1_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def _select_button(markup):
    return markup['inline_keyboard'][0][0]['callback_data']


# ---------------------------------------------------------------------------
# existing ChoiceBook behavior (regression guard for the locking refactor)
# ---------------------------------------------------------------------------

class TestChoiceBookBasics:
    def test_create_resolve_select(self, book):
        group, text, markup = book.create(
            chat_id=1, user_id=7, prompt='Approve?', options=[
                {'label': 'Allow once', 'value': 'allow'},
                {'label': 'Keep read-only', 'value': 'deny'},
            ], meaning={'kind': 'approval'})
        cid = _select_button(markup)
        result = book.resolve(cid, user_id=7, allowlist=[7])
        assert result.kind == 'selected'
        assert result.value == 'allow'
        assert result.group == group
        # Second tap answers spent.
        again = book.resolve(cid, user_id=7, allowlist=[7])
        assert again.kind == 'spent'
        # Other button of the spent group answers spent too.
        other = markup['inline_keyboard'][1][0]['callback_data']
        assert book.resolve(other, user_id=7, allowlist=[7]).kind == 'spent'

    def test_foreign_and_unknown(self, book):
        _group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[{'label': 'a',
                                                        'value': 'a'}],
            meaning={})
        cid = _select_button(markup)
        assert book.resolve(cid, user_id=8, allowlist=[7]).kind == 'foreign'
        assert book.resolve(cid, user_id=7, allowlist=[]).kind == 'foreign'
        assert book.resolve('cnosuchid', user_id=7, allowlist=[7]).kind == \
            'unknown'

    def test_paging_and_drill_still_work(self, book):
        options = [{'label': f'o{i}', 'value': f'v{i}'} for i in range(10)]
        _g, _t, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=options, meaning={})
        nav_row = markup['inline_keyboard'][-1]
        assert [b['text'] for b in nav_row] == ['Next']
        nxt = nav_row[0]['callback_data']
        page2 = book.resolve(nxt, user_id=7, allowlist=[7])
        assert page2.kind == 'page'
        assert [b['text'] for b in page2.reply_markup['inline_keyboard'][-1]] \
            == ['Back']

        _g2, _t2, markup2 = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'top', 'value': 'top', 'children': [
                    {'label': 'kid', 'value': 'kid'}]},
            ], meaning={})
        drill = book.resolve(_select_button(markup2), user_id=7,
                             allowlist=[7])
        assert drill.kind == 'drill'
        kid = drill.reply_markup['inline_keyboard'][0][0]['callback_data']
        sel = book.resolve(kid, user_id=7, allowlist=[7])
        assert sel.kind == 'selected'
        assert sel.value == 'kid'

    def test_file_round_trip(self, tmp_path):
        path = str(tmp_path / 'choices.json')
        book = ChoiceBook(path)
        book.create(chat_id=1, user_id=7, prompt='?', options=[
            {'label': 'a', 'value': 'a'}], meaning={})
        reloaded = ChoiceBook(path)
        assert set(reloaded.callback_ids()) == set(book.callback_ids())
        with open(path) as fh:
            data = json.load(fh)
        assert isinstance(data['buttons'], dict)


# ---------------------------------------------------------------------------
# ChoiceWaiter
# ---------------------------------------------------------------------------

class TestWaiter:
    def test_waiter_blocks_until_resolve(self, book):
        group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'Allow', 'value': 'allow'}], meaning={})
        handle = waiter(group)
        cid = _select_button(markup)
        threading.Timer(
            0.05, book.resolve, args=(cid,),
            kwargs={'user_id': 7, 'allowlist': [7]}).start()
        assert handle.wait(timeout=5) == 'allow'
        assert handle.expired is False

    def test_waiter_timeout_returns_none_not_expired(self, book):
        group, _text, _markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'a'}], meaning={})
        handle = waiter(group)
        start = time.monotonic()
        assert handle.wait(timeout=0.05) is None
        assert handle.expired is False
        assert time.monotonic() - start >= 0.04

    def test_waiter_settled_by_ttl_expiry(self, tmp_path):
        clock = FakeClock()
        book = ChoiceBook(str(tmp_path / 'c.json'), now_fn=clock,
                          ttl_sec=10)
        group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'a'}], meaning={})
        handle = waiter(group)
        clock.t += 11  # past ttl with no tap: only resolve observes it
        result = book.resolve(_select_button(markup), user_id=7,
                              allowlist=[7])
        assert result.kind == 'expired'
        assert handle.wait(timeout=5) is None
        assert handle.expired is True

    def test_waiter_settled_by_expire_group(self, book):
        group, _text, _markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'a'}], meaning={})
        handle = waiter(group)
        book.expire_group(group)
        assert handle.wait(timeout=5) is None
        assert handle.expired is True

    def test_late_waiter_sees_recorded_outcome(self, book):
        group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'picked'}], meaning={})
        book.resolve(_select_button(markup), user_id=7, allowlist=[7])
        handle = waiter(group)  # registered only after the decision
        assert handle.settled is True
        assert handle.wait(timeout=0) == 'picked'

    def test_late_waiter_sees_recorded_expiry(self, book):
        group, _text, _markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'a'}], meaning={})
        book.expire_group(group)
        handle = waiter(group)
        assert handle.wait(timeout=0) is None
        assert handle.expired is True

    def test_foreign_tap_does_not_settle(self, book):
        group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'a'}], meaning={})
        handle = waiter(group)
        assert book.resolve(_select_button(markup), user_id=8,
                            allowlist=[7]).kind == 'foreign'
        assert handle.settled is False
        book.expire_group(group)
        assert handle.expired is True

    def test_waiter_is_one_shot_and_first_outcome_wins(self, book):
        group, _text, markup = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'first'}], meaning={})
        handle = waiter(group)
        cid = _select_button(markup)
        book.resolve(cid, user_id=7, allowlist=[7])
        book.expire_group(group)
        assert handle.wait(timeout=0) == 'first'
        assert handle.expired is False


class TestGroupOutcome:
    def test_pending_selected_expired(self, tmp_path):
        clock = FakeClock()
        book = ChoiceBook(str(tmp_path / 'c.json'), now_fn=clock,
                          ttl_sec=10)
        g1, _t, m1 = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'a', 'value': 'va'}], meaning={})
        g2, _t2, m2 = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'b', 'value': 'vb'}], meaning={})
        g3, _t3, _m3 = book.create(
            chat_id=1, user_id=7, prompt='?', options=[
                {'label': 'c', 'value': 'vc'}], meaning={})
        assert book.group_outcome(g1) is None
        book.resolve(_select_button(m1), user_id=7, allowlist=[7])
        assert book.group_outcome(g1) == ('selected', 'va')
        book.expire_group(g2)
        assert book.group_outcome(g2) == ('expired', None)
        assert book.group_outcome(g3) is None
        clock.t += 11
        assert book.group_outcome(g3) == ('expired', None)
        assert book.group_outcome('cnonexistent') is None


# ---------------------------------------------------------------------------
# Concurrency: broker creates while poll thread resolves
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_create_and_resolve_from_two_threads(self, book):
        iterations = 60
        created = queue.Queue()
        stop = object()
        errors = []

        def creator():
            for i in range(iterations):
                group, _text, markup = book.create(
                    chat_id=1, user_id=7, prompt=f'q{i}',
                    options=[{'label': f'a{i}', 'value': f'v{i}'}],
                    meaning={})
                created.put((group, _select_button(markup)))

        def resolver():
            while True:
                item = created.get()
                if item is stop:
                    return
                group, cid = item
                try:
                    result = book.resolve(cid, user_id=7, allowlist=[7])
                    assert result.kind == 'selected', result.kind
                    assert result.group == group
                    assert result.value is not None
                except Exception as exc:  # noqa: BLE001 — collect, re-raise
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=creator)]
        threads += [threading.Thread(target=resolver) for _ in range(3)]
        for t in threads:
            t.start()
        creator_seen = threads[0]
        for _ in range(iterations):
            pass
        creator_seen.join(timeout=15)
        assert not creator_seen.is_alive()
        for _ in range(3):
            created.put(stop)
        for t in threads[1:]:
            t.join(timeout=15)
            assert not t.is_alive()
        assert errors == []

        # Store is internally consistent and the file parses.
        rows = [json.loads(json.dumps(b)) for b in book._buttons.values()]
        assert len(rows) == iterations
        assert sum(1 for b in book._buttons.values() if b.get('chosen')) \
            == iterations
        with open(book.path) as fh:
            data = json.load(fh)
        assert set(data['buttons']) == set(book.callback_ids())

    def test_simultaneous_create_expire_resolve(self, book):
        """A resolver racing group replacement never corrupts the store."""
        errors = []
        barrier = threading.Barrier(4)

        def churn(n):
            try:
                barrier.wait(timeout=10)
                for i in range(40):
                    group, _t, markup = book.create(
                        chat_id=1, user_id=7, prompt=f'{n}:{i}',
                        options=[{'label': 'a', 'value': f'{n}:{i}'}],
                        meaning={})
                    if i % 2:
                        book.expire_group(group)
                    book.resolve(_select_button(markup), user_id=7,
                                 allowlist=[7])
                    book.bind_message(group, 1000 + i)
                    book.group_message_id(group)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=churn, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert errors == []
        with open(book.path) as fh:
            data = json.load(fh)
        assert set(data['buttons']) == set(book.callback_ids())
