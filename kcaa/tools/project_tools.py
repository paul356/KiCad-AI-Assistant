"""
Project management tools for KiCad.
"""

from collections.abc import Sequence
import logging
import os
from typing import Any

from fastmcp import FastMCP

from kcaa.tools.sheet_tools import _build_sheet_tree
from kcaa.utils.file_utils import get_project_files, load_project_json
from kcaa.utils.kicad_utils import find_kicad_projects, open_kicad_project


def _sheet_tree(root_schematic: str) -> dict[str, Any]:
    """Build a nested sheet-hierarchy node rooted at *root_schematic*.

    Delegates the recursive walk to the shared
    :func:`~kcaa.tools.sheet_tools._build_sheet_tree` and maps each node
    to the ``{"path", "children"}`` shape.  ``children`` is omitted when
    the sheet has no sub-sheets, so leaves are ``{"path": ...}`` nodes.
    """

    def _map(node: dict[str, Any]) -> dict[str, Any]:
        mapped: dict[str, Any] = {"path": os.path.abspath(node["file"])}
        children = node.get("children")
        if children:
            mapped["children"] = [_map(child) for child in children]
        return mapped

    return _map(_build_sheet_tree(root_schematic))


def register_project_tools(
    mcp: FastMCP,
    tools: Sequence[str] = ("list_projects", "get_project_structure", "open_project"),
) -> None:
    """Register project management tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
        tools: Names of the project tools to register.  Defaults to all
            three; pass a subset (e.g. ``("get_project_structure",)``) to
            expose individual tools on profiles that should stay lean.

    Raises:
        ValueError: if a name in ``tools`` is not a known project tool.
    """
    registry: dict[str, Any] = {}

    def list_projects() -> list[dict[str, Any]]:
        """Find and list all KiCad projects on this system."""
        logging.info("Executing list_projects tool...")
        projects = find_kicad_projects()
        logging.info(f"list_projects tool returning {len(projects)} projects.")
        return projects

    registry["list_projects"] = list_projects

    def get_project_structure(project_path: str) -> dict[str, Any]:
        """Get the structure and files of a KiCad project.

        Returns the project metadata and flat file set, plus the layout an
        LLM needs to plan cross-file operations without guessing:

        * ``sheets`` — the schematic sheet hierarchy: the root
          ``.kicad_sch`` and every hierarchical sub-sheet reachable through
          ``(sheet ...)`` references, as a nested tree of ``{"path",
          "children"}`` nodes with absolute paths (cycle-free,
          depth-bounded; ``children`` omitted when empty).
        * ``lib_tables`` — project-local ``sym-lib-table`` / ``fp-lib-table``
          paths, or None when absent.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            dict with keys: name, path, directory, files, metadata, sheets,
            lib_tables.
        """
        if not os.path.exists(project_path):
            return {"error": f"Project not found: {project_path}"}

        project_dir = os.path.dirname(project_path)
        project_name = os.path.basename(project_path)[:-10]  # Remove .kicad_pro extension

        # Get related files
        files = get_project_files(project_path)

        # KiCad stores project metadata under the ``meta`` key of the
        # .kicad_pro JSON (``{filename, version}``); surface it as-is.
        metadata = {}
        project_data = load_project_json(project_path)
        if project_data and "meta" in project_data:
            metadata = project_data["meta"]

        # Schematic sheet hierarchy (root + hierarchical sub-sheets)
        sheets: list[dict[str, Any]] = []
        root_sch = files.get("schematic")
        if root_sch:
            sheets.append(_sheet_tree(root_sch))

        sym_lib_table = os.path.join(project_dir, "sym-lib-table")
        fp_lib_table = os.path.join(project_dir, "fp-lib-table")

        return {
            "name": project_name,
            "path": project_path,
            "directory": project_dir,
            "files": files,
            "metadata": metadata,
            "sheets": sheets,
            "lib_tables": {
                "sym_lib_table": sym_lib_table if os.path.isfile(sym_lib_table) else None,
                "fp_lib_table": fp_lib_table if os.path.isfile(fp_lib_table) else None,
            },
        }

    registry["get_project_structure"] = get_project_structure

    def open_project(project_path: str) -> dict[str, Any]:
        """Open a KiCad project in KiCad."""
        return open_kicad_project(project_path)

    registry["open_project"] = open_project

    for name in tools:
        if name not in registry:
            raise ValueError(f"Unknown project tool: {name!r}")
        mcp.tool()(registry[name])
