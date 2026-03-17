# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

ProjectMan is a GTK4/Adwaita desktop application for managing Claude Code sessions. It displays a project sidebar on the left and an embedded VTE terminal on the right, running `claude` (or `zellij attach`) per project. Projects are directories under `~/.ProjectMan/Projects/` (configurable).

## Running and Testing

```bash
# Run the app
python main.py

# Run all tests
python -m pytest

# Run a single test file
python -m pytest tests/test_session_restore.py

# Run a single test
python -m pytest tests/test_session_restore.py::test_plan_restore_focused
```

Tests run headless (no display required) — all GTK-dependent code is excluded from test modules.

## Architecture

The app is a single-process GTK4 application (`ProjectManApp` in `main.py`) with these main components:

**`model.py`** — Pure data layer, no UI:
- `ProjectStore` — reads/writes project directories and `.archive/` subdirectory
- `HistoryReader` — parses `~/.claude/history.jsonl` to surface recent sessions per project
- `StatusWatcher` — monitors `~/.claude/projectman/status/*.json` via inotify; maps project paths → `StatusSnapshot` (state: `done`/`working`/`waiting`/`idle`)
- `ProjectsWatcher` — inotify monitor on the projects directory; emits `projects-changed`
- `ResourceReader` — reads `/proc/stat` and `/proc/meminfo` for the CPU/RAM bar

**`settings.py`** — `Settings` dataclass persisted to `~/.ProjectMan/settings.json`. Atomic write via `tempfile.mkstemp` + `os.replace`.

**`session.py`** — Session restore logic. Saves/loads `~/.ProjectMan/session.json` (list of open project paths + focused path). Pure functions; no GTK dependency.

**`zellij.py`** — Zellij integration helpers: session naming (`pm-<slug>`), socket detection, `session_alive()` via `zellij list-sessions --no-formatting`, and `ZellijWatcher` (inotify on zellij socket dir).

**`terminal.py`** — `TerminalView` wraps a `Vte.Terminal`. Emits `process-started`, `process-exited`, `process-detached`. `spawn_claude()` runs `claude -c || claude`; `spawn_zellij()` creates/attaches a zellij session using a shell wrapper at `~/.ProjectMan/zellij-shell-init.sh`.

**`sidebar.py`** — `Sidebar` (left panel): `ProjectRow` entries with status dots, expand/collapse for session history, right-click context menu, inline rename entry, `ResourceBar` at the bottom.

**`window.py`** — `AppWindow` orchestrates everything: owns `_terminals` dict (path → TerminalView), `_stack` (Gtk.Stack switching between terminals), session save/restore on open/close.

**`settings_window.py`** — `Adw.PreferencesDialog` with pages for General, Terminal, Appearance (hook script + status color reference), About, and Claude JSON editor.

## Key Runtime Files

| Path | Purpose |
|------|---------|
| `~/.ProjectMan/settings.json` | App settings |
| `~/.ProjectMan/session.json` | Last-session restore data |
| `~/.ProjectMan/Projects/` | Default projects directory |
| `~/.ProjectMan/Projects/.archive/` | Archived projects |
| `~/.claude/history.jsonl` | Claude session history (read-only) |
| `~/.claude/projectman/status/*.json` | Per-project Claude hook status files |
| `~/.claude/projectman/hook.js` | Claude Code hook script for status updates |

## Status Dot CSS Classes

Status dots use `status-{state}` CSS classes defined in `style.css`:
- `status-stopped` — no running process (nearly invisible)
- `status-idle` — detached zellij session (dim)
- `status-done` — green (#8ce10b, Argonaut)
- `status-working` — yellow (#ffb900, Argonaut)
- `status-waiting` — blue (#008df8, Argonaut)

## Signal Flow

Settings changes flow via the `settings-changed` GObject signal on `ProjectManApp`, which calls `AppWindow.apply_settings()` and restarts watchers as needed.

Project state changes (`attached`/`detached`/`inactive`) propagate from `TerminalView` signals → `AppWindow` → `Sidebar.set_project_state()` → `ProjectRow.update_status()`.
