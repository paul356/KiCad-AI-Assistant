"""
High-level router orchestration: turn a user request into a list of
``OutputSegment`` + ``OutputVia`` ready to be written into a .kicad_pcb.

Pipeline
--------

1. **Load PCB & DRC**: parse the S-expression, read the matching ``.kicad_pro``
   for net class rules.
2. **Build world model**: hand the PCB to :func:`kcaa.router.world_model.
   build_world_model` so we know where all obstacles sit.
3. **Find pad centers**: locate the two pads to connect and read their copper
   pad shape's center.
4. **Pick exit points**: choose one or two candidate exit points on the pad
   edge for each end (axis-aligned first, 45° if needed).
5. **Grid search + A\\***: build a walkability grid from inflated obstacles
   and run 8-direction A*.  Multi-layer routes insert via edges at legal
   (x, y) positions between layers in ``via_pairs``.
6. **Postprocess**: simplify collinear runs, miter corners, emit segments
   and vias at layer transitions.

Multi-layer routing
-------------------

When ``start_layer != end_layer`` the router builds one GridMap per
routing layer and runs a multi-layer A* that can insert via edges.
Via edges cost 2.0 mm (configurable) — this penalises vias so A* prefers
routing on a single layer when possible.

No shove
--------

This is the **no-shove** variant. If a route is blocked, we raise
:class:`RouteFailure` rather than displacing existing tracks. That is enough
for ~80% of "connect A to B" requests; the remaining cases need the user to
move a track or add a via first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import os

from shapely.geometry import box as _shapely_box

from kcaa.router.grid_a_star import (
    GRID_RESOLUTION,
    hierarchical_a_star,
    multi_layer_a_star,
    path_to_nodes,
    shortcut_path,
    simplify_path,
    snap_to_45_path_safe,
)
from kcaa.router.path_postprocess import (
    OutputSegment,
    OutputVia,
    postprocess_path,
)
from kcaa.router.visibility_graph import RouteNode
from kcaa.router.world_model import Obstacle, build_world_model
from kcaa.utils.pcb_sexp_utils import load_pcb

logger = logging.getLogger(__name__)


class RouteFailure(RuntimeError):
    """Raised when no valid route can be found."""


class ProFileMissing(RuntimeError):
    """No ``.kicad_pro`` found next to the ``.kicad_pcb``.

    The router needs the project file to look up netclass settings for
    width/clearance. Either create the project file in KiCad, or pass
    ``width=`` and ``clearance=`` explicitly in :class:`RouteRequest`.
    """


class ProFileMalformed(RuntimeError):
    """The ``.kicad_pro`` exists but cannot be read or parsed.

    This is almost always a sign of file corruption. Fix the project file
    in KiCad before re-running.
    """


class NetClassUnresolved(RuntimeError):
    """A net did not match any ``netclass_patterns`` entry, and the project
    has no ``Default`` netclass to fall back to.

    Either add the net to a netclass, add a ``Default`` netclass, or pass
    ``width=`` explicitly in :class:`RouteRequest`.
    """


class DesignRulesUnavailable(RuntimeError):
    """The board's design rules cannot be read, or ``min_clearance`` is
    missing.

    Pass ``clearance=`` explicitly in :class:`RouteRequest` to override.
    """


@dataclass
class RouteRequest:
    """A request to connect two pads with a track.

    The pads may live on different copper layers; in that case the router
    will insert one or more vias to switch layers.

    Attributes:
        pcb_path: Absolute path to the ``.kicad_pcb`` file.
        ref_a / pad_a: Reference designator and pad number for one end.
        ref_b / pad_b: Reference designator and pad number for the other end.
        net: Net name shared by both pads.
        start_layer: Copper layer the pad-A copper shape is on.
        end_layer: Copper layer the pad-B copper shape is on.
        via_pairs: Allowed (top, bottom) layer pairs that may carry a
            through-via. Default is ``(("F.Cu", "B.Cu"),)``. Pass an
            explicit tuple to restrict transitions (e.g. to forbid inner-
            layer vias on a 4-layer board).
        width: Track width; ``None`` → resolve from netclass.
        clearance: Minimum clearance to obstacles; ``None`` → resolve from
            the board's design rules.
        via_diameter / via_drill: Through-via dimensions; ``None`` →
            resolve from netclass.
        max_miter_mm: Maximum corner miter extension before falling back
            to a sharp 90° corner.
        grid_resolution: Grid cell size in mm for the walkability grid.
            Smaller values give finer paths but more cells.  ``None``
            uses the default (0.025 mm).
        via_cost: Distance-equivalent penalty for taking a via edge in
            multi-layer A*.  Higher values discourage unnecessary stack
            vias.  Default 2.0 mm.
    """

    pcb_path: str
    ref_a: str
    pad_a: str
    ref_b: str
    pad_b: str
    net: str
    start_layer: str = "F.Cu"
    end_layer: str = "F.Cu"
    via_pairs: tuple[tuple[str, str], ...] = (("F.Cu", "B.Cu"),)
    width: float | None = None  # if None, use DRC default for the net
    clearance: float | None = None
    via_diameter: float | None = None
    via_drill: float | None = None
    max_miter_mm: float = 1.0
    grid_resolution: float | None = None  # None → GRID_RESOLUTION
    via_cost: float = 2.0  # mm penalty per via edge


@dataclass
class RouteResult:
    """The output of a successful routing attempt.

    Attributes:
        segments: Track segments, all carrying the same ``layer`` as their
            corresponding path run. A route that crosses layers has
            multiple runs (one per layer), separated by vias.
        vias: Through-vias inserted at layer transitions. Empty for a
            single-layer route.
        start / end: The pad centres the route connected.
        layers_used: The copper layers the route actually traversed, in
            order. Useful for callers that want to know whether a via
            was inserted (``len(layers_used) > 1``).
    """

    segments: list[OutputSegment] = field(default_factory=list)
    vias: list[OutputVia] = field(default_factory=list)
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    layers_used: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_route_pair(req: RouteRequest) -> RouteResult:
    """Connect pad ``req.pad_a`` on ``req.ref_a`` to pad ``req.pad_b`` on
    ``req.ref_b`` on the same net ``req.net`` and same layer ``req.start_layer``
    (and ``req.end_layer`` for the destination pad).

    Returns:
        A :class:`RouteResult` containing the segments and (optionally) vias.

    Raises:
        RouteFailure: If no path is found or inputs are invalid.
    """
    data = load_pcb(req.pcb_path)

    # Validate requested layers against the PCB early. The visibility graph
    # query below assumes both layers exist; checking later would produce
    # a less informative error path.
    pcb_layers = _pcb_layer_names(data)
    for layer in (req.start_layer, req.end_layer):
        if layer not in pcb_layers:
            raise RouteFailure(
                f"Layer {layer!r} is not present in PCB {req.pcb_path}; "
                f"PCB layers are {pcb_layers}."
            )
    # via_pairs must reference layers that exist too — otherwise A* would
    # never use those edges and the user might not notice.
    for top, bot in req.via_pairs:
        if top not in pcb_layers:
            raise RouteFailure(
                f"via_pairs contains top layer {top!r} which is not in PCB "
                f"{req.pcb_path}; PCB layers are {pcb_layers}."
            )
        if bot not in pcb_layers:
            raise RouteFailure(
                f"via_pairs contains bottom layer {bot!r} which is not in PCB "
                f"{req.pcb_path}; PCB layers are {pcb_layers}."
            )

    # DRC defaults from the .kicad_pro / board file. Fail loudly if the
    # project file is missing/malformed rather than silently guessing.
    width = req.width
    if width is None:
        try:
            width = _default_track_width(req.pcb_path, req.net)
        except (ProFileMissing, ProFileMalformed, NetClassUnresolved) as exc:
            raise RouteFailure(
                f"Cannot determine track width for net {req.net!r}: {exc}. "
                f"Pass width= explicitly in RouteRequest to skip DRC lookup."
            ) from exc
    clearance = req.clearance
    if clearance is None:
        try:
            clearance = _default_clearance(req.pcb_path, net=req.net)
        except (ProFileMissing, ProFileMalformed, DesignRulesUnavailable) as exc:
            raise RouteFailure(
                f"Cannot determine clearance: {exc}. "
                f"Pass clearance= explicitly in RouteRequest to skip DRC lookup."
            ) from exc
    via_diameter = req.via_diameter
    if via_diameter is None:
        via_diameter = _resolve_via_diameter(req.pcb_path, net=req.net)
    via_drill = req.via_drill
    if via_drill is None:
        via_drill = _resolve_via_drill(req.pcb_path, net=req.net)

    # Pad center coordinates.
    pad_a_xy = _find_pad_center(data, req.ref_a, req.pad_a)
    pad_b_xy = _find_pad_center(data, req.ref_b, req.pad_b)
    if pad_a_xy is None:
        raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} not found")
    if pad_b_xy is None:
        raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} not found")

    # Validate pads exist on the requested copper layers before doing
    # anything else (gives a clear error, not a confusing A* failure).
    if _find_pad_size(data, req.ref_a, req.pad_a, req.start_layer) is None:
        raise RouteFailure(
            f"Pad {req.ref_a}/{req.pad_a} has no copper shape on layer "
            f"{req.start_layer!r}; cannot route from there."
        )
    if _find_pad_size(data, req.ref_b, req.pad_b, req.end_layer) is None:
        raise RouteFailure(
            f"Pad {req.ref_b}/{req.pad_b} has no copper shape on layer "
            f"{req.end_layer!r}; cannot route to there."
        )

    # World model: only existing copper (tracks, vias, keepouts) blocks the
    # route.  Footprint courtyards are NOT obstacles — they're a DRC spacing
    # concept, not a hard copper boundary, and treating them as forbidden
    # forces pointless detours around parts.  Start/end footprints would be
    # excluded anyway, but we skip the whole footprint layer.
    model = build_world_model(
        req.pcb_path,
        net_filter=req.net,
    )

    # Shrink obstacles by half the trace width (so the track is centered on
    # the line) and inflate by clearance. Remaining: forbidden region.
    buffered = _inflate_obstacles(model.obstacles, width / 2.0 + clearance)

    # Group inflated obstacles by layer for multi-layer routing.
    routing_layers = _routing_layers(req)
    obstacles_by_layer: dict[str, list] = {}
    for rl in routing_layers:
        obstacles_by_layer[rl] = [o for o in buffered if rl in o.layers]

    # Detect rectangular pads to replace A* path inside them.
    # A pad is "rectangular" when its width and height differ by ≥ 20%.
    def _is_rect(size: tuple[float, float] | None) -> bool:
        if size is None:
            return False
        w, h = size
        return w > 0 and h > 0 and abs(w - h) / max(w, h) >= 0.2

    def _world_size(local_w: float, local_h: float, fp_rot: float) -> tuple[float, float]:
        """Return (world_w, world_h) accounting for ±90° footprint rotation."""
        if abs(fp_rot % 180.0 - 90.0) < 0.1:
            return local_h, local_w
        return local_w, local_h

    pad_a_size = _find_pad_size(data, req.ref_a, req.pad_a, req.start_layer)
    pad_b_size = _find_pad_size(data, req.ref_b, req.pad_b, req.end_layer)
    pad_a_world_size = (
        _world_size(pad_a_size[0], pad_a_size[1], _fp_rotation(data, req.ref_a))
        if pad_a_size
        else None
    )
    pad_b_world_size = (
        _world_size(pad_b_size[0], pad_b_size[1], _fp_rotation(data, req.ref_b))
        if pad_b_size
        else None
    )
    pad_a_size = pad_a_world_size if pad_a_world_size and _is_rect(pad_a_world_size) else None
    pad_b_size = pad_b_world_size if pad_b_world_size and _is_rect(pad_b_world_size) else None

    # For multi-layer routing, clear the start/end pad areas from the
    # obstacle grid so A* can start from / end at the pad centres.
    # Existing copper (e.g. a track from another net) may occupy the pad.
    if req.start_layer != req.end_layer:
        if pad_a_world_size is not None:
            obstacles_by_layer[req.start_layer] = _subtract_pad_aabb(
                obstacles_by_layer[req.start_layer],
                pad_a_xy,
                pad_a_world_size,
            )
        if pad_b_world_size is not None:
            obstacles_by_layer[req.end_layer] = _subtract_pad_aabb(
                obstacles_by_layer[req.end_layer],
                pad_b_xy,
                pad_b_world_size,
            )

    # Route from pad center to pad center.  A* on the grid naturally
    # produces 0/45/90° segments.
    route_bbox = (
        min(pad_a_xy[0], pad_b_xy[0]) - 5.0,
        min(pad_a_xy[1], pad_b_xy[1]) - 5.0,
        max(pad_a_xy[0], pad_b_xy[0]) + 5.0,
        max(pad_a_xy[1], pad_b_xy[1]) + 5.0,
    )
    grid_res = req.grid_resolution or GRID_RESOLUTION

    # ---- A* directly from pad centre to pad centre ----
    _ax, _ay = pad_a_xy
    _bx, _by = pad_b_xy
    print(
        f"  [route] {req.ref_a}/{req.pad_a} ({_ax:.3f},{_ay:.3f})"
        f" → {req.ref_b}/{req.pad_b} ({_bx:.3f},{_by:.3f})"
        f"  size_a={pad_a_size} size_b={pad_b_size}"
    )
    # Build pad-rectangle descriptors early (used by postprocess_path in
    # both single-layer and multi-layer branches).
    _pad_rects: list[tuple[float, float, float, float]] = []
    for psize, pcenter in [(pad_a_size, pad_a_xy), (pad_b_size, pad_b_xy)]:
        if psize is not None:
            w, h = psize
            _pad_rects.append((pcenter[0], pcenter[1], w / 2.0, h / 2.0))

    # Build viz context: pad rects (all pads, not just rectangular).
    _pad_viz: list[tuple[str, tuple[float, float, float, float]]] = []
    for name, psize, pcenter in [
        (f"{req.ref_a}/{req.pad_a}", pad_a_world_size, pad_a_xy),
        (f"{req.ref_b}/{req.pad_b}", pad_b_world_size, pad_b_xy),
    ]:
        if psize is not None:
            w, h = psize
            _pad_viz.append(
                (
                    name,
                    (
                        pcenter[0] - w / 2,
                        pcenter[1] - h / 2,
                        pcenter[0] + w / 2,
                        pcenter[1] + h / 2,
                    ),
                )
            )

    if req.start_layer != req.end_layer:
        # ── Multi-layer: grid A* with via edges ──────────────────────
        # Build via-forbidden zones from start/end pad AABBs to
        # prevent via-in-pad (DFM issue: solder wicking).
        via_forbidden: list = []
        if pad_a_world_size is not None:
            via_forbidden.append(
                _shapely_box(
                    pad_a_xy[0] - pad_a_world_size[0] / 2.0,
                    pad_a_xy[1] - pad_a_world_size[1] / 2.0,
                    pad_a_xy[0] + pad_a_world_size[0] / 2.0,
                    pad_a_xy[1] + pad_a_world_size[1] / 2.0,
                )
            )
        if pad_b_world_size is not None:
            via_forbidden.append(
                _shapely_box(
                    pad_b_xy[0] - pad_b_world_size[0] / 2.0,
                    pad_b_xy[1] - pad_b_world_size[1] / 2.0,
                    pad_b_xy[0] + pad_b_world_size[0] / 2.0,
                    pad_b_xy[1] + pad_b_world_size[1] / 2.0,
                )
            )
        ml_result = multi_layer_a_star(
            obstacles_by_layer,
            pad_a_xy,
            pad_b_xy,
            req.start_layer,
            req.end_layer,
            req.via_pairs,
            route_bbox,
            grid_res,
            via_cost=req.via_cost,
            via_forbidden_zones=via_forbidden or None,
        )
        if ml_result.path is None:
            raise RouteFailure(
                f"No obstacle-avoiding multi-layer path from "
                f"{req.ref_a}/{req.pad_a} to {req.ref_b}/{req.pad_b} at "
                f"{width}mm track width ({req.start_layer} → {req.end_layer})."
            )
        print(
            f"  [route] multi-layer A*: {len(ml_result.path)} pts"
            f"  cells_visited={ml_result.cells_visited}"
        )

        # Group GridNode by layer, run per-segment postprocess.
        from itertools import groupby

        groups = [
            (layer, list(grp)) for layer, grp in groupby(ml_result.path, key=lambda n: n.layer)
        ]
        all_nodes: list[RouteNode] = []
        node_id = 0
        for gi, (layer, nodes) in enumerate(groups):
            pts = [(n.x, n.y) for n in nodes]
            if len(pts) < 2:
                # Single-point segment (e.g. layer transition without
                # meaningful path on the layer).  Keep the point for
                # via continuity but skip postprocessing.
                for n in nodes:
                    all_nodes.append(RouteNode(x=n.x, y=n.y, layer=layer, node_id=node_id))
                    node_id += 1
                continue
            obs = obstacles_by_layer.get(layer, [])
            prefix = f"layer-{layer}"
            is_first = gi == 0
            is_last = gi == len(groups) - 1

            _dump_viz(f"{prefix}-0-astar", pts, _pad_viz, obs, route_bbox)

            # Pad replacement (start/end segments only).
            if is_first and pad_a_size is not None:
                n_before = len(pts)
                pts = _replace_pad_path(pts, pad_a_xy, pad_a_size, from_center=True)
                _log_path(f"{prefix}-pad-replace", pts, n_before)
            elif is_last and pad_b_size is not None:
                n_before = len(pts)
                pts = _replace_pad_path(pts, pad_b_xy, pad_b_size, from_center=False)
                _log_path(f"{prefix}-pad-replace", pts, n_before)
            _dump_viz(f"{prefix}-1-pad-replace", pts, _pad_viz, obs, route_bbox)

            # Simplify → shortcut → snap45.
            pts = _postprocess_layer_segment(pts, obs, route_bbox, grid_res, prefix, _pad_viz)

            # Align endpoint to pad centre (start/end segments only).
            if is_first and pad_a_size is not None:
                pts = _align_single_endpoint(
                    pts, pad_a_xy, obs, route_bbox, grid_res, pad_a_size, from_center=True
                )
            elif is_last and pad_b_size is not None:
                pts = _align_single_endpoint(
                    pts, pad_b_xy, obs, route_bbox, grid_res, pad_b_size, from_center=False
                )
            _dump_viz(f"{prefix}-6-align", pts, _pad_viz, obs, route_bbox)

            for x, y in pts:
                all_nodes.append(RouteNode(x=x, y=y, layer=layer, node_id=node_id))
                node_id += 1

        segs, vias = postprocess_path(
            all_nodes,
            width=width,
            net=req.net,
            max_miter_mm=req.max_miter_mm,
            via_diameter_mm=via_diameter,
            via_drill_mm=via_drill,
            _obstacles=buffered,
            _pad_rects=_pad_rects or None,
        )
        segs = [s for s in segs if abs(s.x1 - s.x2) > 1e-6 or abs(s.y1 - s.y2) > 1e-6]
        _log_output_segments("final", segs)
        _dump_viz_segments("7-final", segs, _pad_viz, buffered, route_bbox)

        start_xy = (all_nodes[0].x, all_nodes[0].y)
        end_xy = (all_nodes[-1].x, all_nodes[-1].y)
        layers_used = _layers_used(all_nodes)
    else:
        # ── Single-layer: hierarchical A* ───────────────────────────
        result = hierarchical_a_star(
            buffered,
            pad_a_xy,
            pad_b_xy,
            fine_resolution=grid_res,
            route_bbox=route_bbox,
        )
        if result.path is None:
            raise RouteFailure(
                f"No obstacle-avoiding path from {req.ref_a}/{req.pad_a} to "
                f"{req.ref_b}/{req.pad_b} at {req.width or 0.5}mm "
                f"track width on layer {req.start_layer}."
            )
        print(f"  [route] A*: {len(result.path)} pts  cells_visited={result.cells_visited}")

        # Strip layer_idx from the unified A* result.
        raw_pts = [(x, y) for x, y, _ in result.path]
        _dump_viz("0-astar", raw_pts, _pad_viz, buffered, route_bbox)

        # ---- Discard A* path inside rectangular pads and replace with
        #      axis-aligned wire (fence → centre). ----
        best_path_pts = raw_pts
        if pad_a_size is not None:
            n_before = len(best_path_pts)
            best_path_pts = _replace_pad_path(best_path_pts, pad_a_xy, pad_a_size, from_center=True)
            _log_path("pad_a-replace", best_path_pts, n_before)
        if pad_b_size is not None:
            n_before = len(best_path_pts)
            best_path_pts = _replace_pad_path(
                best_path_pts, pad_b_xy, pad_b_size, from_center=False
            )
            _log_path("pad_b-replace", best_path_pts, n_before)
        _dump_viz("1-pad-replace", best_path_pts, _pad_viz, buffered, route_bbox)

        # ---- Post-process: simplify → shortcut → snap45 ----
        best_path_pts = _postprocess_layer_segment(
            best_path_pts, buffered, route_bbox, grid_res, "", _pad_viz
        )

        # ---- Align path endpoints with exact pad centres ----
        best_path_pts = _align_path_endpoints(
            best_path_pts,
            pad_a_xy,
            pad_b_xy,
            buffered,
            route_bbox,
            grid_res,
            pad_a_size=pad_a_size,
            pad_b_size=pad_b_size,
        )
        _log_path("align-endpoints", best_path_pts)
        _dump_viz("6-align-endpoints", best_path_pts, _pad_viz, buffered, route_bbox)

        path_nodes = path_to_nodes(best_path_pts, req.start_layer)
        segs, vias = postprocess_path(
            path_nodes,
            width=width,
            net=req.net,
            max_miter_mm=req.max_miter_mm,
            _obstacles=buffered,
            _pad_rects=_pad_rects or None,
        )
        segs = [s for s in segs if abs(s.x1 - s.x2) > 1e-6 or abs(s.y1 - s.y2) > 1e-6]
        _log_output_segments("final", segs)
        _dump_viz_segments("7-final", segs, _pad_viz, buffered, route_bbox)

        start_xy = (path_nodes[0].x, path_nodes[0].y)
        end_xy = (path_nodes[-1].x, path_nodes[-1].y)
        layers_used = _layers_used(path_nodes)

    # Verify every emitted segment stays inside the board (Edge.Cuts).
    # We use a board polygon that is shrunk by width/2 on each side so the
    # track's copper edge is what we check against, not its centerline.
    #
    # Edge.Cuts is a workflow artifact: the user may legitimately be
    # routing before the board outline is drawn, so a missing board is
    # a warning, not a failure. A *present* board with segments that
    # cross it, on the other hand, is a router bug we must surface.
    if model.board_bbox is None:
        logger.warning(
            "No Edge.Cuts items in %s; skipping board-bounds check. "
            "Add an Edge.Cuts outline to verify segments stay within the board.",
            req.pcb_path,
        )
    else:
        _check_segments_in_board(segs, model.board_bbox)
        if vias:
            _check_vias_in_board(vias, model.board_bbox)

    return RouteResult(
        segments=segs,
        vias=vias,
        start=start_xy,
        end=end_xy,
        layers_used=layers_used,
    )


def connect_with_via(
    seg_a: OutputSegment,
    seg_b: OutputSegment,
    net: str,
    diameter: float,
    drill: float,
    layer_a: str,
    layer_b: str,
) -> OutputVia:
    """Helper to build a through-via connecting two segments on different layers.

    The via is placed at the (x, y) of ``seg_a``'s end. Both segments are
    expected to end at the same point.
    """
    return OutputVia(
        x=seg_a.x2,
        y=seg_a.y2,
        diameter=diameter,
        drill=drill,
        layers=(layer_a, layer_b),
        net=net,
    )


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------


def _seg_angle(x1: float, y1: float, x2: float, y2: float) -> float:
    """Segment angle in degrees (0=right, 90=down)."""
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360


def _log_path(label: str, pts: list[tuple[float, float]], prev_n: int | None = None) -> None:
    """Log a polyline step: point count, endpoints, segment breakdown."""
    n = len(pts)
    delta = f" (was {prev_n})" if prev_n is not None else ""
    if n < 2:
        print(f"  [{label}] {n} pts{delta}")
        return
    segs = []
    for i in range(n - 1):
        x1, y1, x2, y2 = pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]
        ang = _seg_angle(x1, y1, x2, y2)
        # Classify
        if abs(x1 - x2) < 1e-6:
            kind = "vert"
        elif abs(y1 - y2) < 1e-6:
            kind = "horiz"
        elif abs(abs(x2 - x1) - abs(y2 - y1)) < 1e-6:
            kind = "45diag"
        else:
            kind = f"{ang:.0f}deg"
        segs.append(f"({x1:.3f},{y1:.3f})→({x2:.3f},{y2:.3f}){kind}")
    print(f"  [{label}] {n} pts{delta}: {segs[0]}{'  ...  ' + segs[-1] if len(segs) > 1 else ''}")
    if len(segs) <= 6:
        for s in segs:
            print(f"           {s}")


def _log_output_segments(label: str, segs: list) -> None:
    """Log final OutputSegments with angles."""
    for i, s in enumerate(segs):
        ang = _seg_angle(s.x1, s.y1, s.x2, s.y2)
        kind = (
            "horiz"
            if abs(s.y1 - s.y2) < 1e-6
            else "vert"
            if abs(s.x1 - s.x2) < 1e-6
            else "diag"
            if abs(abs(s.x2 - s.x1) - abs(s.y2 - s.y1)) < 1e-6
            else f"{ang:.0f}deg"
        )
        print(f"  [{label}] seg{i}: ({s.x1:.3f},{s.y1:.3f})→({s.x2:.3f},{s.y2:.3f}) {kind}")


# ---------------------------------------------------------------------------
# Visualization dump
# ---------------------------------------------------------------------------

import os as _os
import time as _time


def _viz_dir() -> str:
    """Return the viz dump directory under the kcaa data dir."""
    from kcaa.utils.config import config

    return _os.path.join(config.get_kcaa_data_dir(), "kcaa_viz")


def _dump_viz(
    stage: str,
    pts: list[tuple[float, float]],
    pad_viz: list[tuple[str, tuple[float, float, float, float]]],
    obstacles: list,
    route_bbox: tuple[float, float, float, float],
) -> None:
    """Dump path, pad rects, and obstacles to a JSON file for rendering.

    Only writes when ``config.viz_dump_enabled`` is ``True`` (set via
    ``KCAA_DUMP_ROUTE_PIPELINE=1`` in ``.env``).
    """
    from kcaa.utils.config import config

    if not config.viz_dump_enabled:
        return
    d = _viz_dir()
    _os.makedirs(d, exist_ok=True)
    ts = _time.strftime("%H%M%S")
    fname = _os.path.join(d, f"{ts}_{stage}.json")
    data = {
        "stage": stage,
        "path": [(x, y) for x, y in pts],
        "pads": [(name, list(aabb)) for name, aabb in pad_viz],
        "obstacles": [
            (list(o.shape.exterior.coords), o.kind, o.ref or "")
            for o in obstacles[:500]  # cap to avoid huge files
        ],
        "route_bbox": list(route_bbox),
    }
    with open(fname, "w") as f:
        json.dump(data, f)
    print(f"  [viz] dumped {fname}")


def _dump_viz_segments(
    stage: str,
    segs: list,
    pad_viz: list[tuple[str, tuple[float, float, float, float]]],
    obstacles: list,
    route_bbox: tuple[float, float, float, float],
) -> None:
    """Dump OutputSegments as a path for rendering.

    Only writes when ``config.viz_dump_enabled`` is ``True`` (set via
    ``KCAA_DUMP_ROUTE_PIPELINE=1`` in ``.env``).
    """
    from kcaa.utils.config import config

    if not config.viz_dump_enabled:
        return
    d = _viz_dir()
    _os.makedirs(d, exist_ok=True)
    ts = _time.strftime("%H%M%S")
    fname = _os.path.join(d, f"{ts}_{stage}.json")
    pts: list[tuple[float, float]] = []
    for s in segs:
        pts.append((s.x1, s.y1))
        pts.append((s.x2, s.y2))
    # deduplicate consecutive dups
    dedup = []
    for p in pts:
        if not dedup or abs(p[0] - dedup[-1][0]) > 1e-6 or abs(p[1] - dedup[-1][1]) > 1e-6:
            dedup.append(p)
    data = {
        "stage": stage,
        "path": dedup,
        "pads": [(name, list(aabb)) for name, aabb in pad_viz],
        "obstacles": [
            (list(o.shape.exterior.coords), o.kind, o.ref or "") for o in obstacles[:500]
        ],
        "route_bbox": list(route_bbox),
    }
    with open(fname, "w") as f:
        json.dump(data, f)
    print(f"  [viz] dumped {fname}")


# ---------------------------------------------------------------------------
# Pad area clearing (for multi-layer A* start/end cells)
# ---------------------------------------------------------------------------


def _subtract_pad_aabb(
    obstacles: list[Obstacle],
    pad_center: tuple[float, float],
    pad_size: tuple[float, float],
) -> list[Obstacle]:
    """Subtract a pad's AABB from each obstacle, returning only non-empty results.

    Called before multi-layer A* to unblock the grid cells at the start
    and end pad centres (existing copper from other nets may occupy the pad
    area on the same layer).
    """
    pad_rect = _shapely_box(
        pad_center[0] - pad_size[0] / 2.0,
        pad_center[1] - pad_size[1] / 2.0,
        pad_center[0] + pad_size[0] / 2.0,
        pad_center[1] + pad_size[1] / 2.0,
    )
    out: list[Obstacle] = []
    for o in obstacles:
        diff = o.shape.difference(pad_rect)
        if not diff.is_empty:
            out.append(Obstacle(shape=diff, layers=o.layers, net=o.net, kind=o.kind, ref=o.ref))
    return out


# ---------------------------------------------------------------------------
# Obstacle buffering
# ---------------------------------------------------------------------------


def _inflate_obstacles(obstacles: list[Obstacle], delta: float) -> list[Obstacle]:
    """Grow each obstacle's polygon by ``delta`` (negative shrinks it).

    Returns new Obstacle instances (shapely Polygon buffers are immutable).
    Tracks and vias are widened/shrunk by half the track width and clearance;
    footprints and keepouts are inflated by clearance alone (the track width
    is already implicit in their AABB extent, but clearance is not).
    """
    if delta == 0:
        return list(obstacles)
    out: list[Obstacle] = []
    for o in obstacles:
        new_shape = o.shape.buffer(delta)
        if new_shape.is_empty:
            continue
        out.append(
            Obstacle(
                shape=new_shape,
                layers=o.layers,
                net=o.net,
                kind=o.kind,
                ref=o.ref,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Postprocess pipeline (shared by single-layer and multi-layer)
# ---------------------------------------------------------------------------


def _postprocess_layer_segment(
    pts: list[tuple[float, float]],
    obstacles: list,
    route_bbox: tuple[float, float, float, float],
    grid_res: float,
    stage_prefix: str,
    pad_viz: list[tuple[str, tuple[float, float, float, float]]],
) -> list[tuple[float, float]]:
    """Run simplify → shortcut → snap45 on a single layer's path points.

    Each step dumps a viz file with ``stage_prefix`` prepended, so
    multi-layer dumps carry the layer name (e.g. ``layer-F.Cu-2-simplify``)
    while single-layer dumps use empty prefix (``2-simplify``).

    Called identically by single-layer and multi-layer branches.
    """
    pts = simplify_path(pts)
    _log_path(f"{stage_prefix}simplify", pts)
    _dump_viz(f"{stage_prefix}2-simplify", pts, pad_viz, obstacles, route_bbox)

    pts = shortcut_path(pts, obstacles, route_bbox, resolution=grid_res)
    _log_path(f"{stage_prefix}shortcut", pts)
    _dump_viz(f"{stage_prefix}3-shortcut", pts, pad_viz, obstacles, route_bbox)

    pts = snap_to_45_path_safe(pts, obstacles, route_bbox, resolution=grid_res)
    _log_path(f"{stage_prefix}snap45", pts)
    _dump_viz(f"{stage_prefix}4-snap45", pts, pad_viz, obstacles, route_bbox)

    return pts


def _align_single_endpoint(
    path: list[tuple[float, float]],
    pad_center: tuple[float, float],
    obstacles: list | None,
    bbox: tuple[float, float, float, float],
    resolution: float,
    pad_size: tuple[float, float],
    from_center: bool,
) -> list[tuple[float, float]]:
    """Align one endpoint to a pad centre via X→Y iterative translation.

    ``from_center=True`` aligns the start of *path*; ``False`` aligns
    the end.  See :func:`_align_path_endpoints` for the detailed algorithm.
    """
    if len(path) < 2:
        return path

    pcx, pcy = pad_center
    if from_center:
        # ── Start pad ───────────────────────────────────────────────
        dx = pcx - path[0][0]
        if abs(dx) > 1e-9:
            orig = list(path)
            k = 0
            while k < len(path) and abs(orig[k][0] - orig[0][0]) < 1e-9:
                path[k] = (path[k][0] + dx, path[k][1])
                k += 1
            while k < len(path):
                if abs(orig[k][1] - orig[k - 1][1]) < 1e-9:
                    break
                path[k] = (path[k][0] + dx, path[k][1])
                k += 1

        dy = pcy - path[0][1]
        if abs(dy) > 1e-9:
            orig = list(path)
            k = 0
            while k < len(path) and abs(orig[k][1] - orig[0][1]) < 1e-9:
                path[k] = (path[k][0], path[k][1] + dy)
                k += 1
            while k < len(path):
                if abs(orig[k][0] - orig[k - 1][0]) < 1e-9:
                    break
                path[k] = (path[k][0], path[k][1] + dy)
                k += 1
    else:
        # ── End pad ─────────────────────────────────────────────────
        dx = pcx - path[-1][0]
        if abs(dx) > 1e-9:
            orig = list(path)
            k = len(path) - 1
            while k >= 0 and abs(orig[k][0] - orig[-1][0]) < 1e-9:
                path[k] = (path[k][0] + dx, path[k][1])
                k -= 1
            while k >= 0:
                if abs(orig[k + 1][1] - orig[k][1]) < 1e-9:
                    break
                path[k] = (path[k][0] + dx, path[k][1])
                k -= 1

        dy = pcy - path[-1][1]
        if abs(dy) > 1e-9:
            orig = list(path)
            k = len(path) - 1
            while k >= 0 and abs(orig[k][1] - orig[-1][1]) < 1e-9:
                path[k] = (path[k][0], path[k][1] + dy)
                k -= 1
            while k >= 0:
                if abs(orig[k + 1][0] - orig[k][0]) < 1e-9:
                    break
                path[k] = (path[k][0], path[k][1] + dy)
                k -= 1

    return path


def _align_path_endpoints(
    path: list[tuple[float, float]],
    start_center: tuple[float, float],
    end_center: tuple[float, float],
    obstacles: list,
    bbox: tuple[float, float, float, float],
    resolution: float,
    pad_a_size: tuple[float, float] | None = None,
    pad_b_size: tuple[float, float] | None = None,
) -> list[tuple[float, float]]:
    """Align both endpoints to pad centres via X→Y translation.

    See :func:`_align_single_endpoint` for the per-endpoint algorithm.
    """
    if len(path) < 2:
        return path
    path = _align_single_endpoint(
        path,
        start_center,
        obstacles,
        bbox,
        resolution,
        pad_a_size or (1.0, 1.0),
        from_center=True,
    )
    path = _align_single_endpoint(
        path,
        end_center,
        obstacles,
        bbox,
        resolution,
        pad_b_size or (1.0, 1.0),
        from_center=False,
    )
    return path


def _replace_pad_path(
    path: list[tuple[float, float]],
    center: tuple[float, float],
    size: tuple[float, float],
    from_center: bool,
) -> list[tuple[float, float]]:
    """Drop the A* path inside a rectangular pad and replace it with a
    single axis-aligned wire.  Direction (horizontal vs vertical) is
    determined by which AABB edge the first outside point is on."""

    w, h = size
    cx, cy = center
    hw, hh = w / 2.0, h / 2.0
    minx, maxx = cx - hw, cx + hw
    miny, maxy = cy - hh, cy + hh

    if from_center:
        for k in range(1, len(path)):
            if not _inside_rect(path[k], center, hw, hh):
                fx, fy = path[k - 1]
                ox, oy = path[k]
                keep = _build_pad_wire(cx, cy, fx, fy, ox, oy, minx, maxx, miny, maxy)
                # keep = [projection, fence]
                return keep + path[k:]
        return path
    else:
        for k in range(len(path) - 2, -1, -1):
            if not _inside_rect(path[k], center, hw, hh):
                fx, fy = path[k + 1]
                ox, oy = path[k]
                # Same (center, fence) order — reverse so path reads [fence, projection].
                keep = _build_pad_wire(cx, cy, fx, fy, ox, oy, minx, maxx, miny, maxy)
                return path[: k + 1] + keep[::-1]
        return path


def _inside_rect(
    pt: tuple[float, float],
    center: tuple[float, float],
    hw: float,
    hh: float,
) -> bool:
    cx, cy = center
    return cx - hw <= pt[0] <= cx + hw and cy - hh <= pt[1] <= cy + hh


def _build_pad_wire(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    ox: float,
    oy: float,
    minx: float,
    maxx: float,
    miny: float,
    maxy: float,
) -> list[tuple[float, float]]:
    """Return ``[projection, fence]`` — a single axis-aligned segment
    along the AABB edge the outside point exits from.  The centre is
    kept by the caller (``_replace_pad_path``), and the whole chain
    is translated by ``_align_path_endpoints``."""

    if abs(x1 - x2) < 1e-6 or abs(y1 - y2) < 1e-6:
        return [(x1, y1), (x2, y2)]

    h_exit = ox <= minx or ox >= maxx
    v_exit = oy <= miny or oy >= maxy
    if h_exit and not v_exit:
        horizontal = True
    elif v_exit and not h_exit:
        horizontal = False
    else:
        horizontal = abs(ox - x1) >= abs(oy - y1)

    if horizontal:
        # fence on left/right edge → horizontal wire at fence_y
        return [(x1, y2), (x2, y2)]
    else:
        # fence on top/bottom edge → vertical wire at fence_x
        return [(x2, y1), (x2, y2)]


# ---------------------------------------------------------------------------
# Board-bounds check
# ---------------------------------------------------------------------------


def _check_segments_in_board(
    segs: list[OutputSegment],
    board_bbox: tuple[float, float, float, float],
) -> None:
    """Raise :class:`RouteFailure` if any segment leaves the Edge.Cuts AABB.

    The check is conservative: we test the segment endpoints plus a few
    interior points against the board polygon *shrunk* by the segment's
    own ``width / 2``, so the track's copper edge is what we verify.
    A track whose centerline is exactly on the boundary is allowed (its
    copper would still touch but not cross the edge); a track whose
    centerline is on the wrong side of the shrunk boundary fails.

    Args:
        segs: The segments produced by :func:`postprocess`.
        board_bbox: ``(minx, miny, maxx, maxy)`` from
            :func:`kcaa.router.world_model._board_bbox`.

    Raises:
        RouteFailure: The first segment that would leave the board.
    """
    from shapely.geometry import LineString, Polygon

    minx, miny, maxx, maxy = board_bbox
    if minx >= maxx or miny >= maxy:
        raise RouteFailure(
            f"Board bbox is degenerate ({board_bbox}); cannot verify "
            f"segments stay within the board."
        )

    for i, seg in enumerate(segs):
        # Shrink the board by half the track width so the segment's center
        # is checked against a region the copper itself must stay inside.
        shrink = seg.width / 2.0
        shrunk_bbox = (minx + shrink, miny + shrink, maxx - shrink, maxy - shrink)
        if shrunk_bbox[0] >= shrunk_bbox[2] or shrunk_bbox[1] >= shrunk_bbox[3]:
            raise RouteFailure(
                f"Track width {seg.width} mm is wider than the board "
                f"(shrunk bbox {shrunk_bbox} is degenerate)."
            )
        board_poly = Polygon(
            [
                (shrunk_bbox[0], shrunk_bbox[1]),
                (shrunk_bbox[2], shrunk_bbox[1]),
                (shrunk_bbox[2], shrunk_bbox[3]),
                (shrunk_bbox[0], shrunk_bbox[3]),
            ]
        )
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        if not board_poly.covers(line):
            raise RouteFailure(
                f"Segment {i} from ({seg.x1:.3f},{seg.y1:.3f}) to "
                f"({seg.x2:.3f},{seg.y2:.3f}) would extend outside the "
                f"Edge.Cuts boundary (board {board_bbox}, track width "
                f"{seg.width} mm)."
            )


def _check_vias_in_board(
    vias: list[OutputVia],
    board_bbox: tuple[float, float, float, float],
) -> None:
    """Raise :class:`RouteFailure` if any via would land outside the board.

    The via pad is a circle of radius ``diameter / 2``. To keep the entire
    circle inside the board, we check its center against the AABB shrunk
    by ``diameter / 2``.
    """
    minx, miny, maxx, maxy = board_bbox
    if minx >= maxx or miny >= maxy:
        raise RouteFailure(
            f"Board bbox is degenerate ({board_bbox}); cannot verify vias stay within the board."
        )
    for i, via in enumerate(vias):
        radius = via.diameter / 2.0
        shrunk_bbox = (minx + radius, miny + radius, maxx - radius, maxy - radius)
        if shrunk_bbox[0] >= shrunk_bbox[2] or shrunk_bbox[1] >= shrunk_bbox[3]:
            raise RouteFailure(
                f"Via diameter {via.diameter} mm is wider than the board "
                f"(shrunk bbox {shrunk_bbox} is degenerate)."
            )
        if not (shrunk_bbox[0] <= via.x <= shrunk_bbox[2]) or not (
            shrunk_bbox[1] <= via.y <= shrunk_bbox[3]
        ):
            raise RouteFailure(
                f"Via {i} at ({via.x:.3f},{via.y:.3f}) with diameter "
                f"{via.diameter} mm would extend outside the Edge.Cuts "
                f"boundary (board {board_bbox})."
            )


# ---------------------------------------------------------------------------
# Exit-point selection
# ---------------------------------------------------------------------------


def _routing_layers(req: RouteRequest) -> list[str]:
    """Return the ordered list of layers the router must consider.

    Includes ``start_layer`` and ``end_layer`` and every layer referenced
    by ``via_pairs``. Order is preserved with duplicates removed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for layer in (req.start_layer, req.end_layer):
        if layer not in seen:
            out.append(layer)
            seen.add(layer)
    for top, bot in req.via_pairs:
        for layer in (top, bot):
            if layer not in seen:
                out.append(layer)
                seen.add(layer)
    return out


def _layers_used(path: list) -> list[str]:
    """Return the ordered, deduplicated list of layers touched by ``path``."""
    seen: set[str] = set()
    out: list[str] = []
    for node in path:
        layer = getattr(node, "layer", None)
        if layer is not None and layer not in seen:
            out.append(layer)
            seen.add(layer)
    return out


# ---------------------------------------------------------------------------
# Pad lookup (parse the PCB tree directly)
# ---------------------------------------------------------------------------


def _pcb_layer_names(data: list) -> list[str]:
    """Return the ordered list of layer names declared in the PCB.

    Reads the PCB root's ``(layers (idx "name" type) ...)`` section and
    returns just the names in their declared order. Returns an empty list
    if the section is missing.
    """
    for item in data:
        if not _is_list(item) or str(item[0]) != "layers":
            continue
        names: list[str] = []
        for sub in item[1:]:
            if not _is_list(sub) or len(sub) < 2:
                continue
            v = sub[1]
            names.append(v if isinstance(v, str) else str(v))
        return names
    return []


def _find_pad_center(
    data: list,
    ref: str,
    pad_name: str,
) -> tuple[float, float] | None:
    """Return the (x, y) center of the named pad on the given footprint ref."""
    fp = _find_footprint(data, ref)
    if fp is None:
        return None
    fp_x, fp_y, fp_rot = _node_at3(fp)
    for sub in fp:
        if not _is_list(sub):
            continue
        if str(sub[0]) != "pad":
            continue
        name = _get_pad_name(sub)
        if name != pad_name:
            continue
        # Pad ``at`` is in footprint-local coords.
        at = _get_sub(sub, "at")
        if at is None or len(at) < 3:
            return None
        try:
            px, py = float(at[1]), float(at[2])
        except (TypeError, ValueError):
            return None
        # Transform local → world (only translation + rotation; pads don't
        # scale).
        wx, wy = _rotate(px, py, fp_rot)
        return fp_x + wx, fp_y + wy
    return None


def _find_pad_size(
    data: list,
    ref: str,
    pad_name: str,
    layer: str,
) -> tuple[float, float] | None:
    """Return the (w, h) of the pad shape, for the requested copper layer.

    Returns ``None`` if the pad is not on ``layer`` or not SMD (e.g. thru-hole
    pads are circular and need a different exit strategy).
    """
    fp = _find_footprint(data, ref)
    if fp is None:
        return None
    for sub in fp:
        if not _is_list(sub):
            continue
        if str(sub[0]) != "pad":
            continue
        if _get_pad_name(sub) != pad_name:
            continue
        if layer not in _pad_layers(sub):
            return None
        size_sub = _get_sub(sub, "size")
        if size_sub is None or len(size_sub) < 3:
            return None
        try:
            return float(size_sub[1]), float(size_sub[2])
        except (TypeError, ValueError):
            return None
    return None


def _fp_rotation(data: list, ref: str) -> float:
    """Return the footprint's rotation angle, or 0.0 if not found."""
    fp = _find_footprint(data, ref)
    if fp is None:
        return 0.0
    _, _, rot = _node_at3(fp)
    return rot


def _find_footprint(data: list, ref: str) -> list | None:
    for item in data:
        if not _is_list(item) or str(item[0]) != "footprint":
            continue
        for sub in item:
            if not _is_list(sub):
                continue
            if str(sub[0]) != "property":
                continue
            if len(sub) >= 3 and str(sub[1]) == "Reference":
                v = sub[2]
                val = v if isinstance(v, str) else str(v)
                if val == ref:
                    return item
    return None


def _get_pad_name(pad_node: list) -> str:
    if len(pad_node) >= 2:
        v = pad_node[1]
        return v if isinstance(v, str) else str(v)
    return ""


_ALL_COPPER = {"F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"}


def _pad_layers(pad_node: list) -> list[str]:
    """Return the list of layer names this pad is on, expanding ``*.Cu``."""
    layers: list[str] = []
    for sub in pad_node:
        if _is_list(sub) and str(sub[0]) == "layers" and len(sub) >= 2:
            for v in sub[1:]:
                name = v if isinstance(v, str) else str(v)
                if name == "*.Cu":
                    layers.extend(_ALL_COPPER)
                else:
                    layers.append(name)
    return layers


# ---------------------------------------------------------------------------
# DRC defaults (lightweight: read the netclass table from the .kicad_pro)
# ---------------------------------------------------------------------------


def _default_track_width(pcb_path: str, net: str) -> float:
    """Resolve track width for ``net`` from the project's netclass settings.

    Reads the matching ``.kicad_pro`` and looks up the netclass that
    ``net`` belongs to (via ``netclass_patterns``). Returns that netclass's
    ``track_width``.

    Raises:
        ProFileMissing: No ``.kicad_pro`` next to ``pcb_path`` — pass
            ``RouteRequest(width=...)`` explicitly to skip DRC lookup.
        ProFileMalformed: The ``.kicad_pro`` exists but cannot be parsed or
            lacks the expected structure.
        NetClassUnresolved: The net does not match any netclass pattern and
            there is no ``Default`` netclass to fall back to.
    """
    import json

    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        raise ProFileMissing(pcb_path)
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ProFileMalformed(pro_path, f"invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ProFileMalformed(pro_path, f"cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise ProFileMalformed(pro_path, "top-level JSON is not an object")
    nc_widths = _netclass_track_widths(data)
    assignments = _net_to_netclass(data)
    nc = _resolve_netclass(net, assignments)
    if nc is not None and nc in nc_widths:
        return nc_widths[nc]
    if "Default" in nc_widths:
        return nc_widths["Default"]
    raise NetClassUnresolved(net, pro_path)


def _default_clearance(pcb_path: str, net: str | None = None) -> float:
    """Resolve minimum clearance from the board's effective design rules.

    When the board's ``min_clearance`` is 0.0 (which is common — KiCad
    leaves it at 0 and relies on net class rules) this falls back to the
    clearance of the matching net class.  If no net class matches, uses
    the Default net class clearance (0.2 mm fallback).

    Raises:
        ProFileMissing: No ``.kicad_pro`` next to ``pcb_path``.
        DesignRulesUnavailable: Rules cannot be read; ``min_clearance`` is
            not set.
    """
    try:
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file
    except ImportError as exc:
        raise DesignRulesUnavailable(f"pcb_design_rules module not importable: {exc}") from exc
    try:
        rules = get_effective_design_rules_from_file(pcb_path)
    except Exception as exc:
        raise DesignRulesUnavailable(f"failed to read design rules from {pcb_path}: {exc}") from exc
    design_rules = rules.get("design_rules") if isinstance(rules, dict) else None
    if not isinstance(design_rules, dict):
        raise DesignRulesUnavailable("design rules response is missing the design_rules section")
    v = design_rules.get("min_clearance")
    if v is None:
        raise DesignRulesUnavailable("design rules do not contain min_clearance")
    try:
        clr = float(v)
    except (TypeError, ValueError) as exc:
        raise DesignRulesUnavailable(f"min_clearance is not numeric: {v!r}") from exc

    # When the board's global min_clearance is 0.0, fall back to
    # the net class clearance for the requested net.
    if clr < 0.001 and net is not None:
        net_clr = _netclass_clearance(pcb_path, net)
        if net_clr > 0.0:
            clr = net_clr
    return clr


def _project_file_for(pcb_path: str) -> str | None:
    import re

    base = os.path.splitext(os.path.basename(pcb_path))[0]
    d = os.path.dirname(pcb_path)
    if not base:
        return None
    for f in os.listdir(d):
        if f.startswith(base + ".") and re.match(r".+\.kicad_pro$", f):
            return os.path.join(d, f)
    return None


def _netclass_track_widths(data: dict) -> dict[str, float]:
    """Read netclass track widths from the JSON project file.

    Returns ``{netclass_name: track_width}``.
    """
    out: dict[str, float] = {}
    ns = data.get("net_settings", {}) if isinstance(data, dict) else {}
    classes = ns.get("classes", []) if isinstance(ns, dict) else []
    for c in classes:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        tw = c.get("track_width")
        if isinstance(name, str) and isinstance(tw, int | float):
            out[name] = float(tw)
    return out


def _netclass_clearances(data: dict) -> dict[str, float]:
    """Read netclass clearances from the JSON project file.

    Returns ``{netclass_name: clearance}``.
    """
    out: dict[str, float] = {}
    ns = data.get("net_settings", {}) if isinstance(data, dict) else {}
    classes = ns.get("classes", []) if isinstance(ns, dict) else []
    for c in classes:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        clr = c.get("clearance")
        if isinstance(name, str) and isinstance(clr, int | float):
            out[name] = float(clr)
    return out


def _netclass_clearance(pcb_path: str, net: str) -> float:
    """Resolve clearance for ``net`` from the project's netclass settings.

    Falls back to the Default netclass clearance (0.2 mm) if the net
    cannot be matched to a specific netclass.
    """
    import json

    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        return 0.2
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0.2
    if not isinstance(data, dict):
        return 0.2
    clrs = _netclass_clearances(data)
    assignments = _net_to_netclass(data)
    nc = _resolve_netclass(net, assignments)
    if nc is not None and nc in clrs:
        return clrs[nc]
    if "Default" in clrs:
        return clrs["Default"]
    return 0.2


def _default_via_params(data: dict, net: str) -> tuple[float, float]:
    """Read via_diameter and via_drill from ``net``'s netclass.

    Falls back to Default netclass, then ``(0.6, 0.3)``.
    """
    ns = data.get("net_settings", {}) if isinstance(data, dict) else {}
    classes = ns.get("classes", []) if isinstance(ns, dict) else []
    assignments = _net_to_netclass(data)
    nc_name = _resolve_netclass(net, assignments)

    # Read via params from all classes, indexed by name.
    via_params: dict[str, tuple[float, float]] = {}
    for c in classes:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str):
            continue
        vd = c.get("via_diameter")
        vr = c.get("via_drill")
        if vd is not None and vr is not None:
            try:
                via_params[name] = (float(vd), float(vr))
            except (TypeError, ValueError):
                continue

    if nc_name is not None and nc_name in via_params:
        return via_params[nc_name]
    if "Default" in via_params:
        return via_params["Default"]
    return 0.6, 0.3


def _resolve_via_diameter(pcb_path: str, net: str) -> float:
    """Resolve via diameter from ``net``'s netclass.

    If the ``.kicad_pro`` is missing or unreadable, returns 0.6 mm.
    """
    import json

    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        return 0.6
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        vd, _ = _default_via_params(data, net)
        return vd
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.6


def _resolve_via_drill(pcb_path: str, net: str) -> float:
    """Resolve via drill from ``net``'s netclass.

    If the ``.kicad_pro`` is missing or unreadable, returns 0.3 mm.
    """
    import json

    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        return 0.3
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        _, vr = _default_via_params(data, net)
        return vr
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.3
        return 0.3
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        _, vr = _default_via_params(data)
        return vr
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.3


def _net_to_netclass(data: dict) -> dict[str, str]:
    """Read net→netclass assignments from the JSON project file.

    KiCad's project file uses ``netclass_patterns`` with a ``pattern`` glob —
    a net belongs to the first matching pattern's netclass. This is a
    glob-style match (we use ``fnmatch`` for ``*`` and ``?`` wildcards).
    """

    out: dict[str, str] = {}
    ns = data.get("net_settings", {}) if isinstance(data, dict) else {}
    patterns = ns.get("netclass_patterns", []) if isinstance(ns, dict) else []
    # Build list of (pattern, netclass).
    pat_list: list[tuple[str, str]] = []
    for p in patterns:
        if not isinstance(p, dict):
            continue
        nc = p.get("netclass")
        pat = p.get("pattern")
        if isinstance(nc, str) and isinstance(pat, str):
            pat_list.append((pat, nc))
    # We also support an explicit "nets" table if present (newer KiCad).
    nets = ns.get("nets", []) if isinstance(ns, dict) else []
    for n in nets:
        if not isinstance(n, dict):
            continue
        name = n.get("name")
        nc = n.get("netclass") or n.get("class")
        if isinstance(name, str) and isinstance(nc, str):
            out[name] = nc
    # Resolve patterns into explicit per-net entries.
    # (Net names that are not in the explicit table are resolved here.)
    # The caller will look up by net name; for resolution we need to know
    # the set of net names — but for our purposes (looking up a single
    # net by name) the explicit table is enough. We expose the patterns
    # so a higher layer can resolve ambiguous names. For now, expose
    # ``out`` as the per-net map and also a fallback: if the net is not
    # in ``out``, the caller checks the patterns directly. To keep the
    # API simple, we store the pattern list globally in this function's
    # closure via a small cache on the returned dict.
    if pat_list:
        out.setdefault("__patterns__", None)  # sentinel
        out["__patterns__"] = pat_list  # type: ignore[assignment]
    return out


def _resolve_netclass(net: str, assignments: dict[str, str]) -> str | None:
    """Return the netclass for ``net`` (explicit assignment or pattern)."""
    if net in assignments and net != "__patterns__":
        return assignments[net]
    patterns = assignments.get("__patterns__")
    if patterns:
        import fnmatch

        for pat, nc in patterns:
            if fnmatch.fnmatchcase(net, pat):
                return nc
    return None


# ---------------------------------------------------------------------------
# Local S-expression helpers (mirror world_model.py style)
# ---------------------------------------------------------------------------


def _is_list(v) -> bool:
    return isinstance(v, list) and len(v) > 0


def _get_sub(node: list, tag: str):
    for sub in node:
        if _is_list(sub) and str(sub[0]) == tag:
            return sub
    return None


def _find_section(data: list, tag: str) -> list:
    """Return all subnodes whose head is ``tag``."""
    out = []
    for item in data:
        if _is_list(item) and str(item[0]) == tag:
            out.append(item)
    return out


def _node_at3(node: list) -> tuple[float, float, float]:
    sub = _get_sub(node, "at")
    if sub is None or len(sub) < 3:
        return 0.0, 0.0, 0.0
    try:
        x, y = float(sub[1]), float(sub[2])
        rot = float(sub[3]) if len(sub) >= 4 else 0.0
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    return x, y, rot


def _rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate (x, y) by ``deg`` (CW-positive on screen, matching KiCad's PCB convention).

    In KiCad's +Y-down world, a positive file rotation is clockwise on
    screen, which is equivalent to a math counter-clockwise rotation of
    -deg.  Substituting -deg into the standard CCW formula
    (cos(-d) = cos(d), sin(-d) = -sin(d)) gives the CW-on-screen form:

        x' =  x*cos(d) + y*sin(d)
        y' = -x*sin(d) + y*cos(d)

    This matches the formula used by
    :func:`kcaa.utils.pcb_board_utils.get_fp_courtyard_bbox` and other
    PCB geometry helpers in the codebase.
    """
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return c * x + s * y, -s * x + c * y
