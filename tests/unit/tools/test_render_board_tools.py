"""Unit tests for kcaa/tools/render_board_tools.py.

Covers the #152 render enhancements (pad-label drawing baseline with
short labels and an in-frame flip near the frame edges, label collision
avoidance, region / zoom rendering, footprint reference labels and
pad-coordinate reporting) and circle shape support (gr_circle / fp_circle
parsing with center coordinate, bounding box with circular geometry,
multi-layer rendering, and robust drawing of malformed entries).
"""

import asyncio
import inspect
import math
import os
import struct

import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")

from kcaa.tools.render_board_tools import (  # noqa: E402
    _BG_COLOR,
    BoardData,
    _board_outline,
    _bounds,
    _draw_board_layers,
    _draw_shape,
    _parse_shape,
    parse_board,
    render_board,
)

FIXTURE_PADS = 6  # R1.1/2, C1.1/2 (F.Cu), J1.1/2 (thru-hole, full stack)


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.render_board_tools import register_render_board_tools

    mock = _MockMCP()
    register_render_board_tools(mock)
    return mock.tools


def _run(coro) -> tuple[str, object]:
    return asyncio.run(coro)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "expected a PNG payload"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _write_synthetic_board(path: str, pad_specs: list[tuple[str, float, float]]) -> str:
    """Write a tiny board with one footprint per ``(ref, x, y)`` pad spec.

    Pads are 0.5 mm square on F.Cu at the given world position (footprint
    at origin).  Position nearly-identical pads to force label collisions.
    """
    lines = [
        "(kicad_pcb",
        "\t(version 20260206)",
        '\t(generator "pcbnew")',
        '\t(generator_version "10.0")',
        "\t(general",
        "\t\t(thickness 1.6)",
        "\t)",
        '\t(paper "A4")',
        "\t(layers",
        '\t\t(0 "F.Cu" signal)',
        '\t\t(31 "B.Cu" signal)',
        "\t)",
    ]
    for i, (ref, x, y) in enumerate(pad_specs):
        lines.extend(
            [
                '\t(footprint "Resistor_SMD:R_0402_1005Metric"',
                '\t\t(layer "F.Cu")',
                f'\t\t(uuid "00000000-0000-0000-0000-{i:012d}")',
                "\t\t(at 0 0 0)",
                f'\t\t(property "Reference" "{ref}")',
                '\t\t(property "Value" "R")',
                '\t\t(pad "1" smd rect',
                f"\t\t\t(at {x} {y})",
                "\t\t\t(size 0.5 0.5)",
                '\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")',
                "\t\t)",
                "\t)",
            ]
        )
    lines.append(")")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def _dense_board_path(tmp_path) -> str:
    # 12 pads packed at 0.25 mm pitch in a row: labels (0.6 mm offset,
    # ~1.2 mm wide) are guaranteed to overlap regardless of side chosen.
    return _write_synthetic_board(
        str(tmp_path / "dense.kicad_pcb"), [("U11", i * 0.25, 0.0) for i in range(12)]
    )


def _sparse_board_path(tmp_path) -> str:
    # 4 pads 10 mm apart: labels can never collide.
    return _write_synthetic_board(
        str(tmp_path / "sparse.kicad_pcb"),
        [("R1", 0.0, 0.0), ("R2", 10.0, 0.0), ("R3", 0.0, 10.0), ("R4", 10.0, 10.0)],
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


def _silk_ref_board_path(tmp_path) -> str:
    """One footprint ("U7") with pads at (10,0)/(20,0) and a silkscreen
    reference text anchored at (0,0) — away from the pads, so a region zoom
    around the pads excludes the silk text while full-board views show it.
    """
    lines = [
        "(kicad_pcb",
        "\t(version 20260206)",
        '\t(generator "pcbnew")',
        '\t(generator_version "10.0")',
        "\t(general",
        "\t\t(thickness 1.6)",
        "\t)",
        '\t(paper "A4")',
        "\t(layers",
        '\t\t(0 "F.Cu" signal)',
        '\t\t(31 "B.Cu" signal)',
        "\t)",
        '\t(footprint "Resistor_SMD:R_0402_1005Metric"',
        '\t\t(layer "F.Cu")',
        '\t\t(uuid "00000000-0000-0000-0000-0000000000f0")',
        "\t\t(at 0 0 0)",
        '\t\t(property "Reference" "U7")',
        '\t\t(property "Value" "R")',
        '\t\t(fp_text reference "U7" (at 0 0) (layer "F.SilkS")',
        "\t\t\t(effects (font (size 1 1) (thickness 0.15)))",
        "\t\t)",
        '\t\t(pad "1" smd rect',
        "\t\t\t(at 10 0)",
        "\t\t\t(size 0.5 0.5)",
        '\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")',
        "\t\t)",
        '\t\t(pad "2" smd rect',
        "\t\t\t(at 20 0)",
        "\t\t\t(size 0.5 0.5)",
        '\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")',
        "\t\t)",
        "\t)",
        ")",
    ]
    path = tmp_path / "silk_ref.kicad_pcb"
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def _cutout_board_path(tmp_path) -> str:
    """Board with a rect outline (gr_line chain), an internal gr_rect
    opening, a footprint-level fp_rect opening (LED window), plus a corner
    gr_circle sitting exactly on the outline corner (a rounding, not a
    hole) — the fixture for Edge.Cuts cutout rendering.
    """
    lines = [
        "(kicad_pcb",
        "\t(version 20260206)",
        '\t(generator "pcbnew")',
        '\t(generator_version "10.0")',
        "\t(general",
        "\t\t(thickness 1.6)",
        "\t)",
        '\t(paper "A4")',
        "\t(layers",
        '\t\t(0 "F.Cu" signal)',
        '\t\t(31 "B.Cu" signal)',
        '\t\t(44 "Edge.Cuts" user)',
        "\t)",
        # Outer outline: 100 x 60 mm rectangle.
        '\t(gr_line (start 0 0) (end 100 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        '\t(gr_line (start 100 0) (end 100 60) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        '\t(gr_line (start 100 60) (end 0 60) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        '\t(gr_line (start 0 60) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        # Corner rounding circle: center on the outline corner.
        '\t(gr_circle (center 0 0) (end 5 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        # Internal rectangular opening.
        '\t(gr_rect (start 40 20) (end 50 30) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        # Internal circular opening.
        '\t(gr_circle (center 80 50) (end 83 50) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        # Footprint-level rectangular opening (Smart LED window).
        '\t(footprint "Test:LED"',
        '\t\t(layer "F.Cu")',
        '\t\t(uuid "00000000-0000-0000-0000-0000000000a1")',
        "\t\t(at 70 40 0)",
        '\t\t(fp_rect (start -3 -2) (end 3 2) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
        "\t)",
        ")",
    ]
    path = tmp_path / "cutout.kicad_pcb"
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


class TestEdgeCutoutHoles:
    """Edge.Cuts internal openings render as background-colored holes."""

    @pytest.fixture
    def board(self, tmp_path):
        return parse_board(_cutout_board_path(tmp_path))

    def _bg_filled(self, ax):
        bg = mcolors.to_rgba(_BG_COLOR)
        return [p for p in ax.patches if p.get_fill() and p.get_facecolor() == bg]

    def test_outline_recovered_with_cutouts(self, board):
        # gr_circle/gr_rect among the edges must not break outline recovery.
        outline = _board_outline(board)
        assert outline is not None
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        assert min(xs) == 0.0 and max(xs) == 100.0
        assert min(ys) == 0.0 and max(ys) == 60.0

    def test_internal_rects_filled_with_background(self, board):
        fig, ax = plt.subplots()
        _draw_board_layers(ax, board)
        rects = [p for p in self._bg_filled(ax) if isinstance(p, mpatches.Rectangle)]
        # gr_rect (40,20)-(50,30) and fp_rect (67,38)-(73,42) both filled.
        assert len(rects) == 2
        xys = sorted((p.get_xy()[0], p.get_xy()[1]) for p in rects)
        assert xys == [(40.0, 20.0), (67.0, 38.0)]
        plt.close(fig)

    def test_internal_circle_filled_with_background(self, board):
        fig, ax = plt.subplots()
        _draw_board_layers(ax, board)
        fills = [p for p in self._bg_filled(ax) if isinstance(p, mpatches.Circle)]
        assert len(fills) == 1
        assert fills[0].center == (80.0, 50.0)
        plt.close(fig)

    def test_outline_corner_circle_not_filled(self, board):
        fig, ax = plt.subplots()
        _draw_board_layers(ax, board)
        corner = [
            p for p in ax.patches if isinstance(p, mpatches.Circle) and p.center == (0.0, 0.0)
        ]
        assert len(corner) == 1
        assert not corner[0].get_fill()  # ring only — no hole on the outline
        filled_circles = [p for p in self._bg_filled(ax) if isinstance(p, mpatches.Circle)]
        assert all(p.center != (0.0, 0.0) for p in filled_circles)
        plt.close(fig)


class TestRenderBoardBaseline:
    def test_default_render_report_keys(self):
        lines, png, report = render_board(BOARD_FIXTURE)
        assert report["pads"] == FIXTURE_PADS
        assert report["copper_layers"] == ["F.Cu", "B.Cu"]
        # Unchanged baseline fields.
        assert report["connect_pads_requested"] == 0
        assert report["missing_pads"] == []
        assert report["pending_nets"] == []
        assert report["routed_nets"] == []
        # #152 additions, present by default.
        assert report["pad_labels"] > 0
        assert report["label_collisions"] >= 0
        assert isinstance(report["label_skipped"], list)
        assert report["footprint_refs"] == 3  # R1/C1/J1 (fixture has no silk refs)
        assert report["footprint_refs_skipped"] == []
        assert len(report["region_bbox"]) == 4
        assert _png_size(png)[0] > 0
        assert len(lines) >= 1

    def test_show_pad_labels_false_draws_none(self):
        _, _, report = render_board(BOARD_FIXTURE, show_pad_labels=False)
        assert report["pad_labels"] == 0
        assert report["label_collisions"] == 0
        assert report["label_skipped"] == []
        # Turning pad labels off does not kill the footprint refs.
        assert report["footprint_refs"] == 3

    def test_connect_pads_and_layer_still_work(self):
        _, _, report = render_board(BOARD_FIXTURE, connect_pads=["J1.1", "R1.2"], layer="F.Cu")
        # J1.1 (GND) and R1.2 (GND) share an unrouted net -> ratsnest shown.
        assert report["pending_nets"] == ["GND"]
        assert report["missing_pads"] == []
        # Layer filter does not change pad-label collision behavior.
        assert report["pads"] == FIXTURE_PADS

    def test_label_scale_accepted(self):
        _, _, report = render_board(BOARD_FIXTURE, label_scale=0.5)
        assert report["pad_labels"] > 0


class TestPadLabelCollision:
    def test_dense_pads_skip_overlapping_labels(self, tmp_path):
        lines, _, report = render_board(_dense_board_path(tmp_path))
        assert report["pads"] == 12
        assert report["label_collisions"] > 0
        assert len(report["label_skipped"]) == report["label_collisions"]
        assert report["pad_labels"] == report["pads"] - report["label_collisions"]
        # Skipped refs are real pads of the board.
        labels = {f"U11.{i}" for i in range(1, 13)}
        assert all(s in labels for s in report["label_skipped"])
        # The human report tells the caller about the skips.
        assert any("skipped" in line for line in lines)

    def test_sparse_pads_draw_every_label(self, tmp_path):
        _, _, report = render_board(_sparse_board_path(tmp_path))
        assert report["pads"] == 4
        assert report["label_collisions"] == 0
        assert report["label_skipped"] == []
        assert report["pad_labels"] == 4

    def test_collision_count_is_deterministic(self, tmp_path):
        path = _dense_board_path(tmp_path)
        _, _, first = render_board(path)
        _, _, second = render_board(path)
        assert first["label_collisions"] == second["label_collisions"]
        assert first["label_skipped"] == second["label_skipped"]


class TestRegionRendering:
    def test_region_overrides_rendered_bbox(self, tmp_path):
        region = [5.0, 10.0, 25.0, 35.0]
        lines, png, report = render_board(_sparse_board_path(tmp_path), region=region)
        assert report["region_bbox"] == region
        width, height = _png_size(png)
        assert width > 0 and height > 0
        assert len(lines) >= 1

    def test_default_region_bbox_is_full_board(self, tmp_path):
        _, _, default = render_board(_sparse_board_path(tmp_path))
        board = parse_board(_sparse_board_path(tmp_path))
        from kcaa.tools.render_board_tools import _bounds

        xmin, ymin, xmax, ymax = _bounds(board, [])
        assert default["region_bbox"] == pytest.approx([xmin, ymin, xmax, ymax])

    def test_narrow_region_renders_at_full_board_sharpness(self, tmp_path):
        # A 0.5 mm-wide region of a 40 mm-wide board must still render
        # >= ~1600 px wide (dpi floor scales with the rendered width).
        _, png, _ = render_board(_sparse_board_path(tmp_path), region=[0.0, 0.0, 0.5, 0.5])
        width, _ = _png_size(png)
        assert width >= 1590  # int(1600 / fig_w_in) truncates one px at most

    @pytest.mark.parametrize(
        "bad",
        [
            [1.0, 1.0, 1.0],  # wrong length
            [5.0, 5.0, 2.0, 9.0],  # x_min >= x_max
            [5.0, 9.0, 25.0, 2.0],  # y_min >= y_max
        ],
    )
    def test_invalid_region_raises(self, tmp_path, bad):
        with pytest.raises(ValueError, match="region"):
            render_board(_sparse_board_path(tmp_path), region=bad)


class TestPadCoords:
    def test_centers_match_parsed_board(self, tmp_path):
        path = _sparse_board_path(tmp_path)
        _, _, report = render_board(path, include_pad_coords=True)
        board = parse_board(path)
        expected = {(p.ref, p.number): p for p in board.pads}
        assert len(report["pads_coords"]) == len(board.pads)
        for entry in report["pads_coords"]:
            pad = expected[(entry["ref"], entry["number"])]
            assert entry["net"] == pad.net
            assert entry["center"][0] == pytest.approx(pad.center[0], abs=1e-6)
            assert entry["center"][1] == pytest.approx(pad.center[1], abs=1e-6)
            assert entry["layer"] == (pad.copper_layers[0] if pad.copper_layers else None)

    def test_coords_absent_by_default(self, tmp_path):
        _, _, report = render_board(_sparse_board_path(tmp_path))
        assert "pads_coords" not in report

    def test_coords_filtered_to_rendered_layer(self):
        _, _, report = render_board(BOARD_FIXTURE, layer="B.Cu", include_pad_coords=True)
        refs = {(c["ref"], c["number"]) for c in report["pads_coords"]}
        # Only thru-hole J1 pads span B.Cu (R1/C1 are F.Cu-only).
        assert refs == {("J1", "1"), ("J1", "2")}
        board = parse_board(BOARD_FIXTURE)
        by_ref = {(p.ref, p.number): p for p in board.pads}
        for c in report["pads_coords"]:
            assert "B.Cu" in by_ref[(c["ref"], c["number"])].copper_layers
            # Single-layer render reports the layer it actually shows.
            assert c["layer"] == "B.Cu"

    def test_coords_filtered_to_region(self, tmp_path):
        # Sparse pads at (0,0), (10,0), (0,10), (10,10): only R2.1 fits the box.
        _, _, report = render_board(
            _sparse_board_path(tmp_path),
            include_pad_coords=True,
            region=[5.5, -1.0, 15.5, 1.0],
        )
        assert [(c["ref"], c["number"]) for c in report["pads_coords"]] == [("R2", "1")]
        assert report["pads_coords"][0]["layer"] == "F.Cu"


class TestPadLabelPlacement:
    """The label flips stay in-frame when a pad sits near the frame edge.

    The y axis is inverted (KiCad +Y down): "up" is decreasing data y, so a
    right-up label box spans x [cx+off, cx+off+w], y [cy-off-h, cy-off].
    """

    REGION = (0.0, 0.0, 20.0, 20.0)

    def _box(self, cx: float, cy: float) -> tuple[float, float, float, float]:
        import matplotlib.pyplot as plt

        from kcaa.tools.render_board_tools import (
            Pad,
            _draw_pad_label,
            _label_data_bbox,
            _new_board_figure,
        )

        xmin, ymin, xmax, ymax = self.REGION
        fig, ax, _, _ = _new_board_figure(xmin, ymin, xmax, ymax, 200)
        try:
            renderer = fig.canvas.get_renderer()
            pad = Pad(
                ref="R",
                number="10",
                net=None,
                center=(cx, cy),
                copper_layers=["F.Cu"],
                shape=None,
            )
            artist = _draw_pad_label(ax, pad)
            return _label_data_bbox(ax, artist, renderer)
        finally:
            plt.close(fig)

    def _assert_in_frame(self, box) -> None:
        xmin, ymin, xmax, ymax = self.REGION
        assert box[0] >= xmin and box[2] <= xmax, f"label out of x range: {box}"
        assert box[1] >= ymin and box[3] <= ymax, f"label out of y range: {box}"

    def test_interior_pad_label_right_of_and_above_center(self):
        box = self._box(10.0, 10.0)
        # Right-up box: left edge right of the pad, top edge above it.
        assert box[0] >= 10.0 and box[3] <= 10.0
        self._assert_in_frame(box)

    def test_near_right_edge_flips_left_down(self):
        box = self._box(19.4, 10.0)  # right-up would poke past x_max
        assert box[2] <= 19.4 and box[1] >= 10.0
        self._assert_in_frame(box)

    def test_near_top_edge_flips_left_down(self):
        box = self._box(10.0, 0.4)  # right-up would poke past y_min
        assert box[2] <= 10.0 and box[1] >= 0.4  # left of / below the pad
        self._assert_in_frame(box)

    def test_top_left_corner_stays_in_frame(self):
        box = self._box(0.4, 0.4)  # both primary sides poke out -> right-down
        self._assert_in_frame(box)

    def test_top_right_corner_stays_in_frame(self):
        box = self._box(19.4, 0.4)
        self._assert_in_frame(box)

    def test_bottom_right_corner_stays_in_frame(self):
        box = self._box(19.6, 19.6)  # both primary sides poke out -> left-up
        self._assert_in_frame(box)

    def test_near_left_edge_keeps_right_up(self):
        box = self._box(0.4, 10.0)  # right-up extends right: still fits
        assert box[0] >= 0.4 and box[3] <= 10.0
        self._assert_in_frame(box)

    def test_near_bottom_edge_keeps_right_up(self):
        box = self._box(10.0, 19.6)  # right-up extends up: still fits
        assert box[0] >= 10.0 and box[3] <= 19.6
        self._assert_in_frame(box)


class TestPadLabelFormats:
    def _texts(self, tmp_path, label_format: str = "number") -> set[str]:
        import matplotlib.pyplot as plt

        from kcaa.tools.render_board_tools import (
            _bounds,
            _draw_board_layers,
            _new_board_figure,
        )

        board = parse_board(_sparse_board_path(tmp_path))
        xmin, ymin, xmax, ymax = _bounds(board, [])
        fig, ax, _, _ = _new_board_figure(xmin, ymin, xmax, ymax, 200)
        try:
            _draw_board_layers(ax, board, show_pad_labels=True, label_format=label_format)
            return {t.get_text() for t in ax.texts}
        finally:
            plt.close(fig)

    def test_default_labels_are_short_numbers(self, tmp_path):
        texts = self._texts(tmp_path)
        assert "1" in texts  # short number label
        assert "R1.1" not in texts
        assert all("." not in t for t in texts)

    def test_ref_number_format_keeps_legacy_labels(self, tmp_path):
        texts = self._texts(tmp_path, label_format="ref.number")
        assert "R1.1" in texts
        assert any("." in t for t in texts)

    def test_unknown_format_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="label_format"):
            self._texts(tmp_path, label_format="bogus")


class TestFootprintRefs:
    # test_board.kicad_pcb carries no silkscreen reference texts, so refs
    # are re-drawn for every footprint that shows pads.
    def test_drawn_for_every_footprint_by_default(self):
        _, _, report = render_board(BOARD_FIXTURE)
        assert report["footprint_refs"] == 3
        assert report["footprint_refs_skipped"] == []

    def test_disabled_flag_zeroes_refs(self):
        _, _, report = render_board(BOARD_FIXTURE, show_footprint_refs=False)
        assert report["footprint_refs"] == 0
        assert report["footprint_refs_skipped"] == []

    def test_refs_only_for_footprints_with_rendered_pads(self):
        # B.Cu render keeps only thru-hole J1 pads -> only J1 gets a ref.
        _, _, report = render_board(BOARD_FIXTURE, layer="B.Cu")
        assert report["footprint_refs"] == 1

    def test_deduped_against_visible_silkscreen_ref(self, tmp_path):
        _, _, report = render_board(_silk_ref_board_path(tmp_path))
        assert report["footprint_refs"] == 0  # silk "U7" already on screen

    def test_redrawn_when_region_excludes_silk_ref(self, tmp_path):
        # Region box holds only U7's pads; the silk text anchored at (0,0)
        # is filtered out, so the ref is re-drawn above the pad centroid.
        _, _, report = render_board(_silk_ref_board_path(tmp_path), region=[8.0, -2.0, 22.0, 2.0])
        assert report["footprint_refs"] == 1


class TestToolRegistration:
    def test_export_pcb_layer_image_exposes_new_params(self):
        tools = _get_tools()
        assert "export_pcb_layer_image" in tools
        sig = inspect.signature(tools["export_pcb_layer_image"])
        for param in (
            "show_pad_labels",
            "label_scale",
            "region",
            "include_pad_coords",
            "label_format",
            "show_footprint_refs",
        ):
            assert param in sig.parameters
        assert sig.parameters["show_pad_labels"].default is True
        assert sig.parameters["include_pad_coords"].default is False
        assert sig.parameters["label_format"].default == "number"
        assert sig.parameters["show_footprint_refs"].default is True

    def test_tool_returns_report_and_image(self, tmp_path):
        tools = _get_tools()
        report_text, img = _run(
            tools["export_pcb_layer_image"](
                pcb_path=_sparse_board_path(tmp_path),
                include_pad_coords=True,
                ctx=None,
            )
        )
        assert "report=" in report_text
        assert "pads_coords" in report_text
        assert img.data[:8] == b"\x89PNG\r\n\x1a\n"
