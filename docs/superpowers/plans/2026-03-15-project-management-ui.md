# Project Management UI Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "New Session…" row to session history dropdowns, a "+" button for inline project creation, and inline project rename via context menu.

**Architecture:** New widget classes (`NewSessionRow`, `NewProjectEntryRow`) follow the existing `SessionHistoryRow`/`ProjectRow` pattern. Each widget owns its own state. `ProjectRow` grows a hidden rename entry that swaps with its name label in-place. Signals bubble up `ProjectRow → Sidebar → AppWindow` as before.

**Tech Stack:** Python 3.14, PyGObject, GTK4 4.20.3, libadwaita 1.8.4, pytest for pure-Python model tests.

---

## Chunk 1: Model Layer

### Task 1: Add `create_project` and `rename_project` to `ProjectStore`

**Files:**
- Modify: `model.py`
- Create: `tests/test_model_project_management.py`

- [ ] **Step 1.1: Write failing tests**

Create `tests/test_model_project_management.py`:

```python
import os
import pytest
from settings import Settings
from model import ProjectStore


def test_create_project(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    store.create_project('my-project')
    assert (tmp_path / 'my-project').is_dir()


def test_create_project_exist_ok(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'existing').mkdir()
    store.create_project('existing')  # must not raise
    assert (tmp_path / 'existing').is_dir()


def test_create_project_appears_in_load(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    store.create_project('new-project')
    projects = store.load_projects()
    assert any(p.name == 'new-project' for p in projects)


def test_rename_project(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'old-name').mkdir()
    projects = store.load_projects()
    store.rename_project(projects[0], 'new-name')
    assert (tmp_path / 'new-name').is_dir()
    assert not (tmp_path / 'old-name').exists()


def test_rename_project_appears_in_load(tmp_path):
    settings = Settings(projects_dir=str(tmp_path))
    store = ProjectStore(settings)
    (tmp_path / 'myproject').mkdir()
    projects = store.load_projects()
    store.rename_project(projects[0], 'renamed')
    new_projects = store.load_projects()
    assert any(p.name == 'renamed' for p in new_projects)
    assert not any(p.name == 'myproject' for p in new_projects)
```

- [ ] **Step 1.2: Run tests — confirm they all fail**

```bash
cd /home/dlewis/dev/projectman && python -m pytest tests/test_model_project_management.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'ProjectStore' object has no attribute 'create_project'` or similar.

- [ ] **Step 1.3: Add `create_project` and `rename_project` to `ProjectStore` in `model.py`**

In `model.py`, add these two methods to the `ProjectStore` class (after `restore`):

```python
    def create_project(self, name):
        path = os.path.join(self._projects_dir(), name)
        os.makedirs(path, exist_ok=True)

    def rename_project(self, project, new_name):
        new_path = os.path.join(self._projects_dir(), new_name)
        os.rename(project.path, new_path)
```

- [ ] **Step 1.4: Run tests — all must pass**

```bash
cd /home/dlewis/dev/projectman && python -m pytest tests/test_model_project_management.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 1.5: Run full suite to confirm no regressions**

```bash
cd /home/dlewis/dev/projectman && python -m pytest tests/ -v
```

Expected: all 24 tests PASS.

- [ ] **Step 1.6: Commit**

```bash
cd /home/dlewis/dev/projectman
git add model.py tests/test_model_project_management.py
git commit -m "feat: add create_project and rename_project to ProjectStore"
```

---

## Chunk 2: Sidebar Widget Additions

### Task 2: Add `NewSessionRow` and wire into `ProjectRow`

**Files:**
- Modify: `sidebar.py`

- [ ] **Step 2.1: Add `NewSessionRow` class to `sidebar.py`**

Add this class immediately before `class SessionHistoryRow` (line 276):

```python
class NewSessionRow(Gtk.ListBoxRow):
    """Top entry in the session history dropdown — starts a fresh Claude session."""
    def __init__(self):
        super().__init__()
        self.add_css_class('session-history-row')

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name('list-add-symbolic')
        icon.set_pixel_size(12)
        box.append(icon)

        label = Gtk.Label(label='New Session\u2026')
        label.set_halign(Gtk.Align.START)
        label.add_css_class('session-title')
        box.append(label)

        self.set_child(box)
```

- [ ] **Step 2.2: Update `ProjectRow._load_sessions()` to prepend `NewSessionRow`**

Replace the existing `_load_sessions` method (currently line 251–253):

```python
    def _load_sessions(self):
        self._session_listbox.append(NewSessionRow())
        for i, sess in enumerate(self._history.get_sessions(self._project)):
            self._session_listbox.append(SessionHistoryRow(sess, is_default=(i == 0)))
```

- [ ] **Step 2.3: Update `ProjectRow._on_session_activated()` to handle `NewSessionRow`**

Replace the existing `_on_session_activated` method (currently line 255–257):

```python
    def _on_session_activated(self, listbox, row):
        if isinstance(row, NewSessionRow):
            self.emit('project-new-claude')
        elif isinstance(row, SessionHistoryRow):
            self.emit('session-activated', self._project.path, row._session.session_id)
```

- [ ] **Step 2.4: Smoke-test import**

```bash
cd /home/dlewis/dev/projectman && python -c "from sidebar import Sidebar, NewSessionRow; print('OK')"
```

Expected: `OK`

- [ ] **Step 2.5: Commit**

```bash
cd /home/dlewis/dev/projectman
git add sidebar.py
git commit -m "feat: add NewSessionRow to session history dropdown"
```

---

### Task 3: Add "+" button to PROJECTS header and `NewProjectEntryRow`

**Files:**
- Modify: `sidebar.py`

- [ ] **Step 3.1: Replace standalone header label with header box + "+" button**

In `Sidebar.__init__`, replace these four lines (currently lines 33–36):

```python
        header = Gtk.Label(label='PROJECTS')
        header.add_css_class('sidebar-header')
        header.set_halign(Gtk.Align.START)
        self.append(header)
```

With:

```python
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header_box.add_css_class('sidebar-header')  # margin + font cascade to children
        header = Gtk.Label(label='PROJECTS')
        header.set_halign(Gtk.Align.START)
        header.set_hexpand(True)
        header_box.append(header)
        add_btn = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_btn.add_css_class('flat')
        add_btn.add_css_class('circular')
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.set_tooltip_text('New Project')
        add_btn.connect('clicked', self._on_add_project)
        header_box.append(add_btn)
        self.append(header_box)
```

Note: `sidebar-header` CSS class is moved to `header_box` so its `margin: 12px 12px 4px 12px` applies to the container row. Font styling cascades to the label naturally.

- [ ] **Step 3.2: Add `self._new_project_row = None` to `Sidebar.__init__`**

In `Sidebar.__init__`, after `self._rows = {}` (line 30), add:

```python
        self._new_project_row = None
```

- [ ] **Step 3.3: Add `project-create` signal to `Sidebar.__gsignals__`**

In `Sidebar.__gsignals__`, add after `'show-settings'`:

```python
        'project-create':   (GObject.SignalFlags.RUN_FIRST, None, (str,)),
```

- [ ] **Step 3.4: Add `NewProjectEntryRow` class to `sidebar.py`**

Add this class immediately before `class ProjectRow` (locate by class name — earlier insertions shift line numbers):

```python
class NewProjectEntryRow(Gtk.ListBoxRow):
    """Inline entry row for creating a new project directory."""
    def __init__(self, on_commit, on_cancel):
        super().__init__()
        self.set_selectable(False)
        self.set_activatable(False)
        self._on_commit = on_commit
        self._on_cancel = on_cancel

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name('folder-new-symbolic')
        box.append(icon)

        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text('Project name\u2026')
        self._entry.set_hexpand(True)
        self._entry.connect('activate', self._on_activate)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._entry.add_controller(key_ctrl)

        box.append(self._entry)
        self.set_child(box)
        GLib.idle_add(self._entry.grab_focus)

    def _on_activate(self, entry):
        name = entry.get_text().strip()
        if name and '/' not in name and not name.startswith('.'):
            self._on_commit(name)

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._on_cancel()
            return True
        return False
```

- [ ] **Step 3.5: Add `_on_add_project`, `_commit_new_project`, `_cancel_new_project` to `Sidebar`**

Add these three methods to `Sidebar` (after `start_polling`):

```python
    def _on_add_project(self, button):
        if self._new_project_row is not None:
            self._new_project_row._entry.grab_focus()
            return
        row = NewProjectEntryRow(
            on_commit=self._commit_new_project,
            on_cancel=self._cancel_new_project,
        )
        self._new_project_row = row
        self._listbox.prepend(row)

    def _commit_new_project(self, name):
        self._new_project_row = None
        self.emit('project-create', name)

    def _cancel_new_project(self):
        if self._new_project_row is None:
            return
        self._listbox.remove(self._new_project_row)
        self._new_project_row = None
```

- [ ] **Step 3.6: Smoke-test import**

```bash
cd /home/dlewis/dev/projectman && python -c "from sidebar import Sidebar, NewProjectEntryRow; print('OK')"
```

Expected: `OK`

- [ ] **Step 3.7: Commit**

```bash
cd /home/dlewis/dev/projectman
git add sidebar.py
git commit -m "feat: add '+' button and NewProjectEntryRow for inline project creation"
```

---

### Task 4: Inline rename in `ProjectRow`

**Files:**
- Modify: `sidebar.py`

- [ ] **Step 4.1: Add `project-rename` signal to `ProjectRow.__gsignals__`**

In `ProjectRow.__gsignals__`, add after `'project-edit-md'`:

```python
        'project-rename':     (GObject.SignalFlags.RUN_FIRST, None, (str,)),
```

- [ ] **Step 4.2: Replace anonymous label with `self._name_label` and add hidden `self._rename_entry`**

In `ProjectRow.__init__`, replace these four lines (currently lines 179–182, though prior task insertions shift line numbers — locate by content):

```python
        label = Gtk.Label(label=project.name)
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        top.append(label)
```

With:

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

- [ ] **Step 4.3: Add "Rename" to the context menu in `_setup_context_menu()`**

In `_setup_context_menu`, add to `menu`:

```python
        menu.append('Rename', 'row.rename')
```

Place it after `menu.append('New Claude Session', 'row.new-claude')` so it appears second in the context menu.

Then wire the action (add after the `_add(...)` calls, before `self.insert_action_group`):

```python
        rename_action = Gio.SimpleAction.new('rename', None)
        rename_action.connect('activate', lambda a, p: self._enter_rename_mode())
        ag.add_action(rename_action)
```

- [ ] **Step 4.4: Add rename mode methods to `ProjectRow`**

Add these four methods to `ProjectRow` (after `update_status`):

```python
    def _enter_rename_mode(self):
        self._rename_entry.set_text(self._project.name)
        self._rename_entry.select_region(0, -1)
        self._name_label.set_visible(False)
        self._rename_entry.set_visible(True)
        self._rename_entry.grab_focus()

    def _exit_rename_mode(self):
        self._rename_entry.set_visible(False)
        self._name_label.set_visible(True)

    def _on_rename_activate(self, entry):
        name = entry.get_text().strip()
        valid = (name and '/' not in name
                 and not name.startswith('.') and name != self._project.name)
        self._exit_rename_mode()
        if valid:
            self.emit('project-rename', name)

    def _on_rename_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._exit_rename_mode()
            return True
        return False
```

- [ ] **Step 4.5: Add `project-rename` signal to `Sidebar.__gsignals__` and wire in `_populate()`**

In `Sidebar.__gsignals__`, add after `'project-create'`:

```python
        'project-rename':   (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
```

In `Sidebar._populate()`, add after the `row.connect('project-edit-md', ...)` block:

```python
            row.connect('project-rename',
                        lambda r, new_name, p=proj.path: self.emit('project-rename', p, new_name))
```

- [ ] **Step 4.6: Smoke-test import**

```bash
cd /home/dlewis/dev/projectman && python -c "from sidebar import Sidebar, ProjectRow; print('OK')"
```

Expected: `OK`

- [ ] **Step 4.7: Commit**

```bash
cd /home/dlewis/dev/projectman
git add sidebar.py
git commit -m "feat: inline rename via ProjectRow context menu"
```

---

## Chunk 3: Window Wiring

### Task 5: Add `project-create` and `project-rename` handlers to `AppWindow`

**Files:**
- Modify: `window.py`

- [ ] **Step 5.1: Connect new signals in `AppWindow.__init__`**

In `window.py`, after `self._sidebar.connect('show-settings', self._on_open_settings)` (line 52), add:

```python
        self._sidebar.connect('project-create', self._on_project_create)
        self._sidebar.connect('project-rename', self._on_project_rename)
```

- [ ] **Step 5.2: Add `_on_project_create` handler**

Add this method to `AppWindow` (after `_on_open_settings`):

```python
    def _on_project_create(self, sidebar, name):
        self._store.create_project(name)
        self._sidebar.refresh()
```

- [ ] **Step 5.3: Add `_on_project_rename` handler**

Add this method after `_on_project_create`:

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

        # Migrate terminal stack entry so the running session survives the rename
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

- [ ] **Step 5.4: Smoke-test import**

```bash
cd /home/dlewis/dev/projectman && python -c "from window import AppWindow; print('OK')"
```

Expected: `OK`

- [ ] **Step 5.5: Run full test suite**

```bash
cd /home/dlewis/dev/projectman && python -m pytest tests/ -v
```

Expected: all 24 tests PASS.

- [ ] **Step 5.6: Commit**

```bash
cd /home/dlewis/dev/projectman
git add window.py
git commit -m "feat: wire project-create and project-rename handlers in AppWindow"
```
