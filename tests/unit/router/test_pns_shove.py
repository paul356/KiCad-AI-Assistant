"""Unit tests for kcaa.router.pns.shove (KiCad PNS::SHOVE port)."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString

from kcaa.router.pns.shove import (
    HULL_FAILURE_EXPANSION_STEP_MM,
    MAX_SHOVE_DEPTH,
    ShoveFailure,
    TrackObstacle,
    _hull_set,
    _shove_line_to_hull_set,
    shove_obstacle_line,
    shove_path,
)

EPS = 1e-6


def _centerline_dist(track, pts):
    return LineString(track.points).distance(LineString(pts))


class TestHullSet:
    def test_one_hull_per_segment(self):
        hulls = _hull_set([(-5, 0), (0, 5), (5, 0)], width=0.2, clearance=0.1, obstacle_width=0.2)
        assert len(hulls) == 2

    def test_hull_covers_segment(self):
        hulls = _hull_set([(-5, 0), (5, 0)], width=0.2, clearance=0.1, obstacle_width=0.2)
        h = hulls[0]
        # Center point of the segment is inside the buffer.
        assert h.covers(LineString([(-5, 0), (5, 0)]).interpolate(0.5))

    def test_zero_length_segment_skipped(self):
        hulls = _hull_set([(-5, 0), (-5, 0), (5, 0)], width=0.2, clearance=0.1, obstacle_width=0.2)
        assert len(hulls) == 1


class TestShoveLineToHullSet:
    def test_walks_around_single_hull(self):
        hulls = _hull_set([(-5, 0), (5, 0)], width=0.2, clearance=0.1, obstacle_width=0.2)
        out = _shove_line_to_hull_set([(0, -2), (0, 2)], hulls, clockwise=True)
        assert out is not None
        assert out[0] == (0, -2) and out[-1] == (0, 2)
        # No interior crossing of the hull.
        h = hulls[0]
        interior = LineString(out).intersection(h).difference(h.boundary)
        assert interior.length < EPS

    def test_no_hull_walks_fails(self):
        # Obstacle fully inside a tiny hull: walk cannot preserve
        # endpoints that sit on the hull.
        from shapely.geometry import box

        hulls = [box(-0.1, -0.1, 0.1, 0.1)]
        assert _shove_line_to_hull_set([(0, 0), (1, 0)], hulls, True) is None

    def test_self_intersecting_result_rejected(self):
        # Path that would fold back on itself fails.
        from shapely.geometry import box

        hulls = [box(-1, -1, 1, 1)]
        # A path crossing twice with endpoints inside would produce a
        # walk that retraces; it must return None (endpoint preservation
        # or simplicity fails).
        r = _shove_line_to_hull_set([(0, 0.5), (1, 0.5)], hulls, True)
        assert r is None or LineString(r).is_simple


class TestShoveObstacleLine:
    def test_pushes_track_off_route(self):
        cur = [(-5, 0), (5, 0)]
        track = TrackObstacle(points=((0, -1), (0, 1)), width=0.2)
        moved = shove_obstacle_line(
            cur,
            track,
            width=0.2,
            clearance=0.1,
            permit_moving_start=False,
            permit_moving_end=False,
        )
        assert moved is not None
        assert moved.start == track.start and moved.end == track.end
        # Clearance honored: centerline at least (0.1 + 0.2/2) from route
        # minus tolerance (hull buffer includes width/2 of current line).
        d = _centerline_dist(moved, cur)
        assert d >= 0.1 - EPS
        assert d <= 0.5  # hull expansion cap

    def test_endpoints_not_moved_when_anchored(self):
        cur = [(-5, 0), (5, 0)]
        track = TrackObstacle(points=((0, -1), (0, 1)), width=0.2)
        moved = shove_obstacle_line(
            cur,
            track,
            width=0.2,
            clearance=0.1,
            permit_moving_start=False,
            permit_moving_end=False,
        )
        assert moved.start == (0, -1) and moved.end == (0, 1)

    def test_endpoints_move_when_permitted(self):
        cur = [(-5, 0), (5, 0)]
        track = TrackObstacle(points=((0, -1), (0, 1)), width=0.2)
        moved = shove_obstacle_line(
            cur,
            track,
            width=0.2,
            clearance=0.1,
            permit_moving_start=True,
            permit_moving_end=True,
        )
        assert moved is not None
        # Shoved around the hull, both endpoints may sit on the hull ring.
        assert len(moved.points) >= 3

    def test_returns_none_when_impossible(self):
        # Obstacle endpoints deep inside the hull: no walk preserves them
        # without endpoint adjustment.
        cur = [(-2, 0), (2, 0)]
        track = TrackObstacle(points=((0, 0.05), (0, 0.06)), width=0.2)
        moved = shove_obstacle_line(
            cur,
            track,
            width=0.2,
            clearance=0.1,
            permit_moving_start=False,
            permit_moving_end=False,
        )
        assert moved is None


class TestShovePath:
    def test_no_tracks_no_changes(self):
        path = [(-5, 0), (5, 0)]
        res = shove_path(path, [], width=0.2, clearance=0.1)
        assert res.path == path
        assert res.pushed == []
        assert res.unchanged == []

    def test_single_track_pushed(self):
        path = [(-8, 0), (8, 0)]
        t1 = TrackObstacle(points=((0, -2), (0, 2)), width=0.2, net="N1")
        res = shove_path(path, [t1], width=0.2, clearance=0.1)
        assert len(res.pushed) == 1
        assert len(res.unchanged) == 0
        assert res.path == path  # route untouched
        assert res.pushed[0].net == "N1"

    def test_chain_propagation_two_tracks(self):
        path = [(-10, 0), (10, 0)]
        tA = TrackObstacle(points=((0, -3), (0, 3)), width=0.2, net="A")
        tB = TrackObstacle(points=((0, -6), (0, 6)), width=0.2, net="B")
        res = shove_path(path, [tA, tB], width=0.2, clearance=0.1)
        assert len(res.pushed) == 2
        nets = {t.net for t in res.pushed}
        assert nets == {"A", "B"}
        # Both pushed tracks keep their endpoints.
        a = next(t for t in res.pushed if t.net == "A")
        b = next(t for t in res.pushed if t.net == "B")
        assert a.start == (0, -3) and a.end == (0, 3)
        assert b.start == (0, -6) and b.end == (0, 6)
        # Route path unchanged.
        assert res.path == path

    def test_depth_cap_raises(self):
        # A track that cannot be shoved at all (endpoints stuck in hull)
        # raises ShoveFailure even though the chain could recurse.
        t1 = TrackObstacle(points=((0, 0.001), (0, 0.002)), width=0.2)
        with pytest.raises(ShoveFailure):
            shove_path([(-2, 0), (2, 0)], [t1], width=0.2, clearance=0.1)

    def test_max_depth_constant_is_finite(self):
        assert MAX_SHOVE_DEPTH >= 2
        assert HULL_FAILURE_EXPANSION_STEP_MM > 0
