"""The plugin's vendored resolver must stay byte-identical to the core module."""

import filecmp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE = REPO_ROOT / "kcaa" / "utils" / "project_resolution.py"
VENDORED = REPO_ROOT / "kicad_plugin" / "project_resolution.py"


def test_vendored_project_resolution_matches_core():
    assert VENDORED.is_file(), (
        "kicad_plugin/project_resolution.py is missing; "
        "run python scripts/sync_project_resolution.py"
    )
    assert filecmp.cmp(CORE, VENDORED, shallow=False), (
        "kicad_plugin/project_resolution.py is out of date; "
        "run python scripts/sync_project_resolution.py"
    )
