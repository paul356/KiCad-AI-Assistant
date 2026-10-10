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

from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

from kcaa.router.pns.direction45 import ArcSeg, CornerMode, Trace, build_initial_trace
from kcaa.router.pns.node import ObstacleNode
from kcaa.router.pns.shove import ShoveFailure, ShoveResult, TrackObstacle, shove_path
from kcaa.router.pns.walkaround import WalkFailure, walkaround_line
from kcaa.router.visibility_graph import build_visibility_graph
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

# Walkaround trigger distance.  The audit measures the route COPPER
# (centerline buffered by half the track width) against obstacle edges,
# so the walkaround must start detouring when the copper -- not the raw
# centerline -- comes within ``clearance``.  A bend's round join sweeps
# the copper ``width/2`` past the centerline, so a corner whose
# centerline sits in ``(clearance, clearance + width/2)`` of an obstacle
# is a real DRC violation even though a centerline-only probe never
# sees it.  ``_walkaround_solids`` therefore probes at
# ``clearance + width/2`` and verifies the closest hit against the
# buffered line; the walkaround hull additionally carries
# CLEARANCE_EPS, so bounded lines keep strictly more than ``clearance``
# and never re-trigger.


class PnsFailure(RuntimeError):
    """Engine could not produce a valid path (walkaround stuck / shove
    incomplete) — the caller surfaces this as a RouteFailure with real
    cause.

    ``last_path`` optionally carries the polyline the walkaround was
    working on when it failed (non-empty for oscillation / stuck
    failures), ``last_hit`` a human description of the obstacle that
    triggered the last walkaround, and ``shoved_pairs`` the shove
    displacements successfully completed *before* the failure — the
    caller (router) dumps these so the failure state is inspectable in
    the viz pipeline.

    ``frames`` optionally carries the *process* leading to the failure:
    one entry per walkaround iteration / shove hit / promote round, each
    ``{"stage": str, "path": [...], "hit": str|None, "note": str}``.
    Router dumps each frame as its own viz stage (``fail-pns-000``,
    ``fail-pns-001``, ...) so the failure is rendered as a sequence,
    not a single snapshot.
    """

    def __init__(
        self,
        message: str,
        *,
        last_path: list[tuple[float, float]] | None = None,
        last_hit: str | None = None,
        shoved_pairs: list[tuple[TrackObstacle, TrackObstacle]] | None = None,
        frames: list[dict] | None = None,
    ):
        super().__init__(message)
        self.last_path = last_path
        self.last_hit = last_hit
        self.shoved_pairs = shoved_pairs if shoved_pairs is not None else []
        self.frames = frames if frames is not None else []


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


def _line_in_board(pts: Sequence[tuple[float, float]], board_limit: Polygon) -> bool:
    """True iff the whole polyline (centerline) lies inside ``board_limit``."""
    if not pts:
        return True
    return board_limit.covers(LineString(pts))


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
    board_outline: Polygon | None = None,
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

    ``board_outline`` is the Edge.Cuts outer boundary polygon.  When
    given, every exploration (lane / walkaround / detour) is confined to
    it: a candidate polyline that leaves the outline is rejected instead
    of being emitted, and the final audit re-checks the route against it
    so a detour can never escape the board.

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

    # Edge.Cuts constraint: the route COPPER must stay inside the outer
    # outline, so the centerline is confined to the outline shrunk by
    # half the track width (mirrors ``_check_segments_in_board``).  A
    # degenerate outline (board narrower than the track) removes the
    # constraint — the tool-layer audit reports it.
    if board_outline is not None and not board_outline.is_empty:
        board_limit = board_outline.buffer(-(track_width / 2.0))
        if board_limit is None or board_limit.is_empty:
            board_limit = None
    else:
        board_limit = None

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

    shove_enabled = max_shove_depth != 0
    # Placement stages work on clearance + CLEARANCE_EPS so the final
    # geometry is *strictly* clear; the audit below re-checks against the
    # true clearance.  The walkaround applies the epsilon to its hull
    # margin itself and triggers on the copper-true clearance (see
    # ``_walkaround_solids``); the shove stage below keeps using
    # ``place_clearance``.
    place_clearance = clearance + CLEARANCE_EPS
    # Tracks that the shove cannot move are promoted to fixed solids and
    # the pass is retried — KiCad's semantics: shove what can be shoved,
    # walk around the rest.  Every promotion pins at least one track, so
    # the loop terminates (a track whose endpoint sits on its pad, e.g.
    # the first p7→J1/12 segment at U11/p7, has no legal displacement).
    promoted: set[int] = set()
    out_path: list[tuple[float, float]] | None = None
    pushed: list[TrackObstacle] = []
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = []
    movable_active: list[TrackObstacle] = []
    # Failure-process trace: one frame per walkaround iteration / shove
    # hit / promote round so the failure dumps (and renders) as a
    # sequence, with the hit track's fixed endpoints marked.
    frames: list[dict] = []
    while True:
        movable_active = [t for i, t in enumerate(movable) if i not in promoted]
        movable_shapes_active = [s for i, s in enumerate(movable_shapes) if i not in promoted]
        # Movable tracks are shove candidates only when shoving is
        # enabled (``max_shove_depth != 0``); the walkaround-only
        # strategy treats every track as a fixed solid and routes around
        # it — DRC-clean, but the track is never displaced.
        walk_obstacles = (
            obstacles
            if not shove_enabled
            else [o for o in obstacles if o not in movable_shapes_active]
        )
        if promoted:
            frames.append(
                {
                    "stage": f"promote-round-{len(frames):02d}",
                    "path": list(out_path if out_path is not None else skeleton),
                    "hit": f"pinned {len(promoted)} movable track segment(s) as fixed",
                    "note": f"promoted segments: {sorted(promoted)}",
                    # The walk set this round: movable tracks (not yet
                    # promoted) are excluded, everything else is solid.
                    "obstacles": list(walk_obstacles),
                }
            )
        node = ObstacleNode(walk_obstacles)
        walked: list[tuple[float, float]] | None = None
        walk_err: PnsFailure | None = None
        # Lane-first: when the direct line itself is genuinely blocked
        # (its copper would violate clearance — the same test walkaround
        # uses before detouring), prefer the shortest fully-clear
        # straight lane offset from it over per-obstacle walkaround,
        # which snakes through the row.  A boundary-clean direct line
        # (copper exactly at clearance) is not blocked — it stays the
        # clean direct line, so lane search must not replace it with an
        # offset stub pair.
        nearest_hit = node.nearest([start, end], dfence=clearance + track_width / 2.0)
        direct_blocked = nearest_hit is not None and (
            LineString([start, end])
            .buffer(track_width / 2.0, cap_style="round")
            .distance(nearest_hit.obstacle.shape)
            < clearance - 1e-9
        )
        lane = (
            _try_parallel_lane(start, end, node, track_width, clearance) if direct_blocked else None
        )
        if lane is not None and board_limit is not None and not _line_in_board(lane, board_limit):
            lane = None  # a lane leaving the board outline is not a route
        if lane is not None:
            walked = lane
        else:
            try:
                walked = _walkaround_solids(skeleton, node, track_width, clearance, frames=frames)
                if (
                    walked is not None
                    and board_limit is not None
                    and not _line_in_board(walked, board_limit)
                ):
                    # The walked line escapes the board outline — not a
                    # viable route; let the shove-first fallback see the
                    # failure state and fail loudly instead of emitting
                    # copper that the file audit would reject.
                    walk_err = PnsFailure(
                        "walkaround left the Edge.Cuts outline",
                        last_path=list(walked),
                        frames=frames,
                    )
                    walked = None
            except PnsFailure as exc:
                walk_err = exc
                walked = None
            # Optimization 0 — visibility-graph detour.  Lane probes a
            # straight parallel offset and walkaround hugs single
            # obstacle hulls; when the blockage is a *cluster* (the J1
            # THT pad column fused with a bundle of parallel tracks)
            # both single-context searches fail — the free lane runs
            # around the whole cluster, never beside its surface.  The
            # visibility graph over every obstacle (movable tracks
            # included — a genuine last resort before shove) finds the
            # global family-only detour; the shove stage below then has
            # nothing left to push, exactly like the proven A* result.
            if walked is None:
                walked = _visibility_detour(
                    start,
                    end,
                    ObstacleNode(list(obstacles)),
                    track_width,
                    clearance,
                    frames=frames,
                    board_limit=board_limit,
                )

        # Optimization 1 — shove-first fallback.  KiCad's SHOVE
        # semantics: when walkaround cannot find a detour, do not give
        # up — let the line run straight and *push* the movable tracks
        # that block it (chain propagation, each pushed track kept clear
        # of fixed solids).  After the push, the current line may still
        # cross fixed copper, so walkaround is retried.  Honest limits:
        #   * the retry starts from the failure state
        #     (``walk_err.last_path`` — the oscillation's last line, not
        #     the plain skeleton), so it is a genuinely different
        #     initial condition, and
        #   * a pure fixed-solid lockup cannot be opened by shove —
        #     fixed solids are never displaced — but the attempt is made
        #     explicitly and BOTH stages' intermediate state is carried
        #     to the caller for the viz dump.  A space competition in
        #     which movable tracks are part of the blockage is resolved
        #     here.
        shove_done = False
        if walked is None and movable_active and shove_enabled:
            shove_done = True
            seed = list(walk_err.last_path) if walk_err.last_path else list(skeleton)
            try:
                first: ShoveResult = shove_path(
                    seed,
                    movable_active,
                    width=track_width,
                    clearance=place_clearance,
                    max_depth=MAX_SHOVE_DEPTH if max_shove_depth is None else max_shove_depth,
                    fixed_obstacles=[*walk_obstacles, *extra_fixed],
                )
                # shove_path never moves the caller's path: the current
                # line is still the seed polyline.  It may now cross
                # fixed solids; clear those — the retry starts from the
                # *failed* line (not the skeleton), so when movable
                # tracks were part of the lockup, the pushed state can
                # converge where the first pass did not.
                walked = _walkaround_solids(seed, node, track_width, clearance, frames=frames)
                if (
                    walked is not None
                    and board_limit is not None
                    and not _line_in_board(walked, board_limit)
                ):
                    raise PnsFailure(
                        "walkaround left the Edge.Cuts outline after shove",
                        last_path=list(walked),
                        frames=frames,
                    )
                out_path = walked
                pushed = first.pushed
                moved_pairs = first.moved_pairs
            except ShoveFailure as exc:
                # A track the shove cannot move (pad-pinned endpoint,
                # fixed-solid block) is promoted to fixed and the pass
                # retries around it; without a hit there is nothing to
                # promote, so surface the merged failure.
                if exc.hit is not None:
                    hit_desc = (
                        f"track {exc.hit.start} -> {exc.hit.end}"
                        if hasattr(exc.hit, "start") and hasattr(exc.hit, "end")
                        else f"track obstacle ({exc.hit.net or ''})"
                    )
                    frames.append(
                        {
                            "stage": f"shove-hit-{len(frames):02d}",
                            "path": list(exc.cur_line or seed),
                            "hit": hit_desc,
                            "note": f"shove could not move track: {exc}",
                            "pinned": _pinned_endpoints(exc.hit, walk_obstacles),
                            "obstacles": list(walk_obstacles),
                        }
                    )
                if exc.hit is not None and _promote_track_group(exc.hit, movable, promoted):
                    continue
                raise PnsFailure(
                    f"walkaround failed ({walk_err}); shove-first also failed: {exc}",
                    last_path=walk_err.last_path,
                    last_hit=walk_err.last_hit,
                    shoved_pairs=exc.moved_pairs,
                    frames=frames,
                ) from walk_err
            except PnsFailure as exc:
                raise PnsFailure(
                    f"walkaround failed ({walk_err}); shove-first pushed "
                    f"{len(first.moved_pairs)} track(s) but the route still "
                    f"cannot clear fixed solids: {exc}",
                    last_path=walk_err.last_path,
                    last_hit=walk_err.last_hit,
                    shoved_pairs=list(first.moved_pairs),
                    frames=frames,
                ) from exc
        elif walked is None:
            # No movable tracks (or shove disabled): propagate the
            # original walkaround failure with its state attached.
            raise walk_err from None

        if movable_active and shove_enabled and not shove_done:
            # Shoved tracks must also stay clear of every FIXED solid
            # (pads, vias, keepouts, openings, non-shovable tracks): the
            # shove stage only gauges other movable tracks, so without
            # this a displaced track can be landed on top of a pad.
            # ``walk_obstacles`` is exactly the fixed set here;
            # ``extra_fixed`` adds the route's own earlier-leg copper
            # (foreign to every shoved track).
            fixed = [*walk_obstacles, *extra_fixed]
            try:
                shoved: ShoveResult = shove_path(
                    walked,
                    movable_active,
                    width=track_width,
                    clearance=place_clearance,
                    max_depth=MAX_SHOVE_DEPTH if max_shove_depth is None else max_shove_depth,
                    fixed_obstacles=fixed,
                )
            except ShoveFailure as exc:
                # Same promotion path as the fallback: a pad-pinned
                # track (or one that cannot clear fixed copper) becomes
                # a fixed solid and the pass retries around it.
                if exc.hit is not None:
                    hit_desc = (
                        f"track {exc.hit.start} -> {exc.hit.end}"
                        if hasattr(exc.hit, "start") and hasattr(exc.hit, "end")
                        else f"track obstacle ({exc.hit.net or ''})"
                    )
                    frames.append(
                        {
                            "stage": f"shove-hit-{len(frames):02d}",
                            "path": list(exc.cur_line or walked),
                            "hit": hit_desc,
                            "note": f"main shove could not move track: {exc}",
                            "pinned": _pinned_endpoints(exc.hit, walk_obstacles),
                            "obstacles": list(walk_obstacles),
                        }
                    )
                if exc.hit is not None and _promote_track_group(exc.hit, movable, promoted):
                    continue
                # The caller (auto_route_pair) only knows PnsFailure; a
                # raw ShoveFailure would bubble past router and tool
                # into FastMCP's "success: true + text error" wrapper.
                # Carry the partial shove state so the failure dump
                # shows what had already been displaced.
                raise PnsFailure(
                    f"shove failed: {exc}",
                    last_path=list(walked),
                    last_hit=exc.hit.net if exc.hit is not None and exc.hit.net else None,
                    shoved_pairs=exc.moved_pairs,
                    frames=frames,
                ) from exc
            out_path = shoved.path
            pushed = shoved.pushed
            moved_pairs = shoved.moved_pairs
        else:
            out_path = walked
            pushed = []
            moved_pairs = []
        break

    # Originals displaced from the file (their obstacle entries are gone).
    # Geometric match against the shoved LINEs, not identity: the merged
    # chain object differs from the movable segments it was built from.
    def _chain_contains(chain: TrackObstacle, seg: TrackObstacle) -> bool:
        """True when ``seg`` is one segment (consecutive point pair) of
        ``chain`` — the write path matches file segments against the
        shoved LINE geometrically, since the merged chain object is not
        identity-equal to the movable segments it was built from."""
        if chain.net is not None and seg.net is not None and chain.net != seg.net:
            return False
        pts = chain.points
        for a, b in zip(pts, pts[1:]):
            if (
                abs(a[0] - seg.start[0]) <= 1e-6
                and abs(a[1] - seg.start[1]) <= 1e-6
                and abs(b[0] - seg.end[0]) <= 1e-6
                and abs(b[1] - seg.end[1]) <= 1e-6
            ) or (
                abs(a[0] - seg.end[0]) <= 1e-6
                and abs(a[1] - seg.end[1]) <= 1e-6
                and abs(b[0] - seg.start[0]) <= 1e-6
                and abs(b[1] - seg.start[1]) <= 1e-6
            ):
                return True
        return False

    orig_obstacle_ids: set[int] = set()
    for i, track in enumerate(movable_active):
        if any(_chain_contains(orig, track) for orig, _ in moved_pairs):
            orig_obstacle_ids.add(id(movable_shapes_active[i]))

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
        stay_movable = [
            t for t in movable if not any(_chain_contains(orig, t) for orig, _ in moved_pairs)
        ]
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

    # Coalesce the walkaround's stub pairs.  When a polyline passes
    # within a hair of a family hull corner, the graph traversal emits a
    # pair of ~0.04 mm segments (e.g. a 45° hull-edge sliver plus the
    # axial return onto the original line) whose combined direction is
    # halfway between two family slots.  merge_family_chain can't touch
    # them (not collinear) and snap45 can't realign the pair into one
    # family run — they would survive to the output as a visible zigzag.
    # Absorb each pair into the long family legs flanking it; the merged
    # point lies on both flanking lines so the restart stays on the
    # family.  The final copper audit below is the DRC gate — a merged
    # candidate that violates net clearance keeps the original path.
    if out_path != skeleton:
        merged = _coalesce_stub_pairs(out_path)
        merged = _merge_family_chain(merged)
        if merged != out_path:
            try:
                _audit_final_copper(
                    out_path=merged,
                    width=track_width,
                    net=net,
                    obstacles=obstacles,
                    extra_fixed=extra_fixed,
                    moved_pairs=moved_pairs,
                    orig_obstacle_ids=orig_obstacle_ids,
                    clearance=clearance,
                )
                out_path = merged
            except PnsFailure:
                pass  # merged line would violate DRC: keep walked path

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

    # Edge.Cuts containment: a route that escaped the outline would be
    # caught by the tool-layer audit after the fact; fail here with the
    # proper "no path" semantics so the caller never sees a route that
    # exits the board.
    if board_limit is not None and not _line_in_board(out_path, board_limit):
        raise PnsFailure(
            f"route left the Edge.Cuts outline (centerline not fully inside "
            f"board shrunk by {track_width / 2.0:.4f} mm)",
            last_path=list(out_path),
            frames=frames,
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


def _coalesce_stub_pairs(
    pts: Sequence[tuple[float, float]],
    max_stub: float = 0.12,
) -> list[tuple[float, float]]:
    """Absorb a walkaround stub pair into the long legs flanking it.

    When a polyline passes within a hair of a family hull corner, the
    graph traversal emits a pair of very short segments — one 45-degree
    hull-edge sliver plus the axial/diagonal return onto the original
    line (e.g. ``116.78,79.8083 -> 116.8093,79.779 -> 116.8093,79.7376``,
    both ~0.041 mm).  Their combined direction is halfway between two
    family slots, so ``_merge_family_chain`` keeps them (not collinear)
    and ``_snap45_line`` cannot realign the pair into one family run —
    they would survive to the output as a visible zigzag.

    The pair (a -> b -> c) is absorbed into its neighbors: extend the
    long leg *before* ``a`` and the long leg *after* ``c`` until the
    two extension lines meet at ``p``; replacing [a, b, c] with [p]
    merges both micro-segments into one diagonal whose endpoints lie on
    the two flanking family lines (no off-family direction introduced).
    When the pair is not flanked by two long family legs the pair is
    kept — coalescing must never rewrite a legitimate short feature.

    The candidate is later DRC-audited; if the merged line violates net
    clearance the caller keeps the original path, so this pass is
    strictly an improvement (fewer segments) or a no-op.
    """
    if len(pts) < 4:
        return list(pts)

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
        return None

    out = list(pts)
    i = 1
    while i < len(out) - 2:
        a = out[i - 1]
        b = out[i]
        c = out[i + 1]
        d_ab = math.hypot(b[0] - a[0], b[1] - a[1])
        d_bc = math.hypot(c[0] - b[0], c[1] - b[1])
        if d_ab >= max_stub or d_bc >= max_stub:
            i += 1
            continue
        if i < 2 or i + 2 >= len(out):
            i += 1
            continue  # needs a sane long leg before a and after c
        prev = out[i - 2]
        nxt = out[i + 2]
        leg1 = (a[0] - prev[0], a[1] - prev[1])
        leg2 = (nxt[0] - c[0], nxt[1] - c[1])
        if _slot(prev, a) is None or _slot(c, nxt) is None:
            i += 1
            continue  # flanking legs must stay on the family
        # Extend leg1 (prev -> a) beyond a and leg2 (c -> nxt) beyond c;
        # their intersection becomes the merged point p.
        det = leg1[0] * leg2[1] - leg1[1] * leg2[0]
        if abs(det) < 1e-12:
            i += 1
            continue  # parallel legs: the pair is a real step, keep it
        # Solve prev + t1*leg1 == c + t2*leg2  (t1 counts from prev,
        # extended past a when t1 > 1; t2 counts from c, extended past c
        # when t2 < 1 keeps p -> nxt pointing along leg2).
        ox, oy = c[0] - prev[0], c[1] - prev[1]
        t1 = (ox * leg2[1] - oy * leg2[0]) / det
        t2 = (ox * leg1[1] - oy * leg1[0]) / det
        if t1 < 1.0 - 1e-9 or t2 > 1.0 + 1e-9:
            i += 1
            continue  # intersection is not ahead of a on leg1 / before nxt on leg2
        if t1 * math.hypot(*leg1) < 1e-9:
            i += 1
            continue
        p = (prev[0] + t1 * leg1[0], prev[1] + t1 * leg1[1])
        # Keep a little distance sanity: the merge point must not fly
        # off catastrophically (a degenerate hull sliver would).
        if math.hypot(p[0] - a[0], p[1] - a[1]) > 4 * max_stub:
            i += 1
            continue
        out[i - 1 : i + 2] = [p]
        i = max(1, i - 1)  # re-check the new vertex against its neighbors
    return out


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


def _pinned_endpoints(
    hit: TrackObstacle,
    fixed: Sequence[Obstacle],
) -> list[dict]:
    """Which endpoints of ``hit`` lie inside fixed copper (pads/vias).

    A shove failure on a segment whose endpoint sits on a pad is a
    *pinned-endpoint* failure: the segment cannot translate without
    snapping that endpoint off its pad (KiCad's via-anchored rule).  The
    returned list (one entry per pinned endpoint: ``{"x", "y", "pad"}``)
    is dumped into the failure frames so the renderer can mark the
    locked points on the figure.
    """
    eps = 1e-6
    pinned: list[dict] = []
    pts: list[tuple[float, float]] = []
    if hasattr(hit, "start") and hasattr(hit, "end"):
        pts = [hit.start, hit.end]
    elif hasattr(hit, "points") and hit.points:
        pts = list(hit.points)
    for pt in pts:
        for o in fixed:
            if o.shape is None or o.shape.is_empty:
                continue
            if o.kind not in ("pad", "via"):
                continue
            if o.shape.distance(Point(*pt)) <= eps:
                pinned.append({"x": pt[0], "y": pt[1], "pad": o.ref or o.net or ""})
    return pinned


def _promote_track_group(
    hit: TrackObstacle,
    movable: Sequence[TrackObstacle],
    promoted: set[int],
) -> bool:
    """Promote ``hit`` and every connected segment of its physical track
    to the fixed set.

    A single logical track is stored as consecutive file segments; its
    segments share the net and touch at endpoints.  Shoving one segment
    of such a chain while leaving its neighbours in place would tear the
    track apart, so the whole chain is promoted together.  Returns True
    when at least one track was newly pinned (the caller retries the
    walkaround+shove pass), False when there is nothing left to promote.
    """
    eps = 1e-6
    if not hasattr(hit, "points") or not hasattr(hit, "net"):
        return False  # not a shovable track; nothing to pin
    group: list[int] = []
    frontier: list[TrackObstacle] = [hit]
    seen: set[TrackObstacle] = set()
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for i, t in enumerate(movable):
            if i in promoted or t in seen:
                continue
            if t.net != cur.net:
                continue
            # Touching endpoints (either direction) mark the same
            # physical track; also accept a midpoint-on-segment touch so
            # a route that lands exactly on another segment's interior
            # pins the whole line.
            touches = any(
                math.hypot(a[0] - b[0], a[1] - b[1]) <= eps for a in cur.points for b in t.points
            )
            if touches:
                group.append(i)
                frontier.append(t)
    # The hit itself is part of the chain and must be fixed too —
    # otherwise it stays movable, the walkaround keeps treating it as
    # pushable (and does not detour around it), and the same shove hit
    # repeats forever with nothing left to promote.
    for i, t in enumerate(movable):
        if i not in promoted and t is hit:
            group.append(i)
            break
    newly = [i for i in group if i not in promoted]
    promoted.update(newly)
    return bool(newly)


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


def _try_parallel_lane(
    start: tuple[float, float],
    end: tuple[float, float],
    node: ObstacleNode,
    track_width: float,
    clearance: float,
    max_offset: float = 4.0,
    step: float = 0.4,
) -> list[tuple[float, float]] | None:
    """Try a DRC-clean straight lane offset from the direct line.

    When the direct line brushes a row of solids (a THT pad column, a
    keepout strip), per-obstacle walkaround snakes through the row even
    when a parallel lane a few tenths of a millimetre off to the side is
    completely clear.  Prefer that lane: shift the whole line by the
    smallest worked offset along its normal and emit
    ``[start, start+off, end+off, end]`` — a straight hug instead of a
    zig-zag.

    Returns the shortest clean lane (smallest ``|off|``) or ``None``
    when no tested offset keeps ``clearance`` edge-to-edge from every
    obstacle.  Offsets are tried from ``step`` outward in both normal
    directions, so the chosen lane hugs the requested line as closely as
    possible.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux  # unit normal to the direct line
    margin = clearance + track_width / 2.0

    # Whole-line clearance check: every segment of the candidate three-
    # segment polyline ([start, s2] normal stub, [s2, e2] parallel lane,
    # [e2, end] normal stub) must keep the margin from all solids.
    def _lane_clear(s2: tuple[float, float], e2: tuple[float, float]) -> bool:
        lane = LineString([start, s2, e2, end])
        for o in node.obstacles():
            if o.shape is None or o.shape.is_empty:
                continue
            if float(o.shape.distance(lane)) < margin - 1e-9:
                return False
        return True

    hops = int(math.ceil(max_offset / step))
    best: list[tuple[float, float]] | None = None
    best_mag = math.inf
    for i in range(1, hops + 1):
        for sign in (1.0, -1.0):
            off = sign * i * step
            s2 = (start[0] + nx * off, start[1] + ny * off)
            e2 = (end[0] + nx * off, end[1] + ny * off)
            if not _lane_clear(s2, e2):
                continue
            mag = abs(off)
            if mag < best_mag:
                best_mag = mag
                best = [start, s2, e2, end]
    return best


def _walkaround_solids(
    path: list[tuple[float, float]],
    node: ObstacleNode,
    track_width: float,
    clearance: float,
    max_iter: int = MAX_WALKAROUND_ITER,
    frames: list[dict] | None = None,
) -> list[tuple[float, float]]:
    """Bump the path around every fixed solid until collision-free.

    Each iteration: nearest obstacle within the copper trigger distance,
    walk its hull both CW and CCW, keep the shorter result; repeat.
    Mirrors KiCad's WALKAROUND::Route single-step loop.

    ``frames`` optionally accumulates one entry per iteration
    (``{"stage", "path", "hit", "note"}``) so a failure renders as a
    process sequence, and the hit track's pinned endpoints are visible.

    The trigger mirrors the final DRC audit exactly: it fires when the
    route COPPER (the centerline buffered by half the track width, round
    caps) comes within ``clearance`` of an obstacle edge.  A bend's
    round join sweeps the copper ``width/2`` beyond the centerline, so a
    corner may violate clearance while its centerline is still clear of
    a centerline-only probe.  Because copper distance equals
    ``max(0, centerline distance - width/2)`` for round caps, every
    candidate whose copper can reach ``clearance`` sits within
    ``clearance + width/2`` of the centerline — probe there, then verify
    the closest hit against the buffered line.  If the closest candidate
    is boundary-clean (copper exactly at ``clearance``), every farther
    one is cleaner too (distance is monotone), so the loop can stop.

    The walk hull is inflated by ``clearance + width/2 + CLEARANCE_EPS``
    so a bounded line keeps strictly more than ``clearance`` edge-to-edge
    and never re-triggers the probe on the same obstacle.
    """
    pts = list(path)
    half_w = track_width / 2.0
    hull_margin = clearance + CLEARANCE_EPS + half_w
    probe = clearance + half_w
    for it in range(max_iter):
        hit = node.nearest(pts, dfence=probe)
        if frames is not None:
            frames.append(
                {
                    "stage": f"walk-iter-{it:02d}",
                    "path": list(pts),
                    "hit": f"{hit.obstacle.kind} {hit.obstacle.ref or hit.obstacle.net or ''}".strip()
                    if hit is not None
                    else None,
                    "note": f"walkaround iteration {it}",
                    # The obstacle set the engine actually consults at
                    # this iteration (movable tracks excluded after
                    # promote rounds) — the dump renders THESE, not the
                    # static board model, so a hit is attributable.
                    "obstacles": list(node.obstacles()),
                }
            )
        if hit is None:
            return pts
        if (
            LineString(pts).buffer(half_w, cap_style="round").distance(hit.obstacle.shape)
            >= clearance - 1e-9
        ):
            return pts  # closest candidate is boundary-clean: all are clean
        obs = hit.obstacle
        # Obstacle shape already carries its own half-width; add the
        # route half-width + clearance so the walked line gets DRC margin.
        # The hull is snapped onto the 0/45/90 family (outer octagon):
        # walking family edges yields family-only segments, so no
        # arbitrary-angle chords remain on the detour.
        hull = _family_hull(obs.shape, hull_margin)
        if hull.is_empty:
            raise PnsFailure(
                f"obstacle {obs.kind} has an empty hull",
                last_path=list(pts),
                last_hit=f"{obs.kind} {obs.ref or obs.net or ''}".strip(),
                frames=frames,
            )
        best: list[tuple[float, float]] | None = None
        for cw in (True, False):
            try:
                walked = walkaround_line(pts, hull, cw=cw)
            except WalkFailure:
                continue
            if best is None or _path_len(walked) < _path_len(best):
                best = walked
        if best is None:
            raise PnsFailure(
                f"cannot walk around {obs.kind} obstacle {obs.ref}",
                last_path=list(pts),
                last_hit=f"{obs.kind} {obs.ref or obs.net or ''}".strip(),
                frames=frames,
            )
        pts = best
    hit = node.nearest(pts, dfence=probe)
    raise PnsFailure(
        f"walkaround did not converge in {max_iter} iterations",
        last_path=list(pts),
        last_hit=f"{hit.obstacle.kind} {hit.obstacle.ref or hit.obstacle.net or ''}".strip()
        if hit is not None
        else None,
        frames=frames,
    )


def _visibility_detour(
    start: tuple[float, float],
    end: tuple[float, float],
    node: ObstacleNode,
    track_width: float,
    clearance: float,
    frames: list[dict] | None = None,
    board_limit: Polygon | None = None,
) -> list[tuple[float, float]] | None:
    """Multi-bend detour via the visibility graph.

    Lane probes a straight parallel offset; walkaround hugs one obstacle
    at a time.  Both are single-context searches: when the direct line
    is blocked by a *cluster* (a THT pad column, a bundle of parallel
    tracks), the free lane may lie around the whole cluster, not just
    beside the surface of the nearest member, and neither search looks
    there.  The visibility graph over ``node``'s obstacles — every
    obstacle-family hull, connected by clear-of-obstacles segments —
    finds the global geometric shortest path with any number of bends.

    The graph is built over the family-hull octagons (``margin =
    clearance + width/2``), so every graph edge is already DRC-clean:
    vertices sit at the hull (which carries the route margin) and edges
    are the straight runs between them that cross no hull.  Octagon
    vertices are 0/45/90-family so the detour is family-only, exactly
    like the walkaround hulls.  Returns the polyline ``[start, ..., end]``
    or ``None`` when the graph has no start→end connection (start/end in
    different connected components — a genuine enclosure).
    """
    from kcaa.router.world_model import Obstacle as _Obstacle

    margin = clearance + CLEARANCE_EPS + track_width / 2.0
    try:
        obs = node.obstacles()
    except Exception:
        return None
    layers: dict[str, None] = {}
    for o in obs:
        for l in o.layers:
            layers[l] = None
    if not layers:
        return None
    # The engine runs one layer at a time; if a stray obstacle carries a
    # foreign layer the graph builder would filter it out anyway.
    layer = next(iter(layers))
    hulls: list[_Obstacle] = []
    for o in obs:
        if o.shape is None or o.shape.is_empty:
            continue
        hull = _family_hull(o.shape, margin)
        if hull.is_empty:
            continue
        hulls.append(
            _Obstacle(
                shape=hull,
                layers=frozenset({layer}),
                net=None,  # graph builder treats net-carrying solids as "route" copper
                kind=o.kind,
            )
        )
    if not hulls:
        return None
    graph = build_visibility_graph(
        hulls,
        [layer],
        start,
        end,
        start_layer=layer,
        end_layer=layer,
        board_limit=board_limit,
    )
    ids = graph.shortest_path(0, 1)
    if not ids:
        return None
    pts = [(graph.nodes[i].x, graph.nodes[i].y) for i in ids]
    # A detour must actually detour: a direct sightline would have been
    # caught by ``direct_blocked``/walkaround; if the graph still yields
    # the two-point skeleton (start,end) it means the direct line is
    # clear under the graph's hull metric but not ours — reject.
    if len(pts) <= 2:
        return None
    if frames is not None:
        frames.append(
            {
                "stage": f"vis-graph-{len(frames):02d}",
                "path": list(pts),
                "hit": None,
                "note": f"visibility-graph detour ({len(pts)} pts)",
                "obstacles": list(obs),
            }
        )
    return pts


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
