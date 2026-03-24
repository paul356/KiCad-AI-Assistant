"""
Unit tests for kicad_mcp/tools/symbol_tools.py.

Patches the module-level _index_manager singleton and _sync_state so tests
are fully self-contained and do not require a real KiCad installation.
"""

import asyncio
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# MockMCP — captures @mcp.tool()-decorated coroutines
# ---------------------------------------------------------------------------

class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _get_tools() -> dict:
    """Register symbol tools against a mock MCP and return the captured dict."""
    from kicad_mcp.tools.symbol_tools import register_symbol_tools
    mock = _MockMCP()
    register_symbol_tools(mock)
    return mock.tools


# Lazily-created module-level tools dict (avoids repeated registration).
_tools: dict = {}


def _tools_dict() -> dict:
    global _tools
    if not _tools:
        _tools = _get_tools()
    return _tools


def _call(name, **kwargs):
    """Helper: call a tool by name synchronously via asyncio.run()."""
    return asyncio.run(_tools_dict()[name](**kwargs))


# ---------------------------------------------------------------------------
# TestSyncSymbolIndex
# ---------------------------------------------------------------------------


class TestSyncSymbolIndex:
    """Tests for sync_symbol_index()."""

    def test_starts_when_idle(self):
        """When no sync is running, sync_symbol_index should start a thread."""
        import kicad_mcp.tools.symbol_tools as mod

        # Reset state to idle.
        with mod._sync_lock:
            mod._sync_state.running = False
            mod._sync_state.current = 0
            mod._sync_state.total = 0
            mod._sync_state.current_library = ''
            mod._sync_state.error = None

        with patch.object(threading, "Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

            result = _call("sync_symbol_index", force=False)

        assert result["status"] == "started"
        mock_thread.start.assert_called_once()

        # Clean up: mark as not running so other tests don't see stale state.
        with mod._sync_lock:
            mod._sync_state.running = False

    def test_already_running(self):
        """When a sync is already running, return already_running status."""
        import kicad_mcp.tools.symbol_tools as mod

        with mod._sync_lock:
            mod._sync_state.running = True
            mod._sync_state.current = 5
            mod._sync_state.total = 20

        try:
            result = _call("sync_symbol_index")
            assert result["status"] == "already_running"
            assert "current" in result
        finally:
            with mod._sync_lock:
                mod._sync_state.running = False


# ---------------------------------------------------------------------------
# TestGetSymbolSyncStatus
# ---------------------------------------------------------------------------


class TestGetSymbolSyncStatus:
    """Tests for get_symbol_sync_status()."""

    def test_idle_state(self):
        """Default state should show running=False."""
        import kicad_mcp.tools.symbol_tools as mod

        with mod._sync_lock:
            mod._sync_state.running = False
            mod._sync_state.current = 0
            mod._sync_state.total = 0
            mod._sync_state.current_library = ''
            mod._sync_state.last_result = None
            mod._sync_state.error = None

        result = _call("get_symbol_sync_status")
        assert result["running"] is False
        assert result["current"] == 0
        assert result["total"] == 0

    def test_running_state(self):
        """Should reflect whatever state _sync_state holds."""
        import kicad_mcp.tools.symbol_tools as mod

        with mod._sync_lock:
            mod._sync_state.running = True
            mod._sync_state.current = 3
            mod._sync_state.total = 10
            mod._sync_state.current_library = "Device"
            mod._sync_state.last_result = None
            mod._sync_state.error = None

        try:
            result = _call("get_symbol_sync_status")
            assert result["running"] is True
            assert result["current"] == 3
            assert result["total"] == 10
            assert result["current_library"] == "Device"
        finally:
            with mod._sync_lock:
                mod._sync_state.running = False


# ---------------------------------------------------------------------------
# TestSearchSymbols
# ---------------------------------------------------------------------------


class TestSearchSymbols:
    """Tests for search_symbols()."""

    def _make_symbol_record(self, lib="Device", name="R", desc="Resistor", kw="res", pins=2):
        r = MagicMock()
        r.library_name = lib
        r.symbol_name = name
        r.description = desc
        r.keywords = kw
        r.pin_count = pins
        return r

    def test_returns_results(self):
        rec1 = self._make_symbol_record("Device", "R", "Resistor", "res", 2)
        rec2 = self._make_symbol_record("Device", "C", "Capacitor", "cap", 2)

        mock_mgr = MagicMock()
        mock_mgr.search_symbols.return_value = [rec1, rec2]

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("search_symbols", query="passive")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["symbols"][0]["name"] == "R"
        assert result["symbols"][1]["name"] == "C"
        mock_mgr.search_symbols.assert_called_once_with("passive", limit=50)

    def test_empty_results(self):
        mock_mgr = MagicMock()
        mock_mgr.search_symbols.return_value = []

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("search_symbols", query="xyzzy_no_match")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["symbols"] == []

    def test_manager_error(self):
        mock_mgr = MagicMock()
        mock_mgr.search_symbols.side_effect = RuntimeError("DB locked")

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("search_symbols", query="anything")

        assert result["success"] is False
        assert "DB locked" in result["error"]


# ---------------------------------------------------------------------------
# TestGetSymbol
# ---------------------------------------------------------------------------


class TestGetSymbol:
    """Tests for get_symbol()."""

    def test_found(self):
        rec = MagicMock()
        rec.library_name = "Device"
        rec.symbol_name = "R"
        rec.description = "Resistor"
        rec.keywords = "res passive"
        rec.pin_count = 2

        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = rec

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol", library_name="Device", symbol_name="R")

        assert result["success"] is True
        assert result["library"] == "Device"
        assert result["name"] == "R"
        assert result["pin_count"] == 2

    def test_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = None

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol", library_name="Device", symbol_name="NOEXIST")

        assert result["success"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# TestListSymbolLibraries
# ---------------------------------------------------------------------------


class TestListSymbolLibraries:
    """Tests for list_symbol_libraries()."""

    def test_returns_libraries(self):
        lib1 = MagicMock()
        lib1.library_name = "Device"
        lib1.file_path = "/usr/share/kicad/symbols/Device.kicad_sym"
        lib1.symbol_count = 500
        lib1.kicad_version = "7.0"

        lib2 = MagicMock()
        lib2.library_name = "Connector"
        lib2.file_path = "/usr/share/kicad/symbols/Connector.kicad_sym"
        lib2.symbol_count = 300
        lib2.kicad_version = "7.0"

        mock_mgr = MagicMock()
        mock_mgr.get_all_libraries.return_value = [lib1, lib2]

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("list_symbol_libraries")

        assert result["success"] is True
        assert result["count"] == 2
        names = [lib["name"] for lib in result["libraries"]]
        assert "Device" in names
        assert "Connector" in names
        first = result["libraries"][0]
        assert "path" in first
        assert "symbol_count" in first
        assert "kicad_version" in first

    def test_empty_libraries(self):
        mock_mgr = MagicMock()
        mock_mgr.get_all_libraries.return_value = []

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("list_symbol_libraries")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["libraries"] == []


# ---------------------------------------------------------------------------
# TestGetLibrarySymbols
# ---------------------------------------------------------------------------


class TestGetLibrarySymbols:
    """Tests for get_library_symbols()."""

    def _make_sym(self, name, desc="", kw="", pins=2):
        s = MagicMock()
        s.symbol_name = name
        s.description = desc
        s.keywords = kw
        s.pin_count = pins
        return s

    def test_found(self):
        syms = [self._make_sym("R"), self._make_sym("R_Small")]
        mock_mgr = MagicMock()
        mock_mgr.get_library_symbols.return_value = syms

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_library_symbols", library_name="Device")

        assert result["success"] is True
        assert result["library"] == "Device"
        assert result["count"] == 2
        names = [s["name"] for s in result["symbols"]]
        assert "R" in names

    def test_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.get_library_symbols.return_value = []

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_library_symbols", library_name="NOLIB")

        assert result["success"] is False
        assert "NOLIB" in result["error"]


# ---------------------------------------------------------------------------
# TestGetSymbolIndexStats
# ---------------------------------------------------------------------------


class TestGetSymbolIndexStats:
    """Tests for get_symbol_index_stats()."""

    def test_returns_stats(self):
        stats = MagicMock()
        stats.library_count = 42
        stats.symbol_count = 12345
        stats.last_sync = "2025-01-01T00:00:00"
        stats.db_path = "/home/user/.kicad_mcp/symbols.db"

        mock_mgr = MagicMock()
        mock_mgr.get_statistics.return_value = stats

        with patch("kicad_mcp.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol_index_stats")

        assert result["success"] is True
        assert result["library_count"] == 42
        assert result["symbol_count"] == 12345
        assert result["last_sync"] == "2025-01-01T00:00:00"
        assert result["db_path"] == "/home/user/.kicad_mcp/symbols.db"
