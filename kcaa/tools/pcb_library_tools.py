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

from kcaa.utils.config import config
from kcaa.utils.footprint_index_manager import get_footprint_index_manager, normalize_project_id
from kcaa.utils.fp_lib_table_utils import (
    get_user_fp_lib_table_path,
    register_library_in_table,
    sanitize_lib_nickname,
)
from kcaa.utils.pcb_footprint_utils import (
    get_fp_property,
    get_pcb_version,
    iter_footprint_nodes,
    normalize_footprint_for_library,
    split_footprint_header,
    write_footprint_mod,
)
from kcaa.utils.pcb_library_utils import (
    build_effective_library_list,
    find_fp_lib_tables,
    parse_kicad_mod,
    scan_footprint_library,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

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
        project_path: str,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Start building or refreshing the footprint library index.

        This tool returns immediately — the actual sync runs in a background
        thread to avoid tool call timeouts.  The first sync can take several
        minutes because it parses all .kicad_mod files.  Subsequent calls are
        incremental (only changed libraries are re-read).

        Indexed libraries are scoped to the project: global user/system
        fp-lib-table libraries plus the project's own fp-lib-table.  Rows
        belonging to other projects are never touched.

        After calling this tool, use ``get_footprint_sync_status`` to monitor
        progress and check when the sync completes.  Do NOT call
        ``sync_footprint_index`` again while a sync is already running.

        Args:
            project_path: Path to a .kicad_pro file (or .kicad_pcb); the
                project's directory is used to scope the index.
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
        project_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List all available KiCad footprint libraries scoped to a project.

        Returns libraries from the footprint index if it has been built;
        falls back to a live fp-lib-table scan otherwise.  Run
        ``sync_footprint_index`` first to populate the index.  Only global
        libraries plus the project's own fp-lib-table entries are listed.

        Args:
            project_path: Path to a .kicad_pro file (or .kicad_pcb); its
                directory identifies the project scope.
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
        project_path: str,
        ctx: Context | None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search for footprints by name, description, or tags.

        Uses the footprint index (built by ``sync_footprint_index``) for fast
        full-text search across global plus the project's own libraries.
        If the index is empty, falls back to a slower live scan of .kicad_mod
        files.  Other projects' libraries are never searched.

        Args:
            query: Search string matched against footprint name, description,
                and tags (case-insensitive).
            project_path: Path to a .kicad_pro file (or .kicad_pcb); its
                directory identifies the project scope.
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
        project_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific footprint.

        Returns pad layout, courtyard bounding box, and metadata for a
        footprint identified by its library nickname and name.  Always reads
        the .kicad_mod file directly for full detail.  The lookup is scoped
        to the project's global + project-local libraries; when a same-named
        project library exists it takes precedence over the global one.

        Args:
            library_name: The library nickname (as shown in fp-lib-table),
                e.g. ``"Resistor_SMD"``.
            footprint_name: The footprint name without extension, e.g.
                ``"R_0402_1005Metric"``.
            project_path: Path to a .kicad_pro file (or .kicad_pcb); its
                directory identifies the project scope.
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
            project_id = normalize_project_id(project_path)
            # Project-owned libraries take precedence over the global one
            # with the same nickname.
            for rec in lib_records:
                if rec.library_name == library_name and rec.project == project_id:
                    lib_path = rec.dir_path
                    break
            if lib_path is None:
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

    @mcp.tool()
    async def find_missing_footprints(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List board footprints that exist in no indexed footprint library.

        Read-only: compares the footprints embedded in the board against the
        effective fp-lib-table library list (project-local table plus global
        user table, including system libraries).  Footprints with no
        same-named match anywhere are candidates for export into a
        3rdparty library.

        Args:
            pcb_path: Absolute path to the ``.kicad_pcb`` file.  Its
                directory is treated as the project directory (project-local
                fp-lib-table and ``${KIPRJMOD}`` URIs are resolved from it).
            ctx: MCP context (unused).

        Returns:
            dict with ``missing`` (list of {name, library, reference, value}),
            ``missing_count``, ``existing`` and ``existing_count``; plus
            ``error`` on failure.
        """
        try:
            _, footprints, _ = _collect_board_footprints(pcb_path)
            existing = _collect_existing_names(pcb_path)
            missing = [fp for fp in footprints if fp["name"] not in existing]
            return {
                "missing": missing,
                "missing_count": len(missing),
                "existing": sorted(existing),
                "existing_count": len(existing),
            }
        except Exception as exc:
            log.error("find_missing_footprints failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @mcp.tool()
    async def create_3rdparty_footprint_library(
        name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create and register a new 3rdparty footprint library.

        Creates ``<name>.pretty`` under ``${KICAD10_3RD_PARTY}/footprints``,
        registers it in the global user fp-lib-table (``.bak`` backup,
        idempotent), and indexes exactly that library in the footprint
        database so library-list and search tools see it immediately.
        Project-local fp-lib-table files are never modified.

        Args:
            name: New library nickname (sanitized to fp-lib-table-safe
                characters).
            ctx: MCP context (unused).

        Returns:
            dict with ``library``, ``path``, ``table_path``, ``registered``,
            ``indexed`` (int, footprints indexed; -1 on failure); plus
            ``error`` on failure.
        """
        try:
            nickname = sanitize_lib_nickname(name)
            if not nickname:
                return {"error": f"Invalid library name: {name!r}"}
            # Nicknames are globally unique: block any name that already
            # exists in the index (any project) or the global fp-lib-table.
            # library_name_exists is deliberately cross-project, so no
            # project-scoped stats guard.
            mgr = get_footprint_index_manager(None)
            if mgr.library_name_exists(nickname) or nickname in {
                lib["nickname"] for lib in build_effective_library_list(None)
            }:
                return {
                    "error": (
                        f"Library '{nickname}' already exists; "
                        "use add_footprints_to_3rdparty_library to export into it."
                    )
                }
            library_dir = os.path.join(_3rd_party_footprints_dir(), f"{nickname}.pretty")
            if os.path.exists(library_dir):
                return {
                    "error": (
                        f"Directory already exists, refusing to recreate: {library_dir}. "
                        "Use add_footprints_to_3rdparty_library to export into it."
                    )
                }
            os.makedirs(library_dir, exist_ok=False)
            ver_tag = config.kicad_version.split(".")[0]
            uri = f"${{KICAD{ver_tag}_3RD_PARTY}}/footprints/{nickname}.pretty"
            table_path = get_user_fp_lib_table_path()
            result = register_library_in_table(
                table_path,
                nickname,
                uri,
                description=f"Created by KiCad MCP footprint export ({nickname})",
            )
            indexed = _index_library_entry(nickname, library_dir, raw_uri=uri)
            return {
                "library": nickname,
                "path": library_dir,
                "table_path": table_path,
                "registered": bool(result.get("registered")),
                "indexed": indexed,
            }
        except Exception as exc:
            log.error("create_3rdparty_footprint_library failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @mcp.tool()
    async def add_footprints_to_3rdparty_library(
        pcb_path: str,
        library: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Export board footprints missing from libraries into a 3rdparty library.

        Writes each board-embedded footprint that exists in no indexed library
        as a ``.kicad_mod`` file into the target library directory, then
        updates the footprint database for exactly that library.  Project-local
        fp-lib-table libraries are never indexed by this tool.  The board file
        is never modified.

        A footprint is reported as ``failed`` when its target file already
        exists in the library directory — it is never overwritten.  A
        footprint already present in another indexed library is ``skipped``.

        Args:
            pcb_path: Absolute path to the ``.kicad_pcb`` file.  Its
                directory is treated as the project directory (project-local
                fp-lib-table and ``${KIPRJMOD}`` URIs are resolved from it).
            library: Nickname of the target 3rdparty library, as registered
                in fp-lib-table.
            ctx: MCP context for progress reporting.

        Returns:
            dict with ``library``, ``library_path``, ``exported`` (list of
            paths), ``exported_count``, ``failed`` (list of {name, reason} —
            target file already exists, not overwritten), ``failed_count``,
            ``skipped`` (list of {name, reason}), ``skipped_count``,
            ``indexed``; plus ``error`` on failure.
        """
        try:
            nodes, footprints, version = _collect_board_footprints(pcb_path)
            library_dir = _resolve_library_dir(library, pcb_path)
            # Same source of truth as find_missing_footprints: the index DB
            # (project-scoped), with a live scan only when the index is empty.
            existing = _collect_existing_names(pcb_path)
            if ctx:
                await ctx.info(
                    f"Exporting missing footprints to {library_dir} "
                    f"({len(nodes)} board footprints, {len(existing)} indexed names)"
                )

            exported: list[str] = []
            failed: list[dict[str, str]] = []
            skipped: list[dict[str, str]] = []
            seen_in_run: set[str] = set()
            for node in nodes:
                _lib, name = split_footprint_header(node)
                if name in seen_in_run:
                    skipped.append({"name": name, "reason": "duplicate in run"})
                    continue
                # Refuse to overwrite: an existing target file is a failure,
                # even when the name is already indexed (e.g. a previous run
                # exported it into this same library).
                target_path = os.path.join(library_dir, f"{name}.kicad_mod")
                if os.path.exists(target_path):
                    failed.append(
                        {
                            "name": name,
                            "reason": (
                                f"target file already exists: {target_path} (refusing to overwrite)"
                            ),
                        }
                    )
                    continue
                if name in existing:
                    skipped.append({"name": name, "reason": "already in library"})
                    continue
                try:
                    path = write_footprint_mod(
                        library_dir,
                        name,
                        normalize_footprint_for_library(node, version, library),
                    )
                except FileExistsError:
                    failed.append({"name": name, "reason": "target file already exists"})
                    continue
                exported.append(path)
                seen_in_run.add(name)

            indexed = _index_library_entry(library, library_dir)
            return {
                "library": library,
                "library_path": library_dir,
                "exported": exported,
                "exported_count": len(exported),
                "failed": failed,
                "failed_count": len(failed),
                "skipped": skipped,
                "skipped_count": len(skipped),
                "indexed": indexed,
            }
        except Exception as exc:
            log.error("add_footprints_to_3rdparty_library failed: %s", exc, exc_info=True)
            return {"error": str(exc)}


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


# ---------------------------------------------------------------------------
# PCB → 3rdparty library export helpers
# ---------------------------------------------------------------------------


def _3rd_party_footprints_dir() -> str:
    """Return ``${KICAD10_3RD_PARTY}/footprints`` (resolved, absolute)."""
    return os.path.join(config.kicad_3rd_party, "footprints")


def _resolve_library_dir(library: str, pcb_path: str | None) -> str:
    """Resolve a registered library nickname to its ``.pretty`` directory.

    :raises ValueError: When the nickname is not in fp-lib-table, resolves to
        a missing directory, or is read-only.
    """
    libs = build_effective_library_list(pcb_path)
    by_nickname = {lib["nickname"]: lib for lib in libs}
    if library not in by_nickname:
        raise ValueError(
            f"Library '{library}' not found in fp-lib-table. "
            "Create it first with create_3rdparty_footprint_library."
        )
    lib_dir = by_nickname[library].get("uri", "")
    if not lib_dir or not os.path.isdir(lib_dir):
        raise ValueError(f"Library '{library}' resolves to a missing directory: {lib_dir}")
    if not os.access(lib_dir, os.W_OK):
        raise ValueError(
            f"Library '{library}' is read-only or not writable: {lib_dir}. "
            "Pick a writable 3rdparty library or create a new one."
        )
    return lib_dir


def _collect_existing_names(pcb_path: str | None) -> set[str]:
    """Return every footprint name that already exists in libraries.

    Prefers the footprint index database scoped to the project (global plus
    project-local libraries; consistent with ``sync_footprint_index`` results).
    Falls back to a live fp-lib-table scan when the index is empty.
    """
    existing: set[str] = set()
    try:
        mgr = get_footprint_index_manager(pcb_path)
        if mgr.get_stats().footprint_count > 0:
            existing = mgr.get_all_footprint_names()
        else:
            existing = _live_scan_existing_names(pcb_path)
    except Exception as exc:
        log.warning("Footprint index read failed (%s) — falling back to live scan", exc)
        existing = _live_scan_existing_names(pcb_path)
    return existing


def _live_scan_existing_names(pcb_path: str | None) -> set[str]:
    """Live-scan fallback: every footprint name across the effective library
    list (project-local table plus global user table, ``${KIPRJMOD}`` /
    ``KICAD*`` URI expanded).  Purely in-memory — nothing is written to the
    footprint database.
    """
    names: set[str] = set()
    for lib in build_effective_library_list(pcb_path):
        uri = lib.get("uri", "")
        if uri and os.path.isdir(uri):
            names.update(scan_footprint_library(uri))
    return names


def _collect_board_footprints(pcb_path: str) -> tuple[list[Any], list[dict[str, Any]], int]:
    """Load the board and return (nodes, footprints dicts, version)."""
    data = load_pcb(pcb_path)
    version = get_pcb_version(data)
    nodes: list[list[Any]] = []
    footprints: list[dict[str, Any]] = []
    for node in iter_footprint_nodes(data):
        lib, name = split_footprint_header(node)
        reference = get_fp_property(node, "Reference") or ""
        value = get_fp_property(node, "Value") or ""
        footprints.append(
            {
                "name": name,
                "library": lib or "",
                "reference": reference,
                "value": value,
            }
        )
        nodes.append(node)
    return nodes, footprints, version


def _index_library_entry(
    library: str,
    library_dir: str,
    raw_uri: str = "",
) -> int:
    """Index exactly one library directory into the footprint database.

    Narrow update (no full-table traversal): project-local fp-lib-table
    libraries are never pulled into the database by this path.  Returns the
    number of footprints stored, or -1 on failure.
    """
    try:
        return get_footprint_index_manager(None).index_library(
            library,
            library_dir,
            raw_uri=raw_uri,
        )
    except Exception as exc:
        log.error("Footprint index update failed for %s: %s", library, exc, exc_info=True)
        return -1
