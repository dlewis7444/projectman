"""Virtual project groups — pure data layer (no GTK, no SSH).

Groups are organizational only: projects stay flat on disk. Persistence is
``~/.ProjectMan/project_groups.json`` (local atomic write).

Schema::

    {
      "version": 1,
      "groups": [
        {"id": "uuid", "name": "ABC", "parent_id": null, "expanded": true}
      ],
      "membership": {
        "local:/abs/path": "uuid",
        "ssh:host:name": "uuid"
      }
    }

Rules:
- Max group depth = ``MAX_GROUP_DEPTH`` (root-level group is depth 1).
- ``parent_id`` null = host-root group.
- Membership: project_ref → group_id; missing key ⇒ ungrouped.
- Delete group: remove the group; reparent its *child groups* to the deleted
  group's parent; clear membership entries that pointed at the deleted group
  (those projects become ungrouped). Child groups are kept; only membership
  for the deleted id is cleared.

``build_tree_order`` sorts projects at each level alphabetically by a stable
basename key: the segment after the last ``:`` in the ref, then the final
path component of that segment (so ``local:/a/b/foo`` and ``ssh:h:foo`` both
sort as ``foo``), with the full ref as a tie-break.

Load/save: ``load_forest`` returns a ``LoadResult`` with status
(``ok`` / ``missing`` / ``invalid`` / ``error``). UI must not save over a
file whose last load was ``error`` or ``invalid`` without user confirmation.
``load_forest_or_empty`` is a convenience for callers that only need the forest.

Remote hosts may also use ``pending`` (groups never fetched yet). Only
``ok`` and ``missing`` (after a successful fetch that found no file) are
writable. See :func:`groups_status_allows_save`.

Remote UI orchestration helpers (pure, no GTK/SSH):
``should_apply_remote_groups_fetch`` (stale-apply / dirty / in-flight gate),
``groups_push_schedule`` / ``groups_push_complete`` (async push coalesce).
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable


MAX_GROUP_DEPTH = 5
DEFAULT_GROUPS_PATH = os.path.expanduser('~/.ProjectMan/project_groups.json')
SCHEMA_VERSION = 1


@dataclass
class GroupNode:
    id: str
    name: str
    parent_id: str | None
    expanded: bool = True


@dataclass
class GroupForest:
    groups: dict[str, GroupNode] = field(default_factory=dict)
    membership: dict[str, str] = field(default_factory=dict)


@dataclass
class LoadResult:
    """Result of loading a groups file from disk.

    status:
      - ``ok``: file read and parsed successfully
      - ``missing``: file does not exist (empty forest)
      - ``invalid``: JSON decode error or wrong top-level type (empty forest)
      - ``error``: OSError (permission, etc.); empty forest; ``error`` message set

    UI must not save if last load status was ``error`` or ``invalid`` without
    user confirmation (would wipe a corrupt or unreadable file).
    """
    forest: GroupForest
    status: str  # 'ok' | 'missing' | 'invalid' | 'error'
    error: str | None = None


def empty_forest() -> GroupForest:
    return GroupForest()


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _project_sort_key(ref: str) -> tuple[str, str]:
    """Stable sort key: (basename after last ':', full ref for tie-break)."""
    if not isinstance(ref, str):
        return ('', '')
    tail = ref.rsplit(':', 1)[-1] if ':' in ref else ref
    base = os.path.basename(tail.rstrip('/')) or tail
    return (base.casefold(), ref)


def depth_of(forest: GroupForest, group_id: str) -> int:
    """Depth of a group: root = 1. Returns 0 if missing or cyclic."""
    if group_id not in forest.groups:
        return 0
    depth = 0
    seen: set[str] = set()
    cur: str | None = group_id
    while cur is not None:
        if cur in seen or cur not in forest.groups:
            return 0
        seen.add(cur)
        depth += 1
        cur = forest.groups[cur].parent_id
    return depth


def _subtree_height(forest: GroupForest, group_id: str) -> int:
    """Height of subtree rooted at group_id (node alone = 1)."""
    if group_id not in forest.groups:
        return 0
    kids = [g.id for g in forest.groups.values() if g.parent_id == group_id]
    if not kids:
        return 1
    return 1 + max(_subtree_height(forest, kid) for kid in kids)


def _ancestors(forest: GroupForest, group_id: str | None) -> set[str]:
    out: set[str] = set()
    cur = group_id
    while cur is not None and cur in forest.groups and cur not in out:
        out.add(cur)
        cur = forest.groups[cur].parent_id
    return out


def can_reparent(
    forest: GroupForest,
    group_id: str,
    new_parent_id: str | None,
) -> bool:
    """True if moving group_id under new_parent_id is legal (exists, no cycle, depth)."""
    if group_id not in forest.groups:
        return False
    if new_parent_id is not None:
        if new_parent_id not in forest.groups:
            return False
        if new_parent_id == group_id:
            return False
        # Cycle: new parent must not be group_id or a descendant of it.
        if group_id in _ancestors(forest, new_parent_id):
            return False
        parent_depth = depth_of(forest, new_parent_id)
        if parent_depth <= 0:
            return False
        new_depth = parent_depth + 1
    else:
        new_depth = 1
    # Max depth in moved subtree under the new parent.
    max_depth = new_depth + _subtree_height(forest, group_id) - 1
    return max_depth <= MAX_GROUP_DEPTH


def child_groups(
    forest: GroupForest,
    parent_id: str | None,
) -> list[GroupNode]:
    """Direct child groups of parent_id (None = host-root), sorted by name."""
    kids = [
        g for g in forest.groups.values()
        if g.parent_id == parent_id
    ]
    kids.sort(key=lambda g: (g.name.casefold(), g.id))
    return kids


def projects_in(forest: GroupForest, group_id: str) -> list[str]:
    """Project refs currently members of group_id (unsorted)."""
    return [ref for ref, gid in forest.membership.items() if gid == group_id]


def ungrouped_refs(forest: GroupForest, all_refs: Iterable[str]) -> list[str]:
    """Refs with no membership entry or membership to a missing group."""
    out: list[str] = []
    for ref in all_refs:
        gid = forest.membership.get(ref)
        if gid is None or gid not in forest.groups:
            out.append(ref)
    return out


def group_path_names(forest: GroupForest, group_id: str) -> list[str]:
    """Breadcrumb names root → leaf for group_id. Empty if missing."""
    if group_id not in forest.groups:
        return []
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = group_id
    while cur is not None and cur in forest.groups and cur not in seen:
        seen.add(cur)
        chain.append(forest.groups[cur].name)
        cur = forest.groups[cur].parent_id
    chain.reverse()
    return chain


def _sanitize_forest(forest: GroupForest) -> GroupForest:
    """Ensure forest has no dangling parents, cycles, or over-depth groups."""
    # Drop invalid parent pointers.
    for g in forest.groups.values():
        if g.parent_id is not None and g.parent_id not in forest.groups:
            g.parent_id = None

    # Break cycles: walk each chain; if a back-edge is found, detach that node.
    for start_id in list(forest.groups):
        seen: list[str] = []
        cur: str | None = start_id
        while cur is not None and cur in forest.groups:
            if cur in seen:
                forest.groups[cur].parent_id = None
                break
            seen.append(cur)
            cur = forest.groups[cur].parent_id

    # Clamp depth: walk each node; if too deep, reparent toward root until valid.
    for gid in list(forest.groups):
        g = forest.groups.get(gid)
        if g is None:
            continue
        # Limit iterations to avoid pathological loops.
        for _ in range(len(forest.groups) + 1):
            d = depth_of(forest, gid)
            if d == 0:
                # Still cyclic / broken — detach.
                g.parent_id = None
                break
            if d <= MAX_GROUP_DEPTH:
                break
            # Climb: attach to grandparent (or root).
            parent = forest.groups.get(g.parent_id) if g.parent_id else None
            g.parent_id = parent.parent_id if parent else None

    # Drop membership pointing at unknown groups; keep only str keys/values.
    clean_mem: dict[str, str] = {}
    for ref, gid in forest.membership.items():
        if isinstance(ref, str) and isinstance(gid, str) and gid in forest.groups:
            clean_mem[ref] = gid
    forest.membership = clean_mem
    return forest


def parse_forest(data: Any) -> GroupForest:
    """Parse a JSON-shaped dict into a valid GroupForest. Always succeeds."""
    forest = empty_forest()
    if not isinstance(data, dict):
        return forest

    raw_groups = data.get('groups')
    if isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            gid = item.get('id')
            name = item.get('name')
            if not isinstance(gid, str) or not gid:
                continue
            # First-wins on duplicate group ids.
            if gid in forest.groups:
                continue
            if not isinstance(name, str):
                continue
            name = name.strip()
            # Skip empty / whitespace-only names (membership to them becomes orphan).
            if not name:
                continue
            parent_id = item.get('parent_id', None)
            if parent_id is not None and not isinstance(parent_id, str):
                parent_id = None
            if parent_id == '':
                parent_id = None
            forest.groups[gid] = GroupNode(
                id=gid,
                name=name,
                parent_id=parent_id,
                expanded=_as_bool(item.get('expanded', True), True),
            )

    raw_mem = data.get('membership')
    if isinstance(raw_mem, dict):
        for ref, gid in raw_mem.items():
            if isinstance(ref, str) and isinstance(gid, str):
                forest.membership[ref] = gid

    return _sanitize_forest(forest)


def forest_to_dict(forest: GroupForest) -> dict:
    """Serialize a forest to the on-disk JSON schema."""
    groups = []
    # Stable order: by name then id for readable diffs.
    for g in sorted(forest.groups.values(), key=lambda n: (n.name.casefold(), n.id)):
        groups.append({
            'id': g.id,
            'name': g.name,
            'parent_id': g.parent_id,
            'expanded': bool(g.expanded),
        })
    # Membership keys sorted for stable output.
    membership = {
        k: forest.membership[k]
        for k in sorted(forest.membership.keys())
    }
    return {
        'version': SCHEMA_VERSION,
        'groups': groups,
        'membership': membership,
    }


def load_forest(path: str | None = None) -> LoadResult:
    """Load forest from path. Returns LoadResult (never raises for I/O/parse).

    Statuses:
      - ``ok``: file read and parsed
      - ``missing``: file not found → empty forest
      - ``invalid``: JSON decode / wrong top-level type → empty forest
      - ``error``: OSError (permission, etc.) → empty forest + error message

    UI must not save if status was ``error`` or ``invalid`` without confirmation.
    """
    if path is None:
        path = DEFAULT_GROUPS_PATH
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        return LoadResult(forest=empty_forest(), status='missing')
    except json.JSONDecodeError as e:
        return LoadResult(
            forest=empty_forest(),
            status='invalid',
            error=str(e),
        )
    except (TypeError, ValueError) as e:
        return LoadResult(
            forest=empty_forest(),
            status='invalid',
            error=str(e),
        )
    except OSError as e:
        return LoadResult(
            forest=empty_forest(),
            status='error',
            error=str(e),
        )
    if not isinstance(data, dict):
        return LoadResult(
            forest=empty_forest(),
            status='invalid',
            error=f'top-level JSON must be object, got {type(data).__name__}',
        )
    return LoadResult(forest=parse_forest(data), status='ok')


def load_forest_or_empty(path: str | None = None) -> GroupForest:
    """Convenience: forest only (for callers that don't care about status)."""
    return load_forest(path).forest


def save_forest(forest: GroupForest, path: str | None = None) -> None:
    """Atomically write forest to path (tempfile + os.replace in same dir).

    Always writes. Callers must avoid saving when the last load was
    ``error``/``invalid`` unless the user confirmed overwrite.
    """
    if path is None:
        path = DEFAULT_GROUPS_PATH
    dir_path = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(forest_to_dict(forest), f, indent=2)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def add_group(
    forest: GroupForest,
    name: str,
    parent_id: str | None = None,
    group_id: str | None = None,
) -> GroupNode:
    """Add a group. Generates uuid4 hex if group_id is None.

    Raises ValueError if parent is missing or depth would exceed MAX_GROUP_DEPTH.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError('group name must be a non-empty string')
    name = name.strip()
    if parent_id is not None:
        if parent_id not in forest.groups:
            raise ValueError(f'parent group not found: {parent_id!r}')
        parent_depth = depth_of(forest, parent_id)
        if parent_depth <= 0:
            raise ValueError(f'parent group has invalid depth: {parent_id!r}')
        if parent_depth + 1 > MAX_GROUP_DEPTH:
            raise ValueError(
                f'adding group would exceed max depth {MAX_GROUP_DEPTH}'
            )
    gid = group_id if group_id else uuid.uuid4().hex
    if gid in forest.groups:
        raise ValueError(f'group id already exists: {gid!r}')
    node = GroupNode(id=gid, name=name, parent_id=parent_id, expanded=True)
    forest.groups[gid] = node
    return node


def rename_group(forest: GroupForest, group_id: str, new_name: str) -> bool:
    """Rename a group. Returns False if missing or new_name empty."""
    if group_id not in forest.groups:
        return False
    if not isinstance(new_name, str) or not new_name.strip():
        return False
    forest.groups[group_id].name = new_name.strip()
    return True


def delete_group(forest: GroupForest, group_id: str) -> bool:
    """Delete group; reparent child groups; clear membership for this group.

    Child groups are reparented to the deleted group's parent.
    Membership entries that pointed at the deleted group are removed
    (projects become ungrouped). Returns False if group_id is missing.
    """
    if group_id not in forest.groups:
        return False
    deleted = forest.groups[group_id]
    new_parent = deleted.parent_id
    # Reparent children first.
    for g in forest.groups.values():
        if g.parent_id == group_id:
            g.parent_id = new_parent
    del forest.groups[group_id]
    # Clear membership that pointed at deleted group.
    forest.membership = {
        ref: gid for ref, gid in forest.membership.items() if gid != group_id
    }
    return True


def set_group_parent(
    forest: GroupForest,
    group_id: str,
    new_parent_id: str | None,
) -> bool:
    """Move group under new_parent_id. False on missing/cycle/depth violation."""
    if not can_reparent(forest, group_id, new_parent_id):
        return False
    forest.groups[group_id].parent_id = new_parent_id
    return True


def set_group_expanded(
    forest: GroupForest,
    group_id: str,
    expanded: bool,
) -> bool:
    if group_id not in forest.groups:
        return False
    forest.groups[group_id].expanded = bool(expanded)
    return True


def set_membership(
    forest: GroupForest,
    project_ref: str,
    group_id: str,
) -> bool:
    """Assign project_ref to group_id. False if group missing or ref invalid."""
    if not isinstance(project_ref, str) or not project_ref:
        return False
    if group_id not in forest.groups:
        return False
    forest.membership[project_ref] = group_id
    return True


def clear_membership(forest: GroupForest, project_ref: str) -> bool:
    """Remove membership for project_ref. False if it was not a member."""
    if project_ref not in forest.membership:
        return False
    del forest.membership[project_ref]
    return True


def on_project_renamed(
    forest: GroupForest,
    old_ref: str,
    new_ref: str | None,
) -> None:
    """Rewrite membership key when a project_ref changes.

    If ``new_ref`` is empty or None, no-op (leave old membership intact).
    """
    if not new_ref:
        return
    if old_ref == new_ref:
        return
    if old_ref not in forest.membership:
        return
    gid = forest.membership.pop(old_ref)
    # Prefer keeping membership under the new key; drop if new_ref already set.
    if new_ref not in forest.membership:
        forest.membership[new_ref] = gid


def on_project_removed(forest: GroupForest, project_ref: str) -> None:
    """Drop membership when a project disappears."""
    forest.membership.pop(project_ref, None)


def prune_unknown_projects(
    forest: GroupForest,
    known_refs: set[str],
) -> int:
    """Remove membership keys not in known_refs. Returns count removed."""
    dead = [ref for ref in forest.membership if ref not in known_refs]
    for ref in dead:
        del forest.membership[ref]
    return len(dead)


def groups_status_allows_save(status: str | None, *, is_remote: bool) -> bool:
    """True when the UI may persist a groups forest for this host.

    Writable statuses: ``ok`` (parsed file) and ``missing`` (confirmed no file
    yet — first save may create it).

    Non-writable:
      - ``error`` / ``invalid``: last load failed; must not wipe remote/local
      - ``pending``: remote groups never fetched this session
      - ``None``: no status recorded — remote fail-closed; localhost allows
        save only when status is missing after init is unexpected but a
        forest exists (fail-open for local so expand-toggle still works)
    """
    if status in ('ok', 'missing'):
        return True
    if status is None and not is_remote:
        return True
    return False


def should_prune_membership(projects_trusted: bool) -> bool:
    """Whether health-refresh may prune membership against the project list.

    Only prune when this tick's project list is trusted (list succeeded).
    Never prune against a fallback empty/cached list after list failure —
    that would silently drop valid remote membership.
    """
    return bool(projects_trusted)


def should_apply_remote_groups_fetch(
    *,
    dirty: bool,
    push_inflight: bool = False,
    local_write_gen: int = 0,
    fetch_write_gen: int = 0,
) -> bool:
    """True if a health-fetch forest may replace the in-memory groups for a host.

    Skip apply when:

    - *dirty*: a push failed (or local edits are known ahead of remote)
    - *push_inflight*: a push is in flight (remote still has pre-push content)
    - *local_write_gen* > *fetch_write_gen*: a mutation happened after this
      fetch started — applying would roll back a successful later push or
      clobber unpushed edits (stale-apply race)
    """
    if dirty or push_inflight:
        return False
    if int(local_write_gen or 0) > int(fetch_write_gen or 0):
        return False
    return True


def groups_push_schedule(
    inflight: set,
    pending: set,
    host_id: str,
) -> str:
    """Record a remote push request. Returns ``'start'`` or ``'queue'``.

    If a push is already in flight for *host_id*, mark *pending* so the
    latest forest is pushed when the current worker finishes (coalesce).
    """
    if host_id in inflight:
        pending.add(host_id)
        return 'queue'
    inflight.add(host_id)
    pending.discard(host_id)
    return 'start'


def groups_push_complete(
    inflight: set,
    pending: set,
    dirty: set,
    *,
    host_id: str,
    ok: bool,
    started_gen: int,
    current_gen: int,
) -> str:
    """Finish a remote push. Returns ``'idle'`` or ``'retry'``.

    On failure, *host_id* stays in *dirty*. On success, dirty is cleared only
    when no newer mutation (matching gen) and nothing is pending. If pending
    (or gen advanced), returns ``'retry'`` so the caller starts another push
    of the latest forest.
    """
    inflight.discard(host_id)
    gen_advanced = int(current_gen or 0) != int(started_gen or 0)
    need_retry = host_id in pending or gen_advanced
    pending.discard(host_id)
    if not ok or need_retry:
        dirty.add(host_id)
    else:
        dirty.discard(host_id)
    if need_retry:
        # Caller will re-enter groups_push_schedule / start worker.
        return 'retry'
    return 'idle'


def build_tree_order(
    forest: GroupForest,
    all_project_refs: list[str],
) -> list[tuple]:
    """Flat depth-first display list for one host section.

    Each item is either::

        ("group", group_id, depth)
        ("project", project_ref, depth)

    where ``depth`` is the nest level for indentation (0 = host root).

    File-manager style at **every** level (including under a group):

    1. child groups (alpha by name, id tiebreak)
    2. then projects in that group (sorted by :func:`_project_sort_key`)

    Empty groups are included. Only refs present in ``all_project_refs`` are
    emitted; duplicate refs are de-duplicated (first-seen, then sorted among
    unique). Host-root ungrouped projects appear after all root groups.
    """
    # Dedupe preserving first-seen order, then we re-sort by key among uniques.
    seen: set[str] = set()
    known: list[str] = []
    for ref in all_project_refs:
        if ref not in seen:
            seen.add(ref)
            known.append(ref)

    by_group: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for ref in known:
        gid = forest.membership.get(ref)
        if gid is not None and gid in forest.groups:
            by_group.setdefault(gid, []).append(ref)
        else:
            ungrouped.append(ref)

    for refs in by_group.values():
        refs.sort(key=_project_sort_key)
    ungrouped.sort(key=_project_sort_key)

    out: list[tuple] = []

    def walk(parent_id: str | None, depth: int) -> None:
        # 1. Child groups first (file-manager style).
        for g in child_groups(forest, parent_id):
            out.append(('group', g.id, depth))
            walk(g.id, depth + 1)
        # 2. Then projects in this parent group (host root: ungrouped handled below).
        if parent_id is not None:
            for ref in by_group.get(parent_id, []):
                out.append(('project', ref, depth))

    walk(None, 0)
    # Host-root ungrouped projects at depth 0 (after root groups).
    for ref in ungrouped:
        out.append(('project', ref, 0))
    return out
