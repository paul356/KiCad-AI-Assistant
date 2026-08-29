"""
Unit tests for kcaa/utils/pcb_board_utils.py
"""

import math
import os

import pytest

from kcaa.utils.pcb_board_utils import (
    add_gr_arc,
    add_gr_line,
    add_gr_rect,
    get_edge_cuts_items,
    get_fp_courtyard_bbox,
    get_fp_edge_cuts_items,
    remove_edge_cuts_items,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "tools", "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_outline.kicad_pcb")


@pytest.fixture
def pcb_data():
    return load_pcb(BOARD_FIXTURE)


# ---------------------------------------------------------------------------
# get_edge_cuts_items
# ---------------------------------------------------------------------------


class TestGetEdgeCutsItems:
    def test_returns_four_lines(self, pcb_data):
        items = get_edge_cuts_items(pcb_data)
        assert len(items) == 4

    def test_all_gr_line_type(self, pcb_data):
        items = get_edge_cuts_items(pcb_data)
        assert all(i["type"] == "gr_line" for i in items)

    def test_all_edge_cuts_layer(self, pcb_data):
        items = get_edge_cuts_items(pcb_data)
        assert all(i["layer"] == "Edge.Cuts" for i in items)

    def test_line_has_expected_keys(self, pcb_data):
        items = get_edge_cuts_items(pcb_data)
        for item in items:
            for key in ("x1", "y1", "x2", "y2", "width"):
                assert key in item, f"Missing key '{key}' in {item}"

    def test_empty_when_no_outline(self):
        # Build a minimal PCB with no Edge.Cuts items
        import sexpdata

        data = [sexpdata.Symbol("kicad_pcb")]
        items = get_edge_cuts_items(data)
        assert items == []

    def test_gr_rect_parsed(self):
        """get_edge_cuts_items parses gr_rect items correctly."""
        import sexpdata as sx

        s = sx.Symbol
        data = [
            s("kicad_pcb"),
            [
                s("gr_rect"),
                [s("start"), 0.0, 0.0],
                [s("end"), 50.0, 40.0],
                [s("stroke"), [s("width"), 0.05], [s("type"), s("solid")]],
                [s("layer"), "Edge.Cuts"],
            ],
        ]
        items = get_edge_cuts_items(data)
        assert len(items) == 1
        assert items[0]["type"] == "gr_rect"
        assert items[0]["x1"] == 0.0
        assert items[0]["y2"] == 40.0


# ---------------------------------------------------------------------------
# remove_edge_cuts_items
# ---------------------------------------------------------------------------


class TestRemoveEdgeCutsItems:
    def test_removes_four_lines(self, pcb_data):
        removed = remove_edge_cuts_items(pcb_data)
        assert removed == 4

    def test_no_edge_cuts_left(self, pcb_data):
        remove_edge_cuts_items(pcb_data)
        items = get_edge_cuts_items(pcb_data)
        assert items == []

    def test_footprints_untouched(self, pcb_data):
        """Removing Edge.Cuts must not remove footprints."""
        fp_count_before = sum(
            1
            for item in pcb_data
            if isinstance(item, list) and len(item) > 0 and str(item[0]) == "footprint"
        )
        remove_edge_cuts_items(pcb_data)
        fp_count_after = sum(
            1
            for item in pcb_data
            if isinstance(item, list) and len(item) > 0 and str(item[0]) == "footprint"
        )
        assert fp_count_before == fp_count_after


# ---------------------------------------------------------------------------
# add_gr_line
# ---------------------------------------------------------------------------


class TestAddGrLine:
    def test_appended_to_data(self, pcb_data):
        before = get_edge_cuts_items(pcb_data)
        add_gr_line(pcb_data, 5.0, 0.0, 10.0, 0.0, width=0.05)
        after = get_edge_cuts_items(pcb_data)
        assert len(after) == len(before) + 1

    def test_correct_coordinates(self, pcb_data):
        add_gr_line(pcb_data, 1.23, 4.56, 7.89, 0.12, width=0.1)
        items = get_edge_cuts_items(pcb_data)
        last = items[-1]
        assert last["x1"] == pytest.approx(1.23)
        assert last["y1"] == pytest.approx(4.56)
        assert last["x2"] == pytest.approx(7.89)
        assert last["y2"] == pytest.approx(0.12)

    def test_custom_layer(self):
        import sexpdata as sx

        data = [sx.Symbol("kicad_pcb")]
        add_gr_line(data, 0, 0, 1, 1, layer="Margin")
        # Should not appear in Edge.Cuts query
        items = get_edge_cuts_items(data)
        assert items == []


# ---------------------------------------------------------------------------
# add_gr_rect
# ---------------------------------------------------------------------------


class TestAddGrRect:
    def test_appended(self):
        import sexpdata as sx

        data = [sx.Symbol("kicad_pcb")]
        add_gr_rect(data, 0.0, 0.0, 50.0, 40.0)
        items = get_edge_cuts_items(data)
        assert len(items) == 1
        assert items[0]["type"] == "gr_rect"
        assert items[0]["x2"] == pytest.approx(50.0)
        assert items[0]["y2"] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# add_gr_arc
# ---------------------------------------------------------------------------


class TestAddGrArc:
    def test_appended_to_data(self):
        import sexpdata as sx

        data = [sx.Symbol("kicad_pcb")]
        add_gr_arc(data, 5.0, 5.0, 5.0, 180.0, 270.0)
        items = get_edge_cuts_items(data)
        assert len(items) == 1
        assert items[0]["type"] == "gr_arc"

    def test_start_end_on_circle(self):
        """start and end points must lie on the circle of given radius."""
        import sexpdata as sx

        cx, cy, r = 10.0, 10.0, 3.0
        data = [sx.Symbol("kicad_pcb")]
        add_gr_arc(data, cx, cy, r, 0.0, 90.0)
        items = get_edge_cuts_items(data)
        arc = items[0]
        d_start = math.hypot(arc["start_x"] - cx, arc["start_y"] - cy)
        d_end = math.hypot(arc["end_x"] - cx, arc["end_y"] - cy)
        assert d_start == pytest.approx(r, abs=1e-4)
        assert d_end == pytest.approx(r, abs=1e-4)

    def test_angular_positions_correct(self):
        """Verify the actual XY positions of start/end, not just their distance.

        In KiCad CCW file-angle convention (+Y down):
          0° → (cx+r, cy)  [right]
         90° → (cx,   cy-r) [up]
        180° → (cx-r, cy)  [left]
        270° → (cx,   cy+r) [down]
        """
        import sexpdata as sx

        cx, cy, r = 0.0, 0.0, 5.0
        data = [sx.Symbol("kicad_pcb")]
        add_gr_arc(data, cx, cy, r, 0.0, 90.0)
        arc = get_edge_cuts_items(data)[0]
        # Start at 0°: (r, 0)
        assert arc["start_x"] == pytest.approx(r, abs=1e-4)
        assert arc["start_y"] == pytest.approx(0.0, abs=1e-4)
        # End at 90° CCW (+Y-down): (0, -r)
        assert arc["end_x"] == pytest.approx(0.0, abs=1e-4)
        assert arc["end_y"] == pytest.approx(-r, abs=1e-4)

    def test_wraparound_arc_midpoint(self):
        """An arc from 315° to 45° (crossing 0°) should have midpoint at 0°."""
        import sexpdata as sx

        cx, cy, r = 0.0, 0.0, 5.0
        data = [sx.Symbol("kicad_pcb")]
        add_gr_arc(data, cx, cy, r, 315.0, 45.0)
        arc = get_edge_cuts_items(data)[0]
        # Mid at 0°: (r, 0)
        assert arc["mid_x"] == pytest.approx(r, abs=1e-4)
        assert arc["mid_y"] == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# get_fp_courtyard_bbox
# ---------------------------------------------------------------------------


class TestGetFpCourtyardBbox:
    def _fp_node(self, fp_x, fp_y, fp_rot, courtyard_half_w=1.0, courtyard_half_h=0.75):
        """Build a minimal footprint S-expression node with a rectangular courtyard."""
        import sexpdata as sx

        s = sx.Symbol
        hw, hh = courtyard_half_w, courtyard_half_h
        node = [
            s("footprint"),
            "TestLib:R",
            [s("at"), fp_x, fp_y, fp_rot],
            [s("layer"), "F.Cu"],
            # courtyard lines
            [
                s("fp_line"),
                [s("start"), -hw, -hh],
                [s("end"), hw, -hh],
                [s("layer"), "F.Courtyard"],
                [s("width"), 0.05],
            ],
            [
                s("fp_line"),
                [s("start"), hw, -hh],
                [s("end"), hw, hh],
                [s("layer"), "F.Courtyard"],
                [s("width"), 0.05],
            ],
            [
                s("fp_line"),
                [s("start"), hw, hh],
                [s("end"), -hw, hh],
                [s("layer"), "F.Courtyard"],
                [s("width"), 0.05],
            ],
            [
                s("fp_line"),
                [s("start"), -hw, hh],
                [s("end"), -hw, -hh],
                [s("layer"), "F.Courtyard"],
                [s("width"), 0.05],
            ],
        ]
        return node

    def test_no_rotation(self):
        fp = self._fp_node(10.0, 20.0, 0.0)
        bbox = get_fp_courtyard_bbox(fp, 10.0, 20.0, 0.0)
        assert bbox is not None
        assert bbox["min_x"] == pytest.approx(9.0)
        assert bbox["max_x"] == pytest.approx(11.0)
        assert bbox["min_y"] == pytest.approx(19.25)
        assert bbox["max_y"] == pytest.approx(20.75)
        assert bbox["width"] == pytest.approx(2.0)
        assert bbox["height"] == pytest.approx(1.5)

    def test_90_degree_rotation_swaps_wh(self):
        """After 90° CW rotation a 2×1.5 courtyard becomes 1.5×2."""
        fp = self._fp_node(0.0, 0.0, 90.0)
        bbox = get_fp_courtyard_bbox(fp, 0.0, 0.0, 90.0)
        assert bbox is not None
        assert bbox["width"] == pytest.approx(1.5, abs=1e-4)
        assert bbox["height"] == pytest.approx(2.0, abs=1e-4)

    def test_180_degree_rotation_same_bbox(self):
        """180° rotation around centre produces same AABB as 0° for symmetric courtyard."""
        fp0 = self._fp_node(5.0, 5.0, 0.0)
        fp180 = self._fp_node(5.0, 5.0, 180.0)
        b0 = get_fp_courtyard_bbox(fp0, 5.0, 5.0, 0.0)
        b180 = get_fp_courtyard_bbox(fp180, 5.0, 5.0, 180.0)
        assert b0 is not None and b180 is not None
        assert b0["min_x"] == pytest.approx(b180["min_x"], abs=1e-4)
        assert b0["max_y"] == pytest.approx(b180["max_y"], abs=1e-4)

    def test_no_courtyard_falls_back(self):
        """If no courtyard layer items exist the function falls back to all fp_line."""
        import sexpdata as sx

        s = sx.Symbol
        fp = [
            s("footprint"),
            "TestLib:R",
            [s("at"), 0.0, 0.0, 0.0],
            [s("layer"), "F.Cu"],
            # no courtyard — just fab layer
            [
                s("fp_line"),
                [s("start"), -1.0, -1.0],
                [s("end"), 1.0, 1.0],
                [s("layer"), "F.Fab"],
                [s("width"), 0.1],
            ],
        ]
        bbox = get_fp_courtyard_bbox(fp, 0.0, 0.0, 0.0)
        assert bbox is not None

    def test_no_geometry_returns_none(self):
        import sexpdata as sx

        s = sx.Symbol
        fp = [s("footprint"), "TestLib:R", [s("at"), 0.0, 0.0, 0.0], [s("layer"), "F.Cu"]]
        assert get_fp_courtyard_bbox(fp, 0.0, 0.0, 0.0) is None


# ---------------------------------------------------------------------------
# get_fp_edge_cuts_items
# ---------------------------------------------------------------------------


class TestGetFpEdgeCutsItems:
    def _fp_node(self):
        """A footprint node with fp graphics on several layers."""
        import sexpdata as sx

        s = sx.Symbol
        return [
            s("footprint"),
            "TestLib:EC",
            [s("at"), 0.0, 0.0, 0.0],
            [s("layer"), "F.Cu"],
            [
                s("fp_line"),
                [s("start"), -1.0, 0.5],
                [s("end"), 1.5, -0.5],
                [s("stroke"), [s("width"), 0.1], [s("type"), s("solid")]],
                [s("layer"), "Edge.Cuts"],
            ],
            [
                s("fp_arc"),
                [s("start"), 0.0, 0.0],
                [s("mid"), 1.0, 1.0],
                [s("end"), 2.0, 0.0],
                [s("stroke"), [s("width"), 0.2]],
                [s("layer"), "Edge.Cuts"],
            ],
            [
                s("fp_rect"),
                [s("start"), 0.0, 0.0],
                [s("end"), 3.0, 4.0],
                [s("stroke"), [s("width"), 0.05]],
                [s("layer"), "Edge.Cuts"],
            ],
            [
                s("fp_circle"),
                [s("center"), 5.0, 5.0],
                [s("end"), 6.0, 5.0],
                [s("stroke"), [s("width"), 0.3]],
                [s("layer"), "Edge.Cuts"],
            ],
            [
                s("fp_curve"),
                [s("pts"), [s("xy"), 0.0, 0.0], [s("xy"), 1.0, 2.0], [s("xy"), 2.0, 0.0]],
                [s("stroke"), [s("width"), 0.15]],
                [s("layer"), "Edge.Cuts"],
            ],
            # Must be ignored: courtyard layer, plain line layer
            [
                s("fp_line"),
                [s("start"), 0.0, 0.0],
                [s("end"), 10.0, 10.0],
                [s("layer"), "F.CrtYd"],
                [s("width"), 0.05],
            ],
            [
                s("fp_line"),
                [s("start"), 0.0, 0.0],
                [s("end"), 1.0, 1.0],
                [s("layer"), "F.SilkS"],
                [s("width"), 0.05],
            ],
            # Must be ignored: pads
            [
                s("pad"),
                "1",
                s("smd"),
                s("rect"),
                [s("at"), 0.0, 0.0, 0.0],
                [s("size"), 1.0, 1.0],
                [s("layer"), "F.Cu"],
            ],
        ]

    def test_parses_all_fp_types(self):
        items = get_fp_edge_cuts_items(self._fp_node())
        assert [i["type"] for i in items] == [
            "fp_line",
            "fp_arc",
            "fp_rect",
            "fp_circle",
            "fp_curve",
        ]

    def test_line_geometry(self):
        item = get_fp_edge_cuts_items(self._fp_node())[0]
        assert item["x1"] == pytest.approx(-1.0)
        assert item["y1"] == pytest.approx(0.5)
        assert item["x2"] == pytest.approx(1.5)
        assert item["y2"] == pytest.approx(-0.5)
        assert item["width"] == pytest.approx(0.1)
        assert item["layer"] == "Edge.Cuts"

    def test_arc_geometry(self):
        item = get_fp_edge_cuts_items(self._fp_node())[1]
        assert item["start_x"] == pytest.approx(0.0)
        assert item["start_y"] == pytest.approx(0.0)
        assert item["mid_x"] == pytest.approx(1.0)
        assert item["mid_y"] == pytest.approx(1.0)
        assert item["end_x"] == pytest.approx(2.0)
        assert item["end_y"] == pytest.approx(0.0)
        assert item["width"] == pytest.approx(0.2)

    def test_rect_geometry(self):
        item = get_fp_edge_cuts_items(self._fp_node())[2]
        assert item["x2"] == pytest.approx(3.0)
        assert item["y2"] == pytest.approx(4.0)

    def test_circle_geometry(self):
        item = get_fp_edge_cuts_items(self._fp_node())[3]
        assert item["cx"] == pytest.approx(5.0)
        assert item["cy"] == pytest.approx(5.0)
        assert item["ex"] == pytest.approx(6.0)
        assert item["ey"] == pytest.approx(5.0)
        assert item["width"] == pytest.approx(0.3)

    def test_curve_pts(self):
        item = get_fp_edge_cuts_items(self._fp_node())[4]
        assert item["pts"] == [(0.0, 0.0), (1.0, 2.0), (2.0, 0.0)]
        assert item["width"] == pytest.approx(0.15)

    def test_ignores_non_edge_cuts_layers_and_pads(self):
        items = get_fp_edge_cuts_items(self._fp_node())
        assert len(items) == 5
        assert all(i["layer"] == "Edge.Cuts" for i in items)

    def test_empty_without_edge_cuts(self):
        import sexpdata as sx

        s = sx.Symbol
        fp = [
            s("footprint"),
            "TestLib:Plain",
            [
                s("fp_line"),
                [s("start"), 0.0, 0.0],
                [s("end"), 1.0, 1.0],
                [s("layer"), "F.SilkS"],
                [s("width"), 0.05],
            ],
        ]
        assert get_fp_edge_cuts_items(fp) == []

    def test_legacy_edge_cuts_layer_name(self):
        import sexpdata as sx

        s = sx.Symbol
        fp = [
            [
                s("fp_line"),
                [s("start"), 0.0, 0.0],
                [s("end"), 2.0, 0.0],
                [s("layer"), "Edge_Cuts"],
                [s("width"), 0.1],
            ],
        ]
        items = get_fp_edge_cuts_items(fp)
        assert len(items) == 1
        assert items[0]["layer"] == "Edge_Cuts"

    def test_bare_width_node_supported(self):
        """Legacy fp_line with a direct (width ...) child still yields width."""
        import sexpdata as sx

        s = sx.Symbol
        fp = [
            [
                s("fp_line"),
                [s("start"), 0.0, 0.0],
                [s("end"), 1.0, 1.0],
                [s("layer"), "Edge.Cuts"],
                [s("width"), 0.07],
            ],
        ]
        item = get_fp_edge_cuts_items(fp)[0]
        assert item["width"] == pytest.approx(0.07)
