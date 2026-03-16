# Session Restore Design

**Date:** 2026-03-15
**Status:** Approved

## Summary

When the user closes ProjectMan (and confirms the close), save the list of projects
that had a running claude process plus which one was focused. On next launch, restore
all of them — focused project shown in the main pane, others spawning silently in
the background.

## Behaviour

### Toggle

`Settings → Resume projects on startup` (replaces "Resume Last Project").
When off, nothing opens automatically. When on, session restore runs.

Existing `settings.json` files contain `resume_last_project`. `Settings.load()` only
reads keys present in `__dataclass_fields__`, so the old key is silently ignored and
the new field `resume_projects` uses its dataclass default (`True`). No migration
needed; upgrade behaviour is correct.

### What is saved

- `open_paths`: deduplicated list of project paths where `TerminalView._child_pid is
  not None` at save time. Includes the focused project if it was running.
- `focused_path`: `_active_path` **only if** it is also in `open_paths` (i.e., had a
  live process). If the focused pane had no running process, `focused_path` is `null`.
  In that case, on restore no pane is switched to — the user sees the placeholder
  screen with background sessions running. This is intentional.

`_save_session()` captures a snapshot of `_child_pid` values **at the moment
`_open_shutdown_window()` is entered** — before SIGTERM/SIGKILL. This is the correct
point: all processes the user had open are still alive. The snapshot does not update
as processes subsequently exit.

### When is the save triggered

`_save_session()` is called from **two** places:

1. **`_open_shutdown_window(running)`** — when close is committed with running
   processes. Called whether the user reached this directly or after confirming
   "Close Anyway" in the working-confirm dialog. Not called if the user clicks
   "Keep Running" (that path returns without calling `_open_shutdown_window`).

2. **The no-running-processes path** in `_on_close_request`, just before `return
   False`. This writes an empty `open_paths`/null `focused_path`, which on restore
   is a no-op. If the write fails here, it is silently swallowed (stderr log only) —
   there is no ShutdownWindow to surface the error, and a failed write in this path
   has no impact on user experience.

`_save_session()` is **not** called at the top of `_on_close_request`, which would
cause a spurious write when the user clicks "Keep Running" to cancel.

### What is restored

Called from `main._on_activate()` after `window.present()`, replacing
`_activate_last_project()`:

1. If `resume_projects` is off, return immediately.
2. Load `~/.ProjectMan/session.json`. On any error (missing, corrupt), return silently.
3. Deduplicate `open_paths` (preserve order). Filter to only active (non-archived)
   projects found in `ProjectStore.load_projects()`. Archived projects are excluded —
   they should not be auto-restored.
4. If `focused_path` is set, is in the filtered active project set, and is found in
   the project store, call `_on_project_activated(self._sidebar, focused_path)`.
   This shows the pane and launches `claude -c`. (`_on_project_activated` accepts
   `sidebar` as its first positional argument per signal-handler convention, but
   never uses it — passing `self._sidebar` is safe and matches what
   `_activate_last_project()` already does at line 280.)
5. For each path in the filtered `open_paths` excluding `focused_path`:
   - Call `_get_or_create_terminal(project)` — creates the terminal pane, connects
     `process-started`/`process-exited` signals to the sidebar, and adds to
     `_stack` via `Gtk.Stack.add_named` internally.
   - Call `tv.spawn_claude(project_name=project.name)` to run `claude -c`.
   - Do not switch the visible pane.

`claude -c` continues the most recent session for the project directory — the same
flag used for normal project activation. All spawns are non-blocking (`spawn_async`).
There is no cap on the number of restored sessions.

**Race note (accepted limitation):** `_child_pid` is set asynchronously in
`_on_spawn_done`. If the user closes PM immediately after launch before spawns
complete, `_save_session()` will see all `_child_pid` as `None` and write an empty
session. No special handling; this edge case is accepted.

### Session file lifetime

The file is not deleted after reading. It is overwritten on each committed close. If
PM is killed (OOM/crash), the file from the last committed close is used on the next
launch — restoring the last known good state is preferable to restoring nothing.

**Known limitation:** If the user opens the shutdown dialog, SIGTERMs are sent, then
clicks Cancel — PM stays open with some processes potentially already exited. The
session file already written reflects the pre-cancel state. If PM is subsequently
killed without another clean close, the stale file will be used on next launch.
Accepted: rare corner case, minor downside (an extra `claude -c` for an already-
exited project).

## Storage

**File:** `~/.ProjectMan/session.json`

```json
{
  "open_paths": ["/abs/path/proj-a", "/abs/path/proj-b"],
  "focused_path": "/abs/path/proj-a"
}
```

Written atomically: `os.makedirs(~/.ProjectMan/, exist_ok=True)`, then
`tempfile.mkstemp(dir=~/.ProjectMan/, suffix='.tmp')` + `os.fdopen` + `os.replace()`
— same pattern as `Settings.save()`. Guarantees temp file and destination are on the
same filesystem (avoids `EXDEV`). The `makedirs` call ensures the directory exists
on first launch (before any `settings.save()` has run).

## Files Changed

| File | Change |
|------|--------|
| `settings.py` | Rename field `resume_last_project` → `resume_projects` |
| `settings_window.py` | Update row title to "Resume projects on startup" |
| `window.py` | Add `_save_session()`; call it from `_open_shutdown_window()` and the no-running-processes path in `_on_close_request`; replace `_activate_last_project()` with `_restore_session()` |
| `main.py` | Call `_restore_session()` instead of `_activate_last_project()` |
| `tests/test_settings.py` | Update field name assertion |
| `tests/test_session_restore.py` | New (see Testing section) |

## Data Flow

```
Close (committed)
  _open_shutdown_window(running)       # OR: no-running-processes → return False
    → _save_session()                  # snapshot at this moment; atomic write
    → ShutdownWindow(...) / immediate close

Open
  main._on_activate()
    → window.present()
    → _restore_session()
        → check resume_projects toggle
        → load + validate session.json  # skip all on missing / corrupt
        → filter to active projects only (not archived)
        → _on_project_activated(self._sidebar, focused_path)  # show pane + claude -c
        → for each remaining background path:
              _get_or_create_terminal(project)       # creates pane in _stack
              tv.spawn_claude(project_name=...)      # claude -c, no pane switch
```

## Error Handling

| Condition | Behaviour |
|-----------|-----------|
| `session.json` missing | Skip restore; no error shown |
| `session.json` corrupt (bad JSON) | Skip restore; no error shown |
| `focused_path` not in active project store | Skip focused activation; restore background paths normally |
| Background path not in active project store | Skip that entry; continue with rest |
| Path belongs to archived project | Skip; archived projects are not auto-restored |
| `focused_path` is `null` | Restore background projects only; no pane switch (intentional) |
| `session.json` write fails | Log to stderr; do not raise; do not block close |

## Testing

**`tests/test_settings.py`:**
- `resume_projects` default is `True`
- `resume_last_project` key absent from `Settings.__dataclass_fields__`
- Old `settings.json` with `resume_last_project: false` loads with `resume_projects=True`

**`tests/test_session_restore.py` (new):**
- `_save_session()` writes correct JSON: running terminals → correct `open_paths`; active path in running set → correct `focused_path`
- `_save_session()` writes `focused_path: null` when active path has no running process
- `_save_session()` is not called when user clicks "Keep Running" (cancel path)
- `_restore_session()` is a no-op when `resume_projects` is `False`
- `_restore_session()` skips gracefully on missing `session.json`
- `_restore_session()` skips gracefully on corrupt JSON
- `_restore_session()` skips `focused_path` absent from active project store (no crash)
- `_restore_session()` skips background paths absent from active project store
- `_restore_session()` does not restore archived project paths
- `_restore_session()` deduplicates `open_paths` (duplicate path spawned only once)
- Atomic write uses `mkstemp` in `~/.ProjectMan/` dir followed by `os.replace`
