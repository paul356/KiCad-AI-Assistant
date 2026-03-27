"""
Tests for SymbolIndexManager — orchestrates sym-lib-table reading, .kicad_sym
parsing, and database storage through sync() and search/lookup methods.
"""

from pathlib import Path

import pytest

from kicad_mcp.config import LibraryPathConfig
from kicad_mcp.utils.symbol_index_reader import SymbolIndexReader
from kicad_mcp.utils.symbol_index_manager import SymbolIndexManager

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Test config / helpers
# ---------------------------------------------------------------------------


class _FixtureConfig(LibraryPathConfig):
    """LibraryPathConfig subclass pointing at the test fixture directory."""

    def __init__(self):
        super().__init__()

    @property
    def symbol_table_file(self) -> str:
        return str(FIXTURES_DIR / "sym-lib-table")

    def get_env_vars(self) -> dict:
        return {"KICAD_TEST_FIXTURES_DIR": str(FIXTURES_DIR)}


def _make_manager() -> SymbolIndexManager:
    reader = SymbolIndexReader(_FixtureConfig())
    return SymbolIndexManager(reader, db_path=":memory:")


# ---------------------------------------------------------------------------
# Sync — initial run
# ---------------------------------------------------------------------------


class TestSyncInitial:
    def setup_method(self):
        self.mgr = _make_manager()

    def test_sync_adds_two_libraries(self):
        stats = self.mgr.sync()
        assert stats.added == 2

    def test_sync_no_failures(self):
        stats = self.mgr.sync()
        assert stats.failed == 0

    def test_sync_total_symbols(self):
        """Fixtures: TestDevice (R, C) + TestPower (VCC, GND) = 4 symbols."""
        stats = self.mgr.sync()
        assert stats.total_symbols == 4

    def test_sync_elapsed_positive(self):
        stats = self.mgr.sync()
        assert stats.elapsed_seconds > 0.0

    def test_sync_zero_updated_on_first_run(self):
        stats = self.mgr.sync()
        assert stats.updated == 0

    def test_sync_zero_removed_on_first_run(self):
        stats = self.mgr.sync()
        assert stats.removed == 0

    def test_sync_zero_skipped_on_first_run(self):
        stats = self.mgr.sync()
        assert stats.skipped == 0


# ---------------------------------------------------------------------------
# Sync — incremental (second call must skip unchanged files)
# ---------------------------------------------------------------------------


class TestSyncIncremental:
    def setup_method(self):
        self.mgr = _make_manager()
        self.mgr.sync()  # first sync — loads everything

    def test_second_sync_skips_all_libraries(self):
        stats = self.mgr.sync()
        assert stats.skipped == 2

    def test_second_sync_adds_nothing(self):
        stats = self.mgr.sync()
        assert stats.added == 0

    def test_second_sync_no_failures(self):
        stats = self.mgr.sync()
        assert stats.failed == 0

    def test_force_sync_reparses_all(self):
        stats = self.mgr.sync(force=True)
        assert stats.updated == 2
        assert stats.skipped == 0

    def test_force_sync_total_symbols_unchanged(self):
        stats = self.mgr.sync(force=True)
        assert stats.total_symbols == 4


# ---------------------------------------------------------------------------
# Sync — progress callback
# ---------------------------------------------------------------------------


class TestSyncProgressCallback:
    def setup_method(self):
        self.mgr = _make_manager()

    def test_progress_callback_called(self):
        calls = []
        self.mgr.sync(progress_callback=lambda cur, tot, name: calls.append((cur, tot, name)))
        assert len(calls) > 0

    def test_progress_callback_receives_library_names(self):
        names = []
        self.mgr.sync(progress_callback=lambda cur, tot, name: names.append(name))
        # The final call has name='' (completion signal)
        assert "TestDevice" in names or "TestPower" in names


# ---------------------------------------------------------------------------
# Search (after sync)
# ---------------------------------------------------------------------------


class TestSearchSymbols:
    def setup_method(self):
        self.mgr = _make_manager()
        self.mgr.sync()

    def test_search_resistor_finds_R(self):
        results = self.mgr.search_symbols("Resistor")
        assert any(r.symbol_name == "R" for r in results)

    def test_search_capacitor_finds_C(self):
        results = self.mgr.search_symbols("Capacitor")
        assert any(r.symbol_name == "C" for r in results)

    def test_search_power_finds_vcc_or_gnd(self):
        results = self.mgr.search_symbols("power")
        names = {r.symbol_name for r in results}
        assert names & {"VCC", "GND"}

    def test_search_no_match_returns_empty(self):
        results = self.mgr.search_symbols("xyzzy_no_match_123")
        assert results == []

    def test_search_by_name_R(self):
        results = self.mgr.search_by_name("R", exact=True)
        assert len(results) == 1
        assert results[0].symbol_name == "R"

    def test_search_by_name_partial(self):
        results = self.mgr.search_by_name("CC")
        names = {r.symbol_name for r in results}
        assert "VCC" in names


# ---------------------------------------------------------------------------
# get_symbol / get_library_symbols (after sync)
# ---------------------------------------------------------------------------


class TestLookupAfterSync:
    def setup_method(self):
        self.mgr = _make_manager()
        self.mgr.sync()

    def test_get_symbol_resistor(self):
        sym = self.mgr.get_symbol("TestDevice", "R")
        assert sym is not None
        assert sym.description == "Resistor"
        assert sym.pin_count == 2

    def test_get_symbol_vcc(self):
        sym = self.mgr.get_symbol("TestPower", "VCC")
        assert sym is not None
        assert sym.pin_count == 1

    def test_get_symbol_not_found(self):
        assert self.mgr.get_symbol("TestDevice", "NOEXIST") is None

    def test_get_library_symbols_testdevice(self):
        syms = self.mgr.get_library_symbols("TestDevice")
        assert len(syms) == 2
        names = {s.symbol_name for s in syms}
        assert names == {"R", "C"}

    def test_get_library_symbols_testpower(self):
        syms = self.mgr.get_library_symbols("TestPower")
        assert len(syms) == 2
        names = {s.symbol_name for s in syms}
        assert names == {"VCC", "GND"}

    def test_get_all_libraries(self):
        libs = self.mgr.get_all_libraries()
        assert len(libs) == 2
        names = {lib.library_name for lib in libs}
        assert names == {"TestDevice", "TestPower"}
