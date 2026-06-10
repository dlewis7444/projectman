#!/usr/bin/env python3
# selftest.py — synthetic, deterministic harness for the grok status bridge
# (projectman-status.py). Plain python3 stdlib, no grok, no network.
#
#   python3 bridges/grok/selftest.py
#
# Exit 0 iff every check passes; nonzero on any failure. Prints a per-check
# tally. install.sh copies projectman.json + projectman-status.py into grok's
# hook dir; this file never reaches grok — it is a dev/CI artifact only (the
# standing harness rule: harnesses don't evaporate).
#
# HOW IT DRIVES THE BRIDGE: projectman-status.py reads its event from
# GROK_HOOK_EVENT + stdin JSON and its cwd/session from GROK_WORKSPACE_ROOT /
# GROK_SESSION_ID. So the harness runs the REAL script as a subprocess with a
# synthetic env + stdin per step, pointing HOME at a throwaway sandbox so the
# script's STATUS_DIR resolves inside it. Each step asserts the resulting
# status file's state (or its absence / non-modification).
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "projectman-status.py")

SANDBOX = tempfile.mkdtemp(prefix="pm-grok-selftest-")
STATUS_DIR = os.path.join(SANDBOX, ".ProjectMan", "status")

PROJECT_CWD = "/home/user/.ProjectMan/projects/verify-grok"
SESSION_ID = "019eb297-fa74-7741-863e-d8aa822ac7bf"  # a real UUIDv7 from the probe


def _slug_for(cwd):
    out = []
    for ch in cwd:
        out.append("-" if ch in "/." else ch)
    return "".join(out).lstrip("-")


def status_path_for(cwd=PROJECT_CWD):
    return os.path.join(STATUS_DIR, _slug_for(cwd) + ".json")


def emit(event, cwd=PROJECT_CWD, session=SESSION_ID, payload=None,
         workspace_root=None, drop_event_env=False):
    """Run the real bridge script once with a synthetic grok hook invocation."""
    env = dict(os.environ)
    env["HOME"] = SANDBOX
    if not drop_event_env:
        env["GROK_HOOK_EVENT"] = event
    else:
        env.pop("GROK_HOOK_EVENT", None)
    if workspace_root is None:
        workspace_root = cwd
    if workspace_root is not None:
        env["GROK_WORKSPACE_ROOT"] = workspace_root
    else:
        env.pop("GROK_WORKSPACE_ROOT", None)
    if session is not None:
        env["GROK_SESSION_ID"] = session
    else:
        env.pop("GROK_SESSION_ID", None)
    body = dict(payload or {})
    body.setdefault("hookEventName", event)
    body.setdefault("sessionId", session or "")
    body.setdefault("workspaceRoot", workspace_root or "")
    body.setdefault("cwd", cwd or "")
    stdin = json.dumps(body)
    subprocess.run([sys.executable, SCRIPT], input=stdin, env=env,
                   text=True, timeout=10)


def clear_status_file(cwd=PROJECT_CWD):
    try:
        os.unlink(status_path_for(cwd))
    except OSError:
        pass


def read_state(cwd=PROJECT_CWD):
    p = status_path_for(cwd)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def file_exists(cwd=PROJECT_CWD):
    return os.path.exists(status_path_for(cwd))


# Sentinel done file whose `event` marker the bridge would never emit, so
# "notification changes NOTHING" is provable: if the bridge writes, `event`
# changes (the opencode selftest technique).
SENTINEL_EVENT = "__SENTINEL_DONE__"


def seed_sentinel_done(cwd=PROJECT_CWD):
    os.makedirs(STATUS_DIR, exist_ok=True)
    with open(status_path_for(cwd), "w") as f:
        json.dump({"state": "done", "event": SENTINEL_EVENT, "cwd": cwd,
                   "ts": 1, "session": "seed"}, f)


def sentinel_intact(cwd=PROJECT_CWD):
    s = read_state(cwd)
    return bool(s) and s.get("event") == SENTINEL_EVENT and s.get("state") == "done"


# ── tally ──────────────────────────────────────────────────────────────────
passed = 0
failed = 0
failures = []


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        failures.append(name)
        print(f"  FAIL  {name}")


def run():
    # =====================================================================
    # T-B3 HAPPY PATH: session_start -> user_prompt_submit -> stop drives
    # working* -> working -> done. (session_start is itself a `done` state in
    # the ported map — hook.js maps SessionStart->done — and a real turn shows
    # working at user_prompt_submit then done at stop.)
    # =====================================================================
    clear_status_file()
    emit("session_start", payload={"source": "new"})
    check("HAPPY step1 session_start -> done", read_state() and read_state()["state"] == "done")

    emit("user_prompt_submit", payload={"promptId": "p1",
         "prompt": "<user_query>\nReply with exactly: ok\n</user_query>"})
    check("HAPPY step2 user_prompt_submit -> working", read_state()["state"] == "working")

    # The internal `notification` receipt fires between user_prompt_submit and
    # stop (probe trace order). It must change NOTHING — seed a sentinel WHILE
    # working so a stray write is detectable.
    seed_sentinel_done()  # overwrite with sentinel to prove no-write
    emit("notification", payload={"notificationType": "xai_session",
         "level": "info", "message": "HookExecution { ... }"})
    check("HAPPY step3 notification -> no write (sentinel intact)", sentinel_intact())

    # Restore a working file, then stop -> done (the real turn end).
    clear_status_file()
    emit("user_prompt_submit", payload={"promptId": "p1"})
    emit("stop", payload={"promptId": "p1", "reason": "end_turn"})
    check("HAPPY step4 stop -> done", read_state()["state"] == "done")

    # =====================================================================
    # NOTIFICATION never writes — in every position, fresh or mid-turn.
    # =====================================================================
    clear_status_file()
    emit("notification", payload={"notificationType": "xai_session"})
    check("NOTIF fresh notification -> no file touch", not file_exists())

    clear_status_file()
    emit("user_prompt_submit", payload={"promptId": "p2"})
    check("NOTIF precondition working written", read_state()["state"] == "working")
    seed_sentinel_done()
    emit("notification", payload={"notificationType": "xai_session"})
    check("NOTIF mid-turn notification -> no write (sentinel intact)", sentinel_intact())

    # =====================================================================
    # MAPPING coverage: each mapped event -> its state.
    # =====================================================================
    clear_status_file()
    emit("stop", payload={"reason": "end_turn"})
    check("MAP stop -> done", read_state()["state"] == "done")

    clear_status_file()
    emit("session_start", payload={"source": "load"})
    check("MAP session_start(load) -> done", read_state()["state"] == "done")

    clear_status_file()
    emit("pre_tool_use", payload={"toolName": "Bash"})
    s = read_state()
    check("MAP pre_tool_use -> working", s["state"] == "working")
    check("MAP pre_tool_use carries tool name", s.get("tool") == "Bash")

    clear_status_file()
    emit("post_tool_use", payload={"toolName": "Read"})
    check("MAP post_tool_use -> working", read_state()["state"] == "working")

    clear_status_file()
    emit("post_tool_use_failure", payload={})
    check("MAP post_tool_use_failure -> working", read_state()["state"] == "working")

    # =====================================================================
    # SESSION_END -> file removed (cleanup; TUI /quit path).
    # =====================================================================
    clear_status_file()
    emit("user_prompt_submit", payload={})  # create the file
    check("DEL precondition file exists", file_exists())
    emit("session_end", payload={})
    check("DEL session_end -> file removed", not file_exists())

    # session_end with no existing file is harmless.
    clear_status_file()
    emit("session_end", payload={})
    check("DEL session_end with no file -> no crash, no file", not file_exists())

    # =====================================================================
    # UNKNOWN events no-op (no write, no crash).
    # =====================================================================
    clear_status_file()
    emit("totally_unknown_event", payload={})
    check("ROBUST unknown event -> no file touch", not file_exists())

    # =====================================================================
    # EMPTY-SLUG guard: cwd "/" -> empty slug -> write() and remove() no-ops.
    # =====================================================================
    root_slug_file = os.path.join(STATUS_DIR, _slug_for("/") + ".json")  # ".json"
    try:
        os.unlink(root_slug_file)
    except OSError:
        pass
    emit("user_prompt_submit", cwd="/", workspace_root="/", payload={})
    check("GUARD empty-slug -> no '.json' write", not os.path.exists(root_slug_file))

    # No cwd at all -> no write, no crash.
    clear_status_file()
    emit("user_prompt_submit", cwd="", workspace_root=None, payload={})
    # payload also carries empty cwd/workspaceRoot -> nothing written
    check("GUARD no cwd -> no file touch", not file_exists())

    # =====================================================================
    # ENV vs payload: event name from stdin payload when env var is absent.
    # =====================================================================
    clear_status_file()
    emit("stop", payload={"reason": "end_turn"}, drop_event_env=True)
    check("FALLBACK event name from stdin hookEventName -> done", read_state()["state"] == "done")

    # session field comes from GROK_SESSION_ID.
    clear_status_file()
    emit("user_prompt_submit", session="sess-xyz", payload={})
    check("FIELD session from GROK_SESSION_ID", read_state()["session"] == "sess-xyz")

    # ── done ──
    print("")
    print(f"Tally: {passed} passed, {failed} failed ({passed + failed} checks)")
    if failed:
        print("Failures: " + "; ".join(failures))
    return 1 if failed else 0


def main():
    try:
        rc = run()
    finally:
        import shutil
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
