import os
import json
import shutil
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
                    # Map worktree paths back to parent project
                    wt = '/.worktrees/'
                    wt_idx = key.find(wt)
                    if wt_idx != -1:
                        key = key[:wt_idx]
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
        return snapshot.state


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
    def __init__(self):
        self._prev_idle = 0
        self._prev_total = 0

    def read(self):
        cpu_pct = self._read_cpu()
        mem_used, mem_total = self._read_mem()
        return {
            'cpu_pct': cpu_pct,
            'mem_used_gb': mem_used,
            'mem_total_gb': mem_total,
        }

    def _read_cpu(self):
        try:
            with open('/proc/stat', 'r') as f:
                parts = f.readline().split()
            values = [int(x) for x in parts[1:]]
            idle = values[3] + values[4]
            total = sum(values)
            d_idle = idle - self._prev_idle
            d_total = total - self._prev_total
            self._prev_idle = idle
            self._prev_total = total
            if d_total == 0:
                return 0.0
            return (1.0 - d_idle / d_total) * 100.0
        except (FileNotFoundError, IndexError, ValueError):
            return 0.0

    def _read_mem(self):
        try:
            mem_total = 0
            mem_available = 0
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1])
                    elif line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1])
            total_gb = mem_total / (1024 * 1024)
            used_gb = (mem_total - mem_available) / (1024 * 1024)
            return used_gb, total_gb
        except (FileNotFoundError, IndexError, ValueError):
            return 0.0, 0.0
