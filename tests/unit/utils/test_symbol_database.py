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


# ---------------------------------------------------------------------------
# FTS AFTER UPDATE trigger — in-place row updates must not drift the index
# ---------------------------------------------------------------------------


class TestFTSUpdateTrigger:
    """A raw UPDATE on the symbols table (no save_library round trip) must
    keep symbols_fts in sync: stale tokens disappear, new tokens appear."""

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

    def test_update_refreshes_fts_description(self):
        from sqlalchemy import text as sql_text

        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text("UPDATE symbols SET description = 'new token XYZ' WHERE symbol_name = 'R'")
            )
            conn.commit()

        # The new description token is searchable with the full new row.
        found = self.db.search("XYZ")
        assert any(
            r.symbol_name == "R" and r.description == "new token XYZ" for r in found
        )

        # A second in-place update removes the previous description token.
        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text(
                    "UPDATE symbols SET description = 'scrubbed clean' WHERE symbol_name = 'R'"
                )
            )
            conn.commit()
        assert self.db.search("XYZ") == []
        assert any(r.symbol_name == "R" for r in self.db.search("scrubbed"))

    def test_update_refreshes_fts_keywords(self):
        from sqlalchemy import text as sql_text

        # 'KWX' appears in no other column — only the keywords column is
        # indexed with it, so it isolates the keywords-update path.
        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text("UPDATE symbols SET keywords = 'special KWX' WHERE symbol_name = 'C'")
            )
            conn.commit()
# Project scope (schema v2) — the libraries.project column
# ---------------------------------------------------------------------------


class TestProjectScopeMigration:
    def test_fresh_db_has_project_column(self, db):
        """A new database already carries the project column (default '')."""
        db.save_library("Lib", "/tmp/lib.kicad_sym", 1.0, 100, "", [])
        lib = db.get_library_by_name("Lib", project="")
        assert lib is not None
        assert lib.project == ""

    def test_v1_db_auto_migrates_on_open(self, tmp_path):
        """A v1 database (no project column) gains it via ALTER, data kept."""
        import sqlite3

        db_path = tmp_path / "v1.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE libraries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "library_name VARCHAR NOT NULL, "
            "file_path VARCHAR NOT NULL UNIQUE, "
            "file_size INTEGER NOT NULL DEFAULT 0, "
            "mtime FLOAT NOT NULL DEFAULT 0.0, "
            "checksum VARCHAR NOT NULL DEFAULT '', "
            "symbol_count INTEGER NOT NULL DEFAULT 0, "
            "last_indexed FLOAT NOT NULL DEFAULT 0.0, "
            "kicad_version VARCHAR NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE symbols ("
            "library_name VARCHAR NOT NULL, "
            "symbol_name VARCHAR NOT NULL, "
            "library_id INTEGER NOT NULL, "
            "description VARCHAR NOT NULL DEFAULT '', "
            "keywords VARCHAR NOT NULL DEFAULT '', "
            "pin_count INTEGER NOT NULL DEFAULT 0, "
            "file_index INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (library_name, symbol_name))"
        )
        conn.execute(
            "INSERT INTO libraries (library_name, file_path) VALUES ('OldLib', '/old.kicad_sym')"
        )
        conn.commit()
        conn.close()

        # Opening through the ORM must add the column, keep the row, and
        # default existing rows to the global scope (project='').
        d = SymbolDatabase(str(db_path))
        try:
            libs = d.get_all_libraries()
            assert [lib.library_name for lib in libs] == ["OldLib"]
            assert libs[0].project == ""

            # v2 rows can be stored and project-scoped queries work.
            d.save_library(
                "ProjLib",
                "/proj/ProjLib.kicad_sym",
                1.0,
                100,
                "",
                [_make_symbols("ProjLib", [("P", "Project symbol", "", 1)])[0]],
                project="/proj",
            )
            assert d.get_library_by_name("ProjLib", project="/proj") is not None
            assert d.get_library_by_name("ProjLib", project="") is None
        finally:
            d.close()


class TestProjectScope:
    def _populate(self, db):
        db.save_library(
            "Global",
            "/tmp/global.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols("Global", [("G1", "global sym", "g", 1)]),
        )
        db.save_library(
            "ProjA",
            "/pA/ProjA.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols("ProjA", [("A1", "project A sym", "a", 1)]),
            project="/pA",
        )
        db.save_library(
            "ProjB",
            "/pB/ProjB.kicad_sym",
            1.0,
            100,
            "",
            _make_symbols("ProjB", [("B1", "project B sym", "b", 1)]),
            project="/pB",
        )

    def test_get_library_states_scope(self, db):
        self._populate(db)
        # No scope → everything.
        assert set(db.get_library_states(None)) == {
            "/tmp/global.kicad_sym",
            "/pA/ProjA.kicad_sym",
            "/pB/ProjB.kicad_sym",
        }
        # Global scope → global rows only.
        assert set(db.get_library_states("")) == {"/tmp/global.kicad_sym"}
        # Project scope → global + that project.
        assert set(db.get_library_states("/pA")) == {
            "/tmp/global.kicad_sym",
            "/pA/ProjA.kicad_sym",
        }

    def test_get_all_libraries_scope(self, db):
        self._populate(db)
        assert {l.library_name for l in db.get_all_libraries(None)} == {
            "Global",
            "ProjA",
            "ProjB",
        }
        assert {l.library_name for l in db.get_all_libraries("/pA")} == {"Global", "ProjA"}
        assert {l.library_name for l in db.get_all_libraries("")} == {"Global"}

    def test_get_library_by_name_scope(self, db):
        self._populate(db)
        assert db.get_library_by_name("ProjA", project="/pA") is not None
        assert db.get_library_by_name("ProjA", project="/pB") is None
        assert db.get_library_by_name("ProjA", project="") is None
        # Same nickname in two projects is fine (rows keyed by file_path).
        db.save_library(
            "Shared",
            "/pA/Shared.kicad_sym",
            1.0,
            100,
            "",
            [],
            project="/pA",
        )
        db.save_library(
            "Shared",
            "/pB/Shared.kicad_sym",
            1.0,
            100,
            "",
            [],
            project="/pB",
        )
        assert db.get_library_by_name("Shared", project="/pA").file_path == "/pA/Shared.kicad_sym"
        assert db.get_library_by_name("Shared", project="/pB").file_path == "/pB/Shared.kicad_sym"

    def test_get_library_by_name_exact_ownership(self, db):
        """get_library_by_name_exact matches nickname AND exact ownership —
        never the scope-union — so ownership-scoped deletes can locate (and
        only remove) the row of the owning scope."""
        db.save_library("Shared", "/g/Shared.kicad_sym", 1.0, 100, "", [], project="")
        db.save_library("Shared", "/pA/Shared.kicad_sym", 1.0, 100, "", [], project="/pA")
        db.save_library("Shared", "/pB/Shared.kicad_sym", 1.0, 100, "", [], project="/pB")

        assert db.get_library_by_name_exact("Shared", "").file_path == "/g/Shared.kicad_sym"
        assert db.get_library_by_name_exact("Shared", "/pA").file_path == "/pA/Shared.kicad_sym"
        assert db.get_library_by_name_exact("Shared", "/pB").file_path == "/pB/Shared.kicad_sym"
        # A project scope must not see another project's (or the global) row.
        assert db.get_library_by_name_exact("Shared", "/nope") is None
        assert db.get_library_by_name_exact("NoSuch", "/pA") is None

        # Deleting the global row leaves the project rows untouched.
        db.delete_library(db.get_library_by_name_exact("Shared", "").id)
        assert db.get_library_by_name_exact("Shared", "") is None
        assert db.get_library_by_name_exact("Shared", "/pA") is not None
        assert db.get_library_by_name_exact("Shared", "/pB") is not None

    def test_search_scope(self, db):
        self._populate(db)
        assert any(r.symbol_name == "A1" for r in db.search("sym", project="/pA"))
        assert not any(r.symbol_name == "A1" for r in db.search("sym", project="/pB"))
        assert any(r.symbol_name == "A1" for r in db.search("sym", project=None))
        assert not any(r.symbol_name == "A1" for r in db.search("sym", project=""))

    def test_search_by_name_scope(self, db):
        self._populate(db)
        assert any(r.symbol_name == "A1" for r in db.search_by_name("A1", project="/pA"))
        assert db.search_by_name("A1", project="/pB") == []

    def test_get_symbol_scope(self, db):
        self._populate(db)
        assert db.get_symbol("ProjA", "A1", project="/pA") is not None
        assert db.get_symbol("ProjA", "A1", project="/pB") is None

    def test_get_library_symbols_scope(self, db):
        self._populate(db)
        assert {s.symbol_name for s in db.get_library_symbols("ProjA", "/pA")} == {"A1"}
        assert db.get_library_symbols("ProjA", "/pB") == []

    def test_get_symbol_file_index_scope(self, db):
        self._populate(db)
        assert db.get_symbol_file_index("ProjA", "A1", "/pA") == 0
        assert db.get_symbol_file_index("ProjA", "A1", "/pB") is None

    def test_get_all_symbols_scope(self, db):
        self._populate(db)
        names = {s.symbol_name for s in db.get_all_symbols("/pA")}
        assert names == {"G1", "A1"}
        assert {s.symbol_name for s in db.get_all_symbols("")} == {"G1"}

    def test_get_stats_scope(self, db):
        self._populate(db)
        assert db.get_stats("/pA").library_count == 2  # Global + ProjA
        assert db.get_stats("/pA").symbol_count == 2
        assert db.get_stats("").library_count == 1
        assert db.get_stats("").symbol_count == 1
