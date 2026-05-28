"""
Unit tests for kcaa/tools/version_tools.py.

Mocks version_manager functions so tests are self-contained.
"""

import asyncio
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

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
    """Register version tools against a mock MCP and return the captured dict."""
    from kcaa.tools.version_tools import register_version_tools
    mock = _MockMCP()
    register_version_tools(mock)
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
# save_file_version
# ---------------------------------------------------------------------------


class TestSaveFileVersion:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["save_file_version"]

    @patch("kcaa.tools.version_tools.save_version_snapshot")
    @patch("kcaa.tools.version_tools.list_versions", return_value=[])
    def test_save_new_version(self, mock_list, mock_save, tmp_path):
        """When saving a new version with no existing snapshots."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        snapshot_path = str(tmp_path / ".versions" / "design.kicad_sch.20260528_100000_000000")
        mock_save.return_value = snapshot_path

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["created"] is True
        assert result["version_id"] == "20260528_100000_000000"
        assert result["snapshot_path"] == snapshot_path

    @patch("kcaa.tools.version_tools.save_version_snapshot")
    @patch("kcaa.tools.version_tools.list_versions")
    def test_save_unchanged_file_reuses_snapshot(self, mock_list, mock_save, tmp_path):
        """When file content matches latest snapshot, reuse it."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        existing_id = "20260528_090000_000000"
        mock_list.return_value = [
            {"id": existing_id, "timestamp": "2026-05-28 09:00:00", "size_bytes": 7}
        ]
        
        snapshot_path = str(tmp_path / ".versions" / f"design.kicad_sch.{existing_id}")
        mock_save.return_value = snapshot_path

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["created"] is False
        assert result["version_id"] == existing_id

    @patch("kcaa.tools.version_tools.save_version_snapshot")
    @patch("kcaa.tools.version_tools.list_versions")
    def test_file_not_found(self, mock_list, mock_save, tmp_path):
        """When file does not exist."""
        test_file = tmp_path / "nonexistent.kicad_sch"
        
        mock_list.return_value = []
        mock_save.side_effect = FileNotFoundError(f"File not found: {test_file}")

        result = _run(self.fn(str(test_file), ctx=None))

        assert "error" in result
        assert "File not found" in result["error"]

    @patch("kcaa.tools.version_tools.save_version_snapshot")
    @patch("kcaa.tools.version_tools.list_versions", return_value=[])
    def test_os_error(self, mock_list, mock_save, tmp_path):
        """When save_version_snapshot raises OSError."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        mock_save.side_effect = OSError("Permission denied")

        result = _run(self.fn(str(test_file), ctx=None))

        assert "error" in result
        assert "Failed to save version" in result["error"]


# ---------------------------------------------------------------------------
# list_file_versions
# ---------------------------------------------------------------------------


class TestListFileVersions:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["list_file_versions"]

    @patch("kcaa.tools.version_tools.list_versions")
    def test_list_with_versions(self, mock_list, tmp_path):
        """When there are existing version snapshots."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        versions = [
            {"id": "20260528_100000_000000", "timestamp": "2026-05-28 10:00:00", "size_bytes": 100},
            {"id": "20260528_090000_000000", "timestamp": "2026-05-28 09:00:00", "size_bytes": 90},
        ]
        mock_list.return_value = versions

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["versions"]) == 2
        assert result["versions"][0]["id"] == "20260528_100000_000000"

    @patch("kcaa.tools.version_tools.list_versions", return_value=[])
    def test_list_empty(self, mock_list, tmp_path):
        """When there are no version snapshots."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["count"] == 0
        assert result["versions"] == []

    @patch("kcaa.tools.version_tools.list_versions", return_value=[])
    def test_current_file_info(self, mock_list, tmp_path):
        """When file exists, current info is included."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["current"] is not None
        assert "timestamp" in result["current"]
        assert "size_bytes" in result["current"]
        assert result["current"]["size_bytes"] == 7

    @patch("kcaa.tools.version_tools.list_versions")
    def test_current_file_missing(self, mock_list, tmp_path):
        """When file doesn't exist but snapshots do, current is None."""
        test_file = tmp_path / "nonexistent.kicad_sch"
        
        mock_list.return_value = [
            {"id": "20260528_100000_000000", "timestamp": "2026-05-28 10:00:00", "size_bytes": 100}
        ]

        result = _run(self.fn(str(test_file), ctx=None))

        assert result["success"] is True
        assert result["current"] is None
        assert result["count"] == 1

    @patch("kcaa.tools.version_tools.list_versions")
    def test_os_error(self, mock_list, tmp_path):
        """When list_versions raises OSError."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        mock_list.side_effect = OSError("Permission denied")

        result = _run(self.fn(str(test_file), ctx=None))

        assert "error" in result
        assert "Failed to list versions" in result["error"]


# ---------------------------------------------------------------------------
# restore_file_version
# ---------------------------------------------------------------------------


class TestRestoreFileVersion:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["restore_file_version"]

    @patch("kcaa.tools.version_tools.restore_version")
    def test_restore_success(self, mock_restore, tmp_path):
        """When restore succeeds."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        version_id = "20260528_090000_000000"
        backup_path = str(tmp_path / ".versions" / "design.kicad_sch.20260528_100000_000000")
        
        mock_restore.return_value = {
            "restored_from": version_id,
            "backup_of_current": backup_path,
        }

        result = _run(self.fn(str(test_file), version_id, ctx=None))

        assert result["success"] is True
        assert result["restored_from"] == version_id
        assert result["backup_of_current"] == backup_path

    @patch("kcaa.tools.version_tools.restore_version")
    def test_file_not_found(self, mock_restore, tmp_path):
        """When file does not exist."""
        test_file = tmp_path / "nonexistent.kicad_sch"
        
        mock_restore.side_effect = FileNotFoundError(f"File not found: {test_file}")

        result = _run(self.fn(str(test_file), "20260528_090000_000000", ctx=None))

        assert "error" in result
        assert "File not found" in result["error"]

    @patch("kcaa.tools.version_tools.restore_version")
    def test_version_not_found(self, mock_restore, tmp_path):
        """When version snapshot does not exist."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        mock_restore.side_effect = FileNotFoundError("Version 'invalid' not found")

        result = _run(self.fn(str(test_file), "invalid", ctx=None))

        assert "error" in result
        assert "not found" in result["error"].lower()

    @patch("kcaa.tools.version_tools.restore_version")
    def test_os_error(self, mock_restore, tmp_path):
        """When restore_version raises OSError."""
        test_file = tmp_path / "design.kicad_sch"
        test_file.write_text("content")
        
        mock_restore.side_effect = OSError("Permission denied")

        result = _run(self.fn(str(test_file), "20260528_090000_000000", ctx=None))

        assert "error" in result
        assert "Failed to restore version" in result["error"]
