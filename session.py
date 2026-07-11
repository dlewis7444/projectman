import os
import json
import sys
import tempfile


SESSION_FILE = os.path.expanduser('~/.ProjectMan/session.json')


DEFAULT_HARNESS = 'claude'
LOCALHOST_ID = 'localhost'


def save_session(path, open_paths, focused_path, harnesses=None, hosts=None):
    """Atomically write session state.

    open_paths   : iterable of project path strings (session keys; localhost
                   still uses absolute paths for back-compat)
    focused_path : focused project path, or None
    harnesses    : optional {path: harness_id} map. When None, entries are
                   written in the legacy plain-string form (byte-compatible
                   with v1). When given, each entry becomes the v2/v3 dict form
                   ``{"path": p, "harness": a, ...}``, defaulting absent paths
                   to ``DEFAULT_HARNESS``.
    hosts        : optional {path: host_id} map. When provided (with harnesses),
                   each dict entry also gets ``"host": host_id`` (v3). Absent
                   paths default to ``localhost``.
    """
    if harnesses is None and hosts is None:
        entries = list(open_paths)
    else:
        entries = []
        for p in open_paths:
            entry = {
                'path': p,
                'harness': (harnesses or {}).get(p, DEFAULT_HARNESS),
            }
            if hosts is not None:
                entry['host'] = hosts.get(p, LOCALHOST_ID)
            entries.append(entry)
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
    ``path`` key). v2 dict form: ``{"path": ..., "harness": ...}``.
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
    (``{"path": ..., "harness": ...}``) are transparently reduced to their path,
    so every existing caller is unaffected. Use ``load_harnesses`` to recover the
    per-project harness.
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


def load_harnesses(path):
    """Return {path: harness_id} for the saved session.

    v1 (str) entries and v2 dict entries missing a harness default to
    ``DEFAULT_HARNESS`` ('claude'). Dual-reads ``harness`` (preferred) and
    legacy ``agent``. Returns {} on any error or missing file. The map is keyed
    by the same deduplicated paths ``load_session`` returns.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        raw = data.get('open_paths', [])
        if not isinstance(raw, list):
            return {}
        out = {}
        for entry in raw:
            p = _entry_path(entry)
            if p is None or p in out:
                continue
            if isinstance(entry, dict):
                hid = entry.get('harness')
                if not (isinstance(hid, str) and hid):
                    hid = entry.get('agent')
                out[p] = hid if isinstance(hid, str) and hid else DEFAULT_HARNESS
            else:
                out[p] = DEFAULT_HARNESS
        return out
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError,
            AttributeError):
        return {}


def load_hosts(path):
    """Return {path: host_id} for the saved session.

    v1/v2 entries (no host field) default to ``localhost``. Returns {} on any
    error. Keyed by the same paths as ``load_session`` / ``load_harnesses``.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        raw = data.get('open_paths', [])
        if not isinstance(raw, list):
            return {}
        out = {}
        for entry in raw:
            p = _entry_path(entry)
            if p is None or p in out:
                continue
            if isinstance(entry, dict):
                hid = entry.get('host')
                out[p] = hid if isinstance(hid, str) and hid else LOCALHOST_ID
            else:
                out[p] = LOCALHOST_ID
        return out
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


def collect_harnesses_map(terminals, open_paths, effective_harness_fn):
    """Compute {path: harness_id} for the session snapshot — the RUNNING harness.

    terminals          : dict[path → TerminalView-like] (needs
                         .spawned_harness_signature())
    open_paths         : paths being persisted (from collect_session_state)
    effective_harness_fn : fallback resolver (settings.effective_harness), used
                         ONLY for a path missing from terminals (defensive —
                         open_paths is sourced from terminals, so this leg is
                         normally never taken)

    The persisted harness must be the one the live child was actually spawned
    with (spawn-time truth, the same source the restart prompt reads), NOT the
    one settings would resolve today. Saved-harness-wins (A2) restores a project
    with its saved harness even when settings disagree; deriving the save half
    from settings would re-save that project under the settings harness and the
    NEXT restore would silently drop the running session.
    """
    out = {}
    for path in open_paths:
        tv = terminals.get(path)
        if tv is not None:
            out[path] = tv.spawned_harness_signature()
        else:
            out[path] = effective_harness_fn(path)
    return out


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


def plan_emergency_kill(terminals):
    """Select which terminals to kill on a SIGTERM/SIGHUP emergency shutdown.

    terminals : dict[path → TerminalView-like] (duck-typed: needs
                ``._child_pid`` and ``._is_zellij``)
    Returns   : list[str] — paths of DIRECT-spawn terminals with a live child,
                in dict-iteration order.

    Only direct-spawn children get killed: zellij terminals are skipped because
    their sessions persist by design (the product's detach value — a logout must
    not tear them down). Terminals with no live child (``_child_pid is None``)
    are skipped too.
    """
    return [
        path for path, tv in terminals.items()
        if tv._child_pid is not None and not tv._is_zellij
    ]


def should_save_session(any_started_this_run, existing_open_paths):
    """FB-2 (power #3/#8, C7/C8): whether a close-time save may overwrite
    session.json — the session-erasure guard.

    A FAILED restore (e.g. a zellij/no-auth session that dies the instant it
    restores) leaves NOTHING running; the close-time save would then write an
    EMPTY open_paths over the last good session, silently erasing it. So:

      * if SOMETHING started this run (any_started_this_run) → SAVE — this is the
        normal path, and a deliberate close-everything (the user really did close
        all sessions after starting them) still saves the empty result; AND
      * if NOTHING started this run but the existing session.json ALSO has no open
        paths → SAVE — there's nothing to lose (a fresh install, or a prior empty
        session); BUT
      * if NOTHING started this run AND the existing session.json HAS open paths →
        SKIP — preserve the last good session (a failed restore can never erase
        it).

    Pure boolean decision (the AppWindow logs the skip + reads the existing
    paths). Returns True to save, False to skip.
    """
    if any_started_this_run:
        return True
    return not existing_open_paths


def should_quit_app(primary_window, closing_window):
    """Decide whether closing ``closing_window`` should quit the whole app.

    True iff the window being closed IS the primary window, or there is no
    primary window recorded (``None``). A stray/duplicate window closing must
    not quit the app and take unrelated sessions down with it — defense in
    depth for the duplicate-window class.
    """
    return primary_window is closing_window or primary_window is None


def paa_throb_decision(count, prev_count, unseen, window_open):
    """Decide whether the PAA find-indicator should throb (reveal-3 item 2, G2).

    Constitution C10 (indicators are level-truthful): the throb means "findings
    await you", a LEVEL fact, but it was wired EDGE-triggered
    (``count > prev_count``) AND ``prev_count`` seeded from the persisted ledger
    — so pending findings that survive a restart, or that already exist when PAA
    is enabled mid-session, NEVER armed the button. It showed the count and sat
    inert (18-pending bench precondition).

    The unseen-pending rule. Pure decision (precedent: ``should_quit_app`` /
    ``plan_emergency_kill``):

      * ``count``       — pending findings this emission delivers.
      * ``prev_count``  — pending count from the prior emission.
      * ``unseen``      — has the user NOT yet looked at the current findings
                          this session? Starts True at window construction; set
                          False when the PAA window opens.
      * ``window_open`` — is the PAA card window currently open?

    Returns ``(throb, unseen_after)``:

      * ``throb`` is True iff the window is closed, there is something to see
        (``count > 0``), and either the findings GREW (``count > prev_count`` —
        genuine news) or they are still unseen (the standing-pending arming the
        edge rule missed). A decrease (auto-resolve sweep) is never news.
      * ``unseen_after`` re-arms to True when ``count > prev_count`` (new
        findings are unseen again even after a prior look); otherwise it carries
        ``unseen`` unchanged. Opening the window — which flips ``unseen`` False —
        is the caller's job (``_on_show_paa_window``), not this function's.
    """
    unseen_after = True if count > prev_count else unseen
    throb = window_open is False and count > 0 and (count > prev_count or unseen)
    return throb, unseen_after
