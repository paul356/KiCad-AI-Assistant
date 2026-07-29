"""
Tests for SymbolIndexReader — reads sym-lib-table and expands ${VAR} in URIs.
"""

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


class TestSymbolIndexReaderMissingTable:
    def test_missing_table_raises_file_not_found(self):
        reader = SymbolIndexReader(_MissingTableConfig())
        with pytest.raises(FileNotFoundError):
            reader.get_libraries()

    def test_default_config_used_when_none_passed(self):
        # SymbolIndexReader() with no args should construct without error.
        reader = SymbolIndexReader()
        assert reader is not None
