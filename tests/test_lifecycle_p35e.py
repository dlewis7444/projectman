"""P3.5e Commit 2 — LIFECYCLE & FAILURE (items 9, 10, 11, 12).

Window/app glue tested UNBOUND against SimpleNamespace recorders (the
test_lifecycle.py pattern); the pure decision lives in session.py and is tested
directly. The spawn-path zellij kill (item 11) and the late-death pane feed
(item 10) that need a real Vte terminal are exercised display-gated in
test_terminal_zellij.py / test_terminal_agent.py; here the window-side branching
and the shared zellij.kill_session helper are covered headless.

Binding tests:
  FB-2  should_save_session decision table; _save_session guard skips/saves
  FB-3  _handle_late_death feeds the code; active-death drops the filter,
        background-death does not
  FB-4  zellij.kill_session only kills a live session; spawn-path decision
  FB-10 the spawn-failure toast call pins timeout=0
"""
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import GLib  # noqa: F401

from window import AppWindow
from session import (
    should_save_session, save_session, load_session, SESSION_FILE,  # noqa: F401
)


# ════════════════════════════════════════════════════════════════════════════
# FB-2 — the session-erasure guard (power #3/#8, C7/C8)
# ════════════════════════════════════════════════════════════════════════════

def test_should_save_session_decision_table():
    """BINDING (FB-2): SAVE when something started OR the existing session is
    empty; SKIP only when nothing started AND the existing session has paths."""
    # Something started → always save (incl. deliberate close-everything).
    assert should_save_session(True, []) is True
    assert should_save_session(True, ['/a']) is True
    # Nothing started + existing empty → save (nothing to lose; fresh install).
    assert should_save_session(False, []) is True
    # Nothing started + existing has paths → SKIP (preserve last good session).
    assert should_save_session(False, ['/a', '/b']) is False


def _save_fake(tmp_path, *, any_started, terminals=None):
    """A duck-typed AppWindow self for _save_session, pointed at a temp session
    file via monkeypatched SESSION_FILE (set by the caller)."""
    s = types.SimpleNamespace(resume_projects=True, effective_agent=lambda p: 'claude')
    fake = types.SimpleNamespace(
        _settings=s,
        _terminals=terminals or {},
        _active_path=None,
        _any_started_this_run=any_started,
    )
    return fake


def _live_term(agent='claude'):
    t = types.SimpleNamespace(_child_pid=4242)
    t.spawned_agent_signature = lambda: agent
    return t


def test_save_session_skips_when_failed_restore_would_erase(tmp_path, monkeypatch):
    """BINDING (FB-2): no start this run + a NON-EMPTY existing session.json →
    _save_session does NOT overwrite (the failed-restore eraser is disarmed)."""
    import window
    sess = tmp_path / 'session.json'
    monkeypatch.setattr(window, 'SESSION_FILE', str(sess))
    # The last good session on disk has two projects.
    save_session(str(sess), ['/a', '/b'], '/a')
    before = sess.read_text()
    fake = _save_fake(tmp_path, any_started=False, terminals={})  # nothing running
    AppWindow._save_session(fake)
    assert sess.read_text() == before          # file untouched — preserved


def test_save_session_proceeds_when_started_even_if_all_closed(tmp_path, monkeypatch):
    """BINDING (FB-2): something started + all sessions now closed → the empty
    save proceeds (today's deliberate close-everything behavior is preserved)."""
    import window
    sess = tmp_path / 'session.json'
    monkeypatch.setattr(window, 'SESSION_FILE', str(sess))
    save_session(str(sess), ['/a', '/b'], '/a')
    fake = _save_fake(tmp_path, any_started=True, terminals={})  # started, none live now
    AppWindow._save_session(fake)
    open_paths, _ = load_session(str(sess))
    assert open_paths == []                     # empty save proceeded


def test_save_session_proceeds_on_fresh_install(tmp_path, monkeypatch):
    """BINDING (FB-2): no session.json yet (fresh install) → save proceeds even
    with nothing started (there is nothing to lose)."""
    import window
    sess = tmp_path / 'session.json'
    monkeypatch.setattr(window, 'SESSION_FILE', str(sess))
    assert not sess.exists()
    fake = _save_fake(tmp_path, any_started=False,
                      terminals={'/p': _live_term()})  # one live → will be saved
    fake._active_path = '/p'
    AppWindow._save_session(fake)
    open_paths, _ = load_session(str(sess))
    assert open_paths == ['/p']


# ════════════════════════════════════════════════════════════════════════════
# FB-3 — late-death visibility (power #6, C7)
# ════════════════════════════════════════════════════════════════════════════

class _FeedTerm:
    """Records feed_session_ended(code)."""
    def __init__(self):
        self.fed = None

    def feed_session_ended(self, code):
        self.fed = code


def _latedeath_fake(active_path):
    calls = []
    sidebar = types.SimpleNamespace()
    sidebar.set_active_only = lambda v: calls.append(('set_active_only', v))
    fake = types.SimpleNamespace(_active_path=active_path, _sidebar=sidebar)
    return fake, calls


def test_late_death_feeds_exit_code():
    """BINDING (FB-3a): the pane is fed the exit code derived from wait status.
    status 256 == os.waitstatus_to_exitcode → 1."""
    fake, _ = _latedeath_fake('/p')
    tv = _FeedTerm()
    AppWindow._handle_late_death(fake, tv, 256, '/other')  # status 256 -> code 1
    assert tv.fed == 1


def test_late_death_active_project_drops_filter():
    """BINDING (FB-3b): the dying project IS the active one → Active Only dropped
    (you were looking at it; it must not vanish)."""
    fake, calls = _latedeath_fake('/p')
    tv = _FeedTerm()
    AppWindow._handle_late_death(fake, tv, 0, '/p')   # path == active
    assert ('set_active_only', False) in calls
    assert tv.fed == 0


def test_late_death_background_project_does_not_drop_filter():
    """BINDING (FB-3b): a BACKGROUND death does NOT touch the filter (its row was
    already hidden by the user's own choice). The banner still feeds."""
    fake, calls = _latedeath_fake('/active')
    tv = _FeedTerm()
    AppWindow._handle_late_death(fake, tv, 0, '/background')  # not the active path
    assert calls == []                  # filter untouched
    assert tv.fed == 0                  # banner still fed


def test_late_death_undecodable_status_falls_back_to_raw():
    """A status os.waitstatus_to_exitcode can't decode falls back to the raw
    number rather than crashing the exit path."""
    fake, _ = _latedeath_fake('/p')
    tv = _FeedTerm()
    # A 'stopped' status (0x13 == SIGSTOP-ish) raises ValueError in
    # waitstatus_to_exitcode; the handler must not crash.
    AppWindow._handle_late_death(fake, tv, 0x13, '/p')
    assert tv.fed is not None           # some code was fed, no raise


# ════════════════════════════════════════════════════════════════════════════
# FB-4 — the shared zellij kill helper (audit-1)
# ════════════════════════════════════════════════════════════════════════════

def test_zellij_kill_session_only_kills_when_alive(monkeypatch):
    """BINDING (FB-4): kill_session runs `zellij kill-session` ONLY when the
    session exists; an absent session is a silent no-op."""
    import zellij
    runs = []
    monkeypatch.setattr(zellij.subprocess, 'run',
                        lambda argv, **k: runs.append(argv))
    # Session exists → kill fires.
    monkeypatch.setattr(zellij, 'session_exists', lambda name: True)
    zellij.kill_session('pm-live')
    assert runs == [['zellij', 'kill-session', 'pm-live']]
    # Session absent → no subprocess.
    runs.clear()
    monkeypatch.setattr(zellij, 'session_exists', lambda name: False)
    zellij.kill_session('pm-gone')
    assert runs == []


def test_zellij_kill_session_never_raises(monkeypatch):
    """A missing zellij binary / subprocess error is swallowed (defensive)."""
    import zellij
    monkeypatch.setattr(zellij, 'session_exists', lambda name: True)
    def boom(*a, **k):
        raise FileNotFoundError('no zellij')
    monkeypatch.setattr(zellij.subprocess, 'run', boom)
    zellij.kill_session('pm-x')   # must not raise


# ════════════════════════════════════════════════════════════════════════════
# FB-10 — the spawn-failure toast is persistent (timeout=0) (RB-1 / H2)
# ════════════════════════════════════════════════════════════════════════════

def _spawnfail_fake():
    toasts = []
    sidebar = types.SimpleNamespace(set_active_only=lambda v: None)
    fake = types.SimpleNamespace(
        _sidebar=sidebar,
        _warned_spawn_fail=set(),
    )
    fake._show_toast = lambda text, timeout=5: toasts.append((text, timeout))
    fake._spawn_failure_toast_text = lambda agent_id, raw: f'{agent_id} not found'
    return fake, toasts


def test_spawn_failure_toast_is_persistent():
    """BINDING (FB-10): the spawn-failure toast call pins timeout=0 (persistent,
    like the ccr fallback toast) — reverting to the default timeout=5 FAILS."""
    fake, toasts = _spawnfail_fake()
    AppWindow._on_spawn_failed(fake, '/p', 'grok', 'grok')
    assert len(toasts) == 1
    text, timeout = toasts[0]
    assert timeout == 0
    assert 'grok not found' in text


def test_spawn_failure_toast_dedup_unchanged():
    """The one-shot dedup is unchanged: a second identical (path, agent) miss is
    NOT re-toasted."""
    fake, toasts = _spawnfail_fake()
    AppWindow._on_spawn_failed(fake, '/p', 'grok', 'grok')
    AppWindow._on_spawn_failed(fake, '/p', 'grok', 'grok')   # same key
    assert len(toasts) == 1                                   # deduped
