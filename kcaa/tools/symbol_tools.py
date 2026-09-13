"""
Symbol library tools for KiCad MCP server.

Provides tools to index, search, and look up KiCad symbol libraries
using a SQLite-backed full-text search index.
"""

import contextlib
from dataclasses import dataclass
import logging
import threading
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.utils.config import ServerConfig
from kcaa.utils.symbol_extractor import extract_lib_symbol_raw
from kcaa.utils.symbol_geometry import compute_unit_bboxes
from kcaa.utils.symbol_index_manager import SymbolIndexManager
from kcaa.utils.symbol_index_reader import SymbolIndexReader

log = logging.getLogger(__name__)


def _lib_angle_to_direction(angle_deg: int) -> str:
    """Convert a library-space pin angle to a human-readable wire-exit direction.

    KiCad .kicad_sym files store pin angles as the **stub direction** — the
    direction from the pin tip *toward* the symbol body.  To get the wire-exit
    direction (from body *outward* to the pin tip) we add 180°.

    The library coordinate system is Y-up with CCW angles:
      raw 0°   (stub right  → body) → exit left  → after +180° = 180° → "left"
      raw 90°  (stub up     → body) → exit down  → after +180° = 270° → "down"
      raw 180° (stub left   → body) → exit right → after +180° =   0° → "right"
      raw 270° (stub down   → body) → exit up    → after +180° =  90° → "up"

    Examples with real parts:
      USB-C right-side pin (wire exits right): lib stores 180° → "right" ✓
      Resistor pin at top  (wire exits up):    lib stores 270° → "up"    ✓
    """
    # Add 180° to convert tip-to-body stub direction → body-to-tip exit direction.
    a = (int(round(float(angle_deg))) + 180) % 360
    return {0: "right", 90: "up", 180: "left", 270: "down"}.get(a, f"{a}deg")


# Module-level singleton so the DB connection is reused across tool calls.
_index_manager: SymbolIndexManager | None = None


def _get_index_manager() -> SymbolIndexManager:
    global _index_manager
    if _index_manager is None:
        config = ServerConfig()
        library_reader = SymbolIndexReader(config)
        _index_manager = SymbolIndexManager(library_reader)
    return _index_manager


# ---------------------------------------------------------------------------
# Background sync state (thread-safe via lock)
# ---------------------------------------------------------------------------


@dataclass
class _SyncState:
    running: bool = False
    current: int = 0
    total: int = 0
    current_library: str = ""
    last_result: dict | None = None
    error: str | None = None


_sync_state = _SyncState()
_sync_lock = threading.Lock()


def _run_sync_in_background(force: bool) -> None:
    """Target function executed in the background sync thread."""

    def _progress(current: int, total: int, library_name: str) -> None:
        with _sync_lock:
            _sync_state.current = current
            _sync_state.total = total
            _sync_state.current_library = library_name

    try:
        mgr = _get_index_manager()
        stats = mgr.sync(force=force, progress_callback=_progress)
        result = {
            "success": True,
            "added": stats.added,
            "updated": stats.updated,
            "removed": stats.removed,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "total_symbols": stats.total_symbols,
            "elapsed_seconds": round(stats.elapsed_seconds, 2),
        }
        with _sync_lock:
            _sync_state.last_result = result
            _sync_state.error = None
    except Exception as e:
        log.error(f"Background symbol index sync failed: {e}", exc_info=True)
        with _sync_lock:
            _sync_state.last_result = None
            _sync_state.error = str(e)
    finally:
        with _sync_lock:
            _sync_state.running = False
            _sync_state.current_library = ""


def _load_lib_symbol_raw(library_name: str, symbol_name: str):
    """Look up + extract the raw lib-symbol S-expression, or return (None, error)."""
    mgr = _get_index_manager()
    sym_rec = mgr.get_symbol(library_name, symbol_name)
    if sym_rec is None:
        return None, f"Symbol '{library_name}:{symbol_name}' not found in index."
    lib_rec = mgr.get_library_by_name(library_name)
    if lib_rec is None:
        return None, f"Library '{library_name}' not found in index."
    try:
        raw = extract_lib_symbol_raw(
            lib_rec.file_path,
            sym_rec.file_index,
            symbol_name,
            lib_rec.mtime,
            lib_rec.file_size,
        )
        return raw, None
    except Exception as exc:
        return None, f"Failed to read library file: {exc}"


def _bbox_summary(lib_sym_raw: list) -> dict | None:
    """Return body bbox info for the canonical (unit-1, style-1) view, or None.

    The dict contains the unit-1 ``body_bbox`` (library Y-up coords, mm,
    suitable for placement-clearance reasoning), the symbol's ``unit_count``,
    and per-unit body bboxes under ``unit_bboxes`` for multi-unit symbols.
    """
    try:
        bboxes = compute_unit_bboxes(lib_sym_raw)
    except Exception as e:
        log.debug("bbox computation failed: %s", e)
        return None
    if not bboxes:
        return None
    primary = bboxes.get(1) or next(iter(bboxes.values()))
    return {
        "body_bbox": primary.to_dict(),
        "unit_count": len(bboxes),
        "unit_bboxes": {str(u): bb.to_dict() for u, bb in sorted(bboxes.items())},
    }


def _parse_lib_pins(lib_sym_raw: list) -> list[dict]:
    """Parse pin definitions from a raw lib symbol S-expression list.

    Walks every sub-symbol (``SYMNAME_UNIT_STYLE`` children) and collects each
    ``(pin type shape (at x y angle) (length l) (name N ...) (number M ...))``
    entry. Returns one dict per pin:

    ``{"number": str, "name": str, "type": str, "direction": str}``
    """
    sym_name = lib_sym_raw[1]
    prefix = sym_name + "_"
    pins: list[dict] = []
    seen_numbers: set[str] = set()

    def _walk(children: list) -> None:
        for entry in children:
            if not (
                isinstance(entry, list)
                and len(entry) >= 1
                and isinstance(entry[0], sexpdata.Symbol)
            ):
                continue
            tag = entry[0].value()
            if tag == "pin":
                # (pin <type> <shape> (at x y angle) (length l) (name N ...) (number M ...))
                pin_type = (
                    entry[1].value()
                    if len(entry) > 1 and hasattr(entry[1], "value")
                    else str(entry[1])
                )
                angle = 0
                pin_name = ""
                pin_number = ""
                for child in entry[2:]:
                    if not (
                        isinstance(child, list)
                        and len(child) >= 1
                        and isinstance(child[0], sexpdata.Symbol)
                    ):
                        continue
                    ctag = child[0].value()
                    if ctag == "at" and len(child) >= 4:
                        with contextlib.suppress(ValueError, TypeError):
                            angle = int(round(float(child[3])))
                    elif ctag == "name" and len(child) >= 2:
                        pin_name = str(child[1])
                    elif ctag == "number" and len(child) >= 2:
                        pin_number = str(child[1])
                if pin_number and pin_number not in seen_numbers:
                    seen_numbers.add(pin_number)
                    angle_norm = angle % 360
                    pins.append(
                        {
                            "number": pin_number,
                            "name": pin_name,
                            "type": pin_type,
                            "direction": _lib_angle_to_direction(angle_norm),
                        }
                    )
            elif tag == "symbol" and isinstance(entry[1], str) and entry[1].startswith(prefix):
                _walk(entry[2:])

    _walk(lib_sym_raw[2:])
    pins.sort(key=lambda p: p["number"])
    return pins


def register_symbol_tools(mcp: FastMCP) -> None:
    """Register symbol library tools with the MCP server."""

    @mcp.tool()
    async def sync_symbol_index(force: bool = False, ctx: Context | None = None) -> dict[str, Any]:
        """
        Start syncing the symbol index database with the current KiCad symbol libraries.

        This tool returns immediately — the actual sync runs in a background thread
        to avoid tool call timeouts. The first sync can take several minutes because
        it parses all .kicad_sym files. Subsequent calls are incremental.

        After calling this tool, use get_symbol_sync_status to monitor progress and
        check when the sync completes. Do NOT call sync_symbol_index again while a
        sync is already running.

        Args:
            force: If True, reparse every library regardless of whether it changed. This can take
                   a long time to complete. Use force=True only when the database is messed up.
        """
        with _sync_lock:
            if _sync_state.running:
                return {
                    "status": "already_running",
                    "message": "A sync is already in progress. Use get_symbol_sync_status to check progress.",
                    "current": _sync_state.current,
                    "total": _sync_state.total,
                    "current_library": _sync_state.current_library,
                }
            # Mark as running and reset state before spawning the thread
            _sync_state.running = True
            _sync_state.current = 0
            _sync_state.total = 0
            _sync_state.current_library = ""
            _sync_state.error = None

        if ctx:
            await ctx.info("Starting symbol index sync in background thread...")

        t = threading.Thread(target=_run_sync_in_background, args=(force,), daemon=True)
        t.start()
        log.info("Background symbol sync thread started.")
        return {
            "status": "started",
            "message": "Symbol index sync started in the background. "
            "Call get_symbol_sync_status to monitor progress.",
        }

    @mcp.tool()
    async def get_symbol_sync_status(ctx: Context | None = None) -> dict[str, Any]:
        """
        Return the current status of the background symbol index sync.

        Call this after sync_symbol_index to monitor progress. Poll every few
        seconds until 'running' is False. When running is False and last_result
        is present, the sync completed successfully. When running is False and
        error is present, the sync failed.
        """
        with _sync_lock:
            state = {
                "running": _sync_state.running,
                "current": _sync_state.current,
                "total": _sync_state.total,
                "current_library": _sync_state.current_library,
                "last_result": _sync_state.last_result,
                "error": _sync_state.error,
            }
        return state

    @mcp.tool()
    async def search_symbols(
        query: str,
        limit: int = 50,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Full-text search across all indexed KiCad symbols.

        Searches symbol name, description, and keywords. Results are ordered
        by relevance. Run sync_symbol_index first if the index is empty.

        Args:
            query: Search string (e.g. "resistor", "NPN transistor", "STM32").
            limit: Maximum number of results to return (default 50).

        Returns:
            A dict with a ``symbols`` list. Each entry has:
            - ``library_name``: the exact value to pass as ``library_name`` to
              ``add_symbol_to_schematic``, ``get_symbol``, or
              ``get_library_symbols``.  For KiCad 10 symdir-style libraries
              this is ``"TableName/FileBaseName"`` (e.g. ``"Device/R_Small"``),
              not just the table name (e.g. not ``"Device"``).
            - ``name``: the exact value to pass as ``symbol_name``.
        """
        try:
            mgr = _get_index_manager()
            results = mgr.search_symbols(query, limit=limit)
            return {
                "success": True,
                "query": query,
                "count": len(results),
                "symbols": [
                    {
                        "library_name": s.library_name,
                        "name": s.symbol_name,
                        "description": s.description,
                        "keywords": s.keywords,
                        "pin_count": s.pin_count,
                    }
                    for s in results
                ],
            }
        except Exception as e:
            log.error(f"Symbol search failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_symbol(
        library_name: str,
        symbol_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Look up a single KiCad symbol by library and symbol name.

        Returns symbol metadata plus a ``body_bbox`` describing the symbol's
        extent in **library coordinate space** (Y-up, mm). The bbox is the
        union of graphic primitives and pin connection points, so it is
        suitable for placement-clearance reasoning. For multi-unit symbols,
        ``unit_bboxes`` maps each unit number to its own bbox; the top-level
        ``body_bbox`` is the unit-1 bbox.

        To get the bbox of a *placed* symbol in schematic (Y-down) world
        coordinates, use ``extract_schematic_netlist``.

        Args:
            library_name: The library name returned by ``search_symbols`` in the
                ``library_name`` field.  For KiCad 10 symdir-style libraries
                this is ``"TableName/FileBaseName"`` (e.g. ``"Device/R_Small"``)
                not just the table name (e.g. not ``"Device"``).
            symbol_name:  The symbol name within the library (e.g. "R").
        """
        try:
            mgr = _get_index_manager()
            symbol = mgr.get_symbol(library_name, symbol_name)
            if symbol is None:
                return {
                    "success": False,
                    "error": f"Symbol '{library_name}:{symbol_name}' not found in index.",
                }
            result: dict[str, Any] = {
                "success": True,
                "library_name": symbol.library_name,
                "name": symbol.symbol_name,
                "description": symbol.description,
                "keywords": symbol.keywords,
                "pin_count": symbol.pin_count,
            }
            raw, err = _load_lib_symbol_raw(library_name, symbol_name)
            if raw is not None:
                summary = _bbox_summary(raw)
                if summary is not None:
                    result.update(summary)
            return result
        except Exception as e:
            log.error(f"get_symbol failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def list_symbol_libraries(
        table: str | None = None,
        limit: int = 200,
        offset: int = 0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        List KiCad symbol libraries from the index.

        **Always prefer** ``search_symbols`` when looking for a specific
        component — it searches names, descriptions, and keywords across all
        libraries in one call and is far more token-efficient.

        Use this tool only to **browse** the library tree:

        - No arguments → returns a compact list of *table* names (the
          top-level groupings such as ``"Device"``, ``"Regulator_Linear"``,
          ``"power"``) with aggregated symbol counts.  Typically ~200 entries.
        - ``table=<name>`` → returns individual libraries inside that table
          (e.g. ``table="Device"`` lists ``"Device/R"``, ``"Device/C"``, …).
          Use ``limit`` and ``offset`` to page through large tables.

        Args:
            table:  Table name to drill into (e.g. ``"Regulator_Linear"``).
                    Omit to get the top-level table summary.
            limit:  Maximum entries to return (default 200, max 500).
            offset: 0-based entry offset for pagination (default 0).
        """
        try:
            limit = min(max(1, limit), 500)
            offset = max(0, offset)
            mgr = _get_index_manager()
            libraries = mgr.get_all_libraries()

            if table is None:
                # Aggregate into table-level summaries.
                table_counts: dict[str, int] = {}
                for lib in libraries:
                    tbl = lib.library_name.split("/")[0]
                    table_counts[tbl] = table_counts.get(tbl, 0) + lib.symbol_count
                tables_sorted = sorted(table_counts.items())
                page = tables_sorted[offset : offset + limit]
                return {
                    "success": True,
                    "mode": "tables",
                    "total": len(tables_sorted),
                    "offset": offset,
                    "limit": limit,
                    "truncated": (offset + limit) < len(tables_sorted),
                    "tables": [{"name": t, "symbol_count": c} for t, c in page],
                }
            else:
                # Filter to the requested table.
                prefix = table + "/"
                # Support both symdir style ("Device/R") and flat style ("Device")
                matching = [
                    lib
                    for lib in libraries
                    if lib.library_name.startswith(prefix) or lib.library_name == table
                ]
                page = matching[offset : offset + limit]
                return {
                    "success": True,
                    "mode": "libraries",
                    "table": table,
                    "total": len(matching),
                    "offset": offset,
                    "limit": limit,
                    "truncated": (offset + limit) < len(matching),
                    "libraries": [
                        {
                            "name": lib.library_name,
                            "symbol_count": lib.symbol_count,
                        }
                        for lib in page
                    ],
                }
        except Exception as e:
            log.error(f"list_symbol_libraries failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_library_symbols(
        library_name: str,
        limit: int = 50,
        offset: int = 0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Return symbols in a specific KiCad symbol library.

        **Prefer** ``search_symbols`` when looking for a specific component.
        Use this tool only to enumerate the contents of a *known* library.

        Results are ordered by position in the library file.  Use ``limit``
        and ``offset`` to page through large libraries.

        Args:
            library_name: The library name as returned by ``search_symbols`` or
                ``list_symbol_libraries`` in the ``library_name`` field.  For
                KiCad 10 symdir-style libraries this is
                ``"TableName/FileBaseName"`` (e.g. ``"Device/R_Small"``), not
                just the table name (e.g. not ``"Device"``).
            limit:  Maximum symbols to return (default 50, max 200).
            offset: 0-based symbol offset for pagination (default 0).
        """
        try:
            limit = min(max(1, limit), 200)
            offset = max(0, offset)
            mgr = _get_index_manager()
            symbols = mgr.get_library_symbols(library_name)
            if not symbols:
                return {
                    "success": False,
                    "error": f"Library '{library_name}' not found or has no indexed symbols.",
                }
            page = symbols[offset : offset + limit]
            return {
                "success": True,
                "library_name": library_name,
                "total": len(symbols),
                "offset": offset,
                "limit": limit,
                "truncated": (offset + limit) < len(symbols),
                "symbols": [
                    {
                        "name": s.symbol_name,
                        "description": s.description,
                        "keywords": s.keywords,
                        "pin_count": s.pin_count,
                    }
                    for s in page
                ],
            }
        except Exception as e:
            log.error(f"get_library_symbols failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_symbol_index_stats(ctx: Context | None = None) -> dict[str, Any]:
        """
        Return summary statistics about the symbol index database.

        Shows how many libraries and symbols are indexed, when the last sync
        ran, and where the database file is located.
        """
        try:
            mgr = _get_index_manager()
            stats = mgr.get_statistics()
            return {
                "success": True,
                "library_count": stats.library_count,
                "symbol_count": stats.symbol_count,
                "last_sync": stats.last_sync,
                "db_path": stats.db_path,
            }
        except Exception as e:
            log.error(f"get_symbol_index_stats failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_symbol_pins(
        library_name: str,
        symbol_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return detailed pin information for a KiCad library symbol.

        Reads the full pin definitions from the .kicad_sym library file and
        returns one entry per pin with its number, name, electrical type, and
        wire-exit direction in **library coordinate space** (Y-up, before any
        placement rotation or mirroring on a schematic).

        Direction convention (library Y-up space, wire-exit direction):
        - "right" = wire exits right (→)
        - "up"    = wire exits up    (↑)
        - "left"  = wire exits left  (←)
        - "down"  = wire exits down  (↓)

        Note: KiCad .kicad_sym files store the raw angle as the stub direction
        (tip-to-body).  This tool converts that to the wire-exit direction
        (body-to-tip) automatically by adding 180°.

        To get the absolute world-space exit directions after a symbol has been
        placed on a schematic use ``extract_schematic_netlist`` and look at
        the ``direction`` field in each component's ``pins`` list.

        Args:
            library_name: The library name as returned by ``search_symbols``
                (e.g. ``"Device/R"`` for KiCad 10 symdir-style libraries).
            symbol_name: The symbol name within the library (e.g. ``"R"``).

        Returns:
            dict with keys: success, library_name, symbol_name, pin_count,
            pins (list of {number, name, type, direction}), body_bbox
            (library Y-up clearance bbox), unit_count, unit_bboxes
            (per-unit bboxes when multi-unit).
        """
        try:
            lib_sym_raw, err = _load_lib_symbol_raw(library_name, symbol_name)
            if lib_sym_raw is None:
                return {"success": False, "error": err}

            pins = _parse_lib_pins(lib_sym_raw)
            result: dict[str, Any] = {
                "success": True,
                "library_name": library_name,
                "symbol_name": symbol_name,
                "pin_count": len(pins),
                "pins": pins,
            }
            summary = _bbox_summary(lib_sym_raw)
            if summary is not None:
                result.update(summary)
            return result
        except Exception as e:
            log.error(f"get_symbol_pins failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
