"""Version history MCP tools for KiCad projects.

Provides three tools that allow an LLM to explicitly checkpoint the whole
project (schematic + PCB + project file) before a series of edits and to
list or restore previous checkpoints.  Each version is one ``.tar.gz``
archive under ``.versions/project/``, so all project files share one
version id and are restored together as a consistent unit.
"""

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils import project_state as _pstate
from kcaa.utils import version_manager as _vm

log = logging.getLogger(__name__)


def _published_include_files(project_file: str) -> list[str]:
    """Return absolute project files from the published state, when it owns this project.

    The plugin publishes one record naming the open project.  The manifest is
    merged into a snapshot only when the record describes *project_file*
    exactly: same real project root and same ``profile_file``.  A missing
    environment variable (standalone MCP), an invalid record, or another
    project yields an empty list.  Read failures are swallowed: versioning
    must stay usable even when the state file is unavailable.
    """
    state_file = os.environ.get(_pstate.PROJECT_STATE_ENV)
    if not state_file:
        return []
    try:
        record = _pstate.read_state(state_file)
    except Exception:  # noqa: BLE001 -- a broken state file must not fail the tool
        return []
    if record is None:
        return []
    project_path = os.path.abspath(project_file)
    if os.path.realpath(record["project_root"]) != os.path.realpath(os.path.dirname(project_path)):
        return []
    if record["profile_file"] != os.path.basename(project_path):
        return []
    root = record["project_root"]
    return [os.path.join(root, rel) for rel in record["pcb_files"] + record["schematic_files"]]


def register_version_tools(mcp: FastMCP) -> None:
    """Register project versioning tools on *mcp*."""

    @mcp.tool()
    async def save_project_version(
        project_file: str,
        keep: int | None = None,
        include_files: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Save a version archive that bundles the project's schematic, PCB
        and project file (.kicad_pro) under one version id, so all can be
        restored together as a consistent unit.

        The archive is written to ``<project_dir>/.versions/project/`` and
        packs the same-stem root ``.kicad_sch`` and ``.kicad_pcb`` files
        next to *project_file*; only files that currently exist are
        archived.

        In plugin mode the tool also merges every PCB and schematic listed
        by the published project state, so one call covers the whole project
        (nested sheets and additional boards) without the caller listing
        them.  This applies only when the state record names exactly this
        project; a standalone MCP server or another project keeps the
        explicit list.

        *include_files* adds explicit files, such as ``sym-lib-table`` or
        ``fp-lib-table``.  A relative entry is resolved against the project
        directory; an absolute entry is used as given.  Both are archived
        under their project-relative path, and an entry already covered is
        skipped.  A missing entry or a path outside the project directory
        fails the tool call with an error naming that entry, so a snapshot
        is never silently narrowed.  If the current file set is identical
        to the latest archive, no new archive is created and the existing
        one is returned instead.  At most *keep* archives are retained
        (default 10); oldest are pruned first.

        Args:
            project_file: Absolute path to the project file (.kicad_pro).
            keep: Number of archives to retain, default 10.
            include_files: Extra files to archive, relative to the project
                directory or absolute.  An entry that is missing or resolves
                outside the project directory is an error.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``version_id`` (str): ID that can be passed to restore_project_version
              - ``archive_path`` (str): Full path of the archive
              - ``created`` (bool): True if a new archive was created,
                False if the file set was unchanged and the latest archive was reused
              - ``files`` (list): Relative paths of the archived files
        """
        merged_files = list(include_files or ())
        merged_files.extend(_published_include_files(project_file))
        merged_files = merged_files or None
        try:
            existing_ids = {v["id"] for v in _vm.list_project_versions(project_file)}
            archive_path = _vm.save_project_version(
                project_file, keep=keep, include_files=merged_files
            )
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except ValueError as exc:
            return {"error": f"Failed to save project version: {exc}"}
        except OSError as exc:
            return {"error": f"Failed to save project version: {exc}"}

        version_id = _vm.project_version_id(archive_path, project_file)
        created = version_id not in existing_ids
        files = sorted(_vm.project_file_set(project_file, extra_files=merged_files))

        log.info("save_project_version: %s (created=%s)", archive_path, created)
        return {
            "success": True,
            "version_id": version_id,
            "archive_path": archive_path,
            "created": created,
            "files": files,
        }

    @mcp.tool()
    async def list_project_versions(
        project_file: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List saved project version archives for a project.

        Args:
            project_file: Absolute path to the project file (.kicad_pro).

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``versions`` (list): List of version dicts, newest first.
                Each entry has ``id``, ``timestamp``, ``size_bytes`` and
                ``files`` (relative paths archived in that version).
              - ``count`` (int): Number of available versions
        """
        try:
            versions = _vm.list_project_versions(project_file)
        except OSError as exc:
            return {"error": f"Failed to list project versions: {exc}"}

        return {"success": True, "versions": versions, "count": len(versions)}

    @mcp.tool()
    async def restore_project_version(
        project_file: str,
        version_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Restore the project's schematic, PCB and project file to a
        previously saved project version, returning all files to one
        consistent snapshot.

        The current state of every safe file in the selected archive is
        automatically archived before the restore, so the operation is
        undoable (the new archive's id is returned in ``backup_version_id``).
        Archive members that now escape the project are refused, skipped and
        counted in ``backup_skipped``.

        Args:
            project_file: Absolute path to the project file (.kicad_pro).
            version_id: The ``id`` value from list_project_versions to restore to.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``restored_from`` (str): The version_id that was restored
              - ``backup_of_current`` (str): Archive path of the pre-restore state
              - ``backup_version_id`` (str): Version id of the pre-restore archive
              - ``backup_skipped`` (int): Unsafe members excluded from backup
                and restore
              - ``files`` (list): Relative paths restored from the archive
        """
        try:
            result = _vm.restore_project_version(project_file, version_id)
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"Failed to restore project version: {exc}"}

        log.info(
            "restore_project_version: %s restored to %s (backup: %s)",
            project_file,
            version_id,
            result["backup_of_current"],
        )
        return {"success": True, **result}
