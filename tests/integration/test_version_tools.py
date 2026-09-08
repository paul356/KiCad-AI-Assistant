"""
Integration tests for version management MCP tools.

Covers:
  - save_project_version
  - list_project_versions
  - restore_project_version

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
            json_str = line[len("data:") :].strip()
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
# Tests: save_project_version / list_project_versions / restore_project_version
# ---------------------------------------------------------------------------


def _make_project(tmp_path, name: str = "board") -> str:
    """Create a minimal KiCad project (pro + sch + pcb) and return the pro path."""
    pro = tmp_path / f"{name}.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 1}}))
    (tmp_path / f"{name}.kicad_sch").write_text("sch v1\n")
    (tmp_path / f"{name}.kicad_pcb").write_text("pcb v1\n")
    return str(pro)


class TestSaveProjectVersion:
    def test_saves_project_archive(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pro = _make_project(tmp_path)
        result = _call_tool(port, sid, "save_project_version", {"project_file": pro})
        assert "error" not in result, result
        assert result.get("success") is True
        assert "version_id" in result
        assert result.get("archive_path", "").endswith(".tar.gz")
        assert set(result.get("files", [])) == {
            "board.kicad_pro",
            "board.kicad_sch",
            "board.kicad_pcb",
        }

    def test_invalid_path_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(
            port,
            sid,
            "save_project_version",
            {"project_file": "/nonexistent/path/project.kicad_pro"},
        )
        assert "error" in result


class TestListProjectVersions:
    def test_lists_project_versions(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pro = _make_project(tmp_path)
        _call_tool(port, sid, "save_project_version", {"project_file": pro})
        result = _call_tool(port, sid, "list_project_versions", {"project_file": pro})
        assert "error" not in result, result
        assert result.get("success") is True
        assert result.get("count", 0) >= 1
        v = result["versions"][0]
        assert "id" in v and "timestamp" in v and "size_bytes" in v
        assert set(v.get("files", [])) == {
            "board.kicad_pro",
            "board.kicad_sch",
            "board.kicad_pcb",
        }

    def test_no_versions_returns_empty(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pro = _make_project(tmp_path)
        result = _call_tool(port, sid, "list_project_versions", {"project_file": pro})
        assert "error" not in result
        assert result.get("count") == 0


class TestRestoreProjectVersion:
    def test_restore_project_round_trip(self, mcp_server, tmp_path):
        """save -> modify sch+pcb+pro -> restore -> all files revert together."""
        port, sid = mcp_server
        pro = _make_project(tmp_path)
        saved = _call_tool(port, sid, "save_project_version", {"project_file": pro})
        version_id = saved["version_id"]

        pro_path = tmp_path / "board.kicad_pro"
        pro_path.write_text(json.dumps({"meta": {"filename": "board.kicad_pro", "version": 2}}))
        (tmp_path / "board.kicad_sch").write_text("sch v2\n")
        (tmp_path / "board.kicad_pcb").write_text("pcb v2\n")

        restored = _call_tool(
            port,
            sid,
            "restore_project_version",
            {"project_file": str(pro_path), "version_id": version_id},
        )
        assert "error" not in restored, restored
        assert restored.get("restored_from") == version_id
        assert set(restored.get("files", [])) == {
            "board.kicad_pro",
            "board.kicad_sch",
            "board.kicad_pcb",
        }
        assert (tmp_path / "board.kicad_sch").read_text() == "sch v1\n"
        assert (tmp_path / "board.kicad_pcb").read_text() == "pcb v1\n"
        assert json.loads(pro_path.read_text())["meta"]["version"] == 1

    def test_restore_invalid_version_returns_error(self, mcp_server, tmp_path):
        port, sid = mcp_server
        pro = _make_project(tmp_path)
        result = _call_tool(
            port,
            sid,
            "restore_project_version",
            {"project_file": pro, "version_id": "nonexistent-id"},
        )
        assert "error" in result
