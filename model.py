import os
import json
import shutil
from collections import deque
from dataclasses import dataclass

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GObject, Gio, GLib


STATUS_DIR = os.path.expanduser('~/.claude/projectman/status')
HISTORY_FILE = os.path.expanduser('~/.claude/history.jsonl')


@dataclass
class Project:
    name: str
    path: str
    is_archived: bool = False


@dataclass
class Session:
    session_id: str
    title: str
    last_active: int
    project_path: str


@dataclass
class StatusSnapshot:
    event: str
    cwd: str
    ts: int
    session: str
    tool: str = None
    state: str = 'done'


class ProjectStore:
    def __init__(self, settings):
        self._settings = settings

    def _projects_dir(self):
        return self._settings.resolved_projects_dir

    def _archive_dir(self):
        return os.path.join(self._projects_dir(), '.archive')

    def load_projects(self):
        projects = []
        try:
            for entry in os.scandir(self._projects_dir()):
                if entry.name.startswith('.'):
                    continue
                if entry.is_dir(follow_symlinks=True):
                    projects.append(Project(
                        name=entry.name,
                        path=os.path.realpath(entry.path),
                        is_archived=False,
                    ))
        except FileNotFoundError:
            pass
        projects.sort(key=lambda p: p.name)
        return projects

    def load_archived(self):
        os.makedirs(self._archive_dir(), exist_ok=True)
        projects = []
        try:
            for entry in os.scandir(self._archive_dir()):
                if entry.name.startswith('.'):
                    continue
                if entry.is_dir(follow_symlinks=True):
                    projects.append(Project(
                        name=entry.name,
                        path=os.path.realpath(entry.path),
                        is_archived=True,
                    ))
        except FileNotFoundError:
            pass
        projects.sort(key=lambda p: p.name)
        return projects

    def archive(self, project):
        os.makedirs(self._archive_dir(), exist_ok=True)
        src = os.path.join(self._projects_dir(), project.name)
        dest = os.path.join(self._archive_dir(), project.name)
        shutil.move(src, dest)

    def restore(self, project):
        src = os.path.join(self._archive_dir(), project.name)
        dest = os.path.join(self._projects_dir(), project.name)
        shutil.move(src, dest)

    def create_project(self, name):
        path = os.path.join(self._projects_dir(), name)
        os.makedirs(path, exist_ok=True)

    def rename_project(self, project, new_name):
        new_path = os.path.join(self._projects_dir(), new_name)
        os.rename(project.path, new_path)


class HistoryReader:
    def __init__(self):
        self._cache = {}

    def load(self):
        self._cache.clear()
        sessions = {}
        try:
            with open(HISTORY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = entry.get('sessionId', '')
                    if not sid:
                        continue
                    project = entry.get('project', '')
                    if not project:
                        continue
                    real_project = os.path.realpath(project)
                    ts = entry.get('timestamp', 0)
                    display = entry.get('display', '')

                    if sid not in sessions:
                        sessions[sid] = {
                            'title': display,
                            'last_active': ts,
                            'project_path': real_project,
                        }
                    else:
                        sessions[sid]['last_active'] = max(
                            sessions[sid]['last_active'], ts
                        )
        except FileNotFoundError:
            pass

        by_project = {}
        for sid, info in sessions.items():
            pp = info['project_path']
            if pp not in by_project:
                by_project[pp] = []
            by_project[pp].append(Session(
                session_id=sid,
                title=info['title'],
                last_active=info['last_active'],
                project_path=pp,
            ))

        for pp in by_project:
            by_project[pp].sort(key=lambda s: s.last_active, reverse=True)
            by_project[pp] = by_project[pp][:7]

        self._cache = by_project

    def get_sessions(self, project):
        return self._cache.get(project.path, [])


class StatusWatcher(GObject.GObject):
    __gsignals__ = {
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._status: dict = {}
        self._monitor = None

    def start(self):
        os.makedirs(STATUS_DIR, exist_ok=True)
        f = Gio.File.new_for_path(STATUS_DIR)
        self._monitor = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect('changed', self._on_changed)
        self._reload()

    def force_poll(self):
        self._reload()

    def _on_changed(self, monitor, file, other_file, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED,
                          Gio.FileMonitorEvent.CREATED,
                          Gio.FileMonitorEvent.DELETED):
            self._reload()
            GLib.timeout_add(800, self._delayed_poll)

    def _delayed_poll(self):
        self._reload()
        return False  # don't repeat

    def _reload(self):
        new_status = {}
        try:
            for entry in os.scandir(STATUS_DIR):
                if not entry.name.endswith('.json'):
                    continue
                try:
                    with open(entry.path, 'r') as f:
                        data = json.loads(f.read())
                    cwd = data.get('cwd', '')
                    if not cwd:
                        continue
                    try:
                        key = os.path.realpath(cwd)
                    except OSError:
                        continue
                    # Each status file represents a single cwd. Worktree
                    # status files are NOT rolled up to the parent project —
                    # they're independent Claude sessions in independent
                    # cwds, and conflating them lets stale worktree state
                    # leak into the parent's dot when a worktree session
                    # exits non-gracefully.
                    new_status[key] = StatusSnapshot(
                        event=data.get('event', ''),
                        cwd=cwd,
                        ts=data.get('ts', 0),
                        session=data.get('session', ''),
                        tool=data.get('tool'),
                        state=data.get('state', 'done'),
                    )
                except (OSError, json.JSONDecodeError):
                    continue
        except Exception:
            return  # PermissionError or missing dir — keep previous status
        self._status = new_status
        self.emit('status-changed')

    def get_project_status(self, project):
        snapshot = self._status.get(project.path)
        if snapshot is None:
            return 'idle'
        # If the same session later moved its cwd into a subdirectory (e.g.
        # `cd code && ...`), the hook writes a separate status file for that
        # path.  The project-root snapshot becomes stale while the newer
        # subdirectory snapshot holds the real state.  Pick the most recent
        # snapshot among the project root and any same-session subdirectories.
        # The session-ID guard keeps independent worktree sessions from leaking
        # in — they have different session IDs.
        prefix = project.path + os.sep
        best = snapshot
        for path, s in self._status.items():
            if (path.startswith(prefix)
                    and s.session == snapshot.session
                    and s.ts > best.ts):
                best = s
        return best.state


class ProjectsWatcher(GObject.GObject):
    """Watches a directory via inotify and emits projects-changed on any add/remove."""
    __gsignals__ = {
        'projects-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._monitor = None

    def start(self, path):
        os.makedirs(path, exist_ok=True)
        f = Gio.File.new_for_path(path)
        self._monitor = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect('changed', self._on_changed)

    def restart(self, new_path):
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        self.start(new_path)

    def _on_changed(self, monitor, file, other_file, event_type):
        if event_type in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        ):
            self.emit('projects-changed')


class ResourceReader:
    """Read CPU and RAM usage for ProjectMan and all its descendant processes."""

    _PAGE_SIZE = os.sysconf('SC_PAGESIZE')
    _CLK_TCK = os.sysconf('SC_CLK_TCK')
    _NUM_CPUS = os.cpu_count() or 1
    _WINDOW_SECONDS = 30.0

    def __init__(self):
        self._pid = os.getpid()
        # Per-PID tick counts from the previous sample. Tracking per-PID (rather
        # than a single summed total) keeps the delta meaningful when children
        # come and go — otherwise a new/exiting subprocess makes the sum jump.
        self._prev_pid_ticks = {}
        self._prev_time = None
        # Rolling window of (timestamp, dticks, dt) samples for the CPU average.
        self._samples = deque()

    def read(self):
        pids = self._get_tree(self._pid)
        cpu_pct = self._read_cpu(pids)
        mem_mb = self._read_mem(pids)
        return {
            'cpu_pct': cpu_pct,
            'mem_mb': mem_mb,
        }

    @staticmethod
    def _get_tree(root_pid):
        """Collect root_pid and all descendants via /proc/<pid>/task/*/children."""
        pids = []
        queue = [root_pid]
        while queue:
            pid = queue.pop()
            pids.append(pid)
            try:
                task_dir = f'/proc/{pid}/task'
                for tid in os.listdir(task_dir):
                    children_file = f'{task_dir}/{tid}/children'
                    try:
                        with open(children_file) as f:
                            for child in f.read().split():
                                queue.append(int(child))
                    except (FileNotFoundError, ValueError):
                        pass
            except FileNotFoundError:
                pass
        return pids

    def _read_cpu(self, pids):
        """Time-weighted average CPU% over a rolling ~30s window.

        Per-PID tick tracking ensures the delta ignores children that joined
        or exited between samples, so process-tree churn doesn't produce fake
        spikes or zeros.
        """
        pid_ticks = {}
        for pid in pids:
            try:
                with open(f'/proc/{pid}/stat') as f:
                    fields = f.read().rsplit(') ', 1)[1].split()
                # fields[11]=utime, fields[12]=stime (0-indexed after ')')
                pid_ticks[pid] = int(fields[11]) + int(fields[12])
            except (FileNotFoundError, IndexError, ValueError):
                pass

        # Sum deltas only across PIDs we saw in both samples. New PIDs are
        # recorded but contribute 0 this round; vanished PIDs are dropped.
        dticks = 0
        for pid, ticks in pid_ticks.items():
            prev = self._prev_pid_ticks.get(pid)
            if prev is not None:
                delta = ticks - prev
                if delta > 0:  # Guard against PID reuse (counter went backwards).
                    dticks += delta
        self._prev_pid_ticks = pid_ticks

        now = _monotonic()
        prev_time = self._prev_time
        self._prev_time = now
        if prev_time is None:
            return 0.0
        dt = now - prev_time
        if dt > 0:
            self._samples.append((now, dticks, dt))
            cutoff = now - self._WINDOW_SECONDS
            while len(self._samples) > 1 and self._samples[0][0] < cutoff:
                self._samples.popleft()

        total_ticks = sum(s[1] for s in self._samples)
        total_dt = sum(s[2] for s in self._samples)
        if total_dt <= 0:
            return 0.0
        secs_used = total_ticks / self._CLK_TCK
        return min(secs_used / total_dt * 100.0, self._NUM_CPUS * 100.0)

    def _read_mem(self, pids):
        """Total RSS of the process tree in MB."""
        total_pages = 0
        for pid in pids:
            try:
                with open(f'/proc/{pid}/statm') as f:
                    total_pages += int(f.read().split()[1])  # rss field
            except (FileNotFoundError, IndexError, ValueError):
                pass
        return total_pages * self._PAGE_SIZE / (1024 * 1024)


def _monotonic():
    """time.monotonic() imported lazily to keep module-level side-effects minimal."""
    import time
    return time.monotonic()
