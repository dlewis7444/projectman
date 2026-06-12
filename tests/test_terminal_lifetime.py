"""P3.5e FB-9 (David's reveal #2, C8-amended): sticky-agent lifetime = SESSION
lifetime.

A restored session's TerminalView carries a construction-time ``_explicit_agent``
(A2 saved-agent-wins) that must NOT outlive the session: it is cleared when the
child TRULY ends (natural exit, deactivate via SIGTERM, zellij-kill, spawn
failure) so a PENDING per-project override is honored on the next activation —
but it is PRESERVED through a zellij DETACH (the session lives on; a reattach
legitimately resumes the same agent).

These exercise ``TerminalView._fire_exit_if_current`` UNBOUND against a
``types.SimpleNamespace`` recorder — the single funnel where ``_child_pid`` goes
None for every non-detach end (the established headless pattern, see
test_lifecycle.py / test_session_agents.py). The full restore→override→spawn
round-trip is in test_terminal_agent.py (display-gated, the bench runs it).
"""
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk  # noqa: F401  (import pins the GI types the module needs)

import terminal
from terminal import TerminalView


def _fake_tv(*, child_pid=4242, explicit_agent='grok', is_zellij=False,
             zellij_session=None, spawn_failed=False):
    """A duck-typed TerminalView carrying just what _fire_exit_if_current reads.
    Records every emit so the test can assert which signal fired."""
    emits = []
    t = types.SimpleNamespace(
        _child_pid=child_pid,
        _explicit_agent=explicit_agent,
        _is_zellij=is_zellij,
        _zellij_session=zellij_session,
        _spawn_binary='grok',
    )
    t.emit = lambda *a: emits.append(a)
    t._is_spawn_failure = lambda status: spawn_failed
    t._emits = emits
    return t


# ── T-a / the David repro (clearing half): a true END drops the restore agent ──

def test_natural_exit_clears_explicit_agent():
    """T-d/T-a: a non-zellij child exit (natural quit OR deactivate-via-SIGTERM)
    clears _explicit_agent — so the NEXT spawn follows the pending override, not
    the dead session's agent. Reverting the clear leaves it 'grok' and FAILS."""
    t = _fake_tv(explicit_agent='grok', is_zellij=False)
    TerminalView._fire_exit_if_current(t, 4242, 0)
    assert t._explicit_agent is None
    assert t._child_pid is None
    # The true-end path emits process-exited (not detached).
    assert ('process-exited', 0) in t._emits
    assert all(e[0] != 'process-detached' for e in t._emits)


def test_zellij_kill_deactivate_clears_explicit_agent(monkeypatch):
    """The zellij-KILL deactivate path: the session is dead (session_alive False),
    so the child's exit is a true end → clears. (window._on_project_deactivate
    flips _is_zellij off before the kill; here the session simply isn't alive.)"""
    monkeypatch.setattr(terminal.zellij, 'session_alive', lambda name: False)
    t = _fake_tv(explicit_agent='grok', is_zellij=True, zellij_session='pm-x')
    TerminalView._fire_exit_if_current(t, 4242, 0)
    assert t._explicit_agent is None
    assert t._is_zellij is False
    assert ('process-exited', 0) in t._emits


def test_spawn_failure_end_clears_explicit_agent():
    """A spawn-failure death (missing binary) is still a true end → clears, and
    fires BOTH process-spawn-failed and process-exited."""
    t = _fake_tv(explicit_agent='grok', is_zellij=False, spawn_failed=True)
    TerminalView._fire_exit_if_current(t, 4242, 32512)  # 127 exec-not-found
    assert t._explicit_agent is None
    kinds = [e[0] for e in t._emits]
    assert 'process-spawn-failed' in kinds
    assert 'process-exited' in kinds


# ── T-b: a zellij DETACH preserves the restore agent (reattach resumes it) ─────

def test_zellij_detach_preserves_explicit_agent(monkeypatch):
    """T-b: the zellij session is STILL ALIVE → this is a DETACH, not an end. The
    explicit agent MUST survive (a reattach resumes the same agent). Over-clearing
    here — moving the clear above the detach early-return — FAILS this."""
    monkeypatch.setattr(terminal.zellij, 'session_alive', lambda name: True)
    t = _fake_tv(explicit_agent='grok', is_zellij=True, zellij_session='pm-x')
    TerminalView._fire_exit_if_current(t, 4242, 0)
    assert t._explicit_agent == 'grok'      # preserved through detach
    # Detach fired; no process-exited.
    kinds = [e[0] for e in t._emits]
    assert 'process-detached' in kinds
    assert 'process-exited' not in kinds


def test_stale_pid_does_not_clear():
    """A stale exit (pid != current) is dropped early — it must NOT clear the
    explicit agent of the live session that replaced it."""
    t = _fake_tv(child_pid=9999, explicit_agent='grok')
    rv = TerminalView._fire_exit_if_current(t, 4242, 0)   # 4242 != 9999
    assert rv is False
    assert t._explicit_agent == 'grok'      # untouched
    assert t._child_pid == 9999             # untouched
    assert t._emits == []
