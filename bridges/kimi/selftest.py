#!/usr/bin/env python3
# selftest.py — synthetic, deterministic harness for the kimi status bridge
# (projectman-status.py). Plain python3 stdlib, no kimi, no network.
#
#   python3 bridges/kimi/selftest.py
#
# Exit 0 iff every check passes; nonzero on any failure.
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "projectman-status.py")

SANDBOX = tempfile.mkdtemp(prefix="pm-kimi-selftest-")
STATUS_DIR = os.path.join(SANDBOX, ".ProjectMan", "status")

PROJECT_CWD = "/home/user/.ProjectMan/projects/verify-kimi"
SESSION_ID = "session_1164f4be-3267-4552-b97c-8f9fe0ddf1ab"


def _slug_for(cwd):
    out = []
    for ch in cwd:
        out.append("-" if ch in "/." else ch)
    return "".join(out).lstrip("-")


def status_path_for(cwd=PROJECT_CWD):
    return os.path.join(STATUS_DIR, _slug_for(cwd) + ".json")


def emit(event, cwd=PROJECT_CWD, session=SESSION_ID, payload=None,
         use_snake=False):
    """Run the real bridge script once with a synthetic kimi hook invocation."""
    env = dict(os.environ)
    env["HOME"] = SANDBOX
    body = dict(payload or {})
    if use_snake:
        body.setdefault("hook_event_name", event)
    else:
        body.setdefault("hook_event_name", event)
    body.setdefault("session_id", session or "")
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


SENTINEL_EVENT = "__SENTINEL_DONE__"


def seed_sentinel_done(cwd=PROJECT_CWD):
    os.makedirs(STATUS_DIR, exist_ok=True)
    with open(status_path_for(cwd), "w") as f:
        json.dump({"state": "done", "event": SENTINEL_EVENT, "cwd": cwd,
                   "ts": 1, "session": "seed"}, f)


def sentinel_intact(cwd=PROJECT_CWD):
    s = read_state(cwd)
    return bool(s) and s.get("event") == SENTINEL_EVENT and s.get("state") == "done"


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
    # Happy path: SessionStart -> UserPromptSubmit -> Stop
    clear_status_file()
    emit("SessionStart")
    check("HAPPY step1 SessionStart -> done",
          read_state() and read_state()["state"] == "done")

    emit("UserPromptSubmit", payload={"prompt": "ping"})
    check("HAPPY step2 UserPromptSubmit -> working",
          read_state()["state"] == "working")

    emit("Stop")
    check("HAPPY step3 Stop -> done", read_state()["state"] == "done")

    # PermissionRequest -> waiting (kimi's advantage over grok)
    clear_status_file()
    emit("PermissionRequest", payload={"tool_name": "Bash"})
    s = read_state()
    check("MAP PermissionRequest -> waiting", s and s["state"] == "waiting")
    check("MAP PermissionRequest carries tool", s.get("tool") == "Bash")

    emit("PermissionResult")
    check("MAP PermissionResult -> working", read_state()["state"] == "working")

    # Tool events
    clear_status_file()
    emit("PreToolUse", payload={"tool_name": "Read"})
    check("MAP PreToolUse -> working", read_state()["state"] == "working")

    clear_status_file()
    emit("PostToolUse")
    check("MAP PostToolUse -> working", read_state()["state"] == "working")

    clear_status_file()
    emit("PostToolUseFailure")
    check("MAP PostToolUseFailure -> working",
          read_state()["state"] == "working")

    clear_status_file()
    emit("StopFailure")
    check("MAP StopFailure -> done", read_state()["state"] == "done")

    clear_status_file()
    emit("Interrupt")
    check("MAP Interrupt -> done", read_state()["state"] == "done")

    # snake_case aliases
    clear_status_file()
    emit("user_prompt_submit")
    check("ALIAS user_prompt_submit -> working",
          read_state()["state"] == "working")

    clear_status_file()
    emit("permission_request")
    check("ALIAS permission_request -> waiting",
          read_state()["state"] == "waiting")

    # Notification ignored
    clear_status_file()
    emit("Notification")
    check("NOTIF Notification -> no file touch", not file_exists())

    seed_sentinel_done()
    emit("Notification")
    check("NOTIF mid-turn Notification -> sentinel intact", sentinel_intact())

    # SessionEnd deletes
    clear_status_file()
    emit("UserPromptSubmit")
    check("DEL precondition file exists", file_exists())
    emit("SessionEnd")
    check("DEL SessionEnd -> file removed", not file_exists())

    clear_status_file()
    emit("SessionEnd")
    check("DEL SessionEnd with no file -> no crash", not file_exists())

    # Unknown no-op
    clear_status_file()
    emit("TotallyUnknownEvent")
    check("ROBUST unknown event -> no file touch", not file_exists())

    # Empty slug guard
    root_slug_file = os.path.join(STATUS_DIR, _slug_for("/") + ".json")
    try:
        os.unlink(root_slug_file)
    except OSError:
        pass
    emit("UserPromptSubmit", cwd="/")
    check("GUARD empty-slug -> no '.json' write",
          not os.path.exists(root_slug_file))

    clear_status_file()
    emit("UserPromptSubmit", cwd="")
    check("GUARD no cwd -> no file touch", not file_exists())

    # Session field
    clear_status_file()
    emit("UserPromptSubmit", session="sess-xyz")
    check("FIELD session from session_id",
          read_state()["session"] == "sess-xyz")

    # Hook registration pure tests
    sys.path.insert(0, HERE)
    from register_hooks import (
        merge_kimi_hooks, parse_hooks_blocks, KIMI_HOOK_EVENTS,
        ensure_kimi_hooks_registered, kimi_hooks_are_registered,
        status_script_command,
    )

    cmd = status_script_command(SANDBOX)
    empty_merged = merge_kimi_hooks("", cmd)
    blocks = parse_hooks_blocks(empty_merged)
    check("HOOKS empty merge produces all events",
          len(blocks) == len(KIMI_HOOK_EVENTS))
    check("HOOKS all events present",
          {b["event"] for b in blocks} == set(KIMI_HOOK_EVENTS))
    check("HOOKS all marked PM", all(b["is_pm"] for b in blocks))

    # Idempotent
    again = merge_kimi_hooks(empty_merged, cmd)
    check("HOOKS merge idempotent (byte-identical)", again == empty_merged)

    # Preserve user content
    user = (
        'default_model = "kimi-code/kimi-for-coding"\n'
        '\n'
        '[thinking]\n'
        'enabled = true\n'
        '\n'
        '[[hooks]]\n'
        'event = "UserPromptSubmit"\n'
        'command = "echo user-hook"\n'
    )
    merged = merge_kimi_hooks(user, cmd)
    check("HOOKS preserves user non-PM hook", "echo user-hook" in merged)
    check("HOOKS preserves default_model",
          'default_model = "kimi-code/kimi-for-coding"' in merged)
    check("HOOKS preserves [thinking]", "[thinking]" in merged)
    pm_blocks = [b for b in parse_hooks_blocks(merged) if b["is_pm"]]
    check("HOOKS adds all PM events alongside user hook",
          len(pm_blocks) == len(KIMI_HOOK_EVENTS))

    # ensure on disk
    result = ensure_kimi_hooks_registered(home=SANDBOX)
    check("HOOKS ensure_kimi_hooks_registered installs", result == "installed")
    result2 = ensure_kimi_hooks_registered(home=SANDBOX)
    check("HOOKS ensure is idempotent", result2 == "already")
    check("HOOKS kimi_hooks_are_registered True after ensure",
          kimi_hooks_are_registered(home=SANDBOX))

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
