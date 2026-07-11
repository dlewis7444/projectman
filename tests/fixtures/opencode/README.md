# opencode session-list fixtures

These pin the OpenCode harness session parser. Shapes cover both modern
JSON CLI output and older file-storage layouts; the parser is layered and
defensive because CLI builds differ (JSON flag, storage backend).

| File | What it models | Source |
|---|---|---|
| `session_list.json` | `opencode session list --format json` output | CLI shape (id/title/updated-ms/created/projectId/**directory**), with invented per-project rows |
| `session_list.table.txt` | `opencode session list` (default table) | CLI header + box-drawing separator + rows |
| `storage/project/<id>.json` | worktree→projectId mapping | older file layout (`{id, worktree}`) — invented |
| `storage/session/<projectId>/<sessionId>.json` | per-session record | older file layout (`{id, title, time:{created,updated}}`) — invented |

## Parser layering (defensive — see `harnesses.OpencodeAdapter.list_sessions`)

1. **JSON CLI (primary):** `opencode session list --format json -n N`, filtered
   by `directory == project.path`. The only route that can filter per-project,
   since the table format has no directory column.
2. **Storage scan (fallback):** when the CLI is unavailable/fails, walk
   `~/.local/share/opencode/storage/` — map the project path to a `projectId`
   via `storage/project/*.json`'s `worktree`, then read that project's session
   files. Tolerates both `updated`/`time.updated` shapes.
3. The **table parser** is kept (`_parse_session_list_table`) for completeness
   but cannot filter by project (no directory column), so it is not used for the
   per-project expander — it returns id/title/updated only.

All layers are individually fixture-tested in `tests/test_opencode_sessions.py`.
