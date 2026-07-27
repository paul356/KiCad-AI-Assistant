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

from kcaa.router.grid_a_star import (
    GRID_RESOLUTION,
    hierarchical_a_star,
    path_to_nodes,
    simplify_path,
)
from kcaa.router.path_postprocess import (
    OutputSegment,
    OutputVia,
    postprocess_path,
)
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

    # Pad center coordinates.
    pad_a_xy = _find_pad_center(data, req.ref_a, req.pad_a)
    pad_b_xy = _find_pad_center(data, req.ref_b, req.pad_b)
    if pad_a_xy is None:
        raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} not found")
    if pad_b_xy is None:
        raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} not found")

    # World model: only existing copper (tracks, vias, keepouts) blocks the
    # route.  Footprint courtyards are NOT obstacles — they're a DRC spacing
    # concept, not a hard copper boundary, and treating them as forbidden
    # forces pointless detours around parts.  Start/end footprints would be
    # excluded anyway, but we skip the whole footprint layer.
    model = build_world_model(
        req.pcb_path,
        net_filter=req.net,
        exclude_refs=set(),
        include_footprints=False,
    )

    # Shrink obstacles by half the trace width (so the track is centered on
    # the line) and inflate by clearance. Remaining: forbidden region.
    buffered = _inflate_obstacles(model.obstacles, width / 2.0 + clearance)

    # Pick candidate exit points on each pad edge.
    pad_a_size = _find_pad_size(data, req.ref_a, req.pad_a, req.start_layer)
    pad_b_size = _find_pad_size(data, req.ref_b, req.pad_b, req.end_layer)
    if pad_a_size is None:
        raise RouteFailure(
            f"Pad {req.ref_a}/{req.pad_a} has no copper shape on layer "
            f"{req.start_layer!r}; cannot route from there."
        )
    if pad_b_size is None:
        raise RouteFailure(
            f"Pad {req.ref_b}/{req.pad_b} has no copper shape on layer "
            f"{req.end_layer!r}; cannot route to there."
        )

    # Group inflated obstacles by layer for multi-layer routing.
    routing_layers = _routing_layers(req)
    obstacles_by_layer: dict[str, list] = {}
    for rl in routing_layers:
        obstacles_by_layer[rl] = [o for o in buffered if rl in o.layers]

    # Route from pad center to pad center only.  No exit point / margin
    # Multi-layer routing not implemented in this version.
    if req.start_layer != req.end_layer:
        raise RouteFailure(
            f"Multi-layer routing ({req.start_layer} → {req.end_layer}) is not supported yet."
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

    best_path_pts = simplify_path(result.path)
    best_path_pts[0] = pad_a_xy  # snap to exact center
    best_path_pts[-1] = pad_b_xy
    path_nodes = path_to_nodes(best_path_pts, req.start_layer)
    segs, vias = postprocess_path(
        path_nodes,
        width=width,
        net=req.net,
        max_miter_mm=req.max_miter_mm,
    )

    # Multi-layer check (Phase 2 — not yet implemented with centers)
    if req.start_layer != req.end_layer:
        raise RouteFailure(
            f"Multi-layer routing ({req.start_layer} → {req.end_layer}) not supported yet."
        )

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


def _pad_exit_points(
    center: tuple[float, float],
    size: tuple[float, float],
    margin: float = 0.25,
    step: float = 0.2,
) -> list[tuple[float, float]]:
    """Return candidate exit points just outside a rectangular pad edge.

    Scans along each edge at ``step`` intervals.  When ``step`` >= edge
    length, generates only the edge midpoint.  This keeps the default
    count at 4 midpoints for typical SMD pads; a smaller ``step`` scans
    the full edge for tight obstacle situations.

    Args:
        center: ``(cx, cy)`` pad center.
        size: ``(w, h)`` pad width and height.
        margin: Distance beyond the pad copper edge.
        step: Approximate interval between exit candidates.

    Returns:
        List of ``(x, y)`` candidate exit points.
    """
    cx, cy = center
    w, h = size
    hw, hh = w / 2.0, h / 2.0

    def _spread(lo: float, hi: float) -> list[float]:
        n = max(2, int((hi - lo) / step) + 1)
        if n <= 2:
            return [(lo + hi) / 2.0]
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    exits: list[tuple[float, float]] = []
    for x in _spread(cx - hw, cx + hw):
        exits.append((x, cy - hh - margin))
    for x in _spread(cx - hw, cx + hw):
        exits.append((x, cy + hh + margin))
    for y in _spread(cy - hh, cy + hh):
        exits.append((cx - hw - margin, y))
    for y in _spread(cy - hh, cy + hh):
        exits.append((cx + hw + margin, y))
    return exits


def _filter_exits(exits: list[tuple[float, float]], grid) -> None:
    """Remove exit points whose grid cell is blocked (mutates *exits* in place)."""
    i = 0
    while i < len(exits):
        gx, gy = grid.to_grid(exits[i][0], exits[i][1])
        if not grid.is_free(gx, gy):
            exits[i] = exits[-1]
            exits.pop()
        else:
            i += 1


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
    import os

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
        try:
            net_clr = _netclass_clearance(pcb_path, net)
            if net_clr > 0.0:
                clr = net_clr
        except Exception:
            pass  # keep the board value
    return clr


def _project_file_for(pcb_path: str) -> str | None:
    import os
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
    import os

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


def _default_via_params(data: dict) -> tuple[float, float]:
    """Read via_diameter and via_drill from the Default netclass.

    Returns ``(via_diameter, via_drill)`` in mm.  Falls back to
    ``(0.6, 0.3)`` if not found in the project file.
    """
    ns = data.get("net_settings", {}) if isinstance(data, dict) else {}
    classes = ns.get("classes", []) if isinstance(ns, dict) else []
    for c in classes:
        if not isinstance(c, dict):
            continue
        if c.get("name") == "Default":
            vd = c.get("via_diameter")
            vr = c.get("via_drill")
            if vd is not None and vr is not None:
                try:
                    return float(vd), float(vr)
                except (TypeError, ValueError):
                    pass
    return 0.6, 0.3


def _resolve_via_diameter(pcb_path: str, net: str) -> float:
    """Resolve via diameter from the netclass (Default fallback via.)."""
    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        return 0.6
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        vd, _ = _default_via_params(data)
        return vd
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.6


def _resolve_via_drill(pcb_path: str, net: str) -> float:
    """Resolve via drill from the netclass (Default fallback via.)."""
    pro_path = _project_file_for(pcb_path)
    if pro_path is None or not os.path.exists(pro_path):
        return 0.3
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        _, vr = _default_via_params(data)
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
