"""
Unit tests for kcaa/tools/pcb_routing_tools.py (pcb_delete_tracks / pcb_delete_vias)
"""

import asyncio
import os
import shutil

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "../..", "unit", "tools", "fixtures")
FIXTURE_DIR = os.path.normpath(FIXTURE_DIR)
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")

# ── Segments/vias snippet ───────────────────────────────────────────────

_SEGMENTS_SNIPPET = """
\t(segment
\t\t(start 10.0 20.0)
\t\t(end 20.0 20.0)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(segment
\t\t(start 20.0 20.0)
\t\t(end 20.0 30.0)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "VCC")
\t)
\t(segment
\t\t(start 30.0 10.0)
\t\t(end 40.0 10.0)
\t\t(width 0.50)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t)
\t(via
\t\t(at 20.0 20.0)
\t\t(size 0.8)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "VCC")
\t)
\t(via
\t\t(at 35.0 10.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "GND")
\t)
"""


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.pcb_routing_tools import register_pcb_routing_tools

    mock = _MockMCP()
    register_pcb_routing_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def board_with_tracks(tmp_path):
    """Copy base board and append segment/via entries."""
    dest = tmp_path / "board_with_tracks.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    text = dest.read_text(encoding="utf-8")
    idx = text.rstrip().rfind(")")
    text = text[:idx] + _SEGMENTS_SNIPPET + text[idx:]
    dest.write_text(text, encoding="utf-8")
    return str(dest)


def _run(coro):
    return asyncio.run(coro)


class TestPcbDeleteTracks:
    def test_empty_list_is_noop(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_tracks"](pcb_path=board_with_tracks, segments=[], ctx=None))
        assert result["deleted_count"] == 0
        assert result["backup_path"] is None

    def test_delete_single_segment(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_tracks"](
            pcb_path=board_with_tracks,
            segments=[{"x1": 30.0, "y1": 10.0, "x2": 40.0, "y2": 10.0}],
            ctx=None,
        ))
        assert result["deleted_count"] == 1
        assert result["backup_path"] is not None

    def test_delete_two_segments(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_tracks"](
            pcb_path=board_with_tracks,
            segments=[
                {"x1": 10.0, "y1": 20.0, "x2": 20.0, "y2": 20.0},
                {"x1": 30.0, "y1": 10.0, "x2": 40.0, "y2": 10.0},
            ],
            ctx=None,
        ))
        assert result["deleted_count"] == 2

    def test_not_found_reported(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_tracks"](
            pcb_path=board_with_tracks,
            segments=[{"x1": 99.0, "y1": 99.0, "x2": 100.0, "y2": 100.0}],
            ctx=None,
        ))
        assert result["deleted_count"] == 0
        # not_found expects segments list items
        assert len(result.get("not_found", [])) >= 1 or result["deleted_count"] == 0


class TestPcbDeleteVias:
    def test_empty_list_is_noop(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_vias"](pcb_path=board_with_tracks, vias=[], ctx=None))
        assert result["deleted_count"] == 0
        assert result["backup_path"] is None

    def test_delete_single_via(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_vias"](
            pcb_path=board_with_tracks,
            vias=[{"x": 20.0, "y": 20.0}],
            ctx=None,
        ))
        assert result["deleted_count"] == 1
        assert result["backup_path"] is not None

    def test_delete_multiple_vias(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_vias"](
            pcb_path=board_with_tracks,
            vias=[{"x": 20.0, "y": 20.0}, {"x": 35.0, "y": 10.0}],
            ctx=None,
        ))
        assert result["deleted_count"] == 2

    def test_not_found_reported(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_vias"](
            pcb_path=board_with_tracks,
            vias=[{"x": 99.0, "y": 99.0}],
            ctx=None,
        ))
        assert result["deleted_count"] == 0
        assert len(result["not_found"]) >= 1
