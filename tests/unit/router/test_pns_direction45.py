"""Unit tests for kcaa.router.pns.direction45 (KiCad BuildInitialTrace port)."""

from __future__ import annotations

import math

import pytest

from kcaa.router.pns.direction45 import (
    ArcSeg,
    CornerMode,
    Trace,
    arc_from_start_end_angle,
    arc_from_start_end_center,
    build_initial_trace,
)

EPS = 1e-6


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _assert_endpoints(trace: Trace, p0, p1):
    """Contract: skeleton starts/ends at the requested points."""
    assert trace.points[0] == pytest.approx(p0, abs=EPS)
    assert trace.points[-1] == pytest.approx(p1, abs=EPS)
    poly = trace.as_polyline()
    assert poly[0] == pytest.approx(p0, abs=EPS)
    assert poly[-1] == pytest.approx(p1, abs=EPS)


def _assert_arc_valid(arc: ArcSeg):
    """Contract: all three points equidistant from the reconstructed center."""
    cx, cy = arc.center()
    r0 = _dist(arc.start, (cx, cy))
    r1 = _dist(arc.mid, (cx, cy))
    r2 = _dist(arc.end, (cx, cy))
    assert r0 == pytest.approx(r1, abs=EPS)
    assert r0 == pytest.approx(r2, abs=EPS)
    assert r0 == pytest.approx(arc.radius, abs=EPS)


class TestShortcuts:
    """Single-segment degenerate cases."""

    @pytest.mark.parametrize("mode", list(CornerMode))
    def test_axis_aligned(self, mode):
        t = build_initial_trace((0, 0), (10, 0), mode)
        assert t.points == [(0, 0), (10, 0)]
        assert t.arcs == []
        _assert_endpoints(t, (0, 0), (10, 0))

    @pytest.mark.parametrize("mode", list(CornerMode))
    def test_zero_span(self, mode):
        t = build_initial_trace((3, 4), (3, 4), mode)
        assert t.points == [(3, 4), (3, 4)]

    @pytest.mark.parametrize("mode", [CornerMode.MITERED_45, CornerMode.ROUNDED_45])
    def test_square_span_45_modes_single_segment(self, mode):
        t = build_initial_trace((0, 0), (6, 6), mode)
        assert t.points == [(0, 0), (6, 6)]
        assert t.arcs == []


class TestMitered45:
    def test_wider_than_tall(self):
        # w=8 h=4, horizontal first then diagonal NE.
        t = build_initial_trace((0, 0), (8, 4), CornerMode.MITERED_45)
        assert t.points[1] == pytest.approx((4, 0), abs=EPS)
        _assert_endpoints(t, (0, 0), (8, 4))

    def test_wider_start_diagonal(self):
        t = build_initial_trace((0, 0), (8, 4), CornerMode.MITERED_45, start_diagonal=True)
        assert t.points[1] == pytest.approx((4, 4), abs=EPS)
        _assert_endpoints(t, (0, 0), (8, 4))

    def test_taller_than_wide(self):
        # w=4 h=8, vertical first then diagonal NE.
        t = build_initial_trace((0, 0), (4, 8), CornerMode.MITERED_45)
        assert t.points[1] == pytest.approx((0, 4), abs=EPS)
        _assert_endpoints(t, (0, 0), (4, 8))

    def test_negative_direction(self):
        t = build_initial_trace((8, 4), (0, 0), CornerMode.MITERED_45)
        # w=8 h=4, sw=-1 sh=-1: mp0 = (-4, 0)
        assert t.points[1] == pytest.approx((4, 4), abs=EPS)
        _assert_endpoints(t, (8, 4), (0, 0))


class TestMitered90:
    def test_defaults_horizontal_first(self):
        t = build_initial_trace((0, 0), (8, 4), CornerMode.MITERED_90)
        assert t.points[1] == pytest.approx((8, 0), abs=EPS)
        _assert_endpoints(t, (0, 0), (8, 4))

    def test_start_diagonal_vertical_first(self):
        t = build_initial_trace((0, 0), (8, 4), CornerMode.MITERED_90, start_diagonal=True)
        assert t.points[1] == pytest.approx((0, 4), abs=EPS)
        _assert_endpoints(t, (0, 0), (8, 4))


class TestRounded45:
    def test_manual_sample_wider_negative_tangent(self):
        """Hand-derived: p0=(0,0) p1=(8,6) w>h → w=8 h=6 mp0=(2,0)
        mp1=(6,6) |mp1|=8.485 tangent=-6.485<0 → arc at START from (0,0)
        via center construction; rot_sign = sw*sh*-1 = -1.
        Arc: (0,0)→(3.414,1.414), radius 4.8284, center (0,4.8284);
        straight leg continues at 45° to p1."""
        t = build_initial_trace((0, 0), (8, 6), CornerMode.ROUNDED_45)
        _assert_endpoints(t, (0, 0), (8, 6))
        assert t.arcs[0] is not None and t.arcs[1] is None  # arc first
        arc = t.arcs[0]
        assert arc.start == pytest.approx((0, 0), abs=EPS)
        assert arc.end == pytest.approx((3.414213, 1.414213), abs=1e-3)
        assert arc.radius == pytest.approx(4.828427, abs=1e-3)
        assert arc.center() == pytest.approx((0, 4.828427), abs=1e-3)
        _assert_arc_valid(arc)
        assert t.points[1] == pytest.approx((3.414213, 1.414213), abs=1e-3)

    def test_sample_wider_positive_tangent(self):
        """p0=(0,0) p1=(40,4): w=40 h=4 mp0=(36,0) mp1=(4,4) |mp1|=5.657
        tangent=30.34>0 → arc at END from (30.34,0) to p1."""
        t = build_initial_trace((0, 0), (40, 4), CornerMode.ROUNDED_45)
        _assert_endpoints(t, (0, 0), (40, 4))
        assert t.arcs[0] is None and t.arcs[1] is not None
        arc = t.arcs[1]
        assert arc.start == pytest.approx((30.34, 0), abs=1e-2)
        assert arc.end == pytest.approx((40, 4), abs=1e-6)
        assert arc.radius == pytest.approx(13.656854, abs=1e-2)
        _assert_arc_valid(arc)

    def test_sample_wider_start_diagonal(self):
        """Same 40x4 but start_diagonal: arc at END from p0 to
        p1 - mp0.resize(tangent)."""
        t = build_initial_trace((0, 0), (40, 4), CornerMode.ROUNDED_45, start_diagonal=True)
        _assert_endpoints(t, (0, 0), (40, 4))
        assert t.arcs[0] is not None and t.arcs[1] is None
        arc = t.arcs[0]
        assert arc.start == pytest.approx((0, 0), abs=1e-6)
        assert arc.end == pytest.approx((9.66, 4), abs=1e-2)
        assert arc.radius == pytest.approx(13.656854, abs=1e-2)
        _assert_arc_valid(arc)

    def test_sample_taller_than_wide(self):
        """p0=(0,0) p1=(6,10): w=6 h=10 mp0=(0,4) mp1=(6,6) |mp1|=8.485
        tangent=4-8.485<0 → arc at START (center construction), then
        straight leg at 45° to p1."""
        t = build_initial_trace((0, 0), (6, 10), CornerMode.ROUNDED_45)
        _assert_endpoints(t, (0, 0), (6, 10))
        assert t.arcs[0] is not None and t.arcs[1] is None
        arc = t.arcs[0]
        assert arc.start == pytest.approx((0, 0), abs=EPS)
        assert arc.end == pytest.approx((2.828427, 6.828427), abs=1e-3)
        assert arc.radius == pytest.approx(9.656854, abs=1e-3)
        _assert_arc_valid(arc)

    def test_sample_wider_start_diagonal_positive_tangent(self):
        """p0=(0,0) p1=(4,10) start_diagonal: w=4 h=10 mp0=(0,6) mp1=(4,4)
        |mp1|=5.657 tangent=6-5.657=0.343>0 → arc at START from p0 to
        arcEndpoint = aP1 - mp0.Resize(tangent) = (4, 9.657), then
        straight leg to p1 along mp0."""
        t = build_initial_trace((0, 0), (4, 10), CornerMode.ROUNDED_45, start_diagonal=True)
        _assert_endpoints(t, (0, 0), (4, 10))
        assert t.arcs[0] is not None and t.arcs[1] is None
        arc = t.arcs[0]
        assert arc.start == pytest.approx((0, 0), abs=EPS)
        assert arc.end == pytest.approx((4.0, 9.656854), abs=1e-3)
        _assert_arc_valid(arc)

    def test_sample_start_diagonal_negative_tangent(self):
        """p0=(0,0) p1=(8,6) start_diagonal: w=8 h=6 mp0=(2,0)
        mp1=(6,6) |mp1|=8.485 tangent=2-8.485<0 → arc at END, diagonal
        leg first from p0 along mp1 to arc_start, then arc to p1."""
        t = build_initial_trace((0, 0), (8, 6), CornerMode.ROUNDED_45, start_diagonal=True)
        _assert_endpoints(t, (0, 0), (8, 6))
        assert t.arcs[0] is None and t.arcs[1] is not None
        arc = t.arcs[1]
        assert arc.end == pytest.approx((8, 6), abs=1e-6)
        assert arc.radius == pytest.approx(4.828427, abs=1e-3)
        _assert_arc_valid(arc)
        # Straight leg p0 -> arc.start runs along mp1 = (6,6)/|mp1|.
        mid_pt = t.points[1]
        assert mid_pt[0] == pytest.approx(mid_pt[1], abs=EPS)
        assert arc.start == pytest.approx(mid_pt, abs=EPS)

    @pytest.mark.parametrize(
        "p0,p1",
        [
            ((0, 0), (8, 6)),
            ((8, 6), (0, 0)),
            ((0, 6), (8, 0)),
            ((8, 0), (0, 6)),
        ],
    )
    def test_all_quadrants_endpoints_and_arcs(self, p0, p1):
        for mode in (CornerMode.ROUNDED_45, CornerMode.ROUNDED_90):
            for sd in (False, True):
                t = build_initial_trace(p0, p1, mode, start_diagonal=sd)
                _assert_endpoints(t, p0, p1)
                arcs = [a for a in t.arcs if a is not None]
                assert len(arcs) == 1
                _assert_arc_valid(arcs[0])


class TestRounded90:
    def test_square_span_single_arc(self):
        """w==h → single 90° arc, no straight legs. Center from aP1-mp0."""
        t = build_initial_trace((0, 0), (6, 6), CornerMode.ROUNDED_90)
        assert t.points == [(0, 0), (6, 6)]
        assert len(t.arcs) == 1
        arc = t.arcs[0]
        assert arc is not None
        _assert_arc_valid(arc)
        assert abs(arc.sweep()) == pytest.approx(math.pi / 2, abs=1e-6)
        assert arc.radius == pytest.approx(6.0, abs=1e-6)  # 45° line radius
        _assert_endpoints(t, (0, 0), (6, 6))

    def test_wider_than_tall(self):
        t = build_initial_trace((0, 0), (10, 4), CornerMode.ROUNDED_90)
        _assert_endpoints(t, (0, 0), (10, 4))
        # Straight leg first: points [p0, arc_start, p1], arc in slot 1.
        assert t.arcs[0] is None and t.arcs[1] is not None
        arc = t.arcs[1]
        # Straight leg runs along y=0 from (0,0) to (6,0), arc from (6,0)
        # to (10,4) centered at (6,4).
        assert arc.start == pytest.approx((6, 0), abs=EPS)
        assert arc.end == pytest.approx((10, 4), abs=EPS)
        assert arc.center() == pytest.approx((6, 4), abs=EPS)
        assert arc.radius == pytest.approx(4.0, abs=EPS)
        _assert_arc_valid(arc)

    def test_taller_than_wide(self):
        t = build_initial_trace((0, 0), (4, 10), CornerMode.ROUNDED_90)
        _assert_endpoints(t, (0, 0), (4, 10))
        assert t.arcs[0] is None and t.arcs[1] is not None
        arc = t.arcs[1]
        assert arc.start == pytest.approx((0, 6), abs=EPS)
        assert arc.end == pytest.approx((4, 10), abs=EPS)
        assert arc.center() == pytest.approx((4, 6), abs=EPS)
        _assert_arc_valid(arc)

    def test_start_diagonal(self):
        t = build_initial_trace((0, 0), (10, 4), CornerMode.ROUNDED_90, start_diagonal=True)
        _assert_endpoints(t, (0, 0), (10, 4))
        # Arc first from (0,0) to (4,4) centered (4,0), then straight to (10,4).
        assert t.arcs[0] is not None and t.arcs[1] is None
        arc = t.arcs[0]
        assert arc.start == pytest.approx((0, 0), abs=EPS)
        assert arc.end == pytest.approx((4, 4), abs=EPS)
        assert arc.center() == pytest.approx((4, 0), abs=EPS)
        _assert_arc_valid(arc)


class TestArcGeometry:
    def test_arc_from_start_end_angle_known_case(self):
        """45° arc (0,0)→(3.414,1.414): chord length 3.695, radius 4.828."""
        arc = arc_from_start_end_angle((0, 0), (3.414213, 1.414213), math.pi / 4)
        assert arc.radius == pytest.approx(4.828427, abs=1e-3)
        _assert_arc_valid(arc)

    def test_arc_sweep_sign_controls_direction(self):
        # Positive sweep (math-CCW): center on the +normal side of the chord.
        # Chord (0,0)->(10,0), CCW normal +y → center (5,5).
        arc_ccw = arc_from_start_end_angle((0, 0), (10, 0), math.pi / 2)
        arc_cw = arc_from_start_end_angle((0, 0), (10, 0), -math.pi / 2)
        assert arc_ccw.center() == pytest.approx((5, 5), abs=EPS)
        assert arc_cw.center() == pytest.approx((5, -5), abs=EPS)
        assert arc_ccw.sweep() == pytest.approx(math.pi / 2, abs=EPS)
        assert arc_cw.sweep() == pytest.approx(-math.pi / 2, abs=EPS)
        # Both endpoints hit the target.
        assert arc_ccw.end == (10, 0)
        assert arc_cw.end == (10, 0)

    def test_arc_from_start_end_center(self):
        arc = arc_from_start_end_center((6, 0), (10, 4), (6, 4), clockwise=False)
        assert arc.radius == pytest.approx(4.0, abs=EPS)
        _assert_arc_valid(arc)
        assert arc.sweep() == pytest.approx(math.pi / 2, abs=EPS)

    def test_arc_polyline_continuity(self):
        """Sampled polyline must stay on the circle and terminate exactly."""
        arc = arc_from_start_end_angle((0, 0), (3.414213, 1.414213), math.pi / 4)
        pts = arc.as_polyline(n=64)
        cx, cy = arc.center()
        for p in pts:
            assert _dist(p, (cx, cy)) == pytest.approx(arc.radius, abs=1e-6)
        assert pts[0] == pytest.approx((0, 0), abs=EPS)
        assert pts[-1] == pytest.approx((3.414213, 1.414213), abs=1e-6)


class TestTraceContract:
    def test_polyline_arc_sampling_keeps_endpoints(self):
        t = build_initial_trace((2, 3), (14, 9), CornerMode.ROUNDED_45)
        _assert_endpoints(t, (2, 3), (14, 9))

    def test_trace_length_positive(self):
        for mode in (CornerMode.MITERED_45, CornerMode.ROUNDED_45, CornerMode.ROUNDED_90):
            t = build_initial_trace((0, 0), (12, 5), mode)
            assert t.length() > 0
        # Straight distance is a lower bound.
        t45 = build_initial_trace((0, 0), (12, 5), CornerMode.MITERED_45)
        assert t45.length() == pytest.approx(math.sqrt(50.0) + 7.0, abs=EPS)  # mp0 + diag

    def test_negative_sign_and_reversed(self):
        """Mirrors through all four octant sign combinations."""
        for a, b in [((0, 0), (5, 9)), ((0, 9), (5, 0))]:
            t = build_initial_trace(a, b, CornerMode.ROUNDED_45)
            _assert_endpoints(t, a, b)
            arcs = [arc for arc in t.arcs if arc is not None]
            assert len(arcs) == 1
            _assert_arc_valid(arcs[0])
