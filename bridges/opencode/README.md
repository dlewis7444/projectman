# ProjectMan status bridge for opencode

`projectman.js` is an [opencode](https://opencode.ai) plugin that lights up
ProjectMan's sidebar status dots for opencode sessions, exactly the way
`hooks/hook.js` does for Claude Code. It is the opencode half of ProjectMan's
**status contract**.

## What it does

It maps opencode's lifecycle events onto ProjectMan's per-cwd status schema and
writes one JSON file per working directory under `~/.ProjectMan/status/`:

```json
{ "state": "working" | "waiting" | "done", "event": "...", "cwd": "...", "ts": 0, "session": "..." }
```

`idle` / `stopped` are not written here — ProjectMan derives them from process
and zellij liveness.

| opencode signal | class | ProjectMan state |
|---|---|---|
| `session.idle`, `session.compacted`, `session.status` indicating idle | idle | `done` (stamps the sticky window) |
| `session.status` indicating busy | strong | `working` (bypasses stickiness, clears the stamp) |
| `permission.asked` / `permission.updated` / `permission.ask` hook | strong | `waiting` (bypasses stickiness, clears the stamp) |
| `tool.execute.before` hook | strong | `working` (bypasses stickiness, clears the stamp) |
| `message.updated`, `message.part.updated` | weak | `working` — **ignored within 2000ms of the last idle** |
| `session.updated` | metadata | **ignored — never `working`** (see below) |
| `session.deleted` | — | (status file deleted) |

The plugin reacts to **multiple spellings** of the same signal on purpose:
opencode's event-type names have drifted across versions, and the bridge must
be resilient to that. Unrecognised events are ignored.

### Why "done" sticks — a two-layer defense

opencode emits post-idle noise on two different timescales, and each layer of
the defense kills one. Without them, a flat event→state map flipped the dot
back to `working` after a turn ended and it stuck there forever (knock-on:
closing an idle project raised a false "Interrupt Active Work?").

**Layer 1 — class demotion (any timescale).** `session.updated` is session
*metadata*, not turn activity, so the bridge never treats it as `working` at
any offset. opencode 1.16.2 titles a *new* session in a background LLM call
after its first turn and emits a bare `session.updated` when that finishes —
round-3a traced it at **+78s** after `session.idle` (2378 samples; the session
gained the title "Alpha word response" at that frame), pool-latency-dependent
and once per session. No fixed time window can cover a minute-scale event, so
it is class-demoted outright. Real turns are detected from message-class events
instead (every observed turn opened with `message.updated` or
`message.part.updated`; title generation emits none).

**Layer 2 — time window (ms-scale only).** Message-class events
(`message.updated`, `message.part.updated`) *do* mark real turns, but opencode
also dribbles one out as a post-idle bookkeeping tail — `message.updated` at
**+20ms** after `session.idle` in the round-2 411-sample trace (which also saw
`session.updated` at +12ms, now handled by layer 1). The bridge stamps
`lastIdleAt` on every idle-class event and **ignores message-class events for
2000ms** after it; a genuine new turn arrives well past the window. The window
is time-based rather than "ignore the next N events" because the tail shape
varies between opencode versions (1.17.0 differs from 1.16.2).

Strong signals — a tool actually starting, a permission gate, or
`session.status` flipping to busy — bypass both layers and clear the stamp.

The cwd is taken from the plugin's own `directory` input (the plugin instance
is per-directory), not parsed out of individual events. `directory` is
preferred over `worktree` deliberately: opencode reports `worktree="/"` for
non-git project directories (verified on 1.16.2 and 1.17.0), which would map
every non-git project to an unusable empty slug. An empty-slug guard skips all
writes rather than ever emitting a bare `.json`.

## Install

`install.sh` copies this file into `~/.config/opencode/plugins/` for you
(idempotent — re-running is safe). To install by hand:

```sh
mkdir -p ~/.config/opencode/plugins
cp bridges/opencode/projectman.js ~/.config/opencode/plugins/
```

opencode loads plugins from `~/.config/opencode/plugins/` automatically on its
next launch.

## Status directory

The bridge writes to `~/.ProjectMan/status/` (the agent-neutral location).
ProjectMan's `StatusWatcher` watches that directory and, during the deprecation
window, also the legacy Claude location `~/.claude/projectman/status/`.
