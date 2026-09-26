"""
Unit tests for kcaa/tools/pcb_routing_tools.py (pcb_delete_tracks / pcb_delete_vias)
"""

import asyncio
import os
import shutil
import tempfile

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
\t(segment
\t\t(start 50.0 50.0)
\t\t(end 60.0 50.0)
\t\t(width 0.25)
\t\t(layer "B.Cu")
\t\t(net "NET_A")
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


_CLEAR_BOARD = """(kicad_pcb
\t(version 20260206)
\t(generator "test")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(net 0 "")
\t(net 1 "VCC")
\t(footprint "R"
\t\t(layer "F.Cu")
\t\t(at 30.0 30.0 0.0)
\t\t(property "Reference" "R1")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Mask")
\t\t\t(net 1 "VCC")
\t\t)
\t)
\t(footprint "C"
\t\t(layer "F.Cu")
\t\t(at 60.0 40.0 0.0)
\t\t(property "Reference" "C1")
\t\t(pad "1" smd rect
\t\t\t(at 0.0 -0.5)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Mask")
\t\t\t(net 1 "VCC")
\t\t)
\t)
\t(gr_rect
\t\t(start 20.0 20.0)
\t\t(end 70.0 60.0)
\t\t(stroke (width 0.1) (type solid))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t)
)
"""

_CLEAR_PRO = """{
  "board": {
    "design_settings": {
      "rules": {
        "min_clearance": 0.2,
        "min_track_width": 0.2
      }
    }
  },
  "net_settings": {
    "classes": [
      {
        "name": "Default",
        "clearance": 0.2,
        "track_width": 0.25,
        "via_diameter": 0.6,
        "via_drill": 0.3
      }
    ]
  }
}
"""


@pytest.fixture
def routable_board(tmp_path):
    """Minimal 2-pad single-layer board with no obstacles between them,
    plus a matching .kicad_pro (clearance resolution needs the project
    file's design rules)."""
    dest = tmp_path / "routing.kicad_pcb"
    dest.write_text(_CLEAR_BOARD, encoding="utf-8")
    pro = tmp_path / "routing.kicad_pro"
    pro.write_text(_CLEAR_PRO, encoding="utf-8")
    return str(dest)


def _run(coro):
    return asyncio.run(coro)


class TestPcbDeleteTracks:
    def test_empty_list_is_noop(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_tracks"](pcb_path=board_with_tracks, segments=[], ctx=None))
        assert result["deleted_count"] == 0
        assert result["backup_path"] is None

    def test_delete_single_segment(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 30.0, "y1": 10.0, "x2": 40.0, "y2": 10.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 1
        assert result["backup_path"] is not None

    def test_delete_two_segments(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[
                    {"x1": 10.0, "y1": 20.0, "x2": 20.0, "y2": 20.0},
                    {"x1": 30.0, "y1": 10.0, "x2": 40.0, "y2": 10.0},
                ],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 2

    def test_not_found_reported(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 99.0, "y1": 99.0, "x2": 100.0, "y2": 100.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 0
        assert len(result.get("not_found", [])) >= 1

    def test_layer_filter_does_not_match_other_layer(self, tools, board_with_tracks):
        """Segment on F.Cu should not be deleted when filtering for B.Cu."""
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 10.0, "y1": 20.0, "x2": 20.0, "y2": 20.0, "layer": "B.Cu"}],
                ctx=None,
            )
        )
        # VCC segment (10,20)→(20,20) is on F.Cu, not B.Cu → not found
        assert result["deleted_count"] == 0
        assert len(result.get("not_found", [])) >= 1

    def test_layer_filter_matches_correct_layer(self, tools, board_with_tracks):
        """Segment on B.Cu should be deleted when filtering for B.Cu."""
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 50.0, "y1": 50.0, "x2": 60.0, "y2": 50.0, "layer": "B.Cu"}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 1
        assert result["backup_path"] is not None


class TestPcbDeleteVias:
    def test_empty_list_is_noop(self, tools, board_with_tracks):
        result = _run(tools["pcb_delete_vias"](pcb_path=board_with_tracks, vias=[], ctx=None))
        assert result["deleted_count"] == 0
        assert result["backup_path"] is None

    def test_delete_single_via(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_vias"](
                pcb_path=board_with_tracks,
                vias=[{"x": 20.0, "y": 20.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 1
        assert result["backup_path"] is not None

    def test_delete_multiple_vias(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_vias"](
                pcb_path=board_with_tracks,
                vias=[{"x": 20.0, "y": 20.0}, {"x": 35.0, "y": 10.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 2

    def test_not_found_reported(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_delete_vias"](
                pcb_path=board_with_tracks,
                vias=[{"x": 99.0, "y": 99.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 0
        assert len(result["not_found"]) >= 1


class TestPcbRouteOptions:
    """pcb_route_pad_to_pad: options dict + rounded45 default."""

    def _route(self, tools, board, options=None):
        return _run(
            tools["pcb_route_pad_to_pad"](
                pcb_path=board,
                ref_a="R1",
                pad_a="1",
                ref_b="C1",
                pad_b="1",
                net="VCC",
                ctx=None,
                width=0.2,
                algorithm="pns",
                options=options,
            )
        )

    def test_options_none_defaults_to_rounded45(self, tools, routable_board):
        """Omitting ``options`` routes with corner_mode=rounded45: the
        unobstructed single-layer PNS skeleton emits its fillet arc."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert result["corner_mode"] == "rounded45"
        assert result["algorithm"] == "pns"
        assert result["segment_count"] > 0
        assert result["arc_count"] >= 1
        assert result["arcs"][0]["layer"] == "F.Cu"
        assert result["arcs"][0]["net"] == "VCC"

    def test_options_mitered45_suppresses_arcs(self, tools, routable_board):
        """options={'corner_mode': 'mitered45'} overrides the default:
        the same route emits straight segments only."""
        result = self._route(tools, routable_board, options={"corner_mode": "mitered45"})
        assert "error" not in result
        assert result["corner_mode"] == "mitered45"
        assert result["segment_count"] > 0
        assert result["arc_count"] == 0
        assert result["arcs"] == []

    def test_dry_run_skips_write_and_echoes(self, tools, routable_board):
        """dry_run=True routes and returns the full result but leaves the
        PCB byte-identical (no reload, no .bak); a follow-up dry_run=False
        on the same board writes (file changes)."""
        before = open(routable_board, "rb").read()
        dry = self._route(tools, routable_board, options={"dry_run": True})
        assert "error" not in dry
        assert dry["dry_run"] is True
        assert dry["segment_count"] > 0
        assert dry["backup_path"] is None
        assert "waypoint_violated" in dry
        assert "violated_waypoints" in dry
        assert "via_sites" in dry
        assert open(routable_board, "rb").read() == before

        wet = self._route(tools, routable_board, options={"dry_run": False})
        assert "error" not in wet
        assert wet["dry_run"] is False
        assert wet["segment_count"] > 0
        assert wet["backup_path"] is not None
        assert open(routable_board, "rb").read() != before

class TestPcbRouteCandidates:
    """pcb_route_pad_to_pad: the PNS candidates control surface (W3)."""

    def _route(self, tools, board, options=None, algorithm="pns"):
        return _run(
            tools["pcb_route_pad_to_pad"](
                pcb_path=board,
                ref_a="R1",
                pad_a="1",
                ref_b="C1",
                pad_b="1",
                net="VCC",
                ctx=None,
                width=0.2,
                algorithm=algorithm,
                options=options,
            )
        )

    def test_candidates_default_response_shape_unchanged(self, tools, routable_board):
        """candidates absent (or 1) must leave the response byte-identical
        to today: no candidates / candidates_png keys, primary result
        only."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert "candidates" not in result
        assert "candidates_png" not in result
        assert result["segment_count"] > 0
        assert result["via_count"] == 0

    def test_candidates_two_returns_variants_and_png(self, tools, routable_board):
        """candidates=2 on a clear board dedupes to a single variant but
        still surfaces the candidates list and renders the side-by-side
        PNG next to the request (best-effort)."""
        result = self._route(tools, routable_board, options={"candidates": 2})
        assert "error" not in result
        assert "candidates" in result
        assert len(result["candidates"]) >= 1
        assert result["candidates"][0]["variant"] == "walkaround"
        assert "candidates_png" in result
        assert result["candidates_png"].startswith(
            os.path.join(tempfile.gettempdir(), "kcaa_candidates_")
        )
        assert os.path.exists(result["candidates_png"])
        # Primary top-level result echoes candidate 1.
        assert result["segments"] == result["candidates"][0]["segments"]

    def test_candidates_dry_run_still_renders_png(self, tools, routable_board):
        """dry_run skips only the PCB write; best-effort candidate render
        still fires (it reads the board and writes only temp files)."""
        before = open(routable_board, "rb").read()
        result = self._route(
            tools, routable_board, options={"candidates": 2, "dry_run": True}
        )
        assert "error" not in result
        assert result["dry_run"] is True
        assert "candidates_png" in result
        assert os.path.exists(result["candidates_png"])
        assert open(routable_board, "rb").read() == before

    def test_candidates_with_astar_rejected(self, tools, routable_board):
        """candidates > 1 is the PNS control surface; the A* planner
        rejects it with the input error instead of ignoring it."""
        result = self._route(
            tools, routable_board, options={"candidates": 2}, algorithm="astar"
        )
        assert "error" in result
        assert "only supported with algorithm" in result["error"]

