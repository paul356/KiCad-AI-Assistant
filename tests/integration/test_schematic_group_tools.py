"""
Integration tests for schematic symbol group MCP tools.

Covers:
  - assign_symbols_to_group
  - list_symbol_groups
  - get_symbol_group
  - score_symbol_group
  - place_symbol_group
  - move_symbol_group
  - rotate_symbol_group

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Mutation tests use
tmp_path copies of the fixture schematic.

Run:
    uv run python -m pytest tests/integration/test_schematic_group_tools.py -v
"""

from __future__ import annotations

import itertools
import json
import math
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

_GRID_MM = 1.27

# ---------------------------------------------------------------------------
# Transport helpers (shared with test_schematic_tools.py)
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


_call_id = itertools.count(900)


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
        pytest.skip("MCP server did not start — skipping schematic group integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "schematic-group-integration-test", "version": "1"},
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


# ---------------------------------------------------------------------------
# Helper: compute group member spread
# ---------------------------------------------------------------------------


def _member_spread(members: list[dict]) -> float:
    """Return the max Euclidean distance between any two member positions."""
    if len(members) < 2:
        return 0.0
    max_dist = 0.0
    for i in range(len(members)):
        xi, yi = members[i]["x"], members[i]["y"]
        for j in range(i + 1, len(members)):
            xj, yj = members[j]["x"], members[j]["y"]
            d = math.hypot(xi - xj, yi - yj)
            if d > max_dist:
                max_dist = d
    return max_dist


# ===========================================================================
# Assign + List + Get
# ===========================================================================


class TestAssignListGet:
    """Assign symbols, then list groups and get group details."""

    GROUP = "int_test_power"

    def test_assign_and_list(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        # Assign R1,R2,R3 to a group
        r1 = _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3"],
                "group_name": self.GROUP,
            },
        )
        assert "error" not in r1, r1
        assert len(r1["assigned"]) == 3
        assert r1["group_name"] == self.GROUP

        # List groups
        r2 = _call_tool(port, sid, "list_symbol_groups", {"schematic_path": sch})
        assert "error" not in r2, r2
        assert r2["group_count"] >= 1
        group_names = [g["group_name"] for g in r2["groups"]]
        assert self.GROUP in group_names

    def test_get_group_returns_members(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3"],
                "group_name": self.GROUP,
            },
        )

        result = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert "error" not in result, result
        assert result["group_name"] == self.GROUP
        assert result["member_count"] == 3

        member_refs = {m["reference"] for m in result["members"]}
        assert member_refs == {"R1", "R2", "R3"}

        for m in result["members"]:
            assert "x" in m
            assert "y" in m
            assert "rotation" in m
            assert "pin_count" in m
            assert m["pin_count"] >= 0
            # Each member should have a reasonable position on the sheet
            assert 0 <= m["x"] <= 500, f"{m['reference']} x={m['x']} out of range"
            assert 0 <= m["y"] <= 500, f"{m['reference']} y={m['y']} out of range"

    def test_unassign_moves_to_empty_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        # Assign then unassign
        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R2"],
                "group_name": "",
            },
        )

        # R2 should no longer be in the group
        result = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        member_refs = {m["reference"] for m in result["members"]}
        assert member_refs == {"R1", "R3"}

    def test_reassign_to_new_group(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R2"],
                "group_name": "another_group",
            },
        )

        # R2 should be in another_group, not in original
        r1 = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert {m["reference"] for m in r1["members"]} == {"R1"}

        r2 = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": "another_group",
            },
        )
        assert {m["reference"] for m in r2["members"]} == {"R2"}


# ===========================================================================
# Score
# ===========================================================================


class TestScore:
    """ScoreSymbolGroup returns proximity metrics."""

    GROUP = "int_test_score"

    def test_returns_proximity_metrics(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3", "R4"],
                "group_name": self.GROUP,
            },
        )

        result = _call_tool(
            port,
            sid,
            "score_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert "error" not in result, result
        assert "mean_nn_mm" in result
        assert "mean_spread_mm" in result
        assert math.isfinite(result["mean_nn_mm"])
        assert math.isfinite(result["mean_spread_mm"])
        assert result["group_name"] == self.GROUP

    def test_unknown_group_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        result = _call_tool(
            port,
            sid,
            "score_symbol_group",
            {
                "schematic_path": sch,
                "group_name": "nonexistent",
            },
        )
        assert "error" in result


# ===========================================================================
# Place — verify positions actually cluster
# ===========================================================================


class TestPlace:
    """PlaceSymbolGroup clusters members and persists positions."""

    GROUP = "int_test_place"

    def test_place_clusters_members(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3", "R4"],
                "group_name": self.GROUP,
            },
        )

        # Measure spread before placement
        before = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        spread_before = _member_spread(before["members"])

        # Place
        result = _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert "error" not in result, result
        assert result["placed_count"] == 4
        assert result["found_clear_position"] is True
        assert "backup_path" in result

        # Re-read — positions should be clustered
        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        spread_after = _member_spread(after["members"])

        # After placement, members should be close together
        assert spread_after < 40.0, (
            f"Expected clustered spread < 40 mm, got {spread_after:.1f} mm. "
            f"Before spread: {spread_before:.1f} mm"
        )

    def test_placed_positions_are_on_grid(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R1", "R2", "R3"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )

        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        for m in after["members"]:
            x_on_grid = abs(m["x"] / _GRID_MM - round(m["x"] / _GRID_MM)) < 1e-6
            y_on_grid = abs(m["y"] / _GRID_MM - round(m["y"] / _GRID_MM)) < 1e-6
            assert x_on_grid, f"{m['reference']} x={m['x']} not on {_GRID_MM}mm grid"
            assert y_on_grid, f"{m['reference']} y={m['y']} not on {_GRID_MM}mm grid"

    def test_place_creates_backup(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6"],
                "group_name": self.GROUP,
            },
        )
        result = _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert "error" not in result, result
        assert result["backup_path"] is not None
        assert os.path.isfile(result["backup_path"])


# ===========================================================================
# Move — verify positions actually change
# ===========================================================================


class TestMove:
    """MoveSymbolGroup rigidly translates a placed group."""

    GROUP = "int_test_move"

    def test_move_changes_anchor_position(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6", "R7"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )

        # Target: move the group to a new position
        target_x, target_y = 120.0, 150.0

        result = _call_tool(
            port,
            sid,
            "move_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "anchor_x": target_x,
                "anchor_y": target_y,
            },
        )
        assert "error" not in result, result
        assert result["moved_count"] == 3
        assert result["anchor_position"]["x"] == pytest.approx(target_x, abs=_GRID_MM)
        assert result["anchor_position"]["y"] == pytest.approx(target_y, abs=_GRID_MM)

    def test_move_persists_positions(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6", "R7"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )

        target_x, target_y = 120.0, 150.0
        _call_tool(
            port,
            sid,
            "move_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "anchor_x": target_x,
                "anchor_y": target_y,
            },
        )

        # Re-read: all members should be near the target
        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        for m in after["members"]:
            assert abs(m["x"] - target_x) < 60.0, (
                f"{m['reference']} x={m['x']} far from target {target_x}"
            )
            assert abs(m["y"] - target_y) < 60.0, (
                f"{m['reference']} y={m['y']} far from target {target_y}"
            )

    def test_move_creates_backup(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        result = _call_tool(
            port,
            sid,
            "move_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "anchor_x": 80.0,
                "anchor_y": 80.0,
            },
        )
        assert "error" not in result, result
        assert result["backup_path"] is not None
        assert os.path.isfile(result["backup_path"])


# ===========================================================================
# Rotate — verify positions and rotations change
# ===========================================================================


class TestRotate:
    """RotateSymbolGroup rotates a placed group around its anchor."""

    GROUP = "int_test_rotate"

    def test_rotate_changes_member_positions(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6", "R7"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )

        before = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        before_positions = {
            m["reference"]: (m["x"], m["y"], m["rotation"]) for m in before["members"]
        }

        result = _call_tool(
            port,
            sid,
            "rotate_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "rotation_delta": 90.0,
            },
        )
        assert "error" not in result, result
        assert result["rotated_count"] == 3
        assert result["rotation_delta"] == 90.0

        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        # At least one member should have changed position or rotation
        any_changed = False
        for m in after["members"]:
            bx, by, br = before_positions[m["reference"]]
            if abs(m["x"] - bx) > 0.1 or abs(m["y"] - by) > 0.1 or abs(m["rotation"] - br) > 0.1:
                any_changed = True
                break
        assert any_changed, (
            "Expected at least one member position/rotation to change after 90° rotation"
        )

    def test_zero_rotation_is_noop(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6"],
                "group_name": self.GROUP,
            },
        )
        place_result = _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert "error" not in place_result, place_result

        before = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        before_positions = {
            m["reference"]: (m["x"], m["y"], m["rotation"]) for m in before["members"]
        }

        result = _call_tool(
            port,
            sid,
            "rotate_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "rotation_delta": 0.0,
            },
        )
        assert "error" not in result, result
        assert result["rotation_delta"] == 0.0

        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        # Positions and rotations should be unchanged
        for m in after["members"]:
            bx, by, br = before_positions[m["reference"]]
            assert m["x"] == pytest.approx(bx, abs=0.01)
            assert m["y"] == pytest.approx(by, abs=0.01)
            assert m["rotation"] == pytest.approx(br, abs=0.01)

    def test_full_360_is_identity(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6"],
                "group_name": self.GROUP,
            },
        )
        _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )

        before = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        before_positions = {m["reference"]: (m["x"], m["y"]) for m in before["members"]}

        result = _call_tool(
            port,
            sid,
            "rotate_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "rotation_delta": 360.0,
            },
        )
        assert "error" not in result, result

        after = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        # Positions should return to original after 360° rotation
        for m in after["members"]:
            bx, by = before_positions[m["reference"]]
            assert m["x"] == pytest.approx(bx, abs=_GRID_MM), (
                f"{m['reference']} x={m['x']} != original {bx}"
            )
            assert m["y"] == pytest.approx(by, abs=_GRID_MM), (
                f"{m['reference']} y={m['y']} != original {by}"
            )


# ===========================================================================
# Full workflow round-trip
# ===========================================================================


class TestFullWorkflow:
    """Assign → Place → Move → Rotate → re-read and verify."""

    GROUP = "int_test_wf"

    def test_full_round_trip(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        # 1. Assign R5,R6,R7 to a group
        r_assign = _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["R5", "R6", "R7"],
                "group_name": self.GROUP,
            },
        )
        assert len(r_assign["assigned"]) == 3

        # 2. List — group should appear
        r_list = _call_tool(port, sid, "list_symbol_groups", {"schematic_path": sch})
        assert r_list["group_count"] >= 1

        # 3. Get — verify members
        r_get = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert r_get["member_count"] == 3
        member_refs = {m["reference"] for m in r_get["members"]}
        assert member_refs == {"R5", "R6", "R7"}

        # 4. Score — before placement, spread should be > 0 (scattered)
        r_score1 = _call_tool(
            port,
            sid,
            "score_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert r_score1["mean_spread_mm"] > 0

        # 5. Place — cluster the group
        r_place = _call_tool(
            port,
            sid,
            "place_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert r_place["placed_count"] == 3
        assert r_place["found_clear_position"] is True

        # 6. Score — after placement, members should be closer together
        r_score2 = _call_tool(
            port,
            sid,
            "score_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        assert r_score2["mean_spread_mm"] < r_score1["mean_spread_mm"], (
            f"Expected lower spread after placement, "
            f"got before={r_score1['mean_spread_mm']:.1f} after={r_score2['mean_spread_mm']:.1f}"
        )

        # 7. Move to a known position
        target_x, target_y = 50.0, 70.0
        r_move = _call_tool(
            port,
            sid,
            "move_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "anchor_x": target_x,
                "anchor_y": target_y,
            },
        )
        assert r_move["moved_count"] == 3
        assert r_move["anchor_position"]["x"] == pytest.approx(target_x, abs=_GRID_MM)
        assert r_move["anchor_position"]["y"] == pytest.approx(target_y, abs=_GRID_MM)

        # 8. Verify positions after move
        r_after_move = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        for m in r_after_move["members"]:
            assert abs(m["x"] - target_x) < 60.0, (
                f"{m['reference']} x={m['x']} far from target {target_x}"
            )
            assert abs(m["y"] - target_y) < 60.0, (
                f"{m['reference']} y={m['y']} far from target {target_y}"
            )

        # 9. Rotate 180°
        r_rotate = _call_tool(
            port,
            sid,
            "rotate_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "rotation_delta": 180.0,
            },
        )
        assert r_rotate["rotated_count"] == 3

        # 10. Rotate back 180° — return to original
        r_rotate2 = _call_tool(
            port,
            sid,
            "rotate_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
                "rotation_delta": 180.0,
            },
        )
        assert r_rotate2["rotated_count"] == 3

        r_final = _call_tool(
            port,
            sid,
            "get_symbol_group",
            {
                "schematic_path": sch,
                "group_name": self.GROUP,
            },
        )
        for m in r_final["members"]:
            assert abs(m["x"] - target_x) < 60.0, (
                f"{m['reference']} x={m['x']} far from target {target_x} after 360° rotation"
            )
            assert abs(m["y"] - target_y) < 60.0, (
                f"{m['reference']} y={m['y']} far from target {target_y} after 360° rotation"
            )


# ===========================================================================
# Error cases
# ===========================================================================


class TestErrorCases:
    """Verify proper error handling for all tools."""

    def test_non_sch_file_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        # Create a non-.kicad_sch file
        txt = tmp_path / "test.txt"
        txt.write_text("not a schematic")

        result = _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": str(txt),
                "references": ["R1"],
                "group_name": "g",
            },
        )
        assert "error" in result

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "list_symbol_groups",
            {
                "schematic_path": "/nonexistent/file.kicad_sch",
            },
        )
        assert "error" in result

    def test_unknown_group_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        for tool, args in [
            ("get_symbol_group", {"schematic_path": sch, "group_name": "nonexistent"}),
            ("score_symbol_group", {"schematic_path": sch, "group_name": "nonexistent"}),
            ("place_symbol_group", {"schematic_path": sch, "group_name": "nonexistent"}),
            (
                "move_symbol_group",
                {"schematic_path": sch, "group_name": "nonexistent", "anchor_x": 0, "anchor_y": 0},
            ),
            (
                "rotate_symbol_group",
                {"schematic_path": sch, "group_name": "nonexistent", "rotation_delta": 45},
            ),
        ]:
            result = _call_tool(port, sid, tool, args)
            assert "error" in result, f"Expected error for {tool} with unknown group, got: {result}"

    def test_assign_nonexistent_references(self, mcp_server, tmp_path):
        port, sid = mcp_server
        sch = _copy_sch(tmp_path)

        result = _call_tool(
            port,
            sid,
            "assign_symbols_to_group",
            {
                "schematic_path": sch,
                "references": ["U99", "U100"],
                "group_name": "g",
            },
        )
        assert "error" in result
