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


def test_ensure_kimi_co_shim_is_pm_owned_not_harness_tree(tmp_path):
    """Shim lives under ~/.ProjectMan/bin — never touches ~/.kimi-code."""
    shim_path = harnesses.ensure_kimi_co_shim(home=str(tmp_path))
    assert shim_path
    assert shim_path.startswith(str(tmp_path / '.ProjectMan' / 'bin'))
    assert (tmp_path / '.ProjectMan' / 'bin' / 'kimi-co').is_file()
    # No writes into the harness install tree
    assert not (tmp_path / '.kimi-code').exists()
    # Idempotent rewrite
    assert harnesses.ensure_kimi_co_shim(home=str(tmp_path)) == shim_path


def test_kimi_co_shim_execs_real_kimi(tmp_path):
    bindir = tmp_path / '.kimi-code' / 'bin'
    bindir.mkdir(parents=True)
    marker = tmp_path / 'ran'
    kimi = bindir / 'kimi'
    kimi.write_text(f'#!/bin/sh\necho ok > "{marker}"\n')
    kimi.chmod(0o755)
    shim = harnesses.ensure_kimi_co_shim(home=str(tmp_path))
    env = harnesses.with_harness_path({'PATH': '/usr/bin'}, home=str(tmp_path))
    import subprocess
    r = subprocess.run(
        [shim, '--version'],
        env={**os.environ, **env, 'HOME': str(tmp_path)},
        capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0
    assert marker.read_text().strip() == 'ok'


def test_with_harness_path_prepends_pm_bin(tmp_path):
    (tmp_path / '.ProjectMan' / 'bin').mkdir(parents=True)
    (tmp_path / '.kimi-code' / 'bin').mkdir(parents=True)
    out = harnesses.with_harness_path({'PATH': '/usr/bin'}, home=str(tmp_path))
    parts = out['PATH'].split(os.pathsep)
    assert parts[0] == str(tmp_path / '.ProjectMan' / 'bin')


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
