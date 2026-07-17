"""Remote status bridge install helpers (no live SSH required)."""
import base64
import json
import os

from remote_hooks import _collect_bridge_files, _remote_install_python


def test_collect_bridge_files_includes_hook_and_plugins():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = _collect_bridge_files(root)
    assert '.claude/projectman/hook.js' in files
    assert files['.claude/projectman/hook.js'][0].startswith(b'#!/usr/bin/env node')
    assert '.config/opencode/plugins/projectman.js' in files
    assert '.grok/hooks/projectman-status.py' in files
    assert '.grok/hooks/projectman.json' in files
    # Kimi status bridge + pure register_hooks helper for remote [[hooks]] merge.
    assert '.kimi-code/hooks/projectman-status.py' in files
    content, executable = files['.kimi-code/hooks/projectman-status.py']
    assert executable is True
    assert b'projectman' in content.lower() or b'SessionStart' in content
    assert '.kimi-code/hooks/register_hooks.py' in files
    reg, reg_exec = files['.kimi-code/hooks/register_hooks.py']
    assert reg_exec is False
    assert b'ensure_kimi_hooks_registered' in reg
    assert b'merge_kimi_hooks' in reg


def test_remote_install_script_is_valid_python():
    src = _remote_install_python('')
    compile(src, '<remote_install>', 'exec')


def test_remote_install_script_registers_kimi_hooks(tmp_path, monkeypatch):
    """Drive the remote install python against a fake HOME (no SSH)."""
    import base64
    import json
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = _collect_bridge_files(root)
    assert '.kimi-code/hooks/projectman-status.py' in files
    assert '.kimi-code/hooks/register_hooks.py' in files
    manifest = {
        rel: {
            'b64': base64.b64encode(content).decode('ascii'),
            'exec': bool(executable),
        }
        for rel, (content, executable) in files.items()
    }
    payload = base64.b64encode(
        json.dumps(manifest).encode('utf-8')
    ).decode('ascii')
    script = _remote_install_python('')
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    # Also clear XDG so nothing leaks from the real user.
    env.pop('XDG_CONFIG_HOME', None)
    proc = subprocess.run(
        [sys.executable, '-c', script, payload],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.strip().startswith('OK')
    status = tmp_path / '.kimi-code' / 'hooks' / 'projectman-status.py'
    assert status.is_file()
    assert os.access(status, os.X_OK)
    cfg = tmp_path / '.kimi-code' / 'config.toml'
    assert cfg.is_file()
    text = cfg.read_text()
    assert 'projectman-status.py' in text
    assert 'SessionStart' in text
    assert '[[hooks]]' in text


def test_payload_roundtrip_structure():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = _collect_bridge_files(root)
    manifest = {
        rel: {
            'b64': base64.b64encode(content).decode('ascii'),
            'exec': bool(executable),
        }
        for rel, (content, executable) in files.items()
    }
    payload = base64.b64encode(
        json.dumps(manifest).encode('utf-8')
    ).decode('ascii')
    decoded = json.loads(base64.b64decode(payload))
    assert '.claude/projectman/hook.js' in decoded
    raw = base64.b64decode(decoded['.claude/projectman/hook.js']['b64'])
    assert b'SessionStart' in raw
