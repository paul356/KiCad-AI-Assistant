"""Unit tests for kcaa.router.route_engine (PNS engine pipeline)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Polygon

from kcaa.router.route_engine import (
    PnsFailure,
    _path_len,
    _rect_medians,
    _track_centerline,
    _walkaround_solids,
    route_engine,
)
from kcaa.router.world_model import Obstacle

W = 0.2  # track width
CLR = 0.1


def _pad(x: float, y: float, half: float = 1.0) -> Obstacle:
    return Obstacle(
        shape=Polygon(
            [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]
        ),
        layers=frozenset({"F.Cu"}),
        net=None,
        kind="pad",
    )


def _track_obs(x: float, y0: float, y1: float, net: str) -> Obstacle:
    return Obstacle(
        shape=Polygon([(x - W / 2, y0), (x + W / 2, y0), (x + W / 2, y1), (x - W / 2, y1)]),
        layers=frozenset({"F.Cu"}),
        net=net,
        kind="track",
    )


class TestRectExtraction:
    def test_horizontal_rect_centerline(self):
        poly = Polygon([(0, -0.1), (20, -0.1), (20, 0.1), (0, 0.1)])
        pts = _track_centerline(poly)
        assert pts == [(20.0, 0.0), (0.0, 0.0)]

    def test_vertical_rect_centerline(self):
        poly = Polygon([(5.0, 0.0), (5.0, 10.0), (4.8, 10.0), (4.8, 0.0)])
        pts = _track_centerline(poly)
        assert pts == [(4.9, 10.0), (4.9, 0.0)]

    def test_width_from_short_axis(self):
        poly = Polygon([(0, -0.15), (20, -0.15), (20, 0.15), (0, 0.15)])
        _med = _rect_medians(poly)
        assert _med is not None
        _a, _b, long_len, short_len = _med
        assert long_len == pytest.approx(20.0)
        assert short_len == pytest.approx(0.3)

    def test_oriented_rect_medians(self):
        # 45-degree track from (0,0) to (10,10), width 1: axis-aligned
        # bbox is a square so the original long axis must be recovered
        # from the oriented rect's vertex order.
        from kcaa.router.world_model import _oriented_rect

        poly = _oriented_rect(0, 0, 10, 10, 1.0)
        med = _rect_medians(poly)
        assert med is not None
        a_long, b_long, long_len, short_len = med
        assert long_len == pytest.approx(math.hypot(10, 10), abs=1e-6)
        assert short_len == pytest.approx(1.0, abs=1e-6)
        # Long axis endpoints are the centers of the width edges — the
        # original segment endpoints (any order).
        endpoints = {a_long, b_long}
        assert endpoints == {(0.0, 0.0), (10.0, 10.0)}

    def test_non_rect_shape_rejected(self):
        # Square: equal medians, direction ambiguous for a track — rejected.
        square = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
        assert _track_centerline(square) is None
        # Non-4-vertex (arc buffer) shape rejected.
        arc = LineString([(0, 0), (1, 1), (2, 0)]).buffer(0.2, cap_style="round")
        assert len(arc.exterior.coords) != 5
        assert _track_centerline(arc) is None


class TestWalkaroundSolids:
    def test_clear_path_untouched(self):
        from kcaa.router.pns.node import ObstacleNode

        node = ObstacleNode([])
        path = [(-8, 0), (8, 0)]
        out = _walkaround_solids(path, node, W, CLR)
        assert out == path

    def test_walks_around_single_pad(self):
        from kcaa.router.pns.node import ObstacleNode

        node = ObstacleNode([_pad(0, 0)])
        out = _walkaround_solids([(-8, 0), (8, 0)], node, W, CLR)
        # Detours around the pad, endpoints kept, clearance honored.
        assert out[0] == (-8, 0) and out[-1] == (8, 0)
        d = LineString(out).distance(_pad(0, 0).shape)
        assert d >= CLR - 1e-6

    def test_two_pads_each_visited(self):
        from kcaa.router.pns.node import ObstacleNode

        node = ObstacleNode([_pad(-3, 0), _pad(3, 0)])
        out = _walkaround_solids([(-8, 0), (8, 0)], node, W, CLR)
        assert out[0] == (-8, 0) and out[-1] == (8, 0)
        for pad in (_pad(-3, 0), _pad(3, 0)):
            assert LineString(out).distance(pad.shape) >= CLR - 1e-6

    def test_unwalkable_raises(self):
        from kcaa.router.pns.node import ObstacleNode

        # Canyon: a central wall plus caps above and below trap the
        # direct path; every walkaround bounce (CW or CCW) leads into
        # another wall, so the engine must raise PnsFailure instead of
        # returning a crossing path.
        walls = [
            Obstacle(
                shape=Polygon([(-0.3, -5), (0.3, -5), (0.3, 5), (-0.3, 5)]),
                layers=frozenset({"F.Cu"}),
                net=None,
                kind="pad",
            ),
            Obstacle(
                shape=Polygon([(-5, 3), (5, 3), (5, 3.3), (-5, 3.3)]),
                layers=frozenset({"F.Cu"}),
                net=None,
                kind="pad",
            ),
            Obstacle(
                shape=Polygon([(-5, -3.3), (5, -3.3), (5, -3), (-5, -3)]),
                layers=frozenset({"F.Cu"}),
                net=None,
                kind="pad",
            ),
        ]
        node = ObstacleNode(walls)
        with pytest.raises(PnsFailure):
            _walkaround_solids([(-8, 0), (8, 0)], node, W, CLR)


class TestPathLen:
    def test_straight(self):
        assert _path_len([(0, 0), (3, 4)]) == pytest.approx(5.0)


class TestRouteEngine:
    def test_no_obstacles_straight_path(self):
        res = route_engine((-8, 0), (8, 0), [], W, CLR)
        assert res.path == [(-8, 0), (8, 0)]
        assert res.shoved_tracks == []

    def test_fixed_pad_detour(self):
        res = route_engine((-8, 0), (8, 0), [_pad(0, 0)], W, CLR)
        assert res.path[0] == (-8, 0) and res.path[-1] == (8, 0)
        assert LineString(res.path).distance(_pad(0, 0).shape) >= CLR - 1e-6
        assert res.shoved_tracks == []

    def test_track_is_shoved_not_detoured(self):
        # The route stays straight; the movable track is pushed to the
        # side instead.
        t = _track_obs(5, -3, 3, "N2")
        res = route_engine((-8, 0), (8, 0), [_pad(0, 0), t], W, CLR)
        # Straight-ish route: the track is shoved, no walkaround of it.
        assert res.path[0] == (-8, 0) and res.path[-1] == (8, 0)
        assert len(res.shoved_tracks) == 1
        pushed = res.shoved_tracks[0]
        assert pushed.net == "N2"
        assert pushed.start == (5, -3) and pushed.end == (5, 3)
        d = LineString(pushed.points).distance(LineString(res.path))
        assert d >= CLR + W / 2 - 1e-6

    def test_subwidth_short_track_is_fixed_not_shoved(self):
        # A track shorter than its width (0.2 mm tap-in inside a 0.5 mm
        # pad entry) has no well-defined shove direction.  The world
        # model records its exact centerline + width; the engine treats
        # it as a fixed solid and routes around it.
        t = Obstacle(
            shape=Polygon([(5 - W, -0.1), (5 + W, -0.1), (5 + W, 0.1), (5 - W, 0.1)]),
            layers=frozenset({"F.Cu"}),
            net="N2",
            kind="track",
            track_centerline=((5.0, -0.1), (5.0, 0.1)),  # 0.2 mm long, W wide
            track_width=W,
        )
        res = route_engine((-8, 0), (8, 0), [_pad(0, 0), t], W, CLR)
        assert res.shoved_tracks == []
        assert res.path[0] == (-8, 0) and res.path[-1] == (8, 0)
        assert LineString(res.path).distance(t.shape) >= CLR - 1e-6

    def test_arc_is_fixed_obstacle(self):
        # Arc-shaped track is not movable (no rect centerline): the
        # engine walks around it.
        arc = Obstacle(
            shape=LineString([(0, 0), (1, 0.5), (2, 0)]).buffer(W / 2, cap_style="round"),
            layers=frozenset({"F.Cu"}),
            net="ARCNET",
            kind="track",
        )
        res = route_engine((-6, 0), (6, 0), [arc], W, CLR)
        assert res.shoved_tracks == []
        assert res.path[0] == (-6, 0) and res.path[-1] == (6, 0)
        assert LineString(res.path).distance(arc.shape) >= CLR - 1e-6

    def test_corner_mode_affects_skeleton(self):
        from kcaa.router.pns.direction45 import CornerMode

        res45 = route_engine((-8, 0), (8, 8), [], W, CLR, corner_mode=CornerMode.MITERED_45)
        res90 = route_engine((-8, 0), (8, 8), [], W, CLR, corner_mode=CornerMode.MITERED_90)
        assert res45.path != res90.path

    def test_rounded45_keeps_skeleton_arc(self):
        # No obstacles: a rounded corner in the skeleton must surface as
        # an EngineResult arc with a consistent radius through mid.
        import math

        from kcaa.router.pns.direction45 import CornerMode

        res = route_engine((0, 0), (10, 5), [], W, CLR, corner_mode=CornerMode.ROUNDED_45)
        assert len(res.arcs) == 1
        a = res.arcs[0]
        assert a.start == (0, 0)
        # The arc spans the first skeleton leg; the remaining leg is a
        # straight segment to the destination.
        assert res.path[-1] == (10, 5)
        cx, cy = a.center()
        for pt in (a.start, a.mid, a.end):
            r = math.hypot(pt[0] - cx, pt[1] - cy)
            assert r == pytest.approx(a.radius)

    def test_rounded45_detour_linearizes_arc(self):
        # An obstacle on the way forces a walkaround; the arc is
        # linearized (KiCad does not keep an arc through a detour).
        from kcaa.router.pns.direction45 import CornerMode

        wall = Obstacle(
            shape=Polygon([(5, 0), (6, 0), (6, 6), (5, 6)]),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        res = route_engine((0, 0), (10, 5), [wall], W, CLR, corner_mode=CornerMode.ROUNDED_45)
        assert res.arcs == []
        assert res.path[0] == (0, 0) and res.path[-1] == (10, 5)

    def test_mitered45_never_emits_arcs(self):
        from kcaa.router.pns.direction45 import CornerMode

        res = route_engine((0, 0), (10, 5), [], W, CLR, corner_mode=CornerMode.MITERED_45)
        assert res.arcs == []


class TestMovableExtraction:
    def test_track_obstacle_becomes_track(self):
        t = _track_obs(5, -3, 3, "N2")
        med = _rect_medians(t.shape)
        assert med is not None
        assert med[2] == pytest.approx(6.0)  # long axis length
        assert med[3] == pytest.approx(W)
