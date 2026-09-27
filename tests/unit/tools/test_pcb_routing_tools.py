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


@pytest.fixture
def crossing_board(tmp_path):
    """routable_board plus one foreign-net GND track crossing the direct
    pad-to-pad line — the shove-persistence fixture (the route between
    R1.1 and C1.1 crosses it at (43.75, 30))."""
    dest = tmp_path / "crossing.kicad_pcb"
    text = _CLEAR_BOARD.replace('\t(net 1 "VCC")\n', '\t(net 1 "VCC")\n\t(net 2 "GND")\n')
    seg = (
        "\t(segment (start 40.0 25.0) (end 55.0 45.0) "
        '(width 0.25) (layer "F.Cu") (net 2 "GND"))\n'
    )
    text = text.rstrip()[:-1] + seg + ")"
    dest.write_text(text, encoding="utf-8")
    pro = tmp_path / "crossing.kicad_pro"
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

class TestPcbRouteStrategy:
    """pcb_route_pad_to_pad: the explicit strategy knob + always-on render."""

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

    def test_strategy_default_echoes_auto_with_route_png(self, tools, routable_board):
        """No options: strategy echoes "auto" and the response always
        carries the single-route render path (field shape is
        str/None; the render itself is smoke-checked below)."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert result["strategy"] == "auto"
        assert "route_png" in result
        assert "candidates" not in result
        assert "candidates_png" not in result
        assert result["segment_count"] > 0
        assert result["via_count"] == 0

    def test_strategy_walkaround_parsed_and_echoed(self, tools, routable_board):
        result = self._route(tools, routable_board, options={"strategy": "walkaround"})
        assert "error" not in result
        assert result["strategy"] == "walkaround"

    def test_strategy_shove_parsed_and_echoed(self, tools, routable_board):
        result = self._route(tools, routable_board, options={"strategy": "shove"})
        assert "error" not in result
        assert result["strategy"] == "shove"

    def test_strategy_invalid_value_rejected(self, tools, routable_board):
        """Values outside {auto, walkaround, shove} fail with a clear
        message instead of being silently ignored."""
        result = self._route(tools, routable_board, options={"strategy": "multi"})
        assert "error" in result
        assert "strategy='multi' is invalid" in result["error"]
        assert "'auto'" in result["error"]

    def test_strategy_inert_for_astar(self, tools, routable_board):
        """A* has no shove stage: the value is accepted and echoed, not
        rejected."""
        result = self._route(
            tools, routable_board, options={"strategy": "walkaround"}, algorithm="astar"
        )
        assert "error" not in result
        assert result["strategy"] == "walkaround"

    def test_route_png_always_present_and_existing(self, tools, routable_board):
        """The successful single route renders a real PNG (not just a
        field): path points at an existing non-empty file."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        png = result["route_png"]
        assert png, "best-effort render should produce a path on this fixture"
        assert png.startswith(os.path.join(tempfile.gettempdir(), "kcaa_route_"))
        assert os.path.exists(png)
        assert os.path.getsize(png) > 0

    def test_route_png_rendered_in_dry_run_too(self, tools, routable_board):
        """dry_run skips only the PCB write; the render still fires (it
        reads the board and writes only temp files)."""
        before = open(routable_board, "rb").read()
        result = self._route(
            tools, routable_board, options={"strategy": "auto", "dry_run": True}
        )
        assert "error" not in result
        assert result["dry_run"] is True
        png = result["route_png"]
        assert png and os.path.exists(png)
        assert open(routable_board, "rb").read() == before

    # -- Shove persistence -------------------------------------------------

    @staticmethod
    def _gnd_segments(pcb_path: str) -> list[dict]:
        """All GND (net 2) track segments currently in the file."""
        from kcaa.tools.pcb_routing_tools import _segment_fields
        from kcaa.utils.pcb_sexp_utils import load_pcb

        out = []
        for node in load_pcb(pcb_path):
            fields = _segment_fields(node)
            if fields is not None and fields["net"] == "GND":
                out.append(fields)
        return out

    @staticmethod
    def _vcc_segments(pcb_path: str) -> list[dict]:
        """All VCC track segments currently in the file."""
        from kcaa.tools.pcb_routing_tools import _segment_fields
        from kcaa.utils.pcb_sexp_utils import load_pcb

        out = []
        for node in load_pcb(pcb_path):
            fields = _segment_fields(node)
            if fields is not None and fields["net"] == "VCC":
                out.append(fields)
        return out

    @staticmethod
    def _segments_intersect(a: tuple, b: tuple) -> bool:
        """True when line segments (p1,p2) and (q1,q2) cross properly."""
        (ax, ay), (bx, by) = a
        (qx, qy), (rx, ry) = b

        def ccw(p, q, r):
            return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

        o1 = ccw((ax, ay), (bx, by), (qx, qy))
        o2 = ccw((ax, ay), (bx, by), (rx, ry))
        o3 = ccw((qx, qy), (rx, ry), (ax, ay))
        o4 = ccw((qx, qy), (rx, ry), (bx, by))
        return o1 * o2 < 0 and o3 * o4 < 0

    def _route_vcc(self, tools, board, options):
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

    def test_shove_persists_displaced_gnd_track(self, tools, crossing_board):
        """strategy=shove, non-dry_run: the original GND segment is REMOVED
        from the file and the displaced polyline is written back, so the
        committed board no longer contains a GND track crossing the new
        VCC line."""
        result = self._route_vcc(tools, crossing_board, options={"strategy": "shove"})
        assert "error" not in result
        assert result["shoved"], "fixture must actually shove the GND track"
        # 1) original gone
        gnd = self._gnd_segments(crossing_board)
        assert gnd, "displaced GND track must be present"
        assert not any(
            abs(s["start"][0] - 40.0) <= 1e-6
            and abs(s["start"][1] - 25.0) <= 1e-6
            and abs(s["end"][0] - 55.0) <= 1e-6
            and abs(s["end"][1] - 45.0) <= 1e-6
            for s in gnd
        ), "original GND segment (40,25)->(55,45) must be removed"
        # 2) width/layer/net preserved
        for s in gnd:
            assert s["width"] == pytest.approx(0.25)
            assert s["layer"] == "F.Cu"
            assert s["net"] == "GND"
        # 3) no carried GND segment crosses the routed VCC line(s)
        vcc = [
            (s["start"], s["end"])
            for s in self._vcc_segments(crossing_board)
        ]
        assert vcc
        for s in gnd:
            line = (s["start"], s["end"])
            for v in vcc:
                assert not self._segments_intersect(line, v), (
                    f"GND segment {line} still crosses routed VCC {v}"
                )

    def test_shove_written_segments_tile_displaced_polyline(self, tools, crossing_board):
        """Wherever the displaced polyline has intermediate vertices, the
        written segments tile it contiguously (p_i -> p_{i+1})."""
        result = self._route_vcc(tools, crossing_board, options={"strategy": "shove"})
        assert "error" not in result
        assert result["shoved"]
        pts = result["shoved"][0]["points"]
        if len(pts) < 3:
            return  # single-hop displacement: nothing to tile
        gnd = self._gnd_segments(crossing_board)
        start = (pts[0][0], pts[0][1])
        cur = start
        for p in pts[1:]:
            nxt = (p[0], p[1])
            assert any(
                abs(s["start"][0] - cur[0]) <= 1e-6
                and abs(s["start"][1] - cur[1]) <= 1e-6
                and abs(s["end"][0] - nxt[0]) <= 1e-6
                and abs(s["end"][1] - nxt[1]) <= 1e-6
                for s in gnd
            ), f"missing tiling segment {cur} -> {nxt}"
            cur = nxt

    def test_walkaround_leaves_gnd_track_untouched(self, tools, crossing_board):
        """strategy=walkaround: no shove, no rewrite — the file still
        contains the GND segment exactly at its original position."""
        before = self._gnd_segments(crossing_board)
        assert len(before) == 1
        assert before[0]["start"] == (40.0, 25.0)
        assert before[0]["end"] == (55.0, 45.0)
        result = self._route_vcc(
            tools, crossing_board, options={"strategy": "walkaround"}
        )
        assert "error" not in result
        assert result["shoved"] == []
        after = self._gnd_segments(crossing_board)
        assert after == before, "walkaround must not touch the GND segment"

    def test_shove_dry_run_persists_nothing_but_reports_pairs(self, tools, crossing_board):
        """dry_run=True: file byte-identical, shoved pairs still reported
        in the response (report-only, nothing written)."""
        before = open(crossing_board, "rb").read()
        result = self._route_vcc(
            tools, crossing_board, options={"strategy": "shove", "dry_run": True}
        )
        assert "error" not in result
        assert result["dry_run"] is True
        assert result["shoved"], "dry_run must still report the pushed track"
        assert open(crossing_board, "rb").read() == before
        gnd = self._gnd_segments(crossing_board)
        assert len(gnd) == 1
        assert gnd[0]["start"] == (40.0, 25.0) and gnd[0]["end"] == (55.0, 45.0)


