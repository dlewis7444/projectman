import os
import json
import hashlib
import tempfile
import fcntl
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


LEDGER_PATH = os.path.expanduser('~/.ProjectMan/paa-ledger.json')


@dataclass
class LedgerItem:
    id: str
    type: str
    project: str
    project_path: str
    summary: str
    evidence: str
    severity: str
    status: str = 'pending'
    created: str = ''
    updated: str = ''


def make_item_id(item_type, project, evidence):
    """Deterministic ID from type + project + evidence for deduplication."""
    key = f"{item_type}:{project}:{evidence}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path=LEDGER_PATH):
        self._path = path
        self._items: dict = {}

    def load(self):
        self._items.clear()
        try:
            with open(self._path, 'r') as f:
                data = json.load(f)
            for raw in data.get('items', []):
                known = {k: v for k, v in raw.items()
                         if k in LedgerItem.__dataclass_fields__}
                item = LedgerItem(**known)
                # Legacy: 'approved' was the original Acknowledge status.
                if item.status == 'approved':
                    item.status = 'acknowledged'
                self._items[item.id] = item
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def _lock_path(self):
        return self._path + '.lock'

    def _exclusive(self):
        directory = os.path.dirname(os.path.abspath(self._path)) or '.'
        os.makedirs(directory, exist_ok=True)
        fh = open(self._lock_path(), 'a+')
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _release(self, fh):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def _write_unlocked(self):
        dir_path = os.path.dirname(os.path.abspath(self._path)) or '.'
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(
                    {'items': [asdict(i) for i in self._items.values()]},
                    f, indent=2,
                )
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, op):
        """Exclusive lock, reload, apply one change, save."""
        fh = self._exclusive()
        try:
            self.load()
            result = op()
            self._write_unlocked()
            return result
        finally:
            self._release(fh)

    def save(self):
        """Reload under the lock, keep disk rows, add memory-only rows.

        Mutations already save under the lock. A later ``save()`` must not
        write this object's stale status back over a row another process
        has dismissed or acknowledged. Rows that exist only in memory are
        still added, and rows that exist only on disk are kept.
        """
        fh = self._exclusive()
        try:
            current = dict(self._items)
            self.load()
            for item_id, item in current.items():
                if item_id not in self._items:
                    self._items[item_id] = item
            self._write_unlocked()
        finally:
            self._release(fh)

    def add_if_new(self, item):
        """Add unless a matching item is pending, acknowledged, or dismissed."""
        def op():
            existing = self._items.get(item.id)
            if existing and existing.status in ('pending', 'acknowledged', 'dismissed'):
                return False
            self._items[item.id] = item
            return True
        return self._mutate(op)

    def update_status(self, item_id, status):
        def op():
            if item_id not in self._items:
                return False
            self._items[item_id].status = status
            self._items[item_id].updated = now_iso()
            return True
        return self._mutate(op)

    def sweep(self, active_ids):
        """Auto-resolve pending and acknowledged items no longer detected.

        Acknowledged ('parked') items resolve symmetrically with pending
        ones — if the underlying issue disappears, the parked entry should
        clear instead of lingering forever.

        Reloads under the lock first. Callers that scan hold the scan lock so
        two sweeps cannot resolve each other's fresh rows.
        """
        def op():
            for item in self._items.values():
                if (item.status in ('pending', 'acknowledged')
                        and item.id not in active_ids):
                    item.status = 'resolved'
                    item.updated = now_iso()
        self._mutate(op)

    def pending_items(self):
        return sorted(
            [i for i in self._items.values() if i.status == 'pending'],
            key=lambda i: i.created, reverse=True,
        )

    def acknowledged_items(self):
        return sorted(
            [i for i in self._items.values() if i.status == 'acknowledged'],
            key=lambda i: i.updated or i.created, reverse=True,
        )

    @property
    def pending_count(self):
        return sum(1 for i in self._items.values() if i.status == 'pending')


class LedgerFileRelay:
    """Apply a ``paa-ledger.json`` change to the open ledger and its listeners.

    The GTK file monitor calls :meth:`on_file_event` with the same kinds
    ``StatusWatcher`` uses (``CHANGED`` / ``CREATED`` / ``DELETED``). Tests
    call that method directly — no window click.
    """

    _KINDS = frozenset({
        'CHANGED', 'CREATED', 'DELETED',
        'changed', 'created', 'deleted',
    })

    def __init__(self, ledger, on_count, on_refresh=None):
        self._ledger = ledger
        self._on_count = on_count
        self._on_refresh = on_refresh

    def reload(self):
        self._ledger.load()
        count = self._ledger.pending_count
        if self._on_count is not None:
            self._on_count(count)
        if self._on_refresh is not None:
            self._on_refresh()
        return count

    def on_file_event(self, event_kind):
        if event_kind not in self._KINDS:
            return None
        return self.reload()
