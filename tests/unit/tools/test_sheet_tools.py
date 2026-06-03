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
                auto_place=True,
            )
        )

        assert result["success"] is True, result
        assert result["position_adjusted"] is True
        assert result["requested_position"] == {"x": requested_x, "y": requested_y}
        assert result["position"]["x"] == pytest.approx(candidate["x"])
        assert result["position"]["y"] == pytest.approx(candidate["y"])
        assert result["note"] == "Position adjusted to nearest free area."
