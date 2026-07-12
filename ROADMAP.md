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

### 2. Update PAA to use the correct model(s) — **shipped in 1.4.4**

PAA AI scans and Discuss sessions route through the model axis
(`build_spawn_env` + tier resolution). Scan/chat pick a Claude Code tier
(Fast/Standard/Capable = Haiku/Sonnet/Opus) mapped to the configured
provider. Sidebar label is **AI Scan** (was “Haiku Check”). Storage key
`paa_allow_haiku` kept for settings back-compat.

PAA remains **localhost-only**.

### 3. Custom providers for every harness

Today Settings → Models defines Anthropic-compatible **custom providers**
(base URL, API key, model list, tiers, max context, 1M toggle) that ProjectMan
injects for **Claude Code** only (`models.build_spawn_env`). Grok Build and
OpenCode still run against their own native configs; the Models page shows
placeholders / “managed by the harness” for them.

**Intention:** the same Settings provider catalog should be usable with
**Claude Code, OpenCode, and Grok Build** — pick a harness *and* a custom
provider (or that harness’s native backend) per project. Sidebar Provider /
model pins already use dual axes (`provider_overrides` + `model_pins`, 1.4.1);
the remaining work is adapter-side: teach Grok/OpenCode to honor a selected
custom provider (env / base URL / credentials as appropriate) instead of
native-only, and flesh out the Models UI beyond placeholders.

This is a product goal, not a commitment to a specific design. Native
subscription backends stay first-class; custom providers are the portable
path (e.g. Ollama pool) across harnesses.

### 4. Set up claude/projectman to work with telegram

Wire ProjectMan / Claude workflows to Telegram (details TBD).

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
