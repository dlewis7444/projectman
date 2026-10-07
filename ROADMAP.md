# ProjectMan Roadmap

This is the living roadmap for ProjectMan — a GTK4/Adwaita desktop cockpit for
AI coding harnesses. Items are **goals, not commitments**; order reflects current
priority. The roadmap is appended to as items are defined and pruned as they
ship.

## Near-term

### 1. Support remote (SSH) execution of harnesses

Projects and harness sessions can live on remote hosts over SSH while ProjectMan
on the workstation is the cockpit. Host axis (Settings → Hosts), sectioned
sidebar, remote create/list, SSH spawn, health micro-dots, session restore,
opt-in rich status (bridge install + poll), per-host Edit (name, paths, binaries).

Still optional / polish:

- Detach/reattach (remote zellij) — disconnect kills process today
- ControlMaster / async SSH to avoid UI stalls on slow hosts

### 2. Update PAA to use the correct model(s) — **shipped in 1.4.4; Discuss harness in 1.6.0**

PAA AI scans route through the harness adapter seam (`effective_harness` +
`headless_plan`). Discuss/Chat VTE follows the **global default harness**
(same as a new unpinned project) and that harness’s default provider when it
has a provider axis. Fast/Standard/Capable remain PAA settings and apply
when the resolved harness maps those tiers (Claude Code). Sidebar label is
**AI Scan** (was “Haiku Check”). Storage key `paa_allow_haiku` kept for
settings back-compat.

PAA remains **localhost-only**.

### 3. Custom providers for every harness

Settings → Models defines Anthropic-compatible **custom providers** (base
URL, API key, model list, tiers, max context, 1M toggle, optional search
endpoint).

**Shipped in 1.7.0:** Claude Code and Grok Build. Each harness has its own
**Active Provider** (Settings → Harnesses; absent means that harness’s native
backend). A project can override it from the sidebar. Grok custom spawns use
a ProjectMan-managed home at `~/.ProjectMan/grok-homes/<provider>/` and are
refused over remote SSH. An optional **Search endpoint** on the provider
registers a `web_search` MCP tool for those grok sessions.

**Still open:** OpenCode and Kimi stay native-only (the Active Provider row
is shown and cannot be changed). Remote custom-provider grok needs a way to
ship the managed home to the other host. The phone bot stays on native Grok
until per-provider reviewer certification.

Native subscription backends stay first-class; custom providers are the
portable path (for example an Ollama pool) across harnesses.

### 4. Set up claude/projectman to work with telegram

Telegram-PAA is `docs/paa-telegram-plan.md`. The private bot is the phone
client: it owns one resumable PAA conversation and, in the same process, runs
the existing scan loop so new pending findings show up in that chat. The
Kotlin/Compose Android app is deferred.

**Status 2026-10-05:** live on the primary workstation and in daily testing. Locked behind TOTP
(`/unlock`), ro read-only mode probe-verified, rw reviewer-gated shell via a
bot-owned MCP `run_command` (the reviewer model is the sole gate; static
denylist + denial memory + full-command audit in `paa-journal.md`). Automated
findings deliberately do NOT post to Telegram (the maintainer override). Remaining
PAA-specific changes are tabled; the open items are harness portability
(item 5) and per-provider reviewer certification when item 3 lands.

### 5. Make PAA work with any harness

Today PAA's verified posture exists **only for Grok Build**: `chat_turn`
refuses opencode/kimi for any non-legacy policy (`_UNCONSTRAINED`,
`paa_headless.py:411` — no verified read-only flag), and the rw reviewer
gate is grok-specific end-to-end (bypassPermissions argv, deny-rule names,
MCP registration in the PAA cwd's `.grok/config.toml`, trust entry,
`--reasoning-effort`). Switching `harness_default` makes the bot refuse
every turn (fail-closed, but dead).

The work, per harness: probe-verify a read-only posture (its equivalent of
deny-guarded `plan`); port the reviewer-gated `run_command` channel (MCP
registration + the bot's per-turn token broker are harness-agnostic by
design); re-run the adversarial review + live probe gauntlet. Also add a
`harness_id` pin in `paa-telegram.json` so the bot's harness doesn't
silently follow the desktop default. Kimi first (it has MCP support and
headless chat); OpenCode after; Claude already has the plan/acceptEdits
mapping scaffolded but was never probe-verified for the bot.

## Future Possible Features/Changes

**Not on the roadmap.** Idea parking only — no priority, no commitment, no
schedule. Capture sparks so they are not lost; promote to Near-term only when
deliberately chosen.

- SSH ControlMaster managed by PM per host (snappier health checks + spawn)
- Per-host default harness/provider (“on cage, always native; on laptop, ollama”)
- One-shot copy/rsync project local ↔ remote
- Remote section header context menu: open shell, refresh now, enable status
  integration, remove host
- Manual “Reconnect / check now” on a red health header
- Jump host / ProxyJump field in host profile
- Verify clipboard / OSC52 behavior over SSH
- “Run this project on…” move-between-hosts (usually wrong without sync — skip
  unless a clear design appears)
- First-run “workstation cockpit + agent VM” wizard (add host, test SSH, ensure
  projects dir, offer status opt-in)
- Remote ntfy while laptop is asleep (requires something on the remote to
  publish — helper or remote-side hook)
- Remote zellij as first-class detach/reattach (v1 accepts process death on
  disconnect)
- Per-host health-check interval override (global interval first)
- Expand/collapse chevrons if no-indicator section headers fail real-use testing
