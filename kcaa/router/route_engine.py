"""
PNS routing engine (replaces the grid A* path search).

Pipeline (plan §3.3): skeleton trace from ``build_initial_trace`` → walk
around fixed solids (per hull, CW/CCW, pick shorter, iterate until
collision-free) → shove movable tracks (chain propagation, depth cap) →
cleanup.  Pure Python + shapely.

``Obstacle.shape`` is already inflated by half the *obstacle's* own
width (``_segment_obstacle`` / ``_arc_obstacle``); the route's
half-width plus clearance is applied here when building walkaround hulls
(KiCad ``SEGMENT::Hull( clearance, trackWidth )`` semantics).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

from kcaa.router.pns.direction45 import CornerMode, build_initial_trace
from kcaa.router.pns.node import ObstacleNode
from kcaa.router.pns.shove import ShoveResult, TrackObstacle, shove_path
from kcaa.router.pns.walkaround import WalkFailure, walkaround_line
from kcaa.router.world_model import Obstacle

MAX_WALKAROUND_ITER = 64
MAX_SHOVE_DEPTH = 4


class PnsFailure(RuntimeError):
    """Engine could not produce a valid path (walkaround stuck / shove
    incomplete) — the caller surfaces this as a RouteFailure with real
    cause."""


@dataclass
class EngineResult:
    """Route polyline plus the tracks that were shoved out of the way."""

    path: list[tuple[float, float]]
    shoved_tracks: list[TrackObstacle] = field(default_factory=list)


def route_engine(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: Sequence[Obstacle],
    track_width: float,
    clearance: float,
    corner_mode: CornerMode | str = CornerMode.MITERED_45,
) -> EngineResult:
    """Route ``start`` → ``end`` through the obstacle set with walkaround
    + shove, returning the final polyline and the pushed tracks."""
    skeleton = build_initial_trace(start, end, corner_mode).as_polyline(arc_pts=16)

    # Movable: simple rect tracks shovable at their endpoints' disposal.
    # Everything else (vias, pads, keepouts, arcs) is fixed.
    movable: list[TrackObstacle] = []
    movable_shapes: list[Obstacle] = []
    for obs in obstacles:
        if obs.kind != "track":
            continue
        centerline = _track_centerline(obs.shape)
        if centerline is None:
            continue
        track = TrackObstacle(
            points=tuple(centerline),
            width=_track_width(obs.shape),
            net=obs.net,
            layer=sorted(obs.layers)[0] if obs.layers else None,
        )
        movable.append(track)
        movable_shapes.append(obs)

    fixed = [o for o in obstacles if o not in movable_shapes]
    node = ObstacleNode(fixed)
    walked = _walkaround_solids(skeleton, node, track_width, clearance)

    if movable:
        shoved: ShoveResult = shove_path(
            walked,
            movable,
            width=track_width,
            clearance=clearance,
            max_depth=MAX_SHOVE_DEPTH,
        )
        out_path = shoved.path
        pushed = shoved.pushed
    else:
        out_path = walked
        pushed = []
    return EngineResult(path=out_path, shoved_tracks=pushed)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _walkaround_solids(
    path: list[tuple[float, float]],
    node: ObstacleNode,
    track_width: float,
    clearance: float,
    max_iter: int = MAX_WALKAROUND_ITER,
) -> list[tuple[float, float]]:
    """Bump the path around every fixed solid until collision-free.

    Each iteration: nearest obstacle within demargin, walk its hull both
    CW and CCW, keep the shorter result; repeat.  Mirrors KiCad's
    WALKAROUND::Route single-step loop.
    """
    pts = list(path)
    hull_margin = clearance + track_width / 2.0
    check_margin = clearance
    for _ in range(max_iter):
        hit = node.nearest(pts, dfence=check_margin)
        if hit is None:
            return pts
        obs = hit.obstacle
        # Obstacle shape already carries its own half-width; add the
        # route half-width + clearance so the walked line gets DRC margin.
        hull = obs.shape.buffer(hull_margin, cap_style="round")
        if hull.is_empty:
            raise PnsFailure(f"obstacle {obs.kind} has an empty hull")
        best: list[tuple[float, float]] | None = None
        for cw in (True, False):
            try:
                walked = walkaround_line(pts, hull, cw=cw)
            except WalkFailure:
                continue
            if best is None or _path_len(walked) < _path_len(best):
                best = walked
        if best is None:
            raise PnsFailure(f"cannot walk around {obs.kind} obstacle {obs.ref}")
        pts = best
    raise PnsFailure(f"walkaround did not converge in {max_iter} iterations")


def _path_len(pts: Sequence[tuple[float, float]]) -> float:
    return sum((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 for a, b in zip(pts, pts[1:])) ** 0.5


def _rect_medians(
    poly,
) -> tuple[tuple[float, float], tuple[float, float], float, float] | None:
    """Long and short axis of a 4-vertex rect.

    Returns ``((a_long, b_long), long_len, short_len)`` — the endpoints
    of the long median (which is the track centerline) and both axis
    lengths.  Works for axis-aligned and oriented rectangles regardless
    of vertex winding."""
    coords = list(poly.exterior.coords)[:-1]
    if len(coords) != 4:
        return None
    medians: list[tuple[float, float, float, float, float]] = []
    for i in range(2):
        a1, b1 = coords[i], coords[(i + 1) % 4]
        a2, b2 = coords[(i + 2) % 4], coords[(i + 3) % 4]
        m1 = ((a1[0] + b1[0]) / 2.0, (a1[1] + b1[1]) / 2.0)
        m2 = ((a2[0] + b2[0]) / 2.0, (a2[1] + b2[1]) / 2.0)
        d = math.hypot(m2[0] - m1[0], m2[1] - m1[1])
        medians.append((m1[0], m1[1], m2[0], m2[1], d))
    medians.sort(key=lambda m: m[4], reverse=True)
    m_long = medians[0]
    m_short = medians[1]
    return (
        (m_long[0], m_long[1]),
        (m_long[2], m_long[3]),
        m_long[4],
        m_short[4],
    )


def _track_centerline(poly) -> list[tuple[float, float]] | None:
    """Centerline of a track-obstacle rect (long axis endpoints), or
    None if the shape is not a simple 4-vertex rectangle (e.g. arcs)."""
    if poly is None or poly.is_empty or len(poly.exterior.coords) != 5:
        return None
    med = _rect_medians(poly)
    if med is None:
        return None
    a, b, _, _ = med
    return [a, b]


def _track_width(poly) -> float:
    """Track width of a rect obstacle: the short axis length."""
    med = _rect_medians(poly)
    if med is None:
        return 0.0
    _, _, _, short = med
    return short
