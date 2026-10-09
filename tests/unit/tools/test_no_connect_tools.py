"""
Tests for the no-connect flag tools in no_connect_tools.py:
  - add_no_connect
  - list_no_connects
  - remove_no_connect

All write tests work on a temporary copy of tools_test.kicad_sch so the
original fixture is never modified.

Fixture assumptions (tools_test.kicad_sch):
    The schematic contains no pre-existing no-connect flags.
"""

import asyncio
import os
from pathlib import Path
import shutil
import uuid

import pytest
import skip

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures" / "tools_test.kicad_sch")


def _make_temp_copy() -> str:
    tmp_path = Path(__file__).parent / "fixtures" / f"tools_test_{uuid.uuid4().hex}.kicad_sch"
    shutil.copy(SCHEMATIC_PATH, tmp_path)
    return str(tmp_path)


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
    from kcaa.tools.no_connect_tools import register_no_connect_tools

    mock = _MockMCP()
    register_no_connect_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


def _no_connects(path: str) -> list:
    """Return no_connect elements from a freshly-read schematic, handling the
    skip single-vs-collection quirk (see _iter_no_connects in the tool)."""
    sch = skip.Schematic(path)
    try:
        coll = sch.no_connect
    except AttributeError:
        return []
    if coll is None:
        return []
    elements = getattr(coll, "_elements", None)
    if elements is not None:
        return list(elements)
    return [coll]


def _count_no_connects(path: str) -> int:
    return len(_no_connects(path))


# ---------------------------------------------------------------------------
# add_no_connect
# ---------------------------------------------------------------------------


class TestAddNoConnect:
    def _add(self, tools, path, x=200.0, y=200.0):
        return asyncio.run(tools["add_no_connect"](schematic_path=path, x=x, y=y))

    def test_add_returns_success_and_persists(self, tools, tmp_sch):
        result = self._add(tools, tmp_sch, x=180.0, y=120.0)
        assert result.get("success") is True, result
        assert result["no_connect"] == {"x": 180.0, "y": 120.0}
        assert result["file_modified"] == tmp_sch

        # Verify the flag is persisted at the right spot.
        found = any(
            abs(float(nc.at.value[0]) - 180.0) < 0.01 and abs(float(nc.at.value[1]) - 120.0) < 0.01
            for nc in _no_connects(tmp_sch)
        )
        assert found, "Added no-connect flag not found in saved schematic"

    def test_add_is_idempotent_at_same_spot(self, tools, tmp_sch):
        first = self._add(tools, tmp_sch, x=150.0, y=150.0)
        assert first.get("success") is True
        before = _count_no_connects(tmp_sch)

        second = self._add(tools, tmp_sch, x=150.0, y=150.0)
        assert second.get("success") is True
        assert second.get("already_present") is True
        assert second.get("file_modified") is None
        assert _count_no_connects(tmp_sch) == before

    def test_non_finite_coords_rejected(self, tools, tmp_sch):
        result = asyncio.run(tools["add_no_connect"](schematic_path=tmp_sch, x=float("nan"), y=1.0))
        assert "error" in result

    def test_non_schematic_path_rejected(self, tools):
        result = asyncio.run(tools["add_no_connect"](schematic_path="/tmp/foo.txt", x=0.0, y=0.0))
        assert "error" in result

    def test_missing_file_rejected(self, tools):
        result = asyncio.run(
            tools["add_no_connect"](schematic_path="/nope/missing.kicad_sch", x=0.0, y=0.0)
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# list_no_connects
# ---------------------------------------------------------------------------


class TestListNoConnects:
    def test_empty_schematic_lists_none(self, tools, tmp_sch):
        result = asyncio.run(tools["list_no_connects"](schematic_path=tmp_sch))
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["no_connects"] == []

    def test_lists_added_flags(self, tools, tmp_sch):
        asyncio.run(tools["add_no_connect"](schematic_path=tmp_sch, x=170.0, y=110.0))
        asyncio.run(tools["add_no_connect"](schematic_path=tmp_sch, x=175.0, y=115.0))
        result = asyncio.run(tools["list_no_connects"](schematic_path=tmp_sch))
        assert result["count"] == 2
        coords = {(round(n["x"], 2), round(n["y"], 2)) for n in result["no_connects"]}
        assert (170.0, 110.0) in coords
        assert (175.0, 115.0) in coords


# ---------------------------------------------------------------------------
# remove_no_connect
# ---------------------------------------------------------------------------


class TestRemoveNoConnect:
    def test_remove_deletes_matching_flag(self, tools, tmp_sch):
        asyncio.run(tools["add_no_connect"](schematic_path=tmp_sch, x=160.0, y=130.0))
        assert _count_no_connects(tmp_sch) == 1

        result = asyncio.run(tools["remove_no_connect"](schematic_path=tmp_sch, x=160.0, y=130.0))
        assert result.get("success") is True
        assert result["deleted_count"] == 1
        assert _count_no_connects(tmp_sch) == 0

    def test_remove_no_match_is_noop(self, tools, tmp_sch):
        result = asyncio.run(tools["remove_no_connect"](schematic_path=tmp_sch, x=999.0, y=999.0))
        assert result.get("success") is True
        assert result["deleted_count"] == 0
        assert result.get("file_modified") is None

    def test_remove_honors_tolerance(self, tools, tmp_sch):
        asyncio.run(tools["add_no_connect"](schematic_path=tmp_sch, x=140.0, y=140.0))
        # 0.5 mm away: outside the default 0.01 tolerance, inside a 1.0 tolerance.
        miss = asyncio.run(tools["remove_no_connect"](schematic_path=tmp_sch, x=140.5, y=140.0))
        assert miss["deleted_count"] == 0
        hit = asyncio.run(
            tools["remove_no_connect"](schematic_path=tmp_sch, x=140.5, y=140.0, tolerance=1.0)
        )
        assert hit["deleted_count"] == 1
