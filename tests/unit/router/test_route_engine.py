"""Unit tests for kcaa.router.route_engine (PNS engine pipeline)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Polygon

from kcaa.router.pns.shove import ShoveFailure, TrackObstacle
from kcaa.router.route_engine import (
    PnsFailure,
    _audit_final_copper,
    _path_len,
    _rect_medians,
    _snap45_line,
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

    def test_shove_failure_surfaces_as_pns_failure(self, monkeypatch):
        """A shove-stage ShoveFailure must come out as PnsFailure — the
        auto_route_pair contract knows PnsFailure, not the raw shove
        exception (which would bubble past router and tool into
        FastMCP's success:true + text-error wrapper)."""
        t = _track_obs(5, -3, 3, "N2")

        def _boom(*_args, **_kwargs):
            raise ShoveFailure("cannot shove track (1, 2) -> (3, 4)")

        monkeypatch.setattr("kcaa.router.route_engine.shove_path", _boom)
        with pytest.raises(PnsFailure, match="shove failed"):
            route_engine((-8, 0), (8, 0), [_pad(0, 0), t], W, CLR)

    def test_shoved_track_stays_off_pad(self):
        """The route stays straight (the pad below it is outside the
        walkaround detection margin) while the movable track is pushed
        onto the pad's side of the route hull.  The shove stage must walk
        the displaced track around the pad with the DRC margin
        (regression: shoved tracks used to be placed on top of pads)."""
        pad = Obstacle(
            shape=Polygon([(-1.0, -1.6), (1.0, -1.6), (1.0, -0.2), (-1.0, -0.2)]),
            layers=frozenset({"F.Cu"}),
            net="N9",
            kind="pad",
        )
        t = _track_obs(0, -4, 4, "N2")
        res = route_engine((-8, 0), (8, 0), [pad, t], W, CLR)
        assert len(res.shoved_tracks) == 1
        pushed = res.shoved_tracks[0]
        assert pushed.start == (0, -4) and pushed.end == (0, 4)  # pinned
        d = LineString(pushed.points).distance(pad.shape)
        assert d >= CLR + W / 2 - 1e-6, f"pushed track violates pad clearance: {d:.4f}"

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


def _segments_on_45(pts) -> bool:
    """True when every segment of ``pts`` lies on the 0/45/90 family."""
    for a, b in zip(pts, pts[1:]):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx > 1e-6 and dy > 1e-6 and abs(dx - dy) > 1e-6:
            return False
    return True


def _turns_le_45(pts) -> bool:
    """True when every consecutive turn of ``pts`` is <= 45 degrees."""
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        v1x, v1y = b[0] - a[0], b[1] - a[1]
        v2x, v2y = c[0] - b[0], c[1] - b[1]
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        if l1 < 1e-12 or l2 < 1e-12:
            continue
        dot = v1x * v2x + v1y * v2y
        if dot < (math.sqrt(0.5) - 1e-6) * l1 * l2:
            return False
    return True


class TestSnap45Line:
    """Disturbed polylines are re-snapped onto the 0/45/90 family."""

    def test_keeps_0_45_90_segments_untouched(self):
        pts = [(0, 0), (2, 0), (4, 2), (4, 6)]
        assert _snap45_line(pts, []) == pts

    def test_diagonal_replaced_with_short_leg_hook(self):
        # 14-degree segment: the 45-family short-leg hook (3 mm axis
        # leg + 1 mm 45-degree leg) replaces it; the turn is 45 degrees,
        # endpoints are pinned — a KiCad-style miter shoulder, no L.
        out = _snap45_line([(0, 0), (4, 1)], [])
        assert out == [(0, 0), (3, 0), (4, 1)]
        assert out[0] == (0, 0) and out[-1] == (4, 1)
        assert _segments_on_45(out)

    def test_falls_back_to_original_when_hook_blocked(self):
        from shapely.geometry import box

        hulls = [box(3.9, -0.2, 4.3, 0.4), box(3.8, 0.9, 4.4, 1.3)]
        out = _snap45_line([(0, 0), (4, 1)], hulls)
        # The hook (axis leg under, 45 leg over) is blocked like every
        # family alternative; snapping must keep the original segment
        # (the final audit is the gate, never the snap).
        assert out == [(0, 0), (4, 1)]

    def test_snapped_result_never_enters_a_hull(self):
        from shapely.geometry import box

        h = box(-0.2, 1.4, 0.2, 2.2)
        out = _snap45_line([(0, 0), (4, 1)], [h])
        line = LineString(out)
        interior = line.intersection(h).difference(h.boundary)
        assert interior.length < 1e-6
        assert _segments_on_45(out)

    def test_rejects_hook_that_turns_back_on_itself(self):
        # The following segment runs at -120 deg from (4, 1) — the hook
        # is geometrically clear (no hulls), but its 45-degree exit leg
        # turns 165 deg into it.  The hook must be refused even though
        # snapping would be legal clearance-wise; direction continuity
        # (no re-entry angle) wins, so the original segment survives.
        pts = [(0, 0), (4, 1), (3.5, 0.134)]
        out = _snap45_line(pts, [])
        assert out == pts

    def test_hook_accepted_when_exit_turn_is_shallow(self):
        # Follow-on segment continues at 0 deg: the hook's 45-degree
        # exit leg turns exactly 45 deg into it — on the constraint
        # boundary, so the hook is taken and the polyline stays on the
        # family.
        out = _snap45_line([(0, 0), (4, 1), (6, 1)], [])
        assert out == [(0, 0), (3, 0), (4, 1), (6, 1)]
        assert _segments_on_45(out)

    def test_family_gap_gets_automatic_miter_shoulder(self):
        # 0-degree run then a shallow kink then a 90-degree run: the
        # family slots force the intermediate 45-degree legs
        # (KiCad's miter shoulder) with zero explicit miter parameter.
        pts = [(0, 0), (20, 0), (20.4, 0.4), (20.4, 10)]
        out = _snap45_line(pts, [])
        assert out[0] == (0, 0) and out[-1] == (20.4, 10)
        # The kink (20,0)->(20.4,0.4) is not family: each turn is <= 45
        # degrees and every segment stays on the family — the axis legs
        # join through a 45-degree leg, never a direct 90.
        assert _segments_on_45(out)
        assert _turns_le_45(out)
        assert (20.0, 0.0) in out[:-1]


class TestFinalAudit:
    """The engine's final DRC audit hard-gates the output."""

    def _pad(self, net: str | None = "N2") -> Obstacle:
        return Obstacle(
            shape=Polygon([(-0.4, -0.4), (0.4, -0.4), (0.4, 0.4), (-0.4, 0.4)]),
            layers=frozenset({"F.Cu"}),
            net=net,
            kind="pad",
        )

    def test_route_crossing_foreign_pad_raises(self):
        with pytest.raises(PnsFailure, match="final DRC audit"):
            _audit_final_copper([(-8, 0), (8, 0)], W, None, [self._pad("N2")], [], [], set(), CLR)

    def test_same_net_route_and_pad_not_audited(self):
        # Equal non-None nets need no gap (the route legitimately touches
        # its own pads / earlier-leg copper).
        _audit_final_copper([(-8, 0), (8, 0)], W, "VCC", [self._pad("VCC")], [], [], set(), CLR)

    def test_none_nets_are_audited(self):
        # An unknown route net is not exempted from an unknown obstacle.
        with pytest.raises(PnsFailure, match="final DRC audit"):
            _audit_final_copper([(-8, 0), (8, 0)], W, None, [self._pad(None)], [], [], set(), CLR)

    def test_displaced_track_violation_raises(self):
        # A displacement left inside the route copper fails the audit.
        orig = TrackObstacle(points=((0, -4), (0, 4)), width=W, net="N2", layer="F.Cu")
        disp = TrackObstacle(points=((0.05, -4), (0.05, 4)), width=W, net="N2", layer="F.Cu")
        with pytest.raises(PnsFailure, match="final DRC audit"):
            _audit_final_copper([(-8, 0), (8, 0)], W, None, [], [], [(orig, disp)], set(), CLR)

    def test_displaced_original_position_not_audited(self):
        # The obstacle entry of a displaced track is gone from the file;
        # the route may pass over its ORIGINAL location.  Only the final
        # displacement (clear of the route here) is audited.
        obs = Obstacle(
            shape=Polygon([(-0.1, -4.1), (0.1, -4.1), (0.1, 4.1), (-0.1, 4.1)]),
            layers=frozenset({"F.Cu"}),
            net="N2",
            kind="track",
        )
        orig = TrackObstacle(points=((0, -4), (0, 4)), width=W, net="N2", layer="F.Cu")
        # Displaced clear of the route (horizontal at y=5).
        disp = TrackObstacle(points=((-4, 5), (4, 5)), width=W, net="N2", layer="F.Cu")
        _audit_final_copper(
            [(-8, 0), (8, 0)],
            W,
            None,
            [obs],
            [],
            [(orig, disp)],
            {id(obs)},
            CLR,
        )


class TestSnap45Engine:
    """Engine-level guarantee: snap + audit keep the output clean."""

    def test_same_net_extra_fixed_is_not_audited(self):
        # Early-leg copper is same-net to the route — touching it is
        # legal, and the audit must not flag it (nor the walkaround walk
        # around it).
        pad = Obstacle(
            shape=Polygon([(-0.4, -0.4), (0.4, -0.4), (0.4, 0.4), (-0.4, 0.4)]),
            layers=frozenset({"F.Cu"}),
            net="VCC",
            kind="pad",
        )
        res = route_engine((-8, 0), (8, 0), [], W, CLR, net="VCC", extra_fixed=[pad])
        assert res.path == [(-8, 0), (8, 0)]

    def test_foreign_extra_fixed_overlap_fails_audit(self):
        # Foreign copper under the route: the engine must refuse to hand
        # back a DRC-violating path (PnsFailure, surfaced as the tool's
        # error return).
        pad = Obstacle(
            shape=Polygon([(-0.4, -0.4), (0.4, -0.4), (0.4, 0.4), (-0.4, 0.4)]),
            layers=frozenset({"F.Cu"}),
            net="N2",
            kind="pad",
        )
        with pytest.raises(PnsFailure, match="final DRC audit"):
            route_engine((-8, 0), (8, 0), [], W, CLR, net="VCC", extra_fixed=[pad])

    def test_snapped_detour_and_push_stay_clear(self):
        # A detour + displacement that used to shave ~15 um off the true
        # clearance envelope now passes the audit with the real margin.
        t = _track_obs(0.0, -4.0, 4.0, "N2")
        res = route_engine((-8, 0), (8, 0), [_pad(0, -1.5, half=0.4), t], W, CLR)
        assert len(res.shoved_tracks) == 1
        pushed = res.shoved_tracks[0]
        assert pushed.start == (0, -4) and pushed.end == (0, 4)
        d = LineString(pushed.points).distance(_pad(0, -1.5, half=0.4).shape)
        assert d >= CLR + W / 2 - 1e-6, f"pushed track too close to pad: {d:.4f}"


class TestMovableExtraction:
    def test_track_obstacle_becomes_track(self):
        t = _track_obs(5, -3, 3, "N2")
        med = _rect_medians(t.shape)
        assert med is not None
        assert med[2] == pytest.approx(6.0)  # long axis length
        assert med[3] == pytest.approx(W)
