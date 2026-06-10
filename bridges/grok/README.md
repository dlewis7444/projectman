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

On `pre_tool_use` the payload additionally carries `"phase": "pre_tool_use"`
and `"phase_ts": <epoch>` — the watcher-side waiting inference's aging signal
(see below); every other state-writing event omits them (the clear).

`idle` / `stopped` are not written here — ProjectMan derives them from process
and (where used) zellij liveness.

| grok hook event (wire name) | ProjectMan state |
|---|---|
| `session_start`, `stop` | `done` |
| `user_prompt_submit`, `post_tool_use`, `post_tool_use_failure` | `working` |
| `pre_tool_use` | `working` **+ phase-stamp** (`phase`, `phase_ts` — arms the waiting inference, see below) |
| `permission_denied` | `working` (a deny *outcome*, not a waiting signal; `stop(cancelled)` follows ~30ms later) |
| (wire silence after `pre_tool_use`) | → ProjectMan promotes to **`waiting`** after 5s (watcher-side) |
| `notification` | **ignored — never `waiting`** (see below) |
| `session_end` | (status file deleted) |

### Wire names are snake_case

The hook **definition** JSON (`projectman.json`) uses **PascalCase** keys
(`SessionStart`, `UserPromptSubmit`, `Stop`, …) because that is the shape grok's
hook loader expects. But the `GROK_HOOK_EVENT` environment variable and the
stdin payload's `hookEventName` are **snake_case** (`session_start`,
`user_prompt_submit`, `stop`, …). The status script keys its state machine off
the snake_case wire names. (Bench-probe finding, 2026-06-10.)

### How `waiting` works — phase aging (F11)

Grok has **no fires-at-prompt hook event**: the tool-turn mini-probe observed
that the hook wire goes completely **silent while the permission prompt is on
screen** (54s and 21s gaps measured between `pre_tool_use` and the human's
decision). So `waiting` cannot be a direct event mapping — it is **inferred**:

1. `pre_tool_use` fires *before* the prompt; the bridge writes `working` plus a
   **phase stamp** (`"phase": "pre_tool_use", "phase_ts": <epoch>`).
2. If the tool runs (auto-approved or approved fast — observed completions in
   under 0.3s), `post_tool_use` lands and rewrites the file **without** the
   phase fields, clearing the stamp. `post_tool_use_failure`,
   `permission_denied`, `stop`, and a new `user_prompt_submit` clear it the
   same way.
3. If instead the wire stays silent, ProjectMan's `StatusWatcher` notices the
   phase has aged past **5 seconds** and promotes the dot working → `waiting`
   laptop-side (no extra processes; the watcher arms a one-shot timer at
   snapshot read).

**ACCEPTED LIMITATION (F11):** a long-running *approved* tool also crosses the
5s threshold, so its dot shows a transient false `waiting` until
`post_tool_use` lands and self-corrects it back to `working`. This is a
deliberate trade: for ProjectMan's purpose a false "needs you" beats a silently
stalled session, and grok v0.2.39 offers no fires-at-prompt event to do better.

`permission_denied` is a deny **outcome** (it fires on the deny action, not
while waiting); it maps to `working` and clears the phase — the
`stop(cancelled)` that follows ~30ms later lands `done`.

### Why `notification` is ignored

grok fires a `notification` hook event **after every other hook** — it is an
internal hook-execution receipt (`notificationType="xai_session"`, a
`HookExecution` report), **not** Claude's user-facing permission prompt. Mapping
it to `waiting` would flip the dot blue after every single event, so it is
deliberately ignored — and crucially it must not touch a phase-stamped file
either (the aging phase *is* the waiting signal during the silent window).

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

`install.sh` and the **Settings → Agents → Install bridge** button share one
manifest-driven installer (F12a): both land `projectman.json` AND
`projectman-status.py` (executable) in `~/.grok/hooks/`, idempotently. At
install time the JSON's command paths are **rewritten to the absolute installed
script path** (F12b) — the repo copy keeps the portable `~` form, but the
installed copy never relies on grok shell-expanding `~`. `install.sh`
additionally flips `[compat.claude] hooks = false`.

To install by hand:

```sh
mkdir -p ~/.grok/hooks
cp bridges/grok/projectman.json     ~/.grok/hooks/
cp bridges/grok/projectman-status.py ~/.grok/hooks/
chmod +x ~/.grok/hooks/projectman-status.py
# rewrite the "command" entries in ~/.grok/hooks/projectman.json to the
# absolute script path (replace ~ with your home directory), then set
# [compat.claude] hooks = false in ~/.grok/config.toml
```

grok loads hooks from `~/.grok/hooks/` automatically on its next launch.

## Selftest

`python3 bridges/grok/selftest.py` runs the real status script against synthetic
event sequences (the observed happy path, the `notification` no-write proof,
the F11 phase stamp/clear contract, unknown-event no-ops, the empty-slug guard)
and prints a pass/fail tally. It is a dev/CI artifact and is not installed.

## Status directory

The bridge writes to `~/.ProjectMan/status/` (the agent-neutral location).
ProjectMan's `StatusWatcher` watches that directory and, during the deprecation
window, also the legacy Claude location `~/.claude/projectman/status/`.
