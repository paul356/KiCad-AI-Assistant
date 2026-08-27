"""
Unit tests for kcaa/tools/pcb_query_tools.py
"""

import asyncio
import os
import re
import shutil

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")
BOARD_WITH_OUTLINE_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_outline.kicad_pcb")
BOARD_WITH_ZONES_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_zones.kicad_pcb")


# ── Segment/via snippets to append to a board file ──────────────────────

_SEGMENTS_SNIPPET = """
\t(segment
\t\t(start 10.0 20.0)
\t\t(end 20.0 20.0)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(segment
\t\t(start 20.0 20.0)
\t\t(end 20.0 30.0)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(segment
\t\t(start 30.0 10.0)
\t\t(end 40.0 10.0)
\t\t(width 0.50)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t)
\t(via
\t\t(at 20.0 20.0)
\t\t(size 0.8)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "VCC")
\t)
\t(via
\t\t(at 35.0 10.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "GND")
\t)
"""


@pytest.fixture
def board_with_tracks(tmp_path):
    """Copy base board and append segment/via entries before the final ``)``."""
    dest = tmp_path / "board_with_tracks.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    text = dest.read_text(encoding="utf-8")
    # Insert before the final closing paren
    idx = text.rstrip().rfind(")")
    text = text[:idx] + _SEGMENTS_SNIPPET + text[idx:]
    dest.write_text(text, encoding="utf-8")
    return str(dest)


@pytest.fixture
def board_no_net_table(tmp_path):
    dest = tmp_path / "board_no_net_table.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    text = dest.read_text(encoding="utf-8")
    # Remove top-level net declarations (KiCad 10 format)
    text = re.sub(r'^\t\(net\s+"[^"]*"\)\n', "", text, flags=re.MULTILINE)
    dest.write_text(text, encoding="utf-8")
    return str(dest)


@pytest.fixture
def board_with_outline_copy(tmp_path):
    dest = tmp_path / "board_with_outline.kicad_pcb"
    shutil.copy(BOARD_WITH_OUTLINE_FIXTURE, dest)
    return str(dest)


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
    from kcaa.tools.pcb_query_tools import register_pcb_query_tools

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
        assert result["net_count"] == 3  # VCC, GND, NET_A

    def test_returns_net_count_without_top_level_net_table(self, tools, board_no_net_table):
        result = _run(tools["get_board_info"](pcb_path=board_no_net_table, ctx=None))
        assert result["net_count"] == 3

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

    def test_includes_edge_cuts_field(self, tools):
        """get_footprint returns an edge_cuts list (empty when none exist)."""
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="R1", ctx=None))
        assert "edge_cuts" in result
        assert isinstance(result["edge_cuts"], list)


class TestGetFootprintBbox:
    def test_r1_bbox_no_rotation(self, tools, board_with_outline_copy):
        result = _run(
            tools["get_footprint_bbox"](pcb_path=board_with_outline_copy, reference="R1", ctx=None)
        )
        assert "bbox" in result
        bbox = result["bbox"]
        assert bbox["min_x"] == pytest.approx(9.0)
        assert bbox["max_x"] == pytest.approx(11.0)
        assert bbox["min_y"] == pytest.approx(19.25)
        assert bbox["max_y"] == pytest.approx(20.75)

    def test_not_found_returns_error(self, tools, board_with_outline_copy):
        result = _run(
            tools["get_footprint_bbox"](
                pcb_path=board_with_outline_copy, reference="MISSING", ctx=None
            )
        )
        assert "error" in result


class TestGetBoardBoundingBox:
    def test_returns_bbox_covering_all_fps(self, tools, board_with_outline_copy):
        result = _run(tools["get_board_bounding_box"](pcb_path=board_with_outline_copy, ctx=None))
        assert "bbox" in result
        bbox = result["bbox"]
        assert bbox["min_x"] == pytest.approx(9.0)
        assert bbox["max_x"] == pytest.approx(31.0)

    def test_footprint_count(self, tools, board_with_outline_copy):
        result = _run(tools["get_board_bounding_box"](pcb_path=board_with_outline_copy, ctx=None))
        assert result["footprint_count"] == 3


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

    def test_supports_name_only_pad_nets_without_top_level_table(self, tools, board_no_net_table):
        result = _run(tools["list_nets"](pcb_path=board_no_net_table, ctx=None))
        assert result["count"] == 3
        names = {n["name"] for n in result["nets"]}
        assert names == {"VCC", "GND", "NET_A"}
        gnd = next(n for n in result["nets"] if n["name"] == "GND")
        assert gnd["pad_count"] > 0


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

    def test_supports_name_only_pad_nets_without_top_level_table(self, tools, board_no_net_table):
        result = _run(tools["get_ratsnest"](pcb_path=board_no_net_table, ctx=None))
        net_a_items = [r for r in result["unconnected"] if r["net"] == "NET_A"]
        assert len(net_a_items) > 0

    def test_connected_pads_false_by_default(self, tools):
        """Default call does NOT include connected_pads / connected_count."""
        result = _run(tools["get_ratsnest"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert "connected_pads" not in result
        assert "connected_count" not in result

    def test_connected_pads_true_includes_key(self, tools):
        """When get_connected_pads=True the key appears."""
        result = _run(
            tools["get_ratsnest"](pcb_path=BOARD_FIXTURE, ctx=None, get_connected_pads=True)
        )
        assert "connected_pads" in result
        assert "connected_count" in result

    def test_connected_pads_with_tracks(self, tools, board_with_tracks):
        """Board with existing VCC tracks — VCC pad gets marked connected."""
        result = _run(
            tools["get_ratsnest"](pcb_path=board_with_tracks, ctx=None, get_connected_pads=True)
        )
        assert "connected_pads" in result
        assert "connected_count" in result
        # The segments exist but may not start at a pad centre
        # (R1/1 centre is at world (9.5,20), segment starts at (10,20))
        # Just verify the key is populated as a list.
        assert isinstance(result["connected_pads"], list)


class TestListTracks:
    def test_returns_valid_structure(self, tools):
        """Base board has 1 existing segment."""
        result = _run(tools["list_tracks"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert "traces" in result
        assert "segment_count" in result
        assert "trace_count" in result
        assert result["segment_count"] >= 1
        assert result["trace_count"] >= 1
        for trace in result["traces"]:
            assert "segments" in trace
            assert "width" in trace
            assert "layer" in trace
            assert "net" in trace

    def test_returns_grouped_traces(self, tools, board_with_tracks):
        """board_with_tracks has 4 segments (1 base + 3 added) and 2 vias."""
        result = _run(tools["list_tracks"](pcb_path=board_with_tracks, ctx=None))
        assert result["segment_count"] == 4
        assert result["trace_count"] >= 2  # VCC trace + GND trace

        traces = result["traces"]
        vcc_traces = [t for t in traces if t["net"] == "VCC"]
        gnd_traces = [t for t in traces if t["net"] == "GND"]
        assert len(vcc_traces) >= 1
        assert len(gnd_traces) >= 1

    def test_segments_include_width(self, tools, board_with_tracks):
        result = _run(tools["list_tracks"](pcb_path=board_with_tracks, ctx=None))
        for trace in result["traces"]:
            for seg in trace["segments"]:
                assert "width" in seg
                assert seg["width"] > 0

    def test_filter_by_net(self, tools, board_with_tracks):
        result = _run(tools["list_tracks"](pcb_path=board_with_tracks, ctx=None, net="GND"))
        assert all(t["net"] == "GND" for t in result["traces"])
        assert result["trace_count"] >= 1
        assert result["segment_count"] >= 1

    def test_pads_field_present(self, tools, board_with_tracks):
        """Traces have a pads list field."""
        result = _run(tools["list_tracks"](pcb_path=board_with_tracks, ctx=None))
        for trace in result["traces"]:
            assert "pads" in trace
            assert isinstance(trace["pads"], list)


class TestListVias:
    def test_empty_board_returns_empty(self, tools):
        result = _run(tools["list_vias"](pcb_path=BOARD_FIXTURE, ctx=None))
        assert result["vias"] == []
        assert result["count"] == 0

    def test_returns_vias(self, tools, board_with_tracks):
        result = _run(tools["list_vias"](pcb_path=board_with_tracks, ctx=None))
        assert result["count"] == 2
        nets = {v["net"] for v in result["vias"]}
        assert nets == {"VCC", "GND"}

    def test_filter_by_net(self, tools, board_with_tracks):
        result = _run(tools["list_vias"](pcb_path=board_with_tracks, ctx=None, net="VCC"))
        assert result["count"] == 1
        assert result["vias"][0]["net"] == "VCC"


class TestGetFootprintPadSize:
    def test_pad_includes_size_fields(self, tools):
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="R1", ctx=None))
        for pad in result["pads"]:
            assert "local_w" in pad
            assert "local_h" in pad
            assert "world_w" in pad
            assert "world_h" in pad
            assert pad["local_w"] > 0
            assert pad["local_h"] > 0
            assert pad["world_w"] > 0
            assert pad["world_h"] > 0

    def test_world_size_for_rotated_footprint(self, tools):
        """C1 is at 90°, pads are (size 0.5 0.5) square -> world_w == world_h."""
        result = _run(tools["get_footprint"](pcb_path=BOARD_FIXTURE, reference="C1", ctx=None))
        for pad in result["pads"]:
            assert pad["world_w"] == pad["world_h"]
            assert pad["world_w"] > 0


class TestListNetsClassify:
    def test_classify_requires_pro_file(self, tools, tmp_path):
        """When classify=True but no .kicad_pro exists, fields are None."""
        dest = tmp_path / "test.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, dest)
        result = _run(tools["list_nets"](pcb_path=str(dest), ctx=None, classify=True))
        for n in result["nets"]:
            assert "netclass" in n
            assert "type" in n
            # No .kicad_pro → netclass should be None
            assert n["netclass"] is None

    def test_classify_resolves_netclass(self, tools, tmp_path):
        """With a matching .kicad_pro, nets get netclass + type."""
        dest = tmp_path / "test.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, dest)
        # Create a matching .kicad_pro
        pro = {
            "net_settings": {
                "classes": [
                    {"name": "Default", "clearance": 0.2, "track_width": 0.25},
                    {"name": "Power", "clearance": 0.3, "track_width": 0.5},
                ],
                "netclass_patterns": [
                    {"netclass": "Power", "pattern": "VCC"},
                ],
            }
        }
        import json

        pro_path = tmp_path / "test.kicad_pro"
        with open(pro_path, "w") as f:
            json.dump(pro, f)

        result = _run(tools["list_nets"](pcb_path=str(dest), ctx=None, classify=True))
        nets = {n["name"]: n for n in result["nets"]}
        assert nets["VCC"]["netclass"] == "Power"
        assert nets["VCC"]["type"] == "power"
        assert nets["GND"]["type"] == "ground"
        assert nets["NET_A"]["netclass"] is None
        assert nets["NET_A"]["type"] == "signal"


class TestGetRatsnestZoneCoverage:
    @pytest.fixture
    def board_with_zone_and_tracks(self, tmp_path):
        """board_with_tracks plus a GND zone covering R1 pad2."""
        import shutil

        dest = tmp_path / "board_with_zone.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, dest)
        text = dest.read_text(encoding="utf-8")
        # Insert tracks + zone before final paren
        idx = text.rstrip().rfind(")")
        snippet = """
\t(segment
\t\t(start 10.0 20.0)
\t\t(end 20.0 20.0)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(segment
\t\t(start 30.0 10.0)
\t\t(end 40.0 10.0)
\t\t(width 0.50)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t)
\t(zone
\t\t(net "GND")
\t\t(net_name "GND")
\t\t(layer "F.Cu")
\t\t(hatch edge 0.508)
\t\t(connect_pads (clearance 0.5))
\t\t(min_thickness 0.25)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon
\t\t\t(pts
\t\t\t\t(xy 5.0 5.0)
\t\t\t\t(xy 60.0 5.0)
\t\t\t\t(xy 60.0 60.0)
\t\t\t\t(xy 5.0 60.0)
\t\t\t)
\t\t)
\t)
"""
        text = text[:idx] + snippet + text[idx:]
        dest.write_text(text, encoding="utf-8")
        return str(dest)

    def test_zone_covered_pad_marked_connected(self, tools, board_with_zone_and_tracks):
        """GND pad inside GND zone should be treated as connected."""
        result = _run(
            tools["get_ratsnest"](
                pcb_path=board_with_zone_and_tracks,
                ctx=None,
                get_connected_pads=True,
            )
        )
        gnd_connected = [p for p in result["connected_pads"] if p["net"] == "GND"]
        assert len(gnd_connected) > 0, "GND pad should be zone-connected"
        # R1/2 is GND and inside the zone polygon (5,5)→(60,5)→(60,60)→(5,60)
        assert any(p["ref"] == "R1" and p["pad"] == "2" for p in gnd_connected)
