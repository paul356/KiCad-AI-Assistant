"""
Project management tools for KiCad.
"""

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

_THIRD_PARTY_DIR = "3rdparty"
_MAX_THIRD_PARTY_ENTRIES = 200
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

    Each node is ``{"path": <absolute>, "children": [...]}``.  Cycles (a
    child sheet referencing an ancestor, including via symlinks) are cut by
    tracking real paths, and recursion is depth-bounded, so each sheet
    appears exactly once and the response stays bounded.
    """
    if visited is None:
        visited = set()
    node: dict[str, Any] = {"path": os.path.abspath(root_schematic), "children": []}
    real = os.path.realpath(root_schematic)
    if real in visited or depth >= _MAX_SHEET_DEPTH:
        return node
    visited.add(real)
    for child in _referenced_sheets(root_schematic):
        node["children"].append(_sheet_tree(child, visited, depth + 1))
    return node


def _list_third_party_libraries(project_dir: str) -> list[dict[str, str]]:
    """List symbol/footprint library files under the project's ``3rdparty/``.

    Returns a bounded list of ``{"path", "kind"}`` entries, where kind is
    ``"symbol"`` for ``.kicad_sym`` files and ``"footprint"`` for
    ``.kicad_mod`` files.
    """
    tp_dir = os.path.join(project_dir, _THIRD_PARTY_DIR)
    if not os.path.isdir(tp_dir):
        return []
    entries: list[dict[str, str]] = []
    for root, dirs, fnames in os.walk(tp_dir):
        depth = root[len(tp_dir) :].count(os.sep)
        if depth >= 3:
            dirs[:] = []  # bound the walk
        for fname in sorted(fnames):
            path = os.path.join(root, fname)
            if fname.endswith(".kicad_mod"):
                entries.append({"path": path, "kind": "footprint"})
            elif fname.endswith(".kicad_sym"):
                entries.append({"path": path, "kind": "symbol"})
            if len(entries) >= _MAX_THIRD_PARTY_ENTRIES:
                return entries
    return entries


def register_project_tools(mcp: FastMCP) -> None:
    """Register project management tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        """Find and list all KiCad projects on this system."""
        logging.info("Executing list_projects tool...")
        projects = find_kicad_projects()
        logging.info(f"list_projects tool returning {len(projects)} projects.")
        return projects

    @mcp.tool()
    def get_project_structure(project_path: str) -> dict[str, Any]:
        """Get the structure and files of a KiCad project.

        Returns the project metadata and flat file set, plus the layout an
        LLM needs to plan cross-file operations without guessing:

        * ``sheets`` — the schematic sheet hierarchy: the root ``.kicad_sch``
          and every hierarchical sub-sheet reachable through ``(sheet ...)``
          references, as a nested tree of ``{"path", "children"}`` nodes with
          absolute paths (cycle-free, depth-bounded).
        * ``third_party`` — symbol (``.kicad_sym``) and footprint
          (``.kicad_mod``) libraries under the project's ``3rdparty/``
          directory (bounded list).
        * ``lib_tables`` — project-local ``sym-lib-table`` / ``fp-lib-table``
          paths, or None when absent.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            dict with keys: name, path, directory, files, metadata, sheets,
            third_party, lib_tables.
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
            "third_party": _list_third_party_libraries(project_dir),
            "lib_tables": {
                "sym_lib_table": sym_lib_table if os.path.isfile(sym_lib_table) else None,
                "fp_lib_table": fp_lib_table if os.path.isfile(fp_lib_table) else None,
            },
        }

    @mcp.tool()
    def open_project(project_path: str) -> dict[str, Any]:
        """Open a KiCad project in KiCad."""
        return open_kicad_project(project_path)
