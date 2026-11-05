#!/usr/bin/env bash
# setup_plugin.sh — Create a .venv inside the KiCad plugin directory,
# install kcaa, and download the freerouting JAR for the auto-router
# feature.
#
# Run this script from inside the kicad_ai_assistant plugin folder:
#
#   cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
#   ./setup_plugin.sh                        # kcaa from PyPI
#   ./setup_plugin.sh /path/to/kcaa.whl      # local wheel
#   ./setup_plugin.sh /path/to/kcaa-0.2.9.tar.gz
#   ./setup_plugin.sh /path/to/kcaa          # editable source install

set -euo pipefail

PYTHON_VERSION="3.13"
FREEROUTING_VERSION="2.2.3"
FREEROUTING_JAR="freerouting-${FREEROUTING_VERSION}.jar"
FREEROUTING_URL="https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/${FREEROUTING_JAR}"

usage() {
    cat <<'EOF'
Usage: ./setup_plugin.sh [-h|--help] [<wheel|sdist|source-dir>]

Creates a .venv (Python 3.13) inside the plugin directory, installs kcaa,
and downloads the freerouting JAR for the auto-router feature.

Arguments:
  (none)        Install the released kcaa==<VERSION> from PyPI.
  <path>        Install an unreleased build instead of the PyPI release:
                  *.whl       local wheel
                  *.tar.gz    local sdist (source archive)
                  directory   editable install of that source tree

Options:
  -h, --help    Show this help and exit.
  --            End of options; treat the next argument as a path.

Re-running without an argument targets the PyPI release again; if that release
has the same version as the installed local build, remove .venv to force it
back. Local builds need no VERSION file.
EOF
}

# ---------------------------------------------------------------------------
# Parse arguments and validate the optional local path.
# Nonexistent paths and unsupported file types are rejected before any uv call.
# A directory is accepted as a source tree; whether it is a Python project is
# only checked by uv itself, after the venv step.
# ---------------------------------------------------------------------------
OPTIONAL_PATH=""
POSITIONAL=""   # set when "--" was used, so a leading dash is a path
if [[ $# -gt 0 ]]; then
    case "$1" in
        -h|--help)
            if [[ $# -gt 1 ]]; then
                echo "ERROR: $1 takes no other arguments" >&2
                usage >&2
                exit 2
            fi
            usage
            exit 0
            ;;
        --)
            shift
            POSITIONAL=1
            ;;
    esac
fi

if [[ $# -gt 1 ]]; then
    echo "ERROR: too many arguments (expected at most one path)" >&2
    usage >&2
    exit 2
fi

if [[ $# -eq 1 ]]; then
    if [[ -z "$1" ]]; then
        echo "ERROR: empty path argument" >&2
        echo "Pass a wheel, sdist, or source directory, or no argument for PyPI." >&2
        exit 2
    fi
    if [[ -z "$POSITIONAL" ]]; then
        case "$1" in
            -*)
                echo "ERROR: unknown option: $1" >&2
                usage >&2
                exit 2
                ;;
        esac
    fi
    OPTIONAL_PATH="$1"
fi

SOURCE_KIND="pypi"
LOCAL_PATH=""
if [[ -n "$OPTIONAL_PATH" ]]; then
    if [[ ! -e "$OPTIONAL_PATH" ]]; then
        echo "ERROR: local path does not exist: $OPTIONAL_PATH" >&2
        echo "Expected a *.whl, a *.tar.gz, or a source directory." >&2
        exit 1
    fi
    if [[ -d "$OPTIONAL_PATH" ]]; then
        SOURCE_KIND="source directory"
    elif [[ -f "$OPTIONAL_PATH" && "$OPTIONAL_PATH" == *.whl ]]; then
        SOURCE_KIND="local wheel"
    elif [[ -f "$OPTIONAL_PATH" && "$OPTIONAL_PATH" == *.tar.gz ]]; then
        SOURCE_KIND="local sdist"
    else
        echo "ERROR: unsupported local path: $OPTIONAL_PATH" >&2
        echo "Expected a *.whl, a *.tar.gz, or a source directory." >&2
        exit 1
    fi
    parent="$(cd -- "$(dirname -- "$OPTIONAL_PATH")" && pwd)"
    # "${parent%/}" keeps a root-level artifact at "/name", not "//name".
    LOCAL_PATH="${parent%/}/$(basename -- "$OPTIONAL_PATH")"
fi

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
# The script lives inside the plugin directory.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PLUGIN_DIR/.venv"

# ---------------------------------------------------------------------------
# Determine the kcaa source.
# The VERSION file is only required for the PyPI path: a local build or
# artifact must work from a plain source checkout.
# ---------------------------------------------------------------------------
KCAA_VERSION=""
if [[ "$SOURCE_KIND" == "pypi" ]]; then
    VERSION_FILE="$PLUGIN_DIR/VERSION"
    if [[ -f "$VERSION_FILE" ]]; then
        KCAA_VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
        echo "kcaa source: PyPI kcaa==${KCAA_VERSION} (VERSION file)"
    else
        echo "ERROR: VERSION file not found at $VERSION_FILE"
        echo "The VERSION file is generated by 'make dist-plugin' from pyproject.toml."
        echo "Pass a local wheel, sdist, or source directory to install without it."
        exit 1
    fi
else
    echo "kcaa source: ${SOURCE_KIND} ${LOCAL_PATH}"
fi

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
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
echo ""

echo "Step 1/3 — Creating virtual environment ..."
# --allow-existing: reuse an existing .venv instead of aborting. Current uv
# refuses an existing target ("Use --clear to replace it"), so without this
# flag a re-run could never reinstall over an installed build.
uv venv "$VENV_DIR" --python "$PYTHON_VERSION" --allow-existing

echo "Step 2/3 — Installing kcaa (${SOURCE_KIND}) ..."
if [[ "$SOURCE_KIND" == "pypi" ]]; then
    uv pip install "kcaa==${KCAA_VERSION}" --python "$VENV_DIR"
elif [[ "$SOURCE_KIND" == "source directory" ]]; then
    # --reinstall-package kcaa: an installed equal version must be replaced,
    # without uninstalling and reinstalling every other package in the venv.
    uv pip install -e "$LOCAL_PATH" --reinstall-package kcaa --python "$VENV_DIR"
else
    # --reinstall-package kcaa: a plain install can no-op on an equal version.
    uv pip install "$LOCAL_PATH" --reinstall-package kcaa --python "$VENV_DIR"
fi
uv pip install "pymupdf" --python "$VENV_DIR"

echo "Installed kcaa:"
# Metadata-only probe: importing kcaa requires a KiCad environment
# (KICAD_VERSION or KICAD<N>_*), so report the installed distribution instead.
# Non-fatal: a failed lookup only warns, the install itself already succeeded.
if kcaa_info="$(
    cd "$VENV_DIR" && "$VENV_DIR/bin/python" -c '
import json
from importlib.metadata import distribution
from urllib.parse import unquote, urlparse

dist = distribution("kcaa")
info = "{} at {}".format(dist.version, dist.locate_file(""))
raw = dist.read_text("direct_url.json")
try:
    direct = json.loads(raw) if raw else {}
except ValueError:
    direct = {}
if direct.get("dir_info", {}).get("editable") and direct.get("url"):
    info += " (editable source: {})".format(unquote(urlparse(direct["url"]).path))
print(info)
' 2>&1
)"; then
    echo "  $kcaa_info"
else
    echo "  WARNING: could not read kcaa metadata from $VENV_DIR:" >&2
    printf '%s\n' "$kcaa_info" | sed 's/^/    /' >&2
fi

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
