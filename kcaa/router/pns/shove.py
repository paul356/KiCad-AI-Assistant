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
# KiCad c_HULL_FAILURE_EXPANSION_FACTOR = 1000 nm.
HULL_FAILURE_EXPANSION_STEP_MM = 1000.0 * 1e-6
# Walkaround arc samples cut inside the true hull envelope by the chord
# sagitta (~6 um at routable margins); the engine's placement stages run
# on ``clearance + CLEARANCE_EPS`` (10 um) which absorbs it.  A finished
# line deeper inside a hull than this rode through it, not round it.
HULL_CROSS_TOLERANCE_MM = 10.0 * 1e-3
MAX_SHOVE_DEPTH = 4


class ShoveFailure(RuntimeError):
    """Raised when the shove set cannot be completed (KiCad SH_INCOMPLETE).

    ``moved_pairs`` carries the displacements finished *before* the
    failure (empty when nothing had been pushed yet), ``cur_line`` the
    current line that triggered the failing collision, and ``hit`` a
    description of the track that could not be pushed — the engine and
    router use these to dump the partial shove state for inspection.
    """

    def __init__(
        self,
        message: str,
        *,
        moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] | None = None,
        cur_line: list[tuple[float, float]] | None = None,
        hit: TrackObstacle | None = None,
    ):
        super().__init__(message)
        self.moved_pairs = moved_pairs if moved_pairs is not None else []
        self.cur_line = cur_line
        self.hit = hit


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
        hulls.append(LineString([a, b]).buffer(half, cap_style="square"))
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
    keep_start: bool = True,
    keep_end: bool = True,
) -> list[tuple[float, float]] | None:
    """Re-walk ``obstacle_line`` along the outside of the hull set.

    Returns the new polyline or ``None`` when no walk succeeds (any hull
    walk failing aborts the attempt).  Endpoints are preserved only when
    the caller pins them (``keep_start``/``keep_end``); a free endpoint
    (not anchored to a pad/via) may be pulled by the walkaround — that
    is KiCad's ``permitAdjustingEndpoints`` semantics, where a LINE's
    non-anchored ends ride the hull ring on later attempts.
    """
    path = list(obstacle_line)
    orig = list(obstacle_line)
    # The hulls overlap at route corners: every segment of the caller's
    # line gets its own buffer, and adjacent buffers share the joint's
    # round cap.  A single walk of each hull in turn can land the line
    # back on the boundary of an earlier hull whose cap arcs through a
    # later hull's interior, so the finished polyline crosses the route
    # it was shoved away from.  KiCad merges the hulls into one graph and
    # walks it once; re-walking the hulls until no pass changes the line
    # is the polyline equivalent — a pass that changes nothing cannot
    # have slid the line inside any hull (a hull walk only ever moves a
    # line outward from that hull's interior).  Keep an explicit final
    # outside check as a belt: a stuck oscillation would otherwise hand
    # a route-crossing line to the DRC audit.
    max_walks = len(hulls) * 4 + 4
    for _ in range(max_walks):
        changed = False
        for hull in hulls:
            try:
                walked = walkaround_line(path, hull, cw=clockwise)
            except WalkFailure:
                return None
            if len(walked) != len(path) or any(
                math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-9 for a, b in zip(walked, path)
            ):
                changed = True
                path = walked
        if not changed:
            break
    final_line = LineString(path)
    # A line that ends up riding a hull boundary is legal: the boundary
    # sits exactly ``clearance + half-widths`` from the route centre,
    # and the walk's arc samples cut inside the true envelope only by
    # the chord sagitta (~6 um).  ``crosses/within`` reject that — the
    # finished line is a few um inside the hull while still DRC-clean
    # (the engine's place stages run on clearance + CLEARANCE_EPS).
    # Reject only deep penetration: shrink each hull by the sagitta
    # tolerance; intersecting the core means the line truly cut through
    # the hull instead of walking round it (a stuck oscillation between
    # the overlapping corner hulls).
    for hull in hulls:
        core = hull.buffer(-HULL_CROSS_TOLERANCE_MM)
        if core.is_empty:
            continue
        if final_line.intersects(core):
            return None
    if keep_start and (
        not path or math.hypot(path[0][0] - orig[0][0], path[0][1] - orig[0][1]) > 1e-9
    ):
        return None
    if keep_end and (
        not path or math.hypot(path[-1][0] - orig[-1][0], path[-1][1] - orig[-1][1]) > 1e-9
    ):
        return None
    # Must not self-intersect (KiCad path.SelfIntersecting()).
    if not LineString(path).is_simple:
        return None
    return path


def _merge_track_chain(
    hit: TrackObstacle,
    movable: Sequence[TrackObstacle],
    eps: float = 1e-6,
) -> tuple[TrackObstacle, list[TrackObstacle]]:
    """Merge ``hit`` with every connected segment of its physical track.

    A logical track is stored as consecutive file segments sharing the
    net and touching at endpoints (KiCad tracks the same way).  SHOVE
    pushes a whole LINE — moving one segment alone would tear it from
    its neighbours, so the colliding segment is expanded into the full
    chain it belongs to and that line is shoved as one obstacle.

    Returns ``(merged_line, original_segments)``; a lone segment (no
    same-net endpoint-touching neighbour) is returned unchanged.
    """
    group: list[TrackObstacle] = []
    seen: set[int] = set()
    frontier: list[TrackObstacle] = [hit]
    while frontier:
        cur = frontier.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        group.append(cur)
        for t in movable:
            if id(t) in seen or t.net != cur.net:
                continue
            if any(
                math.hypot(a[0] - b[0], a[1] - b[1]) <= eps for a in cur.points for b in t.points
            ):
                frontier.append(t)
    if len(group) == 1:
        return hit, group
    pts = _order_track_chain(group, eps)
    merged = TrackObstacle(
        points=tuple(pts),
        width=hit.width,
        net=hit.net,
        layer=hit.layer,
    )
    return merged, group


def _order_track_chain(
    group: list[TrackObstacle],
    eps: float = 1e-6,
) -> list[tuple[float, float]]:
    """Order a connected segment group into one polyline chain.

    Builds endpoint adjacency and walks from a free end through each
    segment exactly once (a physical track is a linear chain).  A branch
    (more than one continuation at a joint, e.g. a T-junction) stops the
    walk conservatively — the shoved LINE must stay linear.
    """
    n = len(group)
    if n == 1:
        return list(group[0].points)
    # adj[i] = [(j, shared_point), ...]
    adj: list[list[tuple[int, tuple[float, float]]]] = [[] for _ in range(n)]

    def _shared(i: int, j: int) -> list[tuple[float, float]]:
        out = []
        for a in group[i].points:
            for b in group[j].points:
                if math.hypot(a[0] - b[0], a[1] - b[1]) <= eps:
                    out.append(a)
        return out

    for i in range(n):
        for j in range(i + 1, n):
            if group[i].net != group[j].net:
                continue
            shared = _shared(i, j)
            if shared:
                adj[i].append((j, shared[0]))
                adj[j].append((i, shared[0]))

    def used_point(i: int) -> list[tuple[float, float]]:
        return [s for _, s in adj[i]]

    # Start at a segment with a free endpoint (chain head), else any.
    start = 0
    for i in range(n):
        used = used_point(i)
        if any(
            not any(math.hypot(p[0] - s[0], p[1] - s[1]) <= eps for s in used)
            for p in group[i].points
        ):
            start = i
            break

    pts: list[tuple[float, float]] = []
    visited: set[int] = set()
    cur: int | None = start
    enter: tuple[float, float] | None = None  # point used to enter cur
    while cur is not None and cur not in visited:
        visited.add(cur)
        endpoints = list(group[cur].points)
        if enter is not None:
            other = next(
                (p for p in endpoints if math.hypot(p[0] - enter[0], p[1] - enter[1]) > eps),
                endpoints[-1],
            )
            pts.append(other)
        else:
            # Chain head (or any start segment): orient it so the LAST
            # emitted point is a joint with the next segment.  Extending
            # both endpoints blindly can leave a free end trailing, and
            # then ``nxt`` (matched against the trailing point) finds
            # nothing — the walk dies with a two-point stub.  Emit the
            # free end first, the shared joint last.
            joints = [s for _, s in adj[cur]]
            if joints and any(
                math.hypot(endpoints[-1][0] - s[0], endpoints[-1][1] - s[1]) <= eps for s in joints
            ):
                pts.extend(endpoints)  # last point is already a joint
            elif joints:
                # reverse: free end first, shared joint last
                pts.extend([endpoints[-1], endpoints[0]])
            else:
                pts.extend(endpoints)  # truly isolated segment
        last = pts[-1]
        # Next: an unvisited neighbour sharing our last point.
        nxt: tuple[int, tuple[float, float]] | None = None
        for j, s in adj[cur]:
            if j in visited:
                continue
            if math.hypot(s[0] - last[0], s[1] - last[1]) <= eps:
                if nxt is not None:
                    return pts  # branch: keep the walked part only
                nxt = (j, last)
        if nxt is None:
            break
        cur, enter = nxt
    return pts


def _endpoint_anchored(
    pt: tuple[float, float],
    fixed: Sequence[Obstacle],
    eps: float = 1e-6,
) -> bool:
    """True when ``pt`` sits inside a fixed pad/via.

    A pad/via anchored endpoint is the physical anchor of the LINE —
    it cannot move (KiCad via-anchored rule); every other point of the
    track is free to be pulled by the shove walkaround.
    """
    for o in fixed:
        if o.kind not in ("pad", "via"):
            continue
        if o.shape is None or o.shape.is_empty:
            continue
        if o.shape.distance(Point(*pt)) <= eps:
            return True
    return False


def _line_len(pts: Sequence[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def _shove_clear_of_fixed(
    line_pts: Sequence[tuple[float, float]],
    fixed: Sequence[Obstacle],
    width: float,
    clearance: float,
    track_net: str | None,
    max_iter: int = 16,
    keep_start: bool = True,
    keep_end: bool = True,
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

    Endpoints that are anchored (``keep_start``/``keep_end``) are
    pinned: they are the physical anchors of the LINE (pad/via copper)
    and the displaced track must keep them or it snaps off its pads.  A
    fixed hull covering a pinned endpoint (or a walk that would pull it)
    fails the shove instead of corrupting connectivity.  Free endpoints
    may be moved by the walkaround — KiCad's permitAdjustingEndpoints
    semantics, and the reason an anchor-point check lives here and not
    on every chain vertex.  Same-net copper (``obs.net == track_net``)
    is skipped — that is the track's own anchors (pads, vias), which it
    must keep touching, with no DRC gap required.

    Returns the cleaned polyline, or ``None`` when no walk succeeds or a
    pinned endpoint cannot be honored.
    """
    margin = clearance + width / 2.0
    hulls: list[Polygon] = []
    pts = list(line_pts)
    first_pt = pts[0] if pts else None
    last_pt = pts[-1] if pts else None
    for obs in fixed:
        if track_net is not None and obs.net is not None and obs.net == track_net:
            continue  # own-net anchor copper: no clearance required
        shape = obs.shape
        if shape is None or shape.is_empty:
            continue
        # A pinned LINE endpoint sits ON its anchor pad/via — that
        # copper is the track's own connection target, never an
        # obstacle to clear.  The net test alone is not enough: a THT
        # pad's solder-side copy can carry ``net=None`` in the file
        # (same physical pad, both faces), so skip any fixed solid
        # covering a kept endpoint by geometry *when it is anonymous
        # net* — the only case where the net test can miss it.  A
        # foreign-net pad that happens to cover the endpoint stays an
        # obstacle (tests pin this: a fixed pad swallowing a pushed
        # track's endpoints must fail the shove).
        if (
            keep_start
            and obs.net is None
            and first_pt is not None
            and shape.distance(Point(*first_pt)) <= 1e-8
        ):
            continue
        if (
            keep_end
            and obs.net is None
            and last_pt is not None
            and shape.distance(Point(*last_pt)) <= 1e-8
        ):
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

    first, last = pts[0], pts[-1]

    def pinned(candidate: Sequence[tuple[float, float]]) -> bool:
        if not candidate:
            return False
        ok = True
        if keep_start and (
            math.hypot(candidate[0][0] - first[0], candidate[0][1] - first[1]) >= 1e-8
        ):
            ok = False
        if keep_end and (
            math.hypot(candidate[-1][0] - last[0], candidate[-1][1] - last[1]) >= 1e-8
        ):
            ok = False
        return ok

    for _ in range(max_iter):
        query = LineString(pts)
        hit: Polygon | None = None
        for h in hulls:
            # Collision = the line penetrates the hull core.  The hull
            # boundary sits exactly ``clearance + half-widths`` from the
            # route centre, and a walked line RIDES that boundary —
            # legal placement — while its arc samples cut inside the
            # true envelope only by the chord sagitta (~6 um).  Using
            # plain ``intersects`` (or ``intersects and not touches``)
            # would flag the riding line as colliding (float sagitta
            # reads as penetration), trigger a re-walk that can never
            # converge and fail a legal shove.  Shrink the hull by the
            # sagitta tolerance and test the core — the same predicate
            # as the shove walkaround (``_shove_line_to_hull_set``);
            # only deep penetration — the line really cutting through a
            # fixed solid instead of walking round it — rejects.
            core = h.buffer(-HULL_CROSS_TOLERANCE_MM)
            if core.is_empty:
                continue
            if query.intersects(core):
                hit = h
                break
        if hit is None:
            if not pinned(pts):
                return None
            # Re-pin to the exact original floats so the written joints
            # are byte-identical to the neighbouring segments.
            if keep_start:
                pts[0] = first
            if keep_end:
                pts[-1] = last
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
    line = list(obstacle.points)
    extra = 0.0
    for attempt in range(3):
        hulls = _hull_set(cur_line, width, clearance, obstacle.width, extra)
        if not hulls:
            return None
        # KiCad: permitAdjustingEndpoints gates the whole block, and
        # shoveLineToHullSet only pulls endpoints on attempts >= 2.
        # Before attempt 2 even a free endpoint must hold (the LINE is
        # moved as a whole on the first tries, endpoints stay).
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
        # KiCad tries every orientation and keeps the *shortest* valid
        # result (clockwise toggles per attempt, traversal inverted from
        # attempt 2 on).  Returning the first success is not enough: on a
        # real board one walk direction can detour around a huge pad
        # while the opposite direction slips the LINE the short way past
        # the colliding segment.  Collect all valid orientations and take
        # the minimum-length polyline — same semantic as KiCad's
        # shoveLineToHullSet loop.
        best: TrackObstacle | None = None
        best_len = float("inf")
        for invert in (False, True):
            for clockwise in (True, False):
                ordered = list(reversed(hulls)) if invert else list(hulls)
                pushed_line = _shove_line_to_hull_set(
                    attempt_line,
                    ordered,
                    clockwise,
                    # Anchored endpoints never move; free endpoints may
                    # ride the hull ring once endpoint adjustment is
                    # unlocked (attempt >= 2).
                    keep_start=True if attempt < 2 else not permit_moving_start,
                    keep_end=True if attempt < 2 else not permit_moving_end,
                )
                if pushed_line is None:
                    continue
                length = _line_len(pushed_line)
                if length < best_len:
                    best_len = length
                    best = TrackObstacle(
                        points=tuple(pushed_line),
                        width=obstacle.width,
                        net=obstacle.net,
                        layer=obstacle.layer,
                    )
        if best is not None:
            return best
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

    A pushed obstacle is the *whole physical track*: consecutive file
    segments sharing the net and touching at endpoints are merged into
    one LINE (KiCad tracks a track as a LINE of segments and SHOVE moves
    the LINE).  Only pad/via-anchored endpoints of that LINE are pinned
    (``_endpoint_anchored``); every other vertex — the mid-chain joints
    and any free end — may be pulled by the walkaround, so segment
    lengths adjust naturally (no artificial per-segment endpoint locks).

    Two hard invariants keep every displacement DRC-clean and
    connectivity-preserving:

    * **Anchor pinning.**  A LINE endpoint sitting on pad/via copper is
      the physical anchor of the track; it never moves (the attempt>=2
      ``permitAdjustingEndpoints`` path applies only to free ends).
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
        # Merge the colliding segment with its physical-track neighbours:
        # SHOVE displaces the whole LINE, one segment alone would tear
        # the track.  Only the LINE ends are anchor-checked; the merged
        # chain's interior joints are free to move with the walk.
        line_chain, chain_segments = _merge_track_chain(hit, remaining)
        if fixed_obstacles:
            keep_start = _endpoint_anchored(line_chain.start, fixed_obstacles)
            keep_end = _endpoint_anchored(line_chain.end, fixed_obstacles)
        else:
            keep_start = keep_end = False
        # A pushed track may itself collide with others: it becomes the
        # current line for the next push (chain propagation).
        pushed = shove_obstacle_line(
            cur_line,
            line_chain,
            width,
            clearance,
            permit_moving_start=not keep_start,
            permit_moving_end=not keep_end,
        )
        if pushed is None:
            raise ShoveFailure(
                f"cannot shove track {line_chain.start} -> {line_chain.end}",
                moved_pairs=list(moved_pairs),
                cur_line=list(cur_line),
                hit=line_chain,
            )
        # Every pushed track must also clear the FIXED solids (pads,
        # vias, keepouts, non-shovable tracks, earlier-leg route copper)
        # — the push walkaround only checks other movable tracks.
        if fixed_obstacles:
            clean = _shove_clear_of_fixed(
                pushed.points,
                fixed_obstacles,
                width=pushed.width,
                clearance=clearance,
                track_net=line_chain.net,
                keep_start=keep_start,
                keep_end=keep_end,
            )
            if clean is None:
                raise ShoveFailure(
                    f"shoved track {line_chain.start} -> {line_chain.end} cannot clear "
                    "fixed copper (pad/via/keepout); widen the gap or move the "
                    "obstacle",
                    moved_pairs=list(moved_pairs),
                    cur_line=list(cur_line),
                    hit=line_chain,
                )
            pushed = TrackObstacle(
                points=tuple(clean),
                width=pushed.width,
                net=pushed.net,
                layer=pushed.layer,
            )
        for seg in chain_segments:
            remaining.remove(seg)
        remaining.append(pushed)
        moved.append(pushed)
        # moved_pairs records the whole LINE (original file chain) and
        # its displacement — the write path matches per-segment.
        moved_pairs.append((line_chain, pushed))
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
                "unhandled neighbour; widen the gap",
                moved_pairs=list(moved_pairs),
                cur_line=list(cur_line),
            )

    finalized = [t for t in remaining if t not in moved]
    result = ShoveResult(
        path=list(path),
        pushed=moved,
        unchanged=finalized,
        moved_pairs=moved_pairs,
    )
    return result
