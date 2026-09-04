"""
Project management tools for KiCad.
"""

from collections.abc import Sequence
import logging
import os
import re
from typing import Any

from fastmcp import FastMCP

from kcaa.utils.file_utils import get_project_files, load_project_json
from kcaa.utils.kicad_utils import find_kicad_projects, open_kicad_project

# Sheet-file reference: ``(property "Sheetfile" "child.kicad_sch" ...)`` inside
# a ``(sheet ...)`` node.  Only the Sheetfile property names a file — symbol
# fields and other properties never use this key.
_SHEET_FILE_RE = re.compile(r'\(property\s+"Sheetfile"\s+"([^"]+)"')

_MAX_SHEET_DEPTH = 10


def _referenced_sheets(schematic_path: str) -> list[str]:
    """Return absolute paths of the child sheets referenced by *schematic_path*.

    Reads the ``(property "Sheetfile" "...")`` entries of the ``.kicad_sch``
    file.  Relative paths resolve against the sheet's own directory (KiCad
    convention).  Returns an empty list when the file cannot be read.
    """
    try:
        with open(schematic_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    parent_dir = os.path.dirname(os.path.abspath(schematic_path))
    sheets = []
    for match in _SHEET_FILE_RE.finditer(text):
        raw = match.group(1)
        path = raw if os.path.isabs(raw) else os.path.join(parent_dir, raw)
        sheets.append(os.path.abspath(path))
    return sheets


def _sheet_tree(
    root_schematic: str,
    visited: set[str] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Build a nested sheet-hierarchy node rooted at *root_schematic*.

    Each node is ``{"path": <absolute>, "children": [...]}``; ``children``
    is omitted when the sheet has no sub-sheets.  Cycles (a child sheet
    referencing an ancestor, including via symlinks) are cut by tracking
    real paths, and recursion is depth-bounded, so each sheet appears
    exactly once and the response stays bounded.
    """
    if visited is None:
        visited = set()
    node: dict[str, Any] = {"path": os.path.abspath(root_schematic)}
    real = os.path.realpath(root_schematic)
    if real in visited or depth >= _MAX_SHEET_DEPTH:
        return node
    visited.add(real)
    children = [
        _sheet_tree(child, visited, depth + 1) for child in _referenced_sheets(root_schematic)
    ]
    if children:
        node["children"] = children
    return node


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

        # Get project metadata
        metadata = {}
        project_data = load_project_json(project_path)
        if project_data and "metadata" in project_data:
            metadata = project_data["metadata"]

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
