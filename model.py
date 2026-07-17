import os
import json
import shutil
import time
from collections import deque
from dataclasses import dataclass

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GObject, Gio, GLib


# Canonical, harness-neutral status dir (PM-owned, Decision 2). The opencode
# bridge and (now) hook.js both write here.
STATUS_DIR = os.path.expanduser('~/.ProjectMan/status')
# Legacy Claude-scoped dir. StatusWatcher dual-watches it through the
# deprecation window (dropped at the P4 release) so an unmigrated hook.js or a
# stale status file keeps lighting dots.
LEGACY_STATUS_DIR = os.path.expanduser('~/.claude/projectman/status')
HISTORY_FILE = os.path.expanduser('~/.claude/history.jsonl')


@dataclass
class Project:
    name: str
    path: str
    is_archived: bool = False
    # Host axis: 'localhost' or a configured remote host id.
    # localhost: ``path`` is the local realpath.
    # remote: ``path`` is the unique project_ref (``ssh:<id>:<name>``) for
    # dict/stack keys; ``remote_cwd`` is the path used on the remote host.
    host_id: str = 'localhost'
    remote_cwd: str | None = None

    @property
    def project_ref(self) -> str:
        """Stable settings/session key for this project."""
        from hosts import encode_project_ref, LOCALHOST_ID
        if self.host_id == LOCALHOST_ID or not self.host_id:
            return encode_project_ref(LOCALHOST_ID, self.path)
        return encode_project_ref(self.host_id, self.name)

    @property
    def spawn_cwd(self) -> str:
        """Working directory for the harness process (local or remote)."""
        if self.remote_cwd:
            return self.remote_cwd
        return self.path


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
    # F11 phase-aging fields (agent-generic; today only the grok bridge stamps
    # them). ``phase`` marks a sub-state within ``state`` — currently only
    # ``pre_tool_use`` is meaningful: the harness fired its pre-tool hook and the
    # wire went silent, which is either a fast approved tool (clears in <1s) or
    # a permission prompt waiting on the human (grok has NO fires-at-prompt
    # event — mini-probe finding, 54s/21s observed silences). ``phase_ts`` is
    # the epoch the phase was stamped. Snapshots without these fields (claude,
    # opencode) keep the defaults and behave exactly as before.
    phase: str = None
    phase_ts: float = 0


# F11: how long a ``phase == 'pre_tool_use'`` working snapshot may age before
# the watcher promotes it to 'waiting'. Auto-approved tools complete in <0.3s
# (mini-probe observed), so 10s of pre-tool silence almost always means a
# permission prompt is on screen waiting for the human.
PHASE_WAITING_THRESHOLD = 10.0


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
        """Create a new project directory.

        Raises ValueError if the name is invalid (slash, shell metacharacters,
        empty, leading dot). Raises FileExistsError if a path with that name
        already exists — callers must not treat a pre-existing directory as a
        successful create (that produced a false "New project" toast).
        """
        from hosts import project_name_reject_reason
        reason = project_name_reject_reason(name)
        if reason:
            raise ValueError(reason)
        name = name.strip()
        path = os.path.join(self._projects_dir(), name)
        if os.path.exists(path):
            raise FileExistsError(path)
        os.makedirs(path)

    def rename_project(self, project, new_name):
        """Rename a local project directory.

        Uses the same name policy as :meth:`create_project` (no slash, no
        shell metacharacters, no leading dot). Raises ``ValueError`` on
        invalid names and ``FileExistsError`` if the destination exists.
        """
        from hosts import project_name_reject_reason
        reason = project_name_reject_reason(new_name)
        if reason:
            raise ValueError(reason)
        new_name = new_name.strip()
        new_path = os.path.join(self._projects_dir(), new_name)
        if os.path.exists(new_path):
            raise FileExistsError(new_path)
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

    def __init__(self, *, now_fn=None, schedule_fn=None):
        super().__init__()
        self._status: dict = {}
        # Remote (or other external) status keyed by Project.path — survives
        # local-dir _reload so SSH-polled snapshots are not wiped by inotify.
        self._external_status: dict = {}
        self._monitors = []
        # F11 phase-aging injection points. ``now_fn() -> epoch seconds`` is the
        # clock; ``schedule_fn(seconds, callback)`` arms a one-shot timer whose
        # callback re-emits when a phase crosses the waiting threshold. Defaults
        # are the real clock and a GLib timeout; tests inject both so the aging
        # logic runs instantly and deterministically.
        self._now = now_fn if now_fn is not None else time.time
        self._schedule = schedule_fn if schedule_fn is not None else self._glib_schedule
        # Generation counter invalidating armed phase timers: each publish bumps
        # it, and a timer whose generation is stale dies silently (no emit) —
        # so re-publishes never stack duplicate re-emits.
        self._phase_gen = 0

    @staticmethod
    def _glib_schedule(seconds, callback):
        """Default one-shot scheduler (GLib). ``callback`` returns False so the
        source does not repeat."""
        GLib.timeout_add(max(1, int(seconds * 1000)), callback)

    @staticmethod
    def _status_dirs():
        """Dual-watch dirs (Decision 2), read dynamically so tests that
        monkeypatch ``model.STATUS_DIR``/``LEGACY_STATUS_DIR`` are honored.
        Order: legacy first, new last (new supersedes on a same-cwd tie)."""
        return (LEGACY_STATUS_DIR, STATUS_DIR)

    def start(self):
        self._monitors = []
        for d in self._status_dirs():
            os.makedirs(d, exist_ok=True)
            f = Gio.File.new_for_path(d)
            mon = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            mon.connect('changed', self._on_changed)
            self._monitors.append(mon)
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
        scanned_any = False
        # Scan both dirs, newest-ts-wins per cwd. The new dir is scanned LAST so
        # that on a tie it supersedes a legacy file for the same cwd (the
        # migration direction). A missing dir is skipped, not fatal — as long as
        # ONE dir scanned we publish; if both are gone we keep prior status.
        for d in self._status_dirs():
            try:
                entries = list(os.scandir(d))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except Exception:
                continue  # PermissionError etc. — skip this dir, try the other
            scanned_any = True
            for entry in entries:
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
                    ts = data.get('ts', 0)
                    # Each status file represents a single cwd. Worktree
                    # status files are NOT rolled up to the parent project —
                    # they're independent agent sessions in independent
                    # cwds, and conflating them lets stale worktree state
                    # leak into the parent's dot when a worktree session
                    # exits non-gracefully. When the same cwd appears in both
                    # dirs, the newer ts wins (>= so the later-scanned new dir
                    # supersedes a same-ts legacy file).
                    existing = new_status.get(key)
                    if existing is not None and existing.ts > ts:
                        continue
                    phase_ts = data.get('phase_ts', 0)
                    new_status[key] = StatusSnapshot(
                        event=data.get('event', ''),
                        cwd=cwd,
                        ts=ts,
                        session=data.get('session', ''),
                        tool=data.get('tool'),
                        state=data.get('state', 'done'),
                        # F11: phase fields from phase-stamping bridges (grok).
                        # Absent fields keep the dataclass defaults — a
                        # claude/opencode snapshot is built exactly as before.
                        phase=data.get('phase'),
                        phase_ts=(phase_ts
                                  if isinstance(phase_ts, (int, float)) else 0),
                    )
                except (OSError, json.JSONDecodeError):
                    continue
        if not scanned_any:
            return  # both dirs gone/unreadable — keep previous status
        self._status = new_status
        self._arm_phase_timer(new_status)
        self.emit('status-changed')

    # --- F11: phase aging (working→waiting promotion) ----------------------

    def _phase_remaining(self, snapshot, now):
        """Seconds until ``snapshot`` crosses the waiting threshold, or None.

        None when the snapshot is not an un-aged pre_tool_use working phase —
        i.e. nothing to wait for (no phase fields, wrong state, or already past
        the threshold and thus already promoted on read).
        """
        if (snapshot.state == 'working'
                and snapshot.phase == 'pre_tool_use'
                and snapshot.phase_ts):
            remaining = PHASE_WAITING_THRESHOLD - (now - snapshot.phase_ts)
            if remaining > 0:
                return remaining
        return None

    def _arm_phase_timer(self, status):
        """Arm ONE one-shot timer for the soonest phase-threshold crossing.

        F11: the promotion is read-time (``_effective_state``), so without a
        nudge the dot would only flip on the NEXT file event — which during a
        permission prompt never comes (the wire is silent; that silence IS the
        signal). The timer re-publishes at the crossing so consumers re-query
        and see 'waiting'. Snapshots without phase fields arm nothing — the
        no-phase path is byte-identical to the pre-F11 watcher.
        """
        now = self._now()
        soonest = None
        for s in status.values():
            r = self._phase_remaining(s, now)
            if r is not None and (soonest is None or r < soonest):
                soonest = r
        self._phase_gen += 1
        if soonest is None:
            return
        gen = self._phase_gen

        def _on_threshold():
            if gen != self._phase_gen:
                return False  # superseded by a later publish — die silently
            self._reload()    # re-publishes (and re-arms for any next crossing)
            return False

        # Small cushion so the re-read lands ON/after the threshold, never a
        # hair before it (which would re-arm for a ~0s remainder).
        self._schedule(soonest + 0.05, _on_threshold)

    def _effective_state(self, snapshot):
        """The state consumers should see, with the F11 phase-aging promotion.

        A 'working' snapshot whose ``phase == 'pre_tool_use'`` has aged past
        ``PHASE_WAITING_THRESHOLD`` reads as 'waiting': grok fires pre_tool_use
        BEFORE its permission prompt and then goes wire-silent, so prolonged
        pre-tool silence means the prompt is (almost certainly) on screen.
        ACCEPTED LIMITATION (F11): a long-running APPROVED tool also crosses
        the threshold and shows a transient false 'waiting', self-correcting
        the moment post_tool_use lands — for PM's purpose a false "needs you"
        beats a silently stalled session. Snapshots without phase fields are
        returned unchanged (claude/opencode behavior is untouched).
        """
        if (snapshot.state == 'working'
                and snapshot.phase == 'pre_tool_use'
                and snapshot.phase_ts
                and self._now() - snapshot.phase_ts >= PHASE_WAITING_THRESHOLD):
            return 'waiting'
        return snapshot.state

    def publish_external(self, key, snapshot):
        """Publish a status snapshot under an arbitrary key (e.g. remote project.path).

        Does not touch the local-file map; survives ``_reload``. Emits
        ``status-changed`` so the sidebar can refresh dots. Arms F11 phase
        aging so remote ``pre_tool_use`` silence promotes working→waiting.
        """
        if not key or snapshot is None:
            return
        self._external_status[key] = snapshot
        # Merge external + local for phase-timer arming (local-only arm misses
        # remote snaps and leaves Grok stuck on yellow working without waiting).
        merged = dict(self._status)
        merged.update(self._external_status)
        self._arm_phase_timer(merged)
        self.emit('status-changed')

    def clear_external_prefix(self, prefix: str):
        """Drop external keys starting with *prefix* (unused helper for host remove)."""
        if not prefix:
            return
        drop = [k for k in self._external_status if k.startswith(prefix)]
        for k in drop:
            del self._external_status[k]

    def clear_external_key(self, key: str):
        """Drop one external status key (e.g. remote project with no status file)."""
        if key and key in self._external_status:
            del self._external_status[key]

    def get_project_status(self, project):
        snapshot = self._status.get(project.path)
        if snapshot is None:
            snapshot = self._external_status.get(project.path)
        if snapshot is None:
            # Remote projects: also try spawn_cwd / remote_cwd if something
            # published under the remote absolute path.
            alt = getattr(project, 'remote_cwd', None) or getattr(
                project, 'spawn_cwd', None)
            if alt and alt != project.path:
                snapshot = self._status.get(alt) or self._external_status.get(alt)
        if snapshot is None:
            return 'idle'
        # If the same session later moved its cwd into a subdirectory (e.g.
        # `cd code && ...`), the hook writes a separate status file for that
        # path.  The project-root snapshot becomes stale while the newer
        # subdirectory snapshot holds the real state.  Pick the most recent
        # snapshot among the project root and any same-session subdirectories.
        # The session-ID guard keeps independent worktree sessions from leaking
        # in — they have different session IDs.
        # Local path prefix rollup only (remote keys are opaque ssh: refs).
        best = snapshot
        if not str(project.path).startswith('ssh:'):
            prefix = project.path + os.sep
            for path, s in self._status.items():
                if (path.startswith(prefix)
                        and s.session == snapshot.session
                        and s.ts > best.ts):
                    best = s
        # F11: read through the phase-aging promotion (no-phase snapshots pass
        # through unchanged).
        return self._effective_state(best)


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
