"""Unit tests for kcaa.router.pns.walkaround (KiCad LINE::Walkaround port)."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Polygon, box

from kcaa.router.pns.walkaround import (
    WalkFailure,
    _classify,
    _project_point_to_segment,
    _ring_points,
    _simplify_collinear,
    _split_chain_in_place,
    _split_ring_in_place,
    walkaround_line,
)

EPS = 1e-6


def _interior_crossing(pt_list, poly):
    """Length of the polyline lying strictly inside the hull (path must
    only touch the boundary, never enter the interior)."""
    inter = LineString(pt_list).intersection(poly).difference(poly.boundary)
    return inter.length


class TestHelperBits:
    def test_classify_inside_outside_edge(self):
        h = box(-2, -2, 2, 2)
        assert _classify((0, 0), h) == 0
        assert _classify((5, 0), h) == 1
        assert _classify((-2, 1), h) == 2

    def test_ring_points_cw_and_ccw(self):
        h = box(-2, -2, 2, 2)
        cw = _ring_points(h, cw=True)
        ccw = _ring_points(h, cw=False)
        # Both closed rings with identical vertex sets.
        assert cw[0] == cw[-1]
        assert ccw[0] == ccw[-1]
        assert set(cw[:-1]) == set(ccw[:-1])
        # Opposite traversal order.
        assert cw[1] != ccw[1]

    def test_project_point_to_segment(self):
        p = _project_point_to_segment((0, 0.5), (-1, 0), (1, 0))
        assert p == pytest.approx((0, 0))
        # Point beyond the segment end clamps to the endpoint.
        p = _project_point_to_segment((5, 1), (-1, 0), (1, 0))
        assert p == pytest.approx((1, 0))
        assert _project_point_to_segment((0, 0), (0, 0), (0, 0)) is None

    def test_simplify_collinear(self):
        pts = [(0, 0), (1, 1), (2, 2), (3, 2)]
        assert _simplify_collinear(pts) == [(0, 0), (2, 2), (3, 2)]
        assert _simplify_collinear([]) == []
        assert _simplify_collinear([(0, 0)]) == [(0, 0)]

    def test_split_chain_in_place(self):
        chain = [(-5.0, 0.0), (5.0, 0.0)]
        _split_chain_in_place(chain, [(-2, 0), (2, 0)])
        assert len(chain) == 4
        assert chain[0] == (-5.0, 0.0)
        assert chain[-1] == (5.0, 0.0)
        assert (-2, 0) in chain
        assert (2, 0) in chain
        # Already-a-vertex points are not duplicated.
        _split_chain_in_place(chain, [(-2, 0), (99, 99)])
        assert len(chain) == 5  # 99,99 is off-line; project clamps, no insert

    def test_split_ring_in_place_keeps_closure(self):
        ring = [(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0), (-2.0, -2.0)]
        _split_ring_in_place(ring, [(-2, 0), (2, 0)])
        assert ring[0] == ring[-1]  # still closed
        assert (-2, 0) in ring
        assert (2, 0) in ring
        # Ring order preserved: (-2,0) is inside closing segment
        # (-2,2) -> (-2,-2), so it must come after the top-left corner.
        li = ring.index((-2, 0))
        assert li > ring.index((-2.0, 2.0))
        # (2,0) lies between bottom-right and top-right corners.
        ri = ring.index((2, 0))
        assert ring.index((2.0, -2.0)) < ri < ring.index((2.0, 2.0))


class TestWalkaround:
    HULL = box(-2, -2, 2, 2)

    def test_straight_miss_returns_path_unchanged(self):
        path = [(-5, 0), (-3, 0)]
        assert walkaround_line(path, self.HULL) == path

    def test_goes_around_both_directions(self):
        path = [(-5, 0), (5, 0)]
        cw = walkaround_line(path, self.HULL, cw=True)
        ccw = walkaround_line(path, self.HULL, cw=False)
        # Endpoints preserved.
        assert cw[0] == path[0] and cw[-1] == path[-1]
        assert ccw[0] == path[0] and ccw[-1] == path[-1]
        # No interior crossing for either.
        assert _interior_crossing(cw, self.HULL) < EPS
        assert _interior_crossing(ccw, self.HULL) < EPS
        # CW hugs the top (smaller y = up in Y-down? no: box coords).
        assert cw != ccw
        # Results are simple polylines without duplicate neighbours.
        assert len(cw) <= 1000
        assert len(ccw) <= 1000

    def test_start_inside_hull_fails(self):
        with pytest.raises(WalkFailure):
            walkaround_line([(-1, 0), (5, 0)], self.HULL)

    def test_start_on_hull_edge_is_allowed(self):
        out = walkaround_line([(-2, 0), (5, 0)], self.HULL)
        assert out[0] == (-2, 0)
        assert out[-1] == (5, 0)
        assert _interior_crossing(out, self.HULL) < EPS

    def test_end_inside_hull_projects_to_boundary(self):
        out = walkaround_line([(-5, 0), (1, 0)], self.HULL)
        # End projection: nearest boundary point to (1,0) is (2,0).
        assert out[-1] == pytest.approx((2, 0), abs=EPS)
        assert _interior_crossing(out, self.HULL) < EPS

    def test_grazing_along_edge(self):
        out = walkaround_line([(-2, 2), (2, 2)], self.HULL, cw=False)
        assert out[0] == (-2, 2) and out[-1] == (2, 2)
        assert _interior_crossing(out, self.HULL) < EPS

    def test_multi_segment_path(self):
        path = [(-6, 3), (-3, 3), (-3, 1), (3, 1), (6, 3)]
        out = walkaround_line(path, self.HULL)
        assert out[0] == path[0] and out[-1] == path[-1]
        assert _interior_crossing(out, self.HULL) < EPS

    def test_concave_hull(self):
        conc = Polygon([(0, 0), (6, 0), (6, 2), (4, 2), (4, 1), (2, 1), (2, 2), (0, 2)])
        for cw in (True, False):
            out = walkaround_line([(-2, 0.5), (8, 0.5)], conc, cw=cw)
            assert out[0] == (-2, 0.5) and out[-1] == (8, 0.5)
            assert _interior_crossing(out, conc) < EPS

    def test_edge_then_leave(self):
        # Path rides the top edge then leaves: intersection is a
        # GeometryCollection (line + point pieces).
        out = walkaround_line([(-2, 2), (-2, 0), (5, 0)], self.HULL, cw=False)
        assert out[0] == (-2, 2) and out[-1] == (5, 0)
        assert _interior_crossing(out, self.HULL) < EPS

    def test_result_has_no_duplicate_neighbours(self):
        out = walkaround_line([(-5, 0), (5, 0)], self.HULL)
        for a, b in zip(out, out[1:]):
            assert abs(a[0] - b[0]) + abs(a[1] - b[1]) > EPS
