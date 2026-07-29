"""
Integration tests for all PCB-related MCP tools.

Covers:
  - Query tools:     get_board_info, list_footprints, get_footprint,
                     list_nets, get_ratsnest
  - Placement tools: set_footprint_position, flip_footprint
  - Edit tools:      set_footprint_property
  - Library tools:   sync_footprint_index, get_footprint_sync_status,
                     list_footprint_libraries, search_footprints,
                     get_footprint_details

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  No KiCad installation
is required for the query/placement tests — they use the
``tests/integration/fixtures/test_board.kicad_pcb`` fixture.  Library
tool tests make structure-only assertions that tolerate an empty footprint
library environment.

Run:
    uv run python -m pytest tests/integration/test_pcb_tools.py -v
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
TRACKS_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_tracks.kicad_pcb")

# ---------------------------------------------------------------------------
# Transport helpers (duplicated from test_plugin_smoke for self-containment)
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
    """POST to /mcp; return (parsed_response, session_id).

    FastMCP streamable-http wraps responses in SSE frames.
    """
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
    with opener.open(req, timeout=15) as resp:
        returned_session_id = resp.headers.get("mcp-session-id", session_id)
        raw = resp.read().decode()

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:") :].strip()
            if json_str:
                return json.loads(json_str), returned_session_id

    return json.loads(raw), returned_session_id


_call_id = itertools.count(100)


def _call_tool(port: int, session_id: str | None, name: str, arguments: dict) -> dict:
    """Invoke *name* via tools/call and return the parsed tool result dict.

    Raises AssertionError if the JSON-RPC layer reports an error.  When the
    tool itself returns an ``{"error": ...}`` dict, or FastMCP marks the result
    as ``isError``, the returned dict always contains an ``"error"`` key so
    individual tests can assert on it.
    """
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
    # FastMCP wraps tool output as content blocks; extract the first text block
    content = result.get("content", [])
    text_block = next((c["text"] for c in content if c.get("type") == "text"), None)
    assert text_block is not None, f"No text content in tools/call response for {name!r}: {result}"

    # isError=True means FastMCP caught an exception or a pydantic validation error;
    # the text is a plain error string, not JSON — normalise to {"error": ...}
    if result.get("isError"):
        return {"error": text_block}

    return json.loads(text_block)


# ---------------------------------------------------------------------------
# Fixture: running MCP server (module scope — shared across all test classes)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_server():
    """Start the MCP server (plugin profile); yield (port, session_id)."""
    port = _find_free_port()
    env = os.environ.copy()
    env.update(
        {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_PORT": str(port),
            "MCP_HOST": "127.0.0.1",
            "KICAD_MCP_PROFILE": "plugin",
        }
    )
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
        pytest.skip("MCP server did not start — skipping PCB integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pcb-integration-test", "version": "1"},
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
# Query tool tests (read-only — use fixture directly)
# ---------------------------------------------------------------------------


class TestGetBoardInfo:
    def test_footprint_count(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_info", {"pcb_path": BOARD_FIXTURE})
        assert result.get("footprint_count") == 3

    def test_net_count(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_info", {"pcb_path": BOARD_FIXTURE})
        assert result.get("net_count") == 3

    def test_thickness(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_info", {"pcb_path": BOARD_FIXTURE})
        assert abs(result.get("thickness_mm", 0) - 1.6) < 0.001

    def test_layers_include_copper(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_board_info", {"pcb_path": BOARD_FIXTURE})
        layer_names = [l["name"] for l in result.get("all_layers", [])]
        assert "F.Cu" in layer_names
        assert "B.Cu" in layer_names

    def test_missing_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_board_info", {"pcb_path": "/nonexistent/board.kicad_pcb"}
        )
        assert "error" in result


class TestListFootprints:
    def test_returns_all_refs(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_footprints", {"pcb_path": BOARD_FIXTURE})
        refs = {fp["reference"] for fp in result.get("footprints", [])}
        assert refs == {"R1", "C1", "J1"}

    def test_footprint_position(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_footprints", {"pcb_path": BOARD_FIXTURE})
        r1 = next(fp for fp in result["footprints"] if fp["reference"] == "R1")
        assert abs(r1["x"] - 10.0) < 0.001
        assert abs(r1["y"] - 20.0) < 0.001

    def test_footprint_rotation(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_footprints", {"pcb_path": BOARD_FIXTURE})
        c1 = next(fp for fp in result["footprints"] if fp["reference"] == "C1")
        assert abs(c1["rotation"] - 90.0) < 0.001

    def test_footprint_layer(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_footprints", {"pcb_path": BOARD_FIXTURE})
        j1 = next(fp for fp in result["footprints"] if fp["reference"] == "J1")
        assert j1["layer"] == "B.Cu"


class TestGetFootprint:
    def test_value_and_reference(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_footprint", {"pcb_path": BOARD_FIXTURE, "reference": "R1"}
        )
        assert result.get("reference") == "R1"
        assert result.get("value") == "10k"

    def test_pad_count(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_footprint", {"pcb_path": BOARD_FIXTURE, "reference": "R1"}
        )
        assert len(result.get("pads", [])) == 2

    def test_pad_net_name(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_footprint", {"pcb_path": BOARD_FIXTURE, "reference": "R1"}
        )
        pad1 = next(p for p in result["pads"] if p["number"] == "1")
        assert pad1["net_name"] == "VCC"

    def test_pad_coords_are_local(self, mcp_server):
        """Pads expose local_x/local_y (footprint-relative), not world coords."""
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_footprint", {"pcb_path": BOARD_FIXTURE, "reference": "R1"}
        )
        pad1 = next(p for p in result["pads"] if p["number"] == "1")
        assert "local_x" in pad1
        assert "local_y" in pad1

    def test_missing_reference_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_footprint", {"pcb_path": BOARD_FIXTURE, "reference": "U99"}
        )
        assert "error" in result


class TestListNets:
    def test_excludes_net_zero(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_nets", {"pcb_path": BOARD_FIXTURE})
        net_ids = {n["net_id"] for n in result.get("nets", [])}
        assert 0 not in net_ids

    def test_includes_all_named_nets(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_nets", {"pcb_path": BOARD_FIXTURE})
        names = {n["name"] for n in result["nets"]}
        assert {"VCC", "GND", "NET_A"} <= names

    def test_count(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_nets", {"pcb_path": BOARD_FIXTURE})
        assert result.get("count") == 3


class TestGetRatsnest:
    def test_response_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_ratsnest", {"pcb_path": BOARD_FIXTURE})
        assert "unconnected" in result
        assert "unconnected_count" in result
        assert "fully_routed" in result

    def test_net_a_has_unconnected_pair(self, mcp_server):
        """NET_A: C1:pad2 and J1:pad2 share the net but have no connecting track."""
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_ratsnest", {"pcb_path": BOARD_FIXTURE})
        net_a_pairs = [r for r in result["unconnected"] if r["net"] == "NET_A"]
        assert len(net_a_pairs) > 0, "Expected at least one unconnected pair on NET_A"

    def test_not_fully_routed(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_ratsnest", {"pcb_path": BOARD_FIXTURE})
        assert result["fully_routed"] is False

    def test_pair_structure(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_ratsnest", {"pcb_path": BOARD_FIXTURE})
        for pair in result["unconnected"]:
            assert "net" in pair
            assert "from" in pair and "to" in pair
            assert all(k in pair["from"] for k in ("ref", "pad", "x", "y"))
            assert all(k in pair["to"] for k in ("ref", "pad", "x", "y"))


# ---------------------------------------------------------------------------
# Placement tool tests (mutation — each test gets its own temp PCB copy)
# ---------------------------------------------------------------------------


class TestSetFootprintPosition:
    def test_moves_footprint(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "x": 50.0,
                "y": 60.0,
                "rotation": None,
            },
        )
        assert "error" not in result, result
        assert abs(result["placed_at"]["x"] - 50.0) < 0.001
        assert abs(result["placed_at"]["y"] - 60.0) < 0.001

    def test_preserves_unchanged_axis(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "x": 99.0,
                "y": None,
                "rotation": None,
            },
        )
        assert "error" not in result
        # y must be unchanged from fixture value (20.0)
        assert abs(result["placed_at"]["y"] - 20.0) < 0.001

    def test_creates_backup_file(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "x": 1.0,
                "y": 1.0,
                "rotation": None,
            },
        )
        assert "error" not in result
        bak = result.get("backup_path", "")
        assert bak and os.path.isfile(bak), f"Backup file not found at {bak!r}"

    def test_previous_position_reported(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "x": 5.0,
                "y": 5.0,
                "rotation": None,
            },
        )
        assert "error" not in result
        assert abs(result["moved_from"]["x"] - 10.0) < 0.001
        assert abs(result["moved_from"]["y"] - 20.0) < 0.001

    def test_missing_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "U99",
                "x": 1.0,
                "y": 1.0,
                "rotation": None,
            },
        )
        assert "error" in result

    def test_no_args_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_position",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "x": None,
                "y": None,
                "rotation": None,
            },
        )
        assert "error" in result


class TestFlipFootprint:
    def test_flips_front_to_back(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "flip_footprint",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
            },
        )
        assert "error" not in result, result
        assert result.get("previous_layer") == "F.Cu"
        assert result.get("new_layer") == "B.Cu"

    def test_flips_back_to_front(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "flip_footprint",
            {
                "pcb_path": str(pcb),
                "reference": "J1",
            },
        )
        assert "error" not in result
        assert result.get("previous_layer") == "B.Cu"
        assert result.get("new_layer") == "F.Cu"

    def test_double_flip_restores_layer(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        _call_tool(port, sid, "flip_footprint", {"pcb_path": str(pcb), "reference": "R1"})
        result = _call_tool(port, sid, "flip_footprint", {"pcb_path": str(pcb), "reference": "R1"})
        assert result.get("new_layer") == "F.Cu"

    def test_missing_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "flip_footprint",
            {
                "pcb_path": str(pcb),
                "reference": "U99",
            },
        )
        assert "error" in result

    def test_creates_backup(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(port, sid, "flip_footprint", {"pcb_path": str(pcb), "reference": "R1"})
        assert "error" not in result
        bak = result.get("backup_path", "")
        assert bak and os.path.isfile(bak)


class TestSetFootprintProperty:
    def test_updates_value(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_property",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "property_name": "Value",
                "value": "22k",
            },
        )
        assert "error" not in result, result
        assert result.get("new_value") == "22k"
        assert result.get("previous_value") == "10k"

    def test_update_persists_to_file(self, mcp_server, tmp_path):
        """After set, re-reading the PCB should reflect the new value."""
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        _call_tool(
            port,
            sid,
            "set_footprint_property",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "property_name": "Value",
                "value": "47k",
            },
        )
        result = _call_tool(port, sid, "get_footprint", {"pcb_path": str(pcb), "reference": "R1"})
        assert result.get("value") == "47k"

    def test_missing_property_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_property",
            {
                "pcb_path": str(pcb),
                "reference": "R1",
                "property_name": "NonExistentProp",
                "value": "x",
            },
        )
        assert "error" in result

    def test_missing_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pcb = tmp_path / "board.kicad_pcb"
        shutil.copy2(BOARD_FIXTURE, pcb)
        result = _call_tool(
            port,
            sid,
            "set_footprint_property",
            {
                "pcb_path": str(pcb),
                "reference": "U99",
                "property_name": "Value",
                "value": "x",
            },
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Library tool tests (structure-only; tolerates missing KiCad installation)
# ---------------------------------------------------------------------------


class TestSyncFootprintIndex:
    def test_returns_started_or_already_running(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "sync_footprint_index",
            {
                "force": False,
                "project_path": None,
            },
        )
        assert result.get("status") in ("started", "already_running"), (
            f"Unexpected status: {result}"
        )

    def test_force_sync_returns_started(self, mcp_server):
        """Force=True should always kick off a new sync (or return started)."""
        port, sid = mcp_server
        # Wait a moment in case a previous sync is still running
        time.sleep(0.5)
        result = _call_tool(
            port,
            sid,
            "sync_footprint_index",
            {
                "force": True,
                "project_path": None,
            },
        )
        assert result.get("status") in ("started", "already_running")


class TestGetFootprintSyncStatus:
    def test_returns_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_footprint_sync_status", {})
        for key in (
            "running",
            "current",
            "total",
            "percent_complete",
            "current_library",
            "last_result",
            "error",
        ):
            assert key in result, f"Missing key {key!r} in status response"

    def test_running_is_bool(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_footprint_sync_status", {})
        assert isinstance(result["running"], bool)

    def test_percent_complete_in_range(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_footprint_sync_status", {})
        pct = result["percent_complete"]
        assert 0 <= pct <= 100, f"percent_complete out of range: {pct}"


class TestListFootprintLibraries:
    def test_returns_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_footprint_libraries",
            {
                "project_path": None,
            },
        )
        assert "libraries" in result
        assert "count" in result

    def test_count_matches_list_length(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_footprint_libraries",
            {
                "project_path": None,
            },
        )
        assert result["count"] == len(result["libraries"])

    def test_no_error_in_response(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_footprint_libraries",
            {
                "project_path": None,
            },
        )
        assert "error" not in result


class TestSearchFootprints:
    def test_returns_results_key(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "search_footprints",
            {
                "query": "resistor",
                "project_path": None,
                "max_results": 5,
            },
        )
        assert "results" in result

    def test_results_is_list(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "search_footprints",
            {
                "query": "capacitor",
                "project_path": None,
                "max_results": 5,
            },
        )
        assert isinstance(result["results"], list)

    def test_empty_query_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "search_footprints",
            {
                "query": "",
                "project_path": None,
                "max_results": 5,
            },
        )
        assert "error" in result

    def test_result_entries_have_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "search_footprints",
            {
                "query": "R_0402",
                "project_path": None,
                "max_results": 3,
            },
        )
        for entry in result.get("results", []):
            assert "name" in entry
            assert "library" in entry


class TestGetFootprintDetails:
    def test_nonexistent_library_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_footprint_details",
            {
                "library_name": "__nonexistent_lib__",
                "footprint_name": "R_0402",
                "project_path": None,
            },
        )
        assert "error" in result

    def test_nonexistent_footprint_in_known_library(self, mcp_server):
        """If the library exists but the footprint doesn't, expect an error."""
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_footprint_details",
            {
                "library_name": "Resistor_SMD",
                "footprint_name": "__nonexistent_footprint__",
                "project_path": None,
            },
        )
        assert "error" in result


class TestListTracks:
    FIXTURE = TRACKS_FIXTURE

    def test_returns_traces(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_tracks", {"pcb_path": self.FIXTURE})
        assert "traces" in result
        assert "segment_count" in result
        assert "trace_count" in result
        assert result["segment_count"] >= 1

    def test_traces_have_structure(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_tracks", {"pcb_path": self.FIXTURE})
        for trace in result["traces"]:
            assert "width" in trace
            assert "layer" in trace
            assert "net" in trace
            assert "segments" in trace
            assert "pads" in trace
            assert len(trace["segments"]) > 0

    def test_filter_by_net(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_tracks", {"pcb_path": self.FIXTURE, "net": "GND"})
        assert all(t["net"] == "GND" for t in result["traces"])


class TestListVias:
    FIXTURE = TRACKS_FIXTURE

    def test_returns_vias(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_vias", {"pcb_path": self.FIXTURE})
        assert "vias" in result
        assert "count" in result
        assert result["count"] == 2

    def test_filter_by_net(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_vias", {"pcb_path": self.FIXTURE, "net": "VCC"})
        assert result["count"] == 1
        assert all(v["net"] == "VCC" for v in result["vias"])


class TestGetRatsnestWithConnected:
    FIXTURE = TRACKS_FIXTURE

    def test_default_excludes_connected_pads(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_ratsnest", {"pcb_path": self.FIXTURE})
        assert "connected_pads" not in result

    def test_get_connected_pads_includes_key(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port, sid, "get_ratsnest",
            {"pcb_path": self.FIXTURE, "get_connected_pads": True},
        )
        assert "connected_pads" in result
        assert "connected_count" in result
        assert isinstance(result["connected_pads"], list)
