#!/usr/bin/env python3
# register_hooks.py — idempotent [[hooks]] merge for Kimi Code config.toml.
#
# Kimi does NOT have a separate hooks JSON file (unlike grok). Hooks live as
# ``[[hooks]]`` array-of-tables in ``~/.kimi-code/config.toml``. This module
# surgically inserts one entry per ProjectMan-watched event, identified by a
# command path containing ``projectman-status.py``, without full-reserializing
# the file (preserves user comments/order).
#
# Pure + defensive; python3 stdlib only. Used by:
#   * harnesses.install_harness_bridge (kimi post-step)
#   * install.sh (via the same Python path)
#   * unit tests
import os
import re
import shlex

# Events ProjectMan cares about (PascalCase — Kimi config Event Reference).
KIMI_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionResult",
    "Stop",
    "StopFailure",
    "Interrupt",
    "SessionEnd",
)

_MARKER = "projectman-status.py"
_HOOKS_HEADER_RE = re.compile(r"^\s*\[\[hooks\]\]\s*(#.*)?$")
_EVENT_RE = re.compile(r'^\s*event\s*=\s*"([^"]*)"\s*(#.*)?$')
# Accept double- or single-quoted TOML strings for command (we emit single-
# quoted when the path needs shell-safe quoting, double-quoted otherwise).
_COMMAND_RE = re.compile(
    r"""^\s*command\s*=\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')\s*(#.*)?$"""
)
_ANY_SECTION_RE = re.compile(r"^\s*\[")


def status_script_path(home):
    """Absolute path to the installed status bridge script."""
    return os.path.join(home, ".kimi-code", "hooks", "projectman-status.py")


def status_script_command(home):
    """Shell-safe ``python3 <script>`` for the installed status bridge.

    The script path is quoted via :func:`shlex.quote` so spaces / metacharacters
    in ``home`` cannot break the hook command line.
    """
    return f"python3 {shlex.quote(status_script_path(home))}"


def _toml_command_line(command):
    """Render a TOML ``command = …`` assignment.

    Common case (``python3 /abs/path`` with no ``"``/``\\``) uses double-quoted
    TOML — byte-compatible with pre-shlex installs so re-merge stays
    idempotent. When ``shlex.quote`` wraps a spaced path in single quotes the
    command still embeds fine inside double-quoted TOML. Only if the command
    itself contains ``"`` do we fall back to a TOML single-quoted literal.
    """
    if '"' not in command and "\\" not in command and "\n" not in command:
        return f'command = "{command}"'
    if "'" not in command and "\n" not in command:
        return f"command = '{command}'"
    # Both quote types present — escape for double-quoted TOML.
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'command = "{esc}"'


def _command_from_match(cm):
    """Extract command text from a ``_COMMAND_RE`` match (group 1 or 2)."""
    if cm.group(1) is not None:
        # Double-quoted: undo basic backslash escapes.
        return cm.group(1).replace('\\"', '"').replace("\\\\", "\\")
    # Single-quoted TOML literal: '' → '
    return (cm.group(2) or "").replace("''", "'")


def _is_pm_command(cmd):
    return isinstance(cmd, str) and _MARKER in cmd


def parse_hooks_blocks(text):
    """Return a list of ``{start, end, event, command, is_pm}`` for each
    ``[[hooks]]`` table in ``text``. ``end`` is exclusive line index.
    """
    if not text:
        return []
    lines = text.split("\n")
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        if _HOOKS_HEADER_RE.match(lines[i]) is None:
            i += 1
            continue
        start = i
        i += 1
        event = None
        command = None
        while i < n:
            if _ANY_SECTION_RE.match(lines[i]):
                break
            em = _EVENT_RE.match(lines[i])
            if em:
                event = em.group(1)
            cm = _COMMAND_RE.match(lines[i])
            if cm:
                command = _command_from_match(cm)
            i += 1
        blocks.append({
            "start": start,
            "end": i,
            "event": event,
            "command": command,
            "is_pm": _is_pm_command(command),
        })
    return blocks


def merge_kimi_hooks(text, command):
    """Return ``text`` with ProjectMan ``[[hooks]]`` entries ensured.

    * Identifies existing PM hooks by command containing ``projectman-status.py``.
    * Updates command path if it differs (e.g. home moved).
    * Adds missing events; removes duplicate PM entries for the same event.
    * Leaves non-PM hooks and all other content untouched.
    * Idempotent: re-running on already-correct content returns identical text.
    """
    if command is None:
        command = status_script_command(os.path.expanduser("~"))
    cmd_line = _toml_command_line(command)
    desired_events = list(KIMI_HOOK_EVENTS)
    desired_set = set(desired_events)

    if not text:
        parts = []
        for ev in desired_events:
            parts.append("[[hooks]]")
            parts.append(f'event = "{ev}"')
            parts.append(cmd_line)
            parts.append("")
        return "\n".join(parts)

    lines = text.split("\n")
    blocks = parse_hooks_blocks(text)

    covered = set()
    delete_ranges = []
    rewrite = {}

    for b in blocks:
        if not b["is_pm"]:
            continue
        ev = b["event"]
        if ev in desired_set and ev not in covered:
            covered.add(ev)
            if b["command"] != command:
                for li in range(b["start"] + 1, b["end"]):
                    if _COMMAND_RE.match(lines[li]):
                        rewrite[li] = cmd_line
                        break
        else:
            # Duplicate PM hook for same event, or orphan PM event → drop.
            delete_ranges.append((b["start"], b["end"]))

    changed = bool(rewrite) or bool(delete_ranges)
    missing = [ev for ev in desired_events if ev not in covered]
    if missing:
        changed = True

    if not changed:
        return text

    for li, new in rewrite.items():
        lines[li] = new

    for start, end in sorted(delete_ranges, reverse=True):
        drop_end = end
        if drop_end < len(lines) and lines[drop_end].strip() == "":
            drop_end += 1
        del lines[start:drop_end]

    if missing:
        if lines and lines[-1] == "":
            lines.pop()
        if lines and lines[-1].strip() != "":
            lines.append("")
        for ev in missing:
            lines.append("[[hooks]]")
            lines.append(f'event = "{ev}"')
            lines.append(cmd_line)
            lines.append("")
    return "\n".join(lines)


def kimi_hooks_are_registered(*, home=None, config_path=None, command=None):
    """True iff every KIMI_HOOK_EVENTS entry exists as a PM [[hooks]] block
    with the expected command path."""
    if home is None:
        home = os.path.expanduser("~")
    if command is None:
        command = status_script_command(home)
    path = config_path or os.path.join(home, ".kimi-code", "config.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    blocks = parse_hooks_blocks(text)
    found = {
        b["event"] for b in blocks
        if b["is_pm"] and b["event"] in KIMI_HOOK_EVENTS
        and b["command"] == command
    }
    return found >= set(KIMI_HOOK_EVENTS)


def ensure_kimi_hooks_registered(*, home=None, config_path=None):
    """Ensure config.toml has ProjectMan [[hooks]] entries.

    Returns ``'installed'`` | ``'already'`` | ``'error'``. Creates parent
    dirs and the config file when missing. Never raises.
    """
    if home is None:
        home = os.path.expanduser("~")
    path = config_path or os.path.join(home, ".kimi-code", "config.toml")
    command = status_script_command(home)
    try:
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except FileNotFoundError:
            existing = ""
        new = merge_kimi_hooks(existing, command)
        if new == existing:
            return "already"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        return "installed"
    except OSError:
        return "error"


def main():
    """CLI: ensure_kimi_hooks_registered for $HOME (or argv[1] as home)."""
    import sys
    home = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    print(ensure_kimi_hooks_registered(home=home))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
