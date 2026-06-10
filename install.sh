#!/usr/bin/env bash
# install.sh — Install ProjectMan to ~/.local

set -euo pipefail

INSTALL_DIR="$HOME/.local/share/projectman"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
HOOK_DEST="$HOME/.claude/projectman/hook.js"
OPENCODE_PLUGIN_DIR="$HOME/.config/opencode/plugins"
OPENCODE_PLUGIN_DEST="$OPENCODE_PLUGIN_DIR/projectman.js"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── colours ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}warn:${NC} $*"; }
error() { echo -e "${RED}error:${NC} $*" >&2; }

# ── uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    info "Uninstalling ProjectMan..."
    rm -rf  "$INSTALL_DIR"
    rm -f   "$BIN_DIR/projectman"
    rm -f   "$DESKTOP_DIR/projectman.desktop"
    rm -f   "$DESKTOP_DIR/io.github.projectman.desktop"
    command -v update-desktop-database &>/dev/null && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo ""
    echo "  Uninstalled."
    echo "  The hook script at $HOOK_DEST was left in place."
    echo "  The opencode status bridge at $OPENCODE_PLUGIN_DEST was left in place."
    echo "  The data directory ~/.ProjectMan/ was left in place."
    exit 0
fi

# ── dependency checks ──────────────────────────────────────────────────────────
check_import() { python3 -c "$1" 2>/dev/null; }

if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install Python 3.10+ and try again."
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 10) ]]; then
    error "Python 3.10+ required (found $PY_VER)."
    exit 1
fi

MISSING=()
check_import "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk"   || MISSING+=("GTK 4")
check_import "import gi; gi.require_version('Adw','1');  from gi.repository import Adw"   || MISSING+=("libadwaita")
check_import "import gi; gi.require_version('Vte','3.91'); from gi.repository import Vte" || MISSING+=("VTE 3.91")

if [[ ${#MISSING[@]} -gt 0 ]]; then
    error "Missing system dependencies: ${MISSING[*]}"
    echo ""
    echo "  Fedora / RHEL:   sudo dnf install python3-gobject gtk4 libadwaita vte291-gtk4"
    echo "  Ubuntu / Debian: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91"
    echo "  Arch:            sudo pacman -S python-gobject gtk4 libadwaita vte3"
    echo ""
    exit 1
fi

if ! command -v node &>/dev/null; then
    error "Node.js not found. Install Node.js and try again."
    echo ""
    echo "  Fedora / RHEL:   sudo dnf install nodejs"
    echo "  Ubuntu / Debian: sudo apt install nodejs"
    echo "  Arch:            sudo pacman -S nodejs"
    echo "  Or via nvm:      https://github.com/nvm-sh/nvm"
    echo ""
    exit 1
fi

if ! command -v claude &>/dev/null; then
    warn "'claude' CLI not found in PATH. Install it before running ProjectMan."
fi

# ── copy app files ─────────────────────────────────────────────────────────────
info "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/style.css" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/paa"    "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/themes" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/images/ProjectMan.jpg" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/icons" "$INSTALL_DIR/"
# Bundle the agent status bridges so the Settings → Agents "Install bridge"
# button can find them in the installed tree (install.sh installs them too).
cp -r "$SCRIPT_DIR/bridges" "$INSTALL_DIR/"

# ── wrapper script ─────────────────────────────────────────────────────────────
info "Creating $BIN_DIR/projectman ..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/projectman" <<EOF
#!/bin/sh
exec python3 "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/projectman"

# ── desktop entry ──────────────────────────────────────────────────────────────
info "Installing desktop entry ..."
mkdir -p "$DESKTOP_DIR"
# Remove legacy desktop file from older installs (renamed to io.github.projectman)
rm -f "$DESKTOP_DIR/projectman.desktop"
cat > "$DESKTOP_DIR/io.github.projectman.desktop" <<EOF
[Desktop Entry]
Name=ProjectMan
Comment=Manage Claude Code sessions
Exec=$BIN_DIR/projectman
Icon=io.github.projectman
Type=Application
Categories=Development;
Terminal=false
StartupNotify=true
StartupWMClass=io.github.projectman
EOF
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ── install icon to hicolor theme ──────────────────────────────────────────────
info "Installing icon ..."
mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$INSTALL_DIR/icons/scalable/apps/io.github.projectman.svg" \
   "$HOME/.local/share/icons/hicolor/scalable/apps/"
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# ── hook script ────────────────────────────────────────────────────────────────
info "Installing hook script to $HOOK_DEST ..."
mkdir -p "$(dirname "$HOOK_DEST")"
cp "$SCRIPT_DIR/hooks/hook.js" "$HOOK_DEST"

# ── register hooks in ~/.claude/settings.json ──────────────────────────────────
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HOOK_CMD="node ~/.claude/projectman/hook.js"
HOOK_STATUS="manual"  # one of: registered, already, manual, skipped

register_claude_hooks() {
    if ! command -v jq &>/dev/null; then
        warn "jq not found — cannot auto-register hooks in $CLAUDE_SETTINGS."
        echo "  Install jq (dnf/apt/pacman install jq), or register manually."
        echo "  See README.md → 'Enabling status indicators'."
        HOOK_STATUS="skipped"
        return 0
    fi

    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
    local existed=true
    if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
        existed=false
        echo '{}' > "$CLAUDE_SETTINGS"
    fi

    if ! jq empty "$CLAUDE_SETTINGS" 2>/dev/null; then
        warn "$CLAUDE_SETTINGS is not valid JSON — leaving it untouched."
        echo "  Fix the file, then re-run install.sh, or register hooks manually."
        HOOK_STATUS="skipped"
        return 0
    fi

    local jq_program='
        def add_hook($ev; $cmd):
            .hooks //= {} |
            .hooks[$ev] //= [] |
            if any(.hooks[$ev][]?.hooks[]?.command // empty; test("projectman/hook\\.js"))
            then .
            else .hooks[$ev] += [{hooks: [{type: "command", command: $cmd}]}] end;
        add_hook("PreToolUse"; $cmd)
        | add_hook("PostToolUse"; $cmd)
        | add_hook("PostToolUseFailure"; $cmd)
        | add_hook("UserPromptSubmit"; $cmd)
        | add_hook("PermissionRequest"; $cmd)
        | add_hook("Notification"; $cmd)
        | add_hook("Stop"; $cmd)
        | add_hook("SessionStart"; $cmd)
        | add_hook("SessionEnd"; $cmd)
    '

    local tmp; tmp=$(mktemp)
    if ! jq --arg cmd "$HOOK_CMD" "$jq_program" "$CLAUDE_SETTINGS" > "$tmp"; then
        rm -f "$tmp"
        warn "Failed to compute updated $CLAUDE_SETTINGS — left unchanged."
        HOOK_STATUS="skipped"
        return 0
    fi

    if cmp -s "$CLAUDE_SETTINGS" "$tmp"; then
        rm -f "$tmp"
        if [[ "$existed" == "true" ]]; then
            HOOK_STATUS="already"
        else
            # File didn't exist; we wrote {} but added no hooks (shouldn't happen) — clean up
            HOOK_STATUS="registered"
        fi
        return 0
    fi

    if [[ "$existed" == "true" ]]; then
        local backup="$CLAUDE_SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
        cp "$CLAUDE_SETTINGS" "$backup"
        mv "$tmp" "$CLAUDE_SETTINGS"
        info "Registered ProjectMan hooks in $CLAUDE_SETTINGS (backup: $backup)."
    else
        mv "$tmp" "$CLAUDE_SETTINGS"
        info "Created $CLAUDE_SETTINGS with ProjectMan hooks."
    fi
    HOOK_STATUS="registered"
}

info "Checking Claude Code hook registration ..."
register_claude_hooks

# ── opencode status bridge ──────────────────────────────────────────────────────
# Drop the opencode status-bridge plugin into ~/.config/opencode/plugins/ so
# opencode sessions light up the sidebar dots (the opencode half of the status
# contract). Idempotent: only copies when missing or changed, mirroring the
# hook-install pattern. opencode need not be installed for this to be harmless.
OPENCODE_BRIDGE_STATUS="skipped"  # one of: installed, already, skipped
install_opencode_bridge() {
    local src="$SCRIPT_DIR/bridges/opencode/projectman.js"
    if [[ ! -f "$src" ]]; then
        return 0  # nothing to install (shouldn't happen in a full checkout)
    fi
    mkdir -p "$OPENCODE_PLUGIN_DIR"
    if [[ -f "$OPENCODE_PLUGIN_DEST" ]] && cmp -s "$src" "$OPENCODE_PLUGIN_DEST"; then
        OPENCODE_BRIDGE_STATUS="already"
        return 0
    fi
    cp "$src" "$OPENCODE_PLUGIN_DEST"
    OPENCODE_BRIDGE_STATUS="installed"
}

info "Installing opencode status bridge ..."
install_opencode_bridge

# ── done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}ProjectMan installed.${NC}"
echo ""
echo "  Launch:    projectman"
echo "  Uninstall: $SCRIPT_DIR/install.sh --uninstall"
echo ""
echo "  To update, pull the latest code and re-run this script."
echo ""
case "$HOOK_STATUS" in
    registered)
        echo "  Status indicator hooks are registered. Restart any running Claude"
        echo "  Code sessions for the changes to take effect."
        ;;
    already)
        echo "  Status indicator hooks are already registered in $CLAUDE_SETTINGS."
        ;;
    skipped|manual|*)
        echo "  Status indicators (the coloured dots) require the hook script to be"
        echo "  registered in Claude Code. See README.md → 'Enabling status indicators'."
        ;;
esac
case "$OPENCODE_BRIDGE_STATUS" in
    installed)
        echo "  opencode status bridge installed to $OPENCODE_PLUGIN_DEST."
        echo "  Restart any running opencode sessions for it to take effect."
        ;;
    already)
        echo "  opencode status bridge already up to date at $OPENCODE_PLUGIN_DEST."
        ;;
esac
echo ""

# warn if ~/.local/bin is not on PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH."
    echo "  Add this line to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi
