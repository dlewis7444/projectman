#!/usr/bin/env bash
# install.sh — Install ProjectMan to ~/.local

set -euo pipefail

INSTALL_DIR="$HOME/.local/share/projectman"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
HOOK_DEST="$HOME/.claude/projectman/hook.js"
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
    command -v update-desktop-database &>/dev/null && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo ""
    echo "  Uninstalled."
    echo "  The hook script at $HOOK_DEST was left in place."
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

if ! command -v claude &>/dev/null; then
    warn "'claude' CLI not found in PATH. Install it before running ProjectMan."
fi

# ── copy app files ─────────────────────────────────────────────────────────────
info "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/style.css" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/themes" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/ProjectMan.jpg" "$INSTALL_DIR/"

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
cat > "$DESKTOP_DIR/projectman.desktop" <<EOF
[Desktop Entry]
Name=ProjectMan
Comment=Manage Claude Code sessions
Exec=$BIN_DIR/projectman
Icon=$INSTALL_DIR/ProjectMan.jpg
Type=Application
Categories=Development;
Terminal=false
StartupNotify=true
EOF
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ── hook script ────────────────────────────────────────────────────────────────
info "Installing hook script to $HOOK_DEST ..."
mkdir -p "$(dirname "$HOOK_DEST")"
cp "$SCRIPT_DIR/hooks/hook.js" "$HOOK_DEST"

# ── done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}ProjectMan installed.${NC}"
echo ""
echo "  Launch:    projectman"
echo "  Uninstall: $SCRIPT_DIR/install.sh --uninstall"
echo ""
echo "  To update, pull the latest code and re-run this script."
echo ""
echo "  Status indicators (the coloured dots) require the hook script to be"
echo "  registered in Claude Code. See README.md → 'Enabling Status Indicators'."
echo ""

# warn if ~/.local/bin is not on PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH."
    echo "  Add this line to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi
