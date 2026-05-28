"""
Tests for SymbolDatabase — SQLAlchemy/SQLite storage layer for indexed symbols.
"""

import pytest

from kcaa.utils.symbol_database import SymbolDatabase, SymbolRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_symbols(library_name: str, entries: list[tuple]) -> list[SymbolRecord]:
    """
    Build a list of SymbolRecord objects.

    Each entry is a (symbol_name, description, keywords, pin_count) tuple.
    The library_name and library_id fields are placeholders — SymbolDatabase
    overwrites them with real values during save_library().
    """
    return [
        SymbolRecord(
            library_name=library_name,
            symbol_name=name,
            library_id=0,
            description=desc,
            keywords=kw,
            pin_count=pins,
            file_index=idx,
        )
        for idx, (name, desc, kw, pins) in enumerate(entries)
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    d = SymbolDatabase(":memory:")
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Empty database state
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    def test_stats_are_zero(self, db):
        stats = db.get_stats()
        assert stats.library_count == 0
        assert stats.symbol_count == 0
        assert stats.last_sync == 0.0

    def test_library_states_empty(self, db):
        assert db.get_library_states() == {}

    def test_get_all_symbols_empty(self, db):
        assert db.get_all_symbols() == []

    def test_get_all_libraries_empty(self, db):
        assert db.get_all_libraries() == []

    def test_get_symbol_returns_none(self, db):
        assert db.get_symbol("Lib", "R") is None

    def test_get_library_by_name_returns_none(self, db):
        assert db.get_library_by_name("Lib") is None


# ---------------------------------------------------------------------------
# save_library
# ---------------------------------------------------------------------------


class TestSaveLibrary:
    def test_returns_symbol_count(self, db):
        syms = _make_symbols("Lib", [("R", "Resistor", "R resistor", 2)])
        n = db.save_library("Lib", "/tmp/lib.kicad_sym", 1000.0, 500, "20241101", syms)
        assert n == 1

    def test_library_appears_in_states(self, db):
        syms = _make_symbols("Lib", [("R", "Resistor", "R", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1000.0, 500, "", syms)
        states = db.get_library_states()
        assert "/tmp/lib.kicad_sym" in states

    def test_multiple_symbols_stored(self, db):
        syms = _make_symbols(
            "Dev",
            [
                ("R", "Resistor", "R resistor", 2),
                ("C", "Capacitor", "C capacitor", 2),
            ],
        )
        db.save_library("Dev", "/tmp/dev.kicad_sym", 1.0, 100, "", syms)
        stored = db.get_library_symbols("Dev")
        assert len(stored) == 2

    def test_replace_existing_library(self, db):
        """Saving to the same path again replaces all previous symbols."""
        syms1 = _make_symbols("Lib", [("R", "Resistor", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "", syms1)

        syms2 = _make_symbols("Lib", [("C", "Capacitor", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 2.0, 100, "", syms2)

        stored = db.get_library_symbols("Lib")
        assert len(stored) == 1
        assert stored[0].symbol_name == "C"

    def test_stats_updated_after_save(self, db):
        syms = _make_symbols("Lib", [("R", "Res", "", 2), ("C", "Cap", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "", syms)
        stats = db.get_stats()
        assert stats.library_count == 1
        assert stats.symbol_count == 2
        assert stats.last_sync > 0.0

    def test_two_libraries_independent(self, db):
        db.save_library(
            "DevLib",
            "/tmp/dev.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols("DevLib", [("R", "Resistor", "", 2)]),
        )
        db.save_library(
            "PwrLib",
            "/tmp/pwr.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols("PwrLib", [("VCC", "VCC", "", 1)]),
        )
        stats = db.get_stats()
        assert stats.library_count == 2
        assert stats.symbol_count == 2


# ---------------------------------------------------------------------------
# touch_library
# ---------------------------------------------------------------------------


class TestTouchLibrary:
    def test_touch_updates_mtime(self, db):
        syms = _make_symbols("Lib", [("R", "Resistor", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "abc", syms)
        lib = db.get_library_by_name("Lib")
        db.touch_library(lib.id, 999.0, 200, "xyz")
        states = db.get_library_states()
        _id, mtime, size, checksum = states["/tmp/lib.kicad_sym"]
        assert mtime == 999.0
        assert size == 200
        assert checksum == "xyz"


# ---------------------------------------------------------------------------
# delete_library
# ---------------------------------------------------------------------------


class TestDeleteLibrary:
    def test_delete_removes_library_and_symbols(self, db):
        syms = _make_symbols("Lib", [("R", "Resistor", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "", syms)

        lib = db.get_library_by_name("Lib")
        assert lib is not None
        db.delete_library(lib.id)

        assert db.get_library_by_name("Lib") is None
        assert db.get_library_symbols("Lib") == []

    def test_delete_updates_stats(self, db):
        syms = _make_symbols("Lib", [("R", "Res", "", 2)])
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "", syms)
        lib = db.get_library_by_name("Lib")
        db.delete_library(lib.id)
        stats = db.get_stats()
        assert stats.library_count == 0
        assert stats.symbol_count == 0


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestLookup:
    @pytest.fixture(autouse=True)
    def _populate(self, db):
        self.db = db
        db.save_library(
            "Dev",
            "/tmp/dev.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols(
                "Dev",
                [
                    ("R", "Resistor", "R resistor passive", 2),
                    ("C", "Capacitor", "C capacitor passive", 2),
                ],
            ),
        )
        db.save_library(
            "Pwr",
            "/tmp/pwr.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols(
                "Pwr",
                [
                    ("VCC", "Power supply positive", "power VCC supply", 1),
                    ("GND", "Power supply ground", "power GND ground", 1),
                ],
            ),
        )

    def test_get_symbol_found(self):
        sym = self.db.get_symbol("Dev", "R")
        assert sym is not None
        assert sym.description == "Resistor"
        assert sym.pin_count == 2

    def test_get_symbol_not_found(self):
        assert self.db.get_symbol("Dev", "NONEXISTENT") is None

    def test_get_symbol_wrong_library(self):
        assert self.db.get_symbol("Pwr", "R") is None

    def test_get_library_symbols_count(self):
        syms = self.db.get_library_symbols("Dev")
        assert len(syms) == 2

    def test_get_library_symbols_order(self):
        syms = self.db.get_library_symbols("Dev")
        assert [s.symbol_name for s in syms] == ["R", "C"]

    def test_get_all_symbols_total(self):
        all_syms = self.db.get_all_symbols()
        assert len(all_syms) == 4

    def test_get_all_libraries(self):
        libs = self.db.get_all_libraries()
        assert len(libs) == 2
        names = {lib.library_name for lib in libs}
        assert names == {"Dev", "Pwr"}

    def test_get_library_by_name(self):
        lib = self.db.get_library_by_name("Pwr")
        assert lib is not None
        assert lib.symbol_count == 2

    def test_get_symbol_file_index(self):
        idx = self.db.get_symbol_file_index("Dev", "C")
        assert idx == 1

    def test_get_symbol_file_index_not_found(self):
        assert self.db.get_symbol_file_index("Dev", "MISSING") is None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchByName:
    @pytest.fixture(autouse=True)
    def _populate(self, db):
        self.db = db
        db.save_library(
            "Dev",
            "/tmp/dev.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols(
                "Dev",
                [
                    ("R", "Resistor", "R resistor", 2),
                    ("C", "Capacitor", "C capacitor", 2),
                    ("VCC", "Power positive", "power VCC", 1),
                ],
            ),
        )

    def test_substring_match(self):
        results = self.db.search_by_name("CC")
        names = {r.symbol_name for r in results}
        assert "VCC" in names

    def test_exact_match(self):
        results = self.db.search_by_name("R", exact=True)
        assert len(results) == 1
        assert results[0].symbol_name == "R"

    def test_exact_match_case_insensitive(self):
        results = self.db.search_by_name("r", exact=True)
        assert len(results) == 1

    def test_no_match_returns_empty(self):
        results = self.db.search_by_name("ZZZNOMATCH")
        assert results == []


class TestFTSSearch:
    @pytest.fixture(autouse=True)
    def _populate(self, db):
        self.db = db
        db.save_library(
            "Dev",
            "/tmp/dev.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols(
                "Dev",
                [
                    ("R", "Resistor", "R resistor passive", 2),
                    ("C", "Capacitor", "C capacitor passive", 2),
                ],
            ),
        )

    def test_search_by_description_word(self):
        results = self.db.search("Resistor")
        assert any(r.symbol_name == "R" for r in results)

    def test_search_by_keyword(self):
        results = self.db.search("capacitor")
        assert any(r.symbol_name == "C" for r in results)

    def test_search_no_match(self):
        results = self.db.search("xyzzy_no_match_ever")
        assert results == []
