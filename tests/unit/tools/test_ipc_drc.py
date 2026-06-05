"""
Unit tests for kcaa/tools/drc_impl/ipc_drc.py.

Mocks kipy and pcbnew so the IPC DRC runner can be tested without a
running KiCad instance.
"""

from unittest.mock import MagicMock, patch

import pytest

from kcaa.tools.drc_impl import ipc_drc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMarker:
    """A mock PCB_MARKER compatible with the markers parsed in ipc_drc."""

    def __init__(self, description: str = "", severity: int = 0, x: float = 10.0, y: float = 20.0):
        self._desc = description
        self._sev = severity
        self._pos_x = int(x * 1_000_000)  # pcbnew uses nanometers internally
        self._pos_y = int(y * 1_000_000)

    def GetDescription(self) -> str:
        return self._desc

    def GetSeverity(self) -> int:
        return self._sev

    def GetPosition(self):
        pos = MagicMock()
        pos.x = self._pos_x
        pos.y = self._pos_y
        return pos


def _make_kicad_mock(*, board_open: bool = True):
    """Return a mock kipy.KiCad instance."""
    mock = MagicMock()
    if board_open:
        mock.get_board.return_value = MagicMock()
    else:
        mock.get_board.return_value = None
    mock.run_action.return_value = MagicMock()
    return mock


def _make_pcbnew_mock(markers: list[_FakeMarker] | None = None):
    """Return a mock pcbnew module with GetBoard() → board with GetMarkers()."""
    board = MagicMock()
    board.GetMarkers.return_value = markers or []

    pcbnew_mock = MagicMock()
    pcbnew_mock.GetBoard.return_value = board
    pcbnew_mock.ToMM = lambda val: val / 1_000_000.0
    return pcbnew_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunDrcViaIpc:
    """Tests for run_drc_via_ipc.

    All tests use lazy imports inside the function under test, so we must
    patch at the origin (kcaa.tools.kipy_tools._connect) rather than the
    import site (drc_impl.ipc_drc).
    """

    @pytest.mark.asyncio
    async def test_success_with_violations(self):
        """DRC runs successfully, returns parsed violations."""
        marker_1 = _FakeMarker("Clearance violation", severity=0, x=5.0, y=10.0)
        marker_2 = _FakeMarker("Track width violation", severity=1, x=15.0, y=25.0)
        fake_markers = [marker_1, marker_2]

        mock_kicad = _make_kicad_mock()
        mock_pcbnew = _make_pcbnew_mock(fake_markers)

        with (
            patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad),
            patch.dict("sys.modules", {"pcbnew": mock_pcbnew}),
        ):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["success"] is True
        assert result["total_violations"] == 2
        assert len(result["violations"]) == 2
        assert result["violation_categories"]["Clearance violation"] == 1
        assert result["violation_categories"]["Track width violation"] == 1

        # Verify first violation
        v0 = result["violations"][0]
        assert v0["message"] == "Clearance violation"
        assert v0["severity"] == "error"
        assert v0["location"]["x"] == 5.0
        assert v0["location"]["y"] == 10.0

        # Verify second violation
        v1 = result["violations"][1]
        assert v1["severity"] == "warning"

        # Verify kipy calls
        mock_kicad.run_action.assert_any_call("pcbnew.InspectionTool.clearMarkers")
        mock_kicad.run_action.assert_any_call("pcbnew.InspectionTool.runDRC")

    @pytest.mark.asyncio
    async def test_zero_violations(self):
        """DRC finds no violations — returns empty list."""
        mock_kicad = _make_kicad_mock()
        mock_pcbnew = _make_pcbnew_mock([])

        with (
            patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad),
            patch.dict("sys.modules", {"pcbnew": mock_pcbnew}),
        ):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["success"] is True
        assert result["total_violations"] == 0
        assert result["violations"] == []
        assert result["violation_categories"] == {}

    @pytest.mark.asyncio
    async def test_no_board_open(self):
        """KiCad is connected but no board is open — returns error."""
        mock_kicad = _make_kicad_mock(board_open=False)

        with patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["success"] is False
        assert "No board" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_kicad_not_running(self):
        """_connect raises RuntimeError → error returned."""
        with patch("kcaa.tools.kipy_tools._connect", side_effect=RuntimeError("KiCad not running")):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["success"] is False
        assert "KiCad connection failed" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_severity_mapping(self):
        """All known severity enum values map correctly."""
        from kcaa.tools.drc_impl.ipc_drc import _severity_to_string

        assert _severity_to_string(0) == "error"
        assert _severity_to_string(1) == "warning"
        assert _severity_to_string(2) == "exclusion"
        assert _severity_to_string(3) == "ignore"
        assert _severity_to_string(4) == "info"
        assert _severity_to_string(99) == "unknown(99)"

    @pytest.mark.asyncio
    async def test_empty_description_marker(self):
        """Marker with empty description — uses empty message."""
        marker = _FakeMarker("", severity=0, x=0, y=0)
        mock_kicad = _make_kicad_mock()
        mock_pcbnew = _make_pcbnew_mock([marker])

        with (
            patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad),
            patch.dict("sys.modules", {"pcbnew": mock_pcbnew}),
        ):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["success"] is True
        assert result["total_violations"] == 1
        assert result["violations"][0]["message"] == ""

    @pytest.mark.asyncio
    async def test_duplicate_categories_counted(self):
        """Multiple markers with same description → count in categories."""
        markers = [
            _FakeMarker("Silk clearance", severity=0, x=1, y=2),
            _FakeMarker("Silk clearance", severity=0, x=3, y=4),
            _FakeMarker("Hole clearance", severity=1, x=5, y=6),
        ]
        mock_kicad = _make_kicad_mock()
        mock_pcbnew = _make_pcbnew_mock(markers)

        with (
            patch("kcaa.tools.kipy_tools._connect", return_value=mock_kicad),
            patch.dict("sys.modules", {"pcbnew": mock_pcbnew}),
        ):
            result = await ipc_drc.run_drc_via_ipc("/tmp/test.kicad_pcb")

        assert result["violation_categories"]["Silk clearance"] == 2
        assert result["violation_categories"]["Hole clearance"] == 1
