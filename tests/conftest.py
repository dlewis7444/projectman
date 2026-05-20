"""Shared test fixtures.

The PAA monitor and settings layer both write to fixed paths under
``~/.ProjectMan/``. Without isolation, a test that runs ``PAAMonitor.run_scan``
will clobber the developer's real ``settings.json`` and ``paa-mtime-cache.json``
because ``Settings.save()`` and ``_save_mtime_cache()`` default to those paths.

The autouse fixture below redirects both to a sibling temp dir for every test.
A sibling (rather than nested) dir keeps the per-test ``tmp_path`` clean for
tests that assert on its directory contents.
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
