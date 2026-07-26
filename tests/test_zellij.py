# tests/test_zellij.py
import os
import pytest
from unittest.mock import patch
import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib, GObject
import zellij


def test_session_name_simple():
    assert zellij.session_name('my-project') == 'pm-my-project'


def test_session_name_slugifies_spaces():
    assert zellij.session_name('My Cool Project') == 'pm-My-Cool-Project'


def test_session_name_slugifies_special_chars():
    assert zellij.session_name('foo/bar.baz') == 'pm-foo-bar-baz'


def test_session_name_truncates_long_name():
    long = 'a' * 60
    result = zellij.session_name(long)
    assert result.startswith('pm-')
    assert len(result) <= 51  # 'pm-' + 48 chars


def test_session_name_all_special_chars():
    # slug is all dashes — truthy, so 'default' fallback does NOT fire
    assert zellij.session_name('!!!') == 'pm----'


def test_socket_dir_no_version_subdir(tmp_path, monkeypatch):
    """When no version subdir exists yet, returns the base zellij dir."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    assert zellij.socket_dir() == str(tmp_path / 'zellij')


def test_socket_dir_uses_version_subdir(tmp_path, monkeypatch):
    """When a version subdir exists, returns it (zellij 0.43.1+ layout)."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    version_dir = tmp_path / 'zellij' / '0.43.1'
    version_dir.mkdir(parents=True)
    assert zellij.socket_dir() == str(version_dir)


def test_socket_dir_fallback_no_xdg(monkeypatch):
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    result = zellij.socket_dir()
    assert 'zellij' in result


def test_session_exists_true(tmp_path, monkeypatch):
    monkeypatch.setattr(zellij, 'socket_dir', lambda: str(tmp_path))
    (tmp_path / 'pm-myproject').touch()
    assert zellij.session_exists('pm-myproject') is True


def test_session_exists_false(tmp_path, monkeypatch):
    monkeypatch.setattr(zellij, 'socket_dir', lambda: str(tmp_path))
    assert zellij.session_exists('pm-myproject') is False


def test_zellij_watcher_is_gobject():
    w = zellij.ZellijWatcher()
    assert isinstance(w, GObject.GObject)


def test_zellij_watcher_has_signal():
    w = zellij.ZellijWatcher()
    signals = GObject.signal_list_names(w)
    assert 'sessions-changed' in signals


def test_kill_session_passes_timeout(tmp_path, monkeypatch):
    """kill_session runs on synchronous UI paths (deactivate/archive/respawn);
    it must pass a timeout so a wedged zellij server cannot hang the GTK main
    loop (docs/popover-leak-main-thread-hang.md, landmine #1)."""
    monkeypatch.setattr(zellij, 'socket_dir', lambda: str(tmp_path))
    (tmp_path / 'pm-proj').touch()
    calls = []
    monkeypatch.setattr(
        zellij.subprocess, 'run',
        lambda cmd, **kw: calls.append((cmd, kw)))
    zellij.kill_session('pm-proj')
    assert len(calls) == 1
    assert calls[0][0] == ['zellij', 'kill-session', 'pm-proj']
    assert calls[0][1].get('timeout') is not None


def test_kill_session_timeout_does_not_raise(tmp_path, monkeypatch):
    """A timed-out kill is swallowed like every other kill failure."""
    import subprocess as sp
    monkeypatch.setattr(zellij, 'socket_dir', lambda: str(tmp_path))
    (tmp_path / 'pm-proj').touch()

    def _timeout(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get('timeout'))
    monkeypatch.setattr(zellij.subprocess, 'run', _timeout)
    zellij.kill_session('pm-proj')  # must not raise
