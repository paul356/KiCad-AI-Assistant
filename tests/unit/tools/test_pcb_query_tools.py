"""
Unit tests for kicad_mcp/tools/pcb_query_tools.py
"""
import asyncio
import os

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")


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
    from kicad_mcp.tools.pcb_query_tools import register_pcb_query_tools
    mock = _MockMCP()
    register_pcb_query_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


def _run(coro):
    return asyncio.run(coro)


class TestGetBoardInfo:
    def test_returns_correct_footprint_count(self, tools):
        result = _run(tools["get_board_info"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert result["footprint_count"] == 3

    def test_returns_net_count(self, tools):
        result = _run(tools["get_board_info"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert result["net_count"] == 3  # nets 1-3 (net 0 excluded)

    def test_returns_thickness(self, tools):
        result = _run(tools["get_board_info"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert result["thickness_mm"] == pytest.approx(1.6)

    def test_returns_layers(self, tools):
        result = _run(tools["get_board_info"](pcb_path=BOARD_FIXTURE, ctx=None))
        layer_names = [l["name"] for l in result["all_layers"]]
        assert "F.Cu" in layer_names
        assert "B.Cu" in layer_names

    def test_raises_on_missing_file(self, tools):
        with pytest.raises((FileNotFoundError, ValueError, Exception)):
            _run(tools["get_board_info"](pcb_path="/nonexistent/board.kicad_pcb", ctx=None))


class TestListFootprints:
    def test_returns_all_footprints(self, tools):
        result = _run(tools["list_footprints"](pcb_path=BOARD_FIXTURE, ctx=None))
        refs = {fp["reference"] for fp in result["footprints"]}
        assert refs == {"R1", "C1", "J1"}

    def test_contains_position(self, tools):
        result = _run(tools["list_footprints"](pcb_path=BOARD_FIXTURE, ctx=None))
        r1 = next(fp for fp in result["footprints"] if fp["reference"] == "R1")
        assert r1["x"] == pytest.approx(10.0)
        assert r1["y"] == pytest.approx(20.0)

    def test_contains_rotation(self, tools):
        result = _run(tools["list_footprints"](pcb_path=BOARD_FIXTURE, ctx=None))
        c1 = next(fp for fp in result["footprints"] if fp["reference"] == "C1")
        assert c1["rotation"] == pytest.approx(90.0)

    def test_contains_layer(self, tools):
        result = _run(tools["list_footprints"](pcb_path=BOARD_FIXTURE, ctx=None))
        j1 = next(fp for fp in result["footprints"] if fp["reference"] == "J1")
        assert j1["layer"] == "B.Cu"


class TestGetFootprint:
    def test_returns_footprint_details(self, tools):
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="R1", ctx=None))
        assert result["reference"] == "R1"
        assert result["value"] == "10k"

    def test_returns_pads(self, tools):
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="R1", ctx=None))
        pads = result["pads"]
        assert len(pads) == 2
        pad_nums = {p["number"] for p in pads}
        assert pad_nums == {"1", "2"}

    def test_pad_includes_net(self, tools):
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="R1", ctx=None))
        pad1 = next(p for p in result["pads"] if p["number"] == "1")
        assert pad1["net_name"] == "VCC"

    def test_returns_error_on_missing_reference(self, tools):
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="U99", ctx=None))
        assert "error" in result


class TestListNets:
    def test_excludes_unconnected_net_zero(self, tools):
        result = _run(tools["list_nets"](pcb_path=BOARD_FIXTURE, ctx=None))
        net_ids = {n["net_id"] for n in result["nets"]}
        assert 0 not in net_ids

    def test_includes_named_nets(self, tools):
        result = _run(tools["list_nets"](pcb_path=BOARD_FIXTURE, ctx=None))
        names = {n["name"] for n in result["nets"]}
        assert "VCC" in names
        assert "GND" in names
        assert "NET_A" in names

    def test_returns_three_nets(self, tools):
        result = _run(tools["list_nets"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert result["count"] == 3


class TestGetRatsnest:
    def test_returns_expected_keys(self, tools):
        result = _run(tools["get_ratsnest"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert "unconnected" in result
        assert "unconnected_count" in result
        assert "fully_routed" in result

    def test_board_with_unrouted_pads_reports_them(self, tools):
        # NET_A has C1 pad2 and J1 pad2 — no track connects them
        result = _run(tools["get_ratsnest"](pcb_path=BOARD_FIXTURE, ctx=None))
        net_a_items = [r for r in result["unconnected"] if r["net"] == "NET_A"]
        assert len(net_a_items) > 0
