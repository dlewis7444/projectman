# Session Restore Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remember which projects had running claude processes at close time and restore them (with the focused pane) on next launch.

**Architecture:** A new `session.py` module holds all pure-Python I/O logic (`save_session` / `load_session` / `filter_active_paths`) — fully testable without GTK. `AppWindow` gains thin `_save_session()` and `_restore_session()` methods that delegate to `session.py`. Save is triggered at close-commit time; restore replaces `_activate_last_project()` at startup.

**Tech Stack:** Python 3, GTK4/Adwaita/VTE (existing), `pytest` for tests, `tempfile.mkstemp` for atomic writes.

**Spec:** `docs/superpowers/specs/2026-03-15-session-restore-design.md`

---

## Chunk 1: Settings rename + session.py module

### Task 1: Rename `resume_last_project` → `resume_projects`

**Files:**
- Modify: `settings.py:14`
- Modify: `tests/test_settings.py`
- Modify: `window.py:270`
- Modify: `settings_window.py:60-64,175`

- [ ] **Step 1: Write the failing tests**

In `tests/test_settings.py`, make three changes:

**a)** Change line 11:
```python
# before
assert s.resume_last_project is True
# after
assert s.resume_projects is True
```

**b)** Add to `test_defaults` (right after the line above):
```python
assert 'resume_last_project' not in Settings.__dataclass_fields__
```

**c)** Add a new test at the bottom of the file:
```python
def test_load_ignores_old_resume_last_project_key(tmp_path):
    """Old settings.json with resume_last_project is silently upgraded."""
    path = tmp_path / 'settings.json'
    path.write_text('{"resume_last_project": false}')
    s = Settings.load(str(path))
    # Old key is ignored; new field uses dataclass default (True)
    assert s.resume_projects is True
```

- [ ] **Step 2: Run tests — expect ERROR/FAIL**

```bash
cd /home/dlewis/.ProjectMan/projects/projectman
python -m pytest tests/test_settings.py -v 2>&1 | tail -20
```

Expected: `test_defaults` reports `ERROR` (AttributeError on `resume_projects`) and the old `assert s.resume_last_project` assertion fails. `test_load_ignores_old_resume_last_project_key` errors on `AttributeError`.

- [ ] **Step 3: Rename the field in `settings.py`**

In `settings.py` line 14:
```python
# before
resume_last_project: bool = True
# after
resume_projects: bool = True
```

- [ ] **Step 4: Run settings tests — expect PASS**

```bash
python -m pytest tests/test_settings.py -v 2>&1 | tail -20
```

Expected: all `test_settings.py` tests pass.

- [ ] **Step 5: Update remaining references to the old field name**

In `window.py` line 270:
```python
# before
if not self._settings.resume_last_project:
# after
if not self._settings.resume_projects:
```

In `settings_window.py` lines 60–64 (the SwitchRow constructor + set_active):
```python
# before
self._resume_row = Adw.SwitchRow(
    title='Resume Last Project',
    subtitle='Auto-open the last active project on launch',
)
self._resume_row.set_active(self._settings.resume_last_project)
# after
self._resume_row = Adw.SwitchRow(
    title='Resume projects on startup',
    subtitle='Restore all active projects from the last session',
)
self._resume_row.set_active(self._settings.resume_projects)
```

In `settings_window.py` line 175:
```python
# before
self._settings.resume_last_project = row.get_active()
# after
self._settings.resume_projects = row.get_active()
```

Note: `window.py` and `settings_window.py` changes are not covered by automated tests (GTK imports prevent headless testing). Verify by grepping that no `resume_last_project` references remain:
```bash
grep -r resume_last_project . --include="*.py"
```
Expected: no output.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add settings.py settings_window.py window.py tests/test_settings.py
git commit -m "refactor: rename resume_last_project → resume_projects, update Settings UI label"
```

---

### Task 2: Add `session.py` with I/O + filtering helpers

`session.py` is an implementation-level extraction: the spec places logic in AppWindow methods, but the pure-Python portions are extracted here to enable headless unit testing. The AppWindow methods in Chunk 2 are thin wrappers.

**Files:**
- Create: `session.py`
- Create: `tests/test_session_restore.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_restore.py`:

```python
import json
import os
import types
import pytest
from session import save_session, load_session, filter_active_paths


# ── save_session ──────────────────────────────────────────────────────────────

def test_save_writes_correct_json(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a', '/b'], '/a')
    data = json.loads(open(path).read())
    assert data['open_paths'] == ['/a', '/b']
    assert data['focused_path'] == '/a'


def test_save_null_focused_path(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], None)
    data = json.loads(open(path).read())
    assert data['focused_path'] is None


def test_save_empty_session(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, [], None)
    data = json.loads(open(path).read())
    assert data['open_paths'] == []
    assert data['focused_path'] is None


def test_save_creates_directory(tmp_path):
    path = str(tmp_path / 'nested' / 'dir' / 'session.json')
    save_session(path, ['/x'], '/x')
    assert os.path.exists(path)


def test_save_atomic_no_temp_files(tmp_path):
    """After a successful write only the final file remains, no .tmp leftovers."""
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], '/a')
    files = [f.name for f in tmp_path.iterdir()]
    assert files == ['session.json']


def test_save_swallows_write_error(tmp_path, capsys):
    """A permission error must not raise; error is printed to stderr."""
    path = str(tmp_path / 'session.json')
    os.chmod(tmp_path, 0o444)
    try:
        save_session(path, ['/a'], '/a')  # must not raise
    finally:
        os.chmod(tmp_path, 0o755)
    captured = capsys.readouterr()
    assert 'ProjectMan' in captured.err


# ── load_session ──────────────────────────────────────────────────────────────

def test_load_returns_empty_on_missing_file(tmp_path):
    paths, focused = load_session(str(tmp_path / 'nonexistent.json'))
    assert paths == []
    assert focused is None


def test_load_returns_empty_on_corrupt_json(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text('not json!!!')
    paths, focused = load_session(str(path))
    assert paths == []
    assert focused is None


def test_load_returns_correct_data(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b'], 'focused_path': '/a'}))
    paths, focused = load_session(str(path))
    assert paths == ['/a', '/b']
    assert focused == '/a'


def test_load_deduplicates_paths(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a', '/b', '/a'], 'focused_path': '/a'}))
    paths, _ = load_session(str(path))
    assert paths == ['/a', '/b']


def test_load_returns_empty_on_non_list_open_paths(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': 'not-a-list', 'focused_path': None}))
    paths, focused = load_session(str(path))
    assert paths == []
    assert focused is None


def test_load_null_focused_path(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a'], 'focused_path': None}))
    _, focused = load_session(str(path))
    assert focused is None


def test_load_missing_focused_path_key(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text(json.dumps({'open_paths': ['/a']}))
    paths, focused = load_session(str(path))
    assert paths == ['/a']
    assert focused is None


# ── filter_active_paths ───────────────────────────────────────────────────────

def _proj(path):
    """Minimal Project-like object."""
    p = types.SimpleNamespace()
    p.path = path
    p.name = os.path.basename(path)
    return p


def test_filter_returns_only_matching_active_projects():
    active = [_proj('/a'), _proj('/b')]
    result = filter_active_paths(['/a', '/b', '/c'], active)
    assert set(result.keys()) == {'/a', '/b'}


def test_filter_excludes_archived_paths():
    """Archived projects are not passed in; absent from result."""
    active = [_proj('/a')]          # /b is 'archived' — not in active list
    result = filter_active_paths(['/a', '/b'], active)
    assert '/b' not in result
    assert '/a' in result


def test_filter_excludes_deleted_paths():
    """Paths deleted since last save are absent from active list → excluded."""
    active = [_proj('/a')]
    result = filter_active_paths(['/a', '/deleted'], active)
    assert '/deleted' not in result


def test_filter_preserves_project_objects():
    proj_a = _proj('/a')
    result = filter_active_paths(['/a'], [proj_a])
    assert result['/a'] is proj_a


def test_filter_empty_open_paths():
    result = filter_active_paths([], [_proj('/a')])
    assert result == {}


def test_filter_empty_active_projects():
    result = filter_active_paths(['/a'], [])
    assert result == {}
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/test_session_restore.py -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'filter_active_paths' from 'session'` (module does not exist yet).

- [ ] **Step 3: Implement `session.py`**

Create `session.py`:

```python
import os
import json
import sys
import tempfile


SESSION_FILE = os.path.expanduser('~/.ProjectMan/session.json')


def save_session(path, open_paths, focused_path):
    """Atomically write session state.

    open_paths   : iterable of project path strings
    focused_path : focused project path, or None
    """
    data = {
        'open_paths': list(open_paths),
        'focused_path': focused_path,
    }
    dir_path = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f'ProjectMan: failed to save session: {e}', file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_session(path):
    """Load session state.

    Returns (open_paths, focused_path) on success, or ([], None) on any error.
    open_paths is deduplicated and contains only string entries.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        raw = data.get('open_paths', [])
        if not isinstance(raw, list):
            return [], None
        focused_path = data.get('focused_path')
        seen = set()
        deduped = []
        for p in raw:
            if isinstance(p, str) and p not in seen:
                seen.add(p)
                deduped.append(p)
        return deduped, focused_path
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError,
            AttributeError):
        return [], None


def filter_active_paths(open_paths, active_projects):
    """Return {path: Project} for paths present in active_projects.

    active_projects should be the result of ProjectStore.load_projects() —
    archived projects are excluded by the caller, not here.
    """
    active = {p.path: p for p in active_projects}
    return {path: active[path] for path in open_paths if path in active}
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m pytest tests/test_session_restore.py -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add session.py tests/test_session_restore.py
git commit -m "feat: add session.py (save_session, load_session, filter_active_paths)"
```

---

## Chunk 2: AppWindow integration

GTK widget methods cannot be unit-tested without a live display. The solution: extract
all decision logic into two pure-Python helpers added to `session.py`, TDD those
helpers, then write trivially-thin AppWindow wrappers that compose already-tested
functions. The wrappers have no branches of their own, so they need no further tests.

### Task 3: Add `collect_session_state` to `session.py`, then wire `_save_session`

**Files:**
- Modify: `session.py` — add `collect_session_state`
- Modify: `tests/test_session_restore.py` — tests for `collect_session_state`
- Modify: `window.py` — import, add `_save_session()`, wire into both close paths

- [ ] **Step 1: Write failing tests for `collect_session_state`**

Append to `tests/test_session_restore.py` (after the existing `filter_active_paths`
block):

```python
# ── collect_session_state ─────────────────────────────────────────────────────

from session import collect_session_state


def _tv(pid):
    """Minimal TerminalView stand-in."""
    return types.SimpleNamespace(_child_pid=pid)


def test_collect_includes_only_running_terminals():
    terminals = {'/a': _tv(42), '/b': _tv(None)}
    paths, _ = collect_session_state(terminals, '/a')
    assert paths == ['/a']


def test_collect_focused_path_when_active_is_running():
    terminals = {'/a': _tv(1)}
    _, focused = collect_session_state(terminals, '/a')
    assert focused == '/a'


def test_collect_focused_null_when_active_has_no_process():
    terminals = {'/a': _tv(None), '/b': _tv(1)}
    _, focused = collect_session_state(terminals, '/a')
    assert focused is None


def test_collect_focused_null_when_active_path_is_none():
    terminals = {'/a': _tv(1)}
    _, focused = collect_session_state(terminals, None)
    assert focused is None


def test_collect_empty_when_no_terminals_running():
    terminals = {'/a': _tv(None), '/b': _tv(None)}
    paths, focused = collect_session_state(terminals, '/a')
    assert paths == []
    assert focused is None


def test_collect_empty_terminals():
    paths, focused = collect_session_state({}, None)
    assert paths == []
    assert focused is None
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
cd /home/dlewis/.ProjectMan/projects/projectman
python -m pytest tests/test_session_restore.py -v -k collect 2>&1 | tail -15
```

Expected: `ImportError: cannot import name 'collect_session_state' from 'session'`.

- [ ] **Step 3: Add `collect_session_state` to `session.py`**

Append to `session.py` (after `filter_active_paths`):

```python
def collect_session_state(terminals, active_path):
    """Compute (open_paths, focused_path) from AppWindow terminal state.

    terminals   : dict[path → TerminalView-like] (needs ._child_pid attr)
    active_path : currently visible project path, or None
    Returns     : (open_paths: list[str], focused_path: str | None)
    """
    seen = set()
    open_paths = []
    for path, tv in terminals.items():
        if tv._child_pid is not None and path not in seen:
            seen.add(path)
            open_paths.append(path)
    focused = active_path if active_path in seen else None
    return open_paths, focused
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m pytest tests/test_session_restore.py -v -k collect 2>&1 | tail -15
```

Expected: all `collect_session_state` tests pass.

- [ ] **Step 5: Import session helpers in `window.py`**

Add after the last local import (after `from model import Project`):

```python
from session import save_session, load_session, filter_active_paths, \
    collect_session_state, SESSION_FILE
```

- [ ] **Step 6: Add `_save_session()` to `AppWindow`**

Add this method after `_open_shutdown_window` (after line 115):

```python
def _save_session(self):
    """Snapshot running terminals to SESSION_FILE (atomic write)."""
    if not self._settings.resume_projects:
        return
    open_paths, focused = collect_session_state(self._terminals, self._active_path)
    save_session(SESSION_FILE, open_paths, focused)
```

- [ ] **Step 7: Call `_save_session()` in both close-commit paths**

**Path A — no running processes (immediate close).** In `_on_close_request`, before
`return False` (currently line 79):

```python
if not running:
    self._save_session()      # write empty session; restore is a no-op
    return False
```

**Path B — running processes.** In `_open_shutdown_window`, add as the very first
line:

```python
def _open_shutdown_window(self, running):
    self._save_session()      # snapshot before SIGTERM
    ShutdownWindow(parent=self, running=running, on_complete=self.destroy)
```

- [ ] **Step 8: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add session.py tests/test_session_restore.py window.py
git commit -m "feat: add collect_session_state, wire _save_session into close flow"
```

---

### Task 4: Add `plan_restore` to `session.py`, then wire `_restore_session`

**Files:**
- Modify: `session.py` — add `plan_restore`
- Modify: `tests/test_session_restore.py` — tests for `plan_restore`
- Modify: `window.py` — add `_restore_session()`, remove `_activate_last_project()`
- Modify: `main.py:77`

- [ ] **Step 1: Write failing tests for `plan_restore`**

Append to `tests/test_session_restore.py` (after the `collect_session_state` block):

```python
# ── plan_restore ──────────────────────────────────────────────────────────────

from session import plan_restore


def test_plan_restore_focused_in_active_set():
    active = {'/a': _proj('/a'), '/b': _proj('/b')}
    focused, bg = plan_restore(['/a', '/b'], '/a', active)
    assert focused == '/a'
    assert bg == ['/b']


def test_plan_restore_focused_null_when_not_in_active():
    """focused_path is in open_paths but not in active (e.g. archived)."""
    active = {'/b': _proj('/b')}
    focused, bg = plan_restore(['/a', '/b'], '/a', active)
    assert focused is None
    assert bg == ['/b']


def test_plan_restore_focused_null_when_none():
    active = {'/a': _proj('/a')}
    focused, bg = plan_restore(['/a'], None, active)
    assert focused is None
    assert bg == ['/a']


def test_plan_restore_background_excludes_focused():
    active = {'/a': _proj('/a'), '/b': _proj('/b'), '/c': _proj('/c')}
    focused, bg = plan_restore(['/a', '/b', '/c'], '/b', active)
    assert focused == '/b'
    assert '/b' not in bg
    assert set(bg) == {'/a', '/c'}


def test_plan_restore_preserves_order():
    active = {'/a': _proj('/a'), '/b': _proj('/b'), '/c': _proj('/c')}
    focused, bg = plan_restore(['/a', '/b', '/c'], '/a', active)
    assert bg == ['/b', '/c']


def test_plan_restore_empty_active():
    focused, bg = plan_restore(['/a'], '/a', {})
    assert focused is None
    assert bg == []


def test_plan_restore_empty_open_paths():
    active = {'/a': _proj('/a')}
    focused, bg = plan_restore([], None, active)
    assert focused is None
    assert bg == []
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/test_session_restore.py -v -k plan_restore 2>&1 | tail -15
```

Expected: `ImportError: cannot import name 'plan_restore' from 'session'`.

- [ ] **Step 3: Add `plan_restore` to `session.py`**

Append to `session.py` (after `collect_session_state`):

```python
def plan_restore(open_paths, focused_path, active_map):
    """Compute what to activate vs spawn in the background during restore.

    open_paths   : deduplicated list from load_session
    focused_path : path to show in the main pane, or None
    active_map   : {path: Project} from filter_active_paths
    Returns      : (focused: str|None, background: list[str])
                   focused  — path to activate (None if not in active_map)
                   background — remaining paths in active_map, in open_paths order
    """
    focused = focused_path if focused_path and focused_path in active_map else None
    background = [p for p in open_paths if p in active_map and p != focused_path]
    return focused, background
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m pytest tests/test_session_restore.py -v -k plan_restore 2>&1 | tail -15
```

Expected: all `plan_restore` tests pass.

- [ ] **Step 5: Add `plan_restore` to the import in `window.py`**

Update the session import line added in Task 3:

```python
from session import save_session, load_session, filter_active_paths, \
    collect_session_state, plan_restore, SESSION_FILE
```

- [ ] **Step 6: Add `_restore_session()` to `AppWindow`**

Add directly after `_save_session()`:

```python
def _restore_session(self):
    """Restore projects that were running at the last committed close."""
    if not self._settings.resume_projects:
        return
    open_paths, focused_path = load_session(SESSION_FILE)
    active = filter_active_paths(open_paths, self._store.load_projects())
    focused, background = plan_restore(open_paths, focused_path, active)
    if focused:
        self._on_project_activated(self._sidebar, focused)
    for path in background:
        tv = self._get_or_create_terminal(active[path])
        tv.spawn_claude(project_name=active[path].name)
```

- [ ] **Step 7: Remove `_activate_last_project()`**

Delete the entire method (starts with `def _activate_last_project(self):`, ~13 lines).
Confirm the location first:

```bash
grep -n "_activate_last_project" window.py
```

- [ ] **Step 8: Update `main.py`**

Find the call site:

```bash
grep -n "_activate_last_project" main.py
```

Replace:

```python
# before
self._window._activate_last_project()
# after
self._window._restore_session()
```

- [ ] **Step 9: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 10: Smoke test**

Launch the app, open 2–3 projects, start claude in each. Close PM. Reopen:

```bash
python main.py
```

Verify:
- All previously-running projects appear with running-dot indicators
- The project that was in the main pane is shown again
- `claude -c` has been launched in each restored terminal
- Session file has correct content:
  ```bash
  cat ~/.ProjectMan/session.json
  ```

- [ ] **Step 11: Final commit**

```bash
git add session.py tests/test_session_restore.py window.py main.py
git commit -m "feat: add plan_restore, wire _restore_session into startup, remove _activate_last_project"
```

---

## Final verification

- [ ] **Full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass, zero failures.

- [ ] **No dead references**

```bash
grep -r "resume_last_project\|_activate_last_project" . --include="*.py"
```

Expected: no output.
