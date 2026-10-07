"""
Tests for SymbolIndexReader — reads sym-lib-table and expands ${VAR} in URIs,
including project-table merging (project wins) and ${KIPRJMOD} expansion.
"""

import os
from pathlib import Path

import pytest

from kcaa.utils.config import ServerConfig
from kcaa.utils.symbol_index_reader import SymbolIndexReader

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _FixtureConfig(ServerConfig):
    """ServerConfig subclass that points to the test fixture directory."""

    def __init__(self):
        super().__init__()

    @property
    def symbol_table_file(self) -> str:
        return str(FIXTURES_DIR / "sym-lib-table")

    def get_env_vars(self) -> dict:
        return {"KICAD_TEST_FIXTURES_DIR": str(FIXTURES_DIR)}


class _MissingTableConfig(ServerConfig):
    """Config that points to a non-existent sym-lib-table."""

    def __init__(self):
        super().__init__()

    @property
    def symbol_table_file(self) -> str:
        return "/nonexistent/path/sym-lib-table"

    def get_env_vars(self) -> dict:
        return {}


class _TableConfig(ServerConfig):
    """ServerConfig pointing at a caller-supplied global sym-lib-table."""

    def __init__(self, table_path):
        super().__init__()
        self._table = str(table_path)

    @property
    def symbol_table_file(self) -> str:
        return self._table

    def get_env_vars(self) -> dict:
        return {"KICAD_TEST_FIXTURES_DIR": str(FIXTURES_DIR)}


GLOBAL_TABLE = (
    "(sym_lib_table\n"
    '  (lib (name "TestDevice") (type "KiCad") (uri "${KICAD_TEST_FIXTURES_DIR}/test_device.kicad_sym")'
    '(options "") (descr "global device"))\n'
    '  (lib (name "TestPower") (type "KiCad") (uri "${KICAD_TEST_FIXTURES_DIR}/test_power.kicad_sym")'
    '(options "") (descr "global power"))\n'
    ")\n"
)


def _write_global_table(tmp_path) -> str:
    table = tmp_path / "sym-lib-table"
    table.write_text(GLOBAL_TABLE, encoding="utf-8")
    return str(table)


def _project_table(tmp_path, extra_uri: str) -> str:
    """A project sym-lib-table: project win on TestDevice + one project-only
    library pointing at *extra_uri*."""
    table = os.path.join(str(tmp_path), "sym-lib-table")
    with open(table, "w", encoding="utf-8") as f:
        f.write(
            "(sym_lib_table\n"
            f'  (lib (name "TestDevice") (type "KiCad") (uri "{extra_uri}")'
            '(options "") (descr "project device"))\n'
            f'  (lib (name "ProjOnly") (type "KiCad") (uri "${{KIPRJMOD}}/local.kicad_sym")'
            '(options "") (descr "project local"))\n'
            ")\n"
        )
    return table


class TestSymbolIndexReaderLibraries:
    def setup_method(self):
        self.reader = SymbolIndexReader(_FixtureConfig())

    def test_returns_two_libraries(self):
        libs = self.reader.get_libraries()
        assert len(libs) == 2

    def test_library_names(self):
        libs = self.reader.get_libraries()
        names = {lib.name for lib in libs}
        assert "TestDevice" in names
        assert "TestPower" in names

    def test_library_types_are_kicad(self):
        libs = self.reader.get_libraries()
        for lib in libs:
            assert lib.lib_type == "KiCad"

    def test_env_var_expanded_in_uris(self):
        libs = self.reader.get_libraries()
        for lib in libs:
            assert "${KICAD_TEST_FIXTURES_DIR}" not in lib.uri

    def test_uris_point_to_fixture_dir(self):
        libs = self.reader.get_libraries()
        for lib in libs:
            assert lib.uri.startswith(str(FIXTURES_DIR))

    def test_uris_point_to_existing_files(self):
        libs = self.reader.get_libraries()
        for lib in libs:
            assert Path(lib.uri).exists(), f"URI not found: {lib.uri}"

    def test_library_descriptions_preserved(self):
        libs = self.reader.get_libraries()
        descs = {lib.descr for lib in libs}
        assert "Test discrete components" in descs
        assert "Test power symbols" in descs

    def test_device_library_uri_ends_with_kicad_sym(self):
        libs = self.reader.get_libraries()
        device = next(lib for lib in libs if lib.name == "TestDevice")
        assert device.uri.endswith("test_device.kicad_sym")

    def test_power_library_uri_ends_with_kicad_sym(self):
        libs = self.reader.get_libraries()
        power = next(lib for lib in libs if lib.name == "TestPower")
        assert power.uri.endswith("test_power.kicad_sym")

    def test_entries_carry_table_path(self):
        libs = self.reader.get_libraries()
        expected = os.path.realpath(
            str(FIXTURES_DIR / "sym-lib-table")
        )
        assert all(lib.table_path == expected for lib in libs)


class TestSymbolIndexReaderMissingTable:
    def test_missing_table_raises_file_not_found(self):
        reader = SymbolIndexReader(_MissingTableConfig())
        with pytest.raises(FileNotFoundError):
            reader.get_libraries()

    def test_default_config_used_when_none_passed(self):
        # SymbolIndexReader() with no args should construct without error.
        reader = SymbolIndexReader()
        assert reader is not None

    def test_reader_with_project_dir_falls_back_to_global(self, tmp_path):
        global_path = _write_global_table(tmp_path)
        empty_proj = tmp_path / "proj"
        empty_proj.mkdir()
        reader = SymbolIndexReader(_TableConfig(global_path), project_dir=str(empty_proj))
        entries = reader.get_libraries()
        assert {e.name for e in entries} == {"TestDevice", "TestPower"}


class TestGetLibrariesProjectMerge:
    def test_project_table_parsed_first_and_wins(self, tmp_path):
        global_path = _write_global_table(tmp_path)
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        _project_table(proj_dir, "${KICAD_TEST_FIXTURES_DIR}/test_power.kicad_sym")

        reader = SymbolIndexReader(_TableConfig(global_path), project_dir=str(proj_dir))
        entries = reader.get_libraries()
        names = [(e.name, e.descr) for e in entries]
        # Project wins on TestDevice (different URI target and descr), the
        # project-only lib is present, and TestPower comes from the global table.
        assert ("TestDevice", "project device") in names
        assert ("ProjOnly", "project local") in names
        assert ("TestPower", "global power") in names
        assert len(entries) == 3

        by_name = {e.name: e for e in entries}
        assert by_name["TestDevice"].uri.endswith("test_power.kicad_sym")
        assert by_name["TestDevice"].table_path == os.path.realpath(
            os.path.join(str(proj_dir), "sym-lib-table")
        )
        assert by_name["TestPower"].table_path == os.path.realpath(global_path)

    def test_kiprjmod_expands_to_project_dir(self, tmp_path):
        global_path = _write_global_table(tmp_path)
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        _project_table(proj_dir, "${KICAD_TEST_FIXTURES_DIR}/test_power.kicad_sym")

        reader = SymbolIndexReader(_TableConfig(global_path), project_dir=str(proj_dir))
        entries = reader.get_libraries()
        by_name = {e.name: e for e in entries}
        assert by_name["ProjOnly"].uri == os.path.join(
            str(proj_dir), "local.kicad_sym"
        )

    def test_no_project_dir_skips_project_merge(self, tmp_path):
        global_path = _write_global_table(tmp_path)
        reader = SymbolIndexReader(_TableConfig(global_path))
        entries = reader.get_libraries()
        by_name = {e.name: e for e in entries}
        # Global TestDevice is used (no project table to override it).
        assert by_name["TestDevice"].descr == "global device"
        assert "ProjOnly" not in by_name
