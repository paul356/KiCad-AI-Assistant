"""Unit tests for kcaa.router.pns.line (PNS Line data structure)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString

from kcaa.router.pns.direction45 import CornerMode, build_initial_trace
from kcaa.router.pns.line import Line

EPS = 1e-6


def _make_line(*, mode: CornerMode = CornerMode.MITERED_45, p0=(0, 0), p1=(12, 5), **kw) -> Line:
    trace = build_initial_trace(p0, p1, mode, **kw)
    return Line.from_trace(trace, width=0.25, net="N$1", layer="F.Cu")


class TestLineAttrPassthrough:
    def test_wraps_trace_attrs(self):
        t = build_initial_trace((0, 0), (12, 5), CornerMode.MITERED_45)
        line = Line.from_trace(t, width=0.2, net="CLK", layer="B.Cu")
        assert line.width == 0.2
        assert line.net == "CLK"
        assert line.layer == "B.Cu"
        assert line.points == t.points
        assert line.arcs == t.arcs

    def test_points_copied_not_shared(self):
        t = build_initial_trace((0, 0), (12, 5), CornerMode.MITERED_45)
        line = Line.from_trace(t, width=0.1, net="", layer="")
        line.points.append((99, 99))
        assert len(t.points) == 3  # original trace untouched


class TestLineGeometry:
    def test_start_end(self):
        for mode in (CornerMode.MITERED_45, CornerMode.ROUNDED_45, CornerMode.ROUNDED_90):
            line = _make_line(mode=mode)
            assert line.start() == (0, 0)
            assert line.end() == (12, 5)

    def test_length_matches_trace(self):
        for mode in (CornerMode.MITERED_45, CornerMode.ROUNDED_45, CornerMode.ROUNDED_90):
            line = _make_line(mode=mode)
            trace = build_initial_trace((0, 0), (12, 5), mode)
            assert line.length() == pytest.approx(trace.length(), abs=EPS)

    def test_length_mitered_manual(self):
        # w=12 h=5: mp0 = (7,0), mp1 = (5,5); length = 7 + sqrt(50)
        line = _make_line()
        assert line.length() == pytest.approx(7.0 + math.sqrt(50.0), abs=EPS)

    def test_as_shapely_linestring(self):
        line = _make_line()
        g = line.as_shapely()
        assert isinstance(g, LineString)
        assert g.is_valid
        assert g.length == pytest.approx(line.length(), abs=EPS)

    def test_as_polyline_keeps_endpoints_rounded(self):
        line = _make_line(mode=CornerMode.ROUNDED_45)
        poly = line.as_polyline()
        assert poly[0] == pytest.approx((0, 0), abs=EPS)
        assert poly[-1] == pytest.approx((12, 5), abs=EPS)


class TestIterSegments:
    def test_mitered_all_straight(self):
        line = _make_line()
        segs = list(line.iter_segments())
        assert len(segs) == 2
        for idx, pts in segs:
            assert len(pts) == 2
        assert segs[0][0] == 0
        assert segs[1][0] == 1

    def test_rounded_arc_segment_sampled(self):
        line = _make_line(mode=CornerMode.ROUNDED_45)
        segs = list(line.iter_segments())
        assert len(segs) == 2
        arc_idx = next(i for i, (idx, _) in enumerate(segs) if line.arcs[idx] is not None)
        # The arc segment samples to many points, straight ones to two.
        assert len(segs[arc_idx][1]) > 2

    def test_each_arc_segment_samples_on_radius(self):
        line = _make_line(mode=CornerMode.ROUNDED_45)
        for idx, pts in line.iter_segments(arc_pts=16):
            arc = line.arcs[idx] if idx < len(line.arcs) else None
            if arc is None:
                assert len(pts) == 2
            else:
                cx, cy = arc.center()
                for p in pts:
                    r = math.hypot(p[0] - cx, p[1] - cy)
                    assert r == pytest.approx(arc.radius, abs=1e-6)
