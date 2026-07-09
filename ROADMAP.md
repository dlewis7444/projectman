# ProjectMan Roadmap

This is the living roadmap for ProjectMan — a GTK4/Adwaita desktop cockpit for
AI coding agents. Items are **goals, not commitments**; order reflects current
priority. The roadmap is appended to as items are defined and pruned as they
ship.

(Private dev happens on GitLab; public releases ship to GitHub through the
publish gate. See `CLAUDE.md` for the release process.)

## Near-term

### 1. Support remote (SSH) execution of Claude

Today every project's Claude Code session spawns in a **local** VTE terminal
inside ProjectMan. The agent runs on the same machine as the GUI. We want to
run Claude on a **remote host over SSH** instead — a project's agent session
executes on a remote box (a lab server, a build VM, a beefy GPU host) while
ProjectMan on the laptop drives it.

This is a new transport across the adapter/terminal seam (`agents.py`,
`terminal.py`): an SSH connection in place of (or alongside) the local
PTY, remote session lifecycle (spawn / detach / reattach), and status
bridging over SSH so the sidebar dot still reflects what the remote agent is
doing. The model axis (`build_spawn_env`) already injects per-provider env;
remote execution layers a *host* axis underneath it.

### 2. Update PAA to use the correct model(s)

PAA (the Proactive Agent Assistant monitor, `paa_monitor.py`) runs its
on-demand AI scan — the "Haiku Check" — through a **hardcoded Haiku model
path** (`paa_haiku.run_ai_checks`, gated by the `paa_allow_haiku` toggle).
That path is frozen to one model and ignores the rest of the user's
configuration.

Update PAA to use the **correct model(s)** — route its AI scan through the
model axis (the per-provider tier system / `build_spawn_env`) instead of the
hardcoded Haiku path, so PAA respects the user's configured provider and tier
assignments (e.g. Haiku tier → the user's chosen Haiku-tier model on their
active provider). The `paa_allow_haiku` "Enable AI Scans" toggle stays as the
on/off gate; what changes is *which model* the scan uses.