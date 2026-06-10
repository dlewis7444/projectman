# ProjectMan status bridge for Grok Build

This bridge lights up ProjectMan's sidebar status dots for [Grok Build](https://x.ai/cli)
(xAI's terminal coding CLI, binary `grok`) sessions, exactly the way
`hooks/hook.js` does for Claude Code. It is the grok half of ProjectMan's
**status contract**.

Two files, unlike the single-file opencode plugin:

| File | Role |
|---|---|
| `projectman.json` | the grok **hook definition** — registers the lifecycle events and points each at the status script |
| `projectman-status.py` | the executable **python3** status writer that the hook runs |

It is **python3, not node** (node is not guaranteed on a grok-only machine;
python3 already is — ProjectMan requires it). Zero new dependencies, stdlib only.

## What it does

It maps grok's lifecycle hook events onto ProjectMan's per-cwd status schema and
writes one JSON file per working directory under `~/.ProjectMan/status/`:

```json
{ "state": "working" | "waiting" | "done", "event": "...", "cwd": "...", "ts": 0, "session": "..." }
```

`idle` / `stopped` are not written here — ProjectMan derives them from process
and (where used) zellij liveness.

| grok hook event (wire name) | ProjectMan state |
|---|---|
| `session_start`, `stop` | `done` |
| `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, `post_tool_use_failure` | `working` |
| `notification` | **ignored — never `waiting`** (see below) |
| `session_end` | (status file deleted) |

### Wire names are snake_case

The hook **definition** JSON (`projectman.json`) uses **PascalCase** keys
(`SessionStart`, `UserPromptSubmit`, `Stop`, …) because that is the shape grok's
hook loader expects. But the `GROK_HOOK_EVENT` environment variable and the
stdin payload's `hookEventName` are **snake_case** (`session_start`,
`user_prompt_submit`, `stop`, …). The status script keys its state machine off
the snake_case wire names. (Bench-probe finding, 2026-06-10.)

### Why `notification` is ignored, and the missing `waiting`

grok fires a `notification` hook event **after every other hook** — it is an
internal hook-execution receipt (`notificationType="xai_session"`, a
`HookExecution` report), **not** Claude's user-facing permission prompt. Mapping
it to `waiting` would flip the dot blue after every single event, so it is
deliberately ignored.

That leaves `waiting` **unmapped for now**: the real permission-prompt event
needs to be captured from a tool-use TUI turn (a follow-up mini-probe). Until
then the bridge ships with `waiting` disabled; the happy-path dots
(`working` while a turn runs, `done` when it stops) are correct and sufficient.
The TODO slot is marked in `projectman-status.py`.

### `session_end` does not fire headless

grok's `session_end` only fires on a TUI `/quit`, not in headless mode — so for
headless grok the status file lingers until ProjectMan's liveness check clears
it, exactly as for Claude. The `session_end` → file-remove branch handles the
TUI case where it does fire.

### Slug + empty-slug guard

The bridge uses the **same** cwd→filename slug rule as `hooks/hook.js` and the
opencode bridge (all writers share `~/.ProjectMan/status/`). A documented
collision hazard (M-P3.4: `.` and `/` both map to `-`) is pending its own
cross-writer design round; until then the rule stays byte-identical across all
three writers. An empty-slug guard skips all writes rather than emit a bare
`.json` (cwd `/` or empty).

## Claude-compat: double-fire prevention

grok reads `~/.claude/settings.json` hooks **by default**, so Claude's `hook.js`
would *also* fire on grok events and fight this bridge for the dot. `install.sh`
sets `[compat.claude] hooks = false` in `~/.grok/config.toml` (idempotent,
preserving every existing user key) so this bridge is the **sole** status writer
for grok sessions.

## Install

`install.sh` installs `projectman.json` + `projectman-status.py` into
`~/.grok/hooks/` for you (idempotent) and flips `[compat.claude] hooks = false`.
To install by hand:

```sh
mkdir -p ~/.grok/hooks
cp bridges/grok/projectman.json     ~/.grok/hooks/
cp bridges/grok/projectman-status.py ~/.grok/hooks/
chmod +x ~/.grok/hooks/projectman-status.py
# then set [compat.claude] hooks = false in ~/.grok/config.toml
```

grok loads hooks from `~/.grok/hooks/` automatically on its next launch.

## Selftest

`python3 bridges/grok/selftest.py` runs the real status script against synthetic
event sequences (the observed happy path, the `notification` no-write proof,
unknown-event no-ops, the empty-slug guard) and prints a pass/fail tally. It is a
dev/CI artifact and is not installed.

## Status directory

The bridge writes to `~/.ProjectMan/status/` (the agent-neutral location).
ProjectMan's `StatusWatcher` watches that directory and, during the deprecation
window, also the legacy Claude location `~/.claude/projectman/status/`.
