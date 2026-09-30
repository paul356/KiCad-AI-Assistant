"""
Unit tests for kcaa/tools/pcb_routing_tools.py (pcb_delete_tracks / pcb_delete_vias)
"""

import asyncio
import json
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
\t(arc
\t\t(start 70.0 10.0)
\t\t(mid 70.4 10.3)
\t\t(end 71.0 10.6)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "NET_A")
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
        '\t(segment (start 40.0 25.0) (end 55.0 45.0) (width 0.25) (layer "F.Cu") (net 2 "GND"))\n'
    )
    text = text.rstrip()[:-1] + seg + ")"
    dest.write_text(text, encoding="utf-8")
    pro = tmp_path / "crossing.kicad_pro"
    pro.write_text(_CLEAR_PRO, encoding="utf-8")
    return str(dest)


def _run(coro):
    """Run a tool call and normalize the MCP content return to a dict.

    ``pcb_route_pad_to_pad`` returns ``(json_text, Image)`` content
    blocks (image dropped here — asserted separately via ``_run_raw``)
    or bare JSON text; other tools return plain dicts."""
    result = asyncio.run(coro)
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, str):
        result = json.loads(result)
    return result


def _run_raw(coro):
    """Run a tool call and return the raw MCP content untouched."""
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

    def test_delete_arc_node_by_endpoints(self, tools, board_with_tracks):
        """Arc track nodes delete by their start/end endpoints: the router
        reported rounded corners as x1/y1..x2/y2, so callers pass exactly
        the arc's two ends (regression: the old collector only scanned
        ``(segment ...)`` nodes and reported arcs not_found)."""
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 70.0, "y1": 10.0, "x2": 71.0, "y2": 10.6}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 1
        assert result["matched_count"] == 1
        assert result["backup_path"] is not None
        # The arc is gone from the file; the straight segments survive.
        text = open(board_with_tracks, encoding="utf-8").read()
        assert "(start 70.0 10.0)" not in text
        assert "(start 10.0 20.0)" in text

    def test_arc_endpoints_reversed_still_match(self, tools, board_with_tracks):
        """Endpoint order is irrelevant for arcs, same as segments."""
        result = _run(
            tools["pcb_delete_tracks"](
                pcb_path=board_with_tracks,
                segments=[{"x1": 71.0, "y1": 10.6, "x2": 70.0, "y2": 10.0}],
                ctx=None,
            )
        )
        assert result["deleted_count"] == 1
        assert len(result.get("not_found", [])) == 0


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


class TestModelVisionGate:
    """KICAD_MCP_SUPPORTS_VISION parsing (image blocks at the source)."""

    def _vision(self):
        from kcaa.utils.config import model_supports_vision

        return model_supports_vision()

    def test_defaults_to_vision_when_unset(self, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_SUPPORTS_VISION", raising=False)
        assert self._vision() is True

    def test_explicit_disabled_values(self, monkeypatch):
        for value in ("0", "false", "no", "off"):
            monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", value)
            assert self._vision() is False, value

    def test_enabled_values(self, monkeypatch):
        for value in ("1", "true", "yes"):
            monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", value)
            assert self._vision() is True, value


class TestPcbRouteOptions:
    """pcb_route_pad_to_pad: options dict + mitered45 default."""

    def _route(self, tools, board, options=None, **kwargs):
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
                **kwargs,
            )
        )

    def test_options_none_defaults_to_mitered45(self, tools, routable_board):
        """Omitting ``options`` routes with corner_mode=mitered45: the
        unobstructed single-layer PNS skeleton emits plain 0/45/90
        segments, no arcs."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert result["corner_mode"] == "mitered45"
        assert result["algorithm"] == "pns"
        assert result["segment_count"] > 0
        assert result["arc_count"] == 0
        assert result["arcs"] == []

    def test_options_layer_hint_accepted_via_options(self, tools, routable_board):
        """v3: ``layer_hint`` moved into ``options``.  On an SMD-pad
        fixture the hint is accepted without error and the route lands on
        the fixed copper layer ("F.Cu") — no top-level layer_hint exists
        anymore."""
        result = self._route(tools, routable_board, options={"layer_hint": "F.Cu"})
        assert "error" not in result
        assert result["layers_used"] == ["F.Cu"]
        assert "layer_hint" not in result

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
        dry = self._route(tools, routable_board, dry_run=True)
        assert "error" not in dry
        assert dry["dry_run"] is True
        assert dry["segment_count"] > 0
        assert dry["backup_path"] is None
        assert "waypoint_violated" in dry
        assert "violated_waypoints" in dry
        assert "via_sites" in dry
        assert open(routable_board, "rb").read() == before

        wet = self._route(tools, routable_board, dry_run=False)
        assert "error" not in wet
        assert wet["dry_run"] is False
        assert wet["segment_count"] > 0
        assert wet["backup_path"] is not None
        assert open(routable_board, "rb").read() != before


class TestPcbRouteStrategy:
    """pcb_route_pad_to_pad: the explicit strategy knob + always-on render."""

    def _route(self, tools, board, options=None, algorithm="pns", **kwargs):
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
                **kwargs,
            )
        )

    def test_v3_top_level_defaults_match_old_behavior(self, tools, routable_board):
        """v3: with no top-level knobs and ``options=None`` the call
        behaves exactly like the old default: strategy shove (was the
        ``auto`` default, identical path), no dry_run, no waypoints (empty
        via_sites), mitered45 corners, algorithm pns."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert result["strategy"] == "shove"
        assert result["dry_run"] is False
        assert result["via_sites"] == []
        assert result["corner_mode"] == "mitered45"
        assert result["algorithm"] == "pns"

    def test_strategy_default_echoes_shove_with_route_png(self, tools, routable_board):
        """No options: strategy echoes "shove" and the response always
        carries the single-route render path (field shape is
        str/None; the render itself is smoke-checked below)."""
        result = self._route(tools, routable_board)
        assert "error" not in result
        assert result["strategy"] == "shove"
        assert "route_png" in result
        assert "candidates" not in result
        assert "candidates_png" not in result
        assert result["segment_count"] > 0
        assert result["via_count"] == 0

    def test_strategy_auto_rejected(self, tools, routable_board):
        """``"auto"`` was removed (2026-09-27): it now fails validation
        like any unknown value."""
        result = self._route(tools, routable_board, strategy="auto")
        assert "error" in result
        assert "strategy='auto' is invalid" in result["error"]

    def test_strategy_walkaround_parsed_and_echoed(self, tools, routable_board):
        result = self._route(tools, routable_board, strategy="walkaround")
        assert "error" not in result
        assert result["strategy"] == "walkaround"

    def test_strategy_shove_parsed_and_echoed(self, tools, routable_board):
        result = self._route(tools, routable_board, strategy="shove")
        assert "error" not in result
        assert result["strategy"] == "shove"

    def test_strategy_invalid_value_rejected(self, tools, routable_board):
        """Values outside {shove, walkaround} fail with a clear
        message instead of being silently ignored."""
        result = self._route(tools, routable_board, strategy="multi")
        assert "error" in result
        assert "strategy='multi' is invalid" in result["error"]
        assert "'shove'" in result["error"]

    def test_strategy_inert_for_astar(self, tools, routable_board):
        """A* has no shove stage: the value is accepted and echoed, not
        rejected."""
        result = self._route(tools, routable_board, strategy="walkaround", algorithm="astar")
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
        result = self._route(tools, routable_board, strategy="shove", dry_run=True)
        assert "error" not in result
        assert result["dry_run"] is True
        png = result["route_png"]
        assert png and os.path.exists(png)
        assert open(routable_board, "rb").read() == before

    def test_success_returns_image_content_block(self, tools, routable_board):
        """The tool result carries (json_text, Image): the rendered route
        PNG as an image content block — the plugin splits it into the
        ``_image`` field the VLM actually sees (a path alone is never
        relayed as image data)."""
        from fastmcp.utilities.types import Image

        raw = _run_raw(
            tools["pcb_route_pad_to_pad"](
                pcb_path=routable_board,
                ref_a="R1",
                pad_a="1",
                ref_b="C1",
                pad_b="1",
                net="VCC",
                ctx=None,
                width=0.2,
                algorithm="pns",
            )
        )
        assert isinstance(raw, tuple) and len(raw) == 2
        payload = json.loads(raw[0])
        assert "error" not in payload
        assert payload["strategy"] == "shove"
        image = raw[1]
        assert isinstance(image, Image)
        assert image.data[:8] == b"\x89PNG\r\n\x1a\n"
        if payload.get("route_png"):
            os.remove(payload["route_png"])

    def test_success_omits_image_block_for_text_only_model(
        self, tools, routable_board, monkeypatch
    ):
        """A text-only model (plugin sets KICAD_MCP_SUPPORTS_VISION=0)
        must receive the bare JSON text — no image content block, no
        PNG payload — instead of a block the client has to strip."""
        monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", "0")
        raw = _run_raw(
            tools["pcb_route_pad_to_pad"](
                pcb_path=routable_board,
                ref_a="R1",
                pad_a="1",
                ref_b="C1",
                pad_b="1",
                net="VCC",
                ctx=None,
                width=0.2,
                algorithm="pns",
            )
        )
        assert isinstance(raw, str), f"expected a bare text block, got {type(raw).__name__}"
        payload = json.loads(raw)
        assert "error" not in payload
        assert payload["strategy"] == "shove"
        if payload.get("route_png"):
            os.remove(payload["route_png"])

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

    def _route_vcc(self, tools, board, options=None, **kwargs):
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
                **kwargs,
            )
        )

    def test_shove_persists_displaced_gnd_track(self, tools, crossing_board):
        """strategy=shove, non-dry_run: the original GND segment is REMOVED
        from the file and the displaced polyline is written back, so the
        committed board no longer contains a GND track crossing the new
        VCC line."""
        result = self._route_vcc(tools, crossing_board, strategy="shove")
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
        vcc = [(s["start"], s["end"]) for s in self._vcc_segments(crossing_board)]
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
        result = self._route_vcc(tools, crossing_board, strategy="shove")
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
        result = self._route_vcc(tools, crossing_board, strategy="walkaround")
        assert "error" not in result
        assert result["shoved"] == []
        after = self._gnd_segments(crossing_board)
        assert after == before, "walkaround must not touch the GND segment"

    def test_shove_dry_run_persists_nothing_but_reports_pairs(self, tools, crossing_board):
        """dry_run=True: file byte-identical, shoved pairs still reported
        in the response (report-only, nothing written)."""
        before = open(crossing_board, "rb").read()
        result = self._route_vcc(tools, crossing_board, strategy="shove", dry_run=True)
        assert "error" not in result
        assert result["dry_run"] is True
        assert result["shoved"], "dry_run must still report the pushed track"
        assert open(crossing_board, "rb").read() == before
        gnd = self._gnd_segments(crossing_board)
        assert len(gnd) == 1
        assert gnd[0]["start"] == (40.0, 25.0) and gnd[0]["end"] == (55.0, 45.0)

    def test_shove_failure_returns_error_with_evidence(self, tools, crossing_board, monkeypatch):
        """A shove-stage ShoveFailure must surface as
        {"error": ..., "route_png": ...} — never the FastMCP
        success:true + text-error wrapper (and evidence still renders)."""
        from kcaa.router.pns.shove import ShoveFailure
        import kcaa.router.route_engine as re_mod

        def _boom(*_args, **_kwargs):
            raise ShoveFailure("cannot shove track (10, 20) -> (30, 40)")

        monkeypatch.setattr(re_mod, "shove_path", _boom)
        result = self._route_vcc(tools, crossing_board, strategy="shove", dry_run=True)
        assert "error" in result
        assert "success" not in result
        assert "shove failed" in result["error"]
        assert "cannot shove track" in result["error"]
        png = result.get("route_png")
        assert png is not None and os.path.isfile(png)
        os.remove(png)


# ── Shove write path (defense in depth) ────────────────────────────────


def test_apply_shoved_tracks_collapses_repeated_original() -> None:
    """``_apply_shoved_tracks`` with the same original shoved twice (a
    multi-leg route re-shoves a track from the board snapshot) must write
    ONLY the last displacement — both polylines would fork/double the
    physical track.  The router collapses pairs first; this guards any
    other caller."""
    import sexpdata

    from kcaa.router.pns.shove import TrackObstacle
    from kcaa.tools.pcb_routing_tools import _apply_shoved_tracks, _segment_fields

    orig = TrackObstacle(points=((40.0, 25.0), (55.0, 45.0)), width=0.25, net="GND", layer="F.Cu")
    d1 = TrackObstacle(
        points=((40.0, 25.0), (42.0, 32.0), (55.0, 45.0)), width=0.25, net="GND", layer="F.Cu"
    )
    d2 = TrackObstacle(
        points=((40.0, 25.0), (44.0, 30.0), (55.0, 45.0)), width=0.25, net="GND", layer="F.Cu"
    )

    def seg(x1, y1, x2, y2, width, layer, net):
        return [
            sexpdata.Symbol("segment"),
            [sexpdata.Symbol("start"), x1, y1],
            [sexpdata.Symbol("end"), x2, y2],
            [sexpdata.Symbol("width"), width],
            [sexpdata.Symbol("layer"), layer],
            [sexpdata.Symbol("net"), net],
        ]

    data = [
        seg(1.0, 1.0, 2.0, 1.0, 0.2, "F.Cu", "VCC"),
        seg(40.0, 25.0, 55.0, 45.0, 0.25, "F.Cu", "GND"),
    ]
    _apply_shoved_tracks(data, [(orig, d1), (orig, d2)])
    fields = [f for f in (_segment_fields(n) for n in data) if f is not None]
    gnd = [f for f in fields if f["net"] == "GND"]
    # Original gone; only d2's two hops present (d1's fork never written).
    assert all(
        not (abs(f["start"][0] - 40.0) <= 1e-6 and abs(f["end"][0] - 55.0) <= 1e-6) for f in gnd
    )
    assert len(gnd) == 2
    hops = {
        (
            round(f["start"][0], 6),
            round(f["start"][1], 6),
            round(f["end"][0], 6),
            round(f["end"][1], 6),
        )
        for f in gnd
    }
    assert hops == {(40.0, 25.0, 44.0, 30.0), (44.0, 30.0, 55.0, 45.0)}
    # The unrelated VCC segment is untouched.
    vcc = [f for f in fields if f["net"] == "VCC"]
    assert len(vcc) == 1 and vcc[0]["start"] == (1.0, 1.0) and vcc[0]["end"] == (2.0, 1.0)


# ── Route failure evidence render ──────────────────────────────────────


class TestPcbRouteFailureEvidence:
    def test_route_failure_returns_error_and_png(self, tools, board_with_tracks):
        result = _run(
            tools["pcb_route_pad_to_pad"](
                pcb_path=board_with_tracks,
                ref_a="R1",
                pad_a="1",
                ref_b="C99",
                pad_b="1",
                net="VCC",
                width=0.25,
                ctx=None,
            )
        )
        assert "error" in result
        png = result.get("route_png")
        assert png is not None
        assert os.path.isfile(png)
        assert png.startswith(os.path.join(tempfile.gettempdir(), "kcaa_route_"))
        with open(png, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"
        os.remove(png)

    def test_failure_returns_image_content_block(self, tools, board_with_tracks):
        """The failure envelope also carries the evidence PNG as an image
        content block (the VLM must SEE the failed endpoints, not just a
        temp path)."""
        from fastmcp.utilities.types import Image

        raw = _run_raw(
            tools["pcb_route_pad_to_pad"](
                pcb_path=board_with_tracks,
                ref_a="R1",
                pad_a="1",
                ref_b="C99",
                pad_b="1",
                net="VCC",
                width=0.25,
                ctx=None,
            )
        )
        assert isinstance(raw, tuple) and len(raw) == 2
        payload = json.loads(raw[0])
        assert "error" in payload
        assert payload["route_png"] is not None
        image = raw[1]
        assert isinstance(image, Image)
        assert image.data[:8] == b"\x89PNG\r\n\x1a\n"
        os.remove(payload["route_png"])

    def test_failure_render_silent_when_render_unavailable(
        self, tools, board_with_tracks, monkeypatch
    ):
        import kcaa.tools.pcb_routing_tools as prt

        def _boom(*_args, **_kwargs):
            raise RuntimeError("no display")

        monkeypatch.setattr(prt, "render_route_attempt", _boom)
        result = _run(
            tools["pcb_route_pad_to_pad"](
                pcb_path=board_with_tracks,
                ref_a="R1",
                pad_a="1",
                ref_b="C99",
                pad_b="1",
                net="VCC",
                width=0.25,
                ctx=None,
            )
        )
        assert "error" in result
        assert result["route_png"] is None
