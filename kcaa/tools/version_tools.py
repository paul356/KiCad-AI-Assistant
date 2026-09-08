"""Version history MCP tools for KiCad projects.

Provides three tools that allow an LLM to explicitly checkpoint the whole
project (schematic + PCB + project file) before a series of edits and to
list or restore previous checkpoints.  Each version is one ``.tar.gz``
archive under ``.versions/project/``, so all project files share one
version id and are restored together as a consistent unit.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils import version_manager as _vm

log = logging.getLogger(__name__)


def register_version_tools(mcp: FastMCP) -> None:
    """Register project versioning tools on *mcp*."""

    @mcp.tool()
    async def save_project_version(
        project_file: str,
        keep: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Save a version archive that bundles the project's schematic, PCB
        and project file (.kicad_pro) under one version id, so all three can
        be restored together as a consistent unit.

        The archive is written to ``<project_dir>/.versions/project/`` and
        packs the same-stem root ``.kicad_sch`` and ``.kicad_pcb`` files
        next to *project_file*; only files that currently exist are
        archived.  If the current file set is identical to the latest
        archive, no new archive is created and the existing one is returned
        instead.  At most *keep* archives are retained (default 10); oldest
        are pruned first.

        Args:
            project_file: Absolute path to the project file (.kicad_pro).
            keep: Number of archives to retain, default 10.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``version_id`` (str): ID that can be passed to restore_project_version
              - ``archive_path`` (str): Full path of the archive
              - ``created`` (bool): True if a new archive was created,
                False if the file set was unchanged and the latest archive was reused
              - ``files`` (list): Relative paths of the archived files
        """
        try:
            existing_ids = {v["id"] for v in _vm.list_project_versions(project_file)}
            archive_path = _vm.save_project_version(project_file, keep=keep)
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"Failed to save project version: {exc}"}

        version_id = _vm.project_version_id(archive_path, project_file)
        created = version_id not in existing_ids
        files = sorted(_vm.project_file_set(project_file))

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

        The current file set is automatically archived before the restore,
        so the operation is undoable (the new archive's id is returned in
        ``backup_version_id``).

        Args:
            project_file: Absolute path to the project file (.kicad_pro).
            version_id: The ``id`` value from list_project_versions to restore to.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``restored_from`` (str): The version_id that was restored
              - ``backup_of_current`` (str): Archive path of the pre-restore state
              - ``backup_version_id`` (str): Version id of the pre-restore archive
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
