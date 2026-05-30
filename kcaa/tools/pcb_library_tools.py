"""
Footprint library discovery and inspection tools for KiCad MCP server.

Provides tools to list available footprint libraries, search footprints
by name or description, and retrieve detailed footprint metadata.
"""

from dataclasses import dataclass
import logging
import os
import threading
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.footprint_index_manager import get_footprint_index_manager
from kcaa.utils.pcb_library_utils import (
    build_effective_library_list,
    find_fp_lib_tables,
    parse_kicad_mod,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background sync state (thread-safe via lock)
# ---------------------------------------------------------------------------


@dataclass
class _FpSyncState:
    running: bool = False
    current: int = 0
    total: int = 0
    current_library: str = ""
    last_result: dict | None = None
    error: str | None = None


_fp_sync_state = _FpSyncState()
_fp_sync_lock = threading.Lock()


def _run_fp_sync_in_background(force: bool, project_path: str | None) -> None:
    """Target function executed in the background footprint sync thread."""

    def _progress(current: int, total: int, library_name: str) -> None:
        with _fp_sync_lock:
            _fp_sync_state.current = current
            _fp_sync_state.total = total
            _fp_sync_state.current_library = library_name

    try:
        mgr = get_footprint_index_manager(project_path)
        stats = mgr.sync(force=force, progress_callback=_progress)
        db_stats = mgr.get_stats()
        result = {
            "success": True,
            "added": stats.added,
            "updated": stats.updated,
            "removed": stats.removed,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "total_footprints": stats.total_footprints,
            "elapsed_seconds": round(stats.elapsed_seconds, 2),
            "database": {
                "library_count": db_stats.library_count,
                "footprint_count": db_stats.footprint_count,
                "last_sync": db_stats.last_sync,
            },
        }
        with _fp_sync_lock:
            _fp_sync_state.last_result = result
            _fp_sync_state.error = None
    except Exception as exc:
        log.error("Background footprint index sync failed: %s", exc, exc_info=True)
        with _fp_sync_lock:
            _fp_sync_state.last_result = None
            _fp_sync_state.error = str(exc)
    finally:
        with _fp_sync_lock:
            _fp_sync_state.running = False
            _fp_sync_state.current_library = ""


def register_pcb_library_tools(mcp: FastMCP) -> None:
    """Register footprint library tools with the MCP server."""

    @mcp.tool()
    async def sync_footprint_index(
        project_path: str | None = None,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Start building or refreshing the footprint library index.

        This tool returns immediately — the actual sync runs in a background
        thread to avoid tool call timeouts.  The first sync can take several
        minutes because it parses all .kicad_mod files.  Subsequent calls are
        incremental (only changed libraries are re-read).

        After calling this tool, use ``get_footprint_sync_status`` to monitor
        progress and check when the sync completes.  Do NOT call
        ``sync_footprint_index`` again while a sync is already running.

        Args:
            project_path: Optional path to a .kicad_pro file; its project-local
                fp-lib-table is included in the sync.
            force: If True, reparse every library regardless of cached state.
                Use only when the database is messed up.
            ctx: MCP context for progress reporting.
        """
        with _fp_sync_lock:
            if _fp_sync_state.running:
                return {
                    "status": "already_running",
                    "message": "A sync is already in progress. Use get_footprint_sync_status to check progress.",
                    "current": _fp_sync_state.current,
                    "total": _fp_sync_state.total,
                    "current_library": _fp_sync_state.current_library,
                }
            _fp_sync_state.running = True
            _fp_sync_state.current = 0
            _fp_sync_state.total = 0
            _fp_sync_state.current_library = ""
            _fp_sync_state.error = None

        if ctx:
            await ctx.info("Starting footprint index sync in background thread…")

        t = threading.Thread(
            target=_run_fp_sync_in_background,
            args=(bool(force), project_path),
            daemon=True,
        )
        t.start()
        log.info("Background footprint sync thread started.")
        return {
            "status": "started",
            "message": (
                "Footprint index sync started in the background. "
                "Call get_footprint_sync_status to monitor progress."
            ),
        }

    @mcp.tool()
    async def get_footprint_sync_status(ctx: Context | None = None) -> dict[str, Any]:
        """Return the current status of the background footprint index sync.

        Call this after ``sync_footprint_index`` to monitor progress.  Poll
        every few seconds until ``running`` is False.

        Returns:
            running: True while sync is in progress.
            current / total: libraries processed so far / total libraries found.
            percent_complete: 0–100 progress estimate.
            current_library: name of the library being processed right now.
            last_result: final stats dict when the sync succeeded (None while running).
            error: error message if the last sync failed (None otherwise).
        """
        with _fp_sync_lock:
            total = _fp_sync_state.total or 0
            current = _fp_sync_state.current
            pct = round(100.0 * current / total) if total > 0 else 0
            return {
                "running": _fp_sync_state.running,
                "current": current,
                "total": total,
                "percent_complete": pct,
                "current_library": _fp_sync_state.current_library,
                "last_result": _fp_sync_state.last_result,
                "error": _fp_sync_state.error,
            }

    @mcp.tool()
    async def list_footprint_libraries(
        project_path: str | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List all available KiCad footprint libraries.

        Returns libraries from the footprint index if it has been built;
        falls back to a live fp-lib-table scan otherwise.  Run
        ``sync_footprint_index`` first to populate the index.

        Args:
            project_path: Optional path to a .kicad_pro file; if given the
                project-local fp-lib-table is included.
            ctx: MCP context for progress reporting.

        Returns:
            dict with:
                libraries: list of {nickname, uri, description, footprint_count}
                source: "index" or "live_scan"
                count: total number of libraries found
        """
        if ctx:
            await ctx.info("Locating footprint libraries…")

        mgr = get_footprint_index_manager(project_path)
        db_stats = mgr.get_stats()

        if db_stats.library_count > 0:
            lib_records = mgr.get_all_libraries()
            libraries = [
                {
                    "nickname": r.library_name,
                    "uri": r.dir_path,
                    "description": r.description,
                    "footprint_count": r.footprint_count,
                }
                for r in lib_records
            ]
            return {
                "libraries": libraries,
                "source": "index",
                "count": len(libraries),
            }

        # Fallback: live scan from fp-lib-table files
        table_paths = find_fp_lib_tables(project_path)
        if not table_paths:
            return {
                "libraries": [],
                "table_files": [],
                "count": 0,
                "warning": "No fp-lib-table files found on this system.",
            }

        all_libraries = build_effective_library_list(project_path)
        for lib in all_libraries:
            lib["exists"] = os.path.isdir(lib["uri"])

        return {
            "libraries": all_libraries,
            "table_files": table_paths,
            "source": "live_scan",
            "count": len(all_libraries),
            "hint": "Run sync_footprint_index to build the index for faster search.",
        }

    @mcp.tool()
    async def search_footprints(
        query: str,
        project_path: str | None,
        ctx: Context | None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search for footprints by name, description, or tags.

        Uses the footprint index (built by ``sync_footprint_index``) for fast
        full-text search across all indexed libraries.  If the index is empty,
        falls back to a slower live scan of .kicad_mod files.

        Args:
            query: Search string matched against footprint name, description,
                and tags (case-insensitive).
            project_path: Optional path to a .kicad_pro file for project-local
                libraries.
            ctx: MCP context for progress reporting.
            max_results: Maximum number of results to return (default 50).

        Returns:
            dict with:
                results: list of {library, name, description, tags, attr, pad_count}
                total_matches: total number of matches found
                truncated: whether results were limited by max_results
                source: "index" or "live_scan"
        """
        if not query or not query.strip():
            return {"error": "query must not be empty", "results": [], "total_matches": 0}

        if ctx:
            await ctx.info(f"Searching footprints for '{query}'…")

        mgr = get_footprint_index_manager(project_path)
        db_stats = mgr.get_stats()

        if db_stats.footprint_count > 0:
            records = mgr.search_footprints(query.strip(), limit=max_results)
            results = [
                {
                    "library": r.library_name,
                    "name": r.footprint_name,
                    "description": r.description,
                    "tags": r.tags,
                    "attr": r.attr,
                    "pad_count": r.pad_count,
                    "has_3d_model": r.has_3d_model,
                }
                for r in records
            ]
            return {
                "results": results,
                "total_matches": len(results),
                "truncated": len(results) >= max_results,
                "source": "index",
            }

        # Fallback: live scan (slow)
        if ctx:
            await ctx.warning(
                "Footprint index is empty — running slow live scan. "
                "Call sync_footprint_index to build the index."
            )
        return await _live_search_footprints(query, project_path, max_results)

    @mcp.tool()
    async def get_footprint_details(
        library_name: str,
        footprint_name: str,
        project_path: str | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific footprint.

        Returns pad layout, courtyard bounding box, and metadata for a
        footprint identified by its library nickname and name.  Always reads
        the .kicad_mod file directly for full detail.

        Args:
            library_name: The library nickname (as shown in fp-lib-table),
                e.g. ``"Resistor_SMD"``.
            footprint_name: The footprint name without extension, e.g.
                ``"R_0402_1005Metric"``.
            project_path: Optional path to a .kicad_pro for project-local libs.
            ctx: MCP context for progress reporting.

        Returns:
            dict with name, description, tags, attr, has_3d_model, layer,
            pads list, courtyard_bbox, and library_path.
        """
        mgr = get_footprint_index_manager(project_path)
        db_stats = mgr.get_stats()

        lib_path: str | None = None

        if db_stats.library_count > 0:
            lib_records = mgr.get_all_libraries()
            for rec in lib_records:
                if rec.library_name == library_name:
                    lib_path = rec.dir_path
                    break

        if not lib_path:
            # Fallback: live fp-lib-table scan
            all_libs = build_effective_library_list(project_path)
            for lib in all_libs:
                if lib["nickname"] == library_name:
                    lib_path = lib["uri"]
                    break

        if not lib_path:
            return {"error": f"Library '{library_name}' not found."}

        mod_path = os.path.join(lib_path, footprint_name + ".kicad_mod")
        if not os.path.isfile(mod_path):
            return {"error": f"Footprint '{footprint_name}' not found in library '{library_name}'."}

        info = parse_kicad_mod(mod_path)
        info["library_path"] = lib_path
        info["file_path"] = mod_path
        return info


async def _live_search_footprints(
    query: str,
    project_path: str | None,
    max_results: int,
) -> dict[str, Any]:
    """Slow live-scan fallback for search_footprints when index is empty."""
    from kcaa.utils.pcb_library_utils import scan_footprint_library

    needle = query.strip().lower()
    libraries = build_effective_library_list(project_path)
    if not libraries:
        return {
            "results": [],
            "total_matches": 0,
            "truncated": False,
            "warning": "No fp-lib-table files found.",
            "source": "live_scan",
        }

    matches: list[dict[str, str]] = []
    for lib in libraries:
        lib_path = lib["uri"]
        if not os.path.isdir(lib_path):
            continue
        for fp_name in scan_footprint_library(lib_path):
            desc, tags, attr = "", "", ""
            if needle in fp_name.lower():
                mod_path = os.path.join(lib_path, fp_name + ".kicad_mod")
                try:
                    info = parse_kicad_mod(mod_path)
                    desc = info.get("description", "")
                    tags = info.get("tags", "")
                    attr = info.get("attr", "")
                except Exception:
                    log.debug("Failed to parse footprint metadata from %s", mod_path)
                matches.append(
                    {
                        "library": lib["nickname"],
                        "name": fp_name,
                        "description": desc,
                        "tags": tags,
                        "attr": attr,
                        "pad_count": 0,
                    }
                )
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    return {
        "results": matches[:max_results],
        "total_matches": len(matches),
        "truncated": len(matches) >= max_results,
        "source": "live_scan",
    }
