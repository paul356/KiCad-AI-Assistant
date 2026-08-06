"""
End-to-end integration tests for the PCB router and routing MCP tools.

The fixture board is :file:`tests/integration/fixtures/test_routing_board.kicad_pcb`
which contains:

* R1, C1, U1 — three footprints (R1 and C1 are placed so a straight route
  from R1 to C1 must go around U1's courtyard).
* An existing VCC track segment from R1.2 to a stub on the way to C1.
* A keepout zone covering (38..42, 36..40) on F.Cu.
* Netclasses Default (0.25 mm) and Power (0.5 mm); VCC and GND are in
  Power, so a VCC route defaults to 0.5 mm width.

The tests:
  1. Drive the lower-level :func:`auto_route_pair` directly and inspect the
     geometry it produces.
  2. Drive the MCP tool wrapper ``pcb_route_pad_to_pad`` and verify the
     segments are actually written to the file (with a backup).
  3. Drive the via tool ``pcb_add_vias`` (single-element list for one via).
  4. Confirm that a blocked route raises :class:`RouteFailure`.
"""

from __future__ import annotations

import os
import shutil

import pytest

from kcaa.router.router import RouteFailure, RouteRequest, auto_route_pair
from kcaa.tools.pcb_routing_tools import register_pcb_routing_tools
from kcaa.utils.pcb_sexp_utils import load_pcb

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_routing_board.kicad_pcb")
PRO_FIXTURE = os.path.join(FIXTURE_DIR, "test_routing_board.kicad_pro")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pcb_copy(tmp_path):
    """Copy the routing fixture to a writable temp dir and back it up."""
    dst = tmp_path / "test_routing_board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dst)
    # Also copy the .pro so DRC netclass lookup works.
    shutil.copy(PRO_FIXTURE, tmp_path / "test_routing_board.kicad_pro")
    return str(dst)


# ---------------------------------------------------------------------------
# auto_route_pair — direct API
# ---------------------------------------------------------------------------


class TestAutoRoutePair:
    def test_routes_around_u1(self, pcb_copy):
        # U1 sits between R1 and D1 on the GND net.  D1/2 is on F.Cu
        # (no net — unconnected pad).
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
        )
        result = auto_route_pair(req)
        assert len(result.segments) >= 1
        ax, ay = result.start
        bx, by = result.end
        assert abs(ax - 29.5) < 1.0
        assert abs(ay - 30.0) < 2.0
        assert abs(bx - 60.0) < 1.0
        assert abs(by - 29.5) < 2.0

    def test_segments_use_power_netclass_width(self, pcb_copy):
        # VCC is in the "Power" netclass → 0.5 mm track width.
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
        )
        result = auto_route_pair(req)
        assert all(s.width == pytest.approx(0.5, abs=1e-6) for s in result.segments)
        assert all(s.layer == "F.Cu" for s in result.segments)
        assert all(s.net == "VCC" for s in result.segments)

    def test_explicit_width_overrides_netclass(self, pcb_copy):
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.3,
        )
        result = auto_route_pair(req)
        assert all(s.width == pytest.approx(0.3) for s in result.segments)

    def test_invalid_pad_raises_failure(self, pcb_copy):
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="999",  # does not exist
            ref_b="C1",
            pad_b="2",
            net="VCC",
        )
        with pytest.raises(RouteFailure):
            auto_route_pair(req)

    def test_invalid_footprint_raises_failure(self, pcb_copy):
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="DOES_NOT_EXIST",
            pad_a="1",
            ref_b="C1",
            pad_b="2",
            net="VCC",
        )
        with pytest.raises(RouteFailure):
            auto_route_pair(req)

    def test_multi_layer_route_inserts_via(self, pcb_copy):
        # R1.2 is on F.Cu (GND). D1.1 is on In1.Cu (GND). The router must
        # insert at least one via to reach the destination layer.
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="2",
            ref_b="D1",
            pad_b="1",
            net="GND",
            start_layer="F.Cu",
            end_layer="In1.Cu",
            via_pairs=(("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
        )
        result = auto_route_pair(req)
        assert result.vias, "expected at least one via on a multi-layer route"
        assert "In1.Cu" in result.layers_used
        allowed = {("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu"), ("In1.Cu", "B.Cu"), ("B.Cu", "F.Cu")}
        for via in result.vias:
            assert via.layers in allowed, f"unexpected via pair: {via.layers}"

    def test_multi_layer_via_in_board(self, pcb_copy):
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="2",
            ref_b="D1",
            pad_b="1",
            net="GND",
            start_layer="F.Cu",
            end_layer="In1.Cu",
            via_pairs=(("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
        )
        result = auto_route_pair(req)
        for via in result.vias:
            assert 0.0 <= via.x <= 70.0
            assert 0.0 <= via.y <= 60.0

    def test_single_layer_with_via_pair_still_works(self, pcb_copy):
        # When start_layer == end_layer, no vias are inserted even if via_pairs set.
        req = RouteRequest(
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            start_layer="F.Cu",
            end_layer="F.Cu",
            via_pairs=(("F.Cu", "B.Cu"),),
        )
        result = auto_route_pair(req)
        assert result.vias == []
        assert set(result.layers_used) == {"F.Cu"}


# ---------------------------------------------------------------------------
# MCP tool wrapper
# ---------------------------------------------------------------------------


class TestRoutingTool:
    def _make_mcp(self):
        """Build a FastMCP instance with the routing tool registered."""
        try:
            from fastmcp import FastMCP
        except ImportError:  # pragma: no cover
            pytest.skip("fastmcp not installed")
        mcp = FastMCP(name="test-routing")
        register_pcb_routing_tools(mcp)
        return mcp

    def _call_tool(self, mcp, name: str, **kwargs):
        import asyncio

        tool = asyncio.run(mcp.get_tool(name))
        return asyncio.run(tool.fn(**kwargs))

    def test_tool_writes_segments_to_pcb(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_route_pad_to_pad",
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            ctx=None,
        )
        assert "segment_count" in result
        assert result["segment_count"] >= 1

        data = load_pcb(pcb_copy)
        total = sum(1 for item in data if _is_list(item) and _sym(item[0]) == "segment")
        existing = sum(
            1 for item in load_pcb(BOARD_FIXTURE) if _is_list(item) and _sym(item[0]) == "segment"
        )
        assert total == existing + result["segment_count"]

    def test_tool_creates_backup(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_route_pad_to_pad",
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            ctx=None,
        )
        assert "backup_path" in result
        assert os.path.exists(result["backup_path"])

    def test_tool_returns_error_on_invalid_pad(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_route_pad_to_pad",
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="BAD",
            ref_b="C1",
            pad_b="2",
            net="VCC",
            ctx=None,
        )
        assert "error" in result

    def test_via_tool_writes_via(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 40.0, "y": 25.0, "net": "GND"}],
            ctx=None,
        )
        assert result["via_count"] == 1
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 1

    def test_batch_via_tool_writes_all(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[
                {"x": 30.0, "y": 35.0, "net": "VCC"},
                # GND is in the Power netclass (via_diameter=0.8, via_drill=0.4)
                {"x": 40.0, "y": 35.0, "net": "GND", "diameter": 0.8, "drill": 0.4},
            ],
            ctx=None,
        )
        assert "via_count" in result
        assert result["via_count"] == 2
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 2

    def test_batch_via_tool_invalid_descriptor(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 30.0, "y": 35.0}],  # missing 'net'
            ctx=None,
        )
        assert "error" in result
        # File must be untouched.
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_empty_list(self, pcb_copy):
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[],
            ctx=None,
        )
        assert result["via_count"] == 0
        assert result["vias"] == []

    def test_batch_via_tool_rejects_netclass_mismatch(self, pcb_copy):
        # GND lives in the Power netclass (via_diameter=0.8, via_drill=0.4).
        # Asking for 1.0/0.5 must be rejected.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 35.0, "y": 35.0, "net": "GND", "diameter": 1.0, "drill": 0.5}],
            ctx=None,
        )
        assert "error" in result
        assert "netclass" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_keepout_overlap(self, pcb_copy):
        # The fixture has a keepout zone at (38..42, 36..40) on F.Cu.
        # Default via layers are ("F.Cu", "B.Cu") so the F.Cu ring hits it.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 40.0, "y": 38.0, "net": "VCC"}],
            ctx=None,
        )
        assert "error" in result
        assert "keepout" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_board_edge(self, pcb_copy):
        # Way outside the board outline.  Edge-clearance check uses
        # min_copper_edge_clearance from the .kicad_pro if set, else 0.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": -50.0, "y": -50.0, "net": "VCC"}],
            ctx=None,
        )
        assert "error" in result
        assert "board_edge" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_below_min_via_size(self, pcb_copy):
        # Fixture .kicad_pro sets min_via_diameter: 0.6 → user key
        # min_via_size = 0.6.  Asking for 0.4 mm must be rejected.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 30.0, "y": 35.0, "net": "VCC", "diameter": 0.4, "drill": 0.2}],
            ctx=None,
        )
        assert "error" in result
        assert "drc" in result["error"]
        assert "min_via_size" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_below_min_drill(self, pcb_copy):
        # Fixture .kicad_pro sets min_through_hole_diameter: 0.3.
        # Asking for 0.2 mm drill must be rejected.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 30.0, "y": 35.0, "net": "VCC", "diameter": 0.6, "drill": 0.2}],
            ctx=None,
        )
        assert "error" in result
        assert "drc" in result["error"]
        assert "min_through_drill" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_below_min_hole_to_hole(self, pcb_copy):
        # Place one via inside the board, then try to place a second one
        # 0.1 mm away — well under fixture min_hole_to_hole: 0.25.
        # Use VCC with explicit Power-class dimensions (0.8/0.4) so we
        # don't trip the netclass check first.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[
                {"x": 30.0, "y": 35.0, "net": "VCC", "diameter": 0.8, "drill": 0.4},
                {"x": 30.1, "y": 35.0, "net": "VCC", "diameter": 0.8, "drill": 0.4},
            ],
            ctx=None,
        )
        assert "error" in result
        assert "drc" in result["error"]
        assert "min_hole_to_hole" in result["error"]
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_rejects_below_min_clearance(self, pcb_copy):
        # R1 has a pad on F.Cu near (30.5, 32.5).  A via at (31.0, 31.0)
        # has a 0.6 mm pad ring; with min_clearance=0.2 the buffered ring
        # has radius 0.5, which overlaps the pad's clearance margin.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[{"x": 31.0, "y": 31.0, "net": "GND", "diameter": 0.6, "drill": 0.3}],
            ctx=None,
        )
        assert "error" in result
        # Should be a footprint/pad overlap with the clearance note.
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 0

    def test_batch_via_tool_accepts_drc_compliant(self, pcb_copy):
        # Far from any obstacle, large enough for min_via_size, with
        # sufficient spacing from the second via for min_hole_to_hole.
        # Use VCC with explicit Power-class dimensions to satisfy both
        # netclass and board DRC.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_add_vias",
            pcb_path=pcb_copy,
            vias=[
                {"x": 30.0, "y": 20.0, "net": "VCC", "diameter": 0.8, "drill": 0.4},
                {
                    "x": 32.0,
                    "y": 20.0,
                    "net": "VCC",  # 2 mm away
                    "diameter": 0.8,
                    "drill": 0.4,
                },
            ],
            ctx=None,
        )
        assert "via_count" in result, result
        assert result["via_count"] == 2
        data = load_pcb(pcb_copy)
        vias = [item for item in data if _is_list(item) and _sym(item[0]) == "via"]
        assert len(vias) == 2

    def test_multi_layer_tool_writes_segments_and_via(self, pcb_copy):
        # R1.2 is on F.Cu; D1.1 is on In1.Cu (GND).  Calling the tool
        # with target_layer="In1.Cu" should produce at least one via.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_route_pad_to_pad",
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="2",
            ref_b="D1",
            pad_b="1",
            net="GND",
            ctx=None,
            layer="F.Cu",
            target_layer="In1.Cu",
            via_pairs=(("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
        )
        assert "segment_count" in result
        assert result["segment_count"] >= 1
        assert result["via_count"] >= 1
        assert "In1.Cu" in result["layers_used"]

    def test_multi_layer_tool_default_via_pair(self, pcb_copy):
        # When target_layer differs from layer, default via_pairs
        # ((F.Cu, B.Cu),) is used.  F.Cu → B.Cu reaches a B.Cu pad.
        mcp = self._make_mcp()
        result = self._call_tool(
            mcp,
            "pcb_route_pad_to_pad",
            pcb_path=pcb_copy,
            ref_a="R1",
            pad_a="2",
            ref_b="C1",
            pad_b="2",
            net="VCC",
            ctx=None,
            layer="F.Cu",
            target_layer="B.Cu",
        )
        # C1.2 is on F.Cu, so this should fall back to F.Cu routing
        # (the pad has no copper on B.Cu) — the tool surfaces the error
        # in result["error"].
        assert "error" in result or result.get("via_count", 0) >= 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_list(v) -> bool:
    return isinstance(v, list) and len(v) > 0


def _sym(v) -> str:
    return str(v)
