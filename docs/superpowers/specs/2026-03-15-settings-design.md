# ProjectMan Settings — Design Spec
**Date:** 2026-03-15
**Status:** Approved

---

## Overview

Add a persistent settings system to ProjectMan with an `Adw.PreferencesDialog` UI, replacing all hardcoded configuration values. Settings are stored as JSON in `~/.projectman/settings.json` and propagated through the app via a `Settings` object passed through constructors.

---

## Directory Structure Change

The default project and config locations are moving:

| Location | Old | New (default) |
|----------|-----|---------------|
| Config / settings | `~/.config/projectman/` | `~/.projectman/` |
| Settings file | _(none)_ | `~/.projectman/settings.json` |
| Projects directory | `~/claude-projects/` | `~/.projectman/projects/` |
| Archive directory | `~/claude-projects/.archive/` | `<projects_dir>/.archive/` |
| Status file | `~/.claude/projectman/status.json` | unchanged |
| History file | `~/.claude/history.jsonl` | unchanged |

`projects_dir` is a user-configurable setting; the archive dir is always `<projects_dir>/.archive/` and is not separately configurable.

**Migration note:** Existing users with projects in `~/claude-projects/` will need to move them to `~/.projectman/projects/` or point the Projects Directory setting at `~/claude-projects/`. No automatic migration is implemented; no path validation is required — an invalid path simply yields an empty project list.

---

## Settings Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `projects_dir` | `str` | `~/.projectman/projects` | Root directory scanned for project symlinks/dirs |
| `claude_binary` | `str` | `""` | Path to `claude` binary; empty string = use PATH |
| `resume_last_project` | `bool` | `True` | Auto-open last active project on startup |
| `font_size` | `int` | `11` | VTE terminal font size (pt); range 6–36 |
| `scrollback_lines` | `int` | `10000` | Lines of terminal scrollback; range 1000–100000 |
| `audible_bell` | `bool` | `False` | Enable terminal audible bell |
| `multiplexer` | `str` | `"zellij"` | Terminal multiplexer; one of `"zellij"`, `"tmux"`, `"screen"` |

---

## New Files

### `settings.py`

`Settings` is a plain Python dataclass holding the fields above. `load()` and `save()` are class methods:
- `Settings.load()` — reads `~/.projectman/settings.json`; returns a `Settings` instance with defaults if the file is missing or unparseable.
- `settings.save()` — creates `~/.projectman/` if needed; writes atomically by writing to a temp file in `~/.projectman/` (same filesystem as the target, avoiding `EXDEV` on rename) then `os.replace()`ing it to `settings.json`. Does not return a value.

**Mutation pattern:** callers set fields directly (`settings.font_size = 12`) then call `settings.save()`. There are no `set_*` helpers and no GObject signals on `Settings` itself — the app-level `settings-changed` GObject signal lives on `ProjectManApp` in `main.py`, fired by `SettingsWindow` after each `save()` call.

**Properties:**
- `resolved_projects_dir` — `os.path.expanduser(projects_dir)`
- `resolved_claude_binary` — returns `"claude"` if `claude_binary` is blank/whitespace, else the stored value

### `settings_window.py`

Minimum libadwaita version assumed: 1.4 (available in the target environment: libadwaita 1.8.4). `Adw.EntryRow.apply` signal available since libadwaita 1.2.

```
SettingsWindow(Adw.PreferencesDialog)
  __init__(settings: Settings, app: ProjectManApp, parent: Gtk.Window)

  Pages:
    General (Adw.PreferencesPage, icon: "preferences-system-symbolic")
      Group "Projects"
        projects_dir  — Adw.ActionRow
                        subtitle shows resolved path (kept as a Python reference to update)
                        suffix: "Choose Folder…" Gtk.Button → GtkFileDialog (select-folder)
                        on folder chosen: settings.projects_dir = path; settings.save();
                          row.set_subtitle(settings.resolved_projects_dir);
                          app.emit('settings-changed')
        claude_binary — Adw.EntryRow, placeholder "claude  (PATH default)"
                        saves on the EntryRow `apply` signal (Enter / focus-out), not per-keystroke:
                        settings.claude_binary = val; settings.save(); app.emit('settings-changed')
      Group "Startup"
        resume_last_project — Adw.SwitchRow
                              saves immediately on toggle

    Terminal (Adw.PreferencesPage, icon: "utilities-terminal-symbolic")
      Group "Font"
        font_size     — Adw.SpinRow (min 6, max 36, step 1)
                        saves immediately on each step
      Group "Behavior"
        scrollback_lines — Adw.SpinRow (min 1000, max 100000, step 1000)
                           saves immediately on each step (single discrete step-click, not drag)
        audible_bell  — Adw.SwitchRow; saves immediately on toggle
        multiplexer   — Adw.ComboRow, model: ["zellij", "tmux", "screen"]
                        selected index matches position in that list; saves immediately on change

    Appearance (Adw.PreferencesPage, icon: "preferences-desktop-theme-symbolic")
      Group "Theme"
        theme row     — Adw.ActionRow, set_sensitive(False) (visually greyed/disabled)
                        subtitle: "Coming in a future release"
                        no interactive controls
```

All rows: pattern is `settings.<field> = value; settings.save(); app.emit('settings-changed')`.
EntryRow (`claude_binary`) is the only row that defers until `apply` signal; all others save on each change event.

---

## Modified Files

### `model.py`

- Remove module-level `PROJECTS_DIR` and `ARCHIVE_DIR` constants.
- `ProjectStore.__init__(settings: Settings)` — stores reference; reads `settings.resolved_projects_dir` in `load_projects()` / `load_archived()` / `archive()` / `restore()`. Archive dir is always `os.path.join(settings.resolved_projects_dir, '.archive')`.
- `ProjectsWatcher.start(path: str)` — accepts an explicit path argument (replaces the former constant). `os.makedirs(path, exist_ok=True)` moves from the constant-based call to use this argument.
- `ProjectsWatcher.restart(new_path: str)` — cancels the existing `Gio.FileMonitor` by calling `self._monitor.cancel()`, sets `self._monitor = None`, then calls `self.start(new_path)`.

### `terminal.py`

- `TerminalView.__init__(project, settings: Settings)` — stores `self._settings = settings`; uses `settings.font_size`, `settings.scrollback_lines`, `settings.audible_bell` at init.
- `apply_settings(settings: Settings)` — public method; updates `self._settings = settings`, then:
  - `self._font_size = settings.font_size`
  - `self._apply_font()`
  - `self._terminal.set_scrollback_lines(settings.scrollback_lines)`
  - `self._terminal.set_audible_bell(settings.audible_bell)`
  - Subsequent `zoom_in()` / `zoom_out()` increments are relative to the updated `self._font_size`.
- `zoom_reset()` — resets to `self._settings.font_size` (not hardcoded 11).
- `spawn_claude()` — `argv[0]` is `settings.resolved_claude_binary` instead of hardcoded `"claude"`. `GLib.SpawnFlags.SEARCH_PATH` is kept so a plain `"claude"` is still found via PATH.
- `spawn_multiplexer(binary: str)` — new **public** method; kills existing child, resets terminal, calls `self._spawn([binary])`. `binary` is a bare name (e.g. `"zellij"`) located via `GLib.SpawnFlags.SEARCH_PATH`. If the binary is not installed, VTE displays the OS error in the terminal (same behavior as typing a nonexistent command); no special error handling is needed. `window.py` calls this instead of accessing `_spawn()` directly. This replaces the existing pattern of `window.py` calling `tv._spawn(['zellij'])`.

### `main.py`

- `Settings.load()` is called first in `_on_activate`, before constructing any other objects. The initialization sequence in `_on_activate` is:
  1. `self._settings = Settings.load()`
  2. `self._store = ProjectStore(self._settings)`
  3. `self._history = HistoryReader()` + `load()`
  4. `self._watcher = StatusWatcher()` + `start()`
  5. `self._projects_watcher = ProjectsWatcher()` + `start(self._settings.resolved_projects_dir)`
  6. `self._window = AppWindow(self, self._store, self._history, self._watcher, self._settings)`
  7. Connect signals; `self._window.present()`

- `ProjectManApp` gains a `settings-changed` GObject signal (no args).
- `ProjectManApp` caches `self._last_projects_dir = self._settings.resolved_projects_dir` after step 5 above.
- `_on_settings_changed(app)`:
  - Call `self._window.apply_settings(self._settings)` (public method on `AppWindow`; see window.py below).
  - If `self._settings.resolved_projects_dir != self._last_projects_dir`:
    - `self._projects_watcher.restart(self._settings.resolved_projects_dir)`
    - `self._window._sidebar.refresh()`
    - `self._window._sync_running_state()`
    - `self._last_projects_dir = self._settings.resolved_projects_dir`

### `window.py`

- `AppWindow.__init__` gains `settings: Settings` parameter; stored as `self._settings`; passed to `TerminalView` at creation time in `_get_or_create_terminal()`.
- Connect `sidebar.show-settings` → `_on_open_settings()`: single-instance pattern (same as archive window).
- `_on_open_settings()` creates `SettingsWindow(self._settings, self.get_application(), self)`.
- `AppWindow` gains a public `apply_settings(settings: Settings)` method that iterates `self._terminals.values()` and calls `tv.apply_settings(settings)` on each. `main._on_settings_changed` calls `self._window.apply_settings(self._settings)` instead of accessing `_terminals` directly.
- **Startup behavior:** `AppWindow` gains a `_activate_last_project()` method called by `main._on_activate` after `self._window.present()` (not inside `__init__`). If `settings.resume_last_project` is True:
  - Iterate `self._store.load_projects()` only (not archived projects).
  - For each project, get its sessions via `self._history.get_sessions(project)`.
  - Find the project whose most recent session has the greatest `last_active` timestamp.
  - If found and present in the current project list: call `_on_project_activated(self._sidebar, project.path)`.
  - If not found or the project no longer exists in `load_projects()`: do nothing (show placeholder).
- `_on_project_zellij` renamed to `_on_project_open_multiplexer` (internal rename only). Uses `tv.spawn_multiplexer(self._settings.multiplexer)` instead of `tv._spawn(['zellij'])`.

### `sidebar.py`

- `ResourceBar.__init__` gains an `on_settings_clicked: callable` parameter. The gear button connects `clicked` to `on_settings_clicked`. No direct signal or parent traversal.
- `Sidebar.__init__` creates `ResourceBar(on_settings_clicked=lambda: self.emit('show-settings'))`.
- `Sidebar` gains `show-settings` signal (no args).
- Context menu label "Open in Zellij" → "Open in Multiplexer". GObject signal name (`project-zellij`) and action name (`row.zellij`) are unchanged — only the visible label changes.

---

## Signal / Event Flow

```
SettingsWindow row change
  └─► settings.<field> = value
  └─► settings.save()
  └─► app.emit('settings-changed')
        └─► main._on_settings_changed()
              ├─► for each TerminalView: tv.apply_settings(settings)
              └─► if projects_dir changed vs _last_projects_dir:
                    projects_watcher.restart(new_path)
                    sidebar.refresh()
                    _last_projects_dir = new_path

ResourceBar gear click
  └─► on_settings_clicked callback
  └─► sidebar emits show-settings
        └─► window._on_open_settings()
              └─► SettingsWindow(settings, app, parent=self).present()
```

---

## Out of Scope (First Wave)

- Terminal color schemes / themes (Appearance tab is a placeholder, rendered insensitive)
- Default shell for "Open Bash"
- Per-project settings overrides
- Import/export settings
- Automatic migration from `~/claude-projects/`
- Path validation (invalid `projects_dir` yields empty project list silently)
