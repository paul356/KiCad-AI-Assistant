"""Tests for SymbolIndexManager — orchestrates sym-lib-table reading, .kicad_sym
parsing, and database storage through sync() and search/lookup methods.
"""

import os
from pathlib import Path

from kcaa.utils.config import ServerConfig
from kcaa.utils.symbol_index_manager import SymbolIndexManager
from kcaa.utils.symbol_index_reader import SymbolIndexReader

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Test config / helpers
# ---------------------------------------------------------------------------


class _FixtureConfig(ServerConfig):
    """ServerConfig subclass pointing at the test fixture directory."""

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


# ---------------------------------------------------------------------------
# remove_library (file-style exact + directory-style prefix)
# ---------------------------------------------------------------------------


class TestRemoveLibrary:
    def setup_method(self):
        self.mgr = _make_manager()
        self.mgr.sync()

    def test_remove_exact_file_style_library(self):
        """Bare-nickname (file-style) libraries still match exactly."""
        assert self.mgr.remove_library("TestDevice") is True
        assert self.mgr.get_library_by_name("TestDevice") is None
        assert self.mgr.get_library_symbols("TestDevice") == []

    def test_remove_missing_returns_false(self):
        assert self.mgr.remove_library("NoSuchLib") is False

    def test_remove_does_not_touch_system_symdir_rows(self):
        """Directory-style (symdir) rows keyed ``<nickname>/<file-stem>``
        belong to *system* symbol libraries (e.g. ``usr/share/kicad/symbols``).
        remove_library matches the bare nickname exactly, so it must **not**
        match those rows — delete tools never touch system libraries."""
        from kcaa.utils.symbol_database import SymbolRecord

        def _make_symbol(lib: str, name: str) -> SymbolRecord:
            return SymbolRecord(library_name=lib, symbol_name=name, library_id=-1,
                                description="", keywords="", pin_count=0, file_index=0)

        # Simulate sync() output for a system symdir library: one row per
        # .kicad_sym file inside the directory, keyed nickname/stem.
        for stem in ("STM32F722ICKx", "STM32F723ZETx"):
            self.mgr._db.save_library(
                f"MCU_ST_STM32F7/{stem}",
                f"/usr/share/kicad/symbols/MCU_ST_STM32F7.kicad_symdir/{stem}.kicad_sym",
                100.0,
                100,
                "20220914",
                [_make_symbol(f"MCU_ST_STM32F7/{stem}", stem)],
                "csum",
            )

        # The bare nickname has no exact row (rows are nickname/stem), so
        # removal must report False and leave the system rows untouched.
        assert self.mgr.remove_library("MCU_ST_STM32F7") is False
        remaining = {lib.library_name for lib in self.mgr.get_all_libraries()}
        assert remaining == {"TestDevice", "TestPower",
                             "MCU_ST_STM32F7/STM32F722ICKx",
                             "MCU_ST_STM32F7/STM32F723ZETx"}


# ---------------------------------------------------------------------------
# Project scope — project tables are indexed with the project set, and syncs
# in one scope never touch other scopes' rows.
# ---------------------------------------------------------------------------


class TestProjectScopeSync:
    """Project-scoped managers write project-local rows (project set) while
    the appended global entries stay global.  A sync in one scope must never
    drop rows belonging to another scope."""

    def _project_fixture(self, tmp_path):
        """A project dir with its own sym-lib-table listing one project
        library (a copy of the fixture device file)."""
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / "ProjLib.kicad_sym").write_text(
            (FIXTURES_DIR / "test_device.kicad_sym").read_text(encoding="utf-8")
        )
        (proj_dir / "sym-lib-table").write_text(
            "(sym_lib_table\n  (version 1)\n"
            '  (lib (name "ProjLib") (type "KiCad") (uri "${KIPRJMOD}/ProjLib.kicad_sym")'
            '(options "") (descr "Project library"))\n)\n',
            encoding="utf-8",
        )
        proj_real = os.path.realpath(str(proj_dir))
        proj_mgr = SymbolIndexManager(
            SymbolIndexReader(_FixtureConfig(), project_dir=proj_real),
            db_path=":memory:",
            project_path=proj_real,
        )
        return proj_dir, proj_real, proj_mgr

    def test_project_sync_indexes_project_and_global_rows(self, tmp_path):
        """Project table entry gets the project scope; appended global
        entries (TestDevice, TestPower) stay in the global scope."""
        _, proj_real, proj_mgr = self._project_fixture(tmp_path)
        stats = proj_mgr.sync()
        assert stats.added == 3  # ProjLib (project) + TestDevice + TestPower (global)
        assert stats.failed == 0

        proj_row = proj_mgr.get_library_by_name("ProjLib")
        assert proj_row is not None
        assert proj_row.project == proj_real
        g_device = proj_mgr.get_library_by_name("TestDevice")
        assert g_device.project == ""

        # Project scope sees global + its own rows (that is what the incremental
        # skip in a project-scoped sync relies on)…
        assert set(proj_mgr._db.get_library_states(proj_real)) == {
            os.path.join(proj_real, "ProjLib.kicad_sym"),
            os.path.join(FIXTURES_DIR, "test_device.kicad_sym"),
            os.path.join(FIXTURES_DIR, "test_power.kicad_sym"),
        }
        # …while the global scope sees only its own rows.
        assert set(proj_mgr._db.get_library_states("")) == {
            os.path.join(FIXTURES_DIR, "test_device.kicad_sym"),
            os.path.join(FIXTURES_DIR, "test_power.kicad_sym"),
        }

    def test_incremental_project_sync_skips_unchanged(self, tmp_path):
        _, _, proj_mgr = self._project_fixture(tmp_path)
        proj_mgr.sync()
        stats = proj_mgr.sync()
        assert stats.skipped == 3
        assert stats.added == 0
        assert stats.removed == 0

    def test_global_sync_preserves_project_rows(self, tmp_path):
        """A global-scope sync (project-less reader) must not drop rows that
        a project-scoped manager created."""
        _, _, proj_mgr = self._project_fixture(tmp_path)
        proj_mgr.sync()

        global_mgr = SymbolIndexManager(
            SymbolIndexReader(_FixtureConfig()), db_path=":memory:"
        )
        stats = global_mgr.sync()
        assert stats.removed == 0
        assert stats.added == 2  # TestDevice + TestPower, its own global copies

        # Project row is intact and still project-scoped.
        assert proj_mgr.get_library_by_name("ProjLib") is not None
        assert proj_mgr.get_library_by_name("ProjLib").project == os.path.realpath(
            str(tmp_path / "proj")
        )
        # And invisible to the global-scope manager.
        assert global_mgr.get_library_by_name("ProjLib") is None

    def test_project_sync_preserves_other_project_rows(self, tmp_path):
        """Syncs of one project never touch another project's rows."""
        _, proj_real_a, proj_mgr_a = self._project_fixture(tmp_path)
        proj_mgr_a.sync()

        # A second, unrelated project directory.
        proj_b = tmp_path / "projb"
        proj_b.mkdir()
        proj_b_real = os.path.realpath(str(proj_b))
        proj_mgr_b = SymbolIndexManager(
            SymbolIndexReader(_FixtureConfig(), project_dir=proj_b_real),
            db_path=":memory:",
            project_path=proj_b_real,
        )
        # No sym-lib-table in proj_b -> only the global fixture entries.
        stats = proj_mgr_b.sync()
        assert stats.added == 2
        assert stats.removed == 0
        assert proj_mgr_a.get_library_by_name("ProjLib") is not None

    def test_narrow_index_library_writes_manager_scope(self, tmp_path):
        _, proj_real, proj_mgr = self._project_fixture(tmp_path)
        proj_mgr.index_library("ProjLib", os.path.join(proj_real, "ProjLib.kicad_sym"))
        row = proj_mgr.get_library_by_name("ProjLib")
        assert row is not None and row.project == proj_real

        global_mgr = SymbolIndexManager(
            SymbolIndexReader(_FixtureConfig()), db_path=":memory:"
        )
        global_mgr.index_library("TestDevice", os.path.join(FIXTURES_DIR, "test_device.kicad_sym"))
        assert global_mgr.get_library_by_name("TestDevice").project == ""

    def test_remove_library_is_cross_project(self, tmp_path):
        """remove_library matches by nickname regardless of scope, so a
        project-scoped manager can drop rows created in another scope."""
        _, proj_real, proj_mgr = self._project_fixture(tmp_path)
        proj_mgr.sync()
        global_mgr = SymbolIndexManager(
            SymbolIndexReader(_FixtureConfig()), db_path=":memory:"
        )
        assert proj_mgr.remove_library("ProjLib") is True
        assert proj_mgr.get_library_by_name("ProjLib") is None
        assert global_mgr.remove_library("ProjLib") is False
