# Session Restore Design

**Date:** 2026-03-15
**Status:** Approved

## Summary

When the user closes ProjectMan, save the list of projects that had a running claude
process and which project was focused. On next launch, restore all of them and focus
the same pane.

## Behaviour

- **Toggle:** `Settings → Resume projects on startup` (replaces "Resume Last Project").
  When off, nothing opens automatically. When on, session restore runs.
- **What is saved:** every project whose `TerminalView._child_pid` is not `None` at
  close time, plus the path of the currently-focused project (`_active_path`).
- **When saved:** in `AppWindow._on_close_request`, before the shutdown dialog is
  shown. Only saved when the toggle is on.
- **What is restored:**
  - Background projects (saved but not the focused one): terminal pane created,
    `claude -c` launched silently.
  - Focused project: activated normally via `_on_project_activated` (pane shown,
    `claude -c` launched).
  - Stale paths (project deleted/archived since last run): silently skipped.

## Storage

**File:** `~/.ProjectMan/session.json`

```json
{
  "open_paths": ["/abs/path/proj-a", "/abs/path/proj-b"],
  "focused_path": "/abs/path/proj-a"
}
```

Written atomically (temp file + rename) to avoid corruption. Not deleted after
reading — overwritten on each clean close so the file always reflects the last
known good state.

## Files Changed

| File | Change |
|------|--------|
| `settings.py` | Rename field `resume_last_project` → `resume_projects` |
| `settings_window.py` | Update row title to "Resume projects on startup" |
| `window.py` | Add `_save_session()`, replace `_activate_last_project()` with `_restore_session()` |
| `main.py` | Call `_restore_session()` instead of `_activate_last_project()` |
| `tests/test_settings.py` | Update field name assertion |

## Data Flow

```
Close
  _on_close_request()
    → _save_session()           # writes ~/.ProjectMan/session.json
    → (shutdown dialog if needed)

Open
  _on_activate()
    → _restore_session()
        → load session.json
        → for each background path: _get_or_create_terminal() + spawn_claude(-c)
        → for focused path: _on_project_activated()
```

## Error Handling

- `FileNotFoundError` / `json.JSONDecodeError` on read: silently skip restore.
- Project path not found in store: silently skip that entry.
- `session.json` write failure: log to stderr, don't crash.

## Testing

- `test_settings.py`: assert `resume_projects` default is `True`, old field name absent.
- Manual smoke test: open 3 projects, close PM, reopen — all 3 restore, focused pane matches.
