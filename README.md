# ProjectMan

![ProjectMan](images/ProjectMan.jpg)

A GTK4/Adwaita desktop cockpit for AI coding agents.

ProjectMan displays a project sidebar on the left and an embedded VTE terminal on the right,
running your chosen coding agent — [Claude Code](https://claude.ai/code), [opencode](https://opencode.ai),
or [Grok Build](https://x.ai/cli) — per project. Projects are directories under
`~/.ProjectMan/projects/` (configurable via Settings).

![Main window](images/screencap_main.jpg)

## Features

- Per-project agent sessions with automatic session restore
- Pluggable coding agents — **Claude Code**, **opencode**, and **Grok Build**
  side by side, pick per project (see "Using opencode" / "Using Grok Build")
- Live status indicators: working / waiting / done / idle
- Session history with expand/collapse per project
- Zellij multiplexer integration (optional)
- Project archive with search

  ![Archive window](images/screencap_archive.jpg)

- Ctrl+Tab to switch between recently active projects
- Multiple color themes: Argonaut, Candyland, Phosphor, Salt Spray
- Sidebar pin/collapse with persistent width
- Terminal right-click menu (Copy, Paste, Select All, Open URL / Copy URL)
- Ctrl+click to open URLs and file paths
- Process-tree CPU / RAM resource bar
- ntfy push notifications on session completion

### Projects Admin Agent (PAA)

The sparkle (✦) button in the sidebar opens the PAA — a background health monitor that
continuously scans your projects and surfaces actionable findings in a card-based window.

**Filesystem checks (always on):**
- Missing `CLAUDE.md`
- No git repository
- Context drift — stale file references in `CLAUDE.md` (bare filenames, relative paths,
  and absolute paths all resolved; external references deduplicated automatically)

**AI checks (optional, uses the `claude` CLI + Anthropic credentials):**

> The PAA's AI scans run the `claude` CLI against native Anthropic **regardless of
> your default agent** — they do not route through grok, opencode, or
> claude-code-router. A machine with no Anthropic access can still use the
> filesystem checks above; the AI checks simply stay off.

- Semantic staleness — `CLAUDE.md` no longer describes what the project actually does
- Outdated or conflicting dependency versions
- General project health

**Cross-project analysis:**
- Stale projects (configurable inactivity threshold)
- Broken `../sibling/` references between projects
- Shared dependency version conflicts

**Card window:**
- Filter by project, criticality, or finding type
- **Discuss** button — opens an interactive Claude session with the finding pre-loaded as
  context, plus any other pending findings for the same project so related issues can be
  addressed together
- Dismiss / Acknowledge actions with persistent ledger (survives restarts)
- Sparkle button throbs only when new findings appear

**PAA settings (Settings → PAA):**
- Enable/disable toggle and scan interval
- Haiku toggle, monthly token budget, and model selection for scans and chat

## Requirements

**System packages** (install before running `install.sh`):

| Distro | Command |
|--------|---------|
| Fedora / RHEL | `sudo dnf install python3-gobject gtk4 libadwaita vte291-gtk4` |
| Ubuntu / Debian | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita vte3` |

**Also required:**
- Python 3.10+
- Node.js (for the status-indicator hook script)

**Optional:** `zellij` for multiplexed terminal sessions.

### Agents (install at least one)

ProjectMan is a cockpit for coding agents — it does not bundle one. Install
whichever you use; each is optional and they work side by side:

| Agent | Install |
|-------|---------|
| **Claude Code** | <https://claude.ai/code> |
| **Grok Build** (`grok`) | `curl -fsSL https://x.ai/cli/install.sh \| bash` — see [Installing Grok Build](#installing-grok-build) |
| **opencode** | <https://opencode.ai> (see [opencode docs](https://opencode.ai/docs)) |

You can run ProjectMan with just one agent installed; the others appear in the
**Agent** submenu only when their binary is on your PATH.

## Installation

1. **Clone and run the installer:**

   ```bash
   git clone https://github.com/dlewis7444/projectman.git
   cd projectman
   ./install.sh
   ```

   This installs ProjectMan to `~/.local/share/projectman/`, creates a
   `projectman` launcher in `~/.local/bin/`, and registers it with your desktop
   environment (GNOME, KDE, etc.) so it appears in your app launcher.

2. **Ensure `~/.local/bin` is on your `PATH`.** If the `projectman` command
   isn't found after install, add this to your shell profile (`~/.bashrc`,
   `~/.zshrc`, etc.) and open a new shell:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

3. **Install at least one agent** (see [Agents](#agents-install-at-least-one)
   above) so ProjectMan has something to drive.

### Installing Grok Build

To use xAI's [Grok Build](https://x.ai/cli) CLI as an agent:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
```

This installs the `grok` binary (typically to `~/.grok/bin/grok`). By default
grok signs in with a **SuperGrok / xAI account** (browser OAuth on first run). To
run grok against a **local Ollama / OpenAI-compatible pool with no xAI account**,
see [Grok + the ollama pool](#grok--the-ollama-pool) below — the per-model
`api_key` is what lets it skip the browser sign-in. After installing, pick
**Grok Build** from the sidebar **Agent** submenu or set it as the default in
**Settings → Agents**.

### Migrating existing Claude projects

If you already have Claude Code project directories, move or symlink them into
`~/.ProjectMan/projects/` so ProjectMan can find them:

```bash
mkdir -p ~/.ProjectMan/projects

# Move a project
mv ~/my-project ~/.ProjectMan/projects/

# Or symlink it (leaves the original in place)
ln -s ~/my-project ~/.ProjectMan/projects/my-project
```

You can also point ProjectMan at a different directory entirely via **Settings → Projects Directory**.

### Enabling status indicators

The coloured status dots (working / waiting / done) require a hook script to be registered
with Claude Code. The script is installed to `~/.claude/projectman/hook.js` automatically —
you just need to tell Claude Code to run it.

Add the following to `~/.claude/settings.json` (create the file if it doesn't exist):

```json
{
  "hooks": {
    "PreToolUse":        [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "PostToolUse":       [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "PostToolUseFailure":[{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "PermissionRequest": [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "Notification":      [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "Stop":              [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "SessionStart":      [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}],
    "SessionEnd":        [{"hooks": [{"type": "command", "command": "node ~/.claude/projectman/hook.js"}]}]
  }
}
```

You can also edit this file from within ProjectMan via **Settings → Claude JSON**.

Status dots work without this step — they just won't update in real time until hooks are configured.

## Using opencode

ProjectMan can drive [opencode](https://opencode.ai) as a first-class agent
alongside Claude Code. Pick it per project from the sidebar right-click
**Agent** submenu, or set it as the default in **Settings → Agents**.

- **Spawn / continue / resume** map to `opencode`, `opencode -c`, and
  `opencode -s <id>`; the session-history expander lists a project's recent
  opencode sessions via `opencode session list`, run **from the project
  directory** (the command is cwd-scoped). A storage-scan fallback exists for
  old opencode builds that kept per-session JSON files on disk; current builds
  store sessions in SQLite (`opencode.db`), which the fallback does not read —
  on those versions the CLI is the only session source (SQLite support is
  planned P3 hardening).
- **Per-project model** is passed verbatim as `-m <provider>/<model>` — e.g.
  `ollama/qwen3.5:cloud`. opencode is natively multi-provider, so no
  claude-code-router is involved.
- **Status dots** require the opencode status bridge plugin. `install.sh`
  drops it into `~/.config/opencode/plugins/projectman.js` for you (idempotent),
  or install it from **Settings → Agents → Install bridge**. See
  [`bridges/opencode/README.md`](bridges/opencode/README.md).

### opencode + the ollama pool: the `opencode run` empty-render note

On some opencode builds, scripting it **headlessly** with
`opencode run -m ollama/...` against an Ollama/OpenAI-compatible endpoint
prints **empty output** with exit code 0 even though the endpoint returned a
valid answer. Verified so far: opencode **1.16.2** exhibits it while
**1.2.15** renders the same command correctly against the same endpoint and an
equivalent provider config — so it is opencode-version-dependent, not an
endpoint or config-shape problem. The endpoint was independently exonerated
(it returns the answer, plus a nonstandard `reasoning` field from the
OpenAI-compat layer). The leading hypothesis — unconfirmed — is that the
affected renderer mishandles responses carrying that `reasoning` field,
folding the answer into the thinking channel that `--thinking`
(off by default) controls.

ProjectMan itself spawns the **interactive TUI** (`opencode` / `opencode -c`),
not the headless `run` path, so PM sessions may be unaffected — that is the
first thing to determine on an affected build. If you hit the empty render in
your own `opencode run` scripting: try `--thinking`, and try a different
opencode version. A known-good ollama provider shape for
`~/.config/opencode/opencode.json` (verified working on 1.2.15 — note the
models need **no** special flags):

```jsonc
{
  "model": "ollama/qwen3.5:cloud",
  "provider": {
    "ollama": {
      "name": "Ollama",
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://<host>:11434/v1" },
      "models": { "qwen3.5:cloud": { "name": "qwen3.5:cloud" } }
    }
  }
}
```

## Using Grok Build

ProjectMan can drive [Grok Build](https://x.ai/cli) — xAI's terminal coding CLI
(binary `grok`) — as a first-class agent alongside Claude Code and opencode.
Pick it per project from the sidebar right-click **Agent** submenu, or set it as
the default in **Settings → Agents**.

- **Spawn / continue / resume** map to `grok`, `grok -c`, and `grok -r <id>`;
  the session-history expander lists a project's recent grok sessions via
  `grok sessions list`, run **from the project directory** (the command is
  cwd-scoped). Session ids are UUIDv7. `grok -c` exits cleanly when there's
  nothing to continue, so PM falls back to a fresh `grok` exactly as it does for
  Claude.
- **Per-project model** is passed verbatim as `-m <value>`. For grok the value
  is a **config key** from your `~/.grok/config.toml`, not a `provider/model`
  string — grok reaches custom endpoints through its own config, so no
  claude-code-router is involved.
- **Status dots** require the grok status bridge. `install.sh` installs it into
  `~/.grok/hooks/` for you (idempotent — a JSON hook definition plus an
  executable python3 status script; the JSON's command paths are rewritten to
  the absolute script path at install time), or install it from
  **Settings → Agents → Install bridge** (both share the same installer). See
  [`bridges/grok/README.md`](bridges/grok/README.md).
- **The `waiting` (blue) dot is inferred, with a known quirk.** Grok fires no
  hook event while its permission prompt is on screen (the wire goes silent),
  so ProjectMan infers `waiting` when a tool start goes unanswered for 5
  seconds. Accepted limitation: a long-running *approved* tool also crosses
  the 5s mark and briefly shows a false `waiting`, self-correcting the moment
  the tool completes — a false "needs you" beats a silently stalled session.

### Grok + the ollama pool

To run grok against a local Ollama / OpenAI-compatible pool with **no xAI
account**, add a model entry to `~/.grok/config.toml` — and it **must include
`api_key`**:

```toml
[model.pool-qwen]
model = "qwen3.5:9b"
base_url = "http://<host>:11434/v1"
name = "Qwen3.5 9B (Ollama pool)"
context_window = 32768
api_key = "ollama"
```

The `api_key` value can be any non-empty string (Ollama ignores it) — but it
**must be present**: without a per-model `api_key`, grok triggers its browser
OAuth sign-in flow even for a custom endpoint. With it, turns complete fully
offline of xAI and no `~/.grok/auth.json` is ever created. Then set the
per-project model to `pool-qwen` (the config **key**) in ProjectMan.

### Auto-update note

grok auto-updates by default and ships frequent point releases. ProjectMan
injects **nothing** to suppress this — it is your tool and your policy. To pin a
version (e.g. for reproducible test benches), set it in `~/.grok/config.toml`:

```toml
[cli]
auto_update = false
```

### Claude-compat hooks are disabled

grok reads `~/.claude/settings.json` hooks by default, which would make Claude's
ProjectMan hook **double-fire** on grok events and fight the grok bridge for the
status dot. So `install.sh` sets `[compat.claude] hooks = false` in
`~/.grok/config.toml` (idempotently, preserving every other key) — the grok
bridge is then the sole status writer for grok sessions. If you manage that
config by hand, keep that line in place.

## Updating

```bash
cd projectman
git pull
./install.sh
```

## Uninstalling

```bash
./install.sh --uninstall
```

This removes the installed files, launcher, and desktop entry. Your data directory
(`~/.ProjectMan/`) and hook script (`~/.claude/projectman/hook.js`) are left in place.

## Running from Source

No install required for development:

```bash
python main.py
```

```bash
python -m pytest
```

## Configuration

![Settings](images/screencap_settings.jpg)

| Path | Purpose |
|------|---------|
| `~/.ProjectMan/settings.json` | App settings |
| `~/.ProjectMan/session.json` | Session restore data |
| `~/.ProjectMan/status/` | Per-project agent status files (agent-neutral location) |
| `~/.ProjectMan/projects/` | Default projects directory |
| `~/.ProjectMan/paa-ledger.json` | PAA findings ledger |
| `~/.claude/projectman/hook.js` | Claude Code hook script for status updates |
| `~/.claude/settings.json` | Claude Code settings (hook registration) |
| `~/.config/opencode/plugins/projectman.js` | opencode status bridge plugin |
| `~/.grok/hooks/projectman.json` + `projectman-status.py` | Grok Build status bridge (hook definition + script) |
| `~/.grok/config.toml` | Grok Build config (`[compat.claude] hooks = false`; pool model + `api_key`) |

## License

MIT — see [LICENSE](LICENSE).
