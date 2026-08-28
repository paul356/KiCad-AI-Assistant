#!/bin/bash
# install_macos.sh — One-step installer for the KiCad AI Assistant plugin on macOS.
#
# Usage:
#   ./install_macos.sh
#
# This script:
#   1. Detects the installed KiCad version from /Applications/KiCad/KiCad.app.
#   2. Copies this plugin folder into the correct KiCad scripting/plugins directory:
#        ~/Library/Preferences/kicad/<version>/scripting/plugins/kicad_ai_assistant
#   3. Runs setup_plugin_macos.command to create the .venv and download FreeRouting.
#
# The script can be run from the repository tree or from an extracted plugin zip.
#
# Environment:
#   DRY_RUN=1    Print what would be done without copying files or creating the venv.

set -euo pipefail

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
_info() {
    echo "ℹ️  $1"
}

_error() {
    echo "❌ $1" >&2
    exit 1
}

_warn() {
    echo "⚠️  $1"
}

# ---------------------------------------------------------------------------
# Detect KiCad already running
# ---------------------------------------------------------------------------
_kicad_is_running() {
    pgrep -i "kicad" >/dev/null 2>&1 || pgrep -xi "KiCad" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Auto-enable KiCad API server in kicad_common.json
# ---------------------------------------------------------------------------
_enable_kicad_api() {
    local common_json
    common_json="$HOME/Library/Preferences/kicad/$KICAD_VERSION/kicad_common.json"

    if [ ! -f "$common_json" ]; then
        _warn "KiCad preferences file not found at $common_json. Skipping API auto-enable."
        return 0
    fi

    # Find a Python interpreter. Prefer system python3, fall back to KiCad's.
    local python_cmd="python3"
    if ! command -v python3 >/dev/null 2>&1; then
        python_cmd="/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        if [ ! -x "$python_cmd" ]; then
            _warn "Could not find a Python interpreter to edit KiCad preferences. Please enable the KiCad API manually."
            return 0
        fi
    fi

    local current_state
    current_state="$($python_cmd - <<PY 2>/dev/null
import json
with open('$common_json', 'r') as f:
    data = json.load(f)
print(data.get('api', {}).get('enable_server', False))
PY
    )" || current_state="False"

    if [ "$current_state" = "True" ] || [ "$current_state" = "true" ]; then
        _info "KiCad API server is already enabled."
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        _info "Would enable KiCad API server in $common_json"
        return 0
    fi

    _info "Enabling KiCad API server so the plugin can control the board ..."
    cp "$common_json" "$common_json.kcaa-backup"
    $python_cmd - <<PY
import json
path = '$common_json'
with open(path, 'r') as f:
    data = json.load(f)
if 'api' not in data:
    data['api'] = {}
data['api']['enable_server'] = True
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
PY
    _info "KiCad API server enabled. A backup was saved to $common_json.kcaa-backup"
}

# ---------------------------------------------------------------------------
# Dry-run support
# ---------------------------------------------------------------------------
DRY_RUN="${DRY_RUN:-0}"
if [ "$DRY_RUN" = "1" ]; then
    _info "DRY RUN mode — no changes will be made."
fi

# ---------------------------------------------------------------------------
# Detect KiCad version
# ---------------------------------------------------------------------------
KICAD_APP="/Applications/KiCad/KiCad.app"
KICAD_VERSION="${KICAD_VERSION:-}"

if [ -z "$KICAD_VERSION" ] && [ -d "$KICAD_APP" ]; then
    # Try to read the version from the Info.plist CFBundleShortVersionString.
    PLIST="$KICAD_APP/Contents/Info.plist"
    if [ -f "$PLIST" ]; then
        BUNDLE_VERSION="$(defaults read "$PLIST" CFBundleShortVersionString 2>/dev/null || true)"
        if [ -n "$BUNDLE_VERSION" ]; then
            # KiCad 10.0.2 → 10.0
            KICAD_VERSION="$(echo "$BUNDLE_VERSION" | sed -nE 's/^([0-9]+\.[0-9]+).*/\1/p')"
        fi
    fi
fi

if [ -z "$KICAD_VERSION" ]; then
    # Last resort: look at the existing KiCad config directory names.
    KICAD_PREFS_DIR="$HOME/Library/Preferences/kicad"
    if [ -d "$KICAD_PREFS_DIR" ]; then
        KICAD_VERSION="$(find "$KICAD_PREFS_DIR" -maxdepth 1 -type d -name '[0-9]*.[0-9]*' -exec basename {} \; 2>/dev/null | sort -V | tail -n1 || true)"
    fi
fi

if [ -z "$KICAD_VERSION" ]; then
    _error "Could not detect KiCad version. Please pass it explicitly: KICAD_VERSION=10.0 ./install_macos.sh"
fi

_info "Detected KiCad version: $KICAD_VERSION"

# ---------------------------------------------------------------------------
# Locate source plugin directory
# ---------------------------------------------------------------------------
# The script is expected to live inside kicad_plugin/ (repo layout) or inside
# the already-copied plugin directory. In either case, copy from the directory
# containing this script.
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DST_DIR="$HOME/Library/Preferences/kicad/$KICAD_VERSION/scripting/plugins/kicad_ai_assistant"

if [ "$SRC_DIR" = "$DST_DIR" ]; then
    _info "Already running from the installed plugin directory; skipping copy."
else
    if [ "$DRY_RUN" = "1" ]; then
        _info "Would install plugin to: $DST_DIR"
    else
        _info "Installing plugin to: $DST_DIR"
        rm -rf "$DST_DIR"
        mkdir -p "$(dirname "$DST_DIR")"
        cp -R "$SRC_DIR" "$DST_DIR"
    fi
fi

# Ensure a VERSION file exists in the destination. When running from the repo,
# the plugin dir does not contain a VERSION file (it is generated by make
# dist-plugin). Generate it from pyproject.toml so the setup script can read it.
if [ "$DRY_RUN" != "1" ] && [ ! -f "$DST_DIR/VERSION" ]; then
    PYPROJECT_CANDIDATE=""
    if [ -f "$SRC_DIR/pyproject.toml" ]; then
        PYPROJECT_CANDIDATE="$SRC_DIR/pyproject.toml"
    elif [ -f "$(dirname "$SRC_DIR")/pyproject.toml" ]; then
        PYPROJECT_CANDIDATE="$(dirname "$SRC_DIR")/pyproject.toml"
    fi
    if [ -n "$PYPROJECT_CANDIDATE" ]; then
        VERSION="$(grep -m1 '^version = ' "$PYPROJECT_CANDIDATE" | sed 's/version = "\(.*\)"/\1/')"
        if [ -n "$VERSION" ]; then
            echo "$VERSION" > "$DST_DIR/VERSION"
            _info "Generated VERSION file from $PYPROJECT_CANDIDATE"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Run the macOS setup script
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
    SETUP_SCRIPT="$SRC_DIR/setup_plugin_macos.command"
else
    SETUP_SCRIPT="$DST_DIR/setup_plugin_macos.command"
fi
if [ ! -f "$SETUP_SCRIPT" ]; then
    _error "Setup script not found at $SETUP_SCRIPT. The plugin copy may be incomplete."
fi

# Run setup with the detected version exported so setup_plugin_macos.command
# can find it even if the path-based detection fails.
export KICAD_VERSION
if [ "$DRY_RUN" = "1" ]; then
    _info "Would run setup script: $SETUP_SCRIPT"
    DRY_RUN=1 "$SETUP_SCRIPT"
else
    _info "Running setup script ..."
    chmod +x "$SETUP_SCRIPT"
    "$SETUP_SCRIPT"
fi

_info ""
_enable_kicad_api

_info ""
if [ "$DRY_RUN" = "1" ]; then
    _info "Dry run complete — no changes were made."
else
    _info "Installation complete."
    _info ""
    _info "Next steps:"
    _info "  1. If KiCad is currently open, quit it now."
    _info "  2. Reopen KiCad PCB Editor."
    _info "  3. The KiCad AI Assistant toolbar button should appear automatically."
    _info "     (If not, choose Tools → External Plugins → Refresh Plugins.)"
    if _kicad_is_running; then
        _warn "KiCad appears to be running. Please quit and reopen KiCad for the changes to take effect."
    fi
fi
