"""Unit tests for kcaa/tools/pcb_edit_tools.py."""

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
    from kcaa.tools.pcb_edit_tools import register_pcb_edit_tools

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
        _run(
            tools["add_board_outline_segment"](
                pcb_path=board_copy, x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.05, ctx=None
            )
        )
        after = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))["count"]
        assert after == before + 1

    def test_correct_coordinates_persisted(self, tools, board_copy):
        _run(tools["clear_board_outline"](pcb_path=board_copy, ctx=None))
        _run(
            tools["add_board_outline_segment"](
                pcb_path=board_copy, x1=1.5, y1=2.5, x2=3.5, y2=4.5, width=0.05, ctx=None
            )
        )
        outline = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        seg = outline["items"][0]
        assert seg["x1"] == pytest.approx(1.5)
        assert seg["y2"] == pytest.approx(4.5)

    def test_return_has_added_key(self, tools, board_copy):
        result = _run(
            tools["add_board_outline_segment"](
                pcb_path=board_copy, x1=0.0, y1=0.0, x2=5.0, y2=0.0, width=0.1, ctx=None
            )
        )
        assert "added" in result
        assert result["added"]["type"] == "gr_line"


# ---------------------------------------------------------------------------
# set_board_outline_rect
# ---------------------------------------------------------------------------


class TestSetBoardOutlineRect:
    def test_sharp_corner_uses_gr_rect(self, tools, board_copy):
        result = _run(
            tools["set_board_outline_rect"](
                pcb_path=board_copy,
                x=0.0,
                y=0.0,
                width=60.0,
                height=40.0,
                line_width=0.05,
                corner_radius=0.0,
                ctx=None,
            )
        )
        assert result["items_added"] == 1
        # Verify only one gr_rect on Edge.Cuts
        from kcaa.utils.pcb_sexp_utils import load_pcb
        from kcaa.utils.pcb_board_utils import get_edge_cuts_items

        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 1
        assert items[0]["type"] == "gr_rect"

    def test_rounded_corner_adds_8_items(self, tools, board_copy):
        result = _run(
            tools["set_board_outline_rect"](
                pcb_path=board_copy,
                x=0.0,
                y=0.0,
                width=60.0,
                height=40.0,
                line_width=0.05,
                corner_radius=3.0,
                ctx=None,
            )
        )
        assert result["items_added"] == 8
        from kcaa.utils.pcb_sexp_utils import load_pcb
        from kcaa.utils.pcb_board_utils import get_edge_cuts_items

        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 8

    def test_clears_existing_outline(self, tools, board_copy):
        # Fixture already has 4 lines — after set_board_outline_rect there should be exactly 1 (gr_rect)
        _run(
            tools["set_board_outline_rect"](
                pcb_path=board_copy,
                x=0.0,
                y=0.0,
                width=50.0,
                height=30.0,
                line_width=0.05,
                corner_radius=0.0,
                ctx=None,
            )
        )
        from kcaa.utils.pcb_sexp_utils import load_pcb
        from kcaa.utils.pcb_board_utils import get_edge_cuts_items

        items = get_edge_cuts_items(load_pcb(board_copy))
        assert len(items) == 1

    def test_invalid_corner_radius_rejected(self, tools, board_copy):
        result = _run(
            tools["set_board_outline_rect"](
                pcb_path=board_copy,
                x=0.0,
                y=0.0,
                width=10.0,
                height=10.0,
                line_width=0.05,
                corner_radius=6.0,
                ctx=None,
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# add_board_outline_arc
# ---------------------------------------------------------------------------


class TestAddBoardOutlineArc:
    def test_adds_arc(self, tools, board_copy):
        before = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))["count"]
        _run(
            tools["add_board_outline_arc"](
                pcb_path=board_copy,
                cx=5.0,
                cy=5.0,
                radius=5.0,
                start_angle_deg=180.0,
                end_angle_deg=270.0,
                width=0.05,
                ctx=None,
            )
        )
        outline = _run(tools["get_board_outline"](pcb_path=board_copy, ctx=None))
        assert outline["count"] == before + 1
        arcs = [i for i in outline["items"] if i["type"] == "gr_arc"]
        assert len(arcs) == 1

    def test_arc_result_has_expected_keys(self, tools, board_copy):
        result = _run(
            tools["add_board_outline_arc"](
                pcb_path=board_copy,
                cx=5.0,
                cy=5.0,
                radius=3.0,
                start_angle_deg=0.0,
                end_angle_deg=90.0,
                width=0.05,
                ctx=None,
            )
        )
        assert "added" in result
        assert result["added"]["type"] == "gr_arc"
        assert result["added"]["radius"] == pytest.approx(3.0)


class TestSetFootprintProperty:
    def test_updates_value(self, tools, board_copy):
        result = _run(
            tools["set_footprint_property"](
                pcb_path=board_copy,
                reference="R1",
                property_name="Value",
                value="22k",
                ctx=None,
            )
        )
        assert "error" not in result
        from kcaa.utils.pcb_sexp_utils import load_pcb
        from kcaa.utils.pcb_footprint_utils import find_footprint, get_fp_property

        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        assert get_fp_property(fp, "Value") == "22k"

    def test_creates_backup(self, tools, board_copy):
        result = _run(
            tools["set_footprint_property"](
                pcb_path=board_copy, reference="R1", property_name="Value", value="1k", ctx=None
            )
        )
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(
            tools["set_footprint_property"](
                pcb_path=board_copy, reference="U99", property_name="Value", value="x", ctx=None
            )
        )
        assert "error" in result

    def test_error_on_missing_property(self, tools, board_copy):
        result = _run(
            tools["set_footprint_property"](
                pcb_path=board_copy,
                reference="R1",
                property_name="NoSuchProp",
                value="x",
                ctx=None,
            )
        )
        assert "error" in result
