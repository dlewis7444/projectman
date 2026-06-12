# Changelog

All notable changes to ProjectMan will be documented in this file.

## [Unreleased]

### Added
- **Grok Build is now a first-class agent.** Drive xAI's
  [Grok Build](https://x.ai/cli) CLI (`grok`) per project alongside Claude Code
  and opencode — pick it from the sidebar **Agent** submenu or
  **Settings → Agents**. Spawn / continue / resume map to `grok`, `grok -c`
  (falls back to a fresh `grok` when there's nothing to continue), and
  `grok -r <id>`; per-project model is passed as `-m <config-key>`; the
  session-history expander lists recent sessions via `grok sessions list`.
  Includes a python3 **status bridge** (`bridges/grok/`, installed to
  `~/.grok/hooks/`) for the live status dots and a generalized **Agent submenu /
  Settings page** that surface the third agent automatically.
- The Grok ollama-pool recipe requires a per-model `api_key` in
  `~/.grok/config.toml` (any non-empty value) — without it grok forces a browser
  sign-in even for custom endpoints. Documented in the README.
- **`Ctrl+,` opens Settings.** Previously the gear was the only route. _(Found
  by the Experience Gate pilot.)_
- **Settings → Agents now answers "is my account connected?" per agent.** Each
  agent gets a read-only **Account** line, presence-based (the **Check** button
  stays the live probe): claude shows "Signed in (credentials present)" or
  "Not signed in — run `claude` once to sign in"; grok shows "Signed in (token
  present)", "API key configured (&lt;config path&gt;)" (the offline-pool
  recipe), or "Not signed in — `grok login`"; opencode reports what's provable
  from its config — "Providers configured: &lt;n&gt; (&lt;path&gt;)" or "No
  providers found". Token files are checked for presence only; their contents
  are never read. _(Experience Gate round-1 yield.)_
- **The grok section shows the Claude-hooks compat state.** A read-only
  "Claude-hooks compat" line reads `[compat.claude] hooks` from
  `~/.grok/config.toml`: "disabled ✓ (status dots fire once)" when set false,
  otherwise "⚠ enabled — Claude's hooks may double-fire on grok events (fixed by
  Install/Update bridge)". Closes the gap where a file-driven behavior had no UI.
  _(Experience Gate round-1 yield.)_
- **Creating a project tells you which agent it got.** The `+` flow now fires a
  one-shot "New project '&lt;name&gt;' — agent: &lt;Display&gt;" toast naming the
  resolved effective agent (including the missing-binary fallback), so a fresh
  project's agent is never a silent surprise. _(Experience Gate round-1 yield.)_
- **Grok `waiting` dot via phase aging.** Grok fires no hook event while its
  permission prompt is on screen (the wire goes silent — bench mini-probe), so
  the blue dot is now *inferred*: the bridge stamps a `phase`/`phase_ts` on
  `pre_tool_use`, and ProjectMan promotes working → waiting when that phase
  goes unanswered for 5 seconds (the watcher re-checks on a one-shot timer; no
  extra processes). `post_tool_use`, `post_tool_use_failure`,
  `permission_denied` (newly registered — a deny outcome, not a waiting
  signal), `stop`, and a new prompt all clear the stamp. Known quirk
  (documented): a long-running *approved* tool briefly shows a false `waiting`
  that self-corrects when the tool completes. Claude/opencode status files
  carry no phase fields and behave exactly as before.

### Changed
- **Settings → Models now surfaces grok's and opencode's native model configs
  (read-only).** Each agent that decides its model from its own file —
  grok's `~/.grok/config.toml`, opencode's `opencode.json` — gets a read-only
  section headed with the source path and an "edited in the agent's own config"
  note (the default model is marked). Closes the gap where grok ran Qwen from a
  file no ProjectMan surface showed. _(Found by the Experience Gate pilot.)_
- **PAA "Enable AI Scans" copy is honest about Anthropic.** It now states the
  scans use the `claude` CLI and Anthropic credentials regardless of your
  default agent (they never route through grok/opencode/ccr). _(Found by the
  Experience Gate pilot.)_
- **README and the About page are now agent-neutral.** The headline/intro no
  longer call ProjectMan a Claude-only app ("desktop cockpit for AI coding
  agents"), `claude` moved from a hard requirement into an optional **Agents**
  table (each agent optional, with install pointers), the `PATH` step is a
  numbered install step, and a new **Installing Grok Build** section documents
  the curl installer and the account-vs-pool choice. _(Found by the Experience
  Gate pilot.)_
- **install.sh output is clearer.** It now prints the version + git short-hash
  it's installing ("Installing ProjectMan 1.1.1 (&lt;hash&gt;)"); the Claude-hook
  summary is per-agent-coherent (claude absent ⇒ "hooks staged; they activate
  when claude is installed", no more warn-then-"registered!" contradiction); the
  grok compat note is rewritten for someone who's never seen grok's config; and
  the unused `GROK_*_DEST` shell vars (SC2034) were removed. _(Found by the
  Experience Gate pilot.)_
- **Selecting a per-project agent gives feedback.** Picking an agent from the
  sidebar **Agent** submenu now fires a one-shot "Agent for &lt;project&gt;:
  &lt;agent&gt;" toast. _(Found by the Experience Gate pilot.)_
- **The Claude Code Router (ccr) block no longer frightens people who don't use
  it.** When no custom Claude models are configured (no providers and no
  custom model overrides), the Models page collapses ccr to a single row —
  "Claude Code Router: not in use (only needed for custom Claude models)" —
  instead of showing service-state controls for a router you don't use. When in
  use, the full controls show as before, and the status line now adds "(routes
  custom Claude models)". _(Experience Gate round-1 yield.)_
- **install.sh disables grok's Claude-compat hooks (load-bearing).** grok reads
  `~/.claude/settings.json` hooks by default, so Claude's ProjectMan hook would
  double-fire on grok events. The grok install step now sets
  `[compat.claude] hooks = false` in `~/.grok/config.toml` — an idempotent,
  create-if-missing TOML edit that preserves every existing user key — making
  the grok bridge the sole status writer for grok sessions.

### Fixed
- **PAA "Haiku Check" no longer bills Anthropic when scans are disabled
  (billing leak).** The on-demand AI scan called the `claude` CLI
  unconditionally — with PAA disabled, one click silently spent ~298 Anthropic
  tokens and showed no result. It now checks **both** guards (`paa_enabled` AND
  the "Enable AI Scans" toggle) *before* any model call: disabled → zero calls
  and a "AI scans are disabled (Settings → PAA)" toast; enabled → the scan runs
  and its result is shown. The PAA copy that claimed "no API cost" is gone.
  _(Found by the Experience Gate pilot.)_
- **A missing agent binary no longer wrecks the UI (the first-run
  triple-whammy).** Activating a project whose agent isn't installed used to
  show a raw bash error, drop the project row entirely, and auto-enable the
  "Active Only" filter that hid the wreckage. Now: the spawn failure is
  detected (fast 126/127 exit), the row STAYS visible as inactive, and a
  one-shot toast names the binary and how to install it ("<binary> not found —
  <agent> isn't installed. <install hint>"). The "Active Only" auto-filter now
  engages only when a session actually starts, never on a failed attempt.
  _(Found by the Experience Gate pilot.)_
- **"New Zellij Session" explains itself when zellij is off.** With the
  multiplexer set to anything but zellij, the action silently no-oped (a brief
  spinner, then nothing). It now shows "Zellij is disabled (Settings → Terminal
  → Multiplexer)" and doesn't spin. _(Found by the Experience Gate pilot.)_
- **"Default Model" tells the truth for the active default agent.** The row and
  the sidebar **Model** submenu's "Default (…)" item used to claim "Anthropic
  (native Claude)" even when grok was the default agent running Qwen. They now
  show the effective agent's real model story — for grok/opencode, "Managed by
  &lt;agent&gt; (&lt;config path&gt;)" plus the resolved default model name.
  _(Found by the Experience Gate pilot.)_
- **The Settings → Agents bridge button reflects the installed state.** It
  showed "Install bridge" even when the bridge was already installed and current
  (C5). It now reads the F12a manifest and shows **Bridge installed ✓ /
  Reinstall**, **Update bridge**, or **Install bridge** accordingly. _(Found by
  the Experience Gate pilot.)_
- **The Settings → Agents "Install bridge" button now installs the WHOLE grok
  bridge.** The GUI path previously copied only the hook JSON — the status
  script it points at never landed. Bridge installs are now manifest-driven and
  multi-file (grok: JSON + executable script; opencode: one plugin file), with
  `install.sh` and the GUI button sharing the same installer, so the two paths
  can never drift.
- **The installed grok hook JSON now carries the absolute script path.** The
  command is rewritten at install time (both install paths) instead of relying
  on grok shell-expanding `~`; the repo copy stays portable.

### Internal
- **Unknown-agent fallback no longer hardcodes Claude (M-P3.2).** When a named
  agent isn't registered (a stale/typo'd override), the fallback now resolves
  `agent_default` first, then the first-available registered adapter — so the
  Claude-less promise holds even with a bogus override. The spawn path
  (`get_adapter(id, settings)`), the diagnostic (`resolve_adapter(id, settings)`
  + new `fallback_adapter`), and the "agent X not available" toast all name the
  agent that will actually run, instead of always saying "Claude Code".
- **Continue→fresh fallback is now the adapter's declared policy (M-P3.3).** The
  zellij continue command and the direct-spawn continue wrapper no longer
  hardcode the `<agent> -c || <agent>` exit-semantics for every agent; each
  adapter declares it via `AgentCaps.continue_falls_back_to_fresh`. claude and
  opencode keep today's exact behavior (byte-identical wrapper output, golden-
  pinned); an adapter whose non-zero exit doesn't reliably mean "nothing to
  continue" can refuse the fresh fallback so a resume error never silently
  launches a fresh agent.
- **Adapter registration refuses id collisions (M-P3.5).** New
  `register_adapter()` is the single guarded entry point for custom adapters: it
  raises loudly on any id that already exists — built-ins always win, two customs
  can't fight over one id — replacing the plain-dict path where a custom `claude`
  silently shadowed the built-in.
- Regression net for the above plus a headless pin that the sidebar status dot
  consumes `caps.rich_status` (M-P3.1, landed last cycle); the slug-collision
  hazard (M-P3.4) is documented at the `slugFor`/hook.js slug sites pending its
  own design round.

## [1.1.1] - 2026-06-10

_(Version 1.1.0 was never released; the label is retired.)_

### Added
- **Signal-safe shutdown (SIGTERM/SIGHUP).** A logout, system stop, or terminal
  hangup now saves the session FIRST, then tears down the live **direct-spawn**
  children by their process group, then stops the managed ccr service — instead
  of dying with no session save and orphaning setsid process groups. Zellij
  terminals are deliberately left alive (their sessions persist by design). The
  handler is one-shot (`GLib.SOURCE_REMOVE`) so a second signal mid-shutdown
  cannot re-enter. Kill selection is the pure `session.plan_emergency_kill`; the
  teardown is `AppWindow.emergency_shutdown`.
- **opencode is a first-class second agent.** Pick it per project from the
  sidebar right-click **Agent** submenu, or as the default in **Settings →
  Agents**. Spawn/continue/resume map to `opencode` / `opencode -c` /
  `opencode -s <id>`; the session-history expander lists a project's recent
  opencode sessions via `opencode session list --format json` run **from the
  project directory** (the command is cwd-scoped) and filtered by each entry's
  directory. A storage-scan fallback covers old opencode builds with the
  per-session file layout; current builds store sessions in SQLite, where the
  CLI is the only session source (SQLite read support is P3 hardening).
  Per-project model is passed natively as `-m <provider>/<model>` (e.g.
  `ollama/qwen3.5:cloud`) — no claude-code-router involved. Full core
  experience with zero Claude Code installed.
- **Agent submenu** on each project row (stateful-radio, mirroring the Model
  submenu): Claude Code / opencode / Follow default, writing `agent_overrides`.
  A live session whose agent or model no longer matches offers a restart
  prompt. The row shows an effective-agent subtitle (+ model when set).
- **opencode status bridge** (`bridges/opencode/projectman.js`): a small
  opencode plugin that lights up the sidebar status dots for opencode sessions,
  mapping its lifecycle events onto ProjectMan's status schema. `install.sh`
  installs it idempotently into `~/.config/opencode/plugins/`; there is also an
  **Install bridge** button (and a doctor-lite binary check) in **Settings →
  Agents**.
- **Agent-neutral status directory** `~/.ProjectMan/status/` (Decision 2). The
  opencode bridge and `hook.js` both write here; `StatusWatcher` dual-watches
  the new and the legacy `~/.claude/projectman/status/` dirs through a
  deprecation window so existing installs keep working.

### Changed
- **De-Clauded UI strings** now that agents are pluggable: the empty-state
  placeholder ("start a session"), the close-while-working dialog ("Work is
  currently in progress on…"), the deactivate-session tooltip, and the ntfy
  payload ("<project> finished" instead of "Claude finished"). Claude naming
  stays where the thing *is* Claude (PAA, ccr pages, the Claude JSON editor).
- **Signal `project-new-claude` → `project-new-session`** (sidebar ↔ window),
  and the spawn API neutralized to `spawn_continue`/`spawn_fresh`/
  `spawn_resume` (the `spawn_claude` alias is retained, deprecated).

### Fixed
- **A second activation no longer spawns a duplicate window.** A DBus
  re-activation of the running app used to run the full build path again —
  duplicate window plus a re-restore of every project. `_on_activate` now
  presents the existing window and returns immediately when one is already up.
- **Closing a stray window no longer quits the whole app.** The close handler
  used to call `app.quit()` unconditionally, so closing any duplicate window
  would have taken unrelated sessions down too. `_quit` now quits the app only
  when the closing window is the primary one (pure `session.should_quit_app`),
  clearing `app._window` before quitting so a racing re-activation cannot
  present a destroyed window; a stray window just destroys itself.
- **The application id is test-overridable.** It was hardcoded, so any test or
  harness that constructed/activated the app shared DBus identity with the
  user's live instance. main.py now reads `PM_APP_ID` (falling back to the
  `APP_ID` constant), and an autouse conftest fixture pins every test run to
  `io.github.projectman.test` — a blanket guard independent of the display gate.
- **Session save records the RUNNING agent, not the settings-effective one.**
  A project restored under saved-agent-wins (e.g. saved as opencode while
  settings resolve claude) was re-saved with the settings agent, so the next
  restore silently dropped the running session. `_save_session` now derives
  each project's agent from the live terminal's spawn-time signature
  (`spawned_agent_signature()`, the same truth the restart prompt reads) via
  the new pure `session.collect_agents_map()`, falling back to
  `settings.effective_agent` only for a path with no terminal.
- **The sidebar dot no longer fakes green for a bridgeless agent.** The
  attached-row `idle → done` remap (no status file yet → green) is now gated
  on the effective adapter's `caps.rich_status` — its first consumer. Rich-
  status agents (claude, opencode) keep today's behavior byte-identical; a
  future `rich_status=False` agent shows the honest dim idle dot instead of a
  permanent false "work finished". If adapter resolution fails, the historic
  remap is preserved (the dot path never throws).

### Internal
- **The agent seam is now load-bearing (P1-review mandates M1-M3, m1-m3).**
  Every consumer that previously reached around the P1 seam now goes through
  it: the sidebar expander consumes `adapter.list_sessions(project)` returning
  `SessionRef`s (`.id`, not `.session_id`; `HistoryReader` is Claude-internal
  plumbing); restore threads `load_agents()` so a project recreates its saved
  agent (**saved-agent-wins on restore; settings-wins on new activations**),
  with `TerminalView` taking an explicit construction-time agent id; the zellij
  env decision moved behind `adapter.zellij_spawn_env()` (no `_claude_env`, no
  hardcoded `ANTHROPIC_*` list in `terminal.py`); `spawn_plan` is uniform across
  adapters with ccr test-injection off the protocol signature; the Model/
  expander/resume UI gate on `caps.model_select`/`caps.sessions`/
  `caps.resume_by_id`; and `resolve_adapter()` distinguishes a named-but-missing
  agent so `window.py` can show a one-shot "agent 'X' not available" toast.
- opencode session-list parsing is fixture-tested (recorded shapes under
  `tests/fixtures/opencode/`, marked to-be-verified on the VM gate); the parser
  is layered and defensive (JSON CLI → storage scan) against opencode's
  cross-version CLI/storage drift.

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
