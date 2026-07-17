"""Kimi bridge install: status script + [[hooks]] registration (M5 / m1)."""
import importlib.util
import os
import stat
from pathlib import Path
from unittest.mock import patch

import harnesses


def _app_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_register_hooks():
    """Load bridges/kimi/register_hooks.py without requiring a package tree."""
    path = os.path.join(_app_dir(), 'bridges', 'kimi', 'register_hooks.py')
    spec = importlib.util.spec_from_file_location('kimi_register_hooks_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_harness_bridge_kimi_script_and_hooks(tmp_path):
    home = str(tmp_path)
    result = harnesses.install_harness_bridge(_app_dir(), 'kimi', home=home)
    assert result == 'installed'

    script = Path(home) / '.kimi-code' / 'hooks' / 'projectman-status.py'
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, 'status script must be executable'

    reg = _load_register_hooks()
    cfg = Path(home) / '.kimi-code' / 'config.toml'
    assert cfg.is_file()
    text = cfg.read_text()
    blocks = [b for b in reg.parse_hooks_blocks(text) if b['is_pm']]
    assert len(blocks) == len(reg.KIMI_HOOK_EVENTS)
    assert {b['event'] for b in blocks} == set(reg.KIMI_HOOK_EVENTS)
    assert reg.kimi_hooks_are_registered(home=home)


def test_install_harness_bridge_kimi_second_call_already(tmp_path):
    home = str(tmp_path)
    assert harnesses.install_harness_bridge(_app_dir(), 'kimi', home=home) == 'installed'
    assert harnesses.install_harness_bridge(_app_dir(), 'kimi', home=home) == 'already'


def test_install_preserves_preexisting_non_pm_hooks(tmp_path):
    home = str(tmp_path)
    cfg_dir = Path(home) / '.kimi-code'
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / 'config.toml'
    cfg.write_text(
        'default_model = "kimi-code/kimi-for-coding"\n'
        '\n'
        '[[hooks]]\n'
        'event = "UserPromptSubmit"\n'
        'command = "echo user-hook"\n'
    )
    result = harnesses.install_harness_bridge(_app_dir(), 'kimi', home=home)
    assert result == 'installed'
    reg = _load_register_hooks()
    text = cfg.read_text()
    assert 'echo user-hook' in text
    assert 'default_model = "kimi-code/kimi-for-coding"' in text
    pm = [b for b in reg.parse_hooks_blocks(text) if b['is_pm']]
    assert len(pm) == len(reg.KIMI_HOOK_EVENTS)
    non_pm = [b for b in reg.parse_hooks_blocks(text) if not b['is_pm']]
    assert any(b['command'] == 'echo user-hook' for b in non_pm)


def test_install_returns_error_when_hooks_fail_oserror(tmp_path):
    """M5: hooks post-step OSError → overall 'error' (no half-install success)."""
    home = str(tmp_path)
    # Make config.toml a directory so ensure_kimi_hooks_registered hits OSError.
    cfg = Path(home) / '.kimi-code' / 'config.toml'
    cfg.parent.mkdir(parents=True)
    cfg.mkdir()

    result = harnesses.install_harness_bridge(_app_dir(), 'kimi', home=home)
    assert result == 'error'
    # Script may still have been written — but caller must not toast success.
    script = Path(home) / '.kimi-code' / 'hooks' / 'projectman-status.py'
    assert script.is_file()


def test_install_returns_error_when_hook_step_returns_error(tmp_path):
    """M5: ensure_kimi_hooks_registered returns 'error' → overall 'error'."""
    home = str(tmp_path)
    reg = _load_register_hooks()
    with patch.object(reg, 'ensure_kimi_hooks_registered', return_value='error'):
        # install_harness_bridge may import via package path OR importlib
        # fallback; patch both the package attribute (if loadable) and force
        # the importlib path to use our patched module.
        import types
        import sys
        bridges = types.ModuleType('bridges')
        bridges_kimi = types.ModuleType('bridges.kimi')
        sys.modules['bridges'] = bridges
        sys.modules['bridges.kimi'] = bridges_kimi
        sys.modules['bridges.kimi.register_hooks'] = reg
        try:
            result = harnesses.install_harness_bridge(
                _app_dir(), 'kimi', home=home)
        finally:
            for k in ('bridges.kimi.register_hooks', 'bridges.kimi', 'bridges'):
                sys.modules.pop(k, None)
    assert result == 'error'


def test_ensure_kimi_hooks_registered_idempotent(tmp_path):
    home = str(tmp_path)
    reg = _load_register_hooks()
    assert reg.ensure_kimi_hooks_registered(home=home) == 'installed'
    assert reg.ensure_kimi_hooks_registered(home=home) == 'already'


def test_status_script_command_quotes_spaces():
    reg = _load_register_hooks()
    cmd = reg.status_script_command('/tmp/home with spaces')
    assert 'projectman-status.py' in cmd
    # Path portion must be shell-safe (shlex.quote wraps spaces).
    assert "home with spaces" in cmd
    assert "'" in cmd or '"' in cmd
