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


def test_remote_install_script_is_valid_python():
    src = _remote_install_python('')
    compile(src, '<remote_install>', 'exec')


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
