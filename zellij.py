import os
import re
import subprocess

import gi
gi.require_version('Gio', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gio, GLib, GObject


def session_name(project_name: str) -> str:
    """Generate a stable zellij session name for a project."""
    slug = re.sub(r'[^a-zA-Z0-9_-]', '-', project_name)[:48] or 'default'
    return f'pm-{slug}'


def socket_dir() -> str:
    """Return the directory where zellij stores session sockets.

    Zellij 0.43.1+ nests sockets under a version subdirectory:
      $XDG_RUNTIME_DIR/zellij/<version>/<session-name>
    If a version subdir exists, return it; otherwise return the base dir
    (covers the case where zellij hasn't run yet or uses an older layout).
    """
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    base = os.path.join(xdg, 'zellij') if xdg else os.path.join('/tmp', f'zellij-{os.getuid()}')
    try:
        entries = sorted(
            [e for e in os.scandir(base) if e.is_dir()],
            key=lambda e: tuple(int(x) for x in e.name.split('.') if x.isdigit()),
            reverse=True,
        )
        if entries:
            return entries[0].path
    except FileNotFoundError:
        pass
    return base


def session_exists(name: str) -> bool:
    """Return True if a live zellij session socket exists for this name."""
    return os.path.exists(os.path.join(socket_dir(), name))


def session_alive(name: str) -> bool:
    """Return True if zellij reports this session as active (not EXITED).

    Uses `zellij list-sessions` rather than the socket file, so it correctly
    rejects stale/orphan sockets and EXITED sessions.
    """
    try:
        result = subprocess.run(
            ['zellij', 'list-sessions', '--no-formatting'],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == name and 'EXITED' not in line:
                return True
    except Exception:
        pass
    return False


def alive_session_names() -> set:
    """Return the set of all alive (non-EXITED) session names.

    Single subprocess call — use this when checking multiple sessions at once.
    """
    try:
        result = subprocess.run(
            ['zellij', 'list-sessions', '--no-formatting'],
            capture_output=True, text=True, timeout=2,
        )
        names = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and 'EXITED' not in line:
                names.add(parts[0])
        return names
    except Exception:
        pass
    return set()


class ZellijWatcher(GObject.GObject):
    """Watches the zellij socket directory and emits sessions-changed on any change."""
    __gsignals__ = {
        'sessions-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._monitor = None

    def start(self):
        # socket_dir() returns the versioned subdir if it exists; monitor that.
        # If zellij hasn't run yet the base dir is returned — that's fine,
        # sockets will appear once zellij starts.
        sdir = socket_dir()
        os.makedirs(sdir, exist_ok=True)
        f = Gio.File.new_for_path(sdir)
        self._monitor = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect('changed', self._on_changed)

    def stop(self):
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None

    def _on_changed(self, monitor, file, other_file, event_type):
        if event_type in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
        ):
            self.emit('sessions-changed')
