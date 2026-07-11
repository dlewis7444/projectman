#!/usr/bin/env python3
# projectman-status.py — ProjectMan status bridge for Grok Build (xAI `grok`).
#
# Installed to ~/.grok/hooks/ (by install.sh, idempotently) and invoked by grok
# for each hook event registered in projectman.json. It is the grok half of
# ProjectMan's *status contract*: it translates grok's lifecycle hook events
# into the same per-cwd JSON schema that Claude Code's hook.js and the opencode
# bridge write, so the sidebar status dots light up identically for any harness.
#
# python3, not node (F3): node is NOT guaranteed on a grok-only machine, but
# python3 already is (ProjectMan requires it). Zero new deps — stdlib only.
#
# Schema written (one file per cwd-slug under ~/.ProjectMan/status/):
#   { "state": "working"|"waiting"|"done", "event", "cwd", "ts", "session" }
# (idle/stopped are derived by ProjectMan from process/zellij liveness; grok's
#  session_end does NOT fire headless — probe Q7 — so cleanup stays
#  liveness-based exactly as it is for claude.)
#
# WIRE NAMES ARE snake_case (probe Q7 / surprise #5): the hook DEFINITION JSON
# uses PascalCase keys, but GROK_HOOK_EVENT and the stdin payload's
# `hookEventName` are snake_case (`session_start`, `user_prompt_submit`, `stop`,
# `notification`, `session_end`). This state map keys off the snake_case names.
#
# Ported 1:1 from hooks/hook.js's event→state mapping, onto grok's snake_case
# event set + grok's env contract (cwd from GROK_WORKSPACE_ROOT, session from
# GROK_SESSION_ID).
import json
import os
import sys

STATUS_DIR = os.path.join(os.path.expanduser("~"), ".ProjectMan", "status")

# Event → state, snake_case wire names (probe Q7 + mini-probe observed values).
# Ports hook.js's STATE map: SessionStart/Stop→done, UserPromptSubmit and the
# tool events→working. The happy path (session_start → user_prompt_submit →
# stop) drives done → working → done; a real turn shows `working` while it runs
# and `done` when it stops.
STATE = {
    "session_start": "done",
    "stop": "done",
    # Newer grok builds may emit agent_stop / session_stop aliases.
    "agent_stop": "done",
    "session_stop": "done",
    "user_prompt_submit": "working",
    "pre_tool_use": "working",
    "post_tool_use": "working",
    "post_tool_use_failure": "working",
    # `permission_denied` is an OUTCOME event — it fires on the deny ACTION
    # (mini-probe leg 2c), not while waiting; `stop(cancelled)` follows ~30ms
    # later and lands `done`. Mapping it to working clears the pre-tool phase
    # stamp (every non-pre_tool_use write rewrites the file phase-less).
    "permission_denied": "working",
    # NO direct `waiting` mapping exists — and none CAN (F11): the mini-probe
    # found grok's hook wire goes SILENT while the permission prompt is on
    # screen (54s/21s observed gaps; v0.2.39 has no fires-at-prompt event).
    # `notification` is NOT it either — it is an INTERNAL hook-receipt
    # (notificationType="xai_session", fired after every other hook; probe Q7,
    # surprise #4); mapping it to waiting would flip the dot blue after every
    # event, so it stays deliberately absent from this map and is ignored
    # below. Instead, `waiting` is INFERRED laptop-side: this script
    # phase-stamps pre_tool_use (below) and ProjectMan's StatusWatcher
    # promotes working→waiting once the phase ages past 5s of wire silence.
}

# F11 phase-stamping: `pre_tool_use` fires BEFORE the permission prompt
# (mini-probe), so it maps to working AND stamps `phase`/`phase_ts` into the
# status payload — the silence-age signal StatusWatcher promotes to waiting
# after 5s. Every OTHER state-writing event (post_tool_use,
# post_tool_use_failure, permission_denied, stop, ...) rewrites the file
# WITHOUT phase fields, which clears the stamp.
# ACCEPTED LIMITATION (F11, also in the README): a long-running APPROVED tool
# ages past 5s too and shows a transient false 'waiting', self-correcting the
# moment post_tool_use lands — for PM's purpose a false "needs you" beats a
# silently stalled session, and grok offers no fires-at-prompt event to do
# better.
PHASE_STAMP_EVENT = "pre_tool_use"

# `session_end` → remove the status file (cleanup). Mirrors hook.js. NOTE the
# probe found session_end does NOT fire in headless mode (Q7), so for headless
# grok the file lingers until ProjectMan's liveness check clears it — same as
# claude. This branch handles the TUI /quit case where session_end does fire.
CLEANUP_EVENT = "session_end"


def slug_for(cwd):
    """Same slug rule as Claude's hook.js and the opencode bridge so a cwd maps
    to one stable filename across all three writers.

    KNOWN COLLISION (M-P3.4, deferred): mapping both '.' and '/' to '-'
    conflates `/p/a.b` with `/p/a/b` (same filename -> sibling projects clobber
    each other's dot). A collision-safe scheme is a cross-writer contract change
    (this script + hook.js + the opencode bridge + StatusWatcher must move in
    lockstep), so it has its own design round; until then this MUST stay
    byte-identical to hook.js's rule (all writers share the status dir).
    """
    out = []
    for ch in cwd:
        out.append("-" if ch in "/." else ch)
    slug = "".join(out).lstrip("-")
    return slug


def main():
    # Read the event JSON from stdin (the payload); tolerate empty/garbage.
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}

    # Event name: GROK_HOOK_EVENT (snake_case) is authoritative; fall back to the
    # stdin payload's hookEventName for robustness.
    event_name = (os.environ.get("GROK_HOOK_EVENT")
                  or payload.get("hookEventName") or "")

    # cwd: GROK_WORKSPACE_ROOT is the project root grok ran in (probe Q7); the
    # stdin payload also carries `workspaceRoot`/`cwd` — prefer the env, fall
    # back to the payload.
    cwd = (os.environ.get("GROK_WORKSPACE_ROOT")
           or payload.get("workspaceRoot") or payload.get("cwd") or "")
    # ProjectMan remote fix: if grok still reports bare $HOME as workspace but
    # the process cwd is a ProjectMan project tree, use the project path so
    # status files match sidebar projects (otherwise dots stick yellow/grey).
    try:
        pwd = os.getcwd()
    except OSError:
        pwd = ""
    home = os.path.expanduser("~")
    marker = "/.ProjectMan/projects/"
    pwd_n = pwd.replace("\\", "/")
    cwd_n = (cwd or "").replace("\\", "/").rstrip("/")
    home_n = home.replace("\\", "/").rstrip("/")
    if marker in pwd_n and (not cwd_n or cwd_n == home_n):
        cwd = pwd
    if not cwd:
        return

    # session: GROK_SESSION_ID (probe Q7), with the payload's sessionId fallback.
    session = (os.environ.get("GROK_SESSION_ID")
               or payload.get("sessionId") or "")

    slug = slug_for(cwd)
    if not slug:
        # Empty slug means cwd degenerated to "/" or "" — a bare ".json" would
        # be unmappable garbage. Never write/remove it (same guard the opencode
        # bridge carries).
        return
    slug_path = os.path.join(STATUS_DIR, slug + ".json")

    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
    except OSError:
        return

    if event_name == CLEANUP_EVENT:
        try:
            os.unlink(slug_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return

    state = STATE.get(event_name)
    if not state:
        # Unknown events AND `notification` (the internal receipt) fall through
        # here: no write, no file touch.
        return

    import time
    now = int(time.time())
    status = {
        "state": state,
        "event": event_name,
        "cwd": cwd,
        "ts": now,
        "session": session,
    }
    if event_name == PHASE_STAMP_EVENT:
        # F11: arm the watcher-side waiting timer. Only pre_tool_use stamps;
        # every other event's write omits these keys (the clear).
        status["phase"] = "pre_tool_use"
        status["phase_ts"] = now
    tool = payload.get("toolName") or payload.get("tool_name")
    if tool:
        status["tool"] = tool

    try:
        with open(slug_path, "w") as f:
            json.dump(status, f)
    except OSError:
        # Never throw out of a hook.
        return


if __name__ == "__main__":
    main()
