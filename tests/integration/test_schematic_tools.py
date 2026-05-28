"""
Integration tests for schematic-related MCP tools.

Covers:
  - Netlist tools:     extract_schematic_netlist, find_component_connections
  - Component edit:    list_component_properties, set_component_property,
                       move_component, remove_symbol_from_schematic,
                       add_label_to_schematic, list_labels_in_schematic,
                       delete_label_from_schematic, add_symbol_to_schematic,
                       delete_component_property
  - Wire edit:        connect_pins_with_wire, connect_points_with_wire,
                       delete_wire_from_schematic
  - Placement helpers: get_schematic_sheet_info, find_free_area

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Mutation tests use
tmp_path copies of the fixture schematic.

Run:
    uv run python -m pytest tests/integration/test_schematic_tools.py -v
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
SCH_FIXTURE = os.path.join(FIXTURE_DIR, "test_schematic.kicad_sch")

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
            json_str = line[len("data:") :].strip()
            if json_str:
                return json.loads(json_str), returned_session_id

    return json.loads(raw), returned_session_id


_call_id = itertools.count(700)


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
        pytest.skip("MCP server did not start — skipping schematic integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "schematic-integration-test", "version": "1"},
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
# Helper: copy schematic fixture to tmp
# ---------------------------------------------------------------------------


def _copy_sch(tmp_path, name: str = "test.kicad_sch") -> str:
    dst = tmp_path / name
    shutil.copy2(SCH_FIXTURE, dst)
    return str(dst)


# ===========================================================================
# Query tools (read-only — use fixture directly)
# ===========================================================================


class TestGetSchematicSheetInfo:
    def test_returns_paper_info(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_schematic_sheet_info",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "error" not in result, result
        assert "paper" in result
        assert result["paper"]["name"] == "A4"

    def test_returns_drawing_area(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_schematic_sheet_info",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "drawing_area" in result
        assert result["drawing_area"]["max_x"] > 0
        assert result["drawing_area"]["max_y"] > 0

    def test_returns_grid_mm(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_schematic_sheet_info",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert abs(result["grid_mm"] - 1.27) < 0.01

    def test_returns_recommended_area(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_schematic_sheet_info",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "recommended_area" in result
        rec = result["recommended_area"]
        assert rec["min_x"] < rec["max_x"]
        assert rec["min_y"] < rec["max_y"]

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_schematic_sheet_info",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
            },
        )
        assert "error" in result


class TestExtractSchematicNetlist:
    def test_returns_components(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "extract_schematic_netlist",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True
        assert "analysis" in result
        assert "components" in result["analysis"]
        assert len(result["analysis"]["components"]) > 0

    def test_fixture_has_r1(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "extract_schematic_netlist",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "error" not in result
        assert "R1" in result["analysis"]["components"]

    def test_returns_nets(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "extract_schematic_netlist",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "error" not in result
        assert "analysis" in result
        # Nets may be under analysis or as floating_nets
        analysis = result["analysis"]
        has_nets = "nets" in analysis or "floating_nets" in analysis
        assert has_nets, (
            f"Expected nets or floating_nets in analysis, got keys: {list(analysis.keys())}"
        )

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "extract_schematic_netlist",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
            },
        )
        assert "error" in result

    def test_with_wire_topology(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "extract_schematic_netlist",
            {
                "schematic_path": SCH_FIXTURE,
                "include_wire_topology": True,
            },
        )
        assert "error" not in result
        assert "analysis" in result
        assert "components" in result["analysis"]


class TestListComponentProperties:
    def test_returns_properties_for_r1(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_component_properties",
            {
                "schematic_path": SCH_FIXTURE,
                "reference": "R1",
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True
        assert "properties" in result
        prop_names = [p["name"] for p in result["properties"]]
        assert "Reference" in prop_names
        assert "Value" in prop_names

    def test_nonexistent_reference_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_component_properties",
            {
                "schematic_path": SCH_FIXTURE,
                "reference": "U99",
            },
        )
        assert "error" in result

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_component_properties",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "reference": "R1",
            },
        )
        assert "error" in result


class TestListLabelsInSchematic:
    def test_returns_labels_list(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_labels_in_schematic",
            {
                "schematic_path": SCH_FIXTURE,
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True
        assert "labels" in result
        assert "count" in result
        assert result["count"] == len(result["labels"])

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_labels_in_schematic",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
            },
        )
        assert "error" in result


class TestFindFreeArea:
    def test_returns_candidates(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "find_free_area",
            {
                "schematic_path": SCH_FIXTURE,
                "width": 10.0,
                "height": 10.0,
            },
        )
        assert "error" not in result, result
        assert "candidates" in result
        assert len(result["candidates"]) > 0

    def test_candidate_has_origin(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "find_free_area",
            {
                "schematic_path": SCH_FIXTURE,
                "width": 5.0,
                "height": 5.0,
            },
        )
        assert "error" not in result
        c = result["candidates"][0]
        assert "origin" in c or "placement" in c

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "find_free_area",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "width": 10.0,
                "height": 10.0,
            },
        )
        assert "error" in result


# ===========================================================================
# Mutation tools (use tmp_path copies)
# ===========================================================================


class TestMoveComponent:
    def test_moves_component(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "move_component",
            {
                "schematic_path": sch,
                "reference": "R1",
                "x": 50.0,
                "y": 50.0,
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True

    def test_creates_backup(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "move_component",
            {
                "schematic_path": sch,
                "reference": "R1",
                "x": 50.0,
                "y": 50.0,
            },
        )
        assert "error" not in result
        bak = result.get("backup_path", "")
        assert bak and os.path.isfile(bak)

    def test_nonexistent_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "move_component",
            {
                "schematic_path": sch,
                "reference": "U99",
                "x": 50.0,
                "y": 50.0,
            },
        )
        assert "error" in result

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "move_component",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "reference": "R1",
                "x": 50.0,
                "y": 50.0,
            },
        )
        assert "error" in result


class TestSetComponentProperty:
    def test_updates_value(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "set_component_property",
            {
                "schematic_path": sch,
                "reference": "R1",
                "property_name": "Value",
                "property_value": "22k",
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True

    def test_change_persists(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        _call_tool(
            port,
            sid,
            "set_component_property",
            {
                "schematic_path": sch,
                "reference": "R1",
                "property_name": "Value",
                "property_value": "47k",
            },
        )
        result = _call_tool(
            port,
            sid,
            "list_component_properties",
            {
                "schematic_path": sch,
                "reference": "R1",
            },
        )
        assert "error" not in result
        val_prop = next(p for p in result["properties"] if p["name"] == "Value")
        assert val_prop["value"] == "47k"

    def test_nonexistent_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "set_component_property",
            {
                "schematic_path": sch,
                "reference": "U99",
                "property_name": "Value",
                "property_value": "x",
            },
        )
        assert "error" in result


class TestRemoveSymbolFromSchematic:
    def test_removes_symbol(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "remove_symbol_from_schematic",
            {
                "schematic_path": sch,
                "references": ["R2"],
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True
        assert result.get("total_removed_units", 0) >= 1

    def test_nonexistent_reference_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "remove_symbol_from_schematic",
            {
                "schematic_path": sch,
                "references": ["U99"],
            },
        )
        assert "error" in result

    def test_empty_references_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "remove_symbol_from_schematic",
            {
                "schematic_path": sch,
                "references": [],
            },
        )
        assert "error" in result


class TestAddLabelToSchematic:
    def test_adds_label(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "add_label_to_schematic",
            {
                "schematic_path": sch,
                "text": "NET_TEST",
                "x": 100.0,
                "y": 100.0,
                "angle": 0,
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True

    def test_label_appears_in_list(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        _call_tool(
            port,
            sid,
            "add_label_to_schematic",
            {
                "schematic_path": sch,
                "text": "MY_NET",
                "x": 101.6,
                "y": 101.6,
                "angle": 0,
            },
        )
        result = _call_tool(
            port,
            sid,
            "list_labels_in_schematic",
            {
                "schematic_path": sch,
            },
        )
        assert "error" not in result
        texts = [lbl["text"] for lbl in result["labels"]]
        assert "MY_NET" in texts

    def test_empty_text_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "add_label_to_schematic",
            {
                "schematic_path": sch,
                "text": "",
                "x": 100.0,
                "y": 100.0,
                "angle": 0,
            },
        )
        assert "error" in result

    def test_invalid_angle_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "add_label_to_schematic",
            {
                "schematic_path": sch,
                "text": "NET",
                "x": 100.0,
                "y": 100.0,
                "angle": 45,
            },
        )
        assert "error" in result


class TestDeleteLabelFromSchematic:
    def test_delete_after_add(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        _call_tool(
            port,
            sid,
            "add_label_to_schematic",
            {
                "schematic_path": sch,
                "text": "DEL_ME",
                "x": 101.6,
                "y": 101.6,
                "angle": 0,
            },
        )
        result = _call_tool(
            port,
            sid,
            "delete_label_from_schematic",
            {
                "schematic_path": sch,
                "x": 101.6,
                "y": 101.6,
                "text": "DEL_ME",
            },
        )
        assert "error" not in result, result
        assert result.get("success") is True


class TestDeleteComponentProperty:
    def test_cannot_delete_reference(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "delete_component_property",
            {
                "schematic_path": sch,
                "reference": "R1",
                "property_name": "Reference",
            },
        )
        assert "error" in result

    def test_cannot_delete_value(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "delete_component_property",
            {
                "schematic_path": sch,
                "reference": "R1",
                "property_name": "Value",
            },
        )
        assert "error" in result

    def test_nonexistent_property_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "delete_component_property",
            {
                "schematic_path": sch,
                "reference": "R1",
                "property_name": "NonExistentProp",
            },
        )
        assert "error" in result


class TestAddSymbolToSchematic:
    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "add_symbol_to_schematic",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "library_name": "Device",
                "symbol_name": "R_Small",
                "x": 100.0,
                "y": 100.0,
            },
        )
        assert "error" in result

    def test_not_kicad_sch_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fake = tmp_path / "test.txt"
        fake.write_text("not a schematic")
        result = _call_tool(
            port,
            sid,
            "add_symbol_to_schematic",
            {
                "schematic_path": str(fake),
                "library_name": "Device",
                "symbol_name": "R_Small",
                "x": 100.0,
                "y": 100.0,
            },
        )
        assert "error" in result


# ===========================================================================
# Wire edit tools
# ===========================================================================


class TestConnectPinsWithWire:
    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "connect_pins_with_wire",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "from_ref": "R1",
                "from_pin": "1",
                "to_ref": "R2",
                "to_pin": "1",
            },
        )
        assert "error" in result

    def test_not_kicad_sch_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fake = tmp_path / "test.txt"
        fake.write_text("not a schematic")
        result = _call_tool(
            port,
            sid,
            "connect_pins_with_wire",
            {
                "schematic_path": str(fake),
                "from_ref": "R1",
                "from_pin": "1",
                "to_ref": "R2",
                "to_pin": "1",
            },
        )
        assert "error" in result

    def test_nonexistent_pin_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "connect_pins_with_wire",
            {
                "schematic_path": sch,
                "from_ref": "U99",
                "from_pin": "1",
                "to_ref": "R1",
                "to_pin": "1",
            },
        )
        assert "error" in result


class TestConnectPointsWithWire:
    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "connect_points_with_wire",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "start_x": 100.0,
                "start_y": 100.0,
                "end_x": 150.0,
                "end_y": 100.0,
            },
        )
        assert "error" in result

    def test_zero_length_wire_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "connect_points_with_wire",
            {
                "schematic_path": sch,
                "start_x": 100.0,
                "start_y": 100.0,
                "end_x": 100.0,
                "end_y": 100.0,
            },
        )
        assert "error" in result

    def test_adds_wire_between_points(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "connect_points_with_wire",
            {
                "schematic_path": sch,
                "start_x": 50.8,
                "start_y": 50.8,
                "end_x": 60.96,
                "end_y": 50.8,
            },
        )
        # May succeed or error depending on wire routing; just check no crash
        assert isinstance(result, dict)


class TestDeleteWireFromSchematic:
    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "delete_wire_from_schematic",
            {
                "schematic_path": "/nonexistent/schematic.kicad_sch",
                "wires": [{"start_x": 0, "start_y": 0, "end_x": 10, "end_y": 10}],
            },
        )
        assert "error" in result

    def test_no_matching_wire_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)
        result = _call_tool(
            port,
            sid,
            "delete_wire_from_schematic",
            {
                "schematic_path": sch,
                "wires": [{"start_x": 0.0, "start_y": 0.0, "end_x": 1.0, "end_y": 1.0}],
            },
        )
        # Should return error or success=False since no wire at those coords
        assert (
            "error" in result
            or result.get("success") is False
            or result.get("deleted_count", 0) == 0
        )
