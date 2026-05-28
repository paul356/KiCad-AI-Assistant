"""
Unit tests for kicad_mcp/tools/kipy_tools.py.

Mocks kipy module and related dependencies so tests are self-contained.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# MockMCP — captures @mcp.tool()-decorated coroutines
# ---------------------------------------------------------------------------

class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _get_tools() -> dict:
    """Register kipy tools against a mock MCP and return the captured dict."""
    from kicad_mcp.tools.kipy_tools import register_kipy_tools
    mock = _MockMCP()
    register_kipy_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tools():
    return _get_tools()


@pytest.fixture()
def mock_ctx():
    ctx = MagicMock()
    ctx.info = MagicMock()
    ctx.report_progress = MagicMock(return_value=asyncio.sleep(0))
    return ctx


# ---------------------------------------------------------------------------
# _find_kicad_socket (tested indirectly through tool behavior)
# ---------------------------------------------------------------------------


class TestFindKicadSocket:
    """Test socket discovery logic by calling tools that use it."""

    @patch.dict(os.environ, {"KICAD_API_SOCKET": "ipc:///custom/socket.sock"})
    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_socket_from_env_var(self, mock_connect, tools):
        """When KICAD_API_SOCKET is set, use it."""
        mock_kicad = MagicMock()
        mock_kicad.ping.return_value = None
        mock_connect.return_value = mock_kicad

        fn = tools["check_kicad_ipc_connection"]
        result = _run(fn(ctx=None))

        assert result["success"] is True
        assert result["socket_path"] == "ipc:///custom/socket.sock"

    @patch.dict(os.environ, {}, clear=True)
    @patch("kicad_mcp.tools.kipy_tools.glob.glob")
    @patch("kicad_mcp.tools.kipy_tools.os.path.getmtime")
    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_socket_from_glob_linux(self, mock_connect, mock_getmtime, mock_glob, tools):
        """When no env var, glob for socket files and pick newest."""
        # Remove KICAD_API_SOCKET if it exists
        os.environ.pop("KICAD_API_SOCKET", None)
        
        mock_glob.return_value = ["/tmp/kicad/api-123.sock", "/tmp/kicad/api-456.sock"]
        mock_getmtime.side_effect = [1000, 2000]  # second is newer
        
        mock_kicad = MagicMock()
        mock_kicad.ping.return_value = None
        mock_connect.return_value = mock_kicad

        fn = tools["check_kicad_ipc_connection"]
        result = _run(fn(ctx=None))

        assert result["success"] is True
        assert "api-456.sock" in result["socket_path"]

    @patch.dict(os.environ, {}, clear=True)
    @patch("kicad_mcp.tools.kipy_tools.glob.glob", return_value=[])
    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_socket_default_fallback(self, mock_connect, mock_glob, tools):
        """When no env var and no socket files, use default."""
        os.environ.pop("KICAD_API_SOCKET", None)
        
        mock_kicad = MagicMock()
        mock_kicad.ping.return_value = None
        mock_connect.return_value = mock_kicad

        fn = tools["check_kicad_ipc_connection"]
        result = _run(fn(ctx=None))

        assert result["success"] is True
        assert result["socket_path"] == "ipc:///tmp/kicad/api.sock"


# ---------------------------------------------------------------------------
# check_kicad_ipc_connection
# ---------------------------------------------------------------------------


class TestCheckKicadIpcConnection:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["check_kicad_ipc_connection"]

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_connected_successfully(self, mock_connect):
        """When kipy connects and ping succeeds, return connected=True."""
        mock_kicad = MagicMock()
        mock_kicad.ping.return_value = None
        mock_connect.return_value = mock_kicad

        result = _run(self.fn(ctx=None))

        assert result["success"] is True
        assert result["connected"] is True
        assert "socket_path" in result

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_ping_timeout_still_connected(self, mock_connect):
        """When ping times out but socket exists, still consider connected."""
        # Create a mock kipy.errors module
        mock_kipy_errors = MagicMock()
        
        class MockConnectionError(Exception):
            pass
        
        mock_kipy_errors.ConnectionError = MockConnectionError
        
        # Mock the kipy module
        mock_kipy = MagicMock()
        mock_kipy.errors = mock_kipy_errors
        
        with patch.dict(sys.modules, {"kipy": mock_kipy, "kipy.errors": mock_kipy_errors}):
            mock_kicad = MagicMock()
            mock_kicad.ping.side_effect = MockConnectionError("Connection timed out")
            mock_connect.return_value = mock_kicad

            result = _run(self.fn(ctx=None))

            assert result["success"] is True
            assert result["connected"] is True
            assert "warning" in result

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_connection_refused(self, mock_connect):
        """When connection is refused, return connected=False."""
        mock_kipy_errors = MagicMock()
        
        class MockConnectionError(Exception):
            pass
        
        mock_kipy_errors.ConnectionError = MockConnectionError
        
        mock_kipy = MagicMock()
        mock_kipy.errors = mock_kipy_errors
        
        with patch.dict(sys.modules, {"kipy": mock_kipy, "kipy.errors": mock_kipy_errors}):
            mock_kicad = MagicMock()
            mock_kicad.ping.side_effect = MockConnectionError("Connection refused")
            mock_connect.return_value = mock_kicad

            result = _run(self.fn(ctx=None))

            assert result["success"] is True
            assert result["connected"] is False
            assert "error" in result

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_kipy_not_installed(self, mock_connect):
        """When kipy import fails, return error message."""
        mock_connect.side_effect = RuntimeError("kicad-python is not installed")

        result = _run(self.fn(ctx=None))

        assert result["success"] is True
        assert result["connected"] is False
        assert "not installed" in result["error"]

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_unexpected_exception(self, mock_connect):
        """When unexpected error occurs, return error."""
        mock_connect.side_effect = RuntimeError("Unexpected error")

        result = _run(self.fn(ctx=None))

        assert result["success"] is True
        assert result["connected"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# save_document
# ---------------------------------------------------------------------------


class TestSaveDocument:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["save_document"]

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_save_pcb_success(self, mock_connect):
        """When saving a PCB file successfully."""
        mock_kicad = MagicMock()
        mock_connect.return_value = mock_kicad

        # Mock kipy imports
        mock_document_type = MagicMock()
        mock_document_type.DOCTYPE_PCB = "pcb"
        
        mock_board_doc = MagicMock()
        mock_kicad.get_open_documents.return_value = [mock_board_doc]
        
        mock_board = MagicMock()
        mock_board.save.return_value = None

        with patch.dict(sys.modules, {
            "kipy": MagicMock(),
            "kipy.proto": MagicMock(),
            "kipy.proto.common": MagicMock(),
            "kipy.proto.common.types": MagicMock(DocumentType=mock_document_type),
            "kipy.board": MagicMock(Board=MagicMock(return_value=mock_board)),
        }):
            result = _run(self.fn("/path/to/board.kicad_pcb", ctx=None))

            assert result["success"] is True
            assert result["document_type"] == "pcb"

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_save_schematic_success(self, mock_connect):
        """When saving a schematic file successfully."""
        mock_kicad = MagicMock()
        mock_connect.return_value = mock_kicad

        mock_document_type = MagicMock()
        mock_document_type.DOCTYPE_SCHEMATIC = "schematic"
        
        mock_sch_doc = MagicMock()
        mock_kicad.get_open_documents.return_value = [mock_sch_doc]
        
        mock_schematic = MagicMock()
        mock_schematic.save.return_value = None

        with patch.dict(sys.modules, {
            "kipy": MagicMock(),
            "kipy.proto": MagicMock(),
            "kipy.proto.common": MagicMock(),
            "kipy.proto.common.types": MagicMock(DocumentType=mock_document_type),
            "kipy.schematic": MagicMock(Schematic=MagicMock(return_value=mock_schematic)),
        }):
            result = _run(self.fn("/path/to/design.kicad_sch", ctx=None))

            assert result["success"] is True
            assert result["document_type"] == "schematic"

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_no_pcb_open(self, mock_connect):
        """When no PCB is open in KiCad."""
        mock_kicad = MagicMock()
        mock_connect.return_value = mock_kicad

        mock_document_type = MagicMock()
        mock_document_type.DOCTYPE_PCB = "pcb"
        
        mock_kicad.get_open_documents.return_value = []

        with patch.dict(sys.modules, {
            "kipy": MagicMock(),
            "kipy.proto": MagicMock(),
            "kipy.proto.common": MagicMock(),
            "kipy.proto.common.types": MagicMock(DocumentType=mock_document_type),
        }):
            result = _run(self.fn("/path/to/board.kicad_pcb", ctx=None))

            assert result["success"] is False
            assert "No PCB is currently open" in result["error"]

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_no_schematic_open(self, mock_connect):
        """When no schematic is open in KiCad."""
        mock_kicad = MagicMock()
        mock_connect.return_value = mock_kicad

        mock_document_type = MagicMock()
        mock_document_type.DOCTYPE_SCHEMATIC = "schematic"
        
        mock_kicad.get_open_documents.return_value = []

        # Create proper mock modules
        mock_kipy_proto_common_types = MagicMock()
        mock_kipy_proto_common_types.DocumentType = mock_document_type
        
        mock_kipy_proto_common = MagicMock()
        mock_kipy_proto_common.types = mock_kipy_proto_common_types
        
        mock_kipy_proto = MagicMock()
        mock_kipy_proto.common = mock_kipy_proto_common
        
        mock_kipy_schematic = MagicMock()
        mock_kipy_schematic.Schematic = MagicMock()
        
        mock_kipy = MagicMock()
        mock_kipy.proto = mock_kipy_proto
        mock_kipy.schematic = mock_kipy_schematic

        with patch.dict(sys.modules, {
            "kipy": mock_kipy,
            "kipy.proto": mock_kipy_proto,
            "kipy.proto.common": mock_kipy_proto_common,
            "kipy.proto.common.types": mock_kipy_proto_common_types,
            "kipy.schematic": mock_kipy_schematic,
        }):
            result = _run(self.fn("/path/to/design.kicad_sch", ctx=None))

            assert result["success"] is False
            assert "No schematic is currently open" in result["error"]

    def test_unsupported_file_type(self):
        """When file extension is not supported."""
        result = _run(self.fn("/path/to/file.txt", ctx=None))

        assert result["success"] is False
        assert "Unsupported file type" in result["error"]

    @patch("kicad_mcp.tools.kipy_tools._connect")
    def test_kicad_not_running(self, mock_connect):
        """When KiCad is not running or IPC unavailable."""
        mock_connect.side_effect = RuntimeError("Not connected to KiCad")

        result = _run(self.fn("/path/to/board.kicad_pcb", ctx=None))

        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# reload_kicad
# ---------------------------------------------------------------------------


class TestReloadKicad:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["reload_kicad"]

    @patch("kicad_mcp.utils.kipy_reload.try_reload_schematic_in_kicad", return_value=True)
    def test_reload_schematic_success(self, mock_reload):
        """When schematic reload succeeds."""
        result = _run(self.fn(["/path/to/design.kicad_sch"], ctx=None))

        assert result["success"] is True
        assert "/path/to/design.kicad_sch" in result["reloaded"]
        assert result["failed"] == []

    @patch("kicad_mcp.utils.kipy_reload.try_reload_pcb_in_kicad", return_value=None)
    def test_reload_pcb_success(self, mock_reload):
        """When PCB reload succeeds (no exception)."""
        result = _run(self.fn(["/path/to/board.kicad_pcb"], ctx=None))

        assert result["success"] is True
        assert "/path/to/board.kicad_pcb" in result["reloaded"]
        assert result["failed"] == []

    @patch("kicad_mcp.utils.kipy_reload.try_reload_schematic_in_kicad", return_value=False)
    def test_reload_schematic_failure(self, mock_reload):
        """When schematic reload fails (returns False)."""
        result = _run(self.fn(["/path/to/design.kicad_sch"], ctx=None))

        assert result["success"] is False
        assert "/path/to/design.kicad_sch" in result["failed"]
        assert "errors" in result
        assert "automatically" in result["errors"]["/path/to/design.kicad_sch"].lower()

    @patch("kicad_mcp.utils.kipy_reload.try_reload_pcb_in_kicad", side_effect=RuntimeError("IPC error"))
    def test_reload_pcb_failure(self, mock_reload):
        """When PCB reload raises exception."""
        result = _run(self.fn(["/path/to/board.kicad_pcb"], ctx=None))

        assert result["success"] is False
        assert "/path/to/board.kicad_pcb" in result["failed"]
        assert "errors" in result
        assert "IPC error" in result["errors"]["/path/to/board.kicad_pcb"]

    def test_unsupported_extension(self):
        """When file extension is not supported."""
        result = _run(self.fn(["/path/to/file.txt"], ctx=None))

        assert result["success"] is False
        assert "/path/to/file.txt" in result["failed"]
        assert "errors" in result
        assert "Unsupported file extension" in result["errors"]["/path/to/file.txt"]

    @patch("kicad_mcp.utils.kipy_reload.try_reload_schematic_in_kicad", return_value=True)
    @patch("kicad_mcp.utils.kipy_reload.try_reload_pcb_in_kicad", side_effect=RuntimeError("error"))
    def test_mixed_paths(self, mock_pcb, mock_sch):
        """When some paths succeed and others fail."""
        paths = [
            "/path/to/design.kicad_sch",
            "/path/to/board.kicad_pcb",
            "/path/to/file.txt",
        ]
        result = _run(self.fn(paths, ctx=None))

        assert result["success"] is False
        assert "/path/to/design.kicad_sch" in result["reloaded"]
        assert "/path/to/board.kicad_pcb" in result["failed"]
        assert "/path/to/file.txt" in result["failed"]
        assert len(result["reloaded"]) == 1
        assert len(result["failed"]) == 2

    def test_empty_paths(self):
        """When paths list is empty."""
        result = _run(self.fn([], ctx=None))

        assert result["success"] is True
        assert result["reloaded"] == []
        assert result["failed"] == []
