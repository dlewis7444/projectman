"""P2.1 — lifecycle hardening: signal-safe shutdown + the three incident bugs.

All window/app methods are tested UNBOUND against duck-typed SimpleNamespace
recorders (the established A2/A5 pattern, see test_session_agents.py). Pure
decisions live in ``session.py`` and are tested directly. ``ProjectManApp`` is
CONSTRUCTED (only) for T7 — the spec sanctions headless construction; it is
never registered/run/activated.

Binding tests:
  T1 plan_emergency_kill   — direct live only; zellij & dead skipped
  T2 emergency_shutdown    — order: save → kill; zellij never killed
  T3 _on_unix_signal       — emergency_shutdown → quit, one-shot SOURCE_REMOVE
  T4 _on_activate          — second activation presents, never rebuilds
  T5 should_quit_app       — primary/None quit; stray does not
  T6 _quit                 — stray: no app.quit; primary: clears _window + quit
  T7 app id override       — PM_APP_ID wins; absent → real id; construct-only
  T8 conftest guard        — PM_APP_ID pinned to the test id in every run
"""
import os
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import GLib

import main
from window import AppWindow
from session import plan_emergency_kill, should_quit_app


# ── fakes ─────────────────────────────────────────────────────────────────────

def _term(child_pid, is_zellij, calls=None, path=None):
    """Fake TerminalView: duck-typed _child_pid / _is_zellij, recording
    _kill_child (mirrors the SimpleNamespace fakes elsewhere in the suite)."""
    t = types.SimpleNamespace(_child_pid=child_pid, _is_zellij=is_zellij)

    def kill():
        if calls is not None:
            calls.append(('kill', path))
        t._child_pid = None

    t._kill_child = kill
    return t


# ── T1: plan_emergency_kill ───────────────────────────────────────────────────

def test_plan_emergency_kill_direct_only():
    """T1 — {a: live direct, b: live zellij, c: no child} → exactly [a].
    Zellij sessions persist by design; dead children are nothing to kill."""
    terminals = {
        '/a': _term(111, is_zellij=False),
        '/b': _term(222, is_zellij=True),
        '/c': _term(None, is_zellij=False),
    }
    assert plan_emergency_kill(terminals) == ['/a']


def test_plan_emergency_kill_empty():
    assert plan_emergency_kill({}) == []


def test_plan_emergency_kill_zellij_with_no_child_skipped():
    """A detached zellij terminal (no live child) is not selected either."""
    terminals = {'/z': _term(None, is_zellij=True)}
    assert plan_emergency_kill(terminals) == []


# ── T2: emergency_shutdown ordering ───────────────────────────────────────────

def test_emergency_shutdown_saves_before_kill_and_skips_zellij():
    """T2 — save_session is recorded BEFORE any _kill_child; the zellij
    terminal's _kill_child is NEVER called; ccr stop runs last; the killed
    direct paths are returned."""
    calls = []
    terminals = {
        '/direct': _term(111, is_zellij=False, calls=calls, path='/direct'),
        '/zellij': _term(222, is_zellij=True, calls=calls, path='/zellij'),
        '/dead': _term(None, is_zellij=False, calls=calls, path='/dead'),
    }
    fake = types.SimpleNamespace(_terminals=terminals)
    fake._save_session = lambda: calls.append(('save', None))
    fake._stop_managed_ccr = lambda: calls.append(('ccr_stop', None))

    killed = AppWindow.emergency_shutdown(fake)

    assert killed == ['/direct']
    # save first, exactly one kill (the direct one), ccr stop last.
    assert calls == [('save', None), ('kill', '/direct'), ('ccr_stop', None)]
    # zellij _kill_child never fired.
    assert ('kill', '/zellij') not in calls
    # save strictly precedes every kill.
    save_idx = calls.index(('save', None))
    kill_idxs = [i for i, c in enumerate(calls) if c[0] == 'kill']
    assert all(save_idx < i for i in kill_idxs)


# ── T3: signal handler one-shot ───────────────────────────────────────────────

def test_on_unix_signal_shuts_down_then_quits_one_shot():
    """T3 — emergency_shutdown then quit are both called; the return value is
    GLib.SOURCE_REMOVE (False) so the source is removed after one signal."""
    calls = []
    window = types.SimpleNamespace()
    window.emergency_shutdown = lambda: calls.append('emergency')
    fake = types.SimpleNamespace(_window=window)
    fake.quit = lambda: calls.append('quit')

    rv = main.ProjectManApp._on_unix_signal(fake)

    assert calls == ['emergency', 'quit']
    assert rv == GLib.SOURCE_REMOVE
    assert rv is False  # one-shot: SOURCE_REMOVE


def test_on_unix_signal_no_window_still_quits():
    """No window yet (signal during startup) → no emergency call, but quit
    still fires and the source is still removed."""
    calls = []
    fake = types.SimpleNamespace(_window=None)
    fake.quit = lambda: calls.append('quit')

    rv = main.ProjectManApp._on_unix_signal(fake)

    assert calls == ['quit']
    assert rv is GLib.SOURCE_REMOVE


# ── T4: second activation presents, never rebuilds ────────────────────────────

def test_on_activate_second_activation_presents_not_rebuild():
    """T4 — with an existing _window, _on_activate calls present() once and
    returns immediately; no build-side attribute (e.g. _store) is created."""
    presented = []
    window = types.SimpleNamespace()
    window.present = lambda: presented.append(True)
    fake = types.SimpleNamespace(_window=window)
    keys_before = set(vars(fake).keys())

    main.ProjectManApp._on_activate(fake, fake)

    assert presented == [True]
    # No rebuild: attribute set unchanged, in particular no _store.
    assert set(vars(fake).keys()) == keys_before
    assert not hasattr(fake, '_store')


# ── T5: should_quit_app ───────────────────────────────────────────────────────

def test_should_quit_app_decision_table():
    """T5 — quit when closing IS primary or primary is None; a stray does not."""
    primary = object()
    stray = object()
    assert should_quit_app(primary, primary) is True
    assert should_quit_app(None, stray) is True
    assert should_quit_app(primary, stray) is False


# ── T6: _quit gates app.quit on primary-window identity ───────────────────────

def _quit_fake(app):
    """Fake AppWindow self for _quit: ccr_managed=False so the real
    _stop_managed_ccr guard is exercised as a genuine no-op; destroy recorded;
    get_application returns the given fake app."""
    calls = []
    fake = types.SimpleNamespace(
        _settings=types.SimpleNamespace(ccr_managed=False),
    )
    fake.destroy = lambda: calls.append('destroy')
    fake.get_application = lambda: app
    # Bind the real helper so the ccr guard runs (no-op under ccr_managed=False).
    fake._stop_managed_ccr = lambda: AppWindow._stop_managed_ccr(fake)
    return fake, calls


def test_quit_stray_window_does_not_quit_app():
    """T6a — closing a stray (app._window is a DIFFERENT window): destroy
    fires, app.quit does NOT, and app._window is left intact."""
    quit_calls = []
    primary = object()
    app = types.SimpleNamespace(_window=primary)
    app.quit = lambda: quit_calls.append(True)
    fake, calls = _quit_fake(app)

    AppWindow._quit(fake)

    assert 'destroy' in calls
    assert quit_calls == []          # app NOT quit
    assert app._window is primary    # primary untouched


def test_quit_primary_window_clears_window_then_quits():
    """T6b — closing the primary window: app._window cleared BEFORE quit, and
    app.quit fires (destroy always)."""
    order = []
    app = types.SimpleNamespace()
    fake, calls = _quit_fake(app)
    app._window = fake               # this IS the primary window
    app.quit = lambda: order.append(('quit', app._window))

    AppWindow._quit(fake)

    assert 'destroy' in calls
    assert order == [('quit', None)]  # _window cleared to None before quit ran
    assert app._window is None


def test_quit_no_application_is_safe():
    """get_application() None (window never added to an app): destroy still
    fires, nothing raises."""
    fake, calls = _quit_fake(None)
    AppWindow._quit(fake)
    assert 'destroy' in calls


# ── T7: application id is test-overridable (construct-only) ────────────────────

def test_app_id_uses_pm_app_id_override(monkeypatch):
    """T7 — PM_APP_ID set → the constructed app reports it. Construction only;
    never registered/run/activated."""
    monkeypatch.setenv('PM_APP_ID', 'io.github.projectman.test')
    app = main.ProjectManApp(debug_flag=False)
    assert app.get_application_id() == 'io.github.projectman.test'


def test_app_id_falls_back_to_real_when_env_absent(monkeypatch):
    """T7 — PM_APP_ID absent → the real id (the APP_ID module constant)."""
    monkeypatch.delenv('PM_APP_ID', raising=False)
    app = main.ProjectManApp(debug_flag=False)
    assert app.get_application_id() == 'io.github.projectman'
    assert main.APP_ID == 'io.github.projectman'


def test_app_id_empty_env_falls_back_to_real(monkeypatch):
    """An empty PM_APP_ID (set-but-blank) is treated as unset → real id."""
    monkeypatch.setenv('PM_APP_ID', '')
    app = main.ProjectManApp(debug_flag=False)
    assert app.get_application_id() == 'io.github.projectman'


# ── T8: conftest blanket guard is active in every run ─────────────────────────

def test_conftest_pins_test_app_id():
    """T8 — the autouse fixture pins PM_APP_ID to the test id for every run, so
    no test can ever construct under the user's real DBus identity."""
    assert os.environ['PM_APP_ID'] == 'io.github.projectman.test'
