# ProjectMan status bridge for Kimi Code

This bridge lights up ProjectMan's sidebar status dots for
[Kimi Code](https://moonshotai.github.io/kimi-code/en/) (Moonshot AI's terminal
coding CLI, binary `kimi`) sessions, exactly the way `hooks/hook.js` does for
Claude Code and the grok/opencode bridges do for those harnesses.

| File | Role |
|---|---|
| `projectman-status.py` | executable **python3** status writer invoked by each hook |
| `register_hooks.py` | idempotent merger of `[[hooks]]` entries into `config.toml` |
| `selftest.py` | offline state-machine + merge selftest |

Kimi does **not** have a separate hooks JSON file (unlike Grok). Hooks live as
`[[hooks]]` array-of-tables inside `~/.kimi-code/config.toml`.

## Install path

`install.sh` and Settings → Harnesses → **Install bridge** both call
`harnesses.install_harness_bridge(..., 'kimi')`, which:

1. Copies `projectman-status.py` → `~/.kimi-code/hooks/projectman-status.py`
   (executable).
2. Runs `ensure_kimi_hooks_registered()` to merge one `[[hooks]]` entry per
   watched event into `~/.kimi-code/config.toml`, with an **absolute** command
   path (`python3 /home/…/.kimi-code/hooks/projectman-status.py`).

Re-running is idempotent: ProjectMan hooks are identified by a command path
containing `projectman-status.py`, so user hooks are left alone and PM entries
are not duplicated.

## Event map

Wire names may arrive as PascalCase (config / docs) or snake_case; the bridge
accepts both.

| Event | ProjectMan state |
|---|---|
| `SessionStart` | `done` |
| `UserPromptSubmit` | `working` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | `working` |
| **`PermissionRequest`** | **`waiting`** (fires just before approval — better than grok's silence-age inference) |
| `PermissionResult` | `working` (clears waiting) |
| `Stop` / `StopFailure` / `Interrupt` | `done` |
| `SessionEnd` | status file **deleted** |
| `Notification` | **ignored** (noise) |

Stdin JSON carries `hook_event_name`, `session_id`, and `cwd` (docs-guaranteed).
Status files land under `~/.ProjectMan/status/<slug>.json` with the **same
slug_for rule** as the other bridges (M-P3.4 collision note applies).

## Selftest

```bash
python3 bridges/kimi/selftest.py
```

No network, no `kimi` binary required.
