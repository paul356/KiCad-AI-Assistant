"""
Unit tests for kicad_mcp/tools/pcb_placement_tools.py
"""
import asyncio
import os
import shutil

import pytest

from kicad_mcp.utils.pcb_sexp_utils import load_pcb
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _get_tools() -> dict:
    from kicad_mcp.tools.pcb_placement_tools import register_pcb_placement_tools
    mock = _MockMCP()
    register_pcb_placement_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def board_copy(tmp_path):
    """Return path to a temporary copy of the test board."""
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    return str(dest)


def _run(coro):
    return asyncio.run(coro)


class TestSetFootprintPosition:
    def test_moves_xy(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=99.0, y=88.0, rotation=None, ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, _ = get_fp_at(fp)
        assert x == pytest.approx(99.0)
        assert y == pytest.approx(88.0)

    def test_updates_rotation(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=None, y=None, rotation=45.0, ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        _, _, rot = get_fp_at(fp)
        assert rot == pytest.approx(45.0)

    def test_partial_update_preserves_unchanged(self, tools, board_copy):
        original_data = load_pcb(board_copy)
        original_fp = find_footprint(original_data, "R1")
        _, orig_y, orig_rot = get_fp_at(original_fp)

        _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=5.0, y=None, rotation=None, ctx=None
        ))
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, rot = get_fp_at(fp)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(orig_y)
        assert rot == pytest.approx(orig_rot)

    def test_creates_backup(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=1.0, y=2.0, rotation=None, ctx=None
        ))
        assert "error" not in result
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="U99", x=1.0, y=None, rotation=None, ctx=None
        ))
        assert "error" in result
        assert "U99" in result["error"]

    def test_error_when_no_coords_given(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=None, y=None, rotation=None, ctx=None
        ))
        assert "error" in result


class TestFlipFootprint:
    def test_flips_f_to_b(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="R1", ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        assert get_fp_layer(fp) == "B.Cu"

    def test_flips_b_to_f(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="J1", ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "J1")
        assert get_fp_layer(fp) == "F.Cu"

    def test_double_flip_restores_layer(self, tools, board_copy):
        original_layer = get_fp_layer(find_footprint(load_pcb(board_copy), "R1"))
        _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        final_layer = get_fp_layer(find_footprint(load_pcb(board_copy), "R1"))
        assert final_layer == original_layer

    def test_creates_backup(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="R1", ctx=None
        ))
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="U99", ctx=None
        ))
        assert "error" in result


class TestSetFootprintProperty:
    def test_updates_value(self, tools, board_copy):
        result = _run(tools["set_footprint_property"](
            pcb_path=board_copy, reference="R1", property_name="Value", value="22k", ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        assert get_fp_property(fp, "Value") == "22k"

    def test_creates_backup(self, tools, board_copy):
        result = _run(tools["set_footprint_property"](
            pcb_path=board_copy, reference="R1", property_name="Value", value="1k", ctx=None
        ))
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["set_footprint_property"](
            pcb_path=board_copy, reference="U99", property_name="Value", value="x", ctx=None
        ))
        assert "error" in result

    def test_error_on_missing_property(self, tools, board_copy):
        result = _run(tools["set_footprint_property"](
            pcb_path=board_copy, reference="R1", property_name="NoSuchProp", value="x", ctx=None
        ))
        assert "error" in result
