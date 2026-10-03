"""
Shove: push movable obstacle tracks out of the way (KiCad port).

Port of ``PNS::SHOVE`` roles from pns_shove.cpp, scoped to the plan's
M2 slice: polyline tracks only, no vias, no arc shove (KiCad's arc shove
is unfinished upstream — mirrored here).  The pipeline per collision:

1. Build a ``HULL_SET``: every segment of the *current* line buffered by
   ``clearance + obstacle_width/2`` (KiCad ``SEGMENT::Hull`` chamfer
   rectangles; shapely round-cap buffer is a safe superset).
2. ``shove_line_to_hull_set`` — re-walk the obstacle line around the
   outside of the hull set by walking each hull in turn (4 attempts:
   clockwise toggles, traversal order inverts after attempt 2).
3. ``ShoveObstacleLine``-style retries: 3 attempts with increasing
   ``extraHullExpansion``; endpoints may move only on later attempts and
   only when not anchor-constrained.
4. The pushed line becomes the current line and recursion continues
   (depth cap); any failure unwinds the pushes already made.

Pure Python + shapely; KiCad used as algorithm reference only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

from shapely.geometry import LineString, Point, Polygon

from kcaa.router.pns.walkaround import WalkFailure, walkaround_line
from kcaa.router.world_model import Obstacle

# KiCad c_ENDPOINT_ON_HULL_THRESHOLD = 1000 nm (internal units are nm).
ENDPOINT_ON_HULL_THRESHOLD_MM = 1000.0 * 1e-6
# KiCad cHullFailureExpansionFactor = 1000 nm.
HULL_FAILURE_EXPANSION_STEP_MM = 1000.0 * 1e-6
MAX_SHOVE_DEPTH = 4


class ShoveFailure(RuntimeError):
    """Raised when the shove set cannot be completed (KiCad SH_INCOMPLETE)."""


@dataclass(frozen=True)
class TrackObstacle:
    """A movable track obstacle as a polyline plus width.

    ``points`` is the centerline chain; a shoved track gains intermediate
    vertices (KiCad LINE / SHAPE_LINE_CHAIN)."""

    points: tuple[tuple[float, float], ...]
    width: float
    net: str | None = None
    layer: str | None = None

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1]


@dataclass
class ShoveResult:
    """Outcome of a shove attempt."""

    path: list[tuple[float, float]]
    pushed: list[TrackObstacle] = field(default_factory=list)
    unchanged: list[TrackObstacle] = field(default_factory=list)
    # (original, displaced) pairs — the pre-shove track as it exists in
    # the PCB file and the pushed replacement.  One entry per pushed
    # physical track (chain propagation pushes distinct tracks, the
    # pushed track itself is never re-pushed).  ``pushed`` above stays
    # list-of-displaced for backward compat.
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = field(default_factory=list)


def _hull_set(
    cur_line: Sequence[tuple[float, float]],
    width: float,
    clearance: float,
    obstacle_width: float,
    extra: float = 0.0,
) -> list[Polygon]:
    """Buffered hulls of every segment of ``cur_line``.

    KiCad ``SEGMENT::Hull( clearance + extra, obstacleLineWidth )`` — a
    chamfered rectangle at ``width/2 + clearance + obstacle_width/2``
    around the segment.  Round-cap buffer is a safe superset of KiCad's
    45-degree chamfer (slightly larger, monotone in the same parameters).
    """
    half = width / 2.0 + clearance + obstacle_width / 2.0 + extra
    hulls: list[Polygon] = []
    pts = list(cur_line)
    for a, b in zip(pts, pts[1:]):
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-12:
            continue
        hulls.append(LineString([a, b]).buffer(half, cap_style="round"))
    return hulls


def _nearest_point_on_hull(hull: Polygon, pos: tuple[float, float]) -> tuple[float, float]:
    """Nearest point on the hull boundary to ``pos``."""
    boundary = hull.boundary
    probe = boundary.interpolate(boundary.project(Point(*pos)))
    return (probe.x, probe.y)


def _endpoint_nearest_hull(
    pos: tuple[float, float],
    hulls: Sequence[Polygon],
) -> tuple[float, float] | None:
    """Nearest point on any hull within the threshold (KiCad
    c_ENDPOINT_ON_HULL_THRESHOLD), else None."""
    best: tuple[float, float] | None = None
    best_d = float("inf")
    for hull in hulls:
        d = hull.distance(Point(*pos))
        if d > ENDPOINT_ON_HULL_THRESHOLD_MM or d >= best_d:
            continue
        best_d = d
        best = _nearest_point_on_hull(hull, pos)
    return best


def _shove_line_to_hull_set(
    obstacle_line: Sequence[tuple[float, float]],
    hulls: Sequence[Polygon],
    clockwise: bool,
) -> list[tuple[float, float]] | None:
    """Re-walk ``obstacle_line`` along the outside of the hull set.

    Returns the new polyline or ``None`` when no walk succeeds (any hull
    walk failing aborts the attempt).  Endpoints must be preserved —
    endpoint adjustment is handled by the caller via ``permitAdjusting*``.
    """
    path = list(obstacle_line)
    orig = list(obstacle_line)
    for hull in hulls:
        try:
            walked = walkaround_line(path, hull, cw=clockwise)
        except WalkFailure:
            return None
        path = walked
    # Endpoints must be preserved (KiCad checks CPoint(0)/CLastPoint).
    if not path or math.hypot(path[0][0] - orig[0][0], path[0][1] - orig[0][1]) > 1e-9:
        return None
    if not path or math.hypot(path[-1][0] - orig[-1][0], path[-1][1] - orig[-1][1]) > 1e-9:
        return None
    # Must not self-intersect (KiCad path.SelfIntersecting()).
    if not LineString(path).is_simple:
        return None
    return path


def _line_len(pts: Sequence[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def _shove_clear_of_fixed(
    line_pts: Sequence[tuple[float, float]],
    fixed: Sequence[Obstacle],
    width: float,
    clearance: float,
    track_net: str | None,
    max_iter: int = 16,
) -> list[tuple[float, float]] | None:
    """Walk ``line_pts`` clear of every fixed solid with DRC margin.

    The shove stage only gauges *other movable tracks* — the displaced
    polyline is never checked against pads/vias/keepouts/non-shovable
    tracks, so a pushed track can be landed on top of a pad.  This
    closes that hole: the line is re-walked (both orientations, shorter
    wins, same machinery as the route-level walkaround) around the
    buffered hull of the first fixed obstacle it crosses, iterating
    until no obstacle remains.

    Obstacle shapes already carry their own half-width where applicable
    (track/arc obstacles); the buffer adds ``clearance + width/2``, the
    exact DRC margin for the pushed track (pad/via copper is raw, so the
    same margin formula is correct for them too).

    Endpoints are pinned: the shove chain moves one file segment at a
    time, so a displaced track must keep its exact endpoints or the
    physical track it belongs to is disconnected.  A fixed hull covering
    an endpoint (or a walk that would pull it) fails the shove instead
    of corrupting connectivity.  Same-net copper (``obs.net ==
    track_net``) is skipped — that is the track's own anchors (pads,
    vias), which it must keep touching, with no DRC gap required.

    Returns the cleaned polyline, or ``None`` when no walk succeeds or
    the endpoint pin cannot be honored.
    """
    margin = clearance + width / 2.0
    hulls: list[Polygon] = []
    for obs in fixed:
        if track_net is not None and obs.net is not None and obs.net == track_net:
            continue  # own-net anchor copper: no clearance required
        shape = obs.shape
        if shape is None or shape.is_empty:
            continue
        # Default arc sampling: the walked line RIDES the hull boundary
        # and cuts inside the true clearance envelope by the chord
        # sagitta (~4 um at these margins), but the engine's placement
        # stages run on ``clearance + CLEARANCE_EPS`` which absorbs it.
        # Fine sampling (quad_segs=512) would do the same with no
        # sagitta — but it bloats the hull ring to thousands of
        # vertices, and the walkaround ring traversal (iteration budget
        # 1000) then fails whenever the walk must span a long arc.
        hulls.append(shape.buffer(margin, cap_style="round"))

    pts = list(line_pts)
    first, last = pts[0], pts[-1]

    def pinned(candidate: Sequence[tuple[float, float]]) -> bool:
        if not candidate:
            return False
        return (
            math.hypot(candidate[0][0] - first[0], candidate[0][1] - first[1]) < 1e-8
            and math.hypot(candidate[-1][0] - last[0], candidate[-1][1] - last[1]) < 1e-8
        )

    for _ in range(max_iter):
        query = LineString(pts)
        hit: Polygon | None = None
        for h in hulls:
            # Collision = the line enters the hull INTERIOR.  A line
            # riding the hull boundary (the walkaround's exact-margin
            # placement) touches it but must not re-trigger the walk —
            # intersects-without-touching is that predicate (covers
            # crossing, containment and endpoint-in-hull alike).
            if query.intersects(h) and not query.touches(h):
                hit = h
                break
        if hit is None:
            if not pinned(pts):
                return None
            # Re-pin to the exact original floats so the written joints
            # are byte-identical to the neighbouring segments.
            pts[0], pts[-1] = first, last
            if not LineString(pts).is_simple:
                return None
            return pts
        best: list[tuple[float, float]] | None = None
        for cw in (True, False):
            try:
                walked = walkaround_line(pts, hit, cw=cw)
            except WalkFailure:
                continue
            if not pinned(walked):
                continue
            if best is None or _line_len(walked) < _line_len(best):
                best = walked
        if best is None:
            return None
        pts = best
    return None


def shove_obstacle_line(
    cur_line: Sequence[tuple[float, float]],
    obstacle: TrackObstacle,
    width: float,
    clearance: float,
    permit_moving_start: bool,
    permit_moving_end: bool,
) -> TrackObstacle | None:
    """Push ``obstacle`` away from ``cur_line`` by the clearance distance.

    Mirrors ``SHOVE::ShoveObstacleLine``: 3 attempts with growing extra
    hull expansion; each attempt runs the 4-orientation hull-set walk.
    Endpoints may move only on the last attempt and only when permitted
    (KiCad: ``attempt >= 2`` && not via-anchored).  Returns the moved
    track, or ``None`` when no attempt succeeds.
    """
    line = [obstacle.start, obstacle.end]
    extra = 0.0
    for attempt in range(3):
        hulls = _hull_set(cur_line, width, clearance, obstacle.width, extra)
        if not hulls:
            return None
        # KiCad: permitAdjustingEndpoints gates the whole block, and
        # shoveLineToHullSet only pulls endpoints on attempts >= 2.
        attempt_line = list(line)
        adjust = (attempt >= 2) and (permit_moving_start or permit_moving_end)
        if adjust and len(attempt_line) >= 2:
            if permit_moving_start:
                p0 = _endpoint_nearest_hull(attempt_line[0], hulls)
                if p0 is not None:
                    attempt_line[0] = p0
            if permit_moving_end:
                p1 = _endpoint_nearest_hull(attempt_line[-1], hulls)
                if p1 is not None:
                    attempt_line[-1] = p1
        # 4 orientations: clockwise toggles each attempt, traversal
        # inverts from attempt 2 on (KiCad shoveLineToHullSet loop).
        for invert in (False, True):
            for clockwise in (True, False):
                ordered = list(reversed(hulls)) if invert else list(hulls)
                pushed_line = _shove_line_to_hull_set(attempt_line, ordered, clockwise)
                if pushed_line is None:
                    continue
                return TrackObstacle(
                    points=tuple(pushed_line),
                    width=obstacle.width,
                    net=obstacle.net,
                    layer=obstacle.layer,
                )
        extra += HULL_FAILURE_EXPANSION_STEP_MM
    return None


def shove_path(
    path: Sequence[tuple[float, float]],
    movable_tracks: Sequence[TrackObstacle],
    width: float,
    clearance: float,
    max_depth: int = MAX_SHOVE_DEPTH,
    fixed_obstacles: Sequence[Obstacle] = (),
) -> ShoveResult:
    """Shove ``path`` clear of ``movable_tracks`` via chain propagation.

    Mirrors KiCad's shove chain: the routed line collides with track A,
    A is pushed out of the way; A's new position collides with B, B is
    pushed; ... each pushed track becomes the *current line* that pushes
    the next one (pushLineStack recursion).  The caller's ``path`` does
    not move — the tracks do.  Depth cap; any failure unwinds the whole
    chain (KiCad SH_INCOMPLETE).

    Two hard invariants keep every displacement DRC-clean and
    connectivity-preserving:

    * **Pinned endpoints.**  Each movable obstacle is one file segment;
      its endpoints are the junctions/anchors of the physical track.  A
      pushed segment therefore never moves its endpoints (KiCad moves
      endpoints only in the attempt>=2 ``permitAdjustingEndpoints``
      path, which is disabled here) — moving them would leave a gap to
      the neighbouring segments of the same track.  A hull that covers
      an endpoint simply fails the shove.
    * **Fixed-solid clearance.**  Displaced polylines are re-walked clear
      of every *fixed* obstacle (pads, vias, keepouts, openings,
      non-shovable tracks) with the DRC margin — the shove walkaround
      alone only ever looks at other movable tracks, so without this a
      pushed track could be landed on a pad.  An unresolvable conflict
      raises :class:`ShoveFailure` (whole route fails loudly) instead of
      writing a DRC violation.

    ``fixed_obstacles`` carries that fixed set (pads/vias/keepouts/
    non-shovable tracks plus, for multi-leg routes, the copper of
    earlier legs — route net, hence foreign to every shoved track).
    """
    remaining = list(movable_tracks)
    moved: list[TrackObstacle] = []
    finalized: list[TrackObstacle] = []

    def colliding_with(
        line_pts: Sequence[tuple[float, float]],
        self_track: TrackObstacle | None = None,
    ) -> TrackObstacle | None:
        """First *unhandled* track whose hull collides with ``line_pts``.

        Tracks already pushed (``moved``) are excluded: a shoved track
        hugs the previous line's hull at zero distance, which is the
        walkaround's normal placement — re-detecting it would loop the
        chain forever (A pushes B, B pushes A, ...)."""
        query = LineString(list(line_pts))
        for t in remaining:
            if t is self_track:
                continue
            if any(t is m for m in moved):
                continue
            hull = LineString(t.points).buffer(
                width / 2.0 + clearance + t.width / 2.0, cap_style="round"
            )
            if not query.intersection(hull).is_empty:
                return t
        return None

    # Worklist of (current_line, self_track) starting from the route.
    chains: list[tuple[list[tuple[float, float]], TrackObstacle | None]] = [(list(path), None)]
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = []
    depth = 0
    while chains and depth < max_depth:
        cur_line, self_track = chains.pop()
        hit = colliding_with(cur_line, self_track)
        if hit is None:
            continue  # this chain resolved; nothing more to push
        # A pushed track may itself collide with others: it becomes the
        # current line for the next push (chain propagation).
        pushed = shove_obstacle_line(
            cur_line,
            hit,
            width,
            clearance,
            permit_moving_start=False,
            permit_moving_end=False,
        )
        if pushed is None:
            raise ShoveFailure(f"cannot shove track {hit.start} -> {hit.end}")
        # Every pushed track must also clear the FIXED solids (pads,
        # vias, keepouts, non-shovable tracks, earlier-leg route copper)
        # — the push walkaround only checks other movable tracks.
        if fixed_obstacles:
            clean = _shove_clear_of_fixed(
                pushed.points,
                fixed_obstacles,
                width=pushed.width,
                clearance=clearance,
                track_net=hit.net,
            )
            if clean is None:
                raise ShoveFailure(
                    f"shoved track {hit.start} -> {hit.end} cannot clear fixed "
                    "copper (pad/via/keepout); widen the gap, move the "
                    "obstacle, or use strategy='walkaround'"
                )
            pushed = TrackObstacle(
                points=tuple(clean),
                width=pushed.width,
                net=pushed.net,
                layer=pushed.layer,
            )
        remaining.remove(hit)
        remaining.append(pushed)
        moved.append(pushed)
        moved_pairs.append((hit, pushed))
        chains.append((list(pushed.points), pushed))
        depth += 1

    # Depth cap hit: drain what is left of the chain and verify each
    # entry still collides (entries can go stale — the track that
    # triggered the push may have been moved by a later step).  Any live
    # collision means the shove is incomplete: KiCad returns
    # SH_INCOMPLETE and unwinds the whole set.  Writing the
    # partially-shoved state would leave the route or a displaced track
    # touching an unhandled neighbour — fail loudly instead.
    while chains:
        cur_line, self_track = chains.pop()
        if colliding_with(cur_line, self_track) is not None:
            raise ShoveFailure(
                f"shove chain did not converge within depth {max_depth}: a "
                "collision remains between a displaced track and an "
                "unhandled neighbour; widen the gap or use "
                "strategy='walkaround'"
            )

    finalized = [t for t in remaining if t not in moved]
    result = ShoveResult(
        path=list(path),
        pushed=moved,
        unchanged=finalized,
        moved_pairs=moved_pairs,
    )
    return result
