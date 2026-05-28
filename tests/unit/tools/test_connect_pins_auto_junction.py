"""
Tests for the auto-junction feature of connect_pins_with_wire in wire_edit_tools.py.

New behaviour under test: before drawing a new wire, for each of the two pin
endpoints — if that pin position already has a wire endpoint connected to it
AND no junction exists there yet — a junction is placed automatically.  The
result dict contains ``auto_junctions_added`` (a list of ``{x, y}`` dicts)
when this happened; the key is absent when no auto-junctions were placed.

The ``add_junctions`` parameter has been removed entirely from
``connect_pins_with_wire``.

Fixture assumptions (tools_test.kicad_sch):
    R2 (at 100,100):  pin1 → (100.0,  97.46)  pin2 → (100.0, 102.54)
    R3 (at 120,100):  pin1 → (120.0,  97.46)  pin2 → (120.0, 102.54)
    R4 (at 140,100):  pin1 → (140.0,  97.46)  pin2 → (140.0, 102.54)
    R5 (at 160,100):  pin1 → (160.0,  97.46)  pin2 → (160.0, 102.54)
The schematic has no pre-existing wires or junctions.
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
    """Register component and wire edit tools against a mock MCP and return the dict."""
    from kcaa.tools.component_edit_tools import register_component_edit_tools
    from kcaa.tools.wire_edit_tools import register_wire_edit_tools
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
# Tests
# ---------------------------------------------------------------------------

class TestConnectPinsAutoJunction:

    def _connect(self, tools, sch_path, from_ref, from_pin, to_ref, to_pin):
        return asyncio.run(tools["connect_pins_with_wire"](
            schematic_path=sch_path,
            from_ref=from_ref,
            from_pin=from_pin,
            to_ref=to_ref,
            to_pin=to_pin,
        ))

    def _add_wire(self, tools, sch_path, sx, sy, ex, ey):
        return asyncio.run(tools["connect_points_with_wire"](
            schematic_path=sch_path,
            start_x=sx,
            start_y=sy,
            end_x=ex,
            end_y=ey,
        ))

    # -------------------------------------------------------------------------
    # Test 1: No existing wire at pins → no auto junction
    # -------------------------------------------------------------------------

    def test_no_wire_at_pins_produces_no_auto_junction(self, tools, tmp_sch):
        """Connecting two clean pins (no pre-existing wires) adds no auto junctions."""
        result = self._connect(tools, tmp_sch, "R4", "2", "R5", "2")
        assert result.get("success") is True, result

        auto = result.get("auto_junctions_added", [])
        assert auto == [], f"Expected no auto junctions, got {auto!r}"

        # Verify no junction in the saved schematic
        sch2 = skip.Schematic(tmp_sch)
        try:
            junctions = list(sch2.junction)
        except AttributeError:
            junctions = []
        assert len(junctions) == 0, (
            f"Unexpected junctions in schematic after clean connect: {junctions}"
        )

    # -------------------------------------------------------------------------
    # Test 2: One pin already has a wire endpoint → one auto junction
    # -------------------------------------------------------------------------

    def test_one_pin_has_wire_endpoint_produces_one_auto_junction(self, tools, tmp_sch):
        """When exactly one pin already has a wire endpoint, one junction is auto-placed."""
        # Add a wire whose right endpoint is exactly at R2 pin2 (100.0, 102.54).
        wire_result = self._add_wire(tools, tmp_sch, 90.0, 102.54, 100.0, 102.54)
        assert wire_result.get("success") is True, wire_result

        result = self._connect(tools, tmp_sch, "R2", "2", "R3", "2")
        assert result.get("success") is True, result

        auto = result.get("auto_junctions_added", [])
        assert len(auto) == 1, f"Expected exactly 1 auto junction, got {auto!r}"
        assert abs(auto[0]["x"] - 100.0) < 0.01, auto
        assert abs(auto[0]["y"] - 102.54) < 0.01, auto

        # Junction must exist in the saved schematic
        sch2 = skip.Schematic(tmp_sch)
        junction_coords = [j.at.value[:2] for j in sch2.junction]
        assert any(
            abs(jx - 100.0) < 0.01 and abs(jy - 102.54) < 0.01
            for jx, jy in junction_coords
        ), f"Junction at (100.0, 102.54) not found in schematic; junctions={junction_coords}"

    # -------------------------------------------------------------------------
    # Test 3: Both pins already have wire endpoints → two auto junctions
    # -------------------------------------------------------------------------

    def test_both_pins_have_wire_endpoints_produces_two_auto_junctions(self, tools, tmp_sch):
        """When both pin positions already have wire endpoints, two junctions are placed."""
        # Wire endpoint at R2 pin2 = (100.0, 102.54)
        r2 = self._add_wire(tools, tmp_sch, 90.0, 102.54, 100.0, 102.54)
        assert r2.get("success") is True, r2
        # Wire endpoint at R3 pin2 = (120.0, 102.54)
        r3 = self._add_wire(tools, tmp_sch, 130.0, 102.54, 120.0, 102.54)
        assert r3.get("success") is True, r3

        result = self._connect(tools, tmp_sch, "R2", "2", "R3", "2")
        assert result.get("success") is True, result

        auto = result.get("auto_junctions_added", [])
        assert len(auto) == 2, f"Expected exactly 2 auto junctions, got {auto!r}"

        xs = {round(j["x"], 1) for j in auto}
        assert 100.0 in xs, f"Expected x=100.0 among auto junction xs: {xs}"
        assert 120.0 in xs, f"Expected x=120.0 among auto junction xs: {xs}"

        sch2 = skip.Schematic(tmp_sch)
        junction_coords = [j.at.value[:2] for j in sch2.junction]
        assert any(
            abs(jx - 100.0) < 0.01 and abs(jy - 102.54) < 0.01
            for jx, jy in junction_coords
        ), "Junction at (100.0, 102.54) not found in schematic"
        assert any(
            abs(jx - 120.0) < 0.01 and abs(jy - 102.54) < 0.01
            for jx, jy in junction_coords
        ), "Junction at (120.0, 102.54) not found in schematic"

    # -------------------------------------------------------------------------
    # Test 4: Junction already exists at a pin → no duplicate junction
    # -------------------------------------------------------------------------

    def test_existing_junction_is_not_duplicated(self, tools, tmp_sch):
        """When a junction already exists at a pin position, no duplicate is added."""
        # Add a wire whose right endpoint is exactly at R2 pin2 (100.0, 102.54).
        wire_result = self._add_wire(tools, tmp_sch, 90.0, 102.54, 100.0, 102.54)
        assert wire_result.get("success") is True, wire_result

        # Directly write a junction at that same position using the skip library,
        # bypassing the deleted add_junction_to_schematic tool.
        sch_pre = skip.Schematic(tmp_sch)
        j = sch_pre.junction.new()
        j.at.value = [100.0, 102.54]
        sch_pre.write(tmp_sch)

        # Verify the junction was written before the connect call.
        sch_verify = skip.Schematic(tmp_sch)
        pre_count = sum(
            1 for jj in sch_verify.junction
            if abs(float(jj.at.value[0]) - 100.0) < 0.01
            and abs(float(jj.at.value[1]) - 102.54) < 0.01
        )
        assert pre_count == 1, f"Pre-condition failed: expected 1 junction, got {pre_count}"

        # Connect R2 pin2 → R3 pin2; the existing junction must not be duplicated.
        result = self._connect(tools, tmp_sch, "R2", "2", "R3", "2")
        assert result.get("success") is True, result

        auto = result.get("auto_junctions_added", [])
        duplicated = [
            j for j in auto
            if abs(j["x"] - 100.0) < 0.01 and abs(j["y"] - 102.54) < 0.01
        ]
        assert len(duplicated) == 0, (
            f"A duplicate junction was placed at (100.0, 102.54): {auto!r}"
        )

        # Exactly one junction at (100.0, 102.54) in the schematic.
        sch2 = skip.Schematic(tmp_sch)
        junctions_at_pos = [
            jj for jj in sch2.junction
            if abs(float(jj.at.value[0]) - 100.0) < 0.01
            and abs(float(jj.at.value[1]) - 102.54) < 0.01
        ]
        assert len(junctions_at_pos) == 1, (
            f"Expected exactly 1 junction at (100.0, 102.54), "
            f"found {len(junctions_at_pos)}"
        )

    # -------------------------------------------------------------------------
    # Test 5: Basic end-to-end success — wire drawn, result has correct metadata
    # -------------------------------------------------------------------------

    def test_basic_success_wire_drawn_with_correct_metadata(self, tools, tmp_sch):
        """connect_pins_with_wire draws a wire and returns correct ref/pin/coord data."""
        result = self._connect(tools, tmp_sch, "R4", "2", "R5", "2")
        assert result.get("success") is True, result

        wire = result["wire"]
        assert wire["from"]["ref"] == "R4"
        assert wire["from"]["pin"] == "2"
        assert wire["to"]["ref"] == "R5"
        assert wire["to"]["pin"] == "2"
        assert abs(wire["from"]["x"] - 140.0) < 0.01, wire
        assert abs(wire["from"]["y"] - 102.54) < 0.01, wire
        assert abs(wire["to"]["x"] - 160.0) < 0.01, wire
        assert abs(wire["to"]["y"] - 102.54) < 0.01, wire

        # Verify at least one wire segment starting at R4 pin2 exists in the saved file
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            abs(w.start.value[0] - 140.0) < 0.01
            and abs(w.start.value[1] - 102.54) < 0.01
            for w in sch2.wire
        )
        assert found, "Expected wire segment from R4 pin2 not found in saved schematic"
