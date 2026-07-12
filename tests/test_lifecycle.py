"""P2.1 — lifecycle hardening: signal-safe shutdown + the three incident bugs.

All window/app methods are tested UNBOUND against duck-typed SimpleNamespace
recorders (the established A2/A5 pattern, see test_session_harnesses.py). Pure
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
    terminal's _kill_child is NEVER called; the killed
    direct paths are returned."""
    calls = []
    terminals = {
        '/direct': _term(111, is_zellij=False, calls=calls, path='/direct'),
        '/zellij': _term(222, is_zellij=True, calls=calls, path='/zellij'),
        '/dead': _term(None, is_zellij=False, calls=calls, path='/dead'),
    }
    fake = types.SimpleNamespace(_terminals=terminals)
    fake._save_session = lambda: calls.append(('save', None))

    killed = AppWindow.emergency_shutdown(fake)

    assert killed == ['/direct']
    # save first, exactly one kill (the direct one), ccr stop last.
    assert calls == [('save', None), ('kill', '/direct')]
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
    """Fake AppWindow self for _quit: destroy recorded;
    get_application returns the given fake app."""
    calls = []
    fake = types.SimpleNamespace(
        _settings=types.SimpleNamespace(),
    )
    fake.destroy = lambda: calls.append('destroy')
    fake.get_application = lambda: app
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


# ── G3 (reveal-3 item 3, C3): auto-activate the new project on create ─────────
#
# Creating a project dropped the user on a dead pane. _on_project_create now
# routes through the CANONICAL activation path (_on_project_activated, the
# restore precedent) so the new project opens straight into its harness — while
# the B4 creation toast STAYS (it names the resolved agent). Unbound-method
# idiom against duck-typed recorders, as elsewhere in this file.

def _create_fake(projects_dir='/tmp/projects', create_raises=None):
    """A SimpleNamespace self for AppWindow._on_project_create: records
    create/refresh/activate/toast; _store.create_project can be made to raise."""
    calls = []

    def create_project(name):
        calls.append(('create', name))
        if create_raises is not None:
            raise create_raises

    store = types.SimpleNamespace(
        create_project=create_project,
        _projects_dir=lambda: projects_dir,
    )
    sidebar = types.SimpleNamespace(refresh=lambda: calls.append(('refresh',)))
    fake = types.SimpleNamespace(_store=store, _sidebar=sidebar)
    fake._on_project_activated = (
        lambda sb, path: calls.append(('activate', sb, path)))
    fake._show_toast = lambda text: calls.append(('toast', text))
    fake._project_created_toast_text = lambda name: f"toast::{name}"
    return fake, calls


def test_g3a_create_activates_new_project_canonically():
    """T-G3a (revert proof mandated): a successful create activates the new
    project through the canonical _on_project_activated path with the
    store-derived path (the terminal/stack presentation the activation drives).
    Neutering the activation call FAILS this. Order: create → refresh →
    activate (then toast)."""
    fake, calls = _create_fake(projects_dir='/tmp/projects')
    AppWindow._on_project_create(fake, fake._sidebar, 'localhost', 'newproj')
    expected_path = os.path.join('/tmp/projects', 'newproj')
    assert ('activate', fake._sidebar, expected_path) in calls
    # canonical ordering: create, then refresh, then activate.
    assert calls.index(('create', 'newproj')) < calls.index(('refresh',))


def test_rename_remote_uses_remote_store_not_local_os_rename(monkeypatch):
    """Remote rename must SSH-rename via remote_store, not ProjectStore.os.rename."""
    from model import Project
    from hosts import HostProfile, encode_project_ref
    import remote_store

    old_path = encode_project_ref('h1', 'old')
    new_path = encode_project_ref('h1', 'new')
    proj = Project(name='old', path=old_path, host_id='h1', remote_cwd='/r/old')
    calls = []

    def rename_remote(profile, old_name, new_name, **kw):
        calls.append(('remote_rename', profile.id, old_name, new_name))
        return True, None

    def list_remote(profile, **kw):
        calls.append(('list', profile.id))
        return [Project(name='new', path=new_path, host_id='h1',
                        remote_cwd='/r/new')], None

    monkeypatch.setattr(remote_store, 'rename_remote_project', rename_remote)
    monkeypatch.setattr(remote_store, 'list_remote_projects', list_remote)

    store_calls = []
    fake = types.SimpleNamespace(
        _settings=types.SimpleNamespace(
            host_profiles=lambda: {
                'h1': HostProfile(id='h1', ssh_target='box',
                                  remote_projects_dir='~/p'),
            },
        ),
        _store=types.SimpleNamespace(
            rename_project=lambda *a, **k: store_calls.append(a) or (_ for _ in ()).throw(
                AssertionError('local rename must not run for remote')),
            _projects_dir=lambda: '/tmp/projects',
        ),
        _sidebar=types.SimpleNamespace(
            _remote_projects={'h1': [proj]},
            set_remote_projects=lambda hid, ps: calls.append(
                ('set_remote', hid, [p.name for p in ps])),
            refresh=lambda: calls.append(('refresh',)),
            _process_states={},
            _running_harnesses={},
        ),
        _terminals={},
        _active_path=None,
        _mru=[],
        _find_project=lambda p: proj if p == old_path else None,
        _show_toast=lambda t: calls.append(('toast', t)),
        _sync_running_state=lambda: calls.append(('sync',)),
        _set_active_project=lambda p: calls.append(('active', p.name)),
    )
    AppWindow._on_project_rename(fake, fake._sidebar, old_path, 'new')
    assert ('remote_rename', 'h1', 'old', 'new') in calls
    assert ('set_remote', 'h1', ['new']) in calls
    assert store_calls == []
    assert ('refresh',) not in calls  # remote path refreshes via set_remote_projects
    assert ('sync',) in calls


def test_g3b_creation_toast_still_fires():
    """T-G3b (regression pin on B4): the creation toast still fires AFTER the new
    activation — adding G3 did not displace the harness-naming toast."""
    fake, calls = _create_fake()
    AppWindow._on_project_create(fake, fake._sidebar, 'localhost', 'newproj')
    assert ('toast', 'toast::newproj') in calls
    # toast comes after the activation (the spawn makes the named harness concrete).
    expected_path = os.path.join('/tmp/projects', 'newproj')
    assert calls.index(('activate', fake._sidebar, expected_path)) < calls.index(
        ('toast', 'toast::newproj'))


def test_g3c_create_failure_skips_activation_and_toast():
    """T-G3c: create raising OSError returns early — no activation, no toast.
    The pre-existing clean early-return is preserved."""
    fake, calls = _create_fake(create_raises=OSError('boom'))
    AppWindow._on_project_create(fake, fake._sidebar, 'localhost', 'newproj')
    assert ('create', 'newproj') in calls
    assert not any(c[0] == 'activate' for c in calls)
    assert not any(c[0] == 'toast' for c in calls)
    assert not any(c[0] == 'refresh' for c in calls)


# ════════════════════════════════════════════════════════════════════════════
# P3.5d Item 3 (P0-era dead-knob): --debug only OVERRIDES when PRESENT. The old
# unconditional `settings.debug_logging = self._debug_flag` forced False on every
# flagless launch, and a later settings.save() persisted it — killing the
# Settings-window toggle. The real production method is ProjectManApp.
# _apply_debug_flag; tested UNBOUND against a SimpleNamespace carrying _debug_flag
# (the same pattern as the other lifecycle methods). T7 = flag → True even when
# saved False; T8 = no flag + saved True → stays True (dead knob LIVES);
# T9 = no flag + saved False → False.
# ════════════════════════════════════════════════════════════════════════════

def test_p35d_t7_debug_flag_forces_on_even_when_saved_false():
    """T7: --debug present → debug_logging True even when the saved/Settings value
    is False."""
    from settings import Settings
    s = Settings(debug_logging=False)
    fake = types.SimpleNamespace(_debug_flag=True)
    main.ProjectManApp._apply_debug_flag(fake, s)
    assert s.debug_logging is True


def test_p35d_t8_no_flag_leaves_saved_true_authoritative():
    """T8 (the dead knob lives): no --debug + a saved True → stays True. The old
    unconditional assignment forced it to False here; reverting the fix FAILS."""
    from settings import Settings
    s = Settings(debug_logging=True)
    fake = types.SimpleNamespace(_debug_flag=False)
    main.ProjectManApp._apply_debug_flag(fake, s)
    assert s.debug_logging is True


def test_p35d_t9_no_flag_leaves_saved_false_false():
    """T9: no --debug + a saved False → stays False (no spurious enablement)."""
    from settings import Settings
    s = Settings(debug_logging=False)
    fake = types.SimpleNamespace(_debug_flag=False)
    main.ProjectManApp._apply_debug_flag(fake, s)
    assert s.debug_logging is False


def test_p35d_debug_flag_argv_parsing():
    """main() derives the flag from --debug in argv and strips it before app.run
    (the flag is consumed, never forwarded to GApplication)."""
    import sys
    saved = sys.argv
    constructed = {}
    run_argv = {}
    try:
        sys.argv = ['main.py', '--debug', 'extra']

        class _FakeApp:
            def __init__(self, debug_flag=False):
                constructed['flag'] = debug_flag

            def run(self, argv):
                run_argv['argv'] = argv

        orig = main.ProjectManApp
        main.ProjectManApp = _FakeApp
        try:
            main.main()
        finally:
            main.ProjectManApp = orig
    finally:
        sys.argv = saved
    assert constructed['flag'] is True
    assert '--debug' not in run_argv['argv']
    assert 'extra' in run_argv['argv']
