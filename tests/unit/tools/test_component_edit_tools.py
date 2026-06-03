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
import contextlib
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import skip

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")
SHEET_SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/sheet_netlist_test.kicad_sch")
TEST_SYM_PATH = str(Path(__file__).parent / "fixtures/test_symbols.kicad_sym")

# The symbol and library names used across tests.
_LIB_NAME = "Device"
_SYM_NAME = "R_Small"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_copy() -> str:
    """Return path to a fresh temporary copy of tools_test.kicad_sch."""
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir())
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
    from kcaa.tools.component_edit_tools import register_component_edit_tools

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
    lib_rec.mtime = 0.0  # force fallback parse
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


@pytest.fixture()
def tmp_sheet_sch(tmp_path):
    path = tmp_path / "sheet_netlist_test.kicad_sch"
    shutil.copy(SHEET_SCHEMATIC_PATH, path)
    yield str(path)
    backup = str(path) + ".bak"
    if os.path.exists(backup):
        os.unlink(backup)


# ---------------------------------------------------------------------------
# TestAddSymbolToSchematic
# ---------------------------------------------------------------------------


class TestAddSymbolToSchematic:
    def test_adds_symbol_and_assigns_reference(self, tools, tmp_sch):
        """Adding a symbol should succeed and assign a reference like 'R*'."""
        mgr = _make_mock_manager()
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
        """Coordinates should be aligned to the 1.27 mm (50 mil) grid in the response."""
        mgr = _make_mock_manager()
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.3,  # not on 1.27 mm grid
                    y=49.7,
                )
            )
        assert result.get("success") is True, result
        pos = result["position"]
        grid = 1.27  # KiCad default schematic grid: 50 mils = 1.27 mm
        # Must be a multiple of 1.27 mm
        assert abs(pos["x"] / grid - round(pos["x"] / grid)) < 1e-6, pos
        assert abs(pos["y"] / grid - round(pos["y"] / grid)) < 1e-6, pos

    def test_auto_increments_reference(self, tools, tmp_sch):
        """Successive add_symbol calls should yield distinct reference designators."""
        mgr = _make_mock_manager()
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
        with patch("kcaa.tools.component_edit_tools._get_index_manager", return_value=mgr):
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
                references=["R2"],
            )
        )
        assert result.get("success") is True, result
        assert result["total_removed_units"] >= 1
        assert result["results"]["R2"]["removed_units"] >= 1

    def test_removed_symbol_no_longer_in_schematic(self, tools, tmp_sch):
        """After removing R2, loading the schematic should show no R2."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["R2"],
            )
        )
        sch = skip.Schematic(tmp_sch)
        refs = []
        try:
            for sym in sch.symbol:
                with contextlib.suppress(AttributeError):
                    refs.append(sym.property.Reference.value)
        except AttributeError:
            pass
        assert "R2" not in refs

    def test_other_symbols_preserved(self, tools, tmp_sch):
        """Removing R2 should leave R3, R4, R5 intact."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["R2"],
            )
        )
        sch = skip.Schematic(tmp_sch)
        refs = set()
        try:
            for sym in sch.symbol:
                with contextlib.suppress(AttributeError):
                    refs.add(sym.property.Reference.value)
        except AttributeError:
            pass
        for expected in ("R3", "R4", "R5"):
            assert expected in refs, f"{expected} missing after removing R2"

    def test_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after removal."""
        asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["R2"],
            )
        )
        assert os.path.exists(tmp_sch + ".bak")

    def test_reference_not_found_returns_error(self, tools, tmp_sch):
        """Removing a non-existent reference should return an error."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["Z99"],
            )
        )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path="/tmp/bogus.txt",
                references=["R2"],
            )
        )
        assert "error" in result

    def test_empty_references_list_returns_error(self, tools, tmp_sch):
        """An empty references list should be rejected immediately."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=[],
            )
        )
        assert "error" in result

    def test_batch_remove_multiple_symbols(self, tools, tmp_sch):
        """Batch delete R2 and R3 in one call; both should be gone afterwards."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["R2", "R3"],
            )
        )
        assert result.get("success") is True, result
        assert result["total_removed_units"] >= 2
        assert result["results"]["R2"]["removed_units"] >= 1
        assert result["results"]["R3"]["removed_units"] >= 1

        sch = skip.Schematic(tmp_sch)
        refs = set()
        try:
            for sym in sch.symbol:
                with contextlib.suppress(AttributeError):
                    refs.add(sym.property.Reference.value)
        except AttributeError:
            pass
        assert "R2" not in refs, "R2 should have been removed"
        assert "R3" not in refs, "R3 should have been removed"

    def test_batch_partial_not_found_reports_per_entry(self, tools, tmp_sch):
        """When some references exist and some don't, successes and errors are reported per entry."""
        result = asyncio.run(
            tools["remove_symbol_from_schematic"](
                schematic_path=tmp_sch,
                references=["R2", "Z99"],
            )
        )
        # Overall: at least one was removed so success=True
        assert result.get("success") is True, result
        assert result["results"]["R2"]["removed_units"] >= 1
        assert "error" in result["results"]["Z99"]


# ---------------------------------------------------------------------------
# TestSetComponentProperty
# ---------------------------------------------------------------------------


class TestSetComponentProperty:
    def test_update_existing_value(self, tools, tmp_sch):
        """Updating the Value property of an existing component should succeed."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
                property_value="999k",
            )
        )
        assert result.get("success") is True, result
        assert result["action"] == "updated"
        assert result["units_where_updated"] == 1
        assert result["units_where_added"] == 0

    def test_update_persists_after_write(self, tools, tmp_sch):
        """The updated Value should be readable from the written file."""
        asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
                property_value="47k",
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert sym.property.Value.value == "47k"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_add_new_property(self, tools, tmp_sch):
        """Adding a new custom property (MPN) should report action=='added'."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
                property_value="RC0402FR-0710KL",
            )
        )
        assert result.get("success") is True, result
        assert result["action"] == "added"
        assert result["units_where_added"] == 1
        assert result["units_where_updated"] == 0

    def test_new_property_persists_after_write(self, tools, tmp_sch):
        """A newly added property should be readable from the written file."""
        asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
                property_value="RC0402FR-0710KL",
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert "MPN" in sym.property
                    assert sym.property.MPN.value == "RC0402FR-0710KL"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_new_property_is_hidden(self, tools, tmp_sch):
        """Non-standard properties should have (hide yes) in their effects."""
        import sexpdata

        asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Manufacturer",
                property_value="Yageo",
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value != "R1":
                    continue
            except AttributeError:
                continue
            # Walk the raw tree to verify (hide yes) — both key AND value.
            for prop in sym.property:
                try:
                    if prop.children[0] != "Manufacturer":
                        continue
                except (AttributeError, IndexError):
                    continue
                raw_tree = prop._pv._tree
                for child in raw_tree:
                    if (
                        isinstance(child, list)
                        and len(child) >= 1
                        and isinstance(child[0], sexpdata.Symbol)
                        and child[0].value() == "effects"
                    ):
                        hide_yes_found = any(
                            isinstance(c, list)
                            and len(c) >= 2
                            and isinstance(c[0], sexpdata.Symbol)
                            and c[0].value() == "hide"
                            and isinstance(c[1], sexpdata.Symbol)
                            and c[1].value() == "yes"
                            for c in child
                        )
                        assert hide_yes_found, (
                            f"Expected (hide yes) in effects of new property, got effects: {child}"
                        )
                        return
                pytest.fail("No effects node found on Manufacturer property")
        pytest.fail("R1 not found in written schematic")

    def test_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after editing."""
        asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
                property_value="1k",
            )
        )
        assert os.path.exists(tmp_sch + ".bak")

    def test_reference_not_found_returns_error(self, tools, tmp_sch):
        """An unknown reference should return an error dict."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="Z99",
                property_name="Value",
                property_value="1k",
            )
        )
        assert "error" in result

    def test_empty_reference_returns_error(self, tools, tmp_sch):
        """An empty reference string should be rejected."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="",
                property_name="Value",
                property_value="1k",
            )
        )
        assert "error" in result

    def test_empty_property_name_returns_error(self, tools, tmp_sch):
        """An empty property_name should be rejected."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="",
                property_value="1k",
            )
        )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path="/tmp/bogus.txt",
                reference="R1",
                property_name="Value",
                property_value="1k",
            )
        )
        assert "error" in result

    def test_update_is_idempotent(self, tools, tmp_sch):
        """Calling set_component_property twice should not duplicate properties."""
        asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
                property_value="RC0402FR-0710KL",
            )
        )
        result2 = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
                property_value="RC0402FR-0710KL",
            )
        )
        # Second call should update (not add) the existing property.
        assert result2.get("success") is True, result2
        assert result2["action"] == "updated", (
            f"Second call should be 'updated', got {result2['action']!r}"
        )
        # Reload from disk: exactly one MPN property should exist.
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value != "R1":
                    continue
            except AttributeError:
                continue
            mpn_count = sum(1 for p in sym.property if getattr(p, "children", [None])[0] == "MPN")
            assert mpn_count == 1, f"Expected 1 MPN property, found {mpn_count}"
            assert sym.property.MPN.value == "RC0402FR-0710KL"
            return
        pytest.fail("R1 not found in written schematic")

    def test_units_updated_count(self, tools, tmp_sch):
        """units_updated should equal the number of units with the reference."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
                property_value="2k2",
            )
        )
        assert result.get("success") is True, result
        # tools_test.kicad_sch has single-unit symbols; R1 has 1 unit.
        assert result["units_updated"] == 1

    def test_empty_property_value_accepted(self, tools, tmp_sch):
        """An empty property_value is valid and should be persisted."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
                property_value="",
            )
        )
        assert result.get("success") is True, result
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert sym.property.Value.value == ""
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_file_not_found_returns_error(self, tools):
        """A path to a non-existent .kicad_sch file should return an error."""
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path="/tmp/does_not_exist.kicad_sch",
                reference="R1",
                property_name="Value",
                property_value="1k",
            )
        )
        assert "error" in result

    def test_footprint_update_is_hidden(self, tools, tmp_sch):
        """When adding a new Footprint property, it should be hidden per KiCad convention."""
        import sexpdata

        # Use a reference whose Footprint property is absent in the fixture.
        # Add a fresh symbol first (R8 doesn't exist), then set its Footprint.
        # Alternatively, confirm an existing component's Footprint value can be
        # updated and its hidden state is preserved from the original.
        result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Footprint",
                property_value="R_0402",
            )
        )
        assert result.get("success") is True, result
        # Footprint already exists on placed symbols; updating preserves its
        # existing hide state (which is hide=yes per KiCad convention).
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value != "R1":
                    continue
            except AttributeError:
                continue
            for prop in sym.property:
                try:
                    if prop.children[0] != "Footprint":
                        continue
                except (AttributeError, IndexError):
                    continue
                assert prop.value == "R_0402", f"Unexpected Footprint value: {prop.value}"
                # Confirm the property is still hidden in the written file.
                raw_tree = prop._pv._tree
                for child in raw_tree:
                    if (
                        isinstance(child, list)
                        and len(child) >= 1
                        and isinstance(child[0], sexpdata.Symbol)
                        and child[0].value() == "hide"
                        and len(child) >= 2
                        and isinstance(child[1], sexpdata.Symbol)
                        and child[1].value() == "yes"
                    ):
                        return  # hide=yes confirmed
                # Also check inside (effects ...) for KiCad10 format
                for child in raw_tree:
                    if (
                        isinstance(child, list)
                        and len(child) >= 1
                        and isinstance(child[0], sexpdata.Symbol)
                        and child[0].value() == "effects"
                    ):
                        hide_yes = any(
                            isinstance(c, list)
                            and len(c) >= 2
                            and isinstance(c[0], sexpdata.Symbol)
                            and c[0].value() == "hide"
                            and isinstance(c[1], sexpdata.Symbol)
                            and c[1].value() == "yes"
                            for c in child
                        )
                        if hide_yes:
                            return
                pytest.fail("Footprint property is not hidden after update")
        pytest.fail("R1 not found in written schematic")


# ---------------------------------------------------------------------------
# TestListComponentProperties
# ---------------------------------------------------------------------------


class TestListComponentProperties:
    def test_returns_expected_properties(self, tools, tmp_sch):
        """list_component_properties should return Reference, Value, etc. for R1."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="R1",
            )
        )
        assert result.get("success") is True, result
        assert result["reference"] == "R1"
        names = [p["name"] for p in result["properties"]]
        assert "Reference" in names
        assert "Value" in names

    def test_returns_correct_values(self, tools, tmp_sch):
        """The values returned should match what is in the fixture schematic."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="R1",
            )
        )
        assert result.get("success") is True, result
        by_name = {p["name"]: p["value"] for p in result["properties"]}
        assert by_name["Reference"] == "R1"
        assert by_name["Value"] == "R_Small"

    def test_reference_not_found_returns_error(self, tools, tmp_sch):
        """An unknown reference should return an error dict."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="Z99",
            )
        )
        assert "error" in result

    def test_empty_reference_returns_error(self, tools, tmp_sch):
        """An empty reference string should be rejected."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="",
            )
        )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path="/tmp/bogus.txt",
                reference="R1",
            )
        )
        assert "error" in result

    def test_file_not_found_returns_error(self, tools):
        """A path to a non-existent .kicad_sch file should return an error."""
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path="/tmp/does_not_exist.kicad_sch",
                reference="R1",
            )
        )
        assert "error" in result

    def test_does_not_create_backup(self, tools, tmp_sch):
        """list_component_properties is read-only and must not write a backup."""
        asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="R1",
            )
        )
        assert not os.path.exists(tmp_sch + ".bak")

    def test_round_trip_with_set_property(self, tools, tmp_sch):
        """A property added via set_component_property should appear in the list."""
        add_result = asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
                property_value="RC0402FR-0710KL",
            )
        )
        assert add_result.get("success") is True, f"Setup failed: {add_result}"
        result = asyncio.run(
            tools["list_component_properties"](
                schematic_path=tmp_sch,
                reference="R1",
            )
        )
        assert result.get("success") is True, result
        by_name = {p["name"]: p["value"] for p in result["properties"]}
        assert "MPN" in by_name, f"MPN not found in properties: {list(by_name)}"
        assert by_name["MPN"] == "RC0402FR-0710KL"


# ---------------------------------------------------------------------------
# TestDeleteComponentProperty
# ---------------------------------------------------------------------------


class TestDeleteComponentProperty:
    def _add_custom_property(self, tools, tmp_sch, name="MPN", value="RC0402FR-0710KL"):
        """Helper: add a custom property to R1 and return the result."""
        return asyncio.run(
            tools["set_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name=name,
                property_value=value,
            )
        )

    def test_delete_custom_property_succeeds(self, tools, tmp_sch):
        """Deleting a custom property should return success."""
        add_result = self._add_custom_property(tools, tmp_sch)
        assert add_result.get("success") is True, f"Setup failed: {add_result}"
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
            )
        )
        assert result.get("success") is True, result
        assert result["units_updated"] == 1

    def test_deleted_property_absent_on_reload(self, tools, tmp_sch):
        """After deletion the property should not appear when reloading the file."""
        add_result = self._add_custom_property(tools, tmp_sch)
        assert add_result.get("success") is True, f"Setup failed: {add_result}"
        asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value != "R1":
                    continue
            except AttributeError:
                continue
            names = []
            try:
                for prop in sym.property:
                    with contextlib.suppress(AttributeError, IndexError):
                        names.append(prop.children[0])
            except AttributeError:
                pass
            assert "MPN" not in names, f"MPN still present after deletion: {names}"
            return
        pytest.fail("R1 not found in reloaded schematic")

    def test_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after deletion."""
        add_result = self._add_custom_property(tools, tmp_sch)
        assert add_result.get("success") is True, f"Setup failed: {add_result}"
        asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="MPN",
            )
        )
        assert os.path.exists(tmp_sch + ".bak")

    def test_delete_reference_is_rejected(self, tools, tmp_sch):
        """Attempting to delete the Reference property should return an error."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Reference",
            )
        )
        assert "error" in result
        # File must be unchanged — R1 reference must still exist.
        sch = skip.Schematic(tmp_sch)
        refs = [
            sym.property.Reference.value
            for sym in sch.symbol
            if hasattr(sym, "property") and hasattr(sym.property, "Reference")
        ]
        assert "R1" in refs

    def test_delete_value_is_rejected(self, tools, tmp_sch):
        """Attempting to delete the Value property should return an error."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="Value",
            )
        )
        assert "error" in result
        # File must be unchanged — Value must still exist on R1.
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert sym.property.Value.value == "R_Small"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found after rejected delete")

    def test_property_not_found_returns_error(self, tools, tmp_sch):
        """Deleting a property that does not exist should return an error."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="NonExistentProp",
            )
        )
        assert "error" in result

    def test_reference_not_found_returns_error(self, tools, tmp_sch):
        """An unknown reference should return an error dict."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="Z99",
                property_name="MPN",
            )
        )
        assert "error" in result

    def test_empty_reference_returns_error(self, tools, tmp_sch):
        """An empty reference string should be rejected."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="",
                property_name="MPN",
            )
        )
        assert "error" in result

    def test_empty_property_name_returns_error(self, tools, tmp_sch):
        """An empty property_name should be rejected."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path=tmp_sch,
                reference="R1",
                property_name="",
            )
        )
        assert "error" in result

    def test_file_not_found_returns_error(self, tools):
        """A path to a non-existent .kicad_sch file should return an error."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path="/tmp/does_not_exist.kicad_sch",
                reference="R1",
                property_name="MPN",
            )
        )
        assert "error" in result

    def test_invalid_extension_returns_error(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["delete_component_property"](
                schematic_path="/tmp/bogus.txt",
                reference="R1",
                property_name="MPN",
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# TestMoveComponent
# ---------------------------------------------------------------------------


class TestMoveComponent:
    def test_rotate_only_happy_path(self, tools, tmp_sch):
        """move_component with only rotation should succeed."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                rotation=90,
            )
        )
        assert result.get("success") is True, result
        assert result["rotation"] == 90
        assert result["units_updated"] >= 1
        assert "position" in result

    def test_rotate_persists(self, tools, tmp_sch):
        """The new rotation should be readable from the written file."""
        asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                rotation=180,
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert sym.at.value[2] == 180, f"Expected 180, got {sym.at.value[2]}"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_rotate_position_preserved(self, tools, tmp_sch):
        """x/y coordinates must not change when only the rotation is updated."""
        sch_orig = skip.Schematic(tmp_sch)
        orig_x = orig_y = None
        for sym in sch_orig.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    orig_x, orig_y = sym.at.value[0], sym.at.value[1]
                    break
            except AttributeError:
                continue
        assert orig_x is not None, "R1 not found in fixture"

        asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                rotation=270,
            )
        )
        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert sym.at.value[0] == orig_x, f"x changed: {sym.at.value[0]} != {orig_x}"
                    assert sym.at.value[1] == orig_y, f"y changed: {sym.at.value[1]} != {orig_y}"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_move_xy_only(self, tools, tmp_sch):
        """move_component with only x/y should update position but not rotation."""
        sch_orig = skip.Schematic(tmp_sch)
        orig_rot = None
        for sym in sch_orig.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    orig_rot = sym.at.value[2] if len(sym.at.value) > 2 else 0
                    break
            except AttributeError:
                continue

        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                x=55.88,  # grid-aligned: 44 * 1.27
                y=66.04,  # grid-aligned: 52 * 1.27
            )
        )
        assert result.get("success") is True, result
        assert abs(result["position"]["x"] - 55.88) < 0.001
        assert abs(result["position"]["y"] - 66.04) < 0.001

        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert abs(sym.at.value[0] - 55.88) < 0.001, "x not updated"
                    assert abs(sym.at.value[1] - 66.04) < 0.001, "y not updated"
                    if orig_rot is not None:
                        assert sym.at.value[2] == orig_rot, "rotation should not change"
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_move_xy_and_rotation(self, tools, tmp_sch):
        """move_component with x, y, and rotation should update all three."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                x=76.20,  # 60 * 1.27
                y=88.90,  # 70 * 1.27
                rotation=180,
            )
        )
        assert result.get("success") is True, result
        assert abs(result["position"]["x"] - 76.20) < 0.001
        assert abs(result["position"]["y"] - 88.90) < 0.001
        assert result["rotation"] == 180

        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R1":
                    assert abs(sym.at.value[0] - 76.20) < 0.001
                    assert abs(sym.at.value[1] - 88.90) < 0.001
                    assert sym.at.value[2] == 180
                    return
            except AttributeError:
                continue
        pytest.fail("R1 not found in written schematic")

    def test_move_creates_backup(self, tools, tmp_sch):
        """A .bak file should appear next to the schematic after moving."""
        asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                rotation=90,
            )
        )
        assert os.path.exists(tmp_sch + ".bak")

    def test_fields_autoplaced_added(self, tools, tmp_sch):
        """fields_autoplaced must appear in the S-expression after move_component."""
        import sexpdata

        sch_before = skip.Schematic(tmp_sch)
        r2_sym_before = None
        for sym in sch_before.symbol:
            try:
                if sym.property.Reference.value == "R2":
                    r2_sym_before = sym
                    break
            except AttributeError:
                continue
        assert r2_sym_before is not None, "R2 not found in fixture"
        raw_before = r2_sym_before._pv._tree
        fa_idx_before = -1
        for i, child in enumerate(raw_before):
            if (
                isinstance(child, list)
                and len(child) >= 1
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "fields_autoplaced"
            ):
                fa_idx_before = i
                break
        assert fa_idx_before == -1, (
            "Fixture R2 already has fields_autoplaced — pick a different reference"
        )

        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R2",
                rotation=90,
            )
        )
        assert result.get("success") is True, result

        sch_after = skip.Schematic(tmp_sch)
        for sym in sch_after.symbol:
            try:
                if sym.property.Reference.value == "R2":
                    assert sym.at.value[2] == 90, f"Expected rotation 90, got {sym.at.value[2]}"
                    raw_after = sym._pv._tree
                    fa_present = any(
                        isinstance(child, list)
                        and len(child) >= 1
                        and isinstance(child[0], sexpdata.Symbol)
                        and child[0].value() == "fields_autoplaced"
                        for child in raw_after
                    )
                    assert fa_present, "fields_autoplaced node not found in R2 after move"
                    return
            except AttributeError:
                continue
        pytest.fail("R2 not found in written schematic")

    def test_fields_autoplaced_insert_ordering(self, tools, tmp_sch):
        """When fields_autoplaced is inserted it must appear BEFORE the uuid node."""
        import sexpdata

        def _find_tag_idx(raw_tree, tag_name):
            for i, child in enumerate(raw_tree):
                if (
                    isinstance(child, list)
                    and len(child) >= 1
                    and isinstance(child[0], sexpdata.Symbol)
                    and child[0].value() == tag_name
                ):
                    return i
            return -1

        asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R2",
                rotation=90,
            )
        )

        sch = skip.Schematic(tmp_sch)
        for sym in sch.symbol:
            try:
                if sym.property.Reference.value == "R2":
                    raw_tree = sym._pv._tree
                    fa_idx = _find_tag_idx(raw_tree, "fields_autoplaced")
                    uuid_idx = _find_tag_idx(raw_tree, "uuid")
                    assert fa_idx != -1, "fields_autoplaced node missing after move"
                    assert uuid_idx != -1, "uuid node missing after move"
                    assert fa_idx < uuid_idx, (
                        f"fields_autoplaced (idx={fa_idx}) must precede uuid (idx={uuid_idx})"
                    )
                    return
            except AttributeError:
                continue
        pytest.fail("R2 not found in written schematic")

    def test_no_args_returns_error(self, tools, tmp_sch):
        """Calling with no x/y/rotation should return an error."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
            )
        )
        assert "error" in result

    def test_invalid_rotation(self, tools, tmp_sch):
        """rotation=45 is not valid; should return an error dict."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                rotation=45,
            )
        )
        assert "error" in result

    def test_non_finite_x(self, tools, tmp_sch):
        """Non-finite x coordinate should be rejected."""
        import math as _math

        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="R1",
                x=_math.inf,
            )
        )
        assert "error" in result

    def test_reference_not_found(self, tools, tmp_sch):
        """A non-existent reference should return an error dict."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="ZZZNOTEXIST",
                rotation=90,
            )
        )
        assert "error" in result

    def test_moves_sheet_by_name(self, tools, tmp_sheet_sch):
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sheet_sch,
                reference="Power",
                x=120.0,
                y=80.0,
            )
        )

        assert result == {
            "success": True,
            "sheet_name": "Power",
            "position": {"x": 119.38, "y": 80.01},
            "type": "sheet",
        }

    def test_sheet_move_rejects_rotation(self, tools, tmp_sheet_sch):
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sheet_sch,
                reference="Power",
                rotation=90,
            )
        )

        assert result == {"error": "rotation is not supported for sheet symbols"}

    def test_empty_reference(self, tools, tmp_sch):
        """An empty reference string should be rejected."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path=tmp_sch,
                reference="",
                rotation=90,
            )
        )
        assert "error" in result

    def test_bad_extension(self, tools):
        """A non-.kicad_sch path should be rejected immediately."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path="/tmp/bogus.txt",
                reference="R1",
                rotation=90,
            )
        )
        assert "error" in result

    def test_file_not_found(self, tools):
        """A non-existent .kicad_sch path should return an error."""
        result = asyncio.run(
            tools["move_component"](
                schematic_path="/tmp/does_not_exist.kicad_sch",
                reference="R1",
                rotation=90,
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests for cross-file _next_reference
# ---------------------------------------------------------------------------


class TestNextReferenceCrossFile:
    """Tests for _next_reference with project-wide scanning."""

    @pytest.fixture()
    def project_dir(self, tmp_path):
        """Create a minimal KiCad project with two schematic files."""
        proj = tmp_path / "test_proj"
        proj.mkdir()
        # Create the .kicad_pro marker.
        (proj / "test_proj.kicad_pro").write_text("{}")

        # Create a parent schematic.  We use a minimal but valid kicad_sch
        # sexp so that regex scans work.  (skip.Schematic would need more
        # structure, but we only use regex in cross-file tests.)
        parent_sch = proj / "test_proj.kicad_sch"
        parent_sch.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R1" (at 0 0 0))\n'
            "  )\n"
            '  (symbol (lib_id "Device:R") (at 10 0 0)\n'
            '    (property "Reference" "R3" (at 10 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        # Create a sub-sheet schematic with R2.
        sub_dir = proj / "sub"
        sub_dir.mkdir()
        sub_sch = sub_dir / "sub.kicad_sch"
        sub_sch.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R2" (at 0 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        return proj, parent_sch, sub_sch

    def test_find_project_dir_found(self):
        """_find_project_dir returns the dir containing .kicad_pro."""
        import tempfile

        from kcaa.tools.component_edit_tools import _find_project_dir

        with tempfile.TemporaryDirectory() as td:
            proj_dir = Path(td) / "proj"
            proj_dir.mkdir()
            (proj_dir / "proj.kicad_pro").write_text("{}")
            sch = proj_dir / "proj.kicad_sch"
            sch.write_text("(kicad_sch)\n")
            result = _find_project_dir(str(sch))
            assert result == proj_dir

    def test_find_project_dir_not_found(self):
        """_find_project_dir returns None when no .kicad_pro exists."""
        import tempfile

        from kcaa.tools.component_edit_tools import _find_project_dir

        with tempfile.TemporaryDirectory() as td:
            sch = Path(td) / "orphan.kicad_sch"
            sch.write_text("(kicad_sch)\n")
            result = _find_project_dir(str(sch))
            assert result is None

    def test_collect_project_references(self, project_dir):
        """_collect_project_references gathers refs from all schematics."""
        from kcaa.tools.component_edit_tools import _collect_project_references

        proj, parent_sch, sub_sch = project_dir
        refs = _collect_project_references(str(parent_sch))
        assert len(refs) == 2  # two .kicad_sch files
        parent_refs = refs[str(parent_sch)]
        sub_refs = refs[str(sub_sch)]
        assert parent_refs == {"R1", "R3"}
        assert sub_refs == {"R2"}

    def test_next_reference_skips_own_file_but_scans_others(self, project_dir):
        """_next_reference should see R1, R3 from parent + R2 from sub,
        giving R4 (not R2 which only exists in sub-sheet)."""
        from kcaa.tools.component_edit_tools import _next_reference

        proj, parent_sch, sub_sch = project_dir

        # Create a mock sch that has no symbols of its own, so the
        # cross-file scan is the sole source of reference data.
        class _FakeSym:
            class _Prop:
                Reference = type("Ref", (), {"value": "R0"})()

            property = _Prop()

        class _FakeSch:
            symbol = [_FakeSym()]

        fake_sch = _FakeSch()

        # From the project, parent has R1,R3 and sub has R2; max is 3 → R4.
        ref = _next_reference(fake_sch, "R", schematic_path=str(sub_sch))
        assert ref == "R4"

    def test_next_reference_no_project_uses_current_sch_only(self, tmp_sch):
        """When there's no project, _next_reference falls back to current file only."""
        from kcaa.tools.component_edit_tools import _next_reference
        from kcaa.utils.skip_compat import safe_schematic

        sch = safe_schematic(tmp_sch)
        # Only scan the current schematic (no schematic_path); the fixture has
        # R1 through R7, so max is 7 and next should be 8.
        ref_no_path = _next_reference(sch, "R")
        assert ref_no_path == "R8"
        # With schematic_path but no project found, should return the same.
        ref_with_path = _next_reference(sch, "R", schematic_path=tmp_sch)
        assert ref_with_path == "R8"


# ---------------------------------------------------------------------------
# Tests for rename_symbol
# ---------------------------------------------------------------------------


class TestRenameSymbol:
    """Tests for the rename_symbol tool."""

    def test_successful_rename(self, tools, tmp_sch):
        """Renaming R1 to R10 should update the reference."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="R1",
                to_reference="R10",
            )
        )
        assert result.get("success") is True, result
        assert result["from_reference"] == "R1"
        assert result["to_reference"] == "R10"
        assert result["units_updated"] >= 1

        # Verify the file was actually changed.
        sch = skip.Schematic(tmp_sch)
        found_new = False
        found_old = False
        for sym in sch.symbol:
            try:
                ref = sym.property.Reference.value
                if ref == "R10":
                    found_new = True
                if ref == "R1":
                    found_old = True
            except AttributeError:
                continue
        assert found_new, "R10 should exist after rename"
        assert not found_old, "R1 should not exist after rename"

    def test_from_reference_not_found(self, tools, tmp_sch):
        """Renaming a non-existent reference should return an error."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="ZZ99",
                to_reference="R10",
            )
        )
        assert "error" in result

    def test_to_reference_already_exists(self, tools, tmp_sch):
        """Renaming to an existing reference should return an error."""
        # R5 already exists in tmp_sch.
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="R1",
                to_reference="R5",
            )
        )
        assert "error" in result
        assert "already exists" in result["error"]

    def test_same_from_and_to(self, tools, tmp_sch):
        """Renaming R1 to R1 should return an error."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="R1",
                to_reference="R1",
            )
        )
        assert "error" in result

    def test_empty_from_reference(self, tools, tmp_sch):
        """Empty from_reference should return an error."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="",
                to_reference="R10",
            )
        )
        assert "error" in result

    def test_auto_assign_next_reference(self, tools, tmp_sch):
        """When to_reference is omitted, auto-assign the next free reference."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="R1",
                # to_reference omitted — auto-assign
            )
        )
        assert result.get("success") is True, result
        assert result["auto_assigned"] is True
        # tmp_sch has R1..R7, so next should be R8.
        assert result["to_reference"] == "R8"

        # Verify R1 is gone and R8 is present.
        sch = skip.Schematic(tmp_sch)
        refs = set()
        for sym in sch.symbol:
            try:
                refs.add(sym.property.Reference.value)
            except AttributeError:
                continue
        assert "R1" not in refs
        assert "R8" in refs

    def test_explicit_to_reference_not_auto_assigned(self, tools, tmp_sch):
        """When to_reference is provided, auto_assigned should be False."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path=tmp_sch,
                from_reference="R1",
                to_reference="R10",
            )
        )
        assert result.get("success") is True, result
        assert result["auto_assigned"] is False
        assert result["to_reference"] == "R10"

    def test_file_not_found(self, tools):
        """Non-existent file should return an error."""
        result = asyncio.run(
            tools["rename_symbol"](
                schematic_path="/nonexistent/schematic.kicad_sch",
                from_reference="R1",
                to_reference="R10",
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests for check_reference_conflicts
# ---------------------------------------------------------------------------


class TestCheckReferenceConflicts:
    """Tests for the check_reference_conflicts tool."""

    @pytest.fixture()
    def clean_project(self, tmp_path):
        """Create a project with unique references across sheets."""
        proj = tmp_path / "clean_proj"
        proj.mkdir()
        (proj / "clean_proj.kicad_pro").write_text("{}")
        parent = proj / "clean_proj.kicad_sch"
        parent.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R1" (at 0 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        sub = proj / "power.kicad_sch"
        sub.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R2" (at 0 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        return proj, parent, sub

    @pytest.fixture()
    def conflict_project(self, tmp_path):
        """Create a project with conflicting references across sheets."""
        proj = tmp_path / "conflict_proj"
        proj.mkdir()
        (proj / "conflict_proj.kicad_pro").write_text("{}")
        # Parent has R1 and C1.
        parent = proj / "conflict_proj.kicad_sch"
        parent.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R1" (at 0 0 0))\n'
            "  )\n"
            '  (symbol (lib_id "Device:C") (at 10 0 0)\n'
            '    (property "Reference" "C1" (at 10 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        # Sub-sheet has R1 (conflict!) and C2.
        sub = proj / "power.kicad_sch"
        sub.write_text(
            '(kicad_sch (version 20240108) (generator "eeschema")\n'
            '  (symbol (lib_id "Device:R") (at 0 0 0)\n'
            '    (property "Reference" "R1" (at 0 0 0))\n'
            "  )\n"
            '  (symbol (lib_id "Device:C") (at 10 0 0)\n'
            '    (property "Reference" "C2" (at 10 0 0))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            ")\n"
        )
        return proj, parent, sub

    def test_detects_conflict(self, tools, conflict_project):
        """Should detect R1 appearing in both parent and sub-sheet."""
        proj, parent, sub = conflict_project
        result = asyncio.run(
            tools["check_reference_conflicts"](
                schematic_path=str(parent),
            )
        )
        assert result.get("success") is True, result
        assert result["schematics_scanned"] == 2
        conflicts = result["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["reference"] == "R1"
        assert len(conflicts[0]["sheets"]) == 2
        assert str(parent) in conflicts[0]["sheets"]
        assert str(sub) in conflicts[0]["sheets"]

    def test_no_conflicts(self, tools, clean_project):
        """A clean project should have zero conflicts."""
        proj, parent_sch, sub_sch = clean_project
        result = asyncio.run(
            tools["check_reference_conflicts"](
                schematic_path=str(parent_sch),
            )
        )
        assert result.get("success") is True, result
        assert result["schematics_scanned"] == 2
        assert result["conflicts"] == []

    def test_no_project_found(self, tools, tmp_sch):
        """When no .kicad_pro is found, return empty with a message."""
        result = asyncio.run(
            tools["check_reference_conflicts"](
                schematic_path=tmp_sch,
            )
        )
        assert result.get("success") is True, result
        assert result["project_dir"] is None
        assert result["schematics_scanned"] == 0
        assert result["conflicts"] == []

    def test_invalid_file_extension(self, tools):
        """Non-.kicad_sch paths should return an error."""
        result = asyncio.run(
            tools["check_reference_conflicts"](
                schematic_path="/tmp/test.kicad_pcb",
            )
        )
        assert "error" in result
