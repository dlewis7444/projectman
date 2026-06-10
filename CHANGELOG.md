# Changelog

All notable changes to ProjectMan will be documented in this file.

## [1.0.3] - 2026-06-09

### Fixed
- **`ccr_managed=False` now fully honoured**: the "Manage ccr" toggle previously
  only suppressed the on-quit `stop()`. `sync()` and `spawn_env()` now treat
  `ccr_managed=False` as a hard gate — they never write config, start, stop, or
  restart ccr regardless of custom-model settings. When ccr is not managed and
  not running, `spawn_env()` returns a **distinct** fallback reason that mentions
  "Manage ccr" / starting manually rather than the generic start-failed string.
  If ccr happens to already be running (externally), it is used as before.
- **N-project restore no longer pays N×4s**: a failed `start+poll` cycle now
  sets a ~30-second module-level cooldown. While the cooldown is active, a
  not-running probe makes `spawn_env()` return immediately — no start, no
  4-second poll wait — with the **same** failure reason string as the full
  cycle, so a restore burst aggregates into one toast; the retry-suppression
  detail goes to the debug log only. The probe itself still runs first (one
  ≤0.5s check), so a ccr started manually mid-cooldown is detected and used
  immediately — any successful probe clears the cooldown. Also cleared by
  expiry and by a `sync()` config-write (user may have fixed the provider).
  Worst case for an N-project restore with an unbindable ccr: ONE 4s poll
  budget total, not N×4s.
- **`restart()` bounded reap-wait**: `restart()` now polls the old `_started_proc`
  handle for up to 2s (in 0.25s steps) after `stop()` before overwriting it,
  preventing the handle from being dropped unreaped while the old server is still
  mid-shutdown. Proceeds to `start()` regardless of outcome (never hangs).

### Changed
- **ccr fallback toasts are deduplicated**: multiple projects falling back within
  ~2s (e.g. during a restore) collapse to a single toast —
  `ccr unavailable — N projects running native Claude. <reason>` — rather than
  N individually dismissed toasts. There is at most one ccr toast in the overlay
  at a time; a new aggregate replaces any still-displayed one via
  `Adw.Toast.dismiss()` + re-add rather than queueing. The single-project format
  is verbatim-unchanged: `ccr unavailable — running native Claude. <reason>`.

### Internal
- **Agent adapter seam** (no user-facing change): introduced a pure `agents.py`
  module defining the `AgentAdapter` contract (`AgentCaps`, `SessionRef`,
  `SpawnPlan`) and `ClaudeAdapter` as backend #0, wrapping today's Claude spawn
  behavior bit-for-bit. `terminal.py` now routes every spawn through the
  adapter's `spawn_plan()` (argv + ccr env folded together); `spawn_claude()`
  remains as a thin back-compat alias over `spawn_agent(mode, session_id)`. The
  continue-with-fallback bash wrapper and the zellij init wrapper are
  generalized: the per-session zellij flag file now carries the agent's
  continue command line and the shell wrapper execs its content rather than
  hardcoding `claude -c || claude`. `session.json` gained a v2 dict entry form
  (`{"path", "agent"}`) read alongside the legacy string form (agent defaults
  to `claude`); `settings.py` gained `agents`/`agent_default`/`agent_overrides`
  plus `effective_agent()`, with `claude_binary` migrating into
  `agents['claude']['binary']` on load (old key still honored). Golden
  characterization tests pin the Claude adapter's spawn argv/env and the zellij
  wrapper/flag strings to the pre-refactor behavior byte-for-byte. Groundwork
  for the agent-agnostic program; Claude remains the only adapter and the
  default this release.

## [1.0.2] - 2026-06-09

### Fixed
- **ccr dead-port guard**: custom-model projects no longer silently die with
  connection-refused when `claude-code-router` is not installed or fails to
  start. `ccr.spawn_env()` now gates on ccr actually being reachable before
  injecting `ANTHROPIC_BASE_URL`; if it isn't, the spawn falls back to native
  Anthropic and a non-blocking Adw toast explains why (never a modal, never
  auto-retry). Both the direct-claude and zellij-create paths are guarded.
  ccr is now started **detached** (`Popen` with its own session and no pipes)
  rather than run-and-waited: `ccr` v2.0.0 does not daemonize in a non-tty
  (desktop-launch) context — the CLI process *is* the foreground server and
  never exits, so the previous `subprocess.run(..., timeout=10)` waited out the
  timeout and then killed the very server it had just started. The port probe
  is now the sole readiness arbiter: after a detached start the guard polls the
  port for up to ~4s in 0.25s steps before declaring failure. This also fixes
  1.0.1's startup `sync()` autostart, which had been silently killed at the
  10s timeout on every desktop (non-tty) launch. Starts are **single-flight**:
  a spawn arriving while a detached start is still binding its port polls
  instead of starting a second server — a double start races ccr's pidfile
  bookkeeping and leaves the surviving server unstoppable by `ccr stop`.

## [1.0.1] - 2026-05-20

> **Note:** version 1.0.0 was a mid-cycle bump in main.py that was never tagged as a release; in-tree builds carrying that string contained the test-isolation bug that clobbered `~/.ProjectMan/settings.json` (fixed below). This entry was originally drafted as 1.0.0 and is corrected to 1.0.1 to keep the version label honest.

### Added
- **Model-agnostic sessions**: ProjectMan can now back a Claude Code session with any LLM, not just Anthropic-hosted Claude. Claude Code stays the coder — its hooks, status dots, session history, and `--resume` are client-side and keep working unchanged regardless of which model serves the API.
- **Providers & Models settings page**: define LLM providers and their models as JSON (shape mirrors `opencode.json`) and pick a global default model. "Anthropic (native Claude)" is the default and behaves exactly as before.
- **Per-project model override**: a "Model" submenu in each project's right-click menu pins that project to a specific model (or back to the global default). A changed model applies to the next session — a prompt offers to restart a live one.
- **claude-code-router (ccr) management**: when a custom model is active, ProjectMan writes ccr's config, supervises the service, and points the spawned `claude` at it via environment variables. A "Manage ccr" toggle lets you opt out and run ccr yourself.

### Fixed
- **Test isolation: settings.json no longer clobbered by `pytest`.** `Settings.save()` and `Settings.load()` resolved their default path at function-definition time, so a test that built a `Settings(...)` object and triggered `PAAMonitor.run_scan()` would write the test's settings into the developer's real `~/.ProjectMan/settings.json`. On reboot the leaked `projects_dir` (a pytest tmp path) vanished and the sidebar showed zero projects. Defaults are now resolved at call time, and a `tests/conftest.py` autouse fixture redirects both `DEFAULT_SETTINGS_PATH` and `paa_monitor._MTIME_CACHE_PATH` to a per-test temp dir.

### Known limitations
- ccr's `~/.claude-code-router/config.json` holds provider API keys in cleartext (ccr has no keyring); ProjectMan writes it `0600` in a `0700` directory and hardens `settings.json` to `0600`.
- Tool-use reliability with non-Anthropic models varies by model; MCP tool search is disabled for non-Anthropic endpoints unless `ENABLE_TOOL_SEARCH=true`.
- Per-project models under the zellij multiplexer are best-effort (the zellij server is shared); the default non-multiplexed path is fully supported.

## [0.5.0] - 2026-05-07

### Improved
- **PAA Acknowledge vs Dismiss**: the two actions now have distinct meanings instead of being functionally identical. Dismiss = "not a problem, don't show again" (sticky-forever, unchanged). Acknowledge = "I see it, parking it for now" — hidden by default, surfaced via a new **Show acknowledged** toggle in the card window's filter row, and rendered dimmed with a dashed border when shown. Acknowledged cards swap their primary button to **Un-acknowledge** so a parked item can be returned to pending.
- **PAA sweep**: extended to auto-resolve acknowledged items whose underlying issue disappears, symmetric with how pending items resolve. Fix something elsewhere and the parked entry quietly clears instead of lingering. Dismissed items stay sticky as before.
- **PAA settings layout**: Chat Model moved out of "AI Analysis" into the top "Projects Admin Agent" group. Discuss sessions work whether or not background AI scans are enabled, so the model picker shouldn't be gated on the AI Analysis toggle. Subtitle clarified to "Default model used for Discuss sessions".

### Internal
- Legacy `'approved'` ledger status is auto-migrated to `'acknowledged'` on load.

## [0.4.4] - 2026-05-06

> **Note:** version 0.4.3 was a mid-cycle bump in main.py (commit d2cf128) that was never tagged as a release; in-tree builds carrying that string contained the deactivate respawn race fixed below. This entry was originally drafted as 0.4.3 and is corrected to 0.4.4 to keep the version label honest.

### Improved
- **Smart copy**: collapses TUI hard-wrap artifacts (the `\n  ` / `\n   ` hanging-indent breaks Claude Code and other TUIs emit) so copied prose pastes as flowing text, while paragraph breaks, code indents, and short structural lines are left alone.
- **Resource bar CPU**: per-PID tick sampling with a 30-second rolling average produces a smooth, stable reading instead of jittery system-wide noise.

### Fixed
- **Deactivate respawn race**: the `bash -c` wrapper around `claude -c` now traps `TERM`/`HUP` and exits 143, so a graceful claude exit (codes 1–128 in response to a signal) can no longer trip the respawn guard. Previously, clicking Deactivate could leave the project stuck "active" because the wrapper exec'd a fresh claude inside the same PID.
- **Ctrl+click in scrollback**: switched URL/path matching to VTE's `check_match_at()` so registered regexes do their own coordinate translation. The previous hand-rolled lookup indexed scrollback rows with viewport row numbers, turning every click into a miss whenever any scrollback was present.
- **StatusWatcher subdirectory snapshots**: when worktree status files collapse to the parent project, the newest same-session snapshot wins instead of an arbitrary one.
- **Sidebar rename focus race**: `grab_focus()` is now deferred via `GLib.idle_add` so the closing context-menu popover doesn't restore focus to its previous holder and trigger our focus-leave handler before the entry is interactive.
- **PAA monitor**: skips placeholder projects, honors `paa-ignore`, and uses a hybrid bare-name policy to suppress drift false positives.
- **PAA cross-project refs**: `../<name>` resolves as monorepo-relative when `<name>` exists locally, instead of getting flagged as a broken sibling reference.
- **PAA haiku health prompt**: expanded the internal-project allowlist to reduce noise on lab-only projects.

## [0.4.2] - 2026-04-11

### Improved
- **Resource bar**: shows PM process-tree CPU and RAM instead of system-wide
- **Sidebar**: replaced row-toolbar restart/archive buttons with a single explicit Deactivate button (archive and new-Claude actions remain in the right-click menu)

### Fixed
- **StatusWatcher**: worktree status files no longer roll up to the parent project. Stale `working` state from non-gracefully-exited worktree sessions could clobber the parent dot, leaving it stuck yellow and triggering spurious "Interrupt Active Work?" dialogs on close.
- **Deactivate**: clears zellij flags before killing the session to prevent detached ghost sessions.
- **Ctrl+click on Wayland**: reads modifier state from the seat keyboard rather than the gesture event, which loses modifiers on GTK4/Wayland.

## [0.4.1] - 2026-03-30

### Fixed
- **Shutdown hang**: SIGTERM race in `bash -c 'claude -c || exec claude'` could respawn a new claude that never received the signal, causing the shutdown window to spin indefinitely. Now only restarts for normal failures (exit 1–128), not signal kills (>128).
- **Process linger after Force Shutdown**: added explicit `app.quit()` after window destroy as a safety net so the Python process always exits.
- **Context-drift false positives**: bare filenames mentioned in prose (e.g. `setup-env.sh`) no longer flag when the same file is already referenced by a valid full path elsewhere in CLAUDE.md. Also fixed resolution of absolute paths (not just `~` and relative).

### Improved
- **Discuss sibling cards**: clicking Discuss on a PAA card now includes other pending findings for the same project in the prompt, so Claude is aware of related issues the user may want to address as a group.

## [0.4.0] - 2026-03-28

### Added
- **PAA Phase 4: Cross-project coordination**
  - Stale project detection (configurable threshold, default 60 days)
  - Cross-project reference validation (broken `../sibling/` refs in CLAUDE.md)
  - Shared dependency version conflict detection with optional AI analysis
  - Global health summary row in card window (project counts, git/CLAUDE.md coverage)
  - Green badges for cross-project findings
- **PAA Phase 3: Chat panel**
  - "Discuss" button on each card opens an interactive Claude session with finding context pre-loaded
  - "Chat" header button for general PAA conversation
  - Horizontal split view: cards on left, VTE terminal on right (reveals on demand)
  - Active card visually merges with chat panel (blue border, open right edge)
  - Dismiss/Acknowledge closes active discussion
  - Harness deployment: CLAUDE.md with "On Discuss" flow for finding-specific sessions
- **PAA Phase 2: AI triage**
  - Haiku-powered project health checks: semantic staleness, dependency versions, general health
  - Token budget with monthly reset, unlimited mode with red warning
  - Parallel AI scanning (5 concurrent workers via ThreadPoolExecutor)
  - Mtime-based change detection (zero tokens when idle)
  - AI criticality assessment with CRITICAL badge on cards
  - Scan progress indicators (spinner on sparkle button, project names in card window)
  - Card filters: project dropdown, critical toggle, type filter
  - On-demand per-project "Haiku Check" from right-click context menu
  - Budget display in card window stats row
  - Configurable scan and chat model selection (default: Haiku for scans, Sonnet for chat)
- **PAA Phase 1: Autonomous background monitor**
  - Background monitoring loop scans projects for health issues on a timer
  - Filesystem checks: missing CLAUDE.md, context drift (stale file references), no git repo
  - Persistent action ledger survives restarts (`~/.ProjectMan/paa-ledger.json`)
  - Card-based findings window with Dismiss/Acknowledge actions
  - Sidebar sparkle button with golden glow throb when items pending
  - PAA settings tab: enable toggle, scan interval slider, Phase 2 placeholder controls

### Fixed
- Status hook: `PostToolUse` maps to `working` (not `done`) so status stays yellow while Claude is active
- Status hook: worktree paths map back to parent project for correct status display
- Card window: deferred refresh prevents GTK widget-is-ancestor assertion on dismiss/acknowledge
- Card window: stale widget refs held to prevent premature GC during tooltip cleanup
- AI scans run in background thread to avoid freezing the UI
- AI scans run from `.project-admin-agent/` directory to avoid polluting real project sessions
- Markdown code fences stripped from Haiku responses before JSON parsing
- Dotfiles included in project listings (prevents false "missing .gitignore" findings)
- Semantic staleness prompt tells Haiku not to read CWD files (prevents false positives from PAA directory confusion)
- Wildcard (`*`) dependency specs treated as compatible with all versions in conflict detection

## [0.2.0] - 2026-03-19

### Added
- **PAA MVP: Projects Admin Agent**
  - Sparkle button in sidebar launches PAA terminal window
  - PAA harness files: CLAUDE.md agent instructions, gather-context.sh snapshot generator
  - VTE terminal overlay with project widgets (counts, status, disk, snapshot age)
  - Right-click context menu (copy, paste, select all)

## [0.1.5] - 2026-03-16

### Added
- Per-project status colors (done/working/waiting/idle) via Claude Code hook system
- Argonaut Dark, Candyland, Phosphor, and Salt Spray terminal color themes
- Debug logging toggle (Settings or `--debug` flag)
- Ctrl+click opens URLs and file paths from terminal
- Terminal scrollbar
- Sidebar pin/collapse with persistent width
- Ctrl+Tab switches to previously active project (MRU toggle)
- Project filter search in sidebar and archive window
- Terminal right-click context menu with copy/paste
- ntfy push notifications on session completion
- Confirm popovers for destructive actions (archive, new session)
- Active Only toggle for sidebar filtering

### Fixed
- Deactivate uses per-terminal zellij flag, not global multiplexer setting
- Deactivate kills process group for clean shutdown

## [0.1.0] - 2026-03-10

### Added
- Initial release
- GTK4/Adwaita desktop application for managing Claude Code sessions
- Project sidebar with expand/collapse session history
- Embedded VTE terminal per project
- Session restore on startup
- Zellij multiplexer integration (named sessions, auto-attach, detach detection)
- Settings window (General, Terminal, Appearance, About pages)
- Inline project creation and rename
- Project archiving
- App icon (hammer and anvil SVG)
- install.sh installer script
