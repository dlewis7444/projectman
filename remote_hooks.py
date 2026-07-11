"""Install ProjectMan status bridges on a remote host over SSH.

Idempotent and no-mess-up oriented:
  * Writes only PM-owned files under ``~/.claude/projectman/``,
    ``~/.ProjectMan/status/``, ``~/.config/opencode/plugins/``,
    ``~/.grok/hooks/``.
  * Registers Claude hooks only when the command is not already present
    (same fingerprint as install.sh: ``projectman/hook.js``).
  * Does not rewrite unrelated settings keys.

Pure network I/O via ``ssh_transport.run_ssh``; no GTK.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
from typing import TYPE_CHECKING

from ssh_transport import build_ssh_base_argv, run_ssh

if TYPE_CHECKING:
    from hosts import HostProfile

# Claude events matching install.sh / hooks/hook.js
_CLAUDE_HOOK_EVENTS = (
    'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'UserPromptSubmit',
    'PermissionRequest', 'Notification', 'Stop', 'SessionStart', 'SessionEnd',
)

_HOOK_CMD = 'node ~/.claude/projectman/hook.js'


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _grok_json_absolute(text: str) -> str:
    """Rewrite portable ~ command to absolute $HOME path (F12b spirit)."""
    # Remote installer expands $HOME at write time in the python payload.
    return text.replace(
        'python3 ~/.grok/hooks/projectman-status.py',
        'python3 $HOME/.grok/hooks/projectman-status.py',
    )


def _collect_bridge_files(app_dir: str | None = None) -> dict[str, tuple[bytes, bool]]:
    """Map home-relative dest → (content, executable)."""
    root = app_dir or _app_dir()
    files: dict[str, tuple[bytes, bool]] = {}

    hook = _read_bytes(os.path.join(root, 'hooks', 'hook.js'))
    if hook is not None:
        files['.claude/projectman/hook.js'] = (hook, False)

    oc = _read_bytes(os.path.join(root, 'bridges', 'opencode', 'projectman.js'))
    if oc is not None:
        files['.config/opencode/plugins/projectman.js'] = (oc, False)

    gstat = _read_bytes(os.path.join(root, 'bridges', 'grok', 'projectman-status.py'))
    if gstat is not None:
        files['.grok/hooks/projectman-status.py'] = (gstat, True)

    gj = _read_bytes(os.path.join(root, 'bridges', 'grok', 'projectman.json'))
    if gj is not None:
        # Leave ~ form; remote python rewrites using actual HOME.
        files['.grok/hooks/projectman.json'] = (gj, False)

    return files


def _remote_install_python(payload_b64: str) -> str:
    """Python script run on the remote to write files + register Claude hooks."""
    # payload: base64(json({relpath: {b64, exec: bool}}))
    return r'''
import base64, json, os, stat, sys
from pathlib import Path

raw = base64.b64decode(sys.argv[1])
manifest = json.loads(raw.decode("utf-8"))
home = Path.home()
(home / ".ProjectMan" / "status").mkdir(parents=True, exist_ok=True)
written = []
for rel, meta in manifest.items():
    data = base64.b64decode(meta["b64"])
    dest = home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Grok JSON: rewrite ~ to absolute home for the status script command.
    if rel.endswith("projectman.json") and b"projectman-status.py" in data:
        text = data.decode("utf-8")
        abs_cmd = "python3 " + str(home / ".grok/hooks/projectman-status.py")
        text = text.replace(
            "python3 ~/.grok/hooks/projectman-status.py", abs_cmd
        )
        data = text.encode("utf-8")
    same = dest.is_file() and dest.read_bytes() == data
    if not same:
        dest.write_bytes(data)
        written.append(rel)
    if meta.get("exec"):
        mode = dest.stat().st_mode
        if not (mode & stat.S_IXUSR):
            dest.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if rel not in written:
                written.append(rel + "+x")

# Claude settings.json — add ProjectMan hooks if missing (idempotent).
settings_path = home / ".claude" / "settings.json"
settings_path.parent.mkdir(parents=True, exist_ok=True)
try:
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text() or "{}")
    else:
        settings = {}
except Exception:
    settings = {}
if not isinstance(settings, dict):
    settings = {}
hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}
    settings["hooks"] = hooks
cmd = "node ~/.claude/projectman/hook.js"
events = [
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
    "PermissionRequest", "Notification", "Stop", "SessionStart", "SessionEnd",
]
changed = False
for ev in events:
    entries = hooks.get(ev)
    if not isinstance(entries, list):
        entries = []
        hooks[ev] = entries
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks") or []:
            if isinstance(h, dict) and "projectman/hook.js" in str(h.get("command", "")):
                found = True
                break
        if found:
            break
    if not found:
        entries.append({"hooks": [{"type": "command", "command": cmd}]})
        changed = True
if changed:
    # Backup existing once.
    if settings_path.is_file():
        bak = settings_path.with_suffix(settings_path.suffix + ".bak.projectman")
        if not bak.exists():
            bak.write_bytes(settings_path.read_bytes())
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    written.append(".claude/settings.json")

# Grok: [compat.claude] hooks = false so only ~/.grok/hooks/projectman.json
# drives status (mirrors bridges/grok/compat_toml.py).
cfg = home / ".grok" / "config.toml"
try:
    text = cfg.read_text() if cfg.is_file() else ""
except OSError:
    text = ""
lines = text.splitlines() if text else []
out = []
in_sec = False
sec_seen = False
hooks_set = False
toml_changed = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_sec and not hooks_set:
            out.append("hooks = false")
            hooks_set = True
            toml_changed = True
        in_sec = stripped == "[compat.claude]"
        if in_sec:
            sec_seen = True
        out.append(line)
        continue
    if in_sec and stripped.startswith("hooks"):
        if "false" not in stripped.split("=")[-1]:
            out.append("hooks = false")
            toml_changed = True
        else:
            out.append(line)
        hooks_set = True
        continue
    out.append(line)
if in_sec and not hooks_set:
    out.append("hooks = false")
    toml_changed = True
if not sec_seen:
    if out and out[-1].strip():
        out.append("")
    out.append("[compat.claude]")
    out.append("hooks = false")
    toml_changed = True
if toml_changed:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("\n".join(out) + ("\n" if out else ""))
    written.append(".grok/config.toml")

print("OK " + (",".join(written) if written else "already"))
'''


def install_remote_status_bridges(
    profile: 'HostProfile',
    *,
    app_dir: str | None = None,
    timeout: float = 45,
) -> tuple[bool, str]:
    """Install Claude/OpenCode/Grok status bridges on *profile* via SSH.

    Returns ``(ok, message)``.
    """
    files = _collect_bridge_files(app_dir)
    if not files:
        return False, 'no local bridge sources found'

    manifest = {}
    for rel, (content, executable) in files.items():
        manifest[rel] = {
            'b64': base64.b64encode(content).decode('ascii'),
            'exec': bool(executable),
        }
    payload = base64.b64encode(
        json.dumps(manifest, separators=(',', ':')).encode('utf-8')
    ).decode('ascii')

    # Payload is argv[1] to the remote python -c script (small: hook + plugins).
    remote = (
        'python3 -c ' + shlex.quote(_remote_install_python(''))
        + ' ' + shlex.quote(payload)
    )
    argv = build_ssh_base_argv(profile.ssh_target) + [remote]
    rc, out, err = run_ssh(argv, timeout=timeout)
    text = (out or err or '').strip()
    if rc != 0:
        return False, text or f'ssh failed (rc={rc})'
    if text.startswith('OK'):
        return True, text
    return True, text or 'installed'


def ensure_remote_status_ready(
    profile: 'HostProfile',
    *,
    app_dir: str | None = None,
    timeout: float = 45,
) -> tuple[bool, str]:
    """mkdir status dir + install bridges. Used when rich-status is turned on."""
    from remote_store import ensure_remote_projects_dir
    # projects dir is unrelated but ensures ~/.ProjectMan exists
    ok, err = ensure_remote_projects_dir(profile, timeout=min(timeout, 20))
    if not ok:
        return False, err or 'could not ensure remote projects dir'
    return install_remote_status_bridges(
        profile, app_dir=app_dir, timeout=timeout,
    )
