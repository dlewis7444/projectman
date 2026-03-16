import os
import re

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
