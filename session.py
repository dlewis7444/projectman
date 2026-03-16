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
    tmp = None
    try:
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f'ProjectMan: failed to save session: {e}', file=sys.stderr)
        if tmp is not None:
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
