"""
Integration tests for hierarchical sheet MCP tools.

Covers:
  - list_sheet_symbols
  - get_sheet_hierarchy
  - create_child_sheet

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


# ===========================================================================
# create_child_sheet
# ===========================================================================


class TestCreateChildSchematic:
    def test_creates_valid_file(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "new_child.kicad_sch",
                "paper": "A3",
                "title": "Test Sheet",
            },
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert os.path.exists(result["child_path"])
        assert result["child_uuid"] is not None
        # Verify it ends with the expected filename
        assert result["child_path"].endswith("new_child.kicad_sch")

    def test_created_file_is_parseable(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "parseable.kicad_sch",
            },
        )
        assert result["success"] is True
        # Verify the file can be parsed by kicad-skip
        from kcaa.utils.skip_compat import safe_schematic

        sch = safe_schematic(result["child_path"])
        assert sch.uuid.value == result["child_uuid"]
        assert sch.paper.value == "A4"

    def test_appends_extension_if_missing(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "no_ext",
                "paper": "USLetter",
            },
        )
        assert result["success"] is True
        assert result["child_path"].endswith("no_ext.kicad_sch")

    def test_creates_in_parent_directory(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "relative_child.kicad_sch",
            },
        )
        assert result["success"] is True
        assert os.path.dirname(result["child_path"]) == str(tmp_path)

    def test_duplicate_file_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        # First call succeeds
        result1 = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "duplicate.kicad_sch",
            },
        )
        assert result1["success"] is True

        # Second call errors
        result2 = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "duplicate.kicad_sch",
            },
        )
        assert "error" in result2
        assert "already exists" in result2["error"].lower()

    def test_bad_paper_size_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": parent_path,
                "child_filename": "bad_paper.kicad_sch",
                "paper": "A7",
            },
        )
        assert "error" in result

    def test_nonexistent_parent_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "create_child_sheet",
            {
                "parent_path": str(tmp_path / "no_such_parent.kicad_sch"),
                "child_filename": "orphan.kicad_sch",
            },
        )
        assert "error" in result

    def test_all_standard_paper_sizes_accepted(self, mcp_server, tmp_path):
        port, sid = mcp_server
        _copy_children_to(tmp_path)
        parent_path = _copy_root(tmp_path)

        for paper in [
            "A4",
            "A3",
            "A2",
            "A5",
            "A",
            "B",
            "C",
            "D",
            "E",
            "USLetter",
            "USLegal",
            "USLedger",
        ]:
            result = _call_tool(
                port,
                sid,
                "create_child_sheet",
                {
                    "parent_path": parent_path,
                    "child_filename": f"paper_{paper.replace(' ', '_')}.kicad_sch",
                    "paper": paper,
                },
            )
            assert "error" not in result, f"Paper {paper!r} rejected: {result.get('error')}"
            assert result["success"] is True
