"""Version history MCP tools for KiCad schematic and PCB files.

Provides three tools that allow an LLM to explicitly checkpoint a file
before a series of edits and to list or restore previous checkpoints.
Also provides project-level tools that bundle the schematic + PCB +
project file into one archive, so the whole project can be restored
as a consistent unit.
"""

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils import version_manager as _vm
from kcaa.utils.version_manager import list_versions, restore_version, save_version_snapshot

log = logging.getLogger(__name__)


def register_version_tools(mcp: FastMCP) -> None:
    """Register file versioning tools on *mcp*."""

    @mcp.tool()
    async def save_file_version(
        file_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Save a version snapshot of a KiCad schematic or PCB file.

        Call this BEFORE a series of edits to create a restore point.  If the
        file content has not changed since the last snapshot, no new snapshot
        is created and the existing one is returned instead.

        Args:
            file_path: Absolute path to the .kicad_sch or .kicad_pcb file.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``version_id`` (str): ID that can be passed to restore_file_version
              - ``snapshot_path`` (str): Full path of the snapshot file
              - ``created`` (bool): True if a new snapshot was created,
                False if the file was unchanged and the existing snapshot was reused
        """
        try:
            existing_ids = {v["id"] for v in list_versions(file_path)}
            snapshot_path = save_version_snapshot(file_path)
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"Failed to save version: {exc}"}

        basename = os.path.basename(file_path)
        prefix = basename + "."
        snap_name = os.path.basename(snapshot_path)
        version_id = snap_name[len(prefix) :]
        created = version_id not in existing_ids

        log.info("save_file_version: %s (created=%s)", snapshot_path, created)
        return {
            "success": True,
            "version_id": version_id,
            "snapshot_path": snapshot_path,
            "created": created,
        }

    @mcp.tool()
    async def list_file_versions(
        file_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all saved version snapshots for a KiCad schematic or PCB file.

        Args:
            file_path: Absolute path to the .kicad_sch or .kicad_pcb file.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``current`` (dict): Current file info with ``timestamp`` and ``size_bytes``.
              - ``versions`` (list): List of version dicts, newest first.
                Each entry has ``id``, ``timestamp``, and ``size_bytes``.
              - ``count`` (int): Number of available versions
        """
        try:
            versions = list_versions(file_path)
        except OSError as exc:
            return {"error": f"Failed to list versions: {exc}"}

        current = None
        if os.path.isfile(file_path):
            from datetime import datetime

            stat = os.stat(file_path)
            current = {
                "timestamp": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size_bytes": stat.st_size,
            }

        return {"success": True, "current": current, "versions": versions, "count": len(versions)}

    @mcp.tool()
    async def restore_file_version(
        file_path: str,
        version_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Restore a KiCad schematic or PCB file to a previously saved version.

        The current file content is automatically snapshotted before the
        restore, so the operation is undoable.

        Args:
            file_path: Absolute path to the .kicad_sch or .kicad_pcb file.
            version_id: The ``id`` value from list_file_versions to restore to.

        Returns:
            A dict with keys:
              - ``success`` (bool)
              - ``restored_from`` (str): The version_id that was restored
              - ``backup_of_current`` (str): Snapshot path of the pre-restore state
        """
        try:
            result = restore_version(file_path, version_id)
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"Failed to restore version: {exc}"}

        log.info(
            "restore_file_version: %s restored to %s (backup: %s)",
            file_path,
            version_id,
            result["backup_of_current"],
        )
        return {"success": True, **result}

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
