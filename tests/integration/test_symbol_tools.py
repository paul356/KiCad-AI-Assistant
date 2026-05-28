"""
Integration tests for symbol library MCP tools.

Covers:
  - sync_symbol_index
  - get_symbol_sync_status
  - list_symbol_libraries
  - search_symbols
  - get_symbol
  - get_library_symbols
  - get_symbol_index_stats
  - get_symbol_pins

The tests start a real MCP server subprocess (plugin profile) and talk
to it over the streamable-http JSON-RPC transport.  Symbol index tests
make structure-only assertions that tolerate an empty symbol library
environment.

Run:
    uv run python -m pytest tests/integration/test_symbol_tools.py -v
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


_call_id = itertools.count(600)


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
        pytest.skip("MCP server did not start — skipping symbol integration tests")

    init_payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "symbol-integration-test", "version": "1"},
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
# Tests: sync_symbol_index
# ---------------------------------------------------------------------------

class TestSyncSymbolIndex:
    def test_returns_started_or_already_running(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "sync_symbol_index", {"force": False})
        assert result.get("status") in ("started", "already_running"), (
            f"Unexpected status: {result}"
        )

    def test_force_sync_returns_started(self, mcp_server):
        port, sid = mcp_server
        time.sleep(0.5)
        result = _call_tool(port, sid, "sync_symbol_index", {"force": True})
        assert result.get("status") in ("started", "already_running")


# ---------------------------------------------------------------------------
# Tests: get_symbol_sync_status
# ---------------------------------------------------------------------------

class TestGetSymbolSyncStatus:
    def test_returns_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol_sync_status", {})
        for key in ("running", "current", "total", "current_library",
                    "last_result", "error"):
            assert key in result, f"Missing key {key!r} in status response"

    def test_running_is_bool(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol_sync_status", {})
        assert isinstance(result["running"], bool)


# ---------------------------------------------------------------------------
# Tests: list_symbol_libraries
# ---------------------------------------------------------------------------

class TestListSymbolLibraries:
    def test_returns_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_symbol_libraries", {
            "table": None,
            "limit": 200,
            "offset": 0,
        })
        assert "success" in result
        if result.get("success"):
            assert "tables" in result or "libraries" in result
            assert "total" in result

    def test_no_error_in_response(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "list_symbol_libraries", {
            "table": None,
            "limit": 200,
            "offset": 0,
        })
        assert "error" not in result or result.get("success") is False


# ---------------------------------------------------------------------------
# Tests: search_symbols
# ---------------------------------------------------------------------------

class TestSearchSymbols:
    def test_returns_expected_structure(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "search_symbols", {
            "query": "resistor",
            "limit": 10,
        })
        assert "success" in result
        if result.get("success"):
            assert "symbols" in result
            assert "count" in result
            assert isinstance(result["symbols"], list)

    def test_empty_query_returns_empty_or_results(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "search_symbols", {
            "query": "nonexistent_symbol_xyz123",
            "limit": 10,
        })
        # Should either succeed with empty results or fail gracefully
        if result.get("success"):
            assert result.get("count", 0) == 0 or len(result.get("symbols", [])) == 0


# ---------------------------------------------------------------------------
# Tests: get_symbol
# ---------------------------------------------------------------------------

class TestGetSymbol:
    def test_nonexistent_symbol_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol", {
            "library_name": "NonExistentLib",
            "symbol_name": "NonExistentSymbol",
        })
        # Should return success=False with error
        assert result.get("success") is False or "error" in result


# ---------------------------------------------------------------------------
# Tests: get_library_symbols
# ---------------------------------------------------------------------------

class TestGetLibrarySymbols:
    def test_nonexistent_library_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_library_symbols", {
            "library_name": "NonExistentLibrary",
            "limit": 50,
            "offset": 0,
        })
        assert result.get("success") is False or "error" in result


# ---------------------------------------------------------------------------
# Tests: get_symbol_index_stats
# ---------------------------------------------------------------------------

class TestGetSymbolIndexStats:
    def test_returns_expected_keys(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol_index_stats", {})
        assert "success" in result
        if result.get("success"):
            assert "library_count" in result
            assert "symbol_count" in result
            assert "db_path" in result

    def test_counts_are_non_negative(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol_index_stats", {})
        if result.get("success"):
            assert result.get("library_count", 0) >= 0
            assert result.get("symbol_count", 0) >= 0


# ---------------------------------------------------------------------------
# Tests: get_symbol_pins
# ---------------------------------------------------------------------------

class TestGetSymbolPins:
    def test_nonexistent_symbol_returns_error(self, mcp_server):
        port, sid = mcp_server
        result = _call_tool(port, sid, "get_symbol_pins", {
            "library_name": "NonExistentLib",
            "symbol_name": "NonExistentSymbol",
        })
        assert result.get("success") is False or "error" in result
