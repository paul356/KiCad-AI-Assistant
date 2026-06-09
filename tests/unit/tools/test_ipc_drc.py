"""
Unit tests for kcaa/utils/ipc_drc.py.
Mocks kipy so the IPC DRC runner can be tested without a running KiCad instance.
"""

from unittest.mock import MagicMock, patch

import pytest

from kcaa.utils import ipc_drc


def _make_kicad_mock(*, board_open=True):
    mock = MagicMock()
    mock.get_board.return_value = MagicMock() if board_open else None
    mock.run_action.return_value = MagicMock()
    return mock


class TestRunDrcViaIpc:
    @pytest.mark.asyncio
    async def test_success_opens_drc_dialog(self):
        mock_kicad = _make_kicad_mock()
        with patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")
        assert result["success"] is True
        assert result["pcb_file"] == "/tmp/test.kicad_pcb"
        mock_kicad.run_action.assert_called_once_with("pcbnew.DRCTool.runDRC")

    @pytest.mark.asyncio
    async def test_no_board_open(self):
        mock_kicad = _make_kicad_mock(board_open=False)
        with patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")
        assert result["success"] is False
        assert "No board" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_kicad_not_running(self):
        with patch(
            "kcaa.tools.kipy_tools._connect",
            side_effect=RuntimeError("KiCad not running"),
        ):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")
        assert result["success"] is False
        assert "KiCad connection failed" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_passes_context(self):
        mock_kicad = _make_kicad_mock()
        with patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb", ctx=None)
        assert result["success"] is True
