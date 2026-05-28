"""
Integration tests for version management MCP tools.

Covers:
  - save_file_version
  - list_file_versions
  - restore_file_version

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Version snapshots
are created in temporary directories so tests are fully isolated.

Run:
    uv run python -m pytest tests/integration/test_version_tools.py -v
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
# Transport helpers (same pattern as test_pcb_tools.py)
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
    with opener.open(req, timeout=15) as resp:
        returned_session_id = resp.headers.get("mcp-session-id", session_id)
        raw = resp.read().decode()

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:"):].strip()
            if json_str:
                return json.loads(json_str), returned_session_id

    return json.loads(raw), returned_session_id


_call_id = itertools.count(500)


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
        [sys.executable, "-m", "kicad_mcp.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_for_port(port, timeout=20):
        proc.terminate()
        proc.wait()
        pytest.skip("MCP server did not start — skipping version integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "version-integration-test", "version": "1"},
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
# Helper: create a temp file with known content
# ---------------------------------------------------------------------------

def _make_test_file(tmp_path, name: str = "test.kicad_sch", content: str = "version 1 content") -> str:
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# Tests: save_file_version
# ---------------------------------------------------------------------------

class TestSaveFileVersion:
    def test_saves_version_successfully(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        result = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        assert "error" not in result, result
        assert result.get("success") is True
        assert "version_id" in result
        assert "snapshot_path" in result

    def test_snapshot_file_exists(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        result = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        assert "error" not in result
        snapshot = result.get("snapshot_path", "")
        assert snapshot and os.path.isfile(snapshot), f"Snapshot not found at {snapshot!r}"

    def test_multiple_saves_create_distinct_versions(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        r1 = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        # Modify file before second save
        with open(fp, "w") as f:
            f.write("version 2 content")
        r2 = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        assert "error" not in r1
        assert "error" not in r2
        assert r1["version_id"] != r2["version_id"]

    def test_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "save_file_version", {
            "file_path": "/nonexistent/path/test.kicad_sch",
        })
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: list_file_versions
# ---------------------------------------------------------------------------

class TestListFileVersions:
    def test_lists_saved_versions(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        _call_tool(port, sid, "save_file_version", {"file_path": fp})
        result = _call_tool(port, sid, "list_file_versions", {"file_path": fp})
        assert "error" not in result, result
        assert result.get("success") is True
        assert result.get("count", 0) >= 1
        assert len(result.get("versions", [])) >= 1

    def test_version_entry_has_expected_keys(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        _call_tool(port, sid, "save_file_version", {"file_path": fp})
        result = _call_tool(port, sid, "list_file_versions", {"file_path": fp})
        assert "error" not in result
        versions = result.get("versions", [])
        assert len(versions) >= 1
        v = versions[0]
        assert "id" in v
        assert "timestamp" in v
        assert "size_bytes" in v

    def test_current_file_info_present(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        result = _call_tool(port, sid, "list_file_versions", {"file_path": fp})
        assert "error" not in result
        current = result.get("current")
        assert current is not None
        assert "timestamp" in current
        assert "size_bytes" in current

    def test_no_versions_returns_empty_list(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        result = _call_tool(port, sid, "list_file_versions", {"file_path": fp})
        assert "error" not in result
        assert result.get("count") == 0
        assert result.get("versions") == []


# ---------------------------------------------------------------------------
# Tests: restore_file_version
# ---------------------------------------------------------------------------

class TestRestoreFileVersion:
    def test_restore_reverts_file_content(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path, content="original content")
        save_result = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        assert "error" not in save_result
        version_id = save_result["version_id"]

        # Modify the file
        with open(fp, "w") as f:
            f.write("modified content")

        # Restore
        result = _call_tool(port, sid, "restore_file_version", {
            "file_path": fp,
            "version_id": version_id,
        })
        assert "error" not in result, result
        assert result.get("success") is True

        # Verify content is restored
        with open(fp) as f:
            assert f.read() == "original content"

    def test_restore_creates_backup_of_current(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path, content="v1")
        save_result = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        version_id = save_result["version_id"]

        with open(fp, "w") as f:
            f.write("v2")

        result = _call_tool(port, sid, "restore_file_version", {
            "file_path": fp,
            "version_id": version_id,
        })
        assert "error" not in result
        backup = result.get("backup_of_current", "")
        assert backup and os.path.isfile(backup), f"Backup not found at {backup!r}"

    def test_restore_invalid_version_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        fp = _make_test_file(tmp_path)
        result = _call_tool(port, sid, "restore_file_version", {
            "file_path": fp,
            "version_id": "nonexistent-version-id",
        })
        assert "error" in result

    def test_restore_nonexistent_file_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "restore_file_version", {
            "file_path": "/nonexistent/path/test.kicad_sch",
            "version_id": "some-id",
        })
        assert "error" in result

    def test_save_list_restore_roundtrip(self, mcp_server, tmp_path):
        """Full workflow: save → modify → list → restore → verify."""
        port, sid = mcp_server
        fp = _make_test_file(tmp_path, content="roundtrip v1")

        # Save version
        save = _call_tool(port, sid, "save_file_version", {"file_path": fp})
        assert "error" not in save
        vid = save["version_id"]

        # Modify
        with open(fp, "w") as f:
            f.write("roundtrip v2")

        # List versions
        lst = _call_tool(port, sid, "list_file_versions", {"file_path": fp})
        assert "error" not in lst
        assert lst["count"] >= 1
        assert any(v["id"] == vid for v in lst["versions"])

        # Restore
        restore = _call_tool(port, sid, "restore_file_version", {
            "file_path": fp,
            "version_id": vid,
        })
        assert "error" not in restore

        # Verify
        with open(fp) as f:
            assert f.read() == "roundtrip v1"
