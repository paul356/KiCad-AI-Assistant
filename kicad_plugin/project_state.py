"""The canonical project state shared with the MCP server (plugin side).

The panel probes the open project, scans its directory tree, and writes one
small JSON record to ``<config dir>/project_state.json``.  Everything that
needs to know "which project is open" reads that record:

* the panel (session restore, session stamps, the context block),
* the plugin's LLM client (pre-edit snapshots and project-relative paths),
* the MCP server's ``PathValidator`` (its project trust root), which uses the
  read-only twin in ``kcaa/utils/project_state.py``.

The record is::

    {
      "version": 2,
      "written_at": "2026-11-17T09:12:33Z",
      "project_root": "/abs/project/dir",
      "profile_file": "name.kicad_pro",
      "pcb_files": ["hardware/main.kicad_pcb"],
      "schematic_files": ["name.kicad_sch", "hardware/main_bus.kicad_sch"]
    }

``project_root`` is absolute; every other path is relative to it and uses
``/`` separators.  The writer never emits ``null`` or an empty string: when
no valid record can be assembled, nothing is written and the previous file is
left untouched.

This module is stdlib-only: it must load both under KiCad's embedded Python
and under the plain interpreter used by the unit tests.  The ``kcaa`` package
is never importable from here (the plugin ships without it), so the core
package has its own read-only copy of the validation rules.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import time

# File under the plugin data directory.  The same name is used by the core
# package and by ``ServerManager`` when it exports KCAA_PROJECT_STATE_FILE to
# the MCP server subprocess.
PROJECT_STATE_FILENAME = "project_state.json"

# Set by the plugin launcher for the MCP server subprocess.
PROJECT_STATE_ENV = "KCAA_PROJECT_STATE_FILE"

SCHEMA_VERSION = 2

# Windows may hold the file open for the instant it is read; ``os.replace``
# then fails transiently with PermissionError.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_S = 0.05

# Directories never scanned for project files: KiCad's own version snapshots,
# ``<name>-backups`` trees, hidden directories, and symlinked directories
# (walking them can leave the project root or loop).
_BACKUP_DIR_SUFFIX = "-backups"


def state_path(config_dir: str) -> str:
    """Return the canonical state-file path under *config_dir*."""
    return os.path.join(config_dir, PROJECT_STATE_FILENAME)


def state_file_from_env() -> str | None:
    """Return the state-file path exported to the server, if any."""
    return os.environ.get(PROJECT_STATE_ENV) or None


def project_file_path(state: dict | None) -> str | None:
    """Return the absolute ``.kicad_pro`` path named by *state*, or None.

    Both sides build this key from ``(project_root, profile_file)``; nothing
    reconstructs the path from a file stem.
    """
    if state is None:
        return None
    return os.path.join(state["project_root"], state["profile_file"])


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_within(root: str, path: str) -> bool:
    """Return True when *path* resolves inside *root* (no ``..`` escape)."""
    root_real = os.path.realpath(os.path.abspath(root))
    path_real = os.path.realpath(os.path.abspath(path))
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        # Different drives on Windows.
        return False


def _resolve_inside(root: str, relative: str) -> str | None:
    """Resolve *relative* under *root* and return it when it is a real file.

    Absolute values and ``..`` escapes are refused; a missing file or an
    entry that resolves outside the root returns None.
    """
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        return None
    candidate = os.path.abspath(os.path.join(os.path.abspath(root), relative))
    if not is_within(root, candidate):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def scan_project_files(project_root: str) -> tuple[list[str], list[str]]:
    """Return ``(pcb_files, schematic_files)`` relative to *project_root*.

    The walk is recursive, extension matching is case-insensitive, and the
    result is de-duplicated and stably sorted so an unchanged project always
    produces the same record.  ``.versions`` (hidden), ``<name>-backups``,
    hidden directories and symlinked directories are skipped.
    """
    root = os.path.abspath(project_root)
    pcb_files: list[str] = []
    schematic_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and not name.endswith(_BACKUP_DIR_SUFFIX)
            and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in filenames:
            lower = name.lower()
            if not (lower.endswith(".kicad_pcb") or lower.endswith(".kicad_sch")):
                continue
            relative = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if lower.endswith(".kicad_pcb"):
                pcb_files.append(relative)
            else:
                schematic_files.append(relative)
    return sorted(set(pcb_files)), sorted(set(schematic_files))


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
    """Validate *payload* and return the canonical record, or None.

    Unknown extra fields are ignored.  An otherwise valid record keeps only
    the manifest entries that still exist inside ``project_root``; a bad
    entry drops that entry, never the whole record.
    """
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


def build_record(project_file: str, written_at: str | None = None) -> dict | None:
    """Build the canonical record for the open ``.kicad_pro``, or None.

    Returns None when *project_file* is not a real file (nothing to publish).
    """
    project_file = os.path.abspath(project_file)
    if not os.path.isfile(project_file):
        return None
    root = os.path.dirname(project_file)
    pcb_files, schematic_files = scan_project_files(root)
    return validate_state(
        {
            "version": SCHEMA_VERSION,
            "written_at": written_at or _utc_now(),
            "project_root": root,
            "profile_file": os.path.basename(project_file),
            "pcb_files": pcb_files,
            "schematic_files": schematic_files,
        }
    )


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


def read_state_hint(path: str | None) -> tuple[str, str | None] | None:
    """Return ``(project_root, absolute profile)`` even when the profile is bad.

    Used only by the pre-edit resolver: a stale ``profile_file`` (renamed
    after the record was written) still names the current project root, and
    the caller may accept a replacement found inside that same root.  Every
    other reader must use :func:`read_state`.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return None
    project_root = payload.get("project_root")
    if not isinstance(project_root, str) or not project_root or not os.path.isabs(project_root):
        return None
    root = os.path.abspath(project_root)
    profile = payload.get("profile_file")
    resolved = _resolve_inside(root, profile) if isinstance(profile, str) else None
    return root, resolved


def write_state(path: str, record: dict) -> bool:
    """Atomically write *record* to *path*; return False when invalid.

    An invalid record never touches the file.  Writes go through a temporary
    file in the same directory followed by ``os.replace`` (retried briefly on
    ``PermissionError`` for Windows readers), so the server never observes a
    partially written file.  Invalid manifest entries are dropped before the
    write.
    """
    canonical = validate_state(record)
    if canonical is None:
        return False
    config_dir = os.path.dirname(path) or "."
    os.makedirs(config_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".project_state_", suffix=".json", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(canonical, f)
            f.write("\n")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY_S)
        return True
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
