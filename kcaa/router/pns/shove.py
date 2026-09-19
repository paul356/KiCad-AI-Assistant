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
) -> ShoveResult:
    """Shove ``path`` clear of ``movable_tracks`` via chain propagation.

    Mirrors KiCad's shove chain: the routed line collides with track A,
    A is pushed out of the way; A's new position collides with B, B is
    pushed; ... each pushed track becomes the *current line* that pushes
    the next one (pushLineStack recursion).  The caller's ``path`` does
    not move — the tracks do.  Depth cap; any failure unwinds the whole
    chain (KiCad SH_INCOMPLETE).
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
    depth = 0
    while chains and depth < max_depth:
        cur_line, self_track = chains.pop()
        hit = colliding_with(cur_line, self_track)
        if hit is None:
            continue  # this chain resolved; nothing more to push
        # A pushed track may itself collide with others: it becomes the
        # current line for the next push (chain propagation).
        permit = depth >= 1
        pushed = shove_obstacle_line(
            cur_line,
            hit,
            width,
            clearance,
            permit_moving_start=permit,
            permit_moving_end=permit,
        )
        if pushed is None:
            raise ShoveFailure(f"cannot shove track {hit.start} -> {hit.end}")
        remaining.remove(hit)
        remaining.append(pushed)
        moved.append(pushed)
        chains.append((list(pushed.points), pushed))
        depth += 1

    finalized = [t for t in remaining if t not in moved]
    result = ShoveResult(
        path=list(path),
        pushed=moved,
        unchanged=finalized,
    )
    return result
