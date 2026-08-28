#!/bin/bash
# setup_plugin_macos.command — Double-clickable macOS setup for the KiCad AI Assistant plugin.
#
# This script does the same work as setup_plugin.sh but is packaged as a .command
# file so macOS users can run it by double-clicking it in Finder. It can also be
# invoked from the Terminal.
#
# It creates a .venv inside the plugin directory, installs kcaa from PyPI, and
# downloads the FreeRouting JAR.
#
# Environment:
#   DRY_RUN=1    Print what would be done without creating the venv or downloading.

set -euo pipefail

# ---------------------------------------------------------------------------
# macOS-friendly UI helpers
# ---------------------------------------------------------------------------
_zenity() {
    # Fall back to osascript for native macOS alerts when stdout is not a tty.
    # In CI/headless environments, skip osascript to avoid hangs.
    if [ "${KCAA_HEADLESS:-0}" = "1" ] || [ "${CI:-}" = "true" ]; then
        echo "$2"
        return
    fi
    if [ -t 1 ]; then
        echo "$2"
    else
        osascript -e "display dialog \"$2\" buttons {\"OK\"} default button \"OK\" with icon $1 with title \"KiCad AI Assistant\"" &>/dev/null || true
    fi
}

_info() {
    echo "ℹ️  $1"
}

_error_dialog() {
    echo "❌ $1" >&2
    _zenity "stop" "$1"
}

_success_dialog() {
    _zenity "note" "$1"
}

# ---------------------------------------------------------------------------
# Dry-run support
# ---------------------------------------------------------------------------
DRY_RUN="${DRY_RUN:-0}"
if [ "$DRY_RUN" = "1" ]; then
    _info "DRY RUN mode — no changes will be made."
fi

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
# The .command file lives inside the plugin directory.
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PLUGIN_DIR/.venv"

# ---------------------------------------------------------------------------
# KiCad version detection
# ---------------------------------------------------------------------------
KICAD_VERSION="${KICAD_VERSION:-}"
if [ -z "$KICAD_VERSION" ]; then
    # Try to detect from the plugin install path (e.g. .../kicad/10.0/scripting/plugins/...)
    KICAD_VERSION="$(echo "$PLUGIN_DIR" | sed -nE 's|.*/kicad/([0-9]+\.[0-9]+)/scripting/plugins.*|\1|p')"
fi

if [ -z "$KICAD_VERSION" ]; then
    # Try KICAD{N}_* environment variables (e.g. KICAD10_SYMBOL_DIR → "10.0")
    for key in $(compgen -e | grep '^KICAD' | grep '_'); do
        major="${key#KICAD}"
        major="${major%%_*}"
        if [[ "$major" =~ ^[0-9]+$ ]]; then
            KICAD_VERSION="${major}.0"
            break
        fi
    done
fi

if [ -z "$KICAD_VERSION" ]; then
    _info "Could not detect KiCad version from path or environment; defaulting to 10.0"
    KICAD_VERSION="10.0"
fi

_info "Detected KiCad version: $KICAD_VERSION"

# ---------------------------------------------------------------------------
# Read kcaa version from VERSION file or pyproject.toml
# ---------------------------------------------------------------------------
VERSION_FILE="$PLUGIN_DIR/VERSION"
PYPROJECT_FILE="$PLUGIN_DIR/pyproject.toml"
PYPROJECT_FILE_PARENT="$(dirname "$PLUGIN_DIR")/pyproject.toml"
if [[ -f "$VERSION_FILE" ]]; then
    KCAA_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
    _info "kcaa version: $KCAA_VERSION (from VERSION file)"
elif [[ -f "$PYPROJECT_FILE" ]]; then
    KCAA_VERSION="$(grep -m1 '^version = ' "$PYPROJECT_FILE" | sed 's/version = "\(.*\)"/\1/')"
    if [[ -z "$KCAA_VERSION" ]]; then
        _error_dialog "Could not extract version from $PYPROJECT_FILE"
        exit 1
    fi
    _info "kcaa version: $KCAA_VERSION (from pyproject.toml)"
elif [[ -f "$PYPROJECT_FILE_PARENT" ]]; then
    KCAA_VERSION="$(grep -m1 '^version = ' "$PYPROJECT_FILE_PARENT" | sed 's/version = "\(.*\)"/\1/')"
    if [[ -z "$KCAA_VERSION" ]]; then
        _error_dialog "Could not extract version from $PYPROJECT_FILE_PARENT"
        exit 1
    fi
    _info "kcaa version: $KCAA_VERSION (from pyproject.toml)"
else
    _error_dialog "VERSION file not found at $VERSION_FILE and pyproject.toml not found at $PYPROJECT_FILE or $PYPROJECT_FILE_PARENT. Run 'make dist-plugin' or place a pyproject.toml next to the setup script."
    exit 1
fi

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PYTHON_VERSION="3.13"
FREEROUTING_VERSION="2.2.3"
FREEROUTING_JAR="freerouting-${FREEROUTING_VERSION}.jar"
FREEROUTING_URL="https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/${FREEROUTING_JAR}"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if ! command -v uv &>/dev/null; then
    _error_dialog "uv not found in PATH. Install it from https://github.com/astral-sh/uv (e.g. curl -LsSf https://astral.sh/uv/install.sh | sh), then re-run this setup."
    exit 1
fi

if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
    _error_dialog "curl or wget is required to download FreeRouting. Please install one."
    exit 1
fi

# Warn if KiCad is not in the expected location, but do not fail — the user may
# have a non-standard install.
if [ ! -d "/Applications/KiCad/KiCad.app" ]; then
    _info "KiCad.app was not found at /Applications/KiCad/KiCad.app. If KiCad is installed elsewhere, ensure the plugin is copied to the correct scripting/plugins folder for your version."
fi

# ---------------------------------------------------------------------------
# Create / update the venv
# ---------------------------------------------------------------------------
_info "Plugin dir : $PLUGIN_DIR"
_info "Venv dir   : $VENV_DIR"
_info ""

if [ "$DRY_RUN" = "1" ]; then
    _info "Step 1/3 — Would create virtual environment: $VENV_DIR (python $PYTHON_VERSION)"
    _info "Step 2/3 — Would install kcaa==$KCAA_VERSION and pymupdf"
else
    _info "Step 1/3 — Creating virtual environment ..."
    uv venv "$VENV_DIR" --python "$PYTHON_VERSION"

    _info "Step 2/3 — Installing kcaa from PyPI ..."
    uv pip install "kcaa==${KCAA_VERSION}" --python "$VENV_DIR"
    uv pip install "pymupdf" --python "$VENV_DIR"
fi

# ---------------------------------------------------------------------------
# Download FreeRouting JAR
# ---------------------------------------------------------------------------
_info ""
if [ "$DRY_RUN" = "1" ]; then
    _info "Step 3/3 — Would download FreeRouting JAR to $PLUGIN_DIR/$FREEROUTING_JAR"
else
    _info "Step 3/3 — Downloading FreeRouting JAR ..."
    FREEROUTING_DEST="$PLUGIN_DIR/$FREEROUTING_JAR"
    if [[ -f "$FREEROUTING_DEST" ]]; then
        _info "  Already present: $FREEROUTING_DEST (skipping download)"
    else
        _info "  URL: $FREEROUTING_URL"
        if command -v curl &>/dev/null; then
            curl -fL --progress-bar -o "$FREEROUTING_DEST" "$FREEROUTING_URL"
        else
            wget -q --show-progress -O "$FREEROUTING_DEST" "$FREEROUTING_URL"
        fi
        _info "  Saved to: $FREEROUTING_DEST"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
_info ""
if [ "$DRY_RUN" = "1" ]; then
    _info "Dry run complete — no changes were made."
else
    _info "Done! The plugin venv is ready at:"
    _info "  $VENV_DIR/bin/python"
    _info ""
    _info "Restart KiCad (or reload plugins) for the changes to take effect."
    _success_dialog "Setup complete. Restart KiCad and choose Tools → External Plugins → KiCad AI Assistant."
fi
