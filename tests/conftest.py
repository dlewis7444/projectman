"""Shared test fixtures.

The PAA monitor and settings layer both write to fixed paths under
``~/.ProjectMan/``. Without isolation, a test that runs ``PAAMonitor.run_scan``
will clobber the developer's real ``settings.json`` and ``paa-mtime-cache.json``
because ``Settings.save()`` and ``_save_mtime_cache()`` default to those paths.

The autouse fixture below redirects both to a sibling temp dir for every test.
A sibling (rather than nested) dir keeps the per-test ``tmp_path`` clean for
tests that assert on its directory contents.

ccr module-level state (_started_proc, _cooldown_deadline) is also reset between
tests so that a test that leaves the cooldown active (e.g. a failed-start test)
does not bleed into the next test's spawn_env() call.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_projectman_paths(tmp_path_factory, monkeypatch):
    fake_home = tmp_path_factory.mktemp('_pm_home')
    monkeypatch.setattr(
        'settings.DEFAULT_SETTINGS_PATH',
        str(fake_home / 'settings.json'),
        raising=False,
    )
    # StatusWatcher now DUAL-watches the new (~/.ProjectMan/status) and legacy
    # (~/.claude/projectman/status) dirs. Point the legacy dir at a fresh,
    # non-existent temp path so a developer's real status files never bleed into
    # a test that asserts on _reload() output. Tests that exercise the new dir
    # monkeypatch model.STATUS_DIR themselves; this just neutralizes the legacy
    # half by default. Tests covering the dual-watch merge override both.
    try:
        import model
        monkeypatch.setattr(
            model, 'LEGACY_STATUS_DIR',
            str(fake_home / '_legacy_status_absent'),
            raising=False,
        )
    except ImportError:
        pass
    try:
        import paa_monitor
        monkeypatch.setattr(
            paa_monitor,
            '_MTIME_CACHE_PATH',
            str(fake_home / 'paa-mtime-cache.json'),
            raising=False,
        )
    except ImportError:
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_ccr_module_state():
    """Reset ccr module-level mutable state between tests.

    _started_proc and _cooldown_deadline are module globals that tests can
    leave in a non-default state. Without this reset a failed-start test
    that sets _cooldown_deadline bleeds into subsequent spawn_env() calls and
    causes spurious cooldown-suppressed failures.
    """
    import ccr
    ccr._started_proc = None
    ccr._cooldown_deadline = None
    yield
    ccr._started_proc = None
    ccr._cooldown_deadline = None
