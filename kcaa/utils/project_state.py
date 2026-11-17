"""Read-only view of the plugin's canonical project state.

The KiCad plugin writes ``<config dir>/project_state.json`` (schema v2) and
exports its path to the MCP server as ``KCAA_PROJECT_STATE_FILE``.  This
module validates and reads that record for the core package.

The plugin cannot import ``kcaa`` and the core package cannot import the
plugin, so the validation rules are implemented twice: the writer and the
plugin-side reader live in ``kicad_plugin/project_state.py``, and this
read-only twin backs :class:`kcaa.utils.path_validator.PathValidator`.  A
contract test feeds records written by the plugin module through this module
and requires identical results, including for invalid payloads.

Record shape::

    {
      "version": 2,
      "written_at": "2026-11-17T09:12:33Z",
      "project_root": "/abs/project/dir",
      "profile_file": "name.kicad_pro",
      "pcb_files": ["hardware/main.kicad_pcb"],
      "schematic_files": ["name.kicad_sch", "hardware/main_bus.kicad_sch"]
    }

Unknown extra fields are ignored.  A record is invalid when it is null, its
``version`` is not 2, a required field is missing or has the wrong type,
``project_root`` is not absolute, or ``profile_file`` is missing or outside
the root.  Invalid manifest entries are dropped individually.
"""

from __future__ import annotations

import json
import os

PROJECT_STATE_FILENAME = "project_state.json"

PROJECT_STATE_ENV = "KCAA_PROJECT_STATE_FILE"

SCHEMA_VERSION = 2


def state_path(config_dir: str) -> str:
    """Return the canonical state-file path under *config_dir*."""
    return os.path.join(config_dir, PROJECT_STATE_FILENAME)


def project_file_path(state: dict | None) -> str | None:
    """Return the absolute ``.kicad_pro`` path named by *state*, or None."""
    if state is None:
        return None
    return os.path.join(state["project_root"], state["profile_file"])


def _is_within(root: str, path: str) -> bool:
    """Return True when *path* resolves inside *root* (no ``..`` escape)."""
    root_real = os.path.realpath(os.path.abspath(root))
    path_real = os.path.realpath(os.path.abspath(path))
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        # Different drives on Windows.
        return False


def _resolve_inside(root: str, relative: str) -> str | None:
    """Resolve *relative* under *root* and return it when it is a real file."""
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        return None
    candidate = os.path.abspath(os.path.join(os.path.abspath(root), relative))
    if not _is_within(root, candidate):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _valid_manifest(root: str, value: object) -> list[str] | None:
    """Return the valid, root-contained entries of a manifest list."""
    if not isinstance(value, list):
        return None
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            continue
        if _resolve_inside(root, entry) is None:
            continue
        entries.append(entry)
    return entries


def validate_state(payload: object) -> dict | None:
    """Validate *payload* and return the canonical record, or None."""
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != SCHEMA_VERSION:
        return None
    written_at = payload.get("written_at")
    if not isinstance(written_at, str) or not written_at:
        return None
    project_root = payload.get("project_root")
    if not isinstance(project_root, str) or not project_root or not os.path.isabs(project_root):
        return None
    profile_file = payload.get("profile_file")
    if not isinstance(profile_file, str) or not profile_file:
        return None
    root = os.path.abspath(project_root)
    if _resolve_inside(root, profile_file) is None:
        return None
    pcb_files = _valid_manifest(root, payload.get("pcb_files"))
    schematic_files = _valid_manifest(root, payload.get("schematic_files"))
    if pcb_files is None or schematic_files is None:
        return None
    return {
        "version": SCHEMA_VERSION,
        "written_at": written_at,
        "project_root": root,
        "profile_file": profile_file,
        "pcb_files": pcb_files,
        "schematic_files": schematic_files,
    }


def read_state(path: str | None) -> dict | None:
    """Read and validate the state file; None for every invalid state."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    return validate_state(payload)
