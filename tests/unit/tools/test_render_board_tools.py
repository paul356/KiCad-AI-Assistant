"""
Unit tests for kcaa/tools/render_board_tools.py

Covers parsing and rendering of circle shapes (gr_circle and fp_circle),
bounding box calculations with circular geometry, and multi-layer rendering.
"""

import math

import matplotlib.pyplot as plt

from kcaa.tools.render_board_tools import (
    BoardData,
    _bounds,
    _draw_shape,
    _parse_shape,
    render_board,
)


def test_parse_gr_circle():
    """Verify gr_circle parses center, end, and aliases start."""
    circle_node = [
        "gr_circle",
        ["center", 25.0, 20.0],
        ["end", 27.0, 20.0],
        ["stroke", ["width", 0.1], ["type", "solid"]],
        ["layer", "Edge.Cuts"],
    ]
    entry = _parse_shape(circle_node, (0.0, 0.0, 0.0))
    assert entry is not None
    assert entry["kind"] == "gr_circle"
    assert entry["layer"] == "Edge.Cuts"
    assert entry["center"] == (25.0, 20.0)
    assert entry["start"] == (25.0, 20.0)
    assert entry["end"] == (27.0, 20.0)


def test_parse_fp_circle_with_transform():
    """Verify fp_circle applies footprint offset and rotation to center and end."""
    # Footprint at (10, 10), rotated 90 degrees CCW
    fp_at = (10.0, 10.0, 90.0)
    circle_node = [
        "fp_circle",
        ["center", 5.0, 0.0],
        ["end", 5.0, 2.0],
        ["stroke", ["width", 0.15], ["type", "solid"]],
        ["layer", "F.CrtYd"],
    ]
    entry = _parse_shape(circle_node, fp_at)
    assert entry is not None
    assert entry["kind"] == "fp_circle"
    assert entry["layer"] == "F.CrtYd"
    # (5, 0) rotated 90 deg -> (0, -5) in Y-down -> world (10, 5)
    assert math.isclose(entry["center"][0], 10.0, abs_tol=1e-5)
    assert math.isclose(entry["center"][1], 5.0, abs_tol=1e-5)
    assert entry["start"] == entry["center"]
    # Radius must be invariant under rigid transform
    r = math.hypot(entry["end"][0] - entry["center"][0], entry["end"][1] - entry["center"][1])
    assert math.isclose(r, 2.0, abs_tol=1e-5)


def test_parse_circle_legacy_start_fallback():
    """Verify circle with start node (legacy) still populates center."""
    circle_node = [
        "gr_circle",
        ["start", 15.0, 30.0],
        ["end", 18.0, 30.0],
        ["layer", "Edge.Cuts"],
    ]
    entry = _parse_shape(circle_node, (0.0, 0.0, 0.0))
    assert entry is not None
    assert entry["center"] == (15.0, 30.0)
    assert entry["start"] == (15.0, 30.0)
    assert entry["end"] == (18.0, 30.0)


def test_draw_shape_circle():
    """Verify _draw_shape places a matplotlib Circle patch at center with radius."""
    fig, ax = plt.subplots()
    entry = {
        "kind": "gr_circle",
        "layer": "Edge.Cuts",
        "center": (12.0, 15.0),
        "start": (12.0, 15.0),
        "end": (15.0, 19.0),  # dx=3, dy=4 -> radius=5
    }
    _draw_shape(ax, entry, color="green", lw=1.0, alpha=1.0, zorder=1)
    assert len(ax.patches) == 1
    patch = ax.patches[0]
    assert patch.center == (12.0, 15.0)
    assert math.isclose(patch.radius, 5.0, abs_tol=1e-5)
    plt.close(fig)


def test_draw_shape_malformed_does_not_crash():
    """Verify _draw_shape handles entries missing coordinates gracefully."""
    fig, ax = plt.subplots()
    # Missing end
    _draw_shape(ax, {"kind": "gr_circle", "center": (0.0, 0.0)}, "red", 1.0, 1.0, 1)
    # Missing start/center
    _draw_shape(ax, {"kind": "gr_circle", "end": (1.0, 1.0)}, "red", 1.0, 1.0, 1)
    # Malformed line or rect
    _draw_shape(ax, {"kind": "gr_line"}, "red", 1.0, 1.0, 1)
    _draw_shape(ax, {"kind": "gr_rect"}, "red", 1.0, 1.0, 1)
    _draw_shape(ax, {"kind": "gr_arc"}, "red", 1.0, 1.0, 1)
    assert len(ax.patches) == 0
    plt.close(fig)


def test_bounds_with_circle():
    """Verify _bounds expands bounding box according to circle center and radius."""
    board = BoardData()
    board.edges = [
        {
            "kind": "gr_circle",
            "layer": "Edge.Cuts",
            "center": (50.0, 50.0),
            "end": (60.0, 50.0),  # radius = 10 -> x in [40, 60], y in [40, 60]
        }
    ]
    xmin, ymin, xmax, ymax = _bounds(board, [])
    # _bounds applies pad = 2.0
    assert math.isclose(xmin, 40.0 - 2.0, abs_tol=1e-5)
    assert math.isclose(ymin, 40.0 - 2.0, abs_tol=1e-5)
    assert math.isclose(xmax, 60.0 + 2.0, abs_tol=1e-5)
    assert math.isclose(ymax, 60.0 + 2.0, abs_tol=1e-5)


def test_render_board_with_circles(tmp_path):
    """Verify render_board completes without error across all layer views for board with circles."""
    board_sexp = """(kicad_pcb
  (version 20240108)
  (generator "kicad_ai_assistant_test")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user)
    (37 "F.SilkS" user)
    (44 "Edge.Cuts" user)
    (48 "B.CrtYd" user)
    (49 "F.CrtYd" user)
  )
  (gr_line (start 0 0) (end 100 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 100 0) (end 100 80) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 100 80) (end 0 80) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_line (start 0 80) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_circle (center 10 10) (end 12 10) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_circle (center 90 10) (end 92 10) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (footprint "Test:CircleMount"
    (layer "F.Cu")
    (at 50 40 0)
    (fp_circle (center 0 0) (end 3 0) (stroke (width 0.15) (type solid)) (layer "Edge.Cuts"))
    (fp_circle (center 0 0) (end 5 0) (stroke (width 0.15) (type solid)) (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
)
"""
    pcb_file = tmp_path / "circle_test.kicad_pcb"
    pcb_file.write_text(board_sexp)

    for layer in (None, "F.Cu", "B.Cu"):
        lines, png, report = render_board(str(pcb_file), layer=layer)
        assert len(png) > 1000
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert report["copper_layers"] == ["F.Cu", "B.Cu"]
