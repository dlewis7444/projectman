import os
import json
import time
import pytest
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib
from settings import Settings
from model import ProjectStore, ProjectsWatcher, StatusWatcher, Project


def test_project_store_uses_settings_dir(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'myproject').mkdir()
    projects = store.load_projects()
    assert any(p.name == 'myproject' for p in projects)


def test_project_store_excludes_dotfiles(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / '.archive').mkdir()
    (tmp_path / 'realproject').mkdir()
    projects = store.load_projects()
    names = [p.name for p in projects]
    assert 'realproject' in names
    assert '.archive' not in names


def test_project_store_archive_uses_settings_dir(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    proj_dir = tmp_path / 'myproject'
    proj_dir.mkdir()
    projects = store.load_projects()
    store.archive(projects[0])
    archive_path = tmp_path / '.archive' / 'myproject'
    assert archive_path.exists()


def test_project_store_restore(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    archive_dir = tmp_path / '.archive'
    archive_dir.mkdir()
    (archive_dir / 'myproject').mkdir()
    archived = store.load_archived()
    store.restore(archived[0])
    assert (tmp_path / 'myproject').exists()
    assert not (archive_dir / 'myproject').exists()


def test_projects_watcher_start_accepts_path(tmp_path):
    # ProjectsWatcher.start() must accept an explicit path argument
    watcher = ProjectsWatcher()
    watcher.start(str(tmp_path))  # must not raise
    watcher._monitor.cancel()


def test_projects_watcher_restart(tmp_path):
    watcher = ProjectsWatcher()
    watcher.start(str(tmp_path))
    new_path = str(tmp_path)
    watcher.restart(new_path)   # must not raise; monitor should still be active
    watcher._monitor.cancel()


# --- StatusWatcher tests ---

def test_status_watcher_initial_status_is_empty_dict():
    w = StatusWatcher()
    assert w._status == {}


def test_status_watcher_reload_empty_dir(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    w = StatusWatcher()
    w._reload()
    assert w._status == {}


def test_status_watcher_reload_valid_file(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/myproject'
    (tmp_path / 'tmp-myproject.json').write_text(json.dumps({
        'state': 'working',
        'event': 'PreToolUse',
        'cwd': proj_path,
        'ts': 1000,
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    key = os.path.realpath(proj_path)
    assert key in w._status
    assert w._status[key].state == 'working'


def test_status_watcher_reload_invalid_json_skipped(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    (tmp_path / 'bad.json').write_text('not json')
    w = StatusWatcher()
    w._reload()   # must not raise
    assert w._status == {}


def test_status_watcher_reload_missing_cwd_skipped(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    (tmp_path / 'no-cwd.json').write_text(json.dumps({'state': 'done', 'cwd': ''}))
    w = StatusWatcher()
    w._reload()
    assert w._status == {}


def test_status_watcher_reload_old_format_defaults_state_to_done(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/oldproject'
    (tmp_path / 'tmp-oldproject.json').write_text(json.dumps({
        'event': 'Stop',    # no 'state' field — old format
        'cwd': proj_path,
        'ts': 1000,
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    key = os.path.realpath(proj_path)
    assert w._status[key].state == 'done'


def test_status_watcher_get_project_status_returns_snapshot_state(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_dir = tmp_path / 'myproject'
    proj_dir.mkdir()
    proj_path = str(proj_dir)
    slug = proj_path.replace('/', '-').replace('.', '-').lstrip('-')
    (tmp_path / f'{slug}.json').write_text(json.dumps({
        'state': 'waiting',
        'event': 'Notification',
        'cwd': proj_path,
        'ts': int(time.time()),
        'session': 'abc',
    }))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='myproject', path=os.path.realpath(proj_path))
    assert w.get_project_status(proj) == 'waiting'


def test_status_watcher_get_project_status_returns_idle_when_not_found(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='missing', path='/no/such/project')
    assert w.get_project_status(proj) == 'idle'


def test_status_watcher_reload_replaces_dict_atomically(tmp_path, monkeypatch):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/atomicproject'
    (tmp_path / 'tmp-atomicproject.json').write_text(json.dumps({
        'state': 'working', 'event': 'PreToolUse',
        'cwd': proj_path, 'ts': 1000, 'session': 'x',
    }))
    w = StatusWatcher()
    w._reload()
    # Remove the file and reload — entry should disappear
    (tmp_path / 'tmp-atomicproject.json').unlink()
    w._reload()
    assert os.path.realpath(proj_path) not in w._status


def test_status_watcher_worktree_files_do_not_affect_parent_with_no_main(tmp_path, monkeypatch):
    """Worktree status files belong to a separate cwd and must not roll up
    to the parent project's dot. A project whose main session has cleanly
    exited (no main status file) must show as idle even if its worktrees
    left stale 'working' status files behind from non-graceful exits.

    This is the dex-arbitrage-bot stuck-yellow case."""
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/parentproj'
    # Three stale worktree status files, increasing ts; no main file
    for i, ts in enumerate((1000, 2000, 3000)):
        (tmp_path / f'worktree-phase{i}.json').write_text(json.dumps({
            'state': 'working', 'event': 'PreToolUse',
            'cwd': f'{proj_path}/.worktrees/phase{i}',
            'ts': ts, 'session': f's{i}', 'tool': 'Bash',
        }))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='parentproj', path=os.path.realpath(proj_path))
    assert w.get_project_status(proj) == 'idle'


def test_status_watcher_parent_state_independent_of_worktrees(tmp_path, monkeypatch):
    """When a project has its own main status file, that file is authoritative
    for the parent dot. Worktree status files in subdirectories — even if
    newer and in a 'working' state — must not influence it."""
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    proj_path = '/tmp/maincoexist'
    # Stale-but-newer worktree saying 'working'
    (tmp_path / 'worktree.json').write_text(json.dumps({
        'state': 'working', 'event': 'PreToolUse',
        'cwd': proj_path + '/.worktrees/phase1',
        'ts': 9999, 'session': 'x', 'tool': 'Bash',
    }))
    # Older main with 'done' — must still win the parent dot
    (tmp_path / 'main.json').write_text(json.dumps({
        'state': 'done', 'event': 'Stop',
        'cwd': proj_path, 'ts': 5000, 'session': 'y',
    }))
    w = StatusWatcher()
    w._reload()
    proj = Project(name='maincoexist', path=os.path.realpath(proj_path))
    assert w.get_project_status(proj) == 'done'


# ── StatusWatcher dual-watch (P2/B2, Decision 2) ──────────────────────────────
# The watcher reads BOTH the new ~/.ProjectMan/status dir and the legacy
# ~/.claude/projectman/status dir during the deprecation window. The conftest
# autouse fixture points LEGACY_STATUS_DIR at a non-existent temp path; these
# tests override both dirs explicitly to exercise the merge.

def _write_status(dirpath, fname, cwd, state, ts, session='s'):
    (dirpath / fname).write_text(json.dumps({
        'state': state, 'event': 'X', 'cwd': cwd, 'ts': ts, 'session': session,
    }))


def test_dual_watch_reads_legacy_dir(tmp_path, monkeypatch):
    """A status file ONLY in the legacy dir still lights the dot (unmigrated
    hook.js keeps working through the deprecation window)."""
    import model
    new_dir = tmp_path / 'new'
    legacy_dir = tmp_path / 'legacy'
    new_dir.mkdir()
    legacy_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(legacy_dir))
    _write_status(legacy_dir, 'p.json', '/tmp/dualproj', 'working', 100)
    w = StatusWatcher()
    w._reload()
    assert w._status[os.path.realpath('/tmp/dualproj')].state == 'working'


def test_dual_watch_reads_new_dir(tmp_path, monkeypatch):
    import model
    new_dir = tmp_path / 'new'
    legacy_dir = tmp_path / 'legacy'
    new_dir.mkdir()
    legacy_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(legacy_dir))
    _write_status(new_dir, 'p.json', '/tmp/dualproj2', 'waiting', 100)
    w = StatusWatcher()
    w._reload()
    assert w._status[os.path.realpath('/tmp/dualproj2')].state == 'waiting'


def test_dual_watch_same_cwd_newer_ts_wins(tmp_path, monkeypatch):
    """The same cwd in both dirs → the snapshot with the newer ts wins,
    regardless of which dir it lives in."""
    import model
    new_dir = tmp_path / 'new'
    legacy_dir = tmp_path / 'legacy'
    new_dir.mkdir()
    legacy_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(legacy_dir))
    key = os.path.realpath('/tmp/dualboth')
    # Legacy is NEWER here — it must win even though new dir is scanned last.
    _write_status(legacy_dir, 'p.json', '/tmp/dualboth', 'working', ts=200)
    _write_status(new_dir, 'p.json', '/tmp/dualboth', 'done', ts=100)
    w = StatusWatcher()
    w._reload()
    assert w._status[key].state == 'working'
    # Flip: new dir newer → new wins.
    _write_status(new_dir, 'p.json', '/tmp/dualboth', 'done', ts=300)
    w._reload()
    assert w._status[key].state == 'done'


def test_dual_watch_same_cwd_tie_new_dir_wins(tmp_path, monkeypatch):
    """On an exact ts tie the NEW dir supersedes (the migration direction)."""
    import model
    new_dir = tmp_path / 'new'
    legacy_dir = tmp_path / 'legacy'
    new_dir.mkdir()
    legacy_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(legacy_dir))
    key = os.path.realpath('/tmp/dualtie')
    _write_status(legacy_dir, 'p.json', '/tmp/dualtie', 'working', ts=500)
    _write_status(new_dir, 'p.json', '/tmp/dualtie', 'done', ts=500)
    w = StatusWatcher()
    w._reload()
    assert w._status[key].state == 'done'


def test_dual_watch_missing_legacy_dir_not_fatal(tmp_path, monkeypatch):
    """Legacy dir absent → the new dir alone still publishes status."""
    import model
    new_dir = tmp_path / 'new'
    new_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(tmp_path / 'gone'))
    _write_status(new_dir, 'p.json', '/tmp/dualsolo', 'done', 100)
    w = StatusWatcher()
    w._reload()
    assert w._status[os.path.realpath('/tmp/dualsolo')].state == 'done'


def test_dual_watch_both_dirs_gone_keeps_previous(tmp_path, monkeypatch):
    """Both dirs unreadable → keep the previous status (no wipe)."""
    import model
    new_dir = tmp_path / 'new'
    new_dir.mkdir()
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(tmp_path / 'gone1'))
    _write_status(new_dir, 'p.json', '/tmp/dualkeep', 'done', 100)
    w = StatusWatcher()
    w._reload()
    key = os.path.realpath('/tmp/dualkeep')
    assert key in w._status
    # Now both dirs vanish.
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path / 'gone2'))
    w._reload()
    assert key in w._status   # previous status retained


def test_status_watcher_start_monitors_both_dirs(tmp_path, monkeypatch):
    """start() registers one inotify monitor per dir (new + legacy)."""
    import model
    new_dir = tmp_path / 'new'
    legacy_dir = tmp_path / 'legacy'
    monkeypatch.setattr(model, 'STATUS_DIR', str(new_dir))
    monkeypatch.setattr(model, 'LEGACY_STATUS_DIR', str(legacy_dir))
    w = StatusWatcher()
    w.start()
    assert len(w._monitors) == 2
    assert new_dir.is_dir() and legacy_dir.is_dir()   # created if missing
    for m in w._monitors:
        m.cancel()


# ── StatusWatcher phase aging — F11 working→waiting promotion ─────────────────
# The grok bridge phase-stamps `pre_tool_use` (grok's wire goes silent under a
# permission prompt — mini-probe); the watcher promotes working→waiting once
# the phase ages past model.PHASE_WAITING_THRESHOLD. Injected clock + scheduler
# make the aging deterministic. Snapshots WITHOUT phase fields must behave
# byte-identically to the pre-F11 watcher (claude/opencode regression pins).

def _write_phase_status(dirpath, fname, cwd, state, ts, *, phase=None,
                        phase_ts=None, session='s'):
    data = {'state': state, 'event': 'pre_tool_use', 'cwd': cwd, 'ts': ts,
            'session': session}
    if phase is not None:
        data['phase'] = phase
    if phase_ts is not None:
        data['phase_ts'] = phase_ts
    (dirpath / fname).write_text(json.dumps(data))


class _FakeScheduler:
    """Captures (seconds, callback) pairs; tests fire callbacks manually."""
    def __init__(self):
        self.calls = []

    def __call__(self, seconds, callback):
        self.calls.append((seconds, callback))


def _aging_watcher(tmp_path, monkeypatch, *, now):
    import model
    monkeypatch.setattr(model, 'STATUS_DIR', str(tmp_path))
    clock = {'now': now}
    sched = _FakeScheduler()
    w = StatusWatcher(now_fn=lambda: clock['now'], schedule_fn=sched)
    return w, clock, sched


def test_phase_aged_past_threshold_promotes_to_waiting(tmp_path, monkeypatch):
    """T-F11: working + phase=pre_tool_use older than the threshold reads as
    'waiting' (the silent permission prompt inference)."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=1010.0)  # age 10s
    w._reload()
    assert w.get_project_status(proj) == 'waiting'
    # Already past the threshold → nothing to arm.
    assert sched.calls == []


def test_phase_at_exact_threshold_promotes(tmp_path, monkeypatch):
    """Promotion semantics are age >= threshold ('ages past 5s')."""
    import model
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(
        tmp_path, monkeypatch, now=1000.0 + model.PHASE_WAITING_THRESHOLD)
    w._reload()
    assert w.get_project_status(proj) == 'waiting'


def test_phase_fresh_stays_working_and_arms_timer(tmp_path, monkeypatch):
    """An un-aged phase reads 'working' AND arms a one-shot re-emit for the
    threshold crossing; firing it after the clock passes the threshold
    re-publishes and the status reads 'waiting'."""
    import model
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=1002.0)  # age 2s
    emits = []
    w.connect('status-changed', lambda *_: emits.append(1))
    w._reload()
    assert w.get_project_status(proj) == 'working'   # not aged yet
    assert len(sched.calls) == 1
    delay, callback = sched.calls[0]
    # remaining = threshold - age = 3s, plus the small cushion.
    assert 3.0 <= delay <= 3.2
    emits_before = len(emits)
    # The clock crosses the threshold; the timer fires.
    clock['now'] = 1000.0 + model.PHASE_WAITING_THRESHOLD + 0.1
    assert callback() is False                       # one-shot
    assert len(emits) == emits_before + 1            # re-emitted
    assert w.get_project_status(proj) == 'waiting'   # now promoted
    # The re-publish armed nothing further (already past threshold).
    assert len(sched.calls) == 1


def test_phase_cleared_file_reads_working_no_timer(tmp_path, monkeypatch):
    """A phase-less rewrite (post_tool_use's clear) reads working; no timer."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=1002.0)
    w._reload()
    assert len(sched.calls) == 1
    # The bridge clears the phase (tool completed) before the threshold.
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1003)
    w._reload()
    clock['now'] = 1020.0   # way past what WOULD have been the threshold
    assert w.get_project_status(proj) == 'working'   # no phase → no promotion
    assert len(sched.calls) == 1                     # no new timer armed


def test_phase_stale_timer_dies_silently_on_newer_publish(tmp_path, monkeypatch):
    """Generation guard: a timer armed before a newer publish must not emit."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=1002.0)
    w._reload()
    stale_delay, stale_cb = sched.calls[0]
    # A newer publish supersedes (file cleared, fresh _reload bumps the gen).
    _write_phase_status(tmp_path, 'p.json', proj.path, 'working', 1003)
    w._reload()
    emits = []
    w.connect('status-changed', lambda *_: emits.append(1))
    clock['now'] = 1010.0
    assert stale_cb() is False
    assert emits == []   # stale generation: died without emitting


def test_phase_promotion_only_for_working_state(tmp_path, monkeypatch):
    """A stray phase on a non-working snapshot never promotes (done stays
    done) and arms nothing."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    _write_phase_status(tmp_path, 'p.json', proj.path, 'done', 1000,
                        phase='pre_tool_use', phase_ts=1000)
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=1020.0)
    w._reload()
    assert w.get_project_status(proj) == 'done'
    assert sched.calls == []


def test_no_phase_snapshot_byte_identical_behavior(tmp_path, monkeypatch):
    """REGRESSION PIN (claude/opencode): snapshots without phase fields keep
    the dataclass defaults, never promote, and never arm a timer — for every
    state the other bridges write."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=99999.0)
    for state in ('working', 'waiting', 'done'):
        _write_phase_status(tmp_path, 'p.json', proj.path, state, 1000)
        w._reload()
        snap = w._status[proj.path]
        assert snap.phase is None and snap.phase_ts == 0   # defaults
        assert w.get_project_status(proj) == state          # passthrough
    assert sched.calls == []                                # never armed


def test_phase_garbage_phase_ts_tolerated(tmp_path, monkeypatch):
    """A non-numeric phase_ts (a corrupt writer) parses to 0 → no promotion,
    no timer, no crash."""
    proj_dir = tmp_path / 'p'
    proj_dir.mkdir()
    proj = Project(name='p', path=os.path.realpath(str(proj_dir)))
    (tmp_path / 'p.json').write_text(json.dumps({
        'state': 'working', 'event': 'pre_tool_use', 'cwd': proj.path,
        'ts': 1000, 'session': 's', 'phase': 'pre_tool_use',
        'phase_ts': 'not-a-number',
    }))
    w, clock, sched = _aging_watcher(tmp_path, monkeypatch, now=2000.0)
    w._reload()
    assert w.get_project_status(proj) == 'working'
    assert sched.calls == []
