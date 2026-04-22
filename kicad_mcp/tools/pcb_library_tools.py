"""
Footprint library discovery and inspection tools for KiCad MCP server.

Provides tools to list available footprint libraries, search footprints
by name or description, and retrieve detailed footprint metadata.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastmcp import Context, FastMCP

from kicad_mcp.utils.pcb_library_utils import (
    find_fp_lib_tables,
    parse_fp_lib_table,
    parse_kicad_mod,
    scan_footprint_library,
)
from kicad_mcp.utils.file_utils import get_project_files

log = logging.getLogger(__name__)


def register_pcb_library_tools(mcp: FastMCP) -> None:
    """Register footprint library tools with the MCP server."""

    @mcp.tool()
    async def list_footprint_libraries(
        project_path: Optional[str],
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """List all available KiCad footprint libraries.

        Reads the global fp-lib-table and, if a project path is provided,
        also the project-local fp-lib-table.

        Args:
            project_path: Optional path to a .kicad_pro file; if given the
                project-local fp-lib-table is included.
            ctx: MCP context for progress reporting.

        Returns:
            dict with:
                libraries: list of {nickname, type, uri, description}
                table_files: list of fp-lib-table paths that were read
                count: total number of libraries found
        """
        if ctx:
            await ctx.info("Locating fp-lib-table files…")

        table_paths = find_fp_lib_tables(project_path)
        if not table_paths:
            return {
                "libraries": [],
                "table_files": [],
                "count": 0,
                "warning": "No fp-lib-table files found on this system.",
            }

        seen_nicknames: set = set()
        all_libraries: List[Dict[str, str]] = []
        for tpath in table_paths:
            for lib in parse_fp_lib_table(tpath):
                if lib["nickname"] not in seen_nicknames:
                    seen_nicknames.add(lib["nickname"])
                    # Annotate whether the resolved path actually exists
                    lib["exists"] = os.path.isdir(lib["uri"])
                    all_libraries.append(lib)

        return {
            "libraries": all_libraries,
            "table_files": table_paths,
            "count": len(all_libraries),
        }

    @mcp.tool()
    async def search_footprints(
        query: str,
        project_path: Optional[str],
        ctx: Context | None,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        """Search for footprints by name or description across all libraries.

        Scans available .pretty directories for .kicad_mod files whose name
        or description contains the query string (case-insensitive).

        Args:
            query: Search string matched against footprint name and description.
            project_path: Optional path to a .kicad_pro file for project-local
                libraries.
            ctx: MCP context for progress reporting.
            max_results: Maximum number of results to return (default 50).

        Returns:
            dict with:
                results: list of {library, name, description, path}
                total_matches: total number of matches found
                truncated: whether results were limited by max_results
        """
        if not query or not query.strip():
            return {"error": "query must not be empty", "results": [], "total_matches": 0}

        needle = query.strip().lower()
        table_paths = find_fp_lib_tables(project_path)
        if not table_paths:
            return {"results": [], "total_matches": 0, "truncated": False,
                    "warning": "No fp-lib-table files found."}

        seen_nicknames: set = set()
        libraries: List[Dict[str, str]] = []
        for tpath in table_paths:
            for lib in parse_fp_lib_table(tpath):
                if lib["nickname"] not in seen_nicknames:
                    seen_nicknames.add(lib["nickname"])
                    libraries.append(lib)

        matches: List[Dict[str, str]] = []
        for lib in libraries:
            lib_path = lib["uri"]
            if not os.path.isdir(lib_path):
                continue
            for fp_name in scan_footprint_library(lib_path):
                if needle in fp_name.lower():
                    mod_path = os.path.join(lib_path, fp_name + ".kicad_mod")
                    desc = ""
                    try:
                        info = parse_kicad_mod(mod_path)
                        desc = info.get("description", "")
                    except Exception:
                        pass
                    matches.append({
                        "library": lib["nickname"],
                        "name": fp_name,
                        "description": desc,
                        "path": mod_path,
                    })
                    if len(matches) >= max_results * 2:
                        break
            if len(matches) >= max_results * 2:
                break

        # Also search by description in a second pass if we have few name matches
        if len(matches) < max_results:
            for lib in libraries:
                lib_path = lib["uri"]
                if not os.path.isdir(lib_path):
                    continue
                for fp_name in scan_footprint_library(lib_path):
                    mod_path = os.path.join(lib_path, fp_name + ".kicad_mod")
                    try:
                        info = parse_kicad_mod(mod_path)
                        desc = info.get("description", "")
                        if needle in desc.lower() and not any(
                            m["library"] == lib["nickname"] and m["name"] == fp_name
                            for m in matches
                        ):
                            matches.append({
                                "library": lib["nickname"],
                                "name": fp_name,
                                "description": desc,
                                "path": mod_path,
                            })
                    except Exception:
                        pass
                    if len(matches) >= max_results * 2:
                        break
                if len(matches) >= max_results * 2:
                    break

        total = len(matches)
        truncated = total > max_results
        return {
            "results": matches[:max_results],
            "total_matches": total,
            "truncated": truncated,
        }

    @mcp.tool()
    async def get_footprint_details(
        library_name: str,
        footprint_name: str,
        project_path: Optional[str],
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Get detailed information about a specific footprint.

        Returns pad layout, courtyard bounding box, and metadata for a
        footprint identified by its library nickname and name.

        Args:
            library_name: The library nickname (as shown in fp-lib-table),
                e.g. ``"Resistor_SMD"``.
            footprint_name: The footprint name without extension, e.g.
                ``"R_0402_1005Metric"``.
            project_path: Optional path to a .kicad_pro for project-local libs.
            ctx: MCP context for progress reporting.

        Returns:
            dict with name, description, tags, layer, pads list, courtyard_bbox,
            and library_path.
        """
        table_paths = find_fp_lib_tables(project_path)
        if not table_paths:
            return {"error": "No fp-lib-table files found on this system."}

        lib_path: Optional[str] = None
        for tpath in table_paths:
            for lib in parse_fp_lib_table(tpath):
                if lib["nickname"] == library_name:
                    lib_path = lib["uri"]
                    break
            if lib_path:
                break

        if not lib_path:
            return {"error": f"Library '{library_name}' not found in any fp-lib-table."}

        mod_path = os.path.join(lib_path, footprint_name + ".kicad_mod")
        if not os.path.isfile(mod_path):
            return {"error": f"Footprint '{footprint_name}' not found in library '{library_name}'."}

        info = parse_kicad_mod(mod_path)
        info["library_path"] = lib_path
        info["file_path"] = mod_path
        return info
