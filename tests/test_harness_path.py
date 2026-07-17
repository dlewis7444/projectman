"""Harness user-bin PATH augmentation (local spawn / doctor).

GUI-launched ProjectMan does not source .bashrc, so installer dirs like
``~/.kimi-code/bin`` are missing until we prepend them.
"""
import os

import harnesses


def test_harness_user_bin_dirs_only_existing(tmp_path):
    (tmp_path / 'kimi-code' / 'bin').mkdir(parents=True)
    (tmp_path / 'opencode' / 'bin').mkdir(parents=True)
    # grok missing
    # Monkeypatch expand locations by using a fake home layout matching suffixes
    # HARNESS_USER_BIN_DIRS uses ~/… so home=tmp_path with subdirs named correctly
    home = tmp_path
    (home / '.kimi-code' / 'bin').mkdir(parents=True)
    (home / '.opencode' / 'bin').mkdir(parents=True)
    dirs = harnesses.harness_user_bin_dirs(home=str(home))
    assert str(home / '.kimi-code' / 'bin') in dirs
    assert str(home / '.opencode' / 'bin') in dirs
    assert not any('.grok' in d for d in dirs)


def test_with_harness_path_prepends_and_is_idempotent(tmp_path):
    home = tmp_path
    kimi = home / '.kimi-code' / 'bin'
    kimi.mkdir(parents=True)
    env = {'PATH': '/usr/bin', 'FOO': 'bar'}
    out = harnesses.with_harness_path(env, home=str(home))
    assert out['FOO'] == 'bar'
    parts = out['PATH'].split(os.pathsep)
    assert parts[0] == str(kimi)
    assert '/usr/bin' in parts
    # Second apply: no duplicate
    out2 = harnesses.with_harness_path(out, home=str(home))
    assert out2['PATH'].split(os.pathsep).count(str(kimi)) == 1


def test_with_harness_path_empty_path(tmp_path):
    home = tmp_path
    (home / '.local' / 'bin').mkdir(parents=True)
    out = harnesses.with_harness_path({}, home=str(home))
    assert str(home / '.local' / 'bin') in out['PATH']


def test_ensure_process_harness_path_mutates_os_environ(tmp_path, monkeypatch):
    home = tmp_path
    (home / '.kimi-code' / 'bin').mkdir(parents=True)
    monkeypatch.setenv('PATH', '/usr/bin')
    path = harnesses.ensure_process_harness_path(home=str(home))
    assert str(home / '.kimi-code' / 'bin') in path
    assert str(home / '.kimi-code' / 'bin') in os.environ['PATH']
