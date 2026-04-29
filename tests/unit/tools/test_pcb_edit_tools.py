"""
Unit tests for kicad_mcp/tools/pcb_edit_tools.py
"""
import asyncio
import os
import shutil
import tempfile

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_outline.kicad_pcb")


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _get_tools() -> dict:
    from kicad_mcp.tools.pcb_edit_tools import register_pcb_edit_tools
    mock = _MockMCP()
    register_pcb_edit_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def board_copy(tmp_path):
    """Copy the fixture PCB to a temp file so each test gets a clean slate."""
    dest = tmp_path / "test_board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    return str(dest)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_board_outline
# ---------------------------------------------------------------------------

class TestGetBoardOutline:
    def test_returns_four_items(self, tools, board_copy):
        result = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        assert "items" in result
        assert result["count"] == 4

    def test_all_gr_line(self, tools, board_copy):
        result = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        assert all(i["type"] == "gr_line" for i in result["items"])

    def test_items_have_coordinates(self, tools, board_copy):
        result = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        for item in result["items"]:
            for key in ("x1", "y1", "x2", "y2"):
                assert key in item


# ---------------------------------------------------------------------------
# clear_board_outline
# ---------------------------------------------------------------------------

class TestClearBoardOutline:
    def test_removes_all_edge_cuts(self, tools, board_copy):
        result = _run(tools["clear_board_outline"](pcb_path=board_copy, ctx=None))
        assert result["removed_count"] == 4
        # Verify file was written
        assert os.path.exists(result["backup_path"])

    def test_outline_empty_after_clear(self, tools, board_copy):
        _run(tools["clear_board_outline"](pcb_path=board_copy, ctx=None))
        result = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        assert result["count"] == 0

    def test_backup_created(self, tools, board_copy):
        result = _run(tools["clear_board_outline"](pcb_path=board_copy, ctx=None))
        assert os.path.exists(board_copy + ".bak")


# ---------------------------------------------------------------------------
# add_board_outline_segment
# ---------------------------------------------------------------------------

class TestAddBoardOutlineSegment:
    def test_adds_one_line(self, tools, board_copy):
        before = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))["count"]
        _run(tools["add_board_outline_segment"](
            pcb_path=board_copy, x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.05, ctx=None))
        after = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))["count"]
        assert after == before + 1

    def test_correct_coordinates_persisted(self, tools, board_copy):
        _run(tools["clear_board_outline"](pcb_path=board_copy, ctx=None))
        _run(tools["add_board_outline_segment"](
            pcb_path=board_copy, x1=1.5, y1=2.5, x2=3.5, y2=4.5, width=0.05, ctx=None))
        outline = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        seg = outline["items"][0]
        assert seg["x1"] == pytest.approx(1.5)
        assert seg["y2"] == pytest.approx(4.5)

    def test_return_has_added_key(self, tools, board_copy):
        result = _run(tools["add_board_outline_segment"](
            pcb_path=board_copy, x1=0.0, y1=0.0, x2=5.0, y2=0.0, width=0.1, ctx=None))
        assert "added" in result
        assert result["added"]["type"] == "gr_line"


# ---------------------------------------------------------------------------
# set_board_outline_rect
# ---------------------------------------------------------------------------

class TestSetBoardOutlineRect:
    def test_sharp_corner_uses_gr_rect(self, tools, board_copy):
        result = _run(tools["set_board_outline_rect"](
            pcb_path=board_copy, x=0.0, y=0.0, width=60.0, height=40.0,
            line_width=0.05, corner_radius=0.0, ctx=None))
        assert result["items_added"] == 1
        # Verify only one gr_rect on Edge.Cuts
        from kicad_mcp.utils.pcb_sexp_utils import load_pcb
        from kicad_mcp.utils.pcb_board_utils import get_edge_cuts_items
        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 1
        assert items[0]["type"] == "gr_rect"

    def test_rounded_corner_adds_8_items(self, tools, board_copy):
        result = _run(tools["set_board_outline_rect"](
            pcb_path=board_copy, x=0.0, y=0.0, width=60.0, height=40.0,
            line_width=0.05, corner_radius=3.0, ctx=None))
        assert result["items_added"] == 8
        from kicad_mcp.utils.pcb_sexp_utils import load_pcb
        from kicad_mcp.utils.pcb_board_utils import get_edge_cuts_items
        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 8

    def test_clears_existing_outline(self, tools, board_copy):
        # Fixture already has 4 lines — after set_board_outline_rect there should be exactly 1 (gr_rect)
        _run(tools["set_board_outline_rect"](
            pcb_path=board_copy, x=0.0, y=0.0, width=50.0, height=30.0,
            line_width=0.05, corner_radius=0.0, ctx=None))
        from kicad_mcp.utils.pcb_sexp_utils import load_pcb
        from kicad_mcp.utils.pcb_board_utils import get_edge_cuts_items
        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 1

    def test_invalid_corner_radius_rejected(self, tools, board_copy):
        result = _run(tools["set_board_outline_rect"](
            pcb_path=board_copy, x=0.0, y=0.0, width=10.0, height=10.0,
            line_width=0.05, corner_radius=6.0, ctx=None))
        assert "error" in result


# ---------------------------------------------------------------------------
# add_board_outline_arc
# ---------------------------------------------------------------------------

class TestAddBoardOutlineArc:
    def test_adds_arc(self, tools, board_copy):
        before = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))["count"]
        _run(tools["add_board_outline_arc"](
            pcb_path=board_copy, cx=5.0, cy=5.0, radius=5.0,
            start_angle_deg=180.0, end_angle_deg=270.0, width=0.05, ctx=None))
        outline = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        assert outline["count"] == before + 1
        arcs = [i for i in outline["items"] if i["type"] == "gr_arc"]
        assert len(arcs) == 1

    def test_arc_result_has_expected_keys(self, tools, board_copy):
        result = _run(tools["add_board_outline_arc"](
            pcb_path=board_copy, cx=5.0, cy=5.0, radius=3.0,
            start_angle_deg=0.0, end_angle_deg=90.0, width=0.05, ctx=None))
        assert "added" in result
        assert result["added"]["type"] == "gr_arc"
        assert result["added"]["radius"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# get_footprint_bbox
# ---------------------------------------------------------------------------

class TestGetFootprintBbox:
    def test_r1_bbox_no_rotation(self, tools, board_copy):
        """R1 is at (10, 20), courtyard ±1 × ±0.75 — no rotation."""
        result = _run(tools["get_footprint_bbox"](pcb_path=board_copy, reference="R1", ctx=None))
        assert "bbox" in result
        bbox = result["bbox"]
        assert bbox["min_x"] == pytest.approx(9.0)
        assert bbox["max_x"] == pytest.approx(11.0)
        assert bbox["min_y"] == pytest.approx(19.25)
        assert bbox["max_y"] == pytest.approx(20.75)

    def test_not_found_returns_error(self, tools, board_copy):
        result = _run(tools["get_footprint_bbox"](pcb_path=board_copy, reference="MISSING", ctx=None))
        assert "error" in result


# ---------------------------------------------------------------------------
# get_board_bounding_box
# ---------------------------------------------------------------------------

class TestGetBoardBoundingBox:
    def test_returns_bbox_covering_all_fps(self, tools, board_copy):
        result = _run(tools["get_board_bounding_box"](pcb_path=board_copy, ctx=None))
        assert "bbox" in result
        bbox = result["bbox"]
        # R1 at x=10, R2 at x=30, R3 at x=20 — leftmost courtyard edge 10-1=9, rightmost 30+1=31
        assert bbox["min_x"] == pytest.approx(9.0)
        assert bbox["max_x"] == pytest.approx(31.0)

    def test_footprint_count(self, tools, board_copy):
        result = _run(tools["get_board_bounding_box"](pcb_path=board_copy, ctx=None))
        assert result["footprint_count"] == 3


# ---------------------------------------------------------------------------
# align_footprints
# ---------------------------------------------------------------------------

class TestAlignFootprints:
    def test_align_y_to_mean(self, tools, board_copy):
        # All three resistors are at y=20, so mean=20
        result = _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1", "R2", "R3"],
            axis="y", coordinate=None, ctx=None))
        assert "error" not in result
        assert result["target_coordinate"] == pytest.approx(20.0)

    def test_align_y_to_explicit_coordinate(self, tools, board_copy):
        result = _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1", "R2"],
            axis="y", coordinate=25.0, ctx=None))
        assert "error" not in result
        for entry in result["aligned"]:
            assert entry["new_y"] == pytest.approx(25.0)

    def test_align_x_to_explicit(self, tools, board_copy):
        result = _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1", "R3"],
            axis="x", coordinate=15.0, ctx=None))
        assert "error" not in result
        for entry in result["aligned"]:
            assert entry["new_x"] == pytest.approx(15.0)

    def test_not_found_listed(self, tools, board_copy):
        result = _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1", "NOTHERE"],
            axis="y", coordinate=20.0, ctx=None))
        assert "NOTHERE" in result["not_found"]

    def test_invalid_axis_rejected(self, tools, board_copy):
        result = _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1"], axis="z",
            coordinate=None, ctx=None))
        assert "error" in result

    def test_backup_created(self, tools, board_copy):
        _run(tools["align_footprints"](
            pcb_path=board_copy, references=["R1", "R2"],
            axis="y", coordinate=20.0, ctx=None))
        assert os.path.exists(board_copy + ".bak")


# ---------------------------------------------------------------------------
# distribute_footprints
# ---------------------------------------------------------------------------

class TestDistributeFootprints:
    def test_three_footprints_evenly_spaced(self, tools, board_copy):
        # R1=10, R3=20, R2=30 — distribute along x → spacing=10
        result = _run(tools["distribute_footprints"](
            pcb_path=board_copy, references=["R1", "R2", "R3"],
            axis="x", ctx=None))
        assert "error" not in result
        assert result["spacing_mm"] == pytest.approx(10.0)
        xs = sorted(e["new_x"] for e in result["distributed"])
        assert xs[0] == pytest.approx(10.0)
        assert xs[1] == pytest.approx(20.0)
        assert xs[2] == pytest.approx(30.0)

    def test_two_refs_unchanged(self, tools, board_copy):
        result = _run(tools["distribute_footprints"](
            pcb_path=board_copy, references=["R1", "R2"],
            axis="x", ctx=None))
        assert "error" not in result
        # spacing = 30 - 10 = 20; only 2 points, both at extremes
        assert result["spacing_mm"] == pytest.approx(20.0)

    def test_invalid_axis_rejected(self, tools, board_copy):
        result = _run(tools["distribute_footprints"](
            pcb_path=board_copy, references=["R1", "R2"],
            axis="z", ctx=None))
        assert "error" in result

    def test_too_few_refs_rejected(self, tools, board_copy):
        result = _run(tools["distribute_footprints"](
            pcb_path=board_copy, references=["R1"],
            axis="x", ctx=None))
        assert "error" in result


# ---------------------------------------------------------------------------
# move_footprints_by_delta
# ---------------------------------------------------------------------------

class TestMoveFootprintsByDelta:
    def test_moves_by_delta(self, tools, board_copy):
        result = _run(tools["move_footprints_by_delta"](
            pcb_path=board_copy, references=["R1", "R2"],
            dx=5.0, dy=-3.0, ctx=None))
        assert "error" not in result
        moves = {e["reference"]: e for e in result["moved"]}
        assert moves["R1"]["new_x"] == pytest.approx(15.0)   # 10 + 5
        assert moves["R1"]["new_y"] == pytest.approx(17.0)   # 20 - 3
        assert moves["R2"]["new_x"] == pytest.approx(35.0)   # 30 + 5

    def test_zero_delta_rejected(self, tools, board_copy):
        result = _run(tools["move_footprints_by_delta"](
            pcb_path=board_copy, references=["R1"],
            dx=0.0, dy=0.0, ctx=None))
        assert "error" in result

    def test_empty_refs_rejected(self, tools, board_copy):
        result = _run(tools["move_footprints_by_delta"](
            pcb_path=board_copy, references=[],
            dx=1.0, dy=0.0, ctx=None))
        assert "error" in result

    def test_not_found_listed(self, tools, board_copy):
        result = _run(tools["move_footprints_by_delta"](
            pcb_path=board_copy, references=["R1", "MISSING"],
            dx=1.0, dy=0.0, ctx=None))
        assert "MISSING" in result["not_found"]
        # R1 should still have moved
        assert len(result["moved"]) == 1

    def test_backup_created(self, tools, board_copy):
        _run(tools["move_footprints_by_delta"](
            pcb_path=board_copy, references=["R1"],
            dx=0.0, dy=1.0, ctx=None))
        assert os.path.exists(board_copy + ".bak")
