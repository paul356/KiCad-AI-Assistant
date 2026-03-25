"""
Symbol library tools for KiCad MCP server.

Provides tools to index, search, and look up KiCad symbol libraries
using a SQLite-backed full-text search index.
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp import Context

from kicad_mcp.config import LibraryPathConfig
from kicad_mcp.utils.symbol_index_reader import SymbolIndexReader
from kicad_mcp.utils.symbol_index_manager import SymbolIndexManager

log = logging.getLogger(__name__)

# Module-level singleton so the DB connection is reused across tool calls.
_index_manager: SymbolIndexManager | None = None


def _get_index_manager() -> SymbolIndexManager:
    global _index_manager
    if _index_manager is None:
        config = LibraryPathConfig()
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
    current_library: str = ''
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
            _sync_state.current_library = ''


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
            _sync_state.current_library = ''
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
            return {
                "success": True,
                "library_name": symbol.library_name,
                "name": symbol.symbol_name,
                "description": symbol.description,
                "keywords": symbol.keywords,
                "pin_count": symbol.pin_count,
            }
        except Exception as e:
            log.error(f"get_symbol failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def list_symbol_libraries(ctx: Context | None = None) -> dict[str, Any]:
        """
        List all KiCad symbol libraries currently in the index.

        Returns library names, file paths, symbol counts, and KiCad version.
        Run sync_symbol_index first if the list is empty.
        """
        try:
            mgr = _get_index_manager()
            libraries = mgr.get_all_libraries()
            return {
                "success": True,
                "count": len(libraries),
                "libraries": [
                    {
                        "name": lib.library_name,
                        "path": lib.file_path,
                        "symbol_count": lib.symbol_count,
                        "kicad_version": lib.kicad_version,
                    }
                    for lib in libraries
                ],
            }
        except Exception as e:
            log.error(f"list_symbol_libraries failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_library_symbols(
        library_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Return all symbols in a specific KiCad symbol library.

        Args:
            library_name: The library name returned by ``search_symbols`` or
                ``list_symbol_libraries`` in the ``library_name`` field.  For
                KiCad 10 symdir-style libraries this is
                ``"TableName/FileBaseName"`` (e.g. ``"Device/R_Small"``), not
                just the table name (e.g. not ``"Device"``).
        """
        try:
            mgr = _get_index_manager()
            symbols = mgr.get_library_symbols(library_name)
            if not symbols:
                return {
                    "success": False,
                    "error": f"Library '{library_name}' not found or has no indexed symbols.",
                }
            return {
                "success": True,
                "library_name": library_name,
                "count": len(symbols),
                "symbols": [
                    {
                        "name": s.symbol_name,
                        "description": s.description,
                        "keywords": s.keywords,
                        "pin_count": s.pin_count,
                    }
                    for s in symbols
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
