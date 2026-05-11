#!/usr/bin/env bash
# setup_plugin.sh — Create a .venv inside the KiCad plugin directory,
# install kicad_mcp from the local project as an editable package,
# and download the freerouting JAR for the auto-router feature.
#
# Run this script from inside the kicad_ai_assistant plugin folder:
#
#   cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
#   ./setup_plugin.sh /path/to/kicad-mcp
#
# Arguments:
#   $1  Path to the kicad-mcp project directory (required)

set -euo pipefail

PYTHON_VERSION="3.10.20"
FREEROUTING_VERSION="2.2.3"
FREEROUTING_JAR="freerouting-${FREEROUTING_VERSION}.jar"
FREEROUTING_URL="https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/${FREEROUTING_JAR}"

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <path-to-kicad-mcp-project>"
    echo "Example: $0 ~/code/kicad-mcp"
    exit 1
fi

PROJECT_DIR="$(cd "$1" && pwd)"

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
# The script lives inside the plugin directory.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PLUGIN_DIR/.venv"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    echo "ERROR: No pyproject.toml found in: $PROJECT_DIR"
    echo "Make sure you are passing the correct kicad-mcp project path."
    exit 1
fi

if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found in PATH. Install it from https://github.com/astral-sh/uv"
    exit 1
fi

if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
    echo "ERROR: curl or wget is required to download freerouting. Please install one."
    exit 1
fi

# ---------------------------------------------------------------------------
# Create / update the venv
# ---------------------------------------------------------------------------
echo "Plugin dir : $PLUGIN_DIR"
echo "Venv dir   : $VENV_DIR"
echo "Project dir: $PROJECT_DIR"
echo ""

echo "Step 1/3 — Creating virtual environment ..."
uv venv "$VENV_DIR" --python "$PYTHON_VERSION"

echo "Step 2/3 — Installing kicad_mcp (editable) from $PROJECT_DIR ..."
uv pip install -e "$PROJECT_DIR" --python "$VENV_DIR"

echo ""
echo "Step 3/3 — Downloading freerouting JAR ..."
FREEROUTING_DEST="$PLUGIN_DIR/$FREEROUTING_JAR"
if [[ -f "$FREEROUTING_DEST" ]]; then
    echo "  Already present: $FREEROUTING_DEST (skipping download)"
else
    echo "  URL: $FREEROUTING_URL"
    if command -v curl &>/dev/null; then
        curl -fL --progress-bar -o "$FREEROUTING_DEST" "$FREEROUTING_URL"
    else
        wget -q --show-progress -O "$FREEROUTING_DEST" "$FREEROUTING_URL"
    fi
    echo "  Saved to: $FREEROUTING_DEST"
fi

echo ""
echo "Done! The plugin venv is ready at:"
echo "  $VENV_DIR/bin/python"
echo ""
echo "Restart KiCad (or reload the plugin) for the changes to take effect."
