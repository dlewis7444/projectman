# Per-Project Status Colors Design

**Date:** 2026-03-16
**Status:** Approved

## Problem

ProjectMan currently writes all Claude hook events to a single global status file
(`~/.claude/projectman/status.json`). This means only one project can display a live
color at a time. Additionally, the 60-second staleness expiry clears colors even while
Claude is running, and there is no visual distinction between "Claude finished its turn"
and "Claude needs your attention."

## Goal

When a Claude session is active and attached in a zellij pane, the project row in the
sidebar always shows a color reflecting Claude's current state. Multiple projects can
show colors simultaneously. Detached sessions remain grey-italic with no color, as
before.

## File Layout

Each project gets its own status file:

```
~/.claude/projectman/status/<slug>.json
```

**Slug algorithm:** Replace all `/` and `.` characters in the project's realpath with
`-`, then strip all leading `-`. This is ProjectMan's own convention (not identical to
`~/.claude/projects/`).

Example:
```
/home/dlewis/.ProjectMan/projects/projectman
→ home-dlewis--ProjectMan-projects-projectman.json
```

In JS: `cwd.replace(/[\/\.]/g, '-').replace(/^-+/, '')`
In Python: `re.sub(r'[/.]', '-', path).lstrip('-')`

**Collision risk:** The algorithm maps both `.` and `/` to `-`, so two distinct paths
like `/foo/bar.baz` and `/foo/bar/baz` could theoretically collide. This risk is
accepted — the directory structure of Claude projects makes such collisions essentially
impossible in practice.

File format (`state` field is new; all others unchanged):

```json
{
  "state": "done",
  "event": "Stop",
  "cwd": "/home/dlewis/.ProjectMan/projects/projectman",
  "ts": 1773699838,
  "session": "a414083c-..."
}
```

The `ts` field is retained but the 60-second staleness check is removed. State persists
until the next event overwrites the file or `SessionEnd` deletes it.

**Known limitation:** If a project directory is renamed, the old slug file is orphaned
on disk. The renamed project will show `idle` until Claude emits a new hook event, at
which point a correctly-named slug file is written. This is acceptable.

**Write atomicity:** Files are written with `fs.writeFileSync` (non-atomic). Torn reads
are theoretically possible under rapid successive events. This is the same risk as
before and is accepted given the low event rate and small file size.

## State Machine

The hook maps each event to one of three states and writes it to the `state` field.
`hook_event_name` values are assumed to match the names below; this matches observed
behavior in Claude Code and confirmed hook registrations in `~/.claude/settings.json`
(all 8 events including `PostToolUseFailure` are registered).

| Hook event           | State written | Color  | Meaning                         |
|----------------------|---------------|--------|---------------------------------|
| `SessionStart`       | `done`        | green  | Session just opened, idle       |
| `UserPromptSubmit`   | `working`     | orange | Processing user input           |
| `PreToolUse`         | `working`     | orange | Executing a tool                |
| `PostToolUse`        | `working`     | orange | Tool returned, still thinking   |
| `PostToolUseFailure` | `working`     | orange | Tool failed, Claude continuing  |
| `Stop`               | `done`        | green  | Turn complete, no action needed |
| `Notification`       | `waiting`     | blue   | Claude needs your attention     |
| `PermissionRequest`  | `waiting`     | blue   | Claude needs your approval      |
| `SessionEnd`         | *(delete file)* | —    | Session exiting                 |

`SessionStart` maps to `done` (green): the session is open but idle, no work in flight.
Green means "no action required right now." This is intentional — a project at the
initial prompt (before any user input) shows green and will not trigger the shutdown
confirmation dialog, since no work is in flight.

**UX note:** `status-notification` (red, `#f44336`) is replaced by `status-waiting`
(blue, `#2196f3`). Blue is intentionally less urgent than red — "Claude has a question"
rather than an error. This is a deliberate UX change.

Colors only apply when `_process_state == 'attached'`. Detached sessions show
grey-italic regardless of file contents.

**Attached session with no status file:** When `_process_state == 'attached'` but
`get_project_status` returns `'idle'` (no file for this project), `update_status()` in
`sidebar.py` applies `status-done` (green) as the fallback. `StatusWatcher` itself
always returns `'idle'` for missing files — the override is the responsibility of
`update_status()`, not the watcher.

**Side effect in `window.py`:** The shutdown confirmation dialog (line 110) checks
`get_project_status(proj) == 'working'`. Previously `UserPromptSubmit` mapped to
`'active'` and did not trigger this dialog. Under the new model it maps to `'working'`
and will trigger the dialog. This is intentionally correct: if the user submits a prompt
and immediately tries to quit, the dialog correctly warns them Claude is working.
No code changes to `window.py` are required.

## Component Changes

### `~/.claude/projectman/hook.js`

- Build event-to-state map at top of file:
  ```js
  const STATE = {
    SessionStart: 'done', Stop: 'done',
    UserPromptSubmit: 'working', PreToolUse: 'working',
    PostToolUse: 'working', PostToolUseFailure: 'working',
    Notification: 'waiting', PermissionRequest: 'waiting',
  }
  ```
- Derive slug: `cwd.replace(/[\/\.]/g, '-').replace(/^-+/, '')`
- Ensure directory: `fs.mkdirSync(statusDir, { recursive: true })`
- Write to `~/.claude/projectman/status/<slug>.json` with `state` field included
- On `SessionEnd`: `fs.unlinkSync(slugPath)`, catching and ignoring ENOENT
- Remove the single `STATUS_FILE` constant

### `model.py`

**Constant:** Replace `STATUS_FILE` with:
```python
STATUS_DIR = os.path.expanduser('~/.claude/projectman/status')
```

**`StatusSnapshot`:** Append `state: str = 'done'` as the **last field** (after the
existing `tool: str = None`). Both fields have defaults; `tool` stays before `state`.
Python allows multiple default fields in sequence.

**`StatusWatcher.__init__()`:** Change `self._status = None` to `self._status: dict = {}`.

**`StatusWatcher.start()`:**
- `os.makedirs(STATUS_DIR, exist_ok=True)`
- Monitor `STATUS_DIR` as a directory with `monitor_directory`
- Call `_reload()` at the end of `start()` as before

**`StatusWatcher._on_changed()`:**
- React to `CHANGED`, `CREATED`, and `DELETED` events — all call `_reload()` and all
  also schedule `_delayed_poll` (800ms secondary poll). The delayed poll after a
  `DELETED` event re-reads the directory, confirms the file is gone, and drops that
  entry from `self._status`. This is intentional.
- Spurious events for non-`.json` files in the directory are accepted — `_reload()`
  filters to `.json` files only, so spurious reloads are a harmless no-op.
- Remove the stale single-file `CHANGED`/`CREATED` filter

**`StatusWatcher._reload()`:**
- Wrap the entire directory scan in a broad `except Exception` so `PermissionError` or
  any `OSError` on `os.scandir(STATUS_DIR)` fails silently (no-op reload)
- For each `.json` file: parse JSON; skip (silently) on `json.JSONDecodeError` or
  `OSError`
- Skip files where `cwd` is empty or missing (do not call `realpath` on an empty string)
- Wrap `os.path.realpath(cwd)` in a `try/except OSError` and skip on error
- Build `self._status = { os.path.realpath(cwd): StatusSnapshot(...) }` keyed by
  resolved project path
- Use `data.get('state', 'done')` when constructing `StatusSnapshot` (handles old files
  without the `state` field)
- Replace `self._status` atomically (assign new dict, don't mutate in place)
- Emit `status-changed` signal after reload

**`StatusWatcher.get_project_status(project)`:**
- Look up `project.path` in `self._status`
- If found, return `snapshot.state` directly (`'working'` / `'done'` / `'waiting'`)
- If not found, return `'idle'`
- Remove the 60-second staleness check entirely

### `style.css`

- Rename `.status-active` → `.status-done` (keep `#4caf50` green)
- Add `.status-waiting { background: #2196f3; }` (blue)
- Remove `.status-notification` rule
- `.status-working` (orange, `#ff9800`) already exists and requires no change

### `sidebar.py` — `update_status()`

Replace the full method body. The early-return structure for `inactive` and `detached`
is preserved; the `attached` branch changes to use the new class names and idle fallback:

```python
# Clear all possible status classes (includes legacy names for migration safety)
for s in ('status-stopped', 'status-idle', 'status-active',
          'status-done', 'status-working', 'status-waiting', 'status-notification'):
    self._status_dot.remove_css_class(s)

if self._process_state == 'inactive':
    self._status_dot.add_css_class('status-stopped')
    return
if self._process_state == 'detached':
    self._status_dot.add_css_class('status-idle')
    return

# attached: apply live status, defaulting to done if no file yet
status = self._watcher.get_project_status(self._project)
if status == 'idle':
    status = 'done'
self._status_dot.add_css_class(f'status-{status}')
```

### `settings_window.py`

Replace the `status_colors` list (removes the old `active` and `notification` rows,
adds `done` and `waiting`; `stopped`, `idle`, and `working` are unchanged):

```python
status_colors = [
    ('stopped',  'Stopped',  'alpha(currentColor, 0.08)'),
    ('idle',     'Idle',     'alpha(currentColor, 0.25)'),
    ('done',     'Done',     '#4caf50'),
    ('working',  'Working',  '#ff9800'),
    ('waiting',  'Waiting',  '#2196f3'),
]
```
The first element of each tuple (`_key`) is informational only — not used to apply CSS.
The rows are display-only reference swatches.

### Tests

Any existing tests that assert `get_project_status` returns `'active'` or
`'notification'` will break and must be updated to the new return values (`'done'` and
`'waiting'` respectively). New unit tests for `_reload()` directory scanning are
expected (at minimum: empty dir, valid file, invalid JSON file, missing `cwd` field,
missing `state` field using old format).

## What Does Not Change

- `window.py` — no code changes (see side-effect note above regarding shutdown dialog)
- `zellij.py` — no changes
- Hook registration in `~/.claude/settings.json` — no changes
- The zellij-based `_process_state` logic (`inactive` / `attached` / `detached`)
