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


def test_collect_falls_back_to_live_claude_hook(tmp_path, monkeypatch):
    """Installed app trees that only ship bridges/ still need hook.js.

    Regression: rich-status remote install used app_dir without hooks/, so
    remotes got settings.json registered to a missing hook.js (MODULE_NOT_FOUND).
    """
    from remote_hooks import _collect_bridge_files

    # Fake installed tree: bridges only, no hooks/.
    app = tmp_path / 'projectman'
    (app / 'bridges' / 'opencode').mkdir(parents=True)
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text('// oc\n')
    (app / 'bridges' / 'grok').mkdir(parents=True)
    (app / 'bridges' / 'grok' / 'projectman-status.py').write_text('# g\n')
    (app / 'bridges' / 'grok' / 'projectman.json').write_text('{}\n')
    (app / 'bridges' / 'kimi').mkdir(parents=True)
    (app / 'bridges' / 'kimi' / 'projectman-status.py').write_text('# k\n')
    (app / 'bridges' / 'kimi' / 'register_hooks.py').write_text('# r\n')

    live = tmp_path / 'home' / '.claude' / 'projectman'
    live.mkdir(parents=True)
    hook_body = b'#!/usr/bin/env node\n// SessionStart fallback hook\n'
    (live / 'hook.js').write_bytes(hook_body)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))

    files = _collect_bridge_files(str(app))
    assert '.claude/projectman/hook.js' in files
    assert files['.claude/projectman/hook.js'][0] == hook_body
    assert '.config/opencode/plugins/projectman.js' in files


def test_install_remote_refuses_without_hook_js(tmp_path, monkeypatch):
    from hosts import HostProfile
    from remote_hooks import install_remote_status_bridges

    # Empty app dir → no sources at all after collect fails hook requirement.
    app = tmp_path / 'empty'
    app.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()

    prof = HostProfile(
        id='t',
        ssh_target='nobody@example.invalid',
        display_name='t',
        remote_projects_dir='~/.ProjectMan/projects',
        rich_status_opt_in=True,
    )
    ok, msg = install_remote_status_bridges(prof, app_dir=str(app), timeout=1)
    assert ok is False
    assert 'hook.js' in msg.lower() or 'no local bridge' in msg.lower()


def test_remote_install_skips_settings_without_hook_file(tmp_path):
    """If hook.js is not in the manifest, do not register Claude hooks."""
    import subprocess
    import sys
    from remote_hooks import _remote_install_python

    # Manifest without Claude hook — only a dummy file.
    manifest = {
        '.grok/hooks/projectman-status.py': {
            'b64': base64.b64encode(b'#!/usr/bin/env python3\n').decode('ascii'),
            'exec': True,
        }
    }
    payload = base64.b64encode(json.dumps(manifest).encode('utf-8')).decode('ascii')
    script = _remote_install_python('')
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    env.pop('XDG_CONFIG_HOME', None)
    proc = subprocess.run(
        [sys.executable, '-c', script, payload],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert 'WARN claude hook.js missing' in proc.stdout
    settings = tmp_path / '.claude' / 'settings.json'
    assert not settings.exists() or 'projectman/hook.js' not in settings.read_text()
