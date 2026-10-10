"""Unit tests for kcaa.router.route_engine (PNS engine pipeline)."""

from __future__ import annotations

import math

import pytest
import shapely.affinity
from shapely.geometry import LineString, Polygon

from kcaa.router.pns.shove import ShoveFailure, TrackObstacle
from kcaa.router.route_engine import (
    PnsFailure,
    _audit_final_copper,
    _coalesce_stub_pairs,
    _family_hull,
    _path_len,
    _rect_medians,
    _snap45_line,
    _track_centerline,
    _visibility_detour,
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

    def test_bend_corner_within_copper_margin_triggers_detour(self):
        """A 45-degree bend whose CENTERLINE stays clear of the pad can
        still violate clearance: the round join sweeps the route copper
        width/2 past the corner point.  The trigger must measure the
        buffered copper (same geometry as the final DRC audit), not the
        raw centerline.

        Geometry: path (-10,0) -> (0,0) -> (0.5,-0.5) bends at the
        origin; a 0.4x0.4 pad sits with its top-left corner at (0.1,0.1),
        i.e. sqrt(0.02) ~ 0.1414 from the bend point.  That is inside
        (CLR, CLR + W/2) = (0.1, 0.2): a centerline-only probe at CLR
        would miss it, yet the copper edge (0.1414 - W/2 = 0.0414) is
        well under CLR.
        """
        from kcaa.router.pns.node import ObstacleNode

        pad = _pad(0.3, 0.3, half=0.2)
        node = ObstacleNode([pad])
        # Sanity: centerline distance from the bend to the pad lies in
        # the probe band (CLR, CLR + W/2) -- the regression window.
        centerline_d = LineString([(-10, 0), (0, 0), (0.5, -0.5)]).distance(pad.shape)
        assert CLR < centerline_d < CLR + W / 2
        copper_d = (
            LineString([(-10, 0), (0, 0), (0.5, -0.5)])
            .buffer(W / 2, cap_style="round")
            .distance(pad.shape)
        )
        assert copper_d < CLR  # the copper really violates clearance

        out = _walkaround_solids([(-10, 0), (0, 0), (0.5, -0.5)], node, W, CLR)
        assert out[0] == (-10, 0) and out[-1] == (0.5, -0.5)
        out_copper = LineString(out).buffer(W / 2, cap_style="round")
        assert out_copper.distance(pad.shape) >= CLR - 1e-9
        # The detour must be real -- the skeleton bend was inside the
        # violation band, so returning it unchanged would fail the audit.
        assert len(out) > 3 or out != [(-10, 0), (0, 0), (0.5, -0.5)]

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

    def test_bend_corner_clearance_audit_passes(self):
        """Engine-level regression: a route whose bend corner is clear of
        a pad by centerline yet within the copper violation band must
        detour and pass the final DRC audit instead of raising
        PnsFailure -- the walkaround trigger used to measure only the
        centerline at ``clearance``, letting the round join cut into the
        clearance envelope."""
        pad = _pad(0.3, 0.3, half=0.2)
        skeleton = [(-10, 0), (0, 0), (0.5, -0.5)]
        centerline_d = LineString(skeleton).distance(pad.shape)
        assert CLR < centerline_d < CLR + W / 2  # the regression window
        res = route_engine((-10, 0), (0.5, -0.5), [pad], W, CLR)
        assert res.path[0] == (-10, 0) and res.path[-1] == (0.5, -0.5)
        copper = LineString(res.path).buffer(W / 2, cap_style="round")
        assert copper.distance(pad.shape) >= CLR - 1e-9
        assert res.path != skeleton  # the skeleton bend was not clean

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

    def test_board_outline_confines_detour_to_board(self):
        """Engine-level regression for the F.Cu board-outline fix: with a
        board polygon whose edge blocks the direct line (detour would
        leave the outline), the engine must NOT emit an out-of-board
        route — it must fail with PnsFailure instead of a path whose
        copper exits the Edge.Cuts boundary."""
        # Board 20x10 (0..20, 0..10); the direct line y=5 crosses a pad
        # wall; detouring over the top would leave y>10.
        board = Polygon([(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)])
        wall = Obstacle(
            shape=Polygon([(9.0, 0.0), (11.0, 0.0), (11.0, 10.0), (9.0, 10.0)]),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        with pytest.raises(PnsFailure, match="walkaround"):
            route_engine((2, 5), (18, 5), [wall], W, CLR, board_outline=board)

    def test_board_outline_allows_path_inside(self):
        """A clear lane below a wall stays inside the outline and the
        engine returns a path that never leaves the board."""
        board = Polygon([(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)])
        wall = Obstacle(
            shape=Polygon([(9.0, 3.0), (11.0, 3.0), (11.0, 10.0), (9.0, 10.0)]),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        res = route_engine((2, 5), (18, 5), [wall], W, CLR, board_outline=board)
        # Route stays entirely inside the board.
        shrink = board.buffer(-(W / 2.0 + 1e-9))
        assert shrink.covers(LineString(res.path))
        assert res.path[0] == (2, 5) and res.path[-1] == (18, 5)

    def test_lane_output_is_always_family(self):
        """Regression: the parallel-lane exploration used to emit stubs
        along the DIRECT line's normal — an arbitrary angle when the
        endpoints are off-axis (matrix board U11/p1 -> J1/2 on B.Cu:
        the lane emitted 7.56/97.54-degree segments).  The lane must
        decompose the end-to-end displacement onto the 0/45/90 family
        axes instead, so every non-skeleton route stays on the family.
        """
        import math

        # Off-axis direct line (21.8 deg against horizontal) with a pad
        # column off to the side: the lane is the chosen exploration.
        pad = Obstacle(
            shape=Polygon([(5.0, -2.0), (7.0, -2.0), (7.0, 4.0), (5.0, 4.0)]),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        res = route_engine((-8, 0), (12, 8), [pad], W, CLR)
        assert res.path[0] == (-8, 0) and res.path[-1] == (12, 8)
        for (x1, y1), (x2, y2) in zip(res.path, res.path[1:]):
            ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
            # distance to the nearest family direction (0/45/90/135 mod 180)
            rem = ang % 45.0
            d = min(rem, 45.0 - rem)
            assert d < 1e-6, f"segment ({x1},{y1})->({x2},{y2}) at {ang:.2f} deg"

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


def _turns_in_family(pts) -> bool:
    """True when every consecutive turn of ``pts`` is exactly 45 or 90
    degrees (a legal PCB corner; no sub-degree kinks allowed)."""
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        d1 = math.atan2(b[1] - a[1], b[0] - a[0])
        d2 = math.atan2(c[1] - b[1], c[0] - b[0])
        turn = abs(math.degrees((d2 - d1) % 180))
        turn = min(turn, 180 - turn)
        if turn < 1e-6:
            continue
        if not (abs(turn - 45) < 1e-4 or abs(turn - 90) < 1e-4):
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


class TestCoalesceStubPairs:
    """Walkaround stub pairs (two ~0.04 mm zigzag segments) are absorbed
    into the long family legs flanking them."""

    def test_repro_stub_pair_absorbed(self):
        # Exact repro: the walked path passed the hull corner within a
        # hair, so two stub segments (135-deg sliver + 90-deg return)
        # appeared between the vertical and diagonal legs.
        pts = [
            (116.78, 80.49),  # long vertical leg
            (116.78, 79.8083),  # a — hull corner
            (116.8093, 79.7790),  # b — 135-deg sliver
            (116.8093, 79.7376),  # c — 90-deg return
            (117.3976, 79.1493),  # long diagonal leg
        ]
        out = _coalesce_stub_pairs(pts)
        assert len(out) == 3
        # No segment is shorter than the stub threshold (all absorbed).
        for i in range(len(out) - 1):
            d = math.hypot(out[i + 1][0] - out[i][0], out[i + 1][1] - out[i][1])
            assert d >= 0.12 - 1e-9
        assert _segments_on_45(out)

    def test_unflanked_stub_pair_kept(self):
        # No long leg before the pair: coalescing must leave the path
        # alone (a stub at the very start cannot be absorbed backwards).
        pts = [
            (0.0, 0.0),
            (0.0293, -0.0293),
            (0.0293, -0.0707),
            (1.0, 1.0),
        ]
        assert _coalesce_stub_pairs(pts) == pts

    def test_parallel_legs_pair_kept(self):
        # The two flanking legs are parallel (a real lateral step, not a
        # hull corner graze): no single intersection exists, keep pair.
        pts = [
            (0.0, 0.0),
            (0.0, 0.5),
            (0.0293, 0.5),
            (0.0293, 0.5414),
            (0.0293, 1.0),
        ]
        out = _coalesce_stub_pairs(pts)
        assert out == pts

    def test_degenerate_short_pair_kept(self):
        # Two stubs at the very end of the path with no follower.
        pts = [(0.0, 0.0), (10.0, 0.0), (10.03, 0.03), (10.03, 0.07)]
        assert _coalesce_stub_pairs(pts) == pts


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


class TestFamilyHull:
    """Walkaround hulls ride the 0/45/90 family (outer octagon)."""

    def test_round_pad_hull_is_octagon_on_family(self):
        from shapely.geometry import Point

        pad = Obstacle(
            shape=Point(0, 0).buffer(1.0),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        hull = _family_hull(pad.shape, 0.1)
        assert hull.area > pad.shape.area  # outer, not inner
        coords = list(hull.exterior.coords)[:-1]
        assert len(coords) == 8
        for a, b in zip(coords, coords[1:] + coords[:1]):
            dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
            if dx > 1e-6 and dy > 1e-6:
                assert abs(dx - dy) < 1e-6, "octagon edge not on 45-degree family"
        # Hull keeps DRC margin: far enough from the pad center at least.
        assert hull.distance(Point(0, 0)) == pytest.approx(0, abs=1e-6)

    def test_narrow_strip_keeps_original_hull(self):
        # A long thin obstacle at an off-family angle (a 30-degree track
        # being routed around) must not balloon into a wide octagon — the
        # area-ratio guard keeps the buffered rounded-rect hull.
        wall = Polygon([(-0.3, -5), (0.3, -5), (0.3, 5), (-0.3, 5)])
        wall = shapely.affinity.rotate(wall, 30, origin=(0, 0))
        hull = _family_hull(wall, 0.1)
        direct = wall.buffer(0.1, cap_style="round")
        # The octagon would widen the strip crosswise (~2x the buffered
        # width); the guard returns the original rounded hull instead.
        assert hull.area <= direct.area * 1.05


class TestDetourSegmentReduction:
    """Walkaround around a round obstacle must not splinter into tens of
    micro-segments — the family hull keeps the detour on 0/45/90 with
    few segments (the observed 26-segment bug)."""

    def _route_around_pad(self):
        from shapely.geometry import Point

        pad = Obstacle(
            shape=Point(0, 0).buffer(1.0),
            layers=frozenset({"F.Cu"}),
            net=None,
            kind="pad",
        )
        return route_engine((-8, 0), (8, 0), [pad], W, CLR)

    def test_detour_all_segments_on_45_family(self):
        res = self._route_around_pad()
        out = res.path
        assert out[0] == (-8, 0) and out[-1] == (8, 0)
        # Engine-level: snap45 + family hull keep every segment on the
        # 0/45/90 family, and every turn is a legal 45 or 90 (a raw
        # round-hull detour used to carry sub-degree chords).
        assert _segments_on_45(out)
        assert _turns_in_family(out)

    def test_detour_has_few_segments(self):
        res = self._route_around_pad()
        out = res.path
        n = len(
            [1 for a, b in zip(out, out[1:]) if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9]
        )
        # Family octagon detour: 2 straight legs + 3 octagon edges + 2
        # exit legs max.  The pre-fix bug emitted 60+ micro-segments
        # (walkaround chord sampling + no family merge).
        assert n <= 8, f"detour splintered into {n} segments"


class TestWalkaroundEndsAtTarget:
    """The walkaround traversal must arrive at the path end point (a
    visited-loop break that returns a prefix was a silent bug)."""

    def test_loop_prefix_fails_loudly(self):
        from kcaa.router.pns.node import ObstacleNode

        # Canyon: central wall + caps above/below.  The family hull's
        # straight edges let the graph walk enter a visited loop instead
        # of raising on a stuck vertex; the engine must surface that as a
        # PnsFailure rather than hand back a truncated path.
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


class TestPnsFailureState:
    """PnsFailure carries the failure现场: last-path polyline, last-hit
    description, and the shove displacements completed before failure."""

    def test_walkaround_failure_carries_last_path(self):
        from kcaa.router.pns.node import ObstacleNode

        # Canyon: walkaround cannot converge; the failure must keep the
        # oscillation's last line (non-empty) for the viz dump.
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
        with pytest.raises(PnsFailure) as excinfo:
            _walkaround_solids([(-8, 0), (8, 0)], node, W, CLR)
        exc = excinfo.value
        assert exc.last_path is not None and len(exc.last_path) >= 2
        assert exc.last_path[0] == (-8, 0) and exc.last_path[-1] == (8, 0)
        assert exc.last_hit is not None  # obstacle that blocked the last step

    def test_walkaround_raises_without_state_ok(self):
        # Legacy raise sites (plain PnsFailure(msg)) must still work and
        # default the state fields to None / empty.
        with pytest.raises(PnsFailure) as excinfo:
            raise PnsFailure("legacy")
        exc = excinfo.value
        assert exc.last_path is None
        assert exc.last_hit is None
        assert exc.shoved_pairs == []


class TestShoveFailureState:
    """ShoveFailure carries moved-pairs / current-line / hit for the
    partial-shove dump."""

    def test_cannot_shove_carries_state(self, monkeypatch):
        t = _track_obs(5, -3, 3, "N2")

        def _boom(*_args, **_kwargs):
            raise ShoveFailure(
                "cannot shove track (1, 2) -> (3, 4)",
                moved_pairs=[(t, t)],
                cur_line=[(0, 0), (1, 0)],
                hit=t,
            )

        monkeypatch.setattr("kcaa.router.route_engine.shove_path", _boom)
        with pytest.raises(PnsFailure) as excinfo:
            route_engine((-8, 0), (8, 0), [_pad(0, 0), t], W, CLR)
        exc = excinfo.value
        assert exc.shoved_pairs == [(t, t)]  # partial displacements survive
        assert "shove failed" in str(exc)


class TestFallbackShoveFirst:
    """Optimization 1: walkaround failure falls back to shove-first —
    the straight line pushes movable tracks instead of dying, and the
    shove's partial state is surfaced on failure."""

    def test_fallback_runs_shove_on_walkaround_failure(self, monkeypatch):
        # Canyon walls block walkaround; a movable vertical track sits in
        # the straight line's path.  The engine must attempt shove of the
        # track (which itself fails cleanly because the track cannot
        # clear the canyon) and surface BOTH failure causes with the
        # shove state attached — never a bare "walkaround did not
        # converge".
        # The visibility detour (Optimization 0) resolves this canyon
        # outright, so it is isolated here: this test pins the shove-
        # first fallback contract alone.
        monkeypatch.setattr("kcaa.router.route_engine._visibility_detour", lambda *_a, **_k: None)
        t = _track_obs(0.0, -2.0, 2.0, "N2")
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
        calls: list[list] = []

        # Capture the walkaround failure line first — the fallback must
        # seed shove with THAT line, not the plain skeleton (an
        # identical retry is the dead-code regression this pins).
        with pytest.raises(PnsFailure) as walk_probe:
            route_engine((-8, 0), (8, 0), walls, W, CLR)
        failed_line = list(walk_probe.value.last_path)
        assert failed_line != [(-8, 0), (8, 0)]  # genuinely a detour state

        def _recording_shove(path, movable, **_kw):
            calls.append(["shove", list(path)])
            raise ShoveFailure("cannot shove track X", moved_pairs=[(t, t)])

        monkeypatch.setattr("kcaa.router.route_engine.shove_path", _recording_shove)
        with pytest.raises(PnsFailure) as excinfo:
            route_engine((-8, 0), (8, 0), [*walls, t], W, CLR)
        exc = excinfo.value
        assert len(calls) == 1  # shove ran exactly once in the fallback
        assert calls[0][1] == failed_line  # seeded from failure state
        # Both cause messages are present.
        assert "walkaround failed" in str(exc)
        assert "shove-first also failed" in str(exc)
        assert exc.shoved_pairs == [(t, t)]

    def test_shove_succeeds_but_retry_walkaround_fails(self, monkeypatch):
        # Shove completes (displacing one track) yet the fixed canyon
        # still blocks the retry walkaround: the merged failure must
        # carry the displacements that DID happen — the dump shows
        # partial progress instead of an empty board.
        # The visibility detour would resolve the canyon first, so it is
        # isolated: this pins the shove-first retry contract.
        monkeypatch.setattr("kcaa.router.route_engine._visibility_detour", lambda *_a, **_k: None)
        from kcaa.router.pns.shove import ShoveResult

        t = _track_obs(0.0, -2.0, 2.0, "N2")
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

        def _successful_shove(path, movable, **_kw):
            return ShoveResult(
                path=list(path),
                pushed=[t],
                moved_pairs=[(t, t)],
            )

        monkeypatch.setattr("kcaa.router.route_engine.shove_path", _successful_shove)
        with pytest.raises(PnsFailure) as excinfo:
            route_engine((-8, 0), (8, 0), [*walls, t], W, CLR)
        exc = excinfo.value
        assert "shove-first pushed" in str(exc)
        assert exc.shoved_pairs == [(t, t)]

    def test_no_fallback_without_movable_tracks(self, monkeypatch):
        # Pure fixed lockup, no movable: walkaround failure surfaces
        # directly, shove is never invoked.
        # The visibility detour would resolve the canyon first, so it is
        # isolated: this pins the no-movable failure contract.
        monkeypatch.setattr("kcaa.router.route_engine._visibility_detour", lambda *_a, **_k: None)
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
        calls: list[list] = []

        def _recording_shove(*_args, **_kw):
            calls.append(["shove"])
            raise AssertionError("shove must not run without movable tracks")

        monkeypatch.setattr("kcaa.router.route_engine.shove_path", _recording_shove)
        with pytest.raises(PnsFailure) as excinfo:
            route_engine((-8, 0), (8, 0), walls, W, CLR)
        exc = excinfo.value
        assert calls == []
        assert "walkaround" in str(exc)


class TestVisibilityDetour:
    """Optimization 0: visibility-graph multi-bend detour — the engine's
    third exploration, used when both lane and single-obstacle
    walkaround cannot see the free corridor around a whole cluster.
    """

    def test_detour_around_pad_row(self):
        from kcaa.router.pns.node import ObstacleNode

        # A vertical wall of pads blocks the direct line; the detour
        # must bend around the whole row (any number of bends) and keep
        # clearance from every pad.
        pads = [_pad(0, y, half=0.3) for y in (-2, 0, 2)]
        node = ObstacleNode(pads)
        out = _visibility_detour((-8, 0), (8, 0), node, W, CLR)
        assert out is not None
        assert out[0] == (-8, 0) and out[-1] == (8, 0)
        assert len(out) > 2  # a real detour, not the straight line
        for pad in pads:
            assert LineString(out).distance(pad.shape) >= CLR - 1e-6

    def test_detour_none_for_clear_sightline(self):
        from kcaa.router.pns.node import ObstacleNode

        # The detour's None contract: when the graph yields only the
        # direct start→end edge (a clear sightline), the detour
        # function refuses it — a detour must actually detour, and the
        # engine keeps the plain skeleton for clear lines.
        empty_node = ObstacleNode([])
        assert _visibility_detour((-8, 0), (8, 0), empty_node, W, CLR) is None

    def test_engine_resolves_canyon_via_detour(self):
        # The canyon that used to be an unwalkable lockup (pinned by
        # TestWalkaroundSolids::test_unwalkable_raises at the single-
        # obstacle level) is now routed successfully by the engine,
        # because the visibility detour sees the way around the whole
        # wall cluster.  Endpoints kept, clearance kept.
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
        # First pin the old failure: the canyon really does block the
        # walkaround on its own (isolated detour), so this geometry is
        # the regression window.
        from kcaa.router.pns.node import ObstacleNode

        with pytest.raises(PnsFailure):
            _walkaround_solids([(-8, 0), (8, 0)], ObstacleNode(walls), W, CLR)
        res = route_engine((-8, 0), (8, 0), walls, W, CLR)
        assert res.path[0] == (-8, 0) and res.path[-1] == (8, 0)
        for wall in walls:
            assert LineString(res.path).distance(wall.shape) >= CLR - 1e-6
