#!/usr/bin/env python3
# projectman-status.py — ProjectMan status bridge for Kimi Code (Moonshot `kimi`).
#
# Installed to ~/.kimi-code/hooks/ (by install.sh / Settings → Install bridge)
# and registered as [[hooks]] entries in ~/.kimi-code/config.toml. Translates
# Kimi lifecycle hook events into the same per-cwd JSON schema that Claude's
# hook.js, the opencode bridge, and the grok bridge write, so sidebar status
# dots light up identically for any harness.
#
# python3 stdlib only — node is not assumed on a kimi-only machine.
#
# Schema written (one file per cwd-slug under ~/.ProjectMan/status/):
#   { "state": "working"|"waiting"|"done", "event", "cwd", "ts", "session" }
#
# Wire names: Kimi config uses PascalCase event names (docs Event Reference);
# stdin payload carries hook_event_name (often PascalCase, sometimes snake).
# This map accepts BOTH forms defensively.
#
# KNOWN COLLISION (M-P3.4, deferred): slug_for maps both '.' and '/' to '-'
# and must stay byte-identical to hook.js / grok / opencode (shared status dir).
import json
import os
import sys

STATUS_DIR = os.path.join(os.path.expanduser("~"), ".ProjectMan", "status")

# Event → state. Keys are lowercased for lookup (see normalize_event).
# PermissionRequest → waiting is the win over grok: Kimi fires this just
# before the approval prompt, so we map waiting directly (no phase-aging).
STATE = {
    "sessionstart": "done",
    "session_start": "done",
    "userpromptsubmit": "working",
    "user_prompt_submit": "working",
    "pretooluse": "working",
    "pre_tool_use": "working",
    "posttooluse": "working",
    "post_tool_use": "working",
    "posttoolusefailure": "working",
    "post_tool_use_failure": "working",
    "permissionrequest": "waiting",
    "permission_request": "waiting",
    "permissionresult": "working",
    "permission_result": "working",
    "stop": "done",
    "stopfailure": "done",
    "stop_failure": "done",
    "interrupt": "done",
}

# Tools whose PreToolUse means "parked on the user", not working. Kimi's
# AskUserQuestion (the "user poll" tool) opens the question UI and blocks the
# turn until the user answers; the plain map would show working (yellow) the
# whole time. PostToolUse (answer submitted) restores working via STATE.
WAITING_ON_USER_TOOLS = frozenset({"askuserquestion"})

# SessionEnd → remove the status file (cleanup).
CLEANUP_EVENTS = frozenset({
    "sessionend",
    "session_end",
})

# Notification is ignored (noise; same spirit as grok ignoring internal
# notification receipts). Not listed in STATE → falls through to no-op.


def slug_for(cwd):
    """Same slug rule as Claude's hook.js / grok / opencode bridges.

    KNOWN COLLISION (M-P3.4, deferred): mapping both '.' and '/' to '-'
    conflates `/p/a.b` with `/p/a/b`. Must stay byte-identical across writers.
    """
    out = []
    for ch in cwd:
        out.append("-" if ch in "/." else ch)
    return "".join(out).lstrip("-")


def normalize_event(name):
    """Lowercase + strip for map lookup; keep underscores as-is."""
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Event name: stdin hook_event_name is primary (docs); also accept
    # hookEventName / event and a KIMI_HOOK_EVENT env fallback for robustness.
    event_raw = (
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or os.environ.get("KIMI_HOOK_EVENT")
        or ""
    )
    event_name = normalize_event(event_raw)

    # cwd: docs guarantee stdin `cwd`; also accept workspaceRoot / env.
    cwd = (
        payload.get("cwd")
        or payload.get("workspaceRoot")
        or payload.get("workDir")
        or os.environ.get("KIMI_CWD")
        or ""
    )
    if not cwd:
        return

    session = (
        payload.get("session_id")
        or payload.get("sessionId")
        or os.environ.get("KIMI_SESSION_ID")
        or ""
    )

    slug = slug_for(cwd)
    if not slug:
        return
    slug_path = os.path.join(STATUS_DIR, slug + ".json")

    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
    except OSError:
        return

    if event_name in CLEANUP_EVENTS:
        try:
            os.unlink(slug_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return

    state = STATE.get(event_name)
    if not state:
        # Unknown + Notification: no write, no crash.
        return

    tool = (
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
    )
    if (event_name in ("pretooluse", "pre_tool_use")
            and isinstance(tool, str)
            and tool.strip().lower() in WAITING_ON_USER_TOOLS):
        state = "waiting"

    import time
    now = int(time.time())
    # Prefer a stable event label for the status file: original casing if given,
    # else the normalized key.
    event_label = str(event_raw).strip() if event_raw else event_name
    status = {
        "state": state,
        "event": event_label,
        "cwd": cwd,
        "ts": now,
        "session": session,
    }
    if tool:
        status["tool"] = tool

    try:
        with open(slug_path, "w") as f:
            json.dump(status, f)
    except OSError:
        return


if __name__ == "__main__":
    main()
