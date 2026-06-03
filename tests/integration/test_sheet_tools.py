"""
Integration tests for hierarchical sheet MCP tools.

Covers:
  - list_sheet_symbols
  - get_sheet_hierarchy
  - add_sheet_symbol (with create_child=True)
  - remove_sheet_symbol (with delete_child)

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Mutation tests use
tmp_path copies of the multi-sheet fixture.

Run:
    uv run python -m pytest tests/integration/test_sheet_tools.py -v
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
SHEET_ROOT = os.path.join(FIXTURE_DIR, "multi_sheet_root.kicad_sch")
SHEET_SUB1 = os.path.join(FIXTURE_DIR, "multi_sheet_sub1.kicad_sch")
SHEET_SUB2 = os.path.join(FIXTURE_DIR, "multi_sheet_sub2.kicad_sch")
SHEET_SUB2A = os.path.join(FIXTURE_DIR, "multi_sheet_sub2a.kicad_sch")

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
        pytest.skip("MCP server did not start — skipping sheet integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sheet-integration-test", "version": "1"},
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
# Helpers
# ---------------------------------------------------------------------------


def _copy_root(tmp_path, name: str = "multi_sheet_root.kicad_sch") -> str:
    dst = tmp_path / name
    shutil.copy2(SHEET_ROOT, dst)
    return str(dst)


def _copy_children_to(tmp_path) -> None:
    """Copy all child .kicad_sch files so sheet file references resolve."""
    for src in [SHEET_SUB1, SHEET_SUB2, SHEET_SUB2A]:
        dst = tmp_path / os.path.basename(src)
        if not dst.exists():
            shutil.copy2(src, dst)


# ===========================================================================
# list_sheet_symbols
# ===========================================================================


class TestListSheetSymbols:
    def test_returns_two_sheets(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": SHEET_ROOT},
        )
        assert "error" not in result, result
        assert result["sheet_count"] == 2
        assert len(result["sheets"]) == 2

    def test_sheets_have_required_fields(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": SHEET_ROOT},
        )
        for sheet in result["sheets"]:
            assert "uuid" in sheet
            assert "sheet_name" in sheet
            assert "sheet_file" in sheet
            assert "position" in sheet
            assert "size" in sheet
            assert "pins" in sheet
            assert sheet["uuid"] is not None
            assert sheet["sheet_name"] is not None
            assert sheet["position"]["x"] >= 0
            assert sheet["position"]["y"] >= 0

    def test_power_supply_sheet_has_two_pins(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": SHEET_ROOT},
        )
        ps_sheet = next(s for s in result["sheets"] if s["sheet_name"] == "Power Supply")
        assert len(ps_sheet["pins"]) == 2
        pin_names = {p["name"] for p in ps_sheet["pins"]}
        assert pin_names == {"VCC", "GND"}

    def test_amplifier_sheet_has_two_pins(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": SHEET_ROOT},
        )
        amp_sheet = next(s for s in result["sheets"] if s["sheet_name"] == "Amplifier")
        assert len(amp_sheet["pins"]) == 2
        pin_names = {p["name"] for p in amp_sheet["pins"]}
        assert pin_names == {"IN", "OUT"}

    def test_pin_has_uuid_and_at(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": SHEET_ROOT},
        )
        for sheet in result["sheets"]:
            for pin in sheet["pins"]:
                assert pin["uuid"] is not None, f"Pin {pin['name']!r} missing uuid"
                assert pin["at"] is not None, f"Pin {pin['name']!r} missing at"
                assert len(pin["at"]) == 3, f"Pin {pin['name']!r} at should have 3 values"

    def test_flat_schematic_returns_zero_sheets(self, mcp_server):
        """Flat schematics with no sheet symbols return sheet_count=0."""
        port, sid = mcp_server
        flat = os.path.join(FIXTURE_DIR, "test_schematic.kicad_sch")
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": flat},
        )
        assert "error" not in result, result
        assert result["sheet_count"] == 0
        assert result["sheets"] == []

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": "/nonexistent/schematic.kicad_sch"},
        )
        assert "error" in result

    def test_non_kicad_sch_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_sheet_symbols",
            {"schematic_path": "/tmp/not-a-schematic.txt"},
        )
        assert "error" in result


# ===========================================================================
# get_sheet_hierarchy
# ===========================================================================


class TestGetSheetHierarchy:
    def test_root_has_two_children(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": SHEET_ROOT},
        )
        assert "error" not in result, result
        root = result["hierarchy"]
        assert root["file"] == SHEET_ROOT
        assert root["sheet_count"] == 2
        assert len(root["children"]) == 2

    def test_children_have_sheet_names(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": SHEET_ROOT},
        )
        child_names = {c["sheet_name"] for c in result["hierarchy"]["children"]}
        assert child_names == {"Power Supply", "Amplifier"}

    def test_children_point_to_existing_files(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": SHEET_ROOT},
        )
        for child in result["hierarchy"]["children"]:
            assert os.path.basename(child["file"]) in {
                "multi_sheet_sub1.kicad_sch",
                "multi_sheet_sub2.kicad_sch",
            }

    def test_leaf_children_have_no_grandchildren(self, mcp_server):
        """Sub1 and sub2 are minimal files with no nested sheets."""
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": SHEET_ROOT},
        )
        for child in result["hierarchy"]["children"]:
            assert child["sheet_count"] == 0
            assert child["children"] == []

    def test_respects_max_depth(self, mcp_server):
        """max_depth=0 means root can list its children but those children are cut off."""
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": SHEET_ROOT, "max_depth": 0},
        )
        assert "error" not in result, result
        root = result["hierarchy"]
        assert len(root["children"]) == 2
        # Each child is past max_depth and should have the flag
        for child in root["children"]:
            assert child.get("max_depth_reached") is True, (
                f"Child {child['file']!r} should have max_depth_reached=True"
            )

    def test_nonexistent_root_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": "/nonexistent/schematic.kicad_sch"},
        )
        assert "error" in result

    def test_non_kicad_sch_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "get_sheet_hierarchy",
            {"schematic_path": "/tmp/not-a-schematic.txt"},
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# add_sheet_symbol
# ---------------------------------------------------------------------------


class TestAddSheetSymbol:
    """Integration tests for add_sheet_symbol."""

    def test_add_sheet_symbol_basic(self, mcp_server, tmp_path):
        """Add a sheet symbol without pins or child creation."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_name": "New Sheet",
                "sheet_file": "new_sheet.kicad_sch",
                "x": 100.0,
                "y": 100.0,
                "width": 50.8,
                "height": 50.8,
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert "sheet_uuid" in result
        assert result["sheet_name"] == "New Sheet"
        assert result["sheet_file"] == "new_sheet.kicad_sch"
        assert result["child_path"] is None

        # Verify the sheet was added by listing sheets
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        assert list_result["sheet_count"] == 3  # Original 2 + 1 new
        sheet_names = [s["sheet_name"] for s in list_result["sheets"]]
        assert "New Sheet" in sheet_names

    def test_add_sheet_symbol_with_create_child(self, mcp_server, tmp_path):
        """Add a sheet symbol and create the child file."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_name": "Created Child",
                "sheet_file": "created_child.kicad_sch",
                "x": 150.0,
                "y": 150.0,
                "width": 76.2,
                "height": 50.8,
                "create_child": True,
                "child_paper": "A4",
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert result["child_path"] is not None
        assert os.path.exists(result["child_path"])

        # Verify child file is valid
        child_result = _call_tool(
            port, sid, "list_sheet_symbols", {"schematic_path": result["child_path"]}
        )
        assert "error" not in child_result

    def test_add_sheet_symbol_with_pins(self, mcp_server, tmp_path):
        """Add a sheet symbol with hierarchical pins."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_name": "Pinned Sheet",
                "sheet_file": "pinned_sheet.kicad_sch",
                "x": 200.0,
                "y": 200.0,
                "width": 100.0,
                "height": 75.0,
                "pins": [
                    {"name": "VCC", "edge": "right", "distance_mm": 10.0},
                    {"name": "GND", "edge": "right", "distance_mm": 20.0},
                    {"name": "INPUT", "edge": "left", "distance_mm": 15.0},
                ],
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert result["pins_created"] == 3

        # Verify pins were added
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        pinned_sheet = next(s for s in list_result["sheets"] if s["sheet_name"] == "Pinned Sheet")
        assert len(pinned_sheet["pins"]) == 3
        pin_names = [p["name"] for p in pinned_sheet["pins"]]
        assert "VCC" in pin_names
        assert "GND" in pin_names
        assert "INPUT" in pin_names

    def test_add_sheet_symbol_coordinates_snapped_to_grid(self, mcp_server, tmp_path):
        """Verify coordinates are snapped to 1.27mm grid."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_name": "Grid Test",
                "sheet_file": "grid_test.kicad_sch",
                "x": 100.5,  # Should snap to 100.33
                "y": 200.7,  # Should snap to 200.66
                "width": 50.3,  # Should snap to 50.8
                "height": 75.9,  # Should snap to 76.2
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True

        # Verify snapped coordinates
        pos = result["position"]
        size = result["size"]
        assert abs(pos["x"] - 100.33) < 0.01
        assert abs(pos["y"] - 200.66) < 0.01
        assert abs(size["width"] - 50.8) < 0.01
        assert abs(size["height"] - 76.2) < 0.01

    def test_add_sheet_symbol_invalid_schematic(self, mcp_server, tmp_path):
        """Reject non-.kicad_sch files."""
        port, sid = mcp_server
        bad_file = tmp_path / "not_a_schematic.txt"
        bad_file.write_text("not a schematic")

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": str(bad_file),
                "sheet_name": "Test",
                "sheet_file": "test.kicad_sch",
                "x": 0.0,
                "y": 0.0,
            },
        )
        assert "error" in result

    def test_add_sheet_symbol_nonexistent_schematic(self, mcp_server, tmp_path):
        """Reject nonexistent schematic files."""
        port, sid = mcp_server

        result = _call_tool(
            port,
            sid,
            "add_sheet_symbol",
            {
                "schematic_path": str(tmp_path / "no_such_file.kicad_sch"),
                "sheet_name": "Test",
                "sheet_file": "test.kicad_sch",
                "x": 0.0,
                "y": 0.0,
            },
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# remove_sheet_symbol
# ---------------------------------------------------------------------------


class TestRemoveSheetSymbol:
    """Integration tests for remove_sheet_symbol."""

    def test_remove_sheet_by_uuid(self, mcp_server, tmp_path):
        """Remove a sheet symbol by UUID."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        # Get the UUID of the first sheet
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        assert list_result["sheet_count"] == 2
        target_uuid = list_result["sheets"][0]["uuid"]
        target_name = list_result["sheets"][0]["sheet_name"]

        # Remove it
        result = _call_tool(
            port,
            sid,
            "remove_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert result["removed_uuid"] == target_uuid
        assert result["removed_name"] == target_name

        # Verify it's gone
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        assert list_result["sheet_count"] == 1
        remaining_uuids = [s["uuid"] for s in list_result["sheets"]]
        assert target_uuid not in remaining_uuids

    def test_remove_sheet_by_name(self, mcp_server, tmp_path):
        """Remove a sheet symbol by name."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        # Remove by name
        result = _call_tool(
            port,
            sid,
            "remove_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "Power Supply",
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert result["removed_name"] == "Power Supply"

        # Verify it's gone
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        assert list_result["sheet_count"] == 1
        remaining_names = [s["sheet_name"] for s in list_result["sheets"]]
        assert "Power Supply" not in remaining_names

    def test_remove_nonexistent_sheet_returns_error(self, mcp_server, tmp_path):
        """Removing a nonexistent sheet returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "remove_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "nonexistent-uuid-12345",
            },
        )
        assert "error" in result

    def test_remove_from_empty_schematic_returns_error(self, mcp_server, tmp_path):
        """Removing from a schematic with no sheets returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        # Remove all sheets first
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        for sheet in list_result["sheets"]:
            _call_tool(
                port,
                sid,
                "remove_sheet_symbol",
                {
                    "schematic_path": parent_path,
                    "sheet_identifier": sheet["uuid"],
                },
            )

        # Try to remove from empty schematic
        result = _call_tool(
            port,
            sid,
            "remove_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "any-uuid",
            },
        )
        assert "error" in result

    def test_remove_sheet_invalid_schematic(self, mcp_server, tmp_path):
        """Reject non-.kicad_sch files."""
        port, sid = mcp_server
        bad_file = tmp_path / "not_a_schematic.txt"
        bad_file.write_text("not a schematic")

        result = _call_tool(
            port,
            sid,
            "remove_sheet_symbol",
            {
                "schematic_path": str(bad_file),
                "sheet_identifier": "any-uuid",
            },
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# update_sheet_symbol
# ---------------------------------------------------------------------------


class TestUpdateSheetSymbol:
    """Integration tests for update_sheet_symbol."""

    def test_update_sheet_name(self, mcp_server, tmp_path):
        """Update the sheet name."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        # Get the UUID of the first sheet
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        # Update the name
        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "sheet_name": "Renamed Sheet",
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert "sheet_name" in result["updated_fields"]

        # Verify the update
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        updated_sheet = next(s for s in list_result["sheets"] if s["uuid"] == target_uuid)
        assert updated_sheet["sheet_name"] == "Renamed Sheet"

    def test_update_sheet_file(self, mcp_server, tmp_path):
        """Update the sheet file reference."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "sheet_file": "new_file.kicad_sch",
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert "sheet_file" in result["updated_fields"]

        # Verify the update
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        updated_sheet = next(s for s in list_result["sheets"] if s["uuid"] == target_uuid)
        assert updated_sheet["sheet_file"] == "new_file.kicad_sch"

    def test_update_position(self, mcp_server, tmp_path):
        """Update the sheet position."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        # Use grid-aligned coordinates (multiples of 1.27mm)
        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "x": 300.99,  # 237 * 1.27
                "y": 400.05,  # 315 * 1.27
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert "position" in result["updated_fields"]

        # Verify the update
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        updated_sheet = next(s for s in list_result["sheets"] if s["uuid"] == target_uuid)
        assert abs(updated_sheet["position"]["x"] - 300.99) < 0.01
        assert abs(updated_sheet["position"]["y"] - 400.05) < 0.01

    def test_update_size(self, mcp_server, tmp_path):
        """Update the sheet size."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        # Use grid-aligned sizes (multiples of 1.27mm)
        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "width": 151.13,  # 119 * 1.27
                "height": 100.33,  # 79 * 1.27
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert "size" in result["updated_fields"]

        # Verify the update
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        updated_sheet = next(s for s in list_result["sheets"] if s["uuid"] == target_uuid)
        assert abs(updated_sheet["size"]["width"] - 151.13) < 0.01
        assert abs(updated_sheet["size"]["height"] - 100.33) < 0.01

    def test_update_multiple_fields(self, mcp_server, tmp_path):
        """Update multiple fields at once."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        # Use grid-aligned coordinates (multiples of 1.27mm)
        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "sheet_name": "Multi Update",
                "sheet_file": "multi.kicad_sch",
                "x": 10.16,  # 8 * 1.27 — far from other sheets
                "y": 10.16,  # 8 * 1.27
                "width": 76.2,  # 60 * 1.27
                "height": 88.9,  # 70 * 1.27
            },
        )
        assert "error" not in result, result.get("error")
        assert result["success"] is True
        assert len(result["updated_fields"]) == 4

        # Verify all updates
        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        updated_sheet = next(s for s in list_result["sheets"] if s["uuid"] == target_uuid)
        assert updated_sheet["sheet_name"] == "Multi Update"
        assert updated_sheet["sheet_file"] == "multi.kicad_sch"
        assert abs(updated_sheet["position"]["x"] - 10.16) < 0.01
        assert abs(updated_sheet["position"]["y"] - 10.16) < 0.01
        assert abs(updated_sheet["size"]["width"] - 76.2) < 0.01
        assert abs(updated_sheet["size"]["height"] - 88.9) < 0.01

    def test_update_nonexistent_sheet_returns_error(self, mcp_server, tmp_path):
        """Updating a nonexistent sheet returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "nonexistent-uuid-12345",
                "sheet_name": "Test",
            },
        )
        assert "error" in result

    def test_update_with_no_fields_returns_error(self, mcp_server, tmp_path):
        """Updating with no fields returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
            },
        )
        assert "error" in result

    def test_update_sheet_invalid_schematic(self, mcp_server, tmp_path):
        """Reject non-.kicad_sch files."""
        port, sid = mcp_server
        bad_file = tmp_path / "not_a_schematic.txt"
        bad_file.write_text("not a schematic")

        result = _call_tool(
            port,
            sid,
            "update_sheet_symbol",
            {
                "schematic_path": str(bad_file),
                "sheet_identifier": "any-uuid",
                "sheet_name": "Test",
            },
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Phase 3: Sheet Pin Management
# ---------------------------------------------------------------------------


class TestAddSheetPin:
    """Integration tests for add_sheet_pin tool."""

    def test_add_pin_to_right_edge(self, mcp_server, tmp_path):
        """Add a pin to the right edge of a sheet symbol."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "NEW_PIN",
                "edge": "right",
                "distance_mm": 5.08,
            },
        )
        assert result["success"] is True
        assert result["pin_name"] == "NEW_PIN"
        assert result["edge"] == "right"
        assert "pin_uuid" in result

    def test_add_pin_to_left_edge(self, mcp_server, tmp_path):
        """Add a pin to the left edge of a sheet symbol."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "LEFT_PIN",
                "edge": "left",
                "distance_mm": 2.54,
            },
        )
        assert result["success"] is True
        assert result["edge"] == "left"

    def test_add_pin_to_top_edge(self, mcp_server, tmp_path):
        """Add a pin to the top edge of a sheet symbol."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "TOP_PIN",
                "edge": "top",
                "distance_mm": 3.81,
            },
        )
        assert result["success"] is True
        assert result["edge"] == "top"

    def test_add_pin_to_bottom_edge(self, mcp_server, tmp_path):
        """Add a pin to the bottom edge of a sheet symbol."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "BOTTOM_PIN",
                "edge": "bottom",
                "distance_mm": 1.27,
            },
        )
        assert result["success"] is True
        assert result["edge"] == "bottom"

    def test_add_pin_invalid_edge_returns_error(self, mcp_server, tmp_path):
        """Invalid edge value returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "BAD_PIN",
                "edge": "diagonal",
                "distance_mm": 1.27,
            },
        )
        assert "error" in result

    def test_add_pin_sheet_not_found_returns_error(self, mcp_server, tmp_path):
        """Adding a pin to a non-existent sheet returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "nonexistent-uuid-99999",
                "pin_name": "ORPHAN_PIN",
                "edge": "right",
                "distance_mm": 1.27,
            },
        )
        assert "error" in result


class TestRemoveSheetPin:
    """Integration tests for remove_sheet_pin tool."""

    def test_remove_existing_pin(self, mcp_server, tmp_path):
        """Remove an existing pin from a sheet symbol."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        # Power Supply sheet has VCC and GND pins
        power_sheet = next(s for s in list_result["sheets"] if s["sheet_name"] == "Power Supply")
        target_uuid = power_sheet["uuid"]

        result = _call_tool(
            port,
            sid,
            "remove_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "VCC",
            },
        )
        assert result["success"] is True
        assert result["removed_pin_name"] == "VCC"

    def test_remove_nonexistent_pin_returns_error(self, mcp_server, tmp_path):
        """Removing a pin that doesn't exist returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        result = _call_tool(
            port,
            sid,
            "remove_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "NONEXISTENT_PIN",
            },
        )
        assert "error" in result

    def test_remove_pin_sheet_not_found_returns_error(self, mcp_server, tmp_path):
        """Removing a pin from a non-existent sheet returns an error."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "remove_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": "nonexistent-uuid-88888",
                "pin_name": "ANY_PIN",
            },
        )
        assert "error" in result

    def test_add_then_remove_pin_roundtrip(self, mcp_server, tmp_path):
        """Add a pin then remove it - roundtrip test."""
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        list_result = _call_tool(port, sid, "list_sheet_symbols", {"schematic_path": parent_path})
        target_uuid = list_result["sheets"][0]["uuid"]

        # Add a pin
        add_result = _call_tool(
            port,
            sid,
            "add_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "TEMP_PIN",
                "edge": "right",
                "distance_mm": 2.54,
            },
        )
        assert add_result["success"] is True

        # Remove the same pin
        remove_result = _call_tool(
            port,
            sid,
            "remove_sheet_pin",
            {
                "schematic_path": parent_path,
                "sheet_identifier": target_uuid,
                "pin_name": "TEMP_PIN",
            },
        )
        assert remove_result["success"] is True
        assert remove_result["removed_pin_name"] == "TEMP_PIN"

    def test_remove_pin_invalid_schematic(self, mcp_server, tmp_path):
        """Reject non-.kicad_sch files."""
        port, sid = mcp_server
        bad_file = tmp_path / "not_a_schematic.txt"
        bad_file.write_text("not a schematic")

        result = _call_tool(
            port,
            sid,
            "remove_sheet_pin",
            {
                "schematic_path": str(bad_file),
                "sheet_identifier": "any-uuid",
                "pin_name": "ANY_PIN",
            },
        )
        assert "error" in result
