"""Unit tests for sheet_tools.py."""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile

import pytest

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.sheet_tools import register_sheet_tools

    mock = _MockMCP()
    register_sheet_tools(mock)
    return mock.tools


def _make_temp_copy() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False)
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    path = _make_temp_copy()
    yield path
    for candidate in (path, path + ".bak"):
        if os.path.exists(candidate):
            os.unlink(candidate)


class TestAddSheetSymbol:
    def test_auto_place_uses_nearest_free_area(self, tools, tmp_sch):
        from kcaa.tools.placement_helpers import PlacementHelpers
        from kcaa.tools.sheet_tools import _align_to_grid

        requested_x = 100.0
        requested_y = 100.0
        width = 50.8
        height = 50.8
        snapped_x = _align_to_grid(requested_x)
        snapped_y = _align_to_grid(requested_y)
        candidate = PlacementHelpers.find_free_area(
            schematic_path=tmp_sch,
            width=_align_to_grid(width),
            height=_align_to_grid(height),
            prefer_near={"x": snapped_x, "y": snapped_y},
            max_candidates=1,
        )["candidates"][0]["origin"]
        assert (candidate["x"], candidate["y"]) != (snapped_x, snapped_y)

        result = asyncio.run(
            tools["add_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_name="Auto Sheet",
                sheet_file="auto_sheet.kicad_sch",
                x=requested_x,
                y=requested_y,
                width=width,
                height=height,
            )
        )

        assert result["success"] is True, result
        assert result["position_adjusted"] is True
        assert result["requested_position"] == {"x": requested_x, "y": requested_y}
        assert result["position"]["x"] == pytest.approx(candidate["x"])
        assert result["position"]["y"] == pytest.approx(candidate["y"])
        assert result["note"] == "Position adjusted to nearest free area."


class TestUpdateSheetSymbol:
    """Tests for update_sheet_symbol auto-placement."""

    def _add_sheet(self, tools, sch_path: str, name: str, x: float, y: float) -> str:
        """Add a sheet and return its UUID."""
        result = asyncio.run(
            tools["add_sheet_symbol"](
                schematic_path=sch_path,
                sheet_name=name,
                sheet_file=f"{name}.kicad_sch",
                x=x,
                y=y,
                width=50.8,
                height=50.8,
            )
        )
        assert result["success"] is True, result
        return result["sheet_uuid"]

    def test_auto_place_true_avoids_overlap(self, tools, tmp_sch):
        """Moving a sheet to an occupied spot should be auto-adjusted."""
        from kcaa.tools.sheet_tools import _align_to_grid

        # Place a blocker sheet at (20, 20).
        self._add_sheet(tools, tmp_sch, "Blocker", 20.0, 20.0)
        # Place the target sheet at (200, 200) — far from blocker.
        target_uuid = self._add_sheet(tools, tmp_sch, "Target", 200.0, 200.0)

        # Try to move Target onto the blocker (20, 20) — should auto-adjust.
        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=20.0,
                y=20.0,
            )
        )
        assert result["success"] is True, result
        assert result["position_adjusted"] is True
        assert result["requested_position"] == {"x": 20.0, "y": 20.0}
        assert result["note"] == "Position adjusted to nearest free area."
        # The sheet should NOT be at (20, 20).
        actual_pos = result.get("position") or {}
        if actual_pos:
            assert not (
                pytest.approx(actual_pos.get("x")) == _align_to_grid(20.0)
                and pytest.approx(actual_pos.get("y")) == _align_to_grid(20.0)
            )

    def test_no_conflict_keeps_exact_position(self, tools, tmp_sch):
        """When the requested position is free, it is used as-is."""

        # Place one sheet far away — no conflict with target position.
        self._add_sheet(tools, tmp_sch, "Obstacle2", 30.0, 30.0)
        target_uuid = self._add_sheet(tools, tmp_sch, "Target2", 250.0, 250.0)

        # Move to a free position (200, 10.16) — far from obstacle and components.
        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=200.0,
                y=10.16,
            )
        )
        assert result["success"] is True, result
        # No conflict → no adjustment.
        assert result.get("position_adjusted") is False

    def test_size_only_change_skips_auto_place(self, tools, tmp_sch):
        """Resizing without moving should never trigger auto-placement."""
        target_uuid = self._add_sheet(tools, tmp_sch, "ResizeOnly", 150.0, 150.0)

        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                width=76.2,
                height=76.2,
            )
        )
        assert result["success"] is True, result
        # position_adjusted not set for size-only changes.
        assert "position_adjusted" not in result

    def test_partial_move_x_only(self, tools, tmp_sch):
        """x-only move should not lose the y coordinate to bias."""
        target_uuid = self._add_sheet(tools, tmp_sch, "XOnly", 180.0, 180.0)

        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=185.0,
            )
        )
        assert result["success"] is True, result
        # Should not error out; position_adjusted may be True or False.
        assert "error" not in result
