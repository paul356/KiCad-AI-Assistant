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
# _get_pin_schematic_position — unit tests
# ---------------------------------------------------------------------------

class TestGetPinSchematicPosition:
    """Tests for the _get_pin_schematic_position internal helper."""

    def setup_method(self):
        from kcaa.tools.wire_edit_tools import _get_pin_schematic_position
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
# add_wire_to_schematic — tests (naive H/V fallback tool)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="add_wire_to_schematic intentionally removed")
class TestAddWireToSchematic:
    """Tests for the naive horizontal/vertical-only fallback wire tool.

    This tool only draws axis-aligned segments.  Diagonal endpoints return
    an error.  Use connect_points_with_wire for smart orthogonal routing.
    """

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

    def test_diagonal_wire_rejected(self, tools, tmp_sch):
        """Diagonal endpoints must be rejected with an informative error."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=97.46,
                            end_x=120.0, end_y=102.54)
        assert "error" in result
        assert "horizontal or vertical" in result["error"].lower() or \
               "connect_points_with_wire" in result["error"]

    # --- auto-junction and auto-split ----------------------------------------

    def test_vertical_wire_success(self, tools, tmp_sch):
        """A vertical wire (same X) is drawn successfully."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=50.0, start_y=80.0,
                            end_x=50.0, end_y=90.0)
        assert result.get("success") is True
        assert result["junctions_added"] == []
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            abs(w.start.value[0] - 50.0) < 0.01 and
            abs(w.start.value[1] - 80.0) < 0.01 and
            abs(w.end.value[0] - 50.0) < 0.01 and
            abs(w.end.value[1] - 90.0) < 0.01
            for w in sch2.wire
        )
        assert found, "Vertical wire not found in saved schematic"

    def test_auto_split_start_on_wire_interior(self, tools, tmp_sch):
        """Start point on interior of existing wire → junction + split, new wire
        drawn perpendicularly without creating a duplicate segment."""
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 100.0])
        h.end_at([130.0, 100.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=100.0,
                            end_x=100.0, end_y=80.0)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 1
        assert abs(result["junctions_added"][0]["x"] - 100.0) < 0.01
        assert abs(result["junctions_added"][0]["y"] - 100.0) < 0.01

        sch2 = skip.Schematic(tmp_sch)
        endpoints = set()
        for ww in sch2.wire:
            endpoints.add((round(float(ww.start.value[0]), 3),
                           round(float(ww.start.value[1]), 3)))
            endpoints.add((round(float(ww.end.value[0]), 3),
                           round(float(ww.end.value[1]), 3)))
        assert (100.0, 100.0) in endpoints, "Wire not split at start interior point"
        # Original unsplit wire (80→130) must no longer exist as a single segment.
        unsplit = any(
            abs(float(ww.start.value[0]) - 80.0) < 0.01 and
            abs(float(ww.end.value[0]) - 130.0) < 0.01
            for ww in sch2.wire
        )
        assert not unsplit, "Original wire still present unsplit"

    def test_auto_split_end_on_wire_interior(self, tools, tmp_sch):
        """End point on interior of existing wire → junction + split."""
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 100.0])
        h.end_at([130.0, 100.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=80.0,
                            end_x=100.0, end_y=100.0)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 1
        assert abs(result["junctions_added"][0]["x"] - 100.0) < 0.01
        assert abs(result["junctions_added"][0]["y"] - 100.0) < 0.01

        sch2 = skip.Schematic(tmp_sch)
        endpoints = set()
        for ww in sch2.wire:
            endpoints.add((round(float(ww.start.value[0]), 3),
                           round(float(ww.start.value[1]), 3)))
            endpoints.add((round(float(ww.end.value[0]), 3),
                           round(float(ww.end.value[1]), 3)))
        assert (100.0, 100.0) in endpoints, "Wire not split at end interior point"

    def test_auto_junction_at_wire_endpoint_no_split(self, tools, tmp_sch):
        """Start point exactly at an existing wire endpoint → T-junction added,
        but wire is NOT split (endpoint, not interior)."""
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 100.0])
        h.end_at([100.0, 100.0])
        sch.write(tmp_sch)
        wire_count_before = len(list(skip.Schematic(tmp_sch).wire))

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=100.0,
                            end_x=100.0, end_y=80.0)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 1
        assert abs(result["junctions_added"][0]["x"] - 100.0) < 0.01

        sch2 = skip.Schematic(tmp_sch)
        # Exactly one new wire added (the vertical); no split wire.
        assert len(list(sch2.wire)) == wire_count_before + 1, \
            "Wire count wrong: split occurred at an endpoint (should not happen)"

    def test_no_junction_when_no_existing_wire(self, tools, tmp_sch):
        """Endpoints at isolated positions with no pre-existing wires → no junctions."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=50.0, start_y=50.0,
                            end_x=50.0, end_y=60.0)
        assert result.get("success") is True
        assert result["junctions_added"] == []
        sch2 = skip.Schematic(tmp_sch)
        try:
            assert len(list(sch2.junction)) == 0
        except AttributeError:
            pass

    def test_no_duplicate_junction(self, tools, tmp_sch):
        """Junction already exists at endpoint → no new junction inserted."""
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 100.0])
        h.end_at([100.0, 100.0])
        j = sch.junction.new()
        j.at.value = [100.0, 100.0]
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=100.0,
                            end_x=100.0, end_y=80.0)
        assert result.get("success") is True
        assert result["junctions_added"] == [], \
            "_add_junction_and_split must return False when junction already exists"

        sch2 = skip.Schematic(tmp_sch)
        at_point = [
            j2 for j2 in sch2.junction
            if abs(float(j2.at.value[0]) - 100.0) < 0.01
            and abs(float(j2.at.value[1]) - 100.0) < 0.01
        ]
        assert len(at_point) == 1, f"Expected 1 junction, found {len(at_point)} (duplicate)"

    def test_auto_split_both_endpoints_bridging_two_wires(self, tools, tmp_sch):
        """New horizontal wire with both endpoints on interiors of two separate
        vertical wires → both vertical wires split, two junctions inserted."""
        sch = skip.Schematic(tmp_sch)
        v1 = sch.wire.new()
        v1.start_at([100.0, 80.0])
        v1.end_at([100.0, 130.0])
        v2 = sch.wire.new()
        v2.start_at([120.0, 80.0])
        v2.end_at([120.0, 130.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=100.0,
                            end_x=120.0, end_y=100.0)
        assert result.get("success") is True
        xs = {round(j["x"]) for j in result["junctions_added"]}
        assert 100 in xs, f"Junction at x=100 missing: {result['junctions_added']}"
        assert 120 in xs, f"Junction at x=120 missing: {result['junctions_added']}"

        sch2 = skip.Schematic(tmp_sch)
        endpoints = set()
        for ww in sch2.wire:
            endpoints.add((round(float(ww.start.value[0]), 3),
                           round(float(ww.start.value[1]), 3)))
            endpoints.add((round(float(ww.end.value[0]), 3),
                           round(float(ww.end.value[1]), 3)))
        assert (100.0, 100.0) in endpoints, "Vertical wire 1 not split"
        assert (120.0, 100.0) in endpoints, "Vertical wire 2 not split"

    def test_collinear_tap_no_duplicate_segment(self, tools, tmp_sch):
        """Both endpoints on interior of the same collinear wire.

        Splitting creates the exact (90,100)-(110,100) segment.  The tool
        must NOT draw a second identical segment on top of it.

        Regression: add_wire_to_schematic always called sch.wire.new() after
        the junction/split phase, even when the split had already created the
        needed segment.
        """
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 100.0])
        h.end_at([130.0, 100.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=90.0, start_y=100.0,
                            end_x=110.0, end_y=100.0)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 2

        sch2 = skip.Schematic(tmp_sch)
        duplicates = [
            ww for ww in sch2.wire
            if (
                abs(float(ww.start.value[0]) - 90.0) < 0.01 and
                abs(float(ww.start.value[1]) - 100.0) < 0.01 and
                abs(float(ww.end.value[0]) - 110.0) < 0.01 and
                abs(float(ww.end.value[1]) - 100.0) < 0.01
            ) or (
                abs(float(ww.start.value[0]) - 110.0) < 0.01 and
                abs(float(ww.start.value[1]) - 100.0) < 0.01 and
                abs(float(ww.end.value[0]) - 90.0) < 0.01 and
                abs(float(ww.end.value[1]) - 100.0) < 0.01
            )
        ]
        assert len(duplicates) == 1, (
            f"Expected exactly 1 wire (90,100)-(110,100) from split; "
            f"found {len(duplicates)} (duplicate segment bug)"
        )


# ---------------------------------------------------------------------------
# connect_points_with_wire — tests (smart orthogonal routing)
# ---------------------------------------------------------------------------

class TestConnectPointsWithWire:
    """Tests for the smart orthogonal routing tool (bare coordinates)."""

    def _call(self, tools, **kwargs):
        return asyncio.run(tools["connect_points_with_wire"](**kwargs))

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

    # --- happy path ----------------------------------------------------------

    def test_horizontal_wire_written(self, tools, tmp_sch):
        """Horizontal wire between two pin positions."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=102.54,
                            end_x=120.0, end_y=102.54)
        assert result.get("success") is True
        assert result["wire"]["start"] == {"x": 100.0, "y": 102.54}
        assert result["wire"]["end"] == {"x": 120.0, "y": 102.54}

        sch2 = skip.Schematic(tmp_sch)
        # Smart router may emit one or more segments; just confirm wires exist.
        assert len(list(sch2.wire)) >= 1

    def test_diagonal_wire_routed(self, tools, tmp_sch):
        """Diagonal endpoints are accepted and routed orthogonally."""
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=97.46,
                            end_x=120.0, end_y=102.54)
        assert result.get("success") is True

    def test_backup_created(self, tools, tmp_sch):
        self._call(tools, schematic_path=tmp_sch,
                   start_x=100.0, start_y=100.0,
                   end_x=110.0, end_y=100.0)
        assert os.path.exists(tmp_sch + ".bak")

    def test_auto_junction_on_wire_interior(self, tools, tmp_sch):
        """Endpoint on the interior of an existing wire → junction + split."""
        # Draw a long horizontal wire first.
        sch = skip.Schematic(tmp_sch)
        w = sch.wire.new()
        w.start_at([80.0, 102.54])
        w.end_at([130.0, 102.54])
        sch.write(tmp_sch)

        # Route to a point on the interior of that wire from above.
        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=100.0, start_y=90.0,
                            end_x=100.0, end_y=102.54)
        assert result.get("success") is True

        sch2 = skip.Schematic(tmp_sch)
        # The long wire must have been split at (100, 102.54).
        endpoints = set()
        for ww in sch2.wire:
            endpoints.add((round(float(ww.start.value[0]), 3),
                           round(float(ww.start.value[1]), 3)))
            endpoints.add((round(float(ww.end.value[0]), 3),
                           round(float(ww.end.value[1]), 3)))
        assert (100.0, 102.54) in endpoints, "Wire was not split at endpoint"

    def test_auto_split_junction_appears_in_result(self, tools, tmp_sch):
        """When an endpoint is on a wire interior, the junction coordinate
        must appear in the ``junctions_added`` key of the result.

        Uses coordinates far from component pins (y=50) to avoid pin-collision
        false failures from fixture components at y≈97-104.
        """
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([180.0, 50.0])
        h.end_at([230.0, 50.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=200.0, start_y=30.0,
                            end_x=200.0, end_y=50.0)
        assert result.get("success") is True, (
            f"Expected success; got error: {result.get('error')}"
        )
        jxs = [round(j["x"]) for j in result["junctions_added"]]
        assert 200 in jxs, \
            f"Junction at x=200 expected in junctions_added; got {result['junctions_added']}"

        sch2 = skip.Schematic(tmp_sch)
        assert any(
            abs(float(j.at.value[0]) - 200.0) < 0.01 and
            abs(float(j.at.value[1]) - 50.0) < 0.01
            for j in sch2.junction
        ), "Junction not present in saved file"

    def test_auto_junction_at_wire_endpoint_no_split(self, tools, tmp_sch):
        """Start endpoint exactly at an existing wire endpoint → T-junction
        added but the wire is NOT split (endpoint ≠ interior).

        Uses y=50 to stay away from fixture component pins at y≈97-104.
        """
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([180.0, 50.0])
        h.end_at([200.0, 50.0])
        sch.write(tmp_sch)
        wire_count_before = len(list(skip.Schematic(tmp_sch).wire))

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=200.0, start_y=50.0,
                            end_x=200.0, end_y=30.0)
        assert result.get("success") is True
        assert len(result["junctions_added"]) == 1

        sch2 = skip.Schematic(tmp_sch)
        # Smart router adds at least one routing segment; original wire NOT split.
        assert len(list(sch2.wire)) > wire_count_before

    def test_collinear_tap_both_endpoints_same_wire(self, tools, tmp_sch):
        """Both endpoints on the interior of the same horizontal wire; the
        requested route is collinear with it.

        After splitting the original wire at both endpoints the exact segment
        already exists.  The tool must:
          1. Succeed (not return 'no valid route').
          2. Report two junctions.
          3. Produce exactly 3 wire segments in the file — the three pieces
             created by splitting — with NO extra U-detour routing.

        Uses y=50 to avoid fixture component pins at y≈97-104 that would
        block U-detour routes and obscure which failure mode is hit.

        Regressions tested:
          a) Stale ``existing_wires`` passed to _draw_smart_wire caused it to
             treat the original long wire as an obstacle and reject routes,
             falling back to unnecessary U-detour segments.
          b) After the fix (refresh + pre-check), the pre-existing split
             segment is detected and no extra routing is performed.
        """
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([80.0, 50.0])
        h.end_at([130.0, 50.0])
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=90.0, start_y=50.0,
                            end_x=110.0, end_y=50.0)
        assert result.get("success") is True, (
            f"Expected success but got error: {result.get('error')}"
        )
        assert len(result["junctions_added"]) == 2

        sch2 = skip.Schematic(tmp_sch)
        all_wires = list(sch2.wire)
        # Exactly 3 segments: (80,50)-(90,50), (90,50)-(110,50), (110,50)-(130,50).
        # No extra U-detour routing should be present.
        assert len(all_wires) == 3, (
            f"Expected 3 wire segments (split only, no extra routing); "
            f"found {len(all_wires)}. Extra wires indicate the stale "
            f"existing_wires bug caused unnecessary U-detour routing."
        )
        # Confirm the middle segment exists exactly once.
        mid_segs = [
            ww for ww in all_wires
            if (
                abs(float(ww.start.value[0]) - 90.0) < 0.01 and
                abs(float(ww.start.value[1]) - 50.0) < 0.01 and
                abs(float(ww.end.value[0]) - 110.0) < 0.01 and
                abs(float(ww.end.value[1]) - 50.0) < 0.01
            ) or (
                abs(float(ww.start.value[0]) - 110.0) < 0.01 and
                abs(float(ww.start.value[1]) - 50.0) < 0.01 and
                abs(float(ww.end.value[0]) - 90.0) < 0.01 and
                abs(float(ww.end.value[1]) - 50.0) < 0.01
            )
        ]
        assert len(mid_segs) == 1, (
            f"Expected exactly 1 middle segment (90,50)-(110,50); "
            f"found {len(mid_segs)}"
        )

    def test_no_duplicate_junction(self, tools, tmp_sch):
        """Junction already exists at endpoint → no additional junction placed.

        Uses y=50 to avoid fixture component pins.
        """
        sch = skip.Schematic(tmp_sch)
        h = sch.wire.new()
        h.start_at([180.0, 50.0])
        h.end_at([200.0, 50.0])
        j = sch.junction.new()
        j.at.value = [200.0, 50.0]
        sch.write(tmp_sch)

        result = self._call(tools, schematic_path=tmp_sch,
                            start_x=200.0, start_y=50.0,
                            end_x=200.0, end_y=30.0)
        assert result.get("success") is True
        assert result["junctions_added"] == [], \
            "No new junction should be reported when one already exists"

        sch2 = skip.Schematic(tmp_sch)
        at_point = [
            j2 for j2 in sch2.junction
            if abs(float(j2.at.value[0]) - 200.0) < 0.01
            and abs(float(j2.at.value[1]) - 50.0) < 0.01
        ]
        assert len(at_point) == 1, f"Expected 1 junction, found {len(at_point)}"



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

    def test_connect_no_preexisting_wire_no_auto_junction(self, tools, tmp_sch):
        """Connecting fresh pins with no pre-existing wires adds no auto junctions."""
        result = self._call(tools, schematic_path=tmp_sch,
                            from_ref="R2", from_pin="2",
                            to_ref="R3", to_pin="2")
        assert result.get("success") is True
        assert not result.get("auto_junctions_added")

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.junction)) == 0

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
# delete_wire_from_schematic — tests
# ---------------------------------------------------------------------------

class TestDeleteWireFromSchematic:

    def _call(self, tools, schematic_path, wires, **kwargs):
        return asyncio.run(tools["delete_wire_from_schematic"](
            schematic_path=schematic_path,
            wires=wires,
            **kwargs,
        ))

    def _add_wire(self, tools, tmp_sch, sx, sy, ex, ey):
        sch = skip.Schematic(tmp_sch)
        w = sch.wire.new()
        w.start_at([sx, sy])
        w.end_at([ex, ey])
        sch.write(tmp_sch)

    def _spec(self, sx, sy, ex, ey):
        return {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}

    # --- validation errors ---------------------------------------------------

    def test_wrong_extension(self, tools):
        result = self._call(tools, schematic_path="/tmp/bad.txt",
                            wires=[self._spec(0, 0, 10, 0)])
        assert "error" in result

    def test_file_not_found(self, tools):
        result = self._call(tools, schematic_path="/tmp/no_such.kicad_sch",
                            wires=[self._spec(0, 0, 10, 0)])
        assert "error" in result

    def test_empty_wires_list(self, tools):
        result = self._call(tools, schematic_path=SCHEMATIC_PATH, wires=[])
        assert "error" in result

    def test_non_finite_coordinate(self, tools):
        import math as _math
        result = self._call(tools, schematic_path=SCHEMATIC_PATH,
                            wires=[self._spec(_math.inf, 0, 10, 0)])
        assert "error" in result

    def test_no_matching_wire(self, tools, tmp_sch):
        result = self._call(tools, schematic_path=tmp_sch,
                            wires=[self._spec(999.0, 999.0, 1000.0, 999.0)])
        assert "error" in result

    # --- happy path ----------------------------------------------------------

    def test_delete_exact_wire(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        result = self._call(tools, schematic_path=tmp_sch,
                            wires=[self._spec(100.0, 97.46, 120.0, 97.46)])
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.wire)) == 0

    def test_delete_reverse_direction(self, tools, tmp_sch):
        """Wire stored as A→B should also be found when queried as B→A."""
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        result = self._call(tools, schematic_path=tmp_sch,
                            wires=[self._spec(120.0, 97.46, 100.0, 97.46)])
        assert result.get("success") is True
        assert result["deleted_count"] == 1

    def test_delete_one_of_two_wires(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        self._add_wire(tools, tmp_sch, 100.0, 102.54, 120.0, 102.54)

        result = self._call(tools, schematic_path=tmp_sch,
                            wires=[self._spec(100.0, 97.46, 120.0, 97.46)])
        assert result.get("success") is True
        assert result["deleted_count"] == 1

        sch2 = skip.Schematic(tmp_sch)
        remaining = list(sch2.wire)
        assert len(remaining) == 1
        assert abs(remaining[0].start.value[1] - 102.54) < 0.01

    def test_delete_batch_two_wires(self, tools, tmp_sch):
        """Batch deletion removes two wires in a single call."""
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        self._add_wire(tools, tmp_sch, 100.0, 102.54, 120.0, 102.54)

        result = self._call(tools, schematic_path=tmp_sch, wires=[
            self._spec(100.0, 97.46, 120.0, 97.46),
            self._spec(100.0, 102.54, 120.0, 102.54),
        ])
        assert result.get("success") is True
        assert result["deleted_count"] == 2
        assert "not_found" not in result

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.wire)) == 0

    def test_batch_partial_match_reports_not_found(self, tools, tmp_sch):
        """Specs with no match are reported in not_found, matched ones are deleted."""
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)

        result = self._call(tools, schematic_path=tmp_sch, wires=[
            self._spec(100.0, 97.46, 120.0, 97.46),   # index 0 — exists
            self._spec(999.0, 999.0, 1000.0, 999.0),  # index 1 — does not exist
        ])
        assert result.get("success") is True
        assert result["deleted_count"] == 1
        assert result.get("not_found") == [1]

        sch2 = skip.Schematic(tmp_sch)
        assert len(list(sch2.wire)) == 0

    def test_backup_created(self, tools, tmp_sch):
        self._add_wire(tools, tmp_sch, 100.0, 97.46, 120.0, 97.46)
        self._call(tools, schematic_path=tmp_sch,
                   wires=[self._spec(100.0, 97.46, 120.0, 97.46)])
        assert os.path.exists(tmp_sch + ".bak")
