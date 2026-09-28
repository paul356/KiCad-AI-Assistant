"""
Footprint library discovery and inspection tools for KiCad MCP server.

Provides tools to list available footprint libraries, search footprints
by name or description, and retrieve detailed footprint metadata.
"""

import copy
from dataclasses import dataclass
import logging
import os
import threading
from typing import Any
import uuid

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.utils.config import config
from kcaa.utils.footprint_index_manager import get_footprint_index_manager, normalize_project_id
from kcaa.utils.fp_lib_table_utils import (
    get_user_fp_lib_table_path,
    register_library_in_table,
    sanitize_lib_nickname,
)
from kcaa.utils.pcb_board_utils import get_edge_cuts_items
from kcaa.utils.pcb_footprint_utils import (
    get_fp_layer,
    get_fp_property,
    get_pcb_version,
    is_safe_footprint_name,
    iter_footprint_nodes,
    normalize_footprint_for_library,
    set_fp_at,
    split_footprint_header,
    upsert_fp_property,
    write_footprint_mod,
)
from kcaa.utils.pcb_library_utils import (
    build_effective_library_list,
    find_fp_lib_tables,
    parse_kicad_mod,
    scan_footprint_library,
)
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)


def _sym(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain string."""
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    return str(value)


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
    last_project_path: str | None = None


_fp_sync_state = _FpSyncState()
_fp_sync_lock = threading.Lock()


def _run_fp_sync_in_background(force: bool, project_path: str | None) -> None:
    """Target function executed in the background footprint sync thread."""

    with _fp_sync_lock:
        _fp_sync_state.last_project_path = normalize_project_id(project_path)

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


async def _place_one_footprint(
    pcb_path: str,
    nets: dict[str, str],
    footprint: str | None,
    reference: str | None,
    x: float | None,
    y: float | None,
    rotation: float,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Place exactly one footprint onto *pcb_path*; the shared batch item core.

    Used by the batch ``add_footprints_to_pcb`` path; the footprint is
    resolved by ``footprint`` (``"Library:Name"`` or bare ``"Name"`` searched
    across the fp-lib-table libraries — there is no tool- or item-level
    library restriction).  *nets* must name a net for EVERY pad of the placed
    footprint (``""`` = net 0 / unconnected): a pad missing from *nets* is a
    hard error and nothing is written — the part is never silently shorted
    onto a blanket or zero net.  Returns the dict the tool documents
    (``success``/``reference``/``placed_at``/... or ``error``); never raises
    — every failure mode returns an error dict.
    """
    try:
        if not reference:
            return {"error": "reference must be a non-empty string"}
        if not footprint:
            return {"error": "footprint must be a non-empty string"}
        if x is None or y is None:
            return {"error": "x and y are required"}
        x = float(x)
        y = float(y)
        rotation = float(rotation)
        try:
            data = load_pcb(pcb_path)
        except (FileNotFoundError, ValueError, OSError) as exc:
            return {"error": f"cannot read board: {exc}"}
        if not data or not isinstance(data[0], sexpdata.Symbol) or _sym(data[0]) != "kicad_pcb":
            return {"error": f"{pcb_path} does not look like a .kicad_pcb file"}

        for node in iter_footprint_nodes(data):
            if get_fp_property(node, "Reference") == reference:
                return {
                    "error": (
                        f"reference '{reference}' already exists on the board "
                        f"({reference} is placed elsewhere); pick a unique reference"
                    )
                }

        try:
            header, mod_path, scanned = _find_footprint_mod_path(footprint, None, pcb_path)
        except ValueError as exc:
            return {"error": str(exc)}

        try:
            with open(mod_path, encoding="utf-8") as fh:
                mod_data = sexpdata.loads(fh.read())
        except (OSError, ValueError, TypeError) as exc:
            return {"error": f"cannot parse footprint file {mod_path}: {exc}"}
        if (
            not isinstance(mod_data, list)
            or len(mod_data) < 2
            or not isinstance(mod_data[0], sexpdata.Symbol)
            or _sym(mod_data[0]) not in ("footprint", "module")
        ):
            return {"error": f"{mod_path} is not a KiCad footprint file"}

        # Every pad of the footprint must be covered by nets — a missing pad
        # is a hard error so a partial nets dict can never silently land the
        # uncovered pad on net 0 (the whole-point of the required nets API).
        pad_numbers = [
            _sym(child[1])
            for child in mod_data
            if isinstance(child, list)
            and len(child) > 1
            and isinstance(child[0], sexpdata.Symbol)
            and _sym(child[0]) == "pad"
        ]
        pad_nets_by_num = {str(pad_no): net_name for pad_no, net_name in nets.items()}
        missing = [p for p in pad_numbers if p not in pad_nets_by_num]
        if missing:
            return {
                "error": f"missing net for pad(s): {', '.join(missing)}",
            }

        # Board footprint node: keep every library item (fp_line, fp_text,
        # pads, model ...), then add the board-instance data on top.
        fp_node: list[Any] = [mod_data[0], header]
        fp_node.extend(copy.deepcopy(mod_data[2:]))
        set_fp_at(fp_node, x, y, rotation)
        upsert_fp_property(fp_node, "Reference", reference)
        fp_node.append([sexpdata.Symbol("uuid"), str(uuid.uuid4())])

        # "" (or None) in nets -> net 0; named nets resolve or auto-add to the
        # board's (net ...) list.  Sorted so auto-added numbers are stable.
        pad_nets = {
            pad_no: _resolve_board_net(data, net_name if net_name else None)
            for pad_no, net_name in sorted(pad_nets_by_num.items())
        }
        pad_count, pads_net_list = _apply_nets_to_pads(fp_node, pad_nets, (0, ""))
        data.append(fp_node)
        try:
            bak_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"failed to write board: {exc}"}

        result: dict[str, Any] = {
            "success": True,
            "reference": reference,
            "footprint": header,
            "placed_at": [x, y],
            "rotation": rotation,
            "backup_path": bak_path,
            "pad_count": pad_count,
            "pads_net": pads_net_list,
        }
        outline_msg = _outline_warning(data, x, y)
        if outline_msg:
            result["warnings"] = [outline_msg]
        if ctx:
            net_summary = ", ".join(
                f"{entry['pad']}={entry['net'] or '0'}" for entry in pads_net_list
            )
            await ctx.info(
                f"Placed {header} as {reference} at ({x}, {y}) rot {rotation} "
                f"({pad_count} pads; nets {net_summary})"
            )
        return result
    except Exception as exc:
        log.error("add_footprints_to_pcb item failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


async def _place_many_footprints(
    pcb_path: str,
    footprints: list[dict[str, Any]],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Place several footprints in one call, collecting per-item results.

    The footprint-level placement arguments all live in each *footprints*
    dict — ``footprint`` (``"Library:Name"`` or bare ``"Name"``), ``reference``,
    ``x``, ``y``, optional ``rotation`` and the required ``nets``.  There is
    no single-footprint mode and no tool-level defaults: every item must
    carry a ``nets`` dict covering every pad of its footprint — a pad
    missing from that dict fails the item with "missing net for pad(s): ..."
    rather than silently landing on net 0.  Items are placed one at a time,
    each writing its own save; a failing item never rolls back or blocks the
    others.  Returns ``{"success", "results", "placed_count",
    "failed_count", "failed"}`` where ``success`` is True only when every
    item was placed.
    """
    results: list[dict[str, Any]] = []
    for item in footprints:
        ref = str(item.get("reference") or "") if isinstance(item, dict) else ""
        if not isinstance(item, dict):
            results.append(
                {"success": False, "reference": "", "error": "footprint spec must be an object"}
            )
            continue
        try:
            x_val = item.get("x")
            y_val = item.get("y")
            item_nets = item.get("nets")
            if not isinstance(item_nets, dict):
                raise ValueError("nets must be an object mapping pad numbers to net names")
            res = await _place_one_footprint(
                pcb_path,
                nets=item_nets,
                footprint=item.get("footprint"),
                reference=item.get("reference"),
                x=float(x_val) if x_val is not None else None,
                y=float(y_val) if y_val is not None else None,
                rotation=float(item.get("rotation") or 0.0),
                ctx=ctx,
            )
        except Exception as exc:
            log.error("place_many item (%s) failed: %s", ref, exc, exc_info=True)
            res = {"error": str(exc)}
        if res.get("success") is True and "error" not in res:
            results.append({"success": True, "reference": ref, "result": res})
        else:
            results.append(
                {
                    "success": False,
                    "reference": ref,
                    "error": res.get("error") or "placement failed",
                }
            )

    placed = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    return {
        "success": not failed,
        "results": results,
        "placed_count": len(placed),
        "failed_count": len(failed),
        "failed": [
            {"reference": r["reference"], "error": r.get("error", "placement failed")}
            for r in failed
        ],
    }


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

        Indexed libraries are scoped to the project: global user/system
        fp-lib-table libraries plus the project's own fp-lib-table.  Rows
        belonging to other projects are never touched.

        After calling this tool, use ``get_footprint_sync_status`` to monitor
        progress and check when the sync completes.  Do NOT call
        ``sync_footprint_index`` again while a sync is already running.

        Args:
            project_path: Path to a .kicad_pro file (or .kicad_pcb); the
                project's directory is used to scope the index.  Omit to
                index only the global (non-project) libraries.
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
        project_path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all available KiCad footprint libraries, optionally scoped to a project.

        Returns libraries from the footprint index if it has been built;
        falls back to a live fp-lib-table scan otherwise.  Run
        ``sync_footprint_index`` first to populate the index.  With a
        project_path only global libraries plus that project's own
        fp-lib-table entries are listed; without one, global libraries only.

        Args:
            ctx: MCP context for progress reporting.
            project_path: Optional path to a .kicad_pro file (or
                .kicad_pcb); its directory identifies the project scope.
                Omit for global (non-project) libraries only.

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
        project_path: str | None = None,
        ctx: Context | None = None,
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
            project_path: Optional path to a .kicad_pro file (or .kicad_pcb);
                its directory identifies the project scope; omit for global
                (non-project) libraries only.
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
        project_path: str | None = None,
        ctx: Context | None = None,
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
            project_path: Optional path to a .kicad_pro file (or .kicad_pcb);
                its directory identifies the project scope; omit for global
                (non-project) libraries only.
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
    async def add_footprints_to_pcb(
        pcb_path: str,
        footprints: list[dict[str, Any]],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Place several footprints from footprint libraries onto an existing board.

        Pure batch: the ``footprints`` list carries every footprint-level
        argument (``footprint``, ``reference``, ``x``, ``y``, ``rotation``,
        ``nets``); there is no single-footprint mode and no tool-level
        defaults.  Items are placed one at a time and every item's result is
        collected; a failing item never rolls back or blocks the others —
        successful items are written to the board as they go.

        Reads the ``.kicad_mod`` file of each requested footprint, appends it
        as a new ``(footprint ...)`` node to the ``.kicad_pcb`` board at the
        given world position, and writes the board back after every item
        (atomic write plus a ``.bak`` backup).  The library file is only read
        — nothing in the library is modified.  Placement tools like
        ``set_footprint_position`` only move footprints already on the board;
        this tool adds new ones.

        Footprint resolution: ``footprint`` may be ``"Library:Name"`` or a
        bare ``"Name"`` searched across every library resolved from
        fp-lib-table (project table first, then the user table); there is no
        library restriction argument — each footprint resolves by its own
        ``Library:Name`` prefix or bare name.  The placed footprint's board
        header is ``"Library:Name"`` when the ``footprint`` argument carries
        the library prefix, else the bare name.

        Netting — required, per-pad: each item's ``nets`` argument maps every
        pad number (``"1"``, ``"2"``, ...) to that pad's net name.  A net name
        that does not exist in the board's ``(net ...)`` list is auto-added
        with the next free net number (max + 1) — friendlier than failing,
        and matches drawing-demo boards that have no nets.  An empty string
        (or ``None``) as the net name means net 0 (unconnected); the
        ``(net 0 "")`` node is appended if the board lacks one.  A pad NOT
        covered by an item's ``nets`` is a hard error for that item
        (``"missing net for pad(s): ..."``) and nothing is written for it —
        a partial nets dict can never silently land an uncovered pad on net 0
        and short the part.

        Validation — every item fails with an ``{"error": ...}`` WITHOUT
        touching the board file (only that item fails): unparseable
        ``pcb_path``; unresolvable ``footprint`` (the error lists the
        scanned libraries); ``reference`` already present on the board
        (duplicate reference); unsafe footprint names (path traversal);
        missing ``x``/``y``; ``nets`` not covering every pad.  Placement
        outside the board outline (Edge.Cuts) is a warning only — boards may
        legitimately have no outline, so it is never a hard failure.

        Args:
            pcb_path: Path to the ``.kicad_pcb`` board to modify.
            footprints: List of placement dicts, each with the footprint-
                level arguments (all required unless noted):
                ``footprint`` (``"Library:Name"`` or bare ``"Name"``),
                ``reference`` (unique board reference designator, e.g.
                ``"R9"``), ``x``/``y`` (anchor world mm, +Y down), ``nets``
                (pad number string → net name; ``""``/``None`` = net 0;
                MUST cover every pad of the footprint), and optional
                ``rotation`` (CCW-positive degrees, default 0.0).
            ctx: MCP context for progress reporting.

        Returns:
            dict with ``success`` (True only when every item was placed),
            ``results`` (one entry per item, in order: ``success``,
            ``reference`` and either ``result`` — the full placement dict
            with ``success``/``reference``/``footprint``/``placed_at``/
            ``rotation``/``backup_path``/``pad_count``/``pads_net`` and an
            optional ``warnings`` list — or ``error``), ``placed_count``,
            ``failed_count`` and ``failed`` (list of ``{"reference",
            "error"}`` per failed item).
        """
        if not footprints:
            return {"error": "footprints must be a non-empty list"}
        return await _place_many_footprints(pcb_path, footprints, ctx)

    @mcp.tool()
    async def remove_footprints_from_pcb(
        pcb_path: str,
        references: list[str],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove several footprints from an existing board by reference designator.

        Pure batch: ``references`` is the only designator input — there is no
        single-footprint mode.  Each listed reference is removed one at a
        time in list order and every item's result is collected; a failing
        item never blocks the others (nothing rolls back — items already
        removed stay removed).  The board is loaded once; each item removes
        whichever matching ``(footprint ...)`` nodes remain, so a reference
        listed twice removes the first occurrence and the second is reported
        as not found.

        The board is only written when at least one footprint was actually
        removed — one atomic save plus a ``.bak`` backup for the whole call —
        and an all-not-found batch leaves the file untouched and returns
        ``removed_count`` 0 with ``backup_path`` ``None``.  When written,
        ``success`` is True if and only if every item was removed.

        The board's ``(net ...)`` definitions are deliberately kept as-is
        after a removal: KiCad tolerates net definitions with no pad
        references (dangling nets), and rewriting the net table risks
        breaking net references of the footprints that stay.  Cleanup of
        now-unused nets is left to KiCad's normal board maintenance.

        Args:
            pcb_path: Path to the ``.kicad_pcb`` board to modify.
            references: List of board reference designators to remove, in
                order; each entry is removed once and a repeated entry then
                counts as not found.  Must be a non-empty list.
            ctx: MCP context for progress reporting.

        Returns:
            dict with ``success`` (True only when every item was removed),
            ``results`` (one entry per item, in order: dict with
            ``reference``, ``success``, ``removed`` (0 or 1) and ``error``
            when the item was not found or invalid), ``removed_count``,
            ``not_found_count``, ``not_found`` (list of ``{"reference",
            "error"}`` per failed item), ``backup_path`` (``None`` when
            nothing was written) and ``pcb_path``.
        """
        try:
            if not isinstance(references, list) or not references:
                return {"error": "references must be a non-empty list"}

            try:
                data = load_pcb(pcb_path)
            except (FileNotFoundError, ValueError, OSError) as exc:
                return {"error": f"cannot read board: {exc}"}
            if not data or not isinstance(data[0], sexpdata.Symbol) or _sym(data[0]) != "kicad_pcb":
                return {"error": f"{pcb_path} does not look like a .kicad_pcb file"}

            results: list[dict[str, Any]] = []
            not_found: list[dict[str, Any]] = []
            removed_total = 0
            for item in references:
                if not isinstance(item, str) or not item:
                    error = "reference must be a non-empty string"
                    ref_label = item if isinstance(item, str) else str(item)
                    results.append(
                        {"reference": ref_label, "success": False, "removed": 0, "error": error}
                    )
                    not_found.append({"reference": ref_label, "error": error})
                    continue
                n = 0
                for node in list(iter_footprint_nodes(data)):
                    if get_fp_property(node, "Reference") == item:
                        data.remove(node)
                        n += 1
                if n == 0:
                    error = f"footprint '{item}' not found on the board; nothing to remove"
                    results.append(
                        {"reference": item, "success": False, "removed": 0, "error": error}
                    )
                    not_found.append({"reference": item, "error": error})
                else:
                    results.append({"reference": item, "success": True, "removed": 1})
                    removed_total += 1

            backup_path: str | None = None
            if removed_total > 0:
                try:
                    backup_path = save_pcb(pcb_path, data)
                except OSError as exc:
                    return {"error": f"failed to write board: {exc}"}
            if ctx and removed_total > 0:
                await ctx.info(
                    f"Removed {removed_total} footprint(s) from {pcb_path}; "
                    f"{len(not_found)} not found"
                )
            return {
                "success": removed_total > 0 and not not_found,
                "results": results,
                "removed_count": removed_total,
                "not_found_count": len(not_found),
                "not_found": not_found,
                "backup_path": backup_path,
                "pcb_path": pcb_path,
            }
        except Exception as exc:
            log.error("remove_footprints_from_pcb failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @mcp.tool()
    async def find_footprints_not_in_libraries(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List board footprints that exist in no indexed footprint library.

        Read-only: compares each footprint the board references (its
        ``Library:Name`` pair, or bare ``Name`` for footprints stamped on the
        board) against the libraries actually available — project-local fp-
        lib-table, global user table, and indexed library database.  A
        footprint whose referenced library is missing (or whose name exists
        only in some *other* library) is reported as missing, since the board
        cannot resolve it; sync the footprint index first for the database
        to be authoritative.

        Output is consolidated per ``(library, name)``: each entry lists all
        board references using that footprint, so a population of e.g. ten
        identical resistors appears once rather than as ten same-shaped rows.

        Args:
            pcb_path: Absolute path to the ``.kicad_pcb`` file.  Its
                directory is treated as the project directory (project-local
                fp-lib-table and ``${KIPRJMOD}`` URIs are resolved from it).
            ctx: MCP context (unused).

        Returns:
            dict with ``missing`` (list of {name, library, references,
            reference_count, values}), ``missing_count``; plus ``error`` on
            failure.
        """
        try:
            _, footprints, _ = _collect_board_footprints(pcb_path)
            existing = _collect_existing_footprints(pcb_path)
            missing: list[dict[str, Any]] = []
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for fp in footprints:
                lib, name = fp["library"], fp["name"]
                if (lib, name) in existing:
                    continue
                key = (lib, name)
                entry = merged.get(key)
                if entry is None:
                    entry = {
                        "name": name,
                        "library": lib,
                        "references": [],
                        "values": [],
                    }
                    merged[key] = entry
                entry["references"].append(fp["reference"])
                if fp["value"] and fp["value"] not in entry["values"]:
                    entry["values"].append(fp["value"])
            for entry in merged.values():
                entry["reference_count"] = len(entry["references"])
                missing.append(entry)
            return {
                "missing": missing,
                "missing_count": len(missing),
            }
        except Exception as exc:
            log.error("find_footprints_not_in_libraries failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @mcp.tool()
    async def create_footprint_library(
        name: str,
        project_dir: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create and register a new footprint library.

        Global 3rdparty form (default): creates ``<name>.pretty`` under
        ``${KICAD10_3RD_PARTY}/footprints`` and registers it in the global
        user fp-lib-table.  Project form (``project_dir`` given): creates
        ``<project_dir>/<name>.pretty`` and registers it in the project's
        ``fp-lib-table`` (created if absent).  Either way the library is
        indexed immediately so library-list and search tools see it.

        Args:
            name: New library nickname (sanitized to fp-lib-table-safe
                characters).
            project_dir: Project directory for a project-local library
                (``${KIPRJMOD}`` URI).  Omit for a global 3rdparty library.
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
            # Global scope: nickname uniqueness is enforced across projects
            # anyway (library_name_exists), so any manager scope works.
            mgr = get_footprint_index_manager()
            if mgr.library_name_exists(nickname) or nickname in {
                lib["nickname"] for lib in build_effective_library_list(None)
            }:
                return {
                    "error": (
                        f"Library '{nickname}' already exists; "
                        "use add_footprints_to_library to export into it."
                    )
                }
            if project_dir:
                if not os.path.isdir(project_dir):
                    return {"error": f"Project directory not found: {project_dir}"}
                project_id = os.path.realpath(project_dir)
                library_dir = os.path.join(project_dir, f"{nickname}.pretty")
                uri = f"${{KIPRJMOD}}/{nickname}.pretty"
                table_path = os.path.join(project_dir, "fp-lib-table")
                project_scope: str = project_id
            else:
                library_dir = os.path.join(_3rd_party_footprints_dir(), f"{nickname}.pretty")
                uri = f"${{KICAD{config.kicad_version.split('.')[0]}_3RD_PARTY}}/footprints/{nickname}.pretty"
                table_path = get_user_fp_lib_table_path()
                project_scope = ""
            if os.path.exists(library_dir):
                return {
                    "error": (
                        f"Directory already exists, refusing to recreate: {library_dir}. "
                        "Use add_footprints_to_library to export into it."
                    )
                }
            os.makedirs(library_dir, exist_ok=False)
            result = register_library_in_table(
                table_path,
                nickname,
                uri,
                description=f"Created by KiCad MCP footprint export ({nickname})",
            )
            indexed = _index_library_entry(
                nickname, library_dir, raw_uri=uri, project_id=project_scope
            )
            return {
                "library": nickname,
                "path": library_dir,
                "table_path": table_path,
                "registered": bool(result.get("registered")),
                "indexed": indexed,
            }
        except Exception as exc:
            log.error("create_footprint_library failed: %s", exc, exc_info=True)
            # Roll back the empty directory we just created, otherwise a
            # retry is refused ("Directory already exists") while the library
            # is also absent from fp-lib-table — an unrecoverable dead end.
            if "library_dir" in locals() and os.path.isdir(library_dir):
                try:
                    os.rmdir(library_dir)
                except OSError:
                    pass  # non-empty (indexed some footprints?) — leave for the user
            return {"error": str(exc)}

    @mcp.tool()
    async def add_footprints_to_library(
        pcb_path: str,
        footprints: list[str],
        library: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Export explicitly named board footprints into a footprint library.

        Writes each requested footprint (as reported by
        ``find_footprints_not_in_libraries``) into the target library directory as a
        ``.kicad_mod`` file, then updates the footprint database for exactly
        that library.  Only the listed footprints are considered — there is
        deliberately no "export every missing footprint" mode; call
        ``find_footprints_not_in_libraries`` first to see what is missing.  The board
        file is never modified.

        A requested footprint is ``failed`` when its target file already
        exists in the library directory (never overwritten), and ``skipped``
        when the exact ``(library, name)`` pair the board references is
        already indexed, or when the name is not on the board at all.

        Args:
            pcb_path: Absolute path to the ``.kicad_pcb`` file.  Its
                directory is treated as the project directory (project-local
                fp-lib-table and ``${KIPRJMOD}`` URIs are resolved from it).
            footprints: Names of the footprints to export, as returned by
                ``find_footprints_not_in_libraries``.
            library: Nickname of the target library, as registered in
                fp-lib-table.
            ctx: MCP context for progress reporting.

        Returns:
            dict with ``library``, ``library_path``, ``exported`` (list of
            paths), ``exported_count``, ``failed`` (list of {name, reason} —
            target file already exists, not overwritten), ``failed_count``,
            ``skipped`` (list of {name, reason}), ``skipped_count``,
            ``indexed``; plus ``error`` on failure.
        """
        try:
            nodes, _, version = _collect_board_footprints(pcb_path)
            library_dir, target_table = _resolve_library_dir(library, pcb_path)
            # The target library's ownership decides the index scope: a
            # project-local fp-lib-table library is indexed under the project,
            # a global one under "".
            project_id = normalize_project_id(pcb_path)
            project_table = os.path.join(project_id, "fp-lib-table")
            target_project = project_id if os.path.realpath(target_table) == project_table else ""
            # Same source of truth as find_footprints_not_in_libraries: the
            # (library, name) pairs in the index DB (project-scoped), with a
            # live scan only when the index is empty.  A *name* is skipped
            # only when the exact pair the board references is already
            # available — a same-named footprint in some other library does
            # not satisfy the board's reference.
            existing = _collect_existing_footprints(pcb_path)
            board_names = {name for _, name in (split_footprint_header(node) for node in nodes)}
            if ctx:
                await ctx.info(
                    f"Exporting missing footprints to {library_dir} "
                    f"({len(footprints)} requested, {len(existing)} indexed footprints)"
                )

            exported: list[str] = []
            failed: list[dict[str, str]] = []
            skipped: list[dict[str, str]] = []
            # Deterministic instance selection: when the board carries several
            # footprints with the same name (e.g. 48x CAPC), export the
            # front-side one when it exists (library parts are front-side
            # form, so a B.Cu instance would need a flip that the F.Cu copy
            # already matches), else the first instance in board order.
            node_by_name: dict[str, list[Any]] = {}
            for node in nodes:
                name = split_footprint_header(node)[1]
                current = node_by_name.get(name)
                if current is None or (
                    get_fp_layer(current) != "F.Cu" and get_fp_layer(node) == "F.Cu"
                ):
                    node_by_name[name] = node
            board_pair = {
                name: (split_footprint_header(node)[0] or "", name)
                for name, node in node_by_name.items()
            }
            for name in dict.fromkeys(footprints):  # dedupe, keep order
                if name not in board_names:
                    skipped.append({"name": name, "reason": "not on board"})
                    continue
                node = node_by_name[name]
                # Path-traversal guard: the name comes from the board file, and
                # the target path is built from it.  Report rather than touch
                # the filesystem with an unsafe name.
                if not is_safe_footprint_name(name):
                    failed.append(
                        {
                            "name": name,
                            "reason": f"unsafe footprint name {name!r} (refusing to write)",
                        }
                    )
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
                if board_pair[name] in existing:
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

            indexed = _index_library_entry(library, library_dir, project_id=target_project)
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
            log.error("add_footprints_to_library failed: %s", exc, exc_info=True)
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


def _resolve_library_dir(library: str, pcb_path: str | None) -> tuple[str, str]:
    """Resolve a registered library nickname to its ``.pretty`` directory.

    :returns: ``(lib_dir, table_path)`` — the resolved .pretty directory and
        the fp-lib-table file it was registered in (project table or global).
    :raises ValueError: When the nickname is not in fp-lib-table, resolves to
        a missing directory, or is read-only.
    """
    libs = build_effective_library_list(pcb_path)
    by_nickname = {lib["nickname"]: lib for lib in libs}
    if library not in by_nickname:
        raise ValueError(
            f"Library '{library}' not found in fp-lib-table. "
            "Create it first with create_footprint_library."
        )
    lib_dir = by_nickname[library].get("uri", "")
    table_path = by_nickname[library].get("table_path", "")
    if not lib_dir or not os.path.isdir(lib_dir):
        raise ValueError(f"Library '{library}' resolves to a missing directory: {lib_dir}")
    if not os.access(lib_dir, os.W_OK):
        raise ValueError(
            f"Library '{library}' is read-only or not writable: {lib_dir}. "
            "Pick a writable library or create a new one."
        )
    return lib_dir, table_path


def _fmt_library_list(libs: list[dict[str, str]]) -> str:
    """Render a library list for error messages: ``nick -> uri`` pairs."""
    if not libs:
        return "(no fp-lib-table libraries found)"
    return "; ".join(f"{lib['nickname']} -> {lib['uri']}" for lib in libs)


def _find_footprint_mod_path(
    footprint: str,
    library: str | None,
    pcb_path: str | None,
) -> tuple[str, str, list[dict[str, str]]]:
    """Resolve ``footprint`` to the ``.kicad_mod`` file to place on a board.

    ``footprint`` may be ``"Library:Name"`` or a bare ``"Name"``.  When bare,
    the search covers every library from ``build_effective_library_list``
    unless ``library`` restricts it.  ``library`` accepts a registered
    nickname, an existing ``.pretty`` directory path, or a directory name
    (``"MyLib"`` / ``"MyLib.pretty"``).

    :param footprint: ``"Library:Name"`` or bare footprint name.
    :param library: Optional restriction (nickname, dir path, or dir name).
    :param pcb_path: Board path scoping project fp-lib-table resolution.
    :returns: ``(header, mod_path, scanned)`` — the board header to write
        (``"Library:Name"`` when the caller named the library, else the bare
        name), the resolved ``.kicad_mod`` path, and the effective library
        list for error messages.
    :raises ValueError: On unsafe names, unknown library, or unknown footprint
        (the message lists the libraries searched).
    """
    if ":" in footprint:
        lib_name, _, fp_name = footprint.rpartition(":")
    else:
        lib_name, fp_name = None, footprint

    if not fp_name or not is_safe_footprint_name(fp_name):
        raise ValueError(
            f"unsafe footprint name {footprint!r} (refusing to place); "
            "footprint must be 'Library:Name' or 'Name'"
        )

    libs = build_effective_library_list(pcb_path)
    lib_by_nick = {lib["nickname"]: lib for lib in libs}

    # Candidate library directories as (label, dir) pairs.
    candidates: list[tuple[str, str]] = []
    if library:
        if library in lib_by_nick:
            candidates.append((library, lib_by_nick[library]["uri"]))
        elif os.path.isdir(library):
            candidates.append((library, os.path.abspath(library)))
        else:
            for nick, lib in lib_by_nick.items():
                base = os.path.basename(lib["uri"])
                if base == library or base == library + ".pretty":
                    candidates.append((nick, lib["uri"]))
            if not candidates:
                raise ValueError(
                    f"library {library!r} not found (expected a registered nickname, "
                    f"a .pretty directory path, or a directory name); scanned libraries: "
                    f"{_fmt_library_list(libs)}"
                )
    if lib_name is not None:
        if library and library not in lib_by_nick and os.path.isdir(library):
            raise ValueError(
                f"footprint '{footprint}' names library '{lib_name}' but library= "
                f"is a directory path ({library}); pick one"
            )
        if lib_name not in lib_by_nick:
            raise ValueError(
                f"library '{lib_name}' not found in fp-lib-table; scanned libraries: "
                f"{_fmt_library_list(libs)}"
            )
        candidates = [(lib_name, lib_by_nick[lib_name]["uri"])]
    elif not candidates:  # bare name, no restriction: search everything
        candidates = [(lib["nickname"], lib["uri"]) for lib in libs]

    for label, lib_dir in candidates:
        if not lib_dir or not os.path.isdir(lib_dir):
            continue
        mod_candidate = os.path.join(lib_dir, f"{fp_name}.kicad_mod")
        if os.path.isfile(mod_candidate):
            header = f"{lib_name}:{fp_name}" if lib_name else fp_name
            return header, mod_candidate, libs

    raise ValueError(
        f"footprint '{footprint}' not found in the scanned libraries: "
        f"{_fmt_library_list([{'nickname': label, 'uri': lib_dir} for label, lib_dir in candidates])}"
    )


def _resolve_board_net(data: list[Any], name: str | None) -> tuple[int, str]:
    """Return ``(net_number, net_name)`` to assign to a new footprint's pads.

    With a *name*: the number of the matching ``(net N "name")`` node, or the
    name is auto-added with number max+1 (1 when the board has no nets).
    With ``None``: ``(0, "")`` — KiCad net 0, unconnected — appending a
    ``(net 0 "")`` node when the board does not have one.
    """
    nets: list[tuple[int, str]] = []
    for item in data:
        if not (isinstance(item, list) and len(item) >= 3):
            continue
        if not (isinstance(item[0], sexpdata.Symbol) and _sym(item[0]) == "net"):
            continue
        try:
            nets.append((int(item[1]), _sym(item[2])))
        except (TypeError, ValueError):
            continue

    if name is None:
        if not any(no == 0 for no, _ in nets):
            data.append([sexpdata.Symbol("net"), 0, ""])
        return 0, ""
    for net_no, net_name in nets:
        if net_name == name:
            return net_no, name
    next_no = max((net_no for net_no, _ in nets), default=0) + 1
    data.append([sexpdata.Symbol("net"), next_no, name])
    return next_no, name


def _apply_nets_to_pads(
    fp_node: list[Any],
    pad_nets: dict[str, tuple[int, str]],
    default_net: tuple[int, str],
) -> tuple[int, list[dict[str, str]]]:
    """Assign per-pad ``(net ...)`` nodes to *fp_node* in place.

    Pads whose number is a key of *pad_nets* get that pad's
    ``(net_no, net_name)``; every other pad gets *default_net* — the
    per-pad fallback of the ``add_footprints_to_pcb`` netting.  Replaces
    pre-existing net sub-nodes.  Returns ``(pad_count, pad_net_list)`` where
    *pad_net_list* maps each pad number (in pad order) to its assigned net
    name (``""`` for net 0).
    """
    count = 0
    pad_net_list: list[dict[str, str]] = []
    for child in fp_node:
        if not (isinstance(child, list) and len(child) > 0):
            continue
        if not (isinstance(child[0], sexpdata.Symbol) and _sym(child[0]) == "pad"):
            continue
        pad_no = _sym(child[1]) if len(child) > 1 else ""
        net_no, net_name = pad_nets.get(pad_no, default_net)
        net_node = [sexpdata.Symbol("net"), net_no, net_name]
        for i, sub in enumerate(child):
            if isinstance(sub, list) and len(sub) >= 1 and _sym(sub[0]) == "net":
                child[i] = net_node
                break
        else:
            child.append(net_node)
        count += 1
        pad_net_list.append({"pad": pad_no, "net": net_name})
    return count, pad_net_list


def _outline_warning(data: list[Any], x: float, y: float) -> str | None:
    """Return a warning string when (x, y) lies outside the board outline.

    Conservative bounding-box check over the Edge.Cuts graphic items; boards
    without an outline (or without usable geometry) return ``None``.  This is
    deliberately warn-only — outline-less boards are legal.
    """
    items = get_edge_cuts_items(data)
    if not items:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for item in items:
        for key, target in (("x1", xs), ("x2", xs), ("start_x", xs), ("mid_x", xs), ("end_x", xs)):
            if key in item:
                target.append(item[key])
        for key, target in (("y1", ys), ("y2", ys), ("start_y", ys), ("mid_y", ys), ("end_y", ys)):
            if key in item:
                target.append(item[key])
        for key, target in (("cx", xs), ("cy", ys), ("ex", xs), ("ey", ys)):
            if key in item:
                target.append(item[key])
    if not xs or not ys:
        return None
    eps = 1e-6
    if x < min(xs) - eps or x > max(xs) + eps or y < min(ys) - eps or y > max(ys) + eps:
        return (
            f"placement ({x:.2f}, {y:.2f}) lies outside the board outline bounding "
            f"box ({min(xs):.2f}, {min(ys):.2f}) - ({max(xs):.2f}, {max(ys):.2f})"
        )
    return None


def _collect_existing_footprints(pcb_path: str | None) -> set[tuple[str, str]]:
    """Return every ``(library, footprint_name)`` pair that already exists in
    libraries — the *same-name-in-any-library* answer of the inherited tools
    is deliberately not used here: a name alone cannot tell whether the
    footprint the board references (``Library:Name``) is actually available.

    Prefers the footprint index database — but only when the current project's
    sync has actually completed: a project whose sync never ran (or is still
    running, or failed, or finished for a different project) cannot be
    trusted, so the live fp-lib-table scan is used instead.  This avoids
    trusting a partially-synced or stale database.

    *pcb_path* may be ``.kicad_pro`` or ``.kicad_pcb``; the project identity
    is ``normalize_project_id(pcb_path)`` — the same canonical id
    ``sync_footprint_index`` stores in ``_fp_sync_state.last_project_path``.
    """
    existing: set[tuple[str, str]] = set()
    try:
        with _fp_sync_lock:
            running = _fp_sync_state.running
            last_result = _fp_sync_state.last_result
            last_project = _fp_sync_state.last_project_path
        if (
            running
            or not last_result
            or not last_result.get("success")
            or last_project != normalize_project_id(pcb_path)
        ):
            existing = _live_scan_existing_footprints(pcb_path)
        else:
            mgr = get_footprint_index_manager(project_path=pcb_path)
            existing = mgr.get_all_library_footprints()
    except Exception as exc:
        log.warning("Footprint index read failed (%s) — falling back to live scan", exc)
        existing = _live_scan_existing_footprints(pcb_path)
    return existing


def _live_scan_existing_footprints(pcb_path: str | None) -> set[tuple[str, str]]:
    """Live-scan fallback: every ``(library, footprint_name)`` pair across the
    effective library list (project-local table plus global user table,
    ``${KIPRJMOD}`` / ``KICAD*`` URI expanded).  Purely in-memory — nothing
    is written to the footprint database.
    """
    pairs: set[tuple[str, str]] = set()
    for lib in build_effective_library_list(pcb_path):
        uri = lib.get("uri", "")
        nickname = lib.get("nickname") or ""
        if uri and os.path.isdir(uri):
            for name in scan_footprint_library(uri):
                pairs.add((nickname, name))
    return pairs


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
    project_id: str = "",
) -> int:
    """Index exactly one library directory into the footprint database.

    Narrow update (no full-table traversal); the target library is indexed
    with *project_id* ("") = global, non-empty = project-local).  Returns the
    number of footprints stored, or -1 on failure.
    """
    try:
        # Ownership is passed explicitly to index_library — the singleton's
        # own scope is irrelevant here, so fetch it unscoped.
        return get_footprint_index_manager().index_library(
            library,
            library_dir,
            raw_uri=raw_uri,
            project=project_id,
        )
    except Exception as exc:
        log.error("Footprint index update failed for %s: %s", library, exc, exc_info=True)
        return -1
