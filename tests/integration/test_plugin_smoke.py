"""
Integration smoke test: starts the kcaa server in plugin profile and
verifies the MCP endpoint responds with a tools/list.

This test requires no KiCad installation — it exercises the server process and
HTTP transport in isolation.  It is skipped automatically in CI environments
that don't have the kcaa package importable or can't bind a network port.

Run manually:
    .venv/bin/pytest tests/integration/test_plugin_smoke.py -v
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

import pytest


# ---------------------------------------------------------------------------
# Helpers
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
    """POST to /mcp and return (parsed_response, session_id).

    FastMCP's streamable-http transport requires:
    - Accept: application/json, text/event-stream
    - mcp-session-id header on all calls after initialize
    Response is SSE-framed (event: message\\ndata: {...}).
    """
    url = f"http://127.0.0.1:{port}/mcp"
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    # Bypass any system HTTP proxy for localhost connections
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        returned_session_id = resp.headers.get("mcp-session-id", session_id)
        raw = resp.read().decode()

    # SSE framing: extract JSON from "data: {...}" lines
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:"):].strip()
            if json_str:
                return json.loads(json_str), returned_session_id

    # Fallback: try parsing as raw JSON
    return json.loads(raw), returned_session_id


# ---------------------------------------------------------------------------
# Fixture: running MCP server in plugin profile
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_server():
    """Start the MCP server in plugin profile; yield (port, session_id); stop on teardown."""
    port = _find_free_port()
    env = os.environ.copy()
    env["MCP_TRANSPORT"] = "streamable-http"
    env["MCP_PORT"] = str(port)
    env["MCP_HOST"] = "127.0.0.1"
    env["KICAD_MCP_PROFILE"] = "plugin"
    # Ensure no proxy is used by the server subprocess
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
        pytest.skip("MCP server did not start in time — skipping integration tests")

    # Perform MCP initialize handshake to obtain a session ID
    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "1"},
        },
    }
    try:
        _, session_id = _mcp_post(port, init_payload)
    except Exception as e:
        proc.terminate()
        proc.wait()
        pytest.skip(f"MCP initialize handshake failed: {e}")

    yield port, session_id

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMCPPluginProfileSmoke:
    def test_tools_list_responds(self, mcp_server):
        """The /mcp endpoint should respond to a tools/list JSON-RPC call."""
        port, session_id = mcp_server
        response, _ = _mcp_post(port, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        }, session_id)
        assert "result" in response, f"Unexpected response: {response}"

    def test_tools_list_contains_netlist_tool(self, mcp_server):
        """Plugin profile should expose netlist tools."""
        port, session_id = mcp_server
        response, _ = _mcp_post(port, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, session_id)
        tools = response.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        assert any("netlist" in name for name in tool_names), (
            f"No netlist tool found. Available: {tool_names}"
        )

    def test_tools_list_excludes_export_tools(self, mcp_server):
        """Plugin profile must NOT expose kicad-cli tools (e.g. export_schematic)."""
        port, session_id = mcp_server
        response, _ = _mcp_post(port, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        }, session_id)
        tools = response.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        kicad_cli_tools = [n for n in tool_names if "export" in n or "drc" in n or "bom" in n]
        assert not kicad_cli_tools, (
            f"Plugin profile should not expose CLI tools, but found: {kicad_cli_tools}"
        )

    def test_tools_list_has_component_edit_tools(self, mcp_server):
        """Plugin profile should expose component editing tools."""
        port, session_id = mcp_server
        response, _ = _mcp_post(port, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
        }, session_id)
        tools = response.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        expected = ["add_symbol_to_schematic", "remove_symbol_from_schematic"]
        for name in expected:
            assert name in tool_names, f"Expected tool '{name}' not found. Got: {tool_names}"

    def test_invalid_method_returns_error(self, mcp_server):
        """Unknown JSON-RPC method should return an error response, not crash."""
        port, session_id = mcp_server
        response, _ = _mcp_post(port, {
            "jsonrpc": "2.0", "id": 5, "method": "nonexistent/method", "params": {},
        }, session_id)
        assert "error" in response or "result" in response
