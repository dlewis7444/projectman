# Virtual project groups

**Status:** experimental on branch `feat/project-groups`. Not a product
commitment; schema and UX may change before merge to `testing`.

## Mental model

Groups are **virtual folders** for the sidebar only.

| Layer | Behavior |
|-------|----------|
| Disk | Projects stay **flat** under each host's `projects/` (and `.archive/`). No nested project directories. |
| UI | Host → groups (nested) → projects → sessions. Expand a group to see child groups and member projects; expand a project for session history (same as before). |
| Persistence | One JSON file per host: group tree + `project_ref → group_id` membership. |

Ungrouped projects appear at the host root (after root-level groups), sorted
like other project lists. Deleting a group reparents its **child groups** to
the deleted group's parent and **ungroups** projects that were direct members
of the deleted group (membership keys for that group id are cleared).

## Schema (`project_groups.json`)

**Path:** `~/.ProjectMan/project_groups.json` on localhost; the same path on
each remote host's home (via SSH fetch/push).

```json
{
  "version": 1,
  "groups": [
    {
      "id": "uuid-hex",
      "name": "ABC",
      "parent_id": null,
      "expanded": true
    }
  ],
  "membership": {
    "local:/abs/path/to/project": "uuid-hex",
    "ssh:host_id:project_name": "uuid-hex"
  }
}
```

| Field | Meaning |
|-------|---------|
| `version` | Schema version (`1`). |
| `groups[].id` | Stable id (uuid4 hex). |
| `groups[].name` | Display name (non-empty). |
| `groups[].parent_id` | Parent group id, or `null` for host-root. |
| `groups[].expanded` | Last expand/collapse state in the sidebar. |
| `membership` | Map of `project_ref` → `group_id`. Missing key ⇒ ungrouped. |

**Rules (enforced in `project_groups.py`):**

- Max group depth = 5 (root-level group is depth 1).
- No cycles; invalid parents / over-depth trees are sanitized on load.
- Membership pointing at unknown group ids is dropped on sanitize.
- Local saves are atomic (`tempfile` + `os.replace`). Remote push is
  last-write-wins (no locking).

## Load status / remote safety

`load_forest` / remote fetch return a status the UI must respect before write:

| Status | Meaning | Save/push? |
|--------|---------|------------|
| `ok` | File read and parsed | Yes |
| `missing` | No file (or soft empty after successful fetch) | Yes (first create) |
| `invalid` | Bad JSON / wrong top-level type | **No** (would wipe) |
| `error` | I/O or SSH failure | **No** (would wipe) |
| `pending` | Remote never fetched this session | **No** |

Helpers: `groups_status_allows_save`, `should_prune_membership`,
`should_apply_remote_groups_fetch`, `groups_push_schedule` /
`groups_push_complete`. Health refresh prunes membership only when the project
list for that tick is trusted; never prune against an empty list after list
failure. Remote pushes run off the GTK main thread and coalesce. Each mutation
bumps a per-host write generation so a health-fetch that started earlier cannot
overwrite a forest that was mutated (or successfully pushed) in the meantime.
Failed or in-flight pushes mark the host dirty so the next fetch does not
clobber unsaved local edits.

## UI map

| Action | Where |
|--------|--------|
| New root group | Host-line **+** → **New Group** |
| New project (ungrouped) | Host-line **+** → **New Project** |
| New subgroup | Group-line **+** → **New Subgroup** (r-click menu too; disabled at max depth) |
| New project in group | Group-line **+** → **New Project** (create on disk, then membership) |
| Rename / delete group | Group right-click context menu |
| Move project | Project context menu → **Move to group** (or Ungrouped) |
| Visibility | Creating a group/project switches that host section filter to **all** |
| Expand/collapse group | Group row disclosure; persisted via `expanded` |
| Expand project | Unchanged — shows sessions |

Layout at every level is file-manager style: child groups first (alpha), then
projects in that group (alpha by basename of the ref).

Code map:

- `project_groups.py` — pure forest + local load/save
- `remote_groups.py` — SSH fetch/push
- `sidebar.py` — `GroupRow`, menus, tree fill
- `window.py` — mutations, persist gate, remote refresh apply

## Out of scope (this experiment)

- Drag-and-drop reparent / reorder
- Filesystem nesting of project directories
- Archiving groups (or group-aware archive UI)
- Multi-user locking / merge of concurrent remote edits
- Cross-host groups or shared global forest
