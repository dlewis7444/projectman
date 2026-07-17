"""Groups load/save gating + remote apply/push orchestration (no GTK).

AppWindow is GTK-heavy; status/prune gates and the stale-apply / push-coalesce
state machine live as pure functions in ``project_groups`` and are wired from
``window._persist_groups`` / ``window._apply_remote_refresh`` /
``window._on_groups_push_done``.
"""
from __future__ import annotations

import pytest

from project_groups import (
    GroupForest,
    GroupNode,
    empty_forest,
    groups_push_complete,
    groups_push_schedule,
    groups_status_allows_save,
    prune_unknown_projects,
    should_apply_remote_groups_fetch,
    should_prune_membership,
)


# ── B1: groups_status_allows_save ─────────────────────────────────────────────


@pytest.mark.parametrize(
    'status,is_remote,expected',
    [
        ('ok', True, True),
        ('ok', False, True),
        ('missing', True, True),
        ('missing', False, True),
        ('pending', True, False),
        ('pending', False, False),
        ('error', True, False),
        ('error', False, False),
        ('invalid', True, False),
        ('invalid', False, False),
        (None, True, False),   # remote fail-closed
        (None, False, True),   # localhost: missing key after init rare
        ('unknown', True, False),
        ('unknown', False, False),
    ],
)
def test_groups_status_allows_save(status, is_remote, expected):
    assert groups_status_allows_save(status, is_remote=is_remote) is expected


def test_soft_missing_and_ok_both_writable():
    """After successful fetch, both ok and missing allow first save (B1)."""
    assert groups_status_allows_save('ok', is_remote=True)
    assert groups_status_allows_save('missing', is_remote=True)
    assert not groups_status_allows_save('pending', is_remote=True)


# ── B2: should_prune_membership ───────────────────────────────────────────────


def test_should_prune_membership_only_when_trusted():
    assert should_prune_membership(True) is True
    assert should_prune_membership(False) is False
    assert should_prune_membership(0) is False
    assert should_prune_membership(1) is True


def test_prune_path_respects_trust_flag():
    """Document apply-path policy: prune only when projects_trusted.

    Health refresh must never auto-push after prune (no _persist_groups).
    """
    forest = GroupForest(
        groups={'g1': GroupNode(id='g1', name='G', parent_id=None)},
        membership={
            'ssh:h:keep': 'g1',
            'ssh:h:gone': 'g1',
        },
    )
    known = {'ssh:h:keep'}

    # Untrusted: do not prune (would wipe membership against empty-after-error).
    if should_prune_membership(False):
        prune_unknown_projects(forest, known)
    assert 'ssh:h:gone' in forest.membership

    # Trusted: prune in memory only.
    if should_prune_membership(True):
        n = prune_unknown_projects(forest, known)
        assert n == 1
    assert 'ssh:h:gone' not in forest.membership
    assert forest.membership == {'ssh:h:keep': 'g1'}


# ── Lightweight stand-ins for _persist_groups / _ensure_group_forest policy ──


def _persist_groups_logic(load_status, host_id, *, is_remote, forest_present=True):
    """Mirror window._persist_groups gate without GTK.

    Returns ('refused', reason) or ('allowed',).
    """
    if not forest_present:
        return ('refused', 'no-forest')
    status = load_status.get(host_id)  # no default 'ok'
    if not groups_status_allows_save(status, is_remote=is_remote):
        if is_remote and status in (None, 'pending'):
            return ('refused', 'not-loaded')
        return ('refused', 'load-failed')
    return ('allowed',)


def test_persist_refuses_pending_error_invalid_remote_none():
    for status, reason in (
        ('pending', 'not-loaded'),
        (None, 'not-loaded'),
        ('error', 'load-failed'),
        ('invalid', 'load-failed'),
    ):
        out = _persist_groups_logic({'h': status} if status is not None else {},
                                    'h', is_remote=True)
        if status is None:
            out = _persist_groups_logic({}, 'h', is_remote=True)
        assert out[0] == 'refused', status
        assert out[1] == reason, (status, out)


def test_persist_allows_ok_and_missing_remote():
    for status in ('ok', 'missing'):
        assert _persist_groups_logic({'h': status}, 'h', is_remote=True) == (
            'allowed',
        )


def test_persist_localhost_none_allowed_ok_missing():
    assert _persist_groups_logic({}, 'localhost', is_remote=False) == ('allowed',)
    assert _persist_groups_logic({'localhost': 'ok'}, 'localhost',
                                 is_remote=False) == ('allowed',)
    assert _persist_groups_logic({'localhost': 'missing'}, 'localhost',
                                 is_remote=False) == ('allowed',)
    assert _persist_groups_logic({'localhost': 'error'}, 'localhost',
                                 is_remote=False)[0] == 'refused'


def test_ensure_remote_first_touch_is_pending_not_missing():
    """B1: _ensure_group_forest must not mark never-fetched remote as missing."""
    load_status = {}
    host_id = 'remote1'
    is_localhost = False
    # Mirror ensure policy:
    if host_id not in load_status:
        if is_localhost:
            load_status.setdefault(host_id, 'missing')
        else:
            load_status[host_id] = 'pending'
    assert load_status[host_id] == 'pending'
    assert not groups_status_allows_save(
        load_status[host_id], is_remote=True)


def test_apply_refresh_never_auto_pushes_after_prune():
    """B2 policy: health apply prunes in memory when trusted; never push."""
    calls = []

    def fake_persist(hid):
        calls.append(hid)

    projects_trusted = True
    forest = empty_forest()
    forest.membership['ssh:h:dead'] = 'g1'
    known = set()
    if should_prune_membership(projects_trusted):
        prune_unknown_projects(forest, known)
    # Deliberately no fake_persist — health path must not call it for prune.
    assert forest.membership == {}
    assert calls == []

    # Untrusted empty list must not prune.
    forest.membership['ssh:h:alive'] = 'g1'
    if should_prune_membership(False):
        prune_unknown_projects(forest, set())
        fake_persist('h')
    assert forest.membership == {'ssh:h:alive': 'g1'}
    assert calls == []


def test_dirty_flag_skips_forest_overwrite():
    """On failed push, dirty hosts skip fetch clobber in apply refresh."""
    dirty = {'remote1'}
    hid = 'remote1'
    prev = GroupForest(
        groups={'local': GroupNode(id='local', name='LocalEdit', parent_id=None)},
        membership={},
    )
    incoming = empty_forest()
    if not should_apply_remote_groups_fetch(
        dirty=hid in dirty,
        push_inflight=False,
        local_write_gen=0,
        fetch_write_gen=0,
    ):
        applied = prev
    else:
        applied = incoming
    assert applied is prev
    assert 'local' in applied.groups


# ── Stale-apply race (write generation) ───────────────────────────────────────


def test_should_apply_skips_when_dirty_or_inflight():
    assert not should_apply_remote_groups_fetch(
        dirty=True, push_inflight=False, local_write_gen=0, fetch_write_gen=0,
    )
    assert not should_apply_remote_groups_fetch(
        dirty=False, push_inflight=True, local_write_gen=0, fetch_write_gen=0,
    )
    assert should_apply_remote_groups_fetch(
        dirty=False, push_inflight=False, local_write_gen=0, fetch_write_gen=0,
    )


def test_should_apply_skips_stale_fetch_after_local_mutation():
    """Fetch started at gen 0 must not clobber after user mutated (gen 1).

    This is the success-then-stale-apply race: push cleared dirty, but apply
    still carries pre-mutation remote content.
    """
    assert not should_apply_remote_groups_fetch(
        dirty=False,
        push_inflight=False,
        local_write_gen=1,
        fetch_write_gen=0,
    )
    # Same gen: fetch is not stale.
    assert should_apply_remote_groups_fetch(
        dirty=False,
        push_inflight=False,
        local_write_gen=1,
        fetch_write_gen=1,
    )


def test_stale_apply_race_simulation():
    """End-to-end policy: mutate+push success then stale fetch must keep local."""
    write_gen = {'h': 0}
    dirty = set()
    inflight = set()
    pending = set()
    forests = {
        'h': GroupForest(
            groups={'g1': GroupNode(id='g1', name='Original', parent_id=None)},
            membership={},
        ),
    }

    # Health fetch starts (snapshot gen).
    fetch_gen = write_gen['h']

    # User renames group and persists (async push path).
    write_gen['h'] += 1
    dirty.add('h')
    assert groups_push_schedule(inflight, pending, 'h') == 'start'
    forests['h'] = GroupForest(
        groups={'g1': GroupNode(id='g1', name='Renamed', parent_id=None)},
        membership={},
    )
    # Push succeeds for started gen.
    started = write_gen['h']
    action = groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=started, current_gen=write_gen['h'],
    )
    assert action == 'idle'
    assert 'h' not in dirty

    # Stale fetch apply (write_gen at start was 0).
    if should_apply_remote_groups_fetch(
        dirty='h' in dirty,
        push_inflight='h' in inflight,
        local_write_gen=write_gen['h'],
        fetch_write_gen=fetch_gen,
    ):
        forests['h'] = empty_forest()  # would clobber
    assert 'g1' in forests['h'].groups
    assert forests['h'].groups['g1'].name == 'Renamed'


def test_inflight_push_blocks_apply_even_same_gen():
    """Fetch that starts after gen bump but before push ACK must not apply."""
    write_gen = 1
    fetch_gen = 1  # started after mutation
    assert not should_apply_remote_groups_fetch(
        dirty=True,  # dirty during inflight
        push_inflight=True,
        local_write_gen=write_gen,
        fetch_write_gen=fetch_gen,
    )


# ── Push schedule / coalesce ──────────────────────────────────────────────────


def test_groups_push_schedule_coalesces_while_inflight():
    inflight = set()
    pending = set()
    assert groups_push_schedule(inflight, pending, 'h') == 'start'
    assert inflight == {'h'}
    assert groups_push_schedule(inflight, pending, 'h') == 'queue'
    assert groups_push_schedule(inflight, pending, 'h') == 'queue'
    assert pending == {'h'}
    assert inflight == {'h'}


def test_groups_push_complete_success_clears_dirty():
    inflight = {'h'}
    pending = set()
    dirty = {'h'}
    assert groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=1, current_gen=1,
    ) == 'idle'
    assert inflight == set()
    assert dirty == set()


def test_groups_push_complete_failure_keeps_dirty():
    inflight = {'h'}
    pending = set()
    dirty = set()
    assert groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=False, started_gen=1, current_gen=1,
    ) == 'idle'
    assert 'h' in dirty
    assert inflight == set()


def test_groups_push_complete_pending_retries():
    inflight = {'h'}
    pending = {'h'}
    dirty = {'h'}
    assert groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=1, current_gen=2,
    ) == 'retry'
    assert 'h' in dirty  # stay dirty until final ACK
    assert pending == set()
    assert inflight == set()  # caller re-schedules into inflight


def test_groups_push_complete_gen_advanced_without_pending_retries():
    """Safety net: gen advanced even if pending was cleared still retries."""
    inflight = {'h'}
    pending = set()
    dirty = {'h'}
    assert groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=1, current_gen=3,
    ) == 'retry'
    assert 'h' in dirty


def test_coalesce_sequence_two_mutates_one_retry():
    """Expand spam: first push in flight, second queues, complete → one retry."""
    inflight = set()
    pending = set()
    dirty = set()
    write_gen = 0

    # First expand
    write_gen += 1
    dirty.add('h')
    assert groups_push_schedule(inflight, pending, 'h') == 'start'
    started_1 = write_gen

    # Rapid second expand while inflight
    write_gen += 1
    dirty.add('h')
    assert groups_push_schedule(inflight, pending, 'h') == 'queue'
    assert pending == {'h'}

    # First worker finishes (snapshot was gen 1; current is 2)
    action = groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=started_1, current_gen=write_gen,
    )
    assert action == 'retry'
    assert groups_push_schedule(inflight, pending, 'h') == 'start'
    started_2 = write_gen
    action2 = groups_push_complete(
        inflight, pending, dirty,
        host_id='h', ok=True, started_gen=started_2, current_gen=write_gen,
    )
    assert action2 == 'idle'
    assert dirty == set()
    assert inflight == set()
    assert pending == set()
