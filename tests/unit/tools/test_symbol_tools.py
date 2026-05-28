"""
Unit tests for kcaa/tools/symbol_tools.py.

Patches the module-level _index_manager singleton and _sync_state so tests
are fully self-contained and do not require a real KiCad installation.
"""

import asyncio
import os
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sexpdata


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
    from kcaa.tools.symbol_tools import register_symbol_tools
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
        import kcaa.tools.symbol_tools as mod

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
        import kcaa.tools.symbol_tools as mod

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
        import kcaa.tools.symbol_tools as mod

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
        import kcaa.tools.symbol_tools as mod

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

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("search_symbols", query="passive")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["symbols"][0]["name"] == "R"
        assert result["symbols"][1]["name"] == "C"
        mock_mgr.search_symbols.assert_called_once_with("passive", limit=50)

    def test_empty_results(self):
        mock_mgr = MagicMock()
        mock_mgr.search_symbols.return_value = []

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("search_symbols", query="xyzzy_no_match")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["symbols"] == []

    def test_manager_error(self):
        mock_mgr = MagicMock()
        mock_mgr.search_symbols.side_effect = RuntimeError("DB locked")

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
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

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol", library_name="Device", symbol_name="R")

        assert result["success"] is True
        assert result["library_name"] == "Device"
        assert result["name"] == "R"
        assert result["pin_count"] == 2

    def test_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = None

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
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

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("list_symbol_libraries")

        assert result["success"] is True
        assert result["mode"] == "tables"
        assert result["total"] == 2
        names = [t["name"] for t in result["tables"]]
        assert "Device" in names
        assert "Connector" in names
        first = result["tables"][0]
        assert "symbol_count" in first

    def test_empty_libraries(self):
        mock_mgr = MagicMock()
        mock_mgr.get_all_libraries.return_value = []

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("list_symbol_libraries")

        assert result["success"] is True
        assert result["mode"] == "tables"
        assert result["total"] == 0
        assert result["tables"] == []


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

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_library_symbols", library_name="Device")

        assert result["success"] is True
        assert result["library_name"] == "Device"
        assert result["total"] == 2
        names = [s["name"] for s in result["symbols"]]
        assert "R" in names

    def test_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.get_library_symbols.return_value = []

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
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
        stats.db_path = "/home/user/.kcaa/symbols.db"

        mock_mgr = MagicMock()
        mock_mgr.get_statistics.return_value = stats

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol_index_stats")

        assert result["success"] is True
        assert result["library_count"] == 42
        assert result["symbol_count"] == 12345
        assert result["last_sync"] == "2025-01-01T00:00:00"
        assert result["db_path"] == "/home/user/.kcaa/symbols.db"


# ---------------------------------------------------------------------------
# Helpers shared by TestGetSymbolPins and TestParseLibPins
# ---------------------------------------------------------------------------

_TOOLS_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_SYM = os.path.join(_TOOLS_FIXTURE_DIR, "test_symbols.kicad_sym")


def _load_fixture_symbol(fixture_path: str, symbol_name: str) -> list:
    """Parse a .kicad_sym fixture file and return the raw sexpdata list for *symbol_name*."""
    with open(fixture_path) as f:
        lib_data = sexpdata.loads(f.read())
    for entry in lib_data:
        if (
            isinstance(entry, list)
            and len(entry) >= 2
            and isinstance(entry[0], sexpdata.Symbol)
            and entry[0].value() == "symbol"
            and entry[1] == symbol_name
        ):
            return entry
    raise ValueError(f"Symbol {symbol_name!r} not found in {fixture_path}")


# ---------------------------------------------------------------------------
# TestGetSymbolPins
# ---------------------------------------------------------------------------


class TestGetSymbolPins:
    """Tests for the get_symbol_pins() MCP tool."""

    def _make_mock_mgr(self):
        """Return (mock_mgr, sym_rec, lib_rec) backed by the R_Small fixture."""
        sym_rec = MagicMock()
        sym_rec.file_index = 0

        lib_rec = MagicMock()
        lib_rec.file_path = _FIXTURE_SYM
        lib_rec.mtime = 0.0
        lib_rec.file_size = 0

        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = sym_rec
        mock_mgr.get_library_by_name.return_value = lib_rec
        return mock_mgr

    def test_happy_path(self):
        """get_symbol_pins returns success with pin data for R_Small."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw", return_value=raw):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        assert result["success"] is True
        assert result["symbol_name"] == "R_Small"
        assert result["library_name"] == "Device"
        assert result["pin_count"] > 0

    def test_pin_fields(self):
        """Each pin has exactly number, name, type, direction keys; direction is a str."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw", return_value=raw):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        for pin in result["pins"]:
            assert set(pin.keys()) == {"number", "name", "type", "direction"}
            assert isinstance(pin["direction"], str)

    def test_pin_count_matches_list(self):
        """result['pin_count'] == len(result['pins'])."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw", return_value=raw):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        assert result["pin_count"] == len(result["pins"])

    def test_symbol_not_found(self):
        """Returns error dict when index manager has no record for the symbol."""
        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = None

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="NOEXIST")

        assert result.get("success") is False
        assert "error" in result

    def test_symbol_not_in_index_with_empty_name(self):
        """Returns error dict when index has no record for the symbol (empty name query)."""
        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = None

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="")

        assert result.get("success") is False
        assert "error" in result

    def test_library_not_found(self):
        """Returns error dict when library record is absent from the index."""
        sym_rec = MagicMock()
        mock_mgr = MagicMock()
        mock_mgr.get_symbol.return_value = sym_rec
        mock_mgr.get_library_by_name.return_value = None

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr):
            result = _call("get_symbol_pins", library_name="MISSINGLIB", symbol_name="R")

        assert result.get("success") is False
        assert "error" in result

    def test_extract_lib_raises_returns_error(self):
        """Inner except around extract_lib_symbol_raw returns error dict on FileNotFoundError."""
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw",
                   side_effect=FileNotFoundError("lib gone")):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        assert result.get("success") is False
        assert "error" in result

    def test_extract_lib_raises_value_error_returns_error(self):
        """Inner except around extract_lib_symbol_raw returns error dict on ValueError."""
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw",
                   side_effect=ValueError("bad data")):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        assert result.get("success") is False
        assert "error" in result

    def test_r_small_exact_pin_values(self):
        """Exact pin field values for R_Small: number, empty name, passive type, directions."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        mock_mgr = self._make_mock_mgr()

        with patch("kcaa.tools.symbol_tools._get_index_manager", return_value=mock_mgr), \
             patch("kcaa.tools.symbol_tools.extract_lib_symbol_raw", return_value=raw):
            result = _call("get_symbol_pins", library_name="Device", symbol_name="R_Small")

        by_num = {p["number"]: p for p in result["pins"]}
        assert by_num["1"] == {"number": "1", "name": "", "type": "passive", "direction": "up"}
        assert by_num["2"] == {"number": "2", "name": "", "type": "passive", "direction": "down"}


# ---------------------------------------------------------------------------
# TestParseLibPins
# ---------------------------------------------------------------------------


class TestParseLibPins:
    """Unit tests for the _parse_lib_pins() module-level helper."""

    def setup_method(self):
        from kcaa.tools.symbol_tools import _parse_lib_pins
        self._parse = _parse_lib_pins

    def test_r_small_pin_count(self):
        """R_Small in the fixture file has exactly 2 pins."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        assert len(pins) == 2

    def test_r_small_pin_numbers(self):
        """R_Small pins are numbered '1' and '2'."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        assert {p["number"] for p in pins} == {"1", "2"}

    def test_pin_type_is_passive(self):
        """All R_Small pins have type 'passive'."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        for pin in pins:
            assert pin["type"] == "passive"

    def test_pin_directions_are_valid_strings(self):
        """Every parsed direction is a valid string from the expected set."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        valid = {"right", "up", "left", "down"}
        for pin in pins:
            assert isinstance(pin["direction"], str)
            assert pin["direction"] in valid

    def test_pin_directions_match_fixture(self):
        """Pin 1 is at 270° stub angle → wire exits up (270°+180°=90°→'up' in screen space).
        Pin 2 is at 90° stub angle → wire exits down (90°+180°=270°→'down')."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        by_num = {p["number"]: p for p in pins}
        assert by_num["1"]["direction"] == "up"
        assert by_num["2"]["direction"] == "down"

    def test_no_duplicate_pin_numbers(self):
        """_parse_lib_pins deduplicates pins with the same number."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        numbers = [p["number"] for p in pins]
        assert len(numbers) == len(set(numbers))

    def test_result_sorted_by_number(self):
        """Returned pins are sorted in ascending order by pin number string."""
        raw = _load_fixture_symbol(_FIXTURE_SYM, "R_Small")
        pins = self._parse(raw)
        numbers = [p["number"] for p in pins]
        assert numbers == sorted(numbers)

    def test_dedup_multi_unit_pins(self):
        """_parse_lib_pins deduplicates pin numbers that appear in multiple sub-units."""
        S = sexpdata.Symbol
        fake_raw = [
            S("symbol"), "FakeOp",
            # unit 1 sub-symbol
            [S("symbol"), "FakeOp_1_1",
                [S("pin"), S("input"), S("line"),
                    [S("at"), 0, 0, 0], [S("length"), 2.54],
                    [S("name"), "IN+", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]],
                    [S("number"), "1", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]]],
                [S("pin"), S("power_in"), S("line"),
                    [S("at"), 0, 5.08, 270], [S("length"), 2.54],
                    [S("name"), "VCC", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]],
                    [S("number"), "3", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]]],
            ],
            # unit 2 sub-symbol — pin 3 (VCC) appears again here
            [S("symbol"), "FakeOp_2_1",
                [S("pin"), S("output"), S("line"),
                    [S("at"), 0, 0, 180], [S("length"), 2.54],
                    [S("name"), "OUT", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]],
                    [S("number"), "2", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]]],
                [S("pin"), S("power_in"), S("line"),
                    [S("at"), 0, 5.08, 270], [S("length"), 2.54],
                    [S("name"), "VCC", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]],
                    [S("number"), "3", [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]]],
            ],
        ]

        pins = self._parse(fake_raw)
        assert len(pins) == 3
        assert [p["number"] for p in pins].count("3") == 1
