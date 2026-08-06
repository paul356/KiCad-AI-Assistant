"""Unit tests for kcaa.router.world_model."""

from __future__ import annotations

from typing import Any

import pytest
import sexpdata

from kcaa.router.world_model import (
    WorldModel,
    build_world_model,
)


def _sym(name: str) -> Any:
    return sexpdata.Symbol(name)


def _make_footprint(
    ref: str, x: float, y: float, rot: float = 0.0, courtyard: list | None = None
) -> list:
    """Build a minimal footprint node with optional inline courtyard."""
    fp = [
        _sym("footprint"),
        "Test:Part",
        [_sym("layer"), "F.Cu"],
        [_sym("at"), x, y, rot],
        [_sym("property"), "Reference", ref],
    ]
    if courtyard:
        fp.extend(courtyard)
    # Pad 1
    fp.append(
        [
            _sym("pad"),
            "1",
            _sym("smd"),
            _sym("rect"),
            [_sym("at"), -0.5, 0.0],
            [_sym("size"), 0.5, 0.5],
            [_sym("layers"), "F.Cu", "F.Paste", "F.Mask"],
            [_sym("net"), "VCC"],
        ]
    )
    fp.append(
        [
            _sym("pad"),
            "2",
            _sym("smd"),
            _sym("rect"),
            [_sym("at"), 0.5, 0.0],
            [_sym("size"), 0.5, 0.5],
            [_sym("layers"), "F.Cu", "F.Paste", "F.Mask"],
            [_sym("net"), "GND"],
        ]
    )
    return fp


def _make_pcb(
    footprints: list[list] | None = None,
    segments: list[list] | None = None,
    vias: list[list] | None = None,
    zones: list[list] | None = None,
) -> list:
    pcb = [
        _sym("kicad_pcb"),
        [_sym("version"), 20260206],
        [_sym("generator"), "test"],
    ]
    pcb.extend(footprints or [])
    pcb.extend(segments or [])
    pcb.extend(vias or [])
    pcb.extend(zones or [])
    return pcb


def _write_pcb(tmp_path, pcb_data) -> str:
    path = tmp_path / "test.kicad_pcb"
    path.write_text(sexpdata.dumps(pcb_data))
    return str(path)


# ---------------------------------------------------------------------------
# build_world_model — empty PCB
# ---------------------------------------------------------------------------


class TestEmptyPcb:
    def test_empty_pcb_returns_empty_model(self, tmp_path):
        path = _write_pcb(tmp_path, _make_pcb())
        m = build_world_model(path)
        assert m.obstacles == []
        assert m.board_bbox is None
        assert m.nets() == set()

    def test_model_dataclass_defaults(self):
        m = WorldModel()
        assert m.obstacles == []
        assert m.board_bbox is None


# ---------------------------------------------------------------------------
# Footprint obstacles
# ---------------------------------------------------------------------------


class TestFootprintObstacle:
    def test_footprint_with_inline_courtyard(self, tmp_path):
        # 2x2 courtyard around footprint centre
        courtyard = [
            [
                _sym("fp_line"),
                [_sym("start"), -1.0, -1.0],
                [_sym("end"), 1.0, -1.0],
                [_sym("layer"), "F.CrtYd"],
                [_sym("width"), 0.05],
            ],
            [
                _sym("fp_line"),
                [_sym("start"), 1.0, 1.0],
                [_sym("end"), -1.0, 1.0],
                [_sym("layer"), "F.CrtYd"],
                [_sym("width"), 0.05],
            ],
        ]
        fp = _make_footprint("R1", 10.0, 20.0, courtyard=courtyard)
        path = _write_pcb(tmp_path, _make_pcb([fp]))
        m = build_world_model(path)
        # 2 pad obstacles (courtyard not treated as obstacle)
        assert len(m.obstacles) == 2

    def test_footprint_without_courtyard_is_skipped(self, tmp_path):
        fp = _make_footprint("R1", 10.0, 20.0)  # no courtyard
        path = _write_pcb(tmp_path, _make_pcb([fp]))
        m = build_world_model(path)
        # 2 pad obstacles (no courtyard → no courtyard obstacle)
        assert len(m.obstacles) == 2
        assert all(o.kind == "pad" for o in m.obstacles)


# ---------------------------------------------------------------------------
# Track obstacles
# ---------------------------------------------------------------------------


class TestTrackObstacle:
    def test_horizontal_segment(self, tmp_path):
        seg = [
            _sym("segment"),
            [_sym("start"), 10.0, 20.0],
            [_sym("end"), 20.0, 20.0],
            [_sym("width"), 0.5],
            [_sym("layer"), "F.Cu"],
            [_sym("net"), "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(segments=[seg]))
        m = build_world_model(path)
        assert len(m.obstacles) == 1
        o = m.obstacles[0]
        assert o.kind == "track"
        assert o.layers == frozenset({"F.Cu"})
        assert o.net == "VCC"
        minx, miny, maxx, maxy = o.shape.bounds
        assert minx == pytest.approx(10.0, abs=1e-6)
        assert maxx == pytest.approx(20.0, abs=1e-6)
        assert miny == pytest.approx(19.75, abs=1e-6)
        assert maxy == pytest.approx(20.25, abs=1e-6)

    def test_vertical_segment(self, tmp_path):
        seg = [
            _sym("segment"),
            [_sym("start"), 10.0, 20.0],
            [_sym("end"), 10.0, 30.0],
            [_sym("width"), 0.2],
            [_sym("layer"), "F.Cu"],
            [_sym("net"), "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(segments=[seg]))
        m = build_world_model(path)
        o = m.obstacles[0]
        assert o.kind == "track"
        minx, _, maxx, maxy = o.shape.bounds
        assert minx == pytest.approx(9.9, abs=1e-6)
        assert maxx == pytest.approx(10.1, abs=1e-6)
        assert maxy == pytest.approx(30.0, abs=1e-6)

    def test_diagonal_segment_builds_oriented_rect(self, tmp_path):
        seg = [
            _sym("segment"),
            [_sym("start"), 0.0, 0.0],
            [_sym("end"), 10.0, 10.0],
            [_sym("width"), 1.0],
            [_sym("layer"), "F.Cu"],
            [_sym("net"), "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(segments=[seg]))
        m = build_world_model(path)
        assert len(m.obstacles) == 1
        o = m.obstacles[0]
        # Oriented rect should be a valid polygon with non-zero area
        assert o.shape.area > 0
        # 45° line, length ~14.14, width 1 → bbox diagonal projection ~10.71
        minx, miny, maxx, maxy = o.shape.bounds
        assert (maxx - minx) == pytest.approx(10.707, abs=1e-2)

    def test_track_with_kicad9_net_format(self, tmp_path):
        seg = [
            _sym("segment"),
            [_sym("start"), 10.0, 20.0],
            [_sym("end"), 20.0, 20.0],
            [_sym("width"), 0.25],
            [_sym("layer"), "F.Cu"],
            [_sym("net"), 1, "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(segments=[seg]))
        m = build_world_model(path)
        assert m.obstacles[0].net == "VCC"

    def test_net_filter_excludes_same_net_tracks(self, tmp_path):
        seg = [
            _sym("segment"),
            [_sym("start"), 10.0, 20.0],
            [_sym("end"), 20.0, 20.0],
            [_sym("width"), 0.25],
            [_sym("layer"), "F.Cu"],
            [_sym("net"), "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(segments=[seg]))
        m = build_world_model(path, net_filter="VCC")
        assert m.obstacles == []
        m2 = build_world_model(path, net_filter="GND")
        assert len(m2.obstacles) == 1


# ---------------------------------------------------------------------------
# Via obstacles
# ---------------------------------------------------------------------------


class TestViaObstacle:
    def test_via_obstacle(self, tmp_path):
        via = [
            _sym("via"),
            [_sym("at"), 15.0, 25.0],
            [_sym("size"), 0.6],
            [_sym("drill"), 0.3],
            [_sym("layers"), "F.Cu", "B.Cu"],
            [_sym("net"), "VCC"],
        ]
        path = _write_pcb(tmp_path, _make_pcb(vias=[via]))
        m = build_world_model(path)
        assert len(m.obstacles) == 1
        o = m.obstacles[0]
        assert o.kind == "via"
        assert o.layers == frozenset({"F.Cu", "B.Cu"})
        assert o.net == "VCC"
        assert o.shape.area == pytest.approx(3.14159 * 0.3 * 0.3, abs=1e-3)


# ---------------------------------------------------------------------------
# Keepout obstacles
# ---------------------------------------------------------------------------


class TestKeepoutObstacle:
    def test_keepout_zone_becomes_obstacle(self, tmp_path):
        zone = [
            _sym("zone"),
            [_sym("net"), ""],
            [_sym("net_name"), ""],
            [_sym("layer"), "F.Cu"],
            [
                _sym("keepout"),
                [_sym("tracks"), _sym("not_allowed")],
            ],
            [
                _sym("polygon"),
                [
                    _sym("pts"),
                    [_sym("xy"), 10.0, 10.0],
                    [_sym("xy"), 20.0, 10.0],
                    [_sym("xy"), 20.0, 20.0],
                    [_sym("xy"), 10.0, 20.0],
                ],
            ],
        ]
        path = _write_pcb(tmp_path, _make_pcb(zones=[zone]))
        m = build_world_model(path)
        assert len(m.obstacles) == 1
        o = m.obstacles[0]
        assert o.kind == "keepout"
        assert o.layers == frozenset({"F.Cu"})
        assert o.shape.area == pytest.approx(100.0, abs=1e-6)

    def test_copper_pour_zone_is_not_obstacle(self, tmp_path):
        zone = [
            _sym("zone"),
            [_sym("net"), 1, "GND"],
            [_sym("net_name"), "GND"],
            [_sym("layer"), "F.Cu"],
            [
                _sym("polygon"),
                [
                    _sym("pts"),
                    [_sym("xy"), 0.0, 0.0],
                    [_sym("xy"), 50.0, 0.0],
                    [_sym("xy"), 50.0, 50.0],
                    [_sym("xy"), 0.0, 50.0],
                ],
            ],
        ]
        path = _write_pcb(tmp_path, _make_pcb(zones=[zone]))
        m = build_world_model(path)
        assert m.obstacles == []


# ---------------------------------------------------------------------------
# Fixture integration
# ---------------------------------------------------------------------------


class TestRoutingFixture:
    FIXTURE = "tests/integration/fixtures/test_routing_board.kicad_pcb"

    def test_fixture_has_expected_obstacles(self):
        m = build_world_model(self.FIXTURE)
        kinds = sorted({o.kind for o in m.obstacles})
        assert "track" in kinds
        assert "keepout" in kinds
        assert "pad" in kinds

    def test_fixture_vcc_filter_excludes_track(self):
        m = build_world_model(self.FIXTURE, net_filter="VCC")
        tracks = [o for o in m.obstacles if o.kind == "track"]
        assert tracks == []
