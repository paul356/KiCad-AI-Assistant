"""
Tests for component_edit_tools.py (add_symbol_to_schematic /
remove_symbol_from_schematic).

All tests that write to disk work on a temporary copy of
tests/unit/tools/tools_test.kicad_sch so the original is never modified.

The SymbolExtractor / extract_lib_symbol_raw helper is NOT mocked.
Instead, tests/unit/tools/test_symbols.kicad_sym is used as a real library
fixture so the extraction runs on disk.  Only the index manager
(_get_index_manager) is patched to point records at that fixture file.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import skip

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")
TEST_SYM_PATH = str(Path(__file__).parent / "fixtures/test_symbols.kicad_sym")

# The symbol and library names used across tests.
_LIB_NAME = "Device"
_SYM_NAME = "R_Small"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_copy() -> str:
    """Return path to a fresh temporary copy of tools_test.kicad_sch."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir()
    )
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


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
    from kicad_mcp.tools.component_edit_tools import register_component_edit_tools
    mock = _MockMCP()
    register_component_edit_tools(mock)
    return mock.tools


def _make_mock_manager() -> MagicMock:
    """
    Return a mock SymbolIndexManager whose library/symbol records point at the
    test_symbols.kicad_sym fixture.  mtime and file_size are set to 0 so that
    extract_lib_symbol_raw uses the fallback (full-parse) path.
    """
    mgr = MagicMock()

    lib_rec = MagicMock()
    lib_rec.file_path = TEST_SYM_PATH
    lib_rec.mtime = 0.0       # force fallback parse
    lib_rec.file_size = 0

    sym_rec = MagicMock()
    sym_rec.file_index = 0

    mgr.get_library_by_name.return_value = lib_rec
    mgr.get_symbol.return_value = sym_rec

    return mgr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    """Yield a temp copy of the schematic, then clean up."""
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


# ---------------------------------------------------------------------------
# TestAddSymbolToSchematic
# ---------------------------------------------------------------------------

class TestAddSymbolToSchematic:

    def test_adds_symbol_and_assigns_reference(self, tools, tmp_sch):
        """Adding a symbol should succeed and assign a reference like 'R*'."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.0,
                    y=50.0,
                )
            )
        assert result.get("success") is True, result
        ref = result["reference_assigned"]
        assert ref.startswith("R"), f"expected reference like R1, got {ref!r}"
        assert result["units_added"] >= 1

    def test_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after adding."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.0,
                    y=50.0,
                )
            )
        assert os.path.exists(tmp_sch + ".bak")

    def test_grid_alignment(self, tools, tmp_sch):
        """Coordinates should be aligned to the 0.5 mm grid in the response."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.3,   # not on 0.5 grid
                    y=49.7,
                )
            )
        assert result.get("success") is True, result
        pos = result["position"]
        # Must be a multiple of 0.5
        assert abs(pos["x"] % 0.5) < 1e-9, pos
        assert abs(pos["y"] % 0.5) < 1e-9, pos

    def test_auto_increments_reference(self, tools, tmp_sch):
        """Successive add_symbol calls should yield distinct reference designators."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            r1 = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.0,
                    y=50.0,
                )
            )
            r2 = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=60.0,
                    y=60.0,
                )
            )
        assert r1.get("success") and r2.get("success"), (r1, r2)
        assert r1["reference_assigned"] != r2["reference_assigned"]

    def test_invalid_rotation_returns_error(self, tools, tmp_sch):
        """rotation=45 is not valid; should return an error dict."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.0,
                    y=50.0,
                    rotation=45,
                )
            )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools, tmp_dir=None):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["add_symbol_to_schematic"](
                schematic_path="/tmp/bogus.txt",
                library_name=_LIB_NAME,
                symbol_name=_SYM_NAME,
                x=50.0,
                y=50.0,
            )
        )
        assert "error" in result

    def test_library_not_found_returns_error(self, tools, tmp_sch):
        """When the library is absent from the index, return an error."""
        mgr = MagicMock()
        mgr.get_library_by_name.return_value = None
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name="NonExistentLib",
                    symbol_name=_SYM_NAME,
                    x=50.0,
                    y=50.0,
                )
            )
        assert "error" in result

    def test_symbol_not_found_returns_error(self, tools, tmp_sch):
        """When the symbol is absent from the index, return an error."""
        mgr = MagicMock()
        mgr.get_library_by_name.return_value = MagicMock()
        mgr.get_symbol.return_value = None
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name="NoSuchSymbol",
                    x=50.0,
                    y=50.0,
                )
            )
        assert "error" in result


# ---------------------------------------------------------------------------
# TestRemoveSymbolFromSchematic
# ---------------------------------------------------------------------------

class TestRemoveSymbolFromSchematic:

    def test_removes_existing_symbol(self, tools, tmp_sch):
        """remove_symbol_from_schematic should remove R2 successfully."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                reference="R2",
            )
        )
        assert result.get("success") is True, result
        assert result["removed_units"] >= 1

    def test_removed_symbol_no_longer_in_schematic(self, tools, tmp_sch):
        """After removing R2, loading the schematic should show no R2."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                reference="R2",
            )
        )
        sch = skip.Schematic(tmp_sch)
        refs = []
        try:
            for sym in sch.symbol:
                try:
                    refs.append(sym.property.Reference.value)
                except AttributeError:
                    pass
        except AttributeError:
            pass
        assert "R2" not in refs

    def test_other_symbols_preserved(self, tools, tmp_sch):
        """Removing R2 should leave R3, R4, R5 intact."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                reference="R2",
            )
        )
        sch = skip.Schematic(tmp_sch)
        refs = set()
        try:
            for sym in sch.symbol:
                try:
                    refs.add(sym.property.Reference.value)
                except AttributeError:
                    pass
        except AttributeError:
            pass
        for expected in ("R3", "R4", "R5"):
            assert expected in refs, f"{expected} missing after removing R2"

    def test_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after removal."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                reference="R2",
            )
        )
        assert os.path.exists(tmp_sch + ".bak")

    def test_reference_not_found_returns_error(self, tools, tmp_sch):
        """Removing a non-existent reference should return an error."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                reference="Z99",
            )
        )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path="/tmp/bogus.txt",
                reference="R2",
            )
        )
        assert "error" in result
