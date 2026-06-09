import os
import json
import sys
import tempfile


SESSION_FILE = os.path.expanduser('~/.ProjectMan/session.json')


DEFAULT_AGENT = 'claude'


def save_session(path, open_paths, focused_path, agents=None):
    """Atomically write session state.

    open_paths   : iterable of project path strings
    focused_path : focused project path, or None
    agents       : optional {path: agent_id} map. When None, entries are
                   written in the legacy plain-string form (byte-compatible
                   with v1). When given, each entry becomes the v2 dict form
                   ``{"path": p, "agent": a}``, defaulting absent paths to
                   ``DEFAULT_AGENT``.
    """
    if agents is None:
        entries = list(open_paths)
    else:
        entries = [
            {'path': p, 'agent': agents.get(p, DEFAULT_AGENT)}
            for p in open_paths
        ]
    data = {
        'open_paths': entries,
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


def _entry_path(entry):
    """Extract the project path from a session entry (v1 str or v2 dict).

    Returns the path string, or None for a malformed entry (e.g. a dict with no
    ``path`` key). v2 dict form: ``{"path": ..., "agent": ...}``.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        p = entry.get('path')
        return p if isinstance(p, str) else None
    return None


def load_session(path):
    """Load session state.

    Returns (open_paths, focused_path) on success, or ([], None) on any error.
    open_paths is deduplicated and contains only path strings — v2 dict entries
    (``{"path": ..., "agent": ...}``) are transparently reduced to their path,
    so every existing caller is unaffected. Use ``load_agents`` to recover the
    per-project agent.
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
        for entry in raw:
            p = _entry_path(entry)
            if p is not None and p not in seen:
                seen.add(p)
                deduped.append(p)
        return deduped, focused_path
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError,
            AttributeError):
        return [], None


def load_agents(path):
    """Return {path: agent_id} for the saved session.

    v1 (str) entries and v2 dict entries missing an ``agent`` default to
    ``DEFAULT_AGENT`` ('claude'). Returns {} on any error or missing file. The
    map is keyed by the same deduplicated paths ``load_session`` returns.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        raw = data.get('open_paths', [])
        if not isinstance(raw, list):
            return {}
        agents = {}
        for entry in raw:
            p = _entry_path(entry)
            if p is None or p in agents:
                continue
            if isinstance(entry, dict):
                agent = entry.get('agent')
                agents[p] = agent if isinstance(agent, str) and agent else DEFAULT_AGENT
            else:
                agents[p] = DEFAULT_AGENT
        return agents
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError,
            AttributeError):
        return {}


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
