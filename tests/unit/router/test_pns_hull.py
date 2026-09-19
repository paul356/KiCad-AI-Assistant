"""Unit tests for kcaa.router.pns.hull (seg/arc obstacle hulls)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon

from kcaa.router.pns.direction45 import CornerMode, build_initial_trace
from kcaa.router.pns.hull import arc_hull, buffer_distance, seg_hull

EPS = 1e-6


class TestBufferDistance:
    def test_half_width_plus_clearance(self):
        assert buffer_distance(0.2, 0.3) == pytest.approx(0.35, abs=EPS)
        assert buffer_distance(0.0, 0.1) == pytest.approx(0.05, abs=EPS)


class TestSegHull:
    def test_covers_segment(self):
        poly = seg_hull((0, 0), (10, 0), clearance=0.2, width=0.1)
        assert isinstance(poly, Polygon)
        assert poly.covers(Point(0, 0))
        assert poly.covers(Point(10, 0))
        assert poly.covers(Point(5, 0))

    def test_offset_distance(self):
        # A vertical segment: horizontal distance from the center line to the
        # hull edge must be clearance + width/2.
        poly = seg_hull((5, 0), (5, 10), clearance=0.2, width=0.4)
        xs = [pt[0] for pt in poly.exterior.coords]
        assert max(xs) == pytest.approx(5.4, abs=1e-3)  # 5 + 0.2 + 0.2
        assert min(xs) == pytest.approx(4.6, abs=1e-3)

    def test_zero_size_is_not_empty(self):
        poly = seg_hull((3, 3), (3, 3), clearance=0.1, width=0.2)
        assert not poly.is_empty


class TestArcHull:
    def _arc(self):
        t = build_initial_trace((0, 0), (8, 6), CornerMode.ROUNDED_45)
        return next(a for a in t.arcs if a is not None)

    def test_covers_arc_points(self):
        arc = self._arc()
        poly = arc_hull(arc, clearance=0.2, width=0.1)
        assert isinstance(poly, Polygon)
        for p in arc.as_polyline(n=16):
            assert poly.covers(Point(*p))

    def test_radius_offset(self):
        # Hull of the arc must extend clearance + width/2 beyond the arc.
        arc = self._arc()
        poly = arc_hull(arc, clearance=0.2, width=0.1)
        cx, cy = arc.center()
        far = max(math.hypot(pt[0] - cx, pt[1] - cy) for pt in poly.exterior.coords)
        assert far == pytest.approx(arc.radius + 0.25, abs=1e-3)

    def test_wide_sweep_uses_octagon(self):
        # A 270° CCW arc (chord 14.14 < clearance 15) triggers the
        # octagon fallback around the center — no sampling noise.
        from kcaa.router.pns.direction45 import arc_from_start_end_center

        arc = arc_from_start_end_center((10, 0), (0, -10), (0, 0), clockwise=False)
        assert abs(arc.total_sweep()) > math.pi  # long way around
        poly = arc_hull(arc, clearance=15.0, width=0.0)
        cx, cy = arc.center()
        r = math.hypot(arc.start[0] - cx, arc.start[1] - cy)
        # Octagon must fully contain the arc at radius r.
        for p in arc.as_polyline(n=64):
            assert poly.covers(Point(*p))
        # Every boundary point is at least r + clearance from the center
        # (inradius of the octagon), so nothing inside that radius escapes.
        for pt in poly.exterior.coords:
            assert math.hypot(pt[0] - cx, pt[1] - cy) >= r + 15.0 - 1e-6

    def test_degenerate_collinear_arc_empty_polygon(self):
        # A zero-radius/degenerate arc yields a non-crashing hull.
        from kcaa.router.pns.direction45 import ArcSeg

        arc = ArcSeg((0, 0), (0, 0), (0, 0), 0.0)
        poly = arc_hull(arc, 0.1, 0.0)
        assert poly.is_empty or poly.covers(Point(0, 0))
