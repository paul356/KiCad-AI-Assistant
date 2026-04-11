"""
Tests for wire-related tools in schematic_edit_tools.py.

Uses smart-keyboard.kicad_sch as the reference schematic.  Every test that
writes to disk works on a temporary copy so the original file is never
modified.

Pin positions in smart-keyboard.kicad_sch (rotation=0 for all symbols):
    R2 (at 100,100):  pin1 -> (100.0, 97.46)  pin2 -> (100.0, 102.54)
    R3 (at 120,100):  pin1 -> (120.0, 97.46)  pin2 -> (120.0, 102.54)
    R4 (at 140,100):  pin1 -> (140.0, 97.46)  pin2 -> (140.0, 102.54)
    R5 (at 160,100):  pin1 -> (160.0, 97.46)  pin2 -> (160.0, 102.54)
    C1 (at 150,100):  pin1 -> (150.0, 96.19)  pin2 -> (150.0, 103.81)
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import skip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMATIC_PATH = str(
    Path(__file__).parent / "fixtures" / "tools_test.kicad_sch"
)


def _make_temp_copy() -> str:
    """Return path to a fresh temporary copy of smart-keyboard.kicad_sch."""
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
    """Register schematic and wire edit tools against a mock MCP and return the dict."""
    from kicad_mcp.tools.component_edit_tools import register_component_edit_tools
    from kicad_mcp.tools.wire_edit_tools import register_wire_edit_tools
    mock = _MockMCP()
    register_component_edit_tools(mock)
    register_wire_edit_tools(mock)
    return mock.tools


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
# _get_pin_schematic_position — unit tests
# ---------------------------------------------------------------------------

class TestGetPinSchematicPosition:
    """Tests for the _get_pin_schematic_position internal helper."""

    def setup_method(self):
        from kicad_mcp.tools.wire_edit_tools import _get_pin_schematic_position
        self._fn = _get_pin_schematic_position
        self._sch = skip.Schematic(SCHEMATIC_PATH)

    def test_r2_pin1(self):
        x, y = self._fn(self._sch, "R2", "1")
        assert abs(x - 100.0) < 0.01
        assert abs(y - 97.46) < 0.01

    def test_r2_pin2(self):
        x, y = self._fn(self._sch, "R2", "2")
        assert abs(x - 100.0) < 0.01
        assert abs(y - 102.54) < 0.01

    def test_c1_pin1(self):
        x, y = self._fn(self._sch, "C1", "1")
        assert abs(x - 150.0) < 0.01
        assert abs(y - 96.19) < 0.01

    def test_c1_pin2(self):
        x, y = self._fn(self._sch, "C1", "2")
        assert abs(x - 150.0) < 0.01
        assert abs(y - 103.81) < 0.01

    def test_unknown_reference_raises(self):
        with pytest.raises(ValueError, match="ZZZQ99"):
            self._fn(self._sch, "ZZZQ99", "1")

    def test_unknown_pin_raises(self):
        with pytest.raises(ValueError, match="pin"):
            self._fn(self._sch, "R2", "99")


# ---------------------------------------------------------------------------
# add_wire_to_schematic — tests
# ---------------------------------------------------------------------------

class TestAddWireToSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["add_wire_to_schematic"](**kwargs))

    # --- validation errors ---------------------------------------------------

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt",
                            start_x=0, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such_file.kicad_sch",
                            start_x=0, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_non_finite_coordinate(self, tools):
        import math
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            start_x=math.inf, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_zero_length_wire(self, tools):
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            start_x=100.0, start_y=100.0,
                            end_x=100.0, end_y=100.0)
        assert "error" in result
        assert "zero" in result["error"].lower() or "identical" in result["error"].lower()

    # --- happy path ----------------------------------------------------------

    def test_horizontal_wire_written(self, tools, tmp_sch):
        """Wire from R2-pin2 to R3-pin2: same y=102.54, x from 100 to 120."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=102.54,
                            end_x=120.0, end_y=102.54)
        assert result.get("success") is True
        assert result["wire"]["start"] == {"x": 100.0, "y": 102.54}
        assert result["wire"]["end"] == {"x": 120.0, "y": 102.54}
        assert result["junctions_added"] == []

        # Verify the wire actually exists in the saved file.
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            abs(w.start.value[0] - 100.0) < 0.01 and
            abs(w.start.value[1] - 102.54) < 0.01 and
            abs(w.end.value[0] - 120.0) < 0.01 and
            abs(w.end.value[1] - 102.54) < 0.01
            for w in sch2.wire
        )
        assert found, "Wire not found in saved schematic"

    def test_backup_created(self, tools, tmp_sch):
        self._call(tools, schematic_path=tmp_sch,
                   start_x=100.0, start_y=100.0,
                   end_x=110.0, end_y=100.0)
        assert os.path.exists(tmp_sch + ".bak")

    def test_wire_with_junctions(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=102.54,
                            end_x=120.0, end_y=102.54,
                            add_junction_start=True,
                            add_junction_end=True)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 2

        sch2 = skip.Schematic(tmp_sch)
        junction_coords = [j.at.value[:2] for j in sch2.junction]
        assert any(abs(jx - 100.0) < 0.01 and abs(jy - 102.54) < 0.01
                   for jx, jy in junction_coords)
        assert any(abs(jx - 120.0) < 0.01 and abs(jy - 102.54) < 0.01
                   for jx, jy in junction_coords)

    def test_diagonal_wire(self, tools, tmp_sch):
        """Non-axis-aligned wire is allowed (no routing in scope)."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=97.46,
                            end_x=120.0, end_y=102.54)
        assert result.get("success") is True


# ---------------------------------------------------------------------------
# connect_pins_with_wire — tests
# ---------------------------------------------------------------------------

class TestConnectPinsWithWire:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["connect_pins_with_wire"](**kwargs))

    # --- validation errors ---------------------------------------------------

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt",
                            from_ref="R2", from_pin="2",
                            to_ref="R3", to_pin="2")
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch",
                            from_ref="R2", from_pin="2",
                            to_ref="R3", to_pin="2")
        assert "error" in result

    def test_unknown_from_reference(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="ZZZQ99", from_pin="1",
                            to_ref="R3", to_pin="2")
        assert "error" in result
        assert "ZZZQ99" in result["error"]

    def test_unknown_to_reference(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R2", from_pin="2",
                            to_ref="ZZZQ99", to_pin="1")
        assert "error" in result
        assert "ZZZQ99" in result["error"]

    def test_unknown_pin_number(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R2", from_pin="99",
                            to_ref="R3", to_pin="2")
        assert "error" in result

    # --- happy path ----------------------------------------------------------

    def test_connect_r4_pin2_to_r5_pin2(self, tools, tmp_sch):
        """Both pins at y=102.54; wire should be horizontal."""
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R4", from_pin="2",
                            to_ref="R5", to_pin="2")
        assert result.get("success") is True
        wire_info = result["wire"]
        assert wire_info["from"]["ref"] == "R4"
        assert wire_info["from"]["pin"] == "2"
        assert wire_info["to"]["ref"] == "R5"
        assert wire_info["to"]["pin"] == "2"
        assert abs(wire_info["from"]["x"] - 140.0) < 0.01
        assert abs(wire_info["from"]["y"] - 102.54) < 0.01
        assert abs(wire_info["to"]["x"] - 160.0) < 0.01
        assert abs(wire_info["to"]["y"] - 102.54) < 0.01

        # Confirm wire in saved file.
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            abs(w.start.value[0] - 140.0) < 0.01 and
            abs(w.start.value[1] - 102.54) < 0.01
            for w in sch2.wire
        )
        assert found, "Wire not found in saved schematic"

    def test_connect_r6_pin1_to_r7_pin1(self, tools, tmp_sch):
        """Connect the top pins of R6 and R7."""
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R6", from_pin="1",
                            to_ref="R7", to_pin="1")
        assert result.get("success") is True

    def test_connect_with_junctions(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R2", from_pin="2",
                            to_ref="R3", to_pin="2",
                            add_junctions=True)
        assert result.get("success") is True
        assert result["junctions_added"] is True

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.junction)) >= 2

    def test_backup_created(self, tools, tmp_sch):
        self._call(tools, schematic_path=tmp_sch,
                   from_ref="R2", from_pin="1",
                   to_ref="R3", to_pin="1")
        assert os.path.exists(tmp_sch + ".bak")

    def test_multiple_wires_accumulate(self, tools, tmp_sch):
        """Adding two wires should result in two wires in the schematic."""
        self._call(tools, schematic_path=tmp_sch,
                   from_ref="R2", from_pin="2", to_ref="R3", to_pin="2")
        self._call(tools, schematic_path=tmp_sch,
                   from_ref="R4", from_pin="2", to_ref="R5", to_pin="2")
        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.wire)) >= 2


# ---------------------------------------------------------------------------
# list_wires_in_schematic — tests
# ---------------------------------------------------------------------------

class TestListWiresInSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["list_wires_in_schematic"](**kwargs))

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt")
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch")
        assert "error" in result

    def test_empty_schematic_returns_zero_wires(self, tools, tmp_sch):
        """Fresh schematic copy has no wires."""
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["wires"] == []

    def test_returns_added_wire(self, tools, tmp_sch):
        """After adding a wire it should appear in the list."""
        add = asyncio.run(tools["add_wire_to_schematic"](
            schematic_path=tmp_sch,
            start_x=100.0, start_y=102.54,
            end_x=120.0, end_y=102.54,
        ))
        assert add.get("success") is True

        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 1
        w = result["wires"][0]
        assert abs(w["start"]["x"] - 100.0) < 0.01
        assert abs(w["start"]["y"] - 102.54) < 0.01
        assert abs(w["end"]["x"] - 120.0) < 0.01
        assert abs(w["end"]["y"] - 102.54) < 0.01

    def test_multiple_wires_counted(self, tools, tmp_sch):
        asyncio.run(tools["add_wire_to_schematic"](
            schematic_path=tmp_sch,
            start_x=100.0, start_y=97.46, end_x=120.0, end_y=97.46,
        ))
        asyncio.run(tools["add_wire_to_schematic"](
            schematic_path=tmp_sch,
            start_x=100.0, start_y=102.54, end_x=120.0, end_y=102.54,
        ))
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 2

    def test_wire_coords_are_numbers(self, tools, tmp_sch):
        asyncio.run(tools["add_wire_to_schematic"](
            schematic_path=tmp_sch,
            start_x=110.0, start_y=97.46, end_x=130.0, end_y=97.46,
        ))
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        for w in result["wires"]:
            assert isinstance(w["start"]["x"], float)
            assert isinstance(w["start"]["y"], float)
            assert isinstance(w["end"]["x"], float)
            assert isinstance(w["end"]["y"], float)


# ---------------------------------------------------------------------------
# delete_wire_from_schematic — tests
# ---------------------------------------------------------------------------

class TestDeleteWireFromSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["delete_wire_from_schematic"](**kwargs))

    def _add_wire(self, tools, tmp_sch, sx, sy, ex, ey):
        return asyncio.run(tools["add_wire_to_schematic"](
            schematic_path=tmp_sch,
            start_x=sx, start_y=sy, end_x=ex, end_y=ey,
        ))

    # --- validation errors ---------------------------------------------------

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt",
                            start_x=0, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch",
                            start_x=0, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_non_finite_coordinate(self, tools):
        import math as _math
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            start_x=_math.inf, start_y=0, end_x=10, end_y=0)
        assert "error" in result

    def test_no_matching_wire(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=999.0, start_y=999.0,
                            end_x=1000.0, end_y=999.0)
        assert "error" in result

    # --- happy path ----------------------------------------------------------

    def test_delete_exact_wire(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=97.46,
                            end_x=120.0, end_y=97.46)
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.wire)) == 0

    def test_delete_reverse_direction(self, tools, tmp_sch):
        """Wire stored as A→B should also be found when queried as B→A."""
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=120.0, start_y=97.46,
                            end_x=100.0, end_y=97.46)
        assert result.get("success") is True
        assert result["deleted_count"] == 1

    def test_delete_one_of_two_wires(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        self._add_wire(tools, tmp_sch, 100.0, 102.54, 120.0, 102.54)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=97.46,
                            end_x=120.0, end_y=97.46)
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        remaining = list(sch2.wire)
        assert len(remaining) == 1
        assert abs(remaining[0].start.value[1] - 102.54) < 0.01

    def test_backup_created(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        self._call(tools, schematic_path=tmp_sch,
                   start_x=100.0, start_y=97.46,
                   end_x=120.0, end_y=97.46)
        assert os.path.exists(tmp_sch + ".bak")


# ---------------------------------------------------------------------------
# add_junction_to_schematic — tests
# ---------------------------------------------------------------------------

class TestAddJunctionToSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["add_junction_to_schematic"](**kwargs))

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt", x=0, y=0)
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch",
                            x=0, y=0)
        assert "error" in result

    def test_non_finite_coordinate(self, tools):
        import math as _math
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            x=_math.inf, y=0)
        assert "error" in result

    def test_junction_added_to_file(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch, x=100.0, y=102.54)
        assert result.get("success") is True
        assert result["junction"] == {"x": 100.0, "y": 102.54}

        sch2 = skip.Schematic(tmp_sch)
        found = any(
            abs(float(j.at.value[0]) - 100.0) < 0.01 and
            abs(float(j.at.value[1]) - 102.54) < 0.01
            for j in sch2.junction
        )
        assert found, "Junction not found in saved schematic"

    def test_backup_created(self, tools, tmp_sch):
        self._call(tools, schematic_path=tmp_sch, x=120.0, y=97.46)
        assert os.path.exists(tmp_sch + ".bak")


# ---------------------------------------------------------------------------
# list_junctions_in_schematic — tests
# ---------------------------------------------------------------------------

class TestListJunctionsInSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["list_junctions_in_schematic"](**kwargs))

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt")
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch")
        assert "error" in result

    def test_no_junctions_returns_empty(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["junctions"] == []

    def test_lists_added_junction(self, tools, tmp_sch):
        asyncio.run(tools["add_junction_to_schematic"](
            schematic_path=tmp_sch, x=100.0, y=102.54
        ))
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 1
        j = result["junctions"][0]
        assert abs(j["x"] - 100.0) < 0.01
        assert abs(j["y"] - 102.54) < 0.01

    def test_multiple_junctions_counted(self, tools, tmp_sch):
        asyncio.run(tools["add_junction_to_schematic"](
            schematic_path=tmp_sch, x=100.0, y=97.46
        ))
        asyncio.run(tools["add_junction_to_schematic"](
            schematic_path=tmp_sch, x=120.0, y=97.46
        ))
        result = self._call(tools, schematic_path=tmp_sch)
        assert result.get("success") is True
        assert result["count"] == 2


# ---------------------------------------------------------------------------
# delete_junction_from_schematic — tests
# ---------------------------------------------------------------------------

class TestDeleteJunctionFromSchematic:

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["delete_junction_from_schematic"](**kwargs))

    def _add_junction(self, tools, tmp_sch, x, y):
        return asyncio.run(tools["add_junction_to_schematic"](
            schematic_path=tmp_sch, x=x, y=y
        ))

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt", x=0, y=0)
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch",
                            x=0, y=0)
        assert "error" in result

    def test_non_finite_coordinate(self, tools):
        import math as _math
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            x=_math.nan, y=0)
        assert "error" in result

    def test_no_matching_junction(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch, x=999.0, y=999.0)
        assert "error" in result

    def test_delete_junction(self, tools, tmp_sch):
        self._add_junction(tools, tmp_sch, 100.0, 102.54)
        result = self._call(tools, schematic_path=tmp_sch, x=100.0, y=102.54)
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.junction)) == 0

    def test_delete_one_of_two_junctions(self, tools, tmp_sch):
        self._add_junction(tools, tmp_sch, 100.0, 97.46)
        self._add_junction(tools, tmp_sch, 120.0, 97.46)

        result = self._call(tools, schematic_path=tmp_sch, x=100.0, y=97.46)
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        remaining = list(sch2.junction)
        assert len(remaining) == 1
        assert abs(float(remaining[0].at.value[0]) - 120.0) < 0.01

    def test_backup_created(self, tools, tmp_sch):
        self._add_junction(tools, tmp_sch, 100.0, 97.46)
        self._call(tools, schematic_path=tmp_sch, x=100.0, y=97.46)
        assert os.path.exists(tmp_sch + ".bak")
