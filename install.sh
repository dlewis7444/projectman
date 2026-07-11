#!/usr/bin/env bash
# install.sh — Install ProjectMan to ~/.local

set -euo pipefail

INSTALL_DIR="$HOME/.local/share/projectman"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
HOOK_DEST="$HOME/.claude/projectman/hook.js"
OPENCODE_PLUGIN_DIR="$HOME/.config/opencode/plugins"
OPENCODE_PLUGIN_DEST="$OPENCODE_PLUGIN_DIR/projectman.js"
GROK_HOOKS_DIR="$HOME/.grok/hooks"
# (The grok bridge file destinations live in the shared manifest in harnesses.py —
# install.sh no longer duplicates them here; the unused GROK_*_DEST vars were
# removed, SC2034.)
GROK_CONFIG_TOML="$HOME/.grok/config.toml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── colours ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}warn:${NC} $*" >&2; }
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
    echo "  The grok status bridge in $GROK_HOOKS_DIR was left in place."
    echo "  The data directory ~/.ProjectMan/ was left in place."
    exit 0
fi

# ── version banner (M-UX.9 — the version-echo papercut) ─────────────────────────
# Tell the user EXACTLY what they're installing: the VERSION string from main.py
# plus the git short-hash of the tree being installed (so a "did my pull take?"
# is answerable from the install output, not guesswork).
PM_VERSION="$(python3 - "$SCRIPT_DIR/main.py" <<'PY' 2>/dev/null || true
import re, sys
try:
    with open(sys.argv[1]) as f:
        m = re.search(r"""^VERSION\s*=\s*['"]([^'"]+)['"]""", f.read(), re.M)
    print(m.group(1) if m else "")
except OSError:
    print("")
PY
)"
PM_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || true)"
PM_BANNER="ProjectMan"
[[ -n "$PM_VERSION" ]] && PM_BANNER="$PM_BANNER $PM_VERSION"
[[ -n "$PM_COMMIT" ]]  && PM_BANNER="$PM_BANNER ($PM_COMMIT)"
info "Installing $PM_BANNER"

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

# A session bus is required at runtime (GTK/Gio). Modern desktops already have
# one via the systemd user session ($XDG_RUNTIME_DIR/bus / DBUS_SESSION_BUS_ADDRESS).
# dbus-launch (from dbus-x11) is only needed on minimal/headless installs that
# lack that bus — do NOT warn just because dbus-launch is missing on a full
# Workstation (Fedora ships dbus-broker, not dbus-x11, by default).
_has_session_bus=false
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    _has_session_bus=true
elif [[ -n "${XDG_RUNTIME_DIR:-}" && -S "${XDG_RUNTIME_DIR}/bus" ]]; then
    _has_session_bus=true
elif command -v dbus-launch &>/dev/null || command -v dbus-run-session &>/dev/null; then
    # Can start a bus if needed (minimal/SSH-without-user-bus cases).
    _has_session_bus=true
fi
if [[ "$_has_session_bus" != true ]]; then
    warn "No D-Bus session bus detected — ProjectMan needs one at runtime."
    echo "  On a desktop, log into a graphical session (or start systemd --user)."
    echo "  On minimal/headless installs, install dbus-x11 (provides dbus-launch):"
    echo "  Fedora / RHEL:   sudo dnf install dbus-x11"
    echo "  Ubuntu / Debian: sudo apt install dbus-x11"
    echo "  Arch:            sudo pacman -S dbus"
fi
unset _has_session_bus

# claude is OPTIONAL (ProjectMan drives claude/opencode/grok — install whichever
# you use). Just record its presence so the hook summary below tells a coherent
# story instead of a warn-now / "registered!"-later contradiction (S1).
CLAUDE_PRESENT=true
command -v claude &>/dev/null || CLAUDE_PRESENT=false

# ── copy app files ─────────────────────────────────────────────────────────────
info "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/style.css" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/paa"    "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/themes" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/images/ProjectMan.jpg" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/icons" "$INSTALL_DIR/"
# Bundle the harness status bridges so the Settings → Harnesses "Install bridge"
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
        # SC2155: declare then assign so the date(1) exit status isn't masked.
        local backup
        backup="$CLAUDE_SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
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

# ── harness status bridges (shared manifest, F12a) ────────────────────────────────
# Both bridge installs delegate to harnesses.install_harness_bridge — the SAME
# manifest-driven machinery the Settings → Harnesses "Install bridge" button uses,
# so install.sh and the GUI can never drift on what files constitute a bridge.
# For grok that is the hook JSON (with its commands rewritten at install time
# to the absolute script path, F12b — no reliance on grok expanding `~`) PLUS
# the executable python3 status script; for opencode the single plugin file.
# Idempotent; the harness need not be installed for this to be harmless.
install_bridge_via_manifest() {
    # $1 = harness id (claude|opencode|grok). Echoes installed|already|<other>
    # on stdout for the caller; failures surface on stderr (not swallowed).
    local harness_id="$1"
    local errf
    errf="$(mktemp)"
    # shellcheck disable=SC2094
    if ! PM_SRC="$SCRIPT_DIR" PM_HARNESS="$harness_id" python3 -c '
import os, sys
sys.path.insert(0, os.environ["PM_SRC"])
import harnesses
print(harnesses.install_harness_bridge(
    os.environ["PM_SRC"], os.environ["PM_HARNESS"]))
' 2>"$errf"; then
        if [[ -s "$errf" ]]; then
            warn "Bridge install for $harness_id failed:"
            sed 's/^/    /' "$errf" >&2 || true
        else
            warn "Bridge install for $harness_id failed (no stderr)."
        fi
        rm -f "$errf"
        return 1
    fi
    # Success path: also show stderr if the helper printed warnings.
    if [[ -s "$errf" ]]; then
        sed 's/^/    /' "$errf" >&2 || true
    fi
    rm -f "$errf"
    return 0
}

OPENCODE_BRIDGE_STATUS="skipped"  # one of: installed, already, skipped, error
install_opencode_bridge() {
    local result
    if ! result=$(install_bridge_via_manifest opencode); then
        OPENCODE_BRIDGE_STATUS="error"
        return
    fi
    case "$result" in
        installed) OPENCODE_BRIDGE_STATUS="installed" ;;
        already)   OPENCODE_BRIDGE_STATUS="already" ;;
        *)         OPENCODE_BRIDGE_STATUS="skipped" ;;
    esac
}

info "Installing opencode status bridge ..."
install_opencode_bridge

# ── grok status bridge ──────────────────────────────────────────────────────────
# The bridge files land via the shared manifest above; additionally disable
# grok's claude-compat hooks so Claude's hook.js does NOT double-fire on grok
# events (F4 — our grok bridge becomes the sole status writer for grok
# sessions).
GROK_BRIDGE_STATUS="skipped"   # one of: installed, already, skipped, error
GROK_COMPAT_STATUS="skipped"   # one of: installed, already, skipped
install_grok_bridge() {
    local result
    if ! result=$(install_bridge_via_manifest grok); then
        GROK_BRIDGE_STATUS="error"
    else
        case "$result" in
            installed) GROK_BRIDGE_STATUS="installed" ;;
            already)   GROK_BRIDGE_STATUS="already" ;;
            *)         GROK_BRIDGE_STATUS="skipped" ;;
        esac
    fi

    # Idempotent TOML edit: [compat.claude] hooks = false, create-if-missing,
    # preserving every existing user key/section (delegated to the pure,
    # unit-tested merger so install.sh carries no TOML logic of its own).
    local toml_result
    if toml_result=$(python3 "$SCRIPT_DIR/bridges/grok/compat_toml.py" "$GROK_CONFIG_TOML" 2>/dev/null); then
        case "$toml_result" in
            installed) GROK_COMPAT_STATUS="installed" ;;
            already)   GROK_COMPAT_STATUS="already" ;;
            *)         GROK_COMPAT_STATUS="skipped" ;;
        esac
    else
        GROK_COMPAT_STATUS="skipped"
    fi
}

info "Installing grok status bridge ..."
install_grok_bridge

# ── done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}ProjectMan installed.${NC}"
echo ""
echo "  Launch:    projectman"
echo "  Uninstall: $SCRIPT_DIR/install.sh --uninstall"
echo ""
echo "  To update, pull the latest code and re-run this script."
echo ""
# M-UX.9 (S1): the claude-hook summary is now per-harness-aware and coherent. When
# claude isn't installed, we DON'T warn-then-claim-success — the hooks are STAGED
# in ~/.claude/settings.json and simply activate if/when claude is installed.
# Bullet each harness block so the footer is scannable (not a text wall).
echo "  Status bridges:"
echo ""
if [[ "$CLAUDE_PRESENT" == "false" ]]; then
    case "$HOOK_STATUS" in
        registered|already)
            echo "  • Claude Code not found — its status hooks are staged in"
            echo "    $CLAUDE_SETTINGS and will activate if you install claude."
            echo "    (ProjectMan also drives OpenCode and Grok; Claude is optional.)"
            ;;
        skipped|manual|*)
            echo "  • Claude Code not found, and its hooks weren't staged. If you"
            echo "    install claude later, see README.md → 'Enabling status indicators'."
            ;;
    esac
else
    case "$HOOK_STATUS" in
        registered)
            echo "  • Claude Code status hooks are registered."
            echo "    Restart any running Claude Code sessions for the change to take effect."
            ;;
        already)
            echo "  • Claude Code status hooks are already registered in"
            echo "    $CLAUDE_SETTINGS."
            ;;
        skipped|manual|*)
            echo "  • Status indicators (the coloured dots) require the hook script to be"
            echo "    registered in Claude Code. See README.md → 'Enabling status indicators'."
            ;;
    esac
fi
case "$OPENCODE_BRIDGE_STATUS" in
    installed)
        echo "  • OpenCode status bridge installed to $OPENCODE_PLUGIN_DEST."
        echo "    Restart any running OpenCode sessions for it to take effect."
        ;;
    already)
        echo "  • OpenCode status bridge already up to date at"
        echo "    $OPENCODE_PLUGIN_DEST."
        ;;
esac
case "$GROK_BRIDGE_STATUS" in
    installed)
        echo "  • Grok Build status bridge installed to $GROK_HOOKS_DIR/."
        echo "    Restart any running Grok sessions for it to take effect."
        ;;
    already)
        echo "  • Grok Build status bridge already up to date in $GROK_HOOKS_DIR/."
        ;;
esac
# M-UX.9 (S3): the compat note rewritten for someone who has never seen grok's
# config — explain WHAT the overlap is and WHY we disable it, not just the key.
case "$GROK_COMPAT_STATUS" in
    installed)
        echo "  • Grok also reads Claude-style hooks, so without this they'd BOTH"
        echo "    fire on a Grok turn. Set [compat.claude] hooks = false in"
        echo "    $GROK_CONFIG_TOML so the Grok status dot fires exactly once."
        ;;
    already)
        echo "  • Grok's Claude-hook overlap is already disabled in"
        echo "    $GROK_CONFIG_TOML ([compat.claude] hooks = false),"
        echo "    so its status dot fires once."
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
