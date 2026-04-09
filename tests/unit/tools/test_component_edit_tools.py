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
        """Coordinates should be aligned to the 1.27 mm (50 mil) grid in the response."""
        mgr = _make_mock_manager()
        with patch("kicad_mcp.tools.component_edit_tools._get_index_manager", return_value=mgr):
            result = asyncio.run(
                tools["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=50.3,   # not on 1.27 mm grid
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
                            "Expected (hide yes) in effects of new property, "
                            f"got effects: {child}"
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
            mpn_count = sum(
                1 for p in sym.property
                if getattr(p, "children", [None])[0] == "MPN"
            )
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
                    try:
                        names.append(prop.children[0])
                    except (AttributeError, IndexError):
                        pass
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
