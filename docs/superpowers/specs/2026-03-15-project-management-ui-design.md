# Project Management UI: New Session, New Project, Rename

**Date:** 2026-03-15
**Scope:** Three sidebar UI features: "New Session…" row in session history, "+" button for inline project creation, inline project rename via context menu.

---

## Overview

Three focused additions to `sidebar.py`, `window.py`, and `model.py`:

1. **"New Session…" row** — always at the top of each project's expanded session history; starts a fresh Claude session.
2. **"+" button** — next to the PROJECTS header; creates a new project directory inline.
3. **Inline rename** — "Rename" in the project context menu; edits the project name in-place.

Projects already sort alphabetically (`model.py:63`) — no change needed.

---

## Architecture

New widget classes follow the existing `SessionHistoryRow` / `ProjectRow` pattern. Each widget owns its own state and interaction logic. Signals bubble up the same chain: `ProjectRow` → `Sidebar` → `AppWindow`.

---

## Feature 1: "New Session…" Row

### Widget: `NewSessionRow(Gtk.ListBoxRow)`

A new class in `sidebar.py`. Renders with a `list-add-symbolic` icon and "New Session…" label using the existing `session-history-row` CSS class. Carries no session data. The row's child must be a `Gtk.Box` (matching the `.session-history-row > box` CSS selector used for left-indent).

### Integration

**`ProjectRow._load_sessions()`** — prepends one `NewSessionRow` before all `SessionHistoryRow` items:

```python
def _load_sessions(self):
    self._session_listbox.append(NewSessionRow())
    for i, sess in enumerate(self._history.get_sessions(self._project)):
        self._session_listbox.append(SessionHistoryRow(sess, is_default=(i == 0)))
```

**`ProjectRow._on_session_activated()`** — adds an `isinstance` branch:

```python
def _on_session_activated(self, listbox, row):
    if isinstance(row, NewSessionRow):
        self.emit('project-new-claude')
    elif isinstance(row, SessionHistoryRow):
        self.emit('session-activated', self._project.path, row._session.session_id)
```

No new signals required — reuses the existing `project-new-claude` signal already handled by `AppWindow._on_project_new_claude`.

---

## Feature 2: "+" Button and Inline Project Creation

### Header Change

Replace the standalone `Gtk.Label(label='PROJECTS')` in `Sidebar.__init__` with a horizontal `Gtk.Box` containing:
- The label (`set_hexpand(True)`)
- A flat circular `Gtk.Button` (`list-add-symbolic`, tooltip "New Project")

### Widget: `NewProjectEntryRow(Gtk.ListBoxRow)`

A new class in `sidebar.py`. Properties:
- `set_selectable(False)`, `set_activatable(False)`
- Contains a `folder-new-symbolic` icon and a `Gtk.Entry` (placeholder "Project name…", `set_hexpand(True)`)
- Entry is focused via `GLib.idle_add(self._entry.grab_focus)` after insertion
- Takes `on_commit(name: str)` and `on_cancel()` callbacks

**Entry behaviour:**
- **Enter (`activate` signal):** validates name (non-empty, no `/`, no leading `.`) → calls `on_commit(name)` (row is removed implicitly when `refresh()` rebuilds the listbox)
- **Escape (`EventControllerKey`):** calls `on_cancel()` (row is explicitly removed by `_cancel_new_project`)
- Invalid commit (fails validation): does nothing (stays open)

### `Sidebar` changes

New instance variable: `self._new_project_row = None`

New signal: `'project-create': (GObject.SignalFlags.RUN_FIRST, None, (str,))`

**`Sidebar._on_add_project(button)`:**
- If `self._new_project_row` is not None: grab focus on existing entry, return
- Otherwise: create `NewProjectEntryRow(on_commit=self._commit_new_project, on_cancel=self._cancel_new_project)`, store in `self._new_project_row`, `self._listbox.prepend(row)`

**`Sidebar._commit_new_project(name)`:**
- `self._new_project_row = None`
- `self.emit('project-create', name)`

**`Sidebar._cancel_new_project()`:**
- Guard: `if self._new_project_row is None: return`
- `self._listbox.remove(self._new_project_row)`
- `self._new_project_row = None`

### `AppWindow` changes

New signal connection: `self._sidebar.connect('project-create', self._on_project_create)`

```python
def _on_project_create(self, sidebar, name):
    self._store.create_project(name)
    self._sidebar.refresh()
```

### `ProjectStore` changes

```python
def create_project(self, name):
    path = os.path.join(self._projects_dir(), name)
    os.makedirs(path, exist_ok=True)
```

---

## Feature 3: Inline Rename

### `ProjectRow` changes

**Constructor:** store a reference to the name label and add a hidden entry widget to the same `top` box:

```python
self._name_label = Gtk.Label(label=project.name)
self._name_label.set_halign(Gtk.Align.START)
self._name_label.set_hexpand(True)
top.append(self._name_label)

self._rename_entry = Gtk.Entry()
self._rename_entry.set_hexpand(True)
self._rename_entry.set_visible(False)
self._rename_entry.connect('activate', self._on_rename_activate)
rename_key = Gtk.EventControllerKey.new()
rename_key.connect('key-pressed', self._on_rename_key)
self._rename_entry.add_controller(rename_key)
top.append(self._rename_entry)
```

**New signal:** `'project-rename': (GObject.SignalFlags.RUN_FIRST, None, (str,))` — carries `new_name` only; `old_path` is prepended by Sidebar.

**Context menu:** add `menu.append('Rename', 'row.rename')` and wire a `Gio.SimpleAction` that calls `self._enter_rename_mode()`.

**`_enter_rename_mode()`:**
```python
def _enter_rename_mode(self):
    self._rename_entry.set_text(self._project.name)
    self._rename_entry.select_region(0, -1)
    self._name_label.set_visible(False)
    self._rename_entry.set_visible(True)
    self._rename_entry.grab_focus()
```

**`_exit_rename_mode()`:**
```python
def _exit_rename_mode(self):
    self._rename_entry.set_visible(False)
    self._name_label.set_visible(True)
```

**`_on_rename_activate(entry)`:** validates (non-empty, no `/`, no leading `.`, different from current name) → `_exit_rename_mode()` → `self.emit('project-rename', new_name)`. If invalid or unchanged: `_exit_rename_mode()` (silent cancel).

**`_on_rename_key(ctrl, keyval, keycode, state)`:** Escape → `_exit_rename_mode()`, return True.

### `Sidebar` changes

New signal: `'project-rename': (GObject.SignalFlags.RUN_FIRST, None, (str, str))` — `(old_path, new_name)`

In `_populate()`, add:
```python
row.connect('project-rename',
    lambda r, new_name, p=proj.path: self.emit('project-rename', p, new_name))
```

### `AppWindow` changes

New signal connection: `self._sidebar.connect('project-rename', self._on_project_rename)`

```python
def _on_project_rename(self, sidebar, old_path, new_name):
    project = self._find_project(old_path)
    if not project:
        return
    new_path = os.path.join(os.path.dirname(old_path), new_name)
    try:
        self._store.rename_project(project, new_name)
    except OSError:
        return

    # Migrate terminal stack entry if the project had an open terminal
    if old_path in self._terminals:
        tv = self._terminals.pop(old_path)
        self._stack.remove(tv)
        self._stack.add_named(tv, new_path)
        self._terminals[new_path] = tv

    if self._active_path == old_path:
        self._active_path = new_path
        self._title.set_subtitle(new_name)

    self._sidebar.refresh()
```

### `ProjectStore` changes

```python
def rename_project(self, project, new_name):
    new_path = os.path.join(self._projects_dir(), new_name)
    os.rename(project.path, new_path)
```

---

## Signal Summary

| Signal | Owner | Arguments | New? |
|--------|-------|-----------|------|
| `project-new-claude` | `ProjectRow` / `Sidebar` | `(path: str)` | No — reused |
| `project-create` | `Sidebar` | `(name: str)` | **Yes** |
| `project-rename` | `ProjectRow` | `(new_name: str)` | **Yes** |
| `project-rename` | `Sidebar` | `(old_path: str, new_name: str)` | **Yes** |

---

## Files Changed

| File | Change |
|------|--------|
| `sidebar.py` | Add `NewSessionRow`, `NewProjectEntryRow`; update `Sidebar`, `ProjectRow` |
| `window.py` | Add `_on_project_create`, `_on_project_rename` handlers |
| `model.py` | Add `ProjectStore.create_project`, `ProjectStore.rename_project` |

---

## Not In Scope

- Project deletion (use Archive instead)
- Duplicate name error UI (silent no-op on invalid commit)
- Undo for rename or create
