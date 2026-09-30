"""Unit tests for kcaa/tools/render_board_tools.py.

Covers the #152 render enhancements (pad-label drawing baseline, label
collision avoidance, region / zoom rendering, pad-coordinate reporting)
and circle shape support (gr_circle / fp_circle parsing with center
coordinate, bounding box with circular geometry, multi-layer rendering,
and robust drawing of malformed entries).
"""

import asyncio
import inspect
import math
import os
import struct

import matplotlib.pyplot as plt
import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")

from kcaa.tools.render_board_tools import (  # noqa: E402
    BoardData,
    _bounds,
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
        assert len(report["region_bbox"]) == 4
        assert _png_size(png)[0] > 0
        assert len(lines) >= 1

    def test_show_pad_labels_false_draws_none(self):
        _, _, report = render_board(BOARD_FIXTURE, show_pad_labels=False)
        assert report["pad_labels"] == 0
        assert report["label_collisions"] == 0
        assert report["label_skipped"] == []

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
        ):
            assert param in sig.parameters
        assert sig.parameters["show_pad_labels"].default is True
        assert sig.parameters["include_pad_coords"].default is False

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