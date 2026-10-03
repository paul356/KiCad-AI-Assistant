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

from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree

from kcaa.router.pns.direction45 import ArcSeg, CornerMode, Trace, build_initial_trace
from kcaa.router.pns.node import ObstacleNode
from kcaa.router.pns.shove import ShoveFailure, ShoveResult, TrackObstacle, shove_path
from kcaa.router.pns.walkaround import WalkFailure, walkaround_line
from kcaa.router.world_model import Obstacle

MAX_WALKAROUND_ITER = 64
MAX_SHOVE_DEPTH = 4

# Extra hull margin for the walkaround/shove placement stages.  A line
# that RIDES a round hull boundary cuts inside the true clearance
# envelope by the chord sagitta of the hull's arc sampling (default
# Shapely quad_segs=8 => up to ~6 um for the hull radii used here).
# The final DRC audit measures the true edge distance, so the placement
# stages work on ``clearance + CLEARANCE_EPS`` and the audit on
# ``clearance`` itself — the epsilon absorbs the sagitta instead of
# fine-sampling every hull (which starves the walking state machine).
CLEARANCE_EPS = 1e-2  # 10 um, > worst-case chord sagitta (~6 um)


class PnsFailure(RuntimeError):
    """Engine could not produce a valid path (walkaround stuck / shove
    incomplete) — the caller surfaces this as a RouteFailure with real
    cause."""


@dataclass
class EngineResult:
    """Route polyline plus the tracks that were shoved out of the way.

    ``trace`` is the untouched skeleton (with rounded-corner arcs) when
    neither walkaround nor shove modified the route — the caller then
    emits the skeleton's anchor points and arcs directly.  When the path
    had to detour, ``trace`` is None and ``path`` carries the walked
    polyline (KiCad linearizes arcs it detours around; see plan §3.3).
    """

    path: list[tuple[float, float]]
    shoved_tracks: list[TrackObstacle] = field(default_factory=list)
    arcs: list[ArcSeg] = field(default_factory=list)
    trace: Trace | None = None
    # (original, displaced) shove pairs — the pre-shove track (as it
    # exists in the PCB file) and the pushed replacement — so the write
    # path can persist the displacement.  Empty when nothing was pushed.
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = field(default_factory=list)
    # identity (``id()``) of the obstacle entries whose tracks were
    # displaced — the final audit exempts their original (now
    # copper-free) locations.  Needed by callers that re-audit the
    # polyline after their own post-engine adjustments.
    orig_obstacle_ids: set[int] = field(default_factory=set)


def route_engine(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: Sequence[Obstacle],
    track_width: float,
    clearance: float,
    corner_mode: CornerMode | str = CornerMode.MITERED_45,
    max_shove_depth: float | None = None,
    extra_fixed: Sequence[Obstacle] = (),
    net: str | None = None,
) -> EngineResult:
    """Route ``start`` → ``end`` through the obstacle set with walkaround
    + shove, returning the final polyline and the pushed tracks.

    ``max_shove_depth=0`` runs the walkaround-only strategy (movable
    tracks are treated as fixed solids and never displaced).  ``None``
    (default) is the pre-existing behavior: shove with
    ``MAX_SHOVE_DEPTH`` as the chain cap.

    ``extra_fixed`` extends the fixed-solid set that *shoved* tracks must
    keep clear of, without making the route walk around it (used by
    multi-leg routes: the copper of earlier legs is same-net to the
    route — legal to touch — but foreign to every shoved track).

    ``net`` is the route's net, used by the final DRC audit to exempt
    same-net copper (the route's own pads / earlier legs): same-net
    copper needs no gap.  ``None`` audits conservatively (no exemption).

    Output contract: whatever leaves the engine is DRC-clean **or the
    route fails loudly** —

    * every polyline disturbed by walkaround/shove is re-snapped onto the
      0/45/90 family (KiCad's optimizer-pass analogue; the untouched
      skeleton keeps its fillet arcs), and
    * a final all-copper audit re-checks route + every displacement
      against the whole obstacle set at ``clearance``, raising
      :class:`PnsFailure` on the first violation instead of writing DRY
      errors.
    """
    trace = build_initial_trace(start, end, corner_mode)
    skeleton = trace.as_polyline(arc_pts=16)

    # Movable: simple rect tracks shovable at their endpoints' disposal.
    # Everything else (vias, pads, keepouts, arcs) is fixed.
    # Prefer the exact segment endpoints recorded by the world model —
    # reverse-deriving the centerline from the buffered rect flips the
    # axis for tracks shorter than their width (0.2 mm tap-in segment
    # inside a 0.5 mm pad entry).  Tracks with no metadata fall back to
    # the rect-geometry derivation.  Sub-width tracks are left fixed:
    # shoving a track shorter than it is wide has no well-defined
    # displacement direction, so treat it as a solid.
    movable: list[TrackObstacle] = []
    movable_shapes: list[Obstacle] = []
    for obs in obstacles:
        if obs.kind != "track":
            continue
        if obs.track_centerline is not None and obs.track_width is not None:
            centerline = list(obs.track_centerline)
            width_obs = obs.track_width
        else:
            centerline = _track_centerline(obs.shape)
            if centerline is None:
                continue
            width_obs = _track_width(obs.shape)
        seg_len = math.hypot(
            centerline[1][0] - centerline[0][0], centerline[1][1] - centerline[0][1]
        )
        if seg_len <= width_obs:
            continue  # degenerate short tap-in: fixed solid, not shovable
        track = TrackObstacle(
            points=tuple(centerline),
            width=width_obs,
            net=obs.net,
            layer=sorted(obs.layers)[0] if obs.layers else None,
        )
        movable.append(track)
        movable_shapes.append(obs)

    # Movable tracks are shove candidates only when shoving is enabled
    # (``max_shove_depth != 0``); the walkaround-only strategy treats
    # every track as a fixed solid and routes around it — DRC-clean, but
    # the track is never displaced.
    shove_enabled = max_shove_depth != 0
    walk_obstacles = (
        obstacles if not shove_enabled else [o for o in obstacles if o not in movable_shapes]
    )
    node = ObstacleNode(walk_obstacles)
    # Placement stages work on clearance + CLEARANCE_EPS so the final
    # geometry is *strictly* clear; the audit below re-checks against the
    # true clearance.
    place_clearance = clearance + CLEARANCE_EPS
    walked = _walkaround_solids(skeleton, node, track_width, place_clearance)

    if movable and shove_enabled:
        # Shoved tracks must also stay clear of every FIXED solid (pads,
        # vias, keepouts, openings, non-shovable tracks): the shove stage
        # only gauges other movable tracks, so without this a displaced
        # track can be landed on top of a pad.  ``walk_obstacles`` is
        # exactly the fixed set here; ``extra_fixed`` adds the route's
        # own earlier-leg copper (foreign to every shoved track).
        fixed = [*walk_obstacles, *extra_fixed]
        try:
            shoved: ShoveResult = shove_path(
                walked,
                movable,
                width=track_width,
                clearance=place_clearance,
                max_depth=MAX_SHOVE_DEPTH if max_shove_depth is None else max_shove_depth,
                fixed_obstacles=fixed,
            )
        except ShoveFailure as exc:
            # The caller (auto_route_pair) only knows PnsFailure; a raw
            # ShoveFailure would bubble past router and tool into
            # FastMCP's "success: true + text error" wrapper.
            raise PnsFailure(f"shove failed: {exc}") from exc
        out_path = shoved.path
        pushed = shoved.pushed
        moved_pairs = shoved.moved_pairs
    else:
        out_path = walked
        pushed = []
        moved_pairs = []

    # Originals displaced from the file (their obstacle entries are gone).
    orig_obstacle_ids: set[int] = set()
    for i, track in enumerate(movable):
        if any(track is orig for orig, _ in moved_pairs):
            orig_obstacle_ids.add(id(movable_shapes[i]))

    # ------------------------------------------------------------------
    # KiCad optimizer analogue: re-snap disturbed polylines onto the
    # 0/45/90 family.  The skeleton is born on the family (with optional
    # fillet arcs); a line disturbed by walkaround/shove rides obstacle
    # hulls and picks up arbitrary-angle chords.  Each snapped line keeps
    # the DRC margin to the world it is given (exact-margin boundary
    # riding allowed, same as the walkaround placement); a Manhattan
    # corner that would not fit falls back to the original segment, so
    # snapping never creates a violation by itself.  Skeleton-surviving
    # legs (``out_path == skeleton``) keep their arcs (see below).
    # ------------------------------------------------------------------
    if moved_pairs:
        disp_pts: list[list[tuple[float, float]]] = [
            list(disp.points) for _orig, disp in moved_pairs
        ]
        stay_movable = [t for t in movable if not any(t is orig for orig, _ in moved_pairs)]
        # Displaced tracks snap FIRST (world: fixed solids + route +
        # other movables + other displacements, already-snapped positions
        # for the ones processed earlier); the route snaps LAST.
        for i, (_orig, disp) in enumerate(moved_pairs):
            wk = disp.width
            hulls: list[Polygon] = [
                _family_hull(o.shape, place_clearance + wk / 2.0)
                for o in [*walk_obstacles, *extra_fixed]
                if o.shape is not None and not o.shape.is_empty
            ]
            hulls.append(
                LineString(out_path).buffer(
                    track_width / 2.0 + place_clearance + wk / 2.0,
                    cap_style="round",
                )
            )
            for t in stay_movable:
                hulls.append(
                    _family_hull(
                        LineString(t.points),
                        t.width / 2.0 + place_clearance + wk / 2.0,
                    )
                )
            for j, (_oj, dj) in enumerate(moved_pairs):
                if j == i:
                    continue
                hulls.append(
                    _family_hull(
                        LineString(disp_pts[j]),
                        dj.width / 2.0 + place_clearance + wk / 2.0,
                    )
                )
            disp_pts[i] = _snap45_line(disp_pts[i], hulls)
        moved_pairs = [
            (
                orig,
                TrackObstacle(
                    points=tuple(disp_pts[i]),
                    width=disp.width,
                    net=disp.net,
                    layer=disp.layer,
                ),
            )
            for i, (orig, disp) in enumerate(moved_pairs)
        ]
        pushed = [disp for _orig, disp in moved_pairs]

    if out_path != skeleton:
        route_hulls: list[Polygon] = [
            _family_hull(o.shape, place_clearance + track_width / 2.0)
            for o in obstacles
            if o.shape is not None and not o.shape.is_empty
        ]
        for _orig, disp in moved_pairs:
            route_hulls.append(
                LineString(disp.points).buffer(
                    disp.width / 2.0 + place_clearance + track_width / 2.0,
                    cap_style="round",
                )
            )
        out_path = _snap45_line(out_path, route_hulls)

    # Final all-copper DRC audit: the route and every displacement must
    # keep ``clearance`` from every foreign-net copper item of the final
    # state (fixed solids, unmoved tracks, other displacements, same-net
    # excepted).  Any violation means the engine would write a DRC error
    # — fail loudly instead.
    _audit_final_copper(
        out_path=out_path,
        width=track_width,
        net=net,
        obstacles=obstacles,
        extra_fixed=extra_fixed,
        moved_pairs=moved_pairs,
        orig_obstacle_ids=orig_obstacle_ids,
        clearance=clearance,
    )

    # Rounded skeleton arcs survive only when walkaround left the path
    # untouched (a detour linearizes the arc it goes around).
    arcs: list[ArcSeg] = []
    kept_trace: Trace | None = None
    if out_path == skeleton and not pushed:
        arcs = [a for a in trace.arcs if a is not None]
        kept_trace = trace
    return EngineResult(
        path=out_path,
        shoved_tracks=pushed,
        arcs=arcs,
        trace=kept_trace,
        moved_pairs=moved_pairs,
        orig_obstacle_ids=orig_obstacle_ids,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _snap45_line(
    pts: Sequence[tuple[float, float]],
    hulls: Sequence[Polygon],
) -> list[tuple[float, float]]:
    """Replace every non-family segment with a 45-family short-leg corner.

    The candidate is the *shortest* 45-family hook between the two
    points: one long leg on the axis (0/90) plus a 45-degree short leg.
    Because every step stays in the family and the turn angle is exactly
    45 degrees, an L-shaped (90-degree) corner can never be produced —
    slots between family directions force the intermediate 45-degree
    segment, which is precisely the miter shoulder KiCad's optimizer
    emits.  No explicit miter/radius parameter is needed: the shoulder
    length falls out of the geometry.

    The candidate is accepted only when both legs stay clear of every
    hull, and when the entry turn (previous segment -> first leg) and
    the exit turn (second leg -> following segment) are each <= 45
    degrees — direction-continuity, no re-entry angles (DRC min-angle
    class).  When no candidate fits, the original segment is kept —
    snapping must never create a DRC violation by itself (the final
    audit is the gate).  Fallback geometry that follows a hull may keep
    larger turns: that is the cost of the walkaround, not a snapping
    choice.  Corner smoothing (fillet arcs) is deliberately out of scope
    until the polyline scheme is stable.

    Clear means: no *interior* entry into a hull (``touches``-only
    boundary riding is the walkaround's exact-margin placement and is
    legal).  First/last points are pinned, so a snapped displacement
    keeps the physical track connected.
    """

    def _turn_le_45(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
        """True when the smallest angle at ``b`` from ``a`` to ``c`` is
        <= 45 degrees (zero-length legs count as no turn)."""
        v1x, v1y = b[0] - a[0], b[1] - a[1]
        v2x, v2y = c[0] - b[0], c[1] - b[1]
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        if l1 < 1e-12 or l2 < 1e-12:
            return True
        dot = v1x * v2x + v1y * v2y
        # cos(45 deg) = sqrt(0.5); tolerance absorbs float noise at the
        # boundary without admitting > 45-degree turns.
        return dot >= (math.sqrt(0.5) - 1e-9) * l1 * l2

    def _seg_clear(p1: tuple[float, float], p2: tuple[float, float]) -> bool:
        line = LineString([p1, p2])
        return not any(line.intersects(h) and not line.touches(h) for h in hulls)

    out: list[tuple[float, float]] = [pts[0]]
    for i in range(1, len(pts)):
        x1, y1 = out[-1]
        x2, y2 = pts[i]
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9:
            out.append((x2, y2))
            continue
        # Shortest 45-family hook: long axis leg then 45-degree leg.
        # Unique for a non-family segment — the mirror walk overshoots
        # the target, so no other candidate exists.
        if abs(dx) > abs(dy):
            mid = (x2 - math.copysign(abs(dy), dx), y1)
        else:
            mid = (x1, y2 - math.copysign(abs(dx), dy))
        chosen: tuple[float, float] | None = None
        if _seg_clear(out[-1], mid) and _seg_clear(mid, (x2, y2)):
            # Direction-continuity: no >45-degree turn into or out of
            # the hook (the hook's own axis->45 turn is exactly 45).
            if len(out) < 2 or _turn_le_45(out[-2], out[-1], mid):
                if i + 1 >= len(pts) or _turn_le_45(mid, (x2, y2), pts[i + 1]):
                    chosen = mid
        if chosen is not None:
            out.append(chosen)
        out.append((x2, y2))
    deduped: list[tuple[float, float]] = [out[0]]
    for p in out[1:]:
        if abs(p[0] - deduped[-1][0]) > 1e-9 or abs(p[1] - deduped[-1][1]) > 1e-9:
            deduped.append(p)
    if len(deduped) < 2:
        return [pts[0], pts[-1]]
    return _merge_family_chain(deduped)


def _merge_family_chain(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge runs of consecutive segments in the same 0/45/90 slot.

    A polyline that snaps onto the family can still carry redundant
    vertices: walkaround rides obstacle hulls densely and snap45 turns
    each chord into a family hook, so a straight run on one family
    direction ends up split into many short collinear segments.  Every
    interior vertex between two segments that share the same direction
    slot (V/V, H/H, or same-sign D/D) lies exactly on the line between
    its neighbors — dropping it changes nothing geometrically, so no
    DRC re-check is needed (the final audit already ran on this line
    and still sees the identical copper).  First/last points are pinned
    (pads / via anchors stay connected).

    This is the "merge collinear / 45-degree chain" pass of KiCad's
    optimizer that the engine previously skipped: the walked path kept
    one vertex per hull-sample chord (tens of 2-30 um segments where
    there is one straight leg).
    """
    if len(pts) < 3:
        return pts

    def _slot(p: tuple[float, float], q: tuple[float, float]) -> int | None:
        dx = q[0] - p[0]
        dy = q[1] - p[1]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return None
        if abs(dx) < 1e-9:
            return 0  # V
        if abs(dy) < 1e-9:
            return 1  # H
        if abs(abs(dx) - abs(dy)) < 1e-6:
            return 2 if dx * dy > 0 else 3  # D+ / D-
        return None  # not on the family (should not happen post-snap)

    out: list[tuple[float, float]] = [pts[0]]
    for i in range(1, len(pts)):
        prev_slot = _slot(out[-1], pts[i])
        next_slot = _slot(pts[i], pts[i + 1]) if i + 1 < len(pts) else None
        if prev_slot is not None and prev_slot == next_slot:
            continue  # same family direction on both sides: drop the vertex
        out.append(pts[i])
    if len(out) < 2:
        return [pts[0], pts[-1]]
    return out


def _clip_halfplane(
    verts: list[tuple[float, float]],
    nx: float,
    ny: float,
    s: float,
) -> list[tuple[float, float]]:
    """Sutherland–Hodgman clip of a convex CCW ring by ``n·p <= s``."""
    out: list[tuple[float, float]] = []
    n = len(verts)
    for i in range(n):
        cur = verts[i]
        nxt = verts[(i + 1) % n]
        d_cur = nx * cur[0] + ny * cur[1] - s
        d_nxt = nx * nxt[0] + ny * nxt[1] - s
        if d_cur <= 0:
            out.append(cur)
        if (d_cur > 0) != (d_nxt > 0):
            t = d_cur / (d_cur - d_nxt)
            out.append((cur[0] + t * (nxt[0] - cur[0]), cur[1] + t * (nxt[1] - cur[1])))
    return out


def _outer_family_polygon(poly: Polygon, margin: float = 0.0) -> Polygon | None:
    """45-family outer octagon covering ``poly`` (plus ``margin``).

    Take the support half-plane in each of the 8 family normals
    (0/45/90/135/… degrees) and intersect them.  The result is a convex
    polygon whose every edge is a 0/45/90-family line and that contains
    ``poly`` — a walkaround that rides this octagon produces family
    directions only.  Returns None when clipping degenerates.
    """
    if poly is None or poly.is_empty:
        return None
    big = 1e6
    clip: list[tuple[float, float]] = [
        (-big, -big),
        (big, -big),
        (big, big),
        (-big, big),
    ]
    coords = [(c[0], c[1]) for c in poly.exterior.coords]
    for k in range(8):
        ang = math.radians(k * 45.0)
        nx, ny = math.cos(ang), math.sin(ang)
        s = max(nx * x + ny * y for x, y in coords) + margin
        clip = _clip_halfplane(clip, nx, ny, s)
        if len(clip) < 3:
            return None
    if len(clip) < 3:
        return None
    return Polygon(clip)


def _family_hull(shape, margin: float) -> Polygon:
    """Walkaround/shove hull snapped onto the 0/45/90 family.

    The buffered shape (round-cap pads/vias, rounded track ends, keepout
    solids) is covered by its 45-family outer octagon, so the detector
    walks along family edges and every resulting segment is already
    0/45/90 — no per-segment re-snap needed.  A long thin hull (a track
    being routed around) inflates beyond the guard ratio, so its
    original rounded-rect unlock hull is kept instead: the octagon
    would force a needlessly wide detour, and the track centerline is
    already a single straight run the walkaround rides without
    chopping.  The final DRC audit still measures against the true
    obstacle shapes, so a slightly larger hull can only add margin.

    The guard ratio 1.35: a circle's outer octagon is ~1.055x the
    round's area, an axis-aligned square's octagon ~1.2x; anything
    above 1.35 is a long strip whose octagon detour is excessive.
    """
    hull = shape.buffer(margin, cap_style="round")
    if hull.is_empty:
        return hull
    oct_ = _outer_family_polygon(hull)
    if oct_ is None or len(oct_.exterior.coords) < 4:
        return hull
    if oct_.area <= hull.area * 1.35:
        return oct_
    return hull


def _audit_final_copper(
    out_path: Sequence[tuple[float, float]],
    width: float,
    net: str | None,
    obstacles: Sequence[Obstacle],
    extra_fixed: Sequence[Obstacle],
    moved_pairs: Sequence[tuple[TrackObstacle, TrackObstacle]],
    orig_obstacle_ids: set[int],
    clearance: float,
) -> None:
    """Final post-shove DRC audit of the engine output.

    The final copper state is: fixed solids + unmoved tracks (the
    obstacle set minus the displaced originals), the earlier-leg copper
    (``extra_fixed``), the displaced tracks in their final places, and
    the route line itself.  Every audited line (route + displacements)
    must keep ``clearance`` (edge-to-edge) from every foreign-net item;
    equal non-None nets are exempt (same-net copper needs no gap, ``None``
    nets are always audited).  Raises :class:`PnsFailure` on the first
    violation — the engine never hands back a DRC-violating polyline.
    """
    displaced_copper: list[tuple[Polygon, str | None, str]] = [
        (
            LineString(disp.points).buffer(disp.width / 2.0, cap_style="round", quad_segs=512),
            disp.net,
            f"shoved track (net {disp.net})",
        )
        for _orig, disp in moved_pairs
    ]
    world: list[tuple[Polygon, str | None, str]] = [
        (o.shape, o.net, f"{o.kind} (net {o.net})")
        for o in obstacles
        if (o.shape is not None and not o.shape.is_empty and id(o) not in orig_obstacle_ids)
    ]
    world.extend(
        (o.shape, o.net, f"{o.kind} (net {o.net})")
        for o in extra_fixed
        if o.shape is not None and not o.shape.is_empty
    )
    world.extend(displaced_copper)

    if not out_path or len(out_path) < 2:
        return
    route_copper = LineString(list(out_path)).buffer(width / 2.0, cap_style="round", quad_segs=512)
    world.append((route_copper, net, f"route line (net {net})"))

    tree = STRtree([w[0] for w in world])
    auditees: list[tuple[Polygon, str | None, str]] = [(route_copper, net, "route line")]
    auditees.extend(displaced_copper)
    for poly, n, label in auditees:
        for gi in tree.query(poly.buffer(clearance)):
            other_poly, other_net, other_desc = world[gi]
            if other_poly is poly:
                continue
            if n is not None and other_net is not None and n == other_net:
                continue  # same net: no DRC gap required
            d = float(poly.distance(other_poly))
            if d < clearance - 1e-9:
                raise PnsFailure(
                    f"final DRC audit: {label} comes within {d:.4f} mm of "
                    f"{other_desc} (needs {clearance} mm clearance)"
                )


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
        # The hull is snapped onto the 0/45/90 family (outer octagon):
        # walking family edges yields family-only segments, so no
        # arbitrary-angle chords remain on the detour.
        hull = _family_hull(obs.shape, hull_margin)
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
    None if the shape is not a simple 4-vertex rectangle (e.g. arcs) or
    the axis is ambiguous (near-square)."""
    if poly is None or poly.is_empty or len(poly.exterior.coords) != 5:
        return None
    med = _rect_medians(poly)
    if med is None:
        return None
    a, b, long_len, short_len = med
    if long_len <= 0.0 or short_len / long_len > 0.8:
        return None  # near-square: direction is not well-defined
    return [a, b]


def _track_width(poly) -> float:
    """Track width of a rect obstacle: the short axis length."""
    med = _rect_medians(poly)
    if med is None:
        return 0.0
    _, _, _, short = med
    return short
