# Changelog

All notable changes to ProjectMan will be documented in this file.

## [1.5.3] - 2026-08-03

### Added
- **Terminal links:** Ctrl+click and right-click → Open URL now recognize any
  RFC 3986 `scheme://` URI, not just `http(s)` and `file`. Custom schemes with
  no desktop handler (for example `vivaldi://settings/keyboard`) are handed to
  the default web browser so browser-internal pages can open.

## [1.5.2] - 2026-07-28

### Fixed
- **Project switch focused the nav menu instead of the terminal:** a
  double-click on a row inside a group's nested listbox also reaches the
  OUTER listbox's press gesture, which grabs keyboard focus to the GroupRow
  after the activation handler runs (the leaked group toggle was suppressed
  in 1.5.1, but the focus steal was not — typing went to the sidebar until
  you clicked the terminal). The terminal focus grab in `_switch_to_project`
  is now deferred to idle, landing after the whole click sequence. Regression
  coverage: widget-level red/green test reproducing the outer-listbox steal
  (fails with the synchronous grab, passes with the deferred one).
- **Kimi bridge: dot stayed yellow while Kimi waited on the "user poll"
  tool:** `AskUserQuestion` blocks the turn on the user's answer, but the
  event map marked every `PreToolUse` as `working`. `PreToolUse` with
  `tool_name: AskUserQuestion` now maps to `waiting` (blue); the answer's
  `PostToolUse` restores `working` via the normal map.

## [1.5.1] - 2026-07-26

### Fixed
- **Sidebar PopoverMenu leak — the 2026-07-24 ~60 s freeze + 12 GB heap:**
  `_rebuild_popover` parented a brand-new `Gtk.PopoverMenu` to the row on
  every menu-model change without `unparent()`ing the previous one; the old
  trees accumulated forever (~74k popovers ≈ 11.7 GB, and the burst itself
  was the main-loop freeze). The old popover is now unparented on rebuild,
  and a throttled rebuild-storm guard (>20 rebuilds in 10 s logs a stack
  trace) will catch the still-unidentified burst trigger if it recurs.
- **Zellij main-thread hangs:** `kill_session()` now passes `timeout=5`
  (its callers are synchronous UI paths — a wedged zellij server could hang
  the UI indefinitely), and the zellij socket-dir change handler makes one
  `alive_session_names()` call instead of N per-project `list-sessions`
  subprocesses on the GTK main loop (up to ~60 s blocked per event).
- **Active Only filter behavior:**
  - Clicking a project with an already-running session while in Show-all
    returns the host section to Active Only again (the M-UX.10b move of the
    auto-flip to process-started-only had dropped this for reattach; failed
    spawns still never flip the filter).
  - Active Only now hides group rows that contain no active project
    (decluttered filtered view); the group holding the just-spawned/active
    project always survives, so the auto-flip still can't vanish the tree
    you're looking at.
- **Groups collapse on project switch (two mechanisms, both fixed):**
  1. The M-UX.10b auto "Active Only" filter engages on every successful
     spawn — the browse-Show-all → auto-return-to-active workflow. The
     GroupRow filter rule hid every group without a running project, so the
     organized tree vanished on each switch. Groups are no longer hidden by
     the flip itself (superseded 2026-07-26: groups *without* an active
     project are intentionally hidden in Active Only — see above); expansion
     state is untouched by the flip.
  2. A double-click on a project (or session) row inside a group's nested
     listbox was delivered to both listboxes: the inner activated the project
     (wanted) and the outer activated the containing GroupRow (leaked toggle
     → the group collapsed). A GroupRow activation that immediately follows
     a nested activation on the same ancestor chain is now ignored (one
     suppression per ancestor per nested activation); genuine header toggles
     and keyboard activation are unaffected.
  Reproduced and verified end-to-end on the gated test bench (headless cage + pm-click
  real double-clicks): auto Active-Only engages on spawn AND the group tree
  survives; activation still fires; header double-click still toggles.
  Regression coverage in `tests/test_groups_switch_expansion.py`.
- **Remote hooks:** a remote rich-status install could register ProjectMan
  hooks in the remote's `~/.claude/settings.json` without `hook.js` on disk,
  breaking Claude Code on the remote (MODULE_NOT_FOUND on every lifecycle
  event). `install.sh` now bundles `hooks/hook.js`, provisioning falls back
  to the live local hook for pre-bundle trees, and registrations are only
  written when `hook.js` is actually present on the remote.
- **Per-harness provider/model memory:** switching harness no longer
  permanently loses provider/model pins for the project. On leave, the flat
  pin for the harness's owned axis is **stashed** into `harness_axis_memory`
  (not left sitting in the flat maps); on return that axis is **restored**
  into `provider_overrides` / `model_pins` (reverses the 1.4.x unconditional
  clear-with-no-memory). Claude owns provider overrides; Grok/OpenCode/Kimi
  own model pins. First visit to a harness still clears that harness's axis
  for the project.

_Deferred UX polish TODOs (editor unsaved-changes affordance; sidebar
new-project count while inline-edit row is visible) remain open._

## [1.5.0] - 2026-07-17

### Added
- **Kimi Code harness (4th first-class backend):** Moonshot AI `kimi` adapter
  with full caps (continue/resume/sessions/status/model/headless). Continue does
  **not** fall back via `|| kimi` (probed: kimi itself starts a fresh session
  when nothing is continuable). Sessions via storage-scan of
  `session_index.jsonl` + `state.json`. Status bridge under `bridges/kimi/` with
  `[[hooks]]` merge into `~/.kimi-code/config.toml`. Model aliases from
  `~/.kimi-code/config.toml`; VTE Shift+Enter capture; local and remote PATH
  include installer bin dirs so bare `kimi` resolves for GUI-launched sessions.
- **Virtual project groups:** organize projects into nested folders in the
  sidebar **without** nesting on disk. Projects remain flat under each host's
  `projects/` directory; only the UI tree and a membership map change.
  - Nested groups, max depth 5 (root group = depth 1).
  - Localhost: `~/.ProjectMan/project_groups.json`.
  - Remote hosts: the same path on the remote home directory (fetch/push over
    SSH; last-write-wins).
  - How to use: host-line **+** menu → **New Project** / **New Group**; group-line
    **+** menu → **New Subgroup** / **New Project** (right-click menus remain).
    On a project row, **Move to group**. New group/project creation switches the
    host section filter to **all**. Expanding a group shows its children;
    expanding a project still shows sessions (unchanged).
  - Design note: `docs/project-groups.md`. Experimental UI organization — disk
    layout stays flat.
  - Remote group push is **async** (off the GTK main thread) with coalesce;
    health-fetch apply uses a per-host write generation so a stale fetch
    cannot roll back a just-pushed forest. Nested `select_project` expands
    ancestors with one persist per host (not one push per level).

### Fixed
- **Harness installer PATH for local spawn:** GUI-launched ProjectMan does not
  source `.bashrc`, so bare `kimi` / `grok` / `opencode` failed after install.
  Installer bin dirs are now prepended for local spawns, doctor, and at app
  startup (same idea as remote PATH injection).
- **Settings → Models Add Provider:** Adwaita CRITICAL from double-adding the
  sticky “Add Provider” row (PreferencesGroup internal ListBox parent check),
  and empty providers no longer persist when the editor is dismissed without
  filling name/URL/models.
- **Harness / PAA / ntfy discoverability:** plain-English definition of
  “harness” on Settings → Harnesses; clearer PAA (Projects Admin Agent) and
  ntfy.sh tooltips/subtitles.
- **Project rename name policy:** local rename now rejects the same shell
  metacharacters / path junk as create (`project_name_reject_reason`), so
  `$(…)` / `` ` `` names cannot re-enter via rename after create was hardened.
- **Grok `Ctrl+;` / `Ctrl+'` queue shortcut under VTE:** those chords were
  arriving as bare `;` / `'` because VTE cannot encode Ctrl+punctuation.
  ProjectMan now feeds Kitty CSI-u sequences at CAPTURE, driven by a new
  per-harness `vte_key_captures` section in `settings.json` (Shift+Enter for
  all four harnesses; Grok also gets the queue chords). See
  `docs/grok-ctrl-semicolon-queue-shortcut.md`.

## [1.4.7] - 2026-07-14

### Added
- **Deferred deactivate with UNDO:** the stop control starts a 5s grace period
  instead of an immediate confirm-popover kill. An always-visible **UNDO**
  button cancels; the timer then deactivates. Pending rows italicize the name
  and take a theme warning wash (hover-hidden actions stay visible for UNDO).
  Cancel paths cover natural exit, archive, rename, respawn (`spawn-begin`),
  and shutdown.
- **Missing-harness install dialog (M-UX.10a):** a missing binary no longer
  relies on a persistent toast alone. A dialog offers copy-to-clipboard for the
  official install command and for an AI install prompt (local vs remote SSH
  wording). Per-adapter `install_command` one-liners for Claude, OpenCode, and
  Grok Build.

### Fixed
- **Timer-fired deactivate with no terminal:** missing `TerminalView` paths
  now set the row inactive instead of leaving a stuck pending/attached state.

### Changed
- **Grok waiting threshold:** `PHASE_WAITING_THRESHOLD` 5s → 10s before a
  long `pre_tool_use` working snapshot is promoted to waiting.

## [1.4.6] - 2026-07-12

### Fixed
- **Per-host Active Only on project activation:** selecting a project and
  starting a session again flips that host's sidebar section to "active
  projects" (the old global Active Only behavior, now scoped per host).
  Spawn failures still reveal the board on that host only (C7). Restore arms
  active-only per host with sessions to reopen.

## [1.4.5] - 2026-07-11

### Fixed
- **Harness / provider restart continues the conversation:** choosing a new
  harness (or provider) on a project with a live session and accepting
  **Restart Now** re-spawns with continue semantics (`-c`), same as activating
  the project. Previously this path always used **New Session** (`spawn_fresh`),
  so existing conversations in the newly selected harness were ignored. **New
  Session** in the sidebar still starts fresh.

### Changed
- **README reorganized** for clearer onboarding (table of contents, two-track
  how-to-run, installation lifecycle, cross-harness spawn/continue/resume table,
  PAA subsections, See also links).

## [1.4.4] - 2026-07-11

### Changed
- **PAA model axis (roadmap #2):** AI scans and Discuss use
  `build_spawn_env` + tier resolution so custom providers (e.g. Ollama) and
  per-tier model maps apply. Bare native Anthropic only when no provider is
  configured (same fallback as project terminals).
- **Terminology:** sidebar **AI Scan** (was “Haiku Check”); Settings scan/chat
  pickers labeled Fast/Standard/Capable with Haiku/Sonnet/Opus tier names;
  copy no longer claims scans always bill native Anthropic.
- **Settings cleanup:** removed duplicate Claude Code binary row from General
  (Harnesses page is the sole editor). Removed **Claude JSON** settings tab —
  no in-app editing of `~/.claude/settings.json`.

## [1.4.3] - 2026-07-11

### Added
- **Sticky host headers in the sidebar:** host section chrome pins at the
  top of the project list while you scroll that host's projects (Excel
  freeze-row style). The next host header pushes the previous pin off.
  Implemented via per-host section containers (header outside the project
  `ListBox`) plus a scroll-tracked overlay pin.

### Fixed
- **Project rename (localhost + remote):** context-menu Rename was cancelled
  immediately by a focus-leave race when the popover closed (entry never
  stayed editable). Leave-to-cancel is now armed only after rename focus
  settles. Remote rename also actually runs over SSH via
  `remote_store.rename_remote_project` instead of local `os.rename` on an
  `ssh:` ref (which always failed silently).

## [1.4.2] - 2026-07-10

### Changed
- **Sidebar host filter labels:** under each host name, modes read
  `(all projects)`, `(active projects)`, `(projects hidden)` (was
  `(all)` / `(active)` / `(hide)`); filter label font 10px → 11px.
- **`install.sh` status-bridge footer:** bullet list instead of a dense
  text wall so each harness line is scannable; **OpenCode** / **Grok**
  product capitalization in that footer.
- **`install.sh` session-bus check:** warn only when no session bus is
  available (and no `dbus-launch` / `dbus-run-session` either). Stops the
  false-positive `dbus-x11` warning on Fedora Workstation (dbus-broker
  session bus is already present without `dbus-launch`).

## [1.4.1] - 2026-07-10

### Changed
- **Settings dual-axis split:** per-project pins no longer share one
  `model_overrides` map. **`provider_overrides`** holds provider ids
  (`''` = native; absent = follow `model_default`). **`model_pins`** holds
  optional model ids for harness `-m` (Grok/OpenCode today). Both axes are
  harness-agnostic storage so custom providers can later apply to all three
  harnesses without another settings rewrite. Legacy `model_overrides` is
  dual-read once on load and split; save writes only the new keys.
- Harness switch clears **both** provider and model pins for that project.

### Notes
- Closes the dual-use follow-up from 1.4.0 (provider pin vs model pin sharing
  one map).
- Future native model pickers should write `model_pins` only; Provider menu
  continues to write `provider_overrides`.

## [1.4.0] - 2026-07-10

Harness-agnostic multi-backend release (Claude Code, OpenCode, Grok Build).

### Added
- **Multi-harness cockpit:** Claude Code, OpenCode, and Grok Build as first-class
  backends (pick per project). Adapter seam in `harnesses.py`; status bridges
  under `bridges/opencode/` and `bridges/grok/`.
- **Settings → Harnesses** page: default harness, per-harness binary, doctor-lite,
  account lines, bridge install/update.
- **Projects right-click → Harness** menu: Claude Code / OpenCode / Grok Build
  with `(default)` on the global default.
- **Projects right-click → Provider** menu: one native option for the effective
  harness (Anthropic / Grok / OpenCode) plus Claude custom providers from Settings.
- **Claude model axis** retained: Settings → Models providers, tiers, max context,
  1M toggle, Fable (from 1.3.0). Grok/OpenCode models stay harness-owned configs.
- **Settings → Models:** “Default Provider, Claude Code” plus Grok/OpenCode
  “(future)” placeholders; native sections show “Managed by the harness”.

### Changed
- **Terminology:** UI and code use **Harness** (not “Agent”) for Claude Code /
  OpenCode / Grok Build. Projects menu **Provider** (not “Model”) for the
  provider/native picker. Settings keys: `harnesses` / `harness_default` /
  `harness_overrides` (dual-read legacy `agents` / `agent_*` on load; first save
  rewrites to the new shape). Session `open_paths[].harness` dual-reads legacy
  `agent`.
- **ccr removed:** Claude custom providers use direct env injection via
  `models.build_spawn_env` (no claude-code-router sidecar).
- **Harness switch** clears the project’s provider pin and sticky spawn harness
  so the next session follows the new harness default.
- **OpenCode** display name is `OpenCode` (product branding).

### Fixed
- **`install.sh` status-bridge install** after the harness rename (was calling
  undefined `agents.install_harness_bridge` with stderr silenced). Now uses
  `harnesses.install_harness_bridge` and surfaces failures.

### Notes
- Dual-use `model_overrides` follow-up landed in **1.4.1**.

## [1.3.0] - 2026-07-09

### Added
- **Per-provider "Max context tokens"** in the Provider editor (Settings →
  Models → provider card). Sits just below **API Key**. Injects
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` on Claude custom-provider spawns. Blank
  leaves the harness default (200k for Claude Code). Tooltip:
  *"Set the max tokens for non-1M models. If blank, harness will use its
  default (200k for Claude Code)."*
- **Per-model 1M toggle** on each model row in the provider editor. Encodes
  or strips a trailing `[1m]` on the stored model id (Claude Code's long-
  context flag). Replaces name-based auto-suffixing as the user-visible
  control for 1M context.
- **Fable tier activated** (no longer "(future?)") — tier pin + spawn env
  for `ANTHROPIC_DEFAULT_FABLE_MODEL`; `[1m]` handling broadened beyond
  GLM-only where applicable.

### Changed
- Provider editor layout: removed the top standalone **Context window**
  group; max-context lives in the **Provider** identity group under API Key.

## [1.2.2] - 2026-07-09
### Fixed
- **Settings → General → Projects → "Choose Folder…" no longer throws a
  `TypeError`.** The folder `FileDialog` was being given the `SettingsWindow`
  (an `Adw.PreferencesDialog`, which is not a `Gtk.Window`) as its parent; it
  now receives the stored `AppWindow` parent (a `Gtk.Window`) so the dialog
  opens. _(Pre-existing since the Settings rework; surfaced by the 1.2.1
  release gate persona battery.)_

## [1.2.1] - 2026-07-07
### Added
- **Provider editor sub-window** (Settings → Models) with save-on-change, a
  reachability probe, and per-provider classifier env levers (auto-mode model,
  background classifier, temperature, two-stage toggle).
- **Server-fed "Select Models" picker (C6)** — the per-project Model submenu
  lists each provider's actual models from its `/v1/models` endpoint instead
  of a static list.
- **Classifier-group pruning** — only `AUTO_MODE_TEMPERATURE` classifier levers
  are emitted; stale classifier keys no longer leak into the spawn env.
- **`base_url` input validation** in the provider editor.
### Fixed
- Provider editor never opened (`set_transient_for` on the
  `PreferencesDialog` parent), editor refresh on close, and the
  Escape-commit / stale-tier-scrub polish cluster.

## [1.2.0] - 2026-07-06
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
  by the release gate pilot.)_
- **Settings → Agents now answers "is my account connected?" per agent.** Each
  agent gets a read-only **Account** line, presence-based (the **Check** button
  stays the live probe): claude shows "Signed in (credentials present)" or
  "Not signed in — run `claude` once to sign in"; grok shows "Signed in (token
  present)", "API key configured (&lt;config path&gt;)" (the offline-pool
  recipe), or "Not signed in — `grok login`"; opencode reports what's provable
  from its config — "Providers configured: &lt;n&gt; (&lt;path&gt;)" or "No
  providers found". Token files are checked for presence only; their contents
  are never read. _(release gate round-1 yield.)_
- **The grok section shows the Claude-hooks compat state.** A read-only
  "Claude-hooks compat" line reads `[compat.claude] hooks` from
  `~/.grok/config.toml`: "disabled ✓ (status dots fire once)" when set false,
  otherwise "⚠ enabled — Claude's hooks may double-fire on grok events (fixed by
  Install/Update bridge)". Closes the gap where a file-driven behavior had no UI.
  _(release gate round-1 yield.)_
- **Creating a project tells you which agent it got.** The `+` flow now fires a
  one-shot "New project '&lt;name&gt;' — agent: &lt;Display&gt;" toast naming the
  resolved effective agent (including the missing-binary fallback), so a fresh
  project's agent is never a silent surprise. _(release gate round-1 yield.)_
- **Creating a project opens it straight away.** The `+` flow now activates the
  new project through the normal open path the moment it's created — dropping you
  into its agent instead of leaving you on an empty pane. The creation toast
  (above) still fires, naming the agent that just spawned. _(the maintainer's withheld
  round-3 finding #3.)_
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
- **The per-project Model submenu lists each agent's native models.** Picking a
  grok or opencode project's model in the sidebar now offers that agent's OWN
  models — grok's `~/.grok/config.toml` `[model.*]` keys, opencode's configured
  `provider/model` ids — instead of only the Claude/ccr list. The selection is
  written verbatim into the project's model override and passed to the agent as
  `-m <value>`, exactly as the adapters already expect (so the README's promise
  to "set the per-project model to pool-qwen in ProjectMan" finally works). The
  config-declared default is marked `• default`; claude's submenu is unchanged.
  _(release gate round 2 — noob/subscriber S7.)_
- **README/install notes for `dbus-x11`.** The package table and `install.sh`
  now name the `dbus-x11` package (providing `dbus-launch`); `install.sh` warns
  when it's missing, since a minimal install crashes hard with no session bus.
  _(release gate round 2 — noob S1.)_

### Changed
- **Settings → Models now surfaces grok's and opencode's native model configs
  (read-only).** Each agent that decides its model from its own file —
  grok's `~/.grok/config.toml`, opencode's `opencode.json` — gets a read-only
  section headed with the source path and an "edited in the agent's own config"
  note (the default model is marked). Closes the gap where grok ran Qwen from a
  file no ProjectMan surface showed. _(Found by the release gate pilot.)_
- **PAA "Enable AI Scans" copy is honest about Anthropic.** It now states the
  scans use the `claude` CLI and Anthropic credentials regardless of your
  default agent (they never route through grok/opencode/ccr). _(Found by the
  release gate pilot.)_
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
  release gate pilot.)_
- **Selecting a per-project agent gives feedback.** Picking an agent from the
  sidebar **Agent** submenu now fires a one-shot "Agent for &lt;project&gt;:
  &lt;agent&gt;" toast. _(Found by the release gate pilot.)_
- **The Claude Code Router (ccr) block no longer frightens people who don't use
  it.** When no custom Claude models are configured (no providers and no
  custom model overrides), the Models page collapses ccr to a single row —
  "Claude Code Router: not in use (only needed for custom Claude models)" —
  instead of showing service-state controls for a router you don't use. When in
  use, the full controls show as before, and the status line now adds "(routes
  custom Claude models)". _(release gate round-1 yield.)_
- **install.sh disables grok's Claude-compat hooks (load-bearing).** grok reads
  `~/.claude/settings.json` hooks by default, so Claude's ProjectMan hook would
  double-fire on grok events. The grok install step now sets
  `[compat.claude] hooks = false` in `~/.grok/config.toml` — an idempotent,
  create-if-missing TOML edit that preserves every existing user key — making
  the grok bridge the sole status writer for grok sessions.

### Fixed
- **The Model submenu no longer offers the same choice twice.** A claude project
  with no model override showed BOTH "Default (Anthropic (native Claude))" and a
  bare "Anthropic (native Claude)" — the native sentinel duplicated the Default
  item's resolved story (the same could happen on a grok/opencode row whose
  default resolves to a listed native model). The redundant entry is now
  suppressed, so "follow the default" and an explicit pin read as distinct
  choices again; a pin you actually took stays visible and checked.
  _(the maintainer's withheld round-3 finding #1.)_
- **Pending PAA findings no longer hide behind an inert button after a restart.**
  The find-indicator throb was edge-triggered (it fired only when the count
  *grew*) while its meaning is level-based ("findings await you") — so the 18
  findings that survived a relaunch, or that already existed when PAA was enabled
  mid-session, showed the count and never lit up. The indicator now arms whenever
  there are unseen pending findings and the PAA window is closed, and goes quiet
  once you open the window; genuine new findings still re-arm it.
  _(the maintainer's withheld round-3 finding #2.)_
- **Opening Settings no longer logs a Pango-markup warning.** The Provider
  Definitions row's subtitle is a literal JSON-shape hint with a `<id>`
  placeholder, but AdwActionRow parses subtitles as markup by default — so every
  Settings open emitted a `Gtk-WARNING` (`Element "markup" closed but open
  element is "id"`) and the subtitle rendered broken. The row now disables markup
  parsing. _(the maintainer's withheld round-3 finding #6.)_
- **Ending a session and starting a new one honors a pending agent change.** A
  restored session pinned the agent it was running so an incidental global default
  change couldn't swap it mid-flight — but that pin outlived the session: after
  deactivating and reactivating, the project re-spawned the OLD agent even when
  you had switched it (e.g. restored Grok Build, switched to Claude Code,
  deactivate, reactivate → it came back as Grok Build). The pin's lifetime is now
  the SESSION's: it is dropped when the child truly ends (natural exit, deactivate,
  or the session being killed) and re-resolved on the next launch, while a zellij
  detach/reattach still keeps it. _(the maintainer's withheld round-2 finding #2.)_
- **A failed restore can no longer erase the last good session.** A session that
  died the instant it restored (e.g. a zellij/no-auth project) left nothing
  running, and the close-time save then wrote an empty session over the last good
  one — silently losing it. ProjectMan now skips the overwrite when nothing ran
  this run and a non-empty session already exists (logging one line), preserving
  the previous session. Deliberately closing everything still saves. _(Experience
  Gate round 2 — power #3/#8.)_
- **A session that dies leaves an explanation, and the project you're looking at
  never vanishes.** When a child exits, the pane now shows a "session ended — exit
  N" line instead of freezing silently, and if the dying project is the one in
  view, the Active Only filter is dropped so its row stays visible. _(Experience
  Gate round 2 — power #6.)_
- **Agent-change / new-session over a live zellij project no longer orphans the
  zellij server.** The direct-spawn path now tears down the zellij session first,
  the same way the deactivate path always has (one shared helper). _(Experience
  Gate round 2 — flow-audit-1.)_
- **The "agent not installed" toast waits to be read.** The spawn-failure toast
  (missing agent binary) is now persistent until dismissed, like the ccr fallback
  toast — previously it auto-dismissed after five seconds, so an unfocused user
  could miss the install hint entirely. _(release gate round 2 — RB-1.)_
- **The Projects Admin Agent button is a real icon on every font stack.** The
  sparkle button shipped as a bare ✨ (`U+2728`) text label, which rendered as a
  Unicode "tofu" box on any host without an emoji font. It is now a bundled
  symbolic icon (`pm-sparkle-symbolic`), drawn with `currentColor` so it follows
  the theme; the pending-count and scanning indicator moved to a small adjacent
  label. _(the maintainer's withheld round-2 finding.)_
- **The Default-Model label no longer infers a model that isn't running.** When
  a grok config declares no `[models] default`, the label now reads "built-in
  default (managed by Grok Build)" instead of promoting a lone `[model.*]` block
  as if it were active — the real default in that case is grok's built-in model,
  invisible to the config.
- **First-launch papercuts.** A fresh install now writes `settings.json` on the
  first launch (defaults persisted, not just held in memory); the empty-state
  placeholder reads "Select a project in the sidebar to start a session"; the
  Filter entry and remaining header controls all carry tooltips. The PAA window's
  disabled state points at "Settings → PAA (Ctrl+comma)", and the README's PAA
  filesystem checks are "always on while PAA is enabled" (they don't run when PAA
  is off). _(release gate round 2 — power #1/#2, noob S8.)_
- **The sidebar subtitle now tells the truth about what's running.** When a
  restored session kept running one agent while the row was already configured
  for a *different* next-session agent (e.g. a live opencode session under a row
  set to Grok Build), the subtitle showed only the configured agent — claiming a
  backend that wasn't actually running. It now leads with what's live:
  "&lt;Running&gt; (next: &lt;Configured&gt;)" while a mismatch exists, and is
  unchanged (the configured agent, with any model suffix) otherwise. _(Found by
  the release gate flow-audit-0.)_
- **A failed spawn no longer leaves you with less UI than before.** Restoring a
  session arms the "Active Only" filter; if a restored project's agent binary was
  missing, the just-failed row was correctly kept but then **hidden** behind that
  filter — the recovery toast pointed at a row you couldn't see. A spawn failure
  now always drops the filter, revealing the board so the failed project (and its
  install hint) stays in view. _(release gate known-edge.)_
- **The Settings "debug logging" toggle is no longer a dead knob.** Launching
  without `--debug` unconditionally forced debug logging **off**, and the next
  settings save persisted that — so the Settings-window toggle never stuck.
  `--debug` now only overrides when actually passed; absent, the saved value is
  authoritative. _(P0-era ticket.)_
- **Picking a different agent for a restored session now actually switches it.**
  A restored session keeps its agent "sticky" so a *global* default change can't
  silently swap a running session out from under you. But that stickiness was
  also swallowing a *deliberate* per-project pick: choosing Agent → Claude Code
  on a restored Grok session, then clicking "Restart this session now?", brought
  the session back **still running Grok**. An explicit per-project pick now drops
  the stickiness before the restart re-resolves the backend, so the new agent
  spawns; an incidental global/default change still leaves a restored session
  untouched. _(Found by release gate subscriber-walk S8; confirmed by the maintainer.)_
- **PAA "Haiku Check" no longer bills Anthropic when scans are disabled
  (billing leak).** The on-demand AI scan called the `claude` CLI
  unconditionally — with PAA disabled, one click silently spent ~298 Anthropic
  tokens and showed no result. It now checks **both** guards (`paa_enabled` AND
  the "Enable AI Scans" toggle) *before* any model call: disabled → zero calls
  and a "AI scans are disabled (Settings → PAA)" toast; enabled → the scan runs
  and its result is shown. The PAA copy that claimed "no API cost" is gone.
  _(Found by the release gate pilot.)_
- **A missing agent binary no longer wrecks the UI (the first-run
  triple-whammy).** Activating a project whose agent isn't installed used to
  show a raw bash error, drop the project row entirely, and auto-enable the
  "Active Only" filter that hid the wreckage. Now: the spawn failure is
  detected (fast 126/127 exit), the row STAYS visible as inactive, and a
  one-shot toast names the binary and how to install it ("<binary> not found —
  <agent> isn't installed. <install hint>"). The "Active Only" auto-filter now
  engages only when a session actually starts, never on a failed attempt.
  _(Found by the release gate pilot.)_
- **"New Zellij Session" explains itself when zellij is off.** With the
  multiplexer set to anything but zellij, the action silently no-oped (a brief
  spinner, then nothing). It now shows "Zellij is disabled (Settings → Terminal
  → Multiplexer)" and doesn't spin. _(Found by the release gate pilot.)_
- **"Default Model" tells the truth for the active default agent.** The row and
  the sidebar **Model** submenu's "Default (…)" item used to claim "Anthropic
  (native Claude)" even when grok was the default agent running Qwen. They now
  show the effective agent's real model story — for grok/opencode, "Managed by
  &lt;agent&gt; (&lt;config path&gt;)" plus the resolved default model name.
  _(Found by the release gate pilot.)_
- **The Settings → Agents bridge button reflects the installed state.** It
  showed "Install bridge" even when the bridge was already installed and current
  (C5). It now reads the F12a manifest and shows **Bridge installed ✓ /
  Reinstall**, **Update bridge**, or **Install bridge** accordingly. _(Found by
  the release gate pilot.)_
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
- **The sparkle icon on the Projects Admin Agent button renders instead of an
  empty box.** The bundled `pm-sparkle-symbolic.svg` lived under
  `icons/scalable/actions/`, but GTK4's unthemed `add_search_path` lookup
  resolves files at the search-path ROOT and ignores the freedesktop
  `scalable/<context>/` tree — so the icon never loaded. The SVG now sits at
  `icons/pm-sparkle-symbolic.svg`, where the lookup finds it (the placement rule
  is documented at the `add_search_path` site). _(the maintainer's second reveal.)_
- **A project's Model submenu "Default (…)" label now tells THAT project's
  story.** The label was computed once from the global default agent and pushed
  to every row, so a project that overrode its agent to Claude Code on a
  Grok-default machine read "Default (Managed by Grok Build … — Qwen3.5 9B)" —
  naming an agent it doesn't run. Each row now derives its own label from its
  effective agent, so a Claude-override row shows Claude's native model story and
  a follow-default row still shows the global default's. _(the maintainer's second
  reveal.)_

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
  `tests/fixtures/opencode/`); the parser is layered and defensive
  (JSON CLI → storage scan) against opencode's cross-version CLI/storage drift.

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
