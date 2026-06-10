# opencode session-list fixtures

These pin the `OpencodeAdapter` session parser. **Recorded shapes — VERIFY ON
THE VM GATE against the real `opencode` CLI before trusting in production.** The
laptop dev build and the VM gate build of opencode differ (the laptop has a
SQLite store + `--format json`; the VM's 1.16.2 may use the older file storage
and may or may not support `--format json`), which is exactly why the parser is
layered and defensive and why these fixtures are marked to-be-verified.

| File | What it models | Source |
|---|---|---|
| `session_list.json` | `opencode session list --format json` output | real laptop shape (id/title/updated-ms/created/projectId/**directory**), with invented per-project rows |
| `session_list.table.txt` | `opencode session list` (default table) | real laptop header + box-drawing separator + rows |
| `storage/project/<id>.json` | worktree→projectId mapping | the spec's described older file layout (`{id, worktree}`) — **invented, VM-verify** |
| `storage/session/<projectId>/<sessionId>.json` | per-session record | older file layout (`{id, title, time:{created,updated}}`) — **invented, VM-verify** |

## Parser layering (defensive — see `agents.OpencodeAdapter.list_sessions`)

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
