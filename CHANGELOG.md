# Changelog

All notable changes to ProjectMan will be documented in this file.

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
