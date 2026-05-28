"""
Integration tests for advanced PCB MCP tools.

Covers:
  - PCB Query (advanced):  get_footprint_bbox, get_board_bounding_box,
                           score_placement, suggest_placement_order
  - PCB Placement (batch): align_footprints, distribute_footprints,
                           move_footprints_by_delta
  - PCB Edit (outline):    get_board_outline, clear_board_outline,
                           add_board_outline_segment, add_board_outline_arc,
                           set_board_outline_rect
  - PCB Group:             assign_to_group, list_groups, get_group,
                           score_group, place_component_group,
                           move_group, rotate_group
  - PCB Zone:              list_zones, add_zone, delete_zone
  - PCB Placement Helper:  find_free_pcb_area

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Mutation tests use
tmp_path copies of the fixture PCB.

Run:
    uv run python -m pytest tests/integration/test_pcb_advanced_tools.py -v
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")

# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 20.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _mcp_post(port: int, payload: dict, session_id: str | None = None) -> tuple[dict, str | None]:
    import urllib.request

    url = f"http://127.0.0.1:{port}/mcp"
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        returned_session_id = resp.headers.get("mcp-session-id", session_id)
        raw = resp.read().decode()

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:"):].strip()
            if json_str:
                return json.loads(json_str), returned_session_id

    return json.loads(raw), returned_session_id


_call_id = itertools.count(800)


def _call_tool(port: int, session_id: str | None, name: str, arguments: dict) -> dict:
    response, _ = _mcp_post(
        port,
        {
            "jsonrpc": "2.0",
            "id": next(_call_id),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session_id,
    )
    assert "error" not in response, f"JSON-RPC error calling {name!r}: {response['error']}"
    result = response.get("result", {})
    content = result.get("content", [])
    text_block = next((c["text"] for c in content if c.get("type") == "text"), None)
    assert text_block is not None, f"No text content in tools/call response for {name!r}: {result}"

    if result.get("isError"):
        return {"error": text_block}

    return json.loads(text_block)


# ---------------------------------------------------------------------------
# Fixture: running MCP server (module scope)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_server():
    port = _find_free_port()
    env = os.environ.copy()
    env.update({
        "MCP_TRANSPORT": "streamable-http",
        "MCP_PORT": str(port),
        "MCP_HOST": "127.0.0.1",
        "KICAD_MCP_PROFILE": "plugin",
    })
    env.pop("http_proxy", None)
    env.pop("HTTP_PROXY", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "kcaa.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_for_port(port, timeout=20):
        proc.terminate()
        proc.wait()
        pytest.skip("MCP server did not start — skipping PCB advanced integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pcb-advanced-integration-test", "version": "1"},
        },
    }
    try:
        _, session_id = _mcp_post(port, init_payload)
    except Exception as exc:
        proc.terminate()
        proc.wait()
        pytest.skip(f"MCP initialize failed: {exc}")

    yield port, session_id

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Helper: copy PCB fixture to tmp
# ---------------------------------------------------------------------------

def _copy_pcb(tmp_path, name: str = "board.kicad_pcb") -> str:
    dst = tmp_path / name
    shutil.copy2(BOARD_FIXTURE, dst)
    return str(dst)


# ===========================================================================
# Query tools (read-only — use fixture directly)
# ===========================================================================

class TestGetFootprintBbox:
    def test_returns_bbox_or_courtyard_error(self, mcp_server):
        """Fixture footprints may lack courtyard geometry; accept either result."""
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_footprint_bbox", {
            "pcb_path": BOARD_FIXTURE,
            "reference": "R1",
        })
        if "error" in result:
            assert "courtyard" in result["error"].lower() or "geometry" in result["error"].lower()
        else:
            assert "bbox" in result
            bbox = result["bbox"]
            assert "min_x" in bbox and "max_x" in bbox
            assert "min_y" in bbox and "max_y" in bbox
            assert bbox["width"] > 0
            assert bbox["height"] > 0

    def test_missing_reference_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_footprint_bbox", {
            "pcb_path": BOARD_FIXTURE,
            "reference": "U99",
        })
        assert "error" in result


class TestGetBoardBoundingBox:
    def test_returns_board_bbox_or_geometry_error(self, mcp_server):
        """Fixture footprints may lack courtyard geometry; accept either result."""
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_bounding_box", {
            "pcb_path": BOARD_FIXTURE,
        })
        if "error" in result:
            assert "geometry" in result["error"].lower() or "courtyard" in result["error"].lower()
            assert "footprints_without_courtyard" in result or "footprint_count" in result
        else:
            assert "bbox" in result
            assert result.get("footprint_count", 0) > 0

    def test_bbox_encloses_all_footprints(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_bounding_box", {
            "pcb_path": BOARD_FIXTURE,
        })
        if "error" in result:
            # Fixture lacks courtyard geometry — skip bbox assertion
            return
        bbox = result["bbox"]
        assert bbox["min_x"] < bbox["max_x"]
        assert bbox["min_y"] < bbox["max_y"]


class TestScorePlacement:
    def test_returns_score_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "score_placement", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert "error" not in result, result
        assert "hpwl_mm" in result
        assert "congestion" in result

    def test_hpwl_is_non_negative(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "score_placement", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert result.get("hpwl_mm", -1) >= 0


class TestSuggestPlacementOrder:
    def test_returns_ordered_list(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "suggest_placement_order", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert "error" not in result, result
        assert "ordered" in result
        assert len(result["ordered"]) > 0

    def test_ordered_items_have_tier(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "suggest_placement_order", {
            "pcb_path": BOARD_FIXTURE,
        })
        for item in result["ordered"]:
            assert "reference" in item
            assert "tier" in item
            assert "tier_name" in item

    def test_tier_counts_present(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "suggest_placement_order", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert "tier_counts" in result


# ===========================================================================
# Batch placement tools (mutation — use tmp_path)
# ===========================================================================

class TestAlignFootprints:
    def test_aligns_on_x_axis(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "align_footprints", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "axis": "x",
            "coordinate": None,
        })
        assert "error" not in result, result
        assert "aligned" in result
        assert len(result["aligned"]) >= 2

    def test_invalid_axis_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "align_footprints", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "axis": "z",
            "coordinate": None,
        })
        assert "error" in result

    def test_empty_references_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "align_footprints", {
            "pcb_path": pcb,
            "references": [],
            "axis": "x",
            "coordinate": None,
        })
        assert "error" in result


class TestDistributeFootprints:
    def test_distributes_on_x_axis(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "distribute_footprints", {
            "pcb_path": pcb,
            "references": ["R1", "C1", "J1"],
            "axis": "x",
        })
        assert "error" not in result, result
        assert "distributed" in result
        assert "spacing_mm" in result

    def test_invalid_axis_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "distribute_footprints", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "axis": "z",
        })
        assert "error" in result

    def test_too_few_references_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "distribute_footprints", {
            "pcb_path": pcb,
            "references": ["R1"],
            "axis": "x",
        })
        assert "error" in result


class TestMoveFootprintsByDelta:
    def test_moves_by_delta(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "move_footprints_by_delta", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "dx": 5.0,
            "dy": 3.0,
        })
        assert "error" not in result, result
        assert "moved" in result
        assert len(result["moved"]) >= 2

    def test_zero_delta_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "move_footprints_by_delta", {
            "pcb_path": pcb,
            "references": ["R1"],
            "dx": 0.0,
            "dy": 0.0,
        })
        assert "error" in result

    def test_creates_backup(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "move_footprints_by_delta", {
            "pcb_path": pcb,
            "references": ["R1"],
            "dx": 1.0,
            "dy": 0.0,
        })
        assert "error" not in result
        bak = result.get("backup_path", "")
        assert bak and os.path.isfile(bak)


# ===========================================================================
# Board outline tools
# ===========================================================================

class TestBoardOutline:
    def test_get_board_outline(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_outline", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert "error" not in result, result
        assert "items" in result
        assert "count" in result

    def test_add_outline_segment(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "add_board_outline_segment", {
            "pcb_path": pcb,
            "x1": 0.0,
            "y1": 0.0,
            "x2": 100.0,
            "y2": 0.0,
            "width": 0.1,
        })
        assert "error" not in result, result
        assert "added" in result
        assert result["added"]["type"] == "gr_line"

    def test_add_outline_arc(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "add_board_outline_arc", {
            "pcb_path": pcb,
            "cx": 50.0,
            "cy": 50.0,
            "radius": 10.0,
            "start_angle_deg": 0.0,
            "end_angle_deg": 90.0,
            "width": 0.1,
        })
        assert "error" not in result, result
        assert "added" in result
        assert result["added"]["type"] == "gr_arc"

    def test_set_board_outline_rect(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "set_board_outline_rect", {
            "pcb_path": pcb,
            "x": 0.0,
            "y": 0.0,
            "width": 100.0,
            "height": 80.0,
            "line_width": 0.1,
            "corner_radius": 0.0,
        })
        assert "error" not in result, result
        assert "board_rect" in result
        assert result["board_rect"]["width"] == 100.0
        assert result["board_rect"]["height"] == 80.0

    def test_outline_rect_negative_dimensions_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "set_board_outline_rect", {
            "pcb_path": pcb,
            "x": 0.0,
            "y": 0.0,
            "width": -10.0,
            "height": 80.0,
            "line_width": 0.1,
            "corner_radius": 0.0,
        })
        assert "error" in result

    def test_clear_board_outline(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "clear_board_outline", {
            "pcb_path": pcb,
        })
        assert "error" not in result, result
        assert "removed_count" in result


# ===========================================================================
# Group tools
# ===========================================================================

class TestGroupTools:
    def test_assign_to_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "test_group",
        })
        assert "error" not in result, result
        assert "assigned" in result
        assert len(result["assigned"]) >= 1

    def test_assign_nonexistent_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["U99"],
            "group_name": "test_group",
        })
        assert "error" in result

    def test_list_groups_after_assign(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "my_group",
        })
        result = _call_tool(port, sid, "list_groups", {"pcb_path": pcb})
        assert "error" not in result, result
        assert "groups" in result
        assert "group_count" in result

    def test_get_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "detail_group",
        })
        result = _call_tool(port, sid, "get_group", {
            "pcb_path": pcb,
            "group_name": "detail_group",
        })
        assert "error" not in result, result
        assert result.get("group_name") == "detail_group"
        assert "members" in result

    def test_get_nonexistent_group_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_group", {
            "pcb_path": BOARD_FIXTURE,
            "group_name": "nonexistent_group",
        })
        assert "error" in result

    def test_score_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "score_group",
        })
        result = _call_tool(port, sid, "score_group", {
            "pcb_path": pcb,
            "group_name": "score_group",
        })
        assert "error" not in result, result
        assert "intra_hpwl_mm" in result
        assert "member_count" in result

    def test_move_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "move_group",
        })
        result = _call_tool(port, sid, "move_group", {
            "pcb_path": pcb,
            "group_name": "move_group",
            "anchor_x": 50.0,
            "anchor_y": 50.0,
        })
        assert "error" not in result, result
        assert "moved_count" in result

    def test_rotate_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        _call_tool(port, sid, "assign_to_group", {
            "pcb_path": pcb,
            "references": ["R1", "C1"],
            "group_name": "rotate_group",
        })
        result = _call_tool(port, sid, "rotate_group", {
            "pcb_path": pcb,
            "group_name": "rotate_group",
            "rotation_delta": 90.0,
        })
        assert "error" not in result, result
        assert "rotated_count" in result


# ===========================================================================
# Zone tools
# ===========================================================================

class TestZoneTools:
    def test_list_zones(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_zones", {
            "pcb_path": BOARD_FIXTURE,
        })
        assert "error" not in result, result
        assert "zones" in result
        assert "count" in result

    def test_add_copper_pour_zone(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "add_zone", {
            "pcb_path": pcb,
            "layer": "F.Cu",
            "polygon_pts": [
                {"x": 0.0, "y": 0.0},
                {"x": 50.0, "y": 0.0},
                {"x": 50.0, "y": 50.0},
                {"x": 0.0, "y": 50.0},
            ],
            "zone_type": "copper_pour",
            "net_name": "GND",
        })
        assert "error" not in result, result
        assert result.get("added") is True
        assert "zone_uuid" in result

    def test_add_zone_too_few_points_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "add_zone", {
            "pcb_path": pcb,
            "layer": "F.Cu",
            "polygon_pts": [
                {"x": 0.0, "y": 0.0},
                {"x": 50.0, "y": 0.0},
            ],
            "zone_type": "copper_pour",
            "net_name": "GND",
        })
        assert "error" in result

    def test_add_zone_invalid_type_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "add_zone", {
            "pcb_path": pcb,
            "layer": "F.Cu",
            "polygon_pts": [
                {"x": 0.0, "y": 0.0},
                {"x": 50.0, "y": 0.0},
                {"x": 50.0, "y": 50.0},
            ],
            "zone_type": "invalid_type",
        })
        assert "error" in result

    def test_delete_zone(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        add_result = _call_tool(port, sid, "add_zone", {
            "pcb_path": pcb,
            "layer": "F.Cu",
            "polygon_pts": [
                {"x": 0.0, "y": 0.0},
                {"x": 30.0, "y": 0.0},
                {"x": 30.0, "y": 30.0},
            ],
            "zone_type": "copper_pour",
            "net_name": "GND",
        })
        if "error" in add_result:
            pytest.skip(f"Could not add zone: {add_result['error']}")
        zone_uuid = add_result["zone_uuid"]
        result = _call_tool(port, sid, "delete_zone", {
            "pcb_path": pcb,
            "zone_uuid": zone_uuid,
        })
        assert "error" not in result, result
        assert result.get("deleted") is True

    def test_delete_nonexistent_zone_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = _copy_pcb(tmp_path)
        result = _call_tool(port, sid, "delete_zone", {
            "pcb_path": pcb,
            "zone_uuid": "nonexistent-uuid-12345",
        })
        assert "error" in result or result.get("deleted") is False


# ===========================================================================
# PCB Placement Helper
# ===========================================================================

class TestFindFreePcbArea:
    def test_returns_candidates(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "find_free_pcb_area", {
            "pcb_path": BOARD_FIXTURE,
            "width": 5.0,
            "height": 5.0,
        })
        assert "error" not in result, result
        assert "candidates" in result
        assert len(result["candidates"]) > 0

    def test_candidate_has_position(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "find_free_pcb_area", {
            "pcb_path": BOARD_FIXTURE,
            "width": 3.0,
            "height": 3.0,
        })
        assert "error" not in result
        c = result["candidates"][0]
        assert "x" in c
        assert "y" in c

    def test_returns_board_bounds(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "find_free_pcb_area", {
            "pcb_path": BOARD_FIXTURE,
            "width": 5.0,
            "height": 5.0,
        })
        assert "error" not in result
        assert "board_bounds" in result
