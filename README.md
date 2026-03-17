# ProjectMan

![ProjectMan](ProjectMan.jpg)

A GTK4/Adwaita desktop application for managing [Claude Code](https://claude.ai/code) sessions.

## What It Does

ProjectMan displays a project sidebar on the left and an embedded VTE terminal on the right, running `claude` (or `zellij attach`) per project. Projects are directories under `~/.ProjectMan/projects/` (configurable).

## Features

- Per-project Claude Code sessions with automatic session restore
- Zellij multiplexer integration (optional)
- Status indicators showing live Claude state (working / waiting / done / idle)
- Session history with expand/collapse per project
- Project archive with search and Escape-to-close
- Ctrl+Tab to toggle between recently active projects
- Multiple color themes: Argonaut, Candyland, Phosphor, Salt Spray
- Sidebar pin/collapse with persistent position
- Terminal right-click menu (Copy, Paste, Select All, Open URL / Copy URL)
- Ctrl+click to open URLs and file paths
- Resource bar showing CPU and RAM usage

## Requirements

- Python 3.10+
- GTK 4
- libadwaita
- VTE 3.91
- `claude` CLI

Optional: `zellij` for multiplexed sessions.

## Running

```bash
python main.py
```

## Running Tests

```bash
python -m pytest
```

## Configuration

| Path | Purpose |
|------|---------|
| `~/.ProjectMan/settings.json` | App settings |
| `~/.ProjectMan/session.json` | Session restore data |
| `~/.ProjectMan/projects/` | Default projects directory |
| `~/.claude/projectman/hook.js` | Claude Code hook script for status updates |
