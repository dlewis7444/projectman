import os
import json
import hashlib
import tempfile
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
                self._items[item.id] = item
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def save(self):
        dir_path = os.path.dirname(os.path.abspath(self._path))
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

    def add_if_new(self, item):
        """Add item unless a matching one is already active or dismissed."""
        existing = self._items.get(item.id)
        if existing and existing.status in ('pending', 'approved', 'dismissed'):
            return False
        self._items[item.id] = item
        return True

    def update_status(self, item_id, status):
        if item_id in self._items:
            self._items[item_id].status = status
            self._items[item_id].updated = now_iso()

    def sweep(self, active_ids):
        """Auto-resolve pending items no longer detected in current scan."""
        for item in self._items.values():
            if item.status == 'pending' and item.id not in active_ids:
                item.status = 'resolved'
                item.updated = now_iso()

    def pending_items(self):
        return sorted(
            [i for i in self._items.values() if i.status == 'pending'],
            key=lambda i: i.created, reverse=True,
        )

    @property
    def pending_count(self):
        return sum(1 for i in self._items.values() if i.status == 'pending')
