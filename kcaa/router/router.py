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
   edge for each end (axis-aligned first, 45 deg  if needed).
5. **Grid search + A\\***: build a walkability grid from inflated obstacles
   and run 8-direction A*.  Multi-layer routes insert via edges at legal
   (x, y) positions between layers in ``via_pairs``.
6. **Postprocess**: simplify collinear runs, miter corners, emit segments
   and vias at layer transitions.

Multi-layer routing
-------------------

When ``start_layer != end_layer`` the router builds one GridMap per
routing layer and runs a multi-layer A* that can insert via edges.
Via edges cost 2.0 mm (configurable) -- this penalises vias so A* prefers
routing on a single layer when possible.

With ``algorithm="pns"`` multi-layer routes decompose into one
walkaround + shove leg per layer (shortest layer path through
``via_pairs``), joined by through-vias placed along the direct
pad-to-pad line and DRC-validated (same-net pad faces, existing
copper, board edge, hole-to-hole).  A PNS leg emits rounded-corner
arcs (corner_mode ``rounded45``/``rounded90``, both opt-in — the
default ``mitered45`` stays all-straight) when the skeleton survived
walkaround/shove and the corner sits away from a via junction; legs
whose skeleton was disturbed, or whose fillet would end on a via, fall
back to straight segments.  Via junctions themselves stay
straight-through connections.

No shove
--------

The A* planner is the **no-shove** variant: if a route is blocked it
raises :class:`RouteFailure` rather than displacing existing tracks.
That is enough for ~80% of "connect A to B" requests; the remaining
cases need the user to move a track or add a via first -- or hand the
request to the ``pns`` engine, which walks around fixed solids and
shoves movable tracks of other nets instead of failing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import logging
import math
import os
import tempfile
import time

from shapely.geometry import LineString, Point, Polygon
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
    OutputArc,
    OutputSegment,
    OutputVia,
    postprocess_path,
)
from kcaa.router.pns.direction45 import CornerMode
from kcaa.router.pns.shove import TrackObstacle
from kcaa.router.route_engine import PnsFailure, _audit_final_copper, route_engine
from kcaa.router.via_check import ProposedVia, check_vias
from kcaa.router.visibility_graph import RouteNode
from kcaa.router.world_model import Obstacle, _get_net, build_world_model
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

    Layer selection is automatic: the router inspects each pad's type and
    copper layers.  For SMD/connect pads the layer is fixed by the pad
    itself.  For thru-hole pads (``*.Cu``) the router picks the best
    shared copper layer, preferring ``layer_hint`` when it is valid.

    Attributes:
        pcb_path: Absolute path to the ``.kicad_pcb`` file.
        ref_a / pad_a: Reference designator and pad number for one end.
        ref_b / pad_b: Reference designator and pad number for the other end.
        net: Net name shared by both pads.
        layer_hint: Preferred copper layer for thru-hole pads.  When
            ``None`` (default) the router picks the best layer
            automatically.  Ignored for SMD pads whose layer is fixed.
        via_pairs: Allowed (top, bottom) layer pairs that may carry a
            through-via. Default is ``(("F.Cu", "B.Cu"),)``. Pass an
            explicit tuple to restrict transitions (e.g. to forbid inner-
            layer vias on a 4-layer board).
        width: Track width; ``None`` -> resolve from netclass.
        clearance: Minimum clearance to obstacles; ``None`` -> resolve from
            the board's design rules.
        via_diameter / via_drill: Through-via dimensions; ``None`` ->
            resolve from netclass.
        max_miter_mm: Maximum corner miter extension before falling back
            to a sharp 90 deg  corner.
        grid_resolution: Grid cell size in mm for the walkability grid.
            Smaller values give finer paths but more cells.  ``None``
            uses the default (0.025 mm).
        via_cost: Distance-equivalent penalty for taking a via edge in
            multi-layer A*.  Higher values discourage unnecessary stack
            vias.  Default 2.0 mm.
        turn_penalty: Distance-equivalent cost added when the path
            changes direction.  0 disables (pure shortest path).
            Default 0.3 mm ~ 3 cells at 0.1 mm resolution.
        algorithm: Routing algorithm to use: ``pns`` (walkaround + shove
            engine) or ``astar`` (grid-based A*).  Defaults to ``astar``
            here; the tool layer picks ``pns`` for vision-capable models
            unless an explicit value is given.  A single route always
            uses exactly one algorithm.
        waypoints: Anchor-chain control surface for the ``pns`` algorithm
            (ignored by ``astar``); each entry is a dict with a ``kind``:
            ``"waypoint"`` (``pos``, optional ``tol_mm``) forces the route
            through a soft pass-through point on the current leg layer.
            With ``tol_mm > 0`` the leg endpoint floats to a DRC-clean
            spot inside the ``tol_mm``-radius circle around ``pos`` (soft
            anchor), so the per-leg fillet arcs are no longer pinned onto
            the waypoint joint and the corner renders rounded; omitting
            ``tol_mm`` (or 0) keeps the exact anchor -- the chain is
            pinned through the waypoint and the whole-chain tangent
            fillet rounds the joint in place (rounded corner modes).
            Unreachable waypoints are recorded in
            ``RouteResult.violated_waypoints`` and skipped, the route
            continues; ``"via"`` (``pos``, ``to_layer``) switches the leg
            layer at a DRC-validated through-via site near ``pos``
            (micro-shifted within ``tol_mm``).  Any other kind (incl.
            ``"pad"``) is rejected with ``RouteFailure`` "unsupported
            anchor kind".  Waypoints consume one leg each: N waypoints
            split the route into N+1 legs.
        dry_run: Route and return the result without writing anything to
            the PCB file.  The router never writes; this flag lets the
            tool layer skip its ``save_pcb`` step.
        strategy: Explicit PNS shove-mode knob (trailing request field):
            ``"shove"`` (default — walkaround + shove with the default
            depth), ``"walkaround"`` (no movable push at all — foreign
            tracks are treated as fixed obstacles and the route detours
            around them).  The A* planner has no shove stage and ignores
            the value (the value itself is still validated).
    """

    pcb_path: str
    ref_a: str
    pad_a: str
    ref_b: str
    pad_b: str
    net: str
    layer_hint: str | None = None
    via_pairs: tuple[tuple[str, str], ...] = (("F.Cu", "B.Cu"),)
    width: float | None = None  # if None, use DRC default for the net
    clearance: float | None = None
    via_diameter: float | None = None
    via_drill: float | None = None
    max_miter_mm: float = 1.0
    grid_resolution: float | None = None  # None -> GRID_RESOLUTION
    via_cost: float = 2.0  # mm penalty per via edge
    turn_penalty: float = 0.3  # mm penalty per direction change; 0 disables
    algorithm: str = "astar"  # astar (grid A*) | pns (walkaround + shove); tool layer picks pns for vision models
    corner_mode: str = "mitered45"  # mitered45 (default) | rounded45 | rounded90 | mitered90
    waypoints: list[dict] = field(default_factory=list)
    dry_run: bool = False  # tool-layer hint: skip save_pcb (router never writes)
    strategy: str = "shove"  # shove | walkaround (PNS shove-mode knob)


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
        algorithm: The routing algorithm that produced this route.
        waypoint_violated: True when at least one waypoint anchor was
            unreachable and got skipped (the route still reached pad_b).
        violated_waypoints: (x, y) of every skipped waypoint anchor.
        via_sites: Emitted via sites in request order: one dict per
            explicit via waypoint with ``{"pos": [x, y], "to_layer": ...}``
            (the DRC-clean site actually used, possibly micro-shifted).
        strategy: Echo of the requested strategy knob.
        route_png: Path of a best-effort rendered image of the routed
            track (single route; `None` if rendering failed).
    """

    segments: list[OutputSegment] = field(default_factory=list)
    vias: list[OutputVia] = field(default_factory=list)
    arcs: list[OutputArc] = field(default_factory=list)
    shoved_tracks: list[TrackObstacle] = field(default_factory=list)
    corner_mode: str = "mitered45"
    algorithm: str = "astar"
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    layers_used: list[str] = field(default_factory=list)
    waypoint_violated: bool = False
    violated_waypoints: list[tuple[float, float]] = field(default_factory=list)
    via_sites: list[dict] = field(default_factory=list)
    strategy: str = "shove"  # echo of the requested strategy knob
    route_png: str | None = None  # best-effort single-route render path
    # (original, displaced) shove pairs: the pre-shove track as it exists
    # in the PCB file and the pushed replacement.  The tool layer deletes
    # the original file segment(s) and writes the displaced polyline.
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_route_pair(req: RouteRequest) -> RouteResult:
    """Connect pad ``req.pad_a`` on ``req.ref_a`` to pad ``req.pad_b`` on
    ``req.ref_b`` on the same net ``req.net``.

    Layers are auto-resolved from pad types: SMD pads use their fixed
    layer; thru-hole pads use a shared copper layer (preferring
    ``req.layer_hint``).

    Returns:
        A :class:`RouteResult` containing the segments and (optionally) vias.

    Raises:
        RouteFailure: If no path is found or inputs are invalid.
    """
    data = load_pcb(req.pcb_path)

    # Validate the routing algorithm selector early.
    if req.algorithm not in ("astar", "pns"):
        raise RouteFailure(
            f"algorithm={req.algorithm!r} is invalid; supported values are "
            f"'pns' (walkaround + shove) and 'astar' (grid-based)."
        )

    # Waypoints are the PNS waypoint-chain control surface; the A* planner
    # must not silently ignore them.
    if req.waypoints and req.algorithm != "pns":
        raise RouteFailure(
            f"waypoints are only supported with algorithm='pns' (got "
            f"algorithm={req.algorithm!r}); route {req.ref_a}/{req.pad_a} -> "
            f"{req.ref_b}/{req.pad_b} needs to drop the waypoints or use the "
            "PNS engine"
        )

    # Strategy is the explicit PNS shove-mode knob; validate the VALUE for
    # both planners (A* has no shove stage and silently ignores it).
    if req.strategy not in ("shove", "walkaround"):
        raise RouteFailure(
            f"strategy={req.strategy!r} is invalid; supported values are 'shove' or 'walkaround'."
        )

    # Validate corner_mode early: both planners must reject an unknown
    # value even though only the PNS engine renders arcs.
    corner_mode = _parse_corner_mode(req.corner_mode)

    # Validate via_pairs against the PCB layers early.
    pcb_layers = _pcb_layer_names(data)
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

    # Auto-resolve start/end layers from pad types + layer_hint.
    start_layer, end_layer = _resolve_layers(data, req)
    for layer in (start_layer, end_layer):
        if layer not in pcb_layers:
            raise RouteFailure(
                f"Layer {layer!r} is not present in PCB {req.pcb_path}; "
                f"PCB layers are {pcb_layers}."
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

    # Pad center coordinates. The layer keeps center and size on the SAME
    # pad when a footprint declares several pads with one name.
    pad_a_xy = _find_pad_center(data, req.ref_a, req.pad_a, start_layer)
    pad_b_xy = _find_pad_center(data, req.ref_b, req.pad_b, end_layer)
    if pad_a_xy is None:
        if _find_pad_center(data, req.ref_a, req.pad_a) is None:
            raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} not found")
        raise RouteFailure(
            f"Pad {req.ref_a}/{req.pad_a} has no copper shape on layer "
            f"{start_layer!r}; cannot route from there."
        )
    if pad_b_xy is None:
        if _find_pad_center(data, req.ref_b, req.pad_b) is None:
            raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} not found")
        raise RouteFailure(
            f"Pad {req.ref_b}/{req.pad_b} has no copper shape on layer "
            f"{end_layer!r}; cannot route to there."
        )

    # Validate pads exist on the resolved copper layers before doing
    # anything else (gives a clear error, not a confusing A* failure).
    if _find_pad_size(data, req.ref_a, req.pad_a, start_layer) is None:
        raise RouteFailure(
            f"Pad {req.ref_a}/{req.pad_a} has no copper shape on layer "
            f"{start_layer!r}; cannot route from there."
        )
    if _find_pad_size(data, req.ref_b, req.pad_b, end_layer) is None:
        raise RouteFailure(
            f"Pad {req.ref_b}/{req.pad_b} has no copper shape on layer "
            f"{end_layer!r}; cannot route to there."
        )

    # Early net check: pads on different nets must not be routed together.
    # A foreign-net pad is an obstacle -- past the net check the world model
    # would dutifully treat it as one and fail deep inside A*, so fail fast
    # with the actual cause.
    for ref, pad, layer in (
        (req.ref_a, req.pad_a, start_layer),
        (req.ref_b, req.pad_b, end_layer),
    ):
        pad_net = _find_pad_net(data, ref, pad, layer)
        if pad_net is not None and pad_net != req.net:
            raise RouteFailure(
                f"Pad {ref}/{pad} is on net {pad_net!r}, not the requested "
                f"net {req.net!r}; cannot route between different nets."
            )

    # World model: only existing copper (tracks, vias, keepouts) blocks the
    # route.  Footprint courtyards are NOT obstacles -- they're a DRC spacing
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
    routing_layers = _routing_layers(req, start_layer, end_layer)
    obstacles_by_layer: dict[str, list] = {}
    for rl in routing_layers:
        obstacles_by_layer[rl] = [o for o in buffered if rl in o.layers]

    # Detect rectangular pads to replace A* path inside them.
    # A pad is "rectangular" when its width and height differ by >= 20%.
    def _is_rect(size: tuple[float, float] | None) -> bool:
        if size is None:
            return False
        w, h = size
        return w > 0 and h > 0 and abs(w - h) / max(w, h) >= 0.2

    def _world_size(local_w: float, local_h: float, fp_rot: float) -> tuple[float, float]:
        """Return (world_w, world_h) accounting for +/-90 degree footprint rotation."""
        if abs(fp_rot % 180.0 - 90.0) < 0.1:
            return local_h, local_w
        return local_w, local_h

    pad_a_size = _find_pad_size(data, req.ref_a, req.pad_a, start_layer)
    pad_b_size = _find_pad_size(data, req.ref_b, req.pad_b, end_layer)
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
    if start_layer != end_layer:
        if pad_a_world_size is not None:
            obstacles_by_layer[start_layer] = _subtract_pad_aabb(
                obstacles_by_layer[start_layer],
                pad_a_xy,
                pad_a_world_size,
            )
        if pad_b_world_size is not None:
            obstacles_by_layer[end_layer] = _subtract_pad_aabb(
                obstacles_by_layer[end_layer],
                pad_b_xy,
                pad_b_world_size,
            )

    # The world model drops same-net pad copper entirely (the route must
    # land on its own endpoint pads).  Re-add every same-net pad as a
    # transit obstacle on the copper layers it covers so the track never
    # overlaps other same-net pad copper -- it terminates on the endpoint
    # pad only and connects to other same-net pads by separate tracks
    # later.  Buffer by ``width / 2 + clearance``: the track edge must
    # keep a clearance gap from the pad copper, same as for any other
    # obstacle.  If this makes the route impossible at the requested
    # width, that is the correct answer -- the user should try a
    # narrower track.  The two endpoint pads are exempt on their own
    # terminal layers so the track can start/end at their centres.
    pad_half = width / 2.0 + clearance
    _sn_shapes: list = []
    for poly, players, _ref, _pname, center in _same_net_pad_polygons(data, req.net):
        is_end = (abs(center[0] - pad_a_xy[0]) < 1e-6 and abs(center[1] - pad_a_xy[1]) < 1e-6) or (
            abs(center[0] - pad_b_xy[0]) < 1e-6 and abs(center[1] - pad_b_xy[1]) < 1e-6
        )
        buf = poly.buffer(pad_half)
        if buf.is_empty or not buf.is_valid:
            continue
        _sn_shapes.append(buf)
        for layer in players:
            if layer not in obstacles_by_layer:
                continue
            if is_end and layer in (start_layer, end_layer):
                continue
            obstacles_by_layer[layer].append(
                Obstacle(
                    shape=buf,
                    layers=frozenset({layer}),
                    net=req.net,
                    kind="pad",
                )
            )

    # Route bounding box must cover the endpoint pads and every same-net
    # pad obstacle: a detour around a wide pad cluster can extend past
    # the +/-5 mm endpoint margin.
    _hb = [s.bounds for s in _sn_shapes]
    _hx0 = min((b[0] for b in _hb), default=pad_a_xy[0])
    _hy0 = min((b[1] for b in _hb), default=pad_a_xy[1])
    _hx1 = max((b[2] for b in _hb), default=pad_a_xy[0])
    _hy1 = max((b[3] for b in _hb), default=pad_b_xy[1])
    route_bbox = (
        min(pad_a_xy[0], pad_b_xy[0], _hx0) - 5.0,
        min(pad_a_xy[1], pad_b_xy[1], _hy0) - 5.0,
        max(pad_a_xy[0], pad_b_xy[0], _hx1) + 5.0,
        max(pad_a_xy[1], pad_b_xy[1], _hy1) + 5.0,
    )
    # The A* search area is clipped to route_bbox; an obstacle that spans
    # the whole box (e.g. an Edge.Cuts opening the two pads sit on either
    # side of) then looks like an impassable wall even though a detour
    # around it exists outside the box.  Extend the box to cover the
    # bounds of every obstacle that intersects it (iterated to closure so
    # obstacles discovered on the second ring are included too), giving
    # the search enough room to route around them.
    _routing = set(routing_layers)
    _x0, _y0, _x1, _y1 = route_bbox
    _grew = True
    while _grew:
        _grew = False
        for _o in buffered:
            if not (_routing & _o.layers):
                continue
            _b = _o.shape.bounds
            if _b[2] < _x0 or _b[0] > _x1 or _b[3] < _y0 or _b[1] > _y1:
                continue  # no overlap with the current search box
            _nx0, _ny0, _nx1, _ny1 = (
                min(_x0, _b[0]),
                min(_y0, _b[1]),
                max(_x1, _b[2]),
                max(_y1, _b[3]),
            )
            if (_nx0, _ny0, _nx1, _ny1) != (_x0, _y0, _x1, _y1):
                _x0, _y0, _x1, _y1 = _nx0, _ny0, _nx1, _ny1
                _grew = True
    route_bbox = (_x0, _y0, _x1, _y1)
    grid_res = req.grid_resolution or GRID_RESOLUTION

    # Board-outline context for A* failure messages (empty when the board
    # has no Edge.Cuts outline).  The same bbox fences the A* search so it
    # cannot step outside the board.
    _board_note = f" within board {model.board_bbox}" if model.board_bbox is not None else ""

    # ---- A* directly from pad centre to pad centre ----
    _ax, _ay = pad_a_xy
    _bx, _by = pad_b_xy
    print(
        f"  [route] {req.ref_a}/{req.pad_a} ({_ax:.3f},{_ay:.3f})"
        f" -> {req.ref_b}/{req.pad_b} ({_bx:.3f},{_by:.3f})"
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

    # Anchor-chain results; populated by the PNS waypoints path and echoed
    # back as defaults everywhere else.
    waypoint_violated = False
    violated_waypoints: list[tuple[float, float]] = []
    via_sites: list[dict] = []
    # Anchor chain actually used, for the success render (A* has none):
    # the PNS waypoints path overwrites this with the real chain.
    used_chain: list[tuple[float, float]] = [pad_a_xy]
    route_png: str | None = None  # best-effort render of the routed track
    # (original, displaced) shove pairs collected from the engine; the
    # write path persists them (A* has no shove -> stays empty).
    moved_pairs: list[tuple[TrackObstacle, TrackObstacle]] = []

    if req.algorithm == "astar":
        # -- Grid A* ---------------------------------------------
        if start_layer != end_layer:
            # -- Multi-layer: grid A* with via edges ------------------
            # Via-forbidden zones cover EVERY same-net pad, not just the two
            # endpoint pads: a via on any same-net pad face is a DFM defect
            # (solder wicking, annular-ring breakout).  Same-net pads are
            # absent from the obstacle grid (the route must land on its own
            # pads), so without this the via search would happily drop a via
            # on a finger pad.
            via_forbidden: list = []
            for poly, _players, _ref, _pname, _center in _same_net_pad_polygons(data, req.net):
                via_forbidden.append(poly)
            ml_result = multi_layer_a_star(
                obstacles_by_layer,
                pad_a_xy,
                pad_b_xy,
                start_layer,
                end_layer,
                req.via_pairs,
                route_bbox,
                grid_res,
                via_cost=req.via_cost,
                via_forbidden_zones=via_forbidden or None,
                turn_penalty=req.turn_penalty,
                fence_bbox=model.board_bbox,
            )
            if ml_result.path is None:
                # Dump the failure state so the blockage can be inspected.
                _dump_viz("fail-multi-astar", [], _pad_viz, buffered, route_bbox)
                msg = (
                    f"No obstacle-avoiding multi-layer path from "
                    f"{req.ref_a}/{req.pad_a} to {req.ref_b}/{req.pad_b} at "
                    f"{width}mm track width ({start_layer} -> {end_layer})"
                    f"{_board_note}."
                )
                png = _render_route_failure_evidence(
                    req.pcb_path,
                    chain=[pad_a_xy],
                    attempted_end=pad_b_xy,
                    layer=start_layer,
                    obstacles=obstacles_by_layer[start_layer],
                )
                if png:
                    msg += f"\nFailure evidence: {png}"
                raise RouteFailure(msg)
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

                # Simplify -> shortcut -> snap45.
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

                # The alignment step can snap the endpoint into a
                # sub-width tap-in (a leg shorter than the track width) —
                # merge such stubs away before the final audit.
                pts = _drop_subwidth_points(pts, width, clearance, obs, req.net)
                _dump_viz(f"{prefix}-7-no-subwidth", pts, _pad_viz, obs, route_bbox)

                # No adjustment escapes DRC: re-audit this layer's final
                # polyline after pad replacement + alignment (the A*
                # grid check ran before these steps, on cell data).
                _final_path_drc(
                    pts,
                    width,
                    clearance,
                    req.net,
                    [o for o in model.obstacles if layer in o.layers],
                    [],
                    [],
                    set(),
                    req,
                    layer,
                    _pad_viz,
                    obs,
                    route_bbox,
                )

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

            # Multi-layer routing emits straight segments only; rounded
            # corner arcs are a single-layer skeleton feature.
            arcs_out: list[OutputArc] = []
            pushed: list[TrackObstacle] = []

            start_xy = (all_nodes[0].x, all_nodes[0].y)
            end_xy = (all_nodes[-1].x, all_nodes[-1].y)
            layers_used = _layers_used(all_nodes)
        else:
            # -- Single-layer: hierarchical A* ------------------------
            # Only copper on the routing layer can block the track; other
            # layers are parallel physical planes and must not poison the
            # grid (the multi-layer branch groups by layer for the same
            # reason).
            layer_obstacles = obstacles_by_layer[start_layer]
            result = hierarchical_a_star(
                layer_obstacles,
                pad_a_xy,
                pad_b_xy,
                fine_resolution=grid_res,
                route_bbox=route_bbox,
                turn_penalty=req.turn_penalty,
                fence_bbox=model.board_bbox,
            )
            if result.path is None:
                _dump_viz("fail-astar", [], _pad_viz, layer_obstacles, route_bbox)
                msg = (
                    f"No obstacle-avoiding path from {req.ref_a}/{req.pad_a} to "
                    f"{req.ref_b}/{req.pad_b} at {width}mm track width on layer "
                    f"{start_layer}{_board_note}."
                )
                png = _render_route_failure_evidence(
                    req.pcb_path,
                    chain=[pad_a_xy],
                    attempted_end=pad_b_xy,
                    layer=start_layer,
                    obstacles=layer_obstacles,
                )
                if png:
                    msg += f"\nFailure evidence: {png}"
                raise RouteFailure(msg)
            print(
                f"  [route] single-layer A*: {len(result.path)} pts"
                f"  cells_visited={result.cells_visited}"
            )

            # Strip layer_idx from the unified A* result.
            raw_pts = [(x, y) for x, y, _ in result.path]
            _dump_viz("0-astar", raw_pts, _pad_viz, buffered, route_bbox)

            # ---- Discard A* path inside rectangular pads and replace
            #      with axis-aligned wire (fence -> centre). ----
            best_path_pts = raw_pts
            if pad_a_size is not None:
                n_before = len(best_path_pts)
                best_path_pts = _replace_pad_path(
                    best_path_pts, pad_a_xy, pad_a_size, from_center=True
                )
                _log_path("pad_a-replace", best_path_pts, n_before)
            if pad_b_size is not None:
                n_before = len(best_path_pts)
                best_path_pts = _replace_pad_path(
                    best_path_pts, pad_b_xy, pad_b_size, from_center=False
                )
                _log_path("pad_b-replace", best_path_pts, n_before)
            _dump_viz("1-pad-replace", best_path_pts, _pad_viz, buffered, route_bbox)

            # ---- Post-process: simplify -> shortcut -> snap45 ----
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

            # The alignment step can snap the endpoint into a sub-width
            # tap-in (a leg shorter than the track width) — merge such
            # stubs away before the final audit.
            best_path_pts = _drop_subwidth_points(
                best_path_pts,
                width,
                clearance,
                [o for o in model.obstacles if start_layer in o.layers],
                req.net,
            )
            _dump_viz("7-no-subwidth", best_path_pts, _pad_viz, buffered, route_bbox)

            # No adjustment escapes DRC: re-audit the final polyline
            # after pad replacement + alignment (which the engine audit,
            # if any, ran before these steps).
            _final_path_drc(
                best_path_pts,
                width,
                clearance,
                req.net,
                [o for o in model.obstacles if start_layer in o.layers],
                [],
                [],
                set(),
                req,
                start_layer,
                _pad_viz,
                buffered,
                route_bbox,
            )

            path_nodes = path_to_nodes(best_path_pts, start_layer)
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

            # Single-layer A* emits straight segments only; rounded
            # corner arcs are a PNS-skeleton feature.
            arcs_out: list[OutputArc] = []
            pushed: list[TrackObstacle] = []

            start_xy = (path_nodes[0].x, path_nodes[0].y)
            end_xy = (path_nodes[-1].x, path_nodes[-1].y)
            layers_used = _layers_used(path_nodes)
    else:
        # -- PNS engine (walkaround + shove) ----------------------------
        # Only copper on the routing layer can block the track; other
        # layers are parallel physical planes and must not poison the
        # engine.  Every same-net pad polygon becomes a transit obstacle
        # the route may overlay (it is already connected).  The engine
        # walks the BuildInitialTrace skeleton around fixed solids, then
        # shoves movable tracks of other nets; it never uses the grid.
        # Single-layer routes may emit rounded-corner arcs; multi-layer
        # routes decompose into per-layer legs joined by through-vias
        # placed along the direct pad-to-pad line (see below).
        #
        # -- Per-leg machinery shared by the multi-layer path and the
        #    anchor-chain path below (a leg is a leg in both): run_leg
        #    routes one skeleton pair and feeds the shared node pipeline;
        #    finalize_legs postprocesses the emitted nodes into the final
        #    segment/arc/via lists.
        # -- Strategy knob (trailing request field): walkaround = no
        #    movable push (0 depth), shove = default MAX_SHOVE_DEPTH.
        shove_depth: float | None = 0 if req.strategy == "walkaround" else None
        used_chain: list[tuple[float, float]] = [pad_a_xy]
        all_nodes: list[RouteNode] = []
        node_id = 0
        pushed = []
        # Multi-leg shove safety: every leg's final polyline (and every
        # through-via site) is handed to the LATER legs' shove stage as
        # fixed copper — same net as the route (the route may touch it),
        # foreign to every shoved track.  Without this, a track displaced
        # by leg k can be re-landed onto leg 1..k-1 copper by a later leg,
        # or the write path would fork the track (see _collapse_moved_pairs).
        prev_leg_polylines: list[tuple[str, list[tuple[float, float]]]] = []
        # Per-leg direct emission: None -> the leg keeps the postprocess
        # (mitered straight) output below; otherwise the skeleton
        # segments / arcs emitted straight from eng.trace.
        direct_segs: list[list[OutputSegment] | None] = []
        direct_arcs: list[list[OutputArc] | None] = []
        leg_layers: list[str] = []
        # Arc joints: a rounded skeleton fillet never ends on a via/waypoint
        # joint (the joint is rounded afterwards by the chain tangent fillet,
        # which needs clean segment/segment joins).  Via anchors additionally
        # stay straight-through (KiCad behavior): never filleted.
        via_anchors: set[tuple[float, float]] = set()
        fillet_excluded: set[tuple[float, float]] = set()

        def run_leg(
            start_pt: tuple[float, float],
            end_pt: tuple[float, float],
            layer: str,
            *,
            li: int,
            n_legs: int,
            render_evidence: bool = False,
            evidence_ctx: dict | None = None,
        ) -> None:
            """Route one PNS leg (walkaround + shove) and feed its nodes
            into the shared postprocess pipeline.  ``li``/``n_legs`` are
            only used in diagnostic messages; the ``shove_depth`` cell
            (set from the trailing ``strategy`` knob: 0 = walkaround-only,
            None = default shove depth) selects the engine variant."""
            nonlocal node_id
            leg_layers.append(layer)
            if math.hypot(end_pt[0] - start_pt[0], end_pt[1] - start_pt[1]) < 1e-6:
                raise RouteFailure(
                    f"PNS leg {li + 1}/{n_legs} on {layer} is too deep for the "
                    f"pad span ({pad_a_xy} -> {pad_b_xy}); reduce via "
                    "transitions or split the route with waypoints."
                )
            engine_obstacles = _layer_engine_obstacles(
                model,
                data,
                req,
                start_layer,
                end_layer,
                layer,
                pad_a_xy,
                pad_b_xy,
            )
            # Earlier legs' copper is fixed for the shove stage of this
            # leg (buffered by the route half-width, mirroring track
            # obstacle shapes).  Same-net to the route — the route walks
            # right over it — but foreign to every shoved track, which
            # must keep full DRC margin from it.
            extra_fixed: list[Obstacle] = [
                Obstacle(
                    shape=LineString(pts).buffer(width / 2.0, cap_style="round"),
                    layers=frozenset({layer}),
                    net=req.net,
                    kind="track",
                )
                for prev_layer, pts in prev_leg_polylines
                if prev_layer == layer and len(pts) >= 2
            ]
            for va in via_anchors:
                extra_fixed.append(
                    Obstacle(
                        shape=Point(va[0], va[1]).buffer(via_diameter / 2.0),
                        layers=frozenset({layer}),
                        net=req.net,
                        kind="via",
                    )
                )
            try:
                eng = route_engine(
                    start_pt,
                    end_pt,
                    engine_obstacles,
                    track_width=width,
                    clearance=clearance,
                    corner_mode=corner_mode,
                    max_shove_depth=shove_depth,
                    extra_fixed=extra_fixed,
                    net=req.net,
                )
            except PnsFailure as exc:
                _dump_viz(
                    "fail-pns",
                    exc.last_path or [],
                    _pad_viz,
                    buffered,
                    route_bbox,
                    shoved=[
                        {"net": orig.net, "from": orig.points, "to": disp.points}
                        for orig, disp in exc.shoved_pairs
                    ]
                    if exc.shoved_pairs
                    else None,
                )
                msg = (
                    f"No obstacle-avoiding path from {req.ref_a}/{req.pad_a} to "
                    f"{req.ref_b}/{req.pad_b} at {width}mm track width on layer "
                    f"{layer} (leg {li + 1}/{n_legs}): {exc}"
                )
                if render_evidence and evidence_ctx:
                    png = _render_route_failure_evidence(
                        req.pcb_path,
                        chain=evidence_ctx["chain"],
                        attempted_end=end_pt,
                        layer=layer,
                        obstacles=engine_obstacles,
                    )
                    if png:
                        msg += f"\nFailure evidence: {png}"
                raise RouteFailure(msg) from exc
            print(
                f"  [route] PNS leg {li + 1}/{n_legs} on {layer}: "
                f"{len(eng.path)} pts  shoved={len(eng.shoved_tracks)}"
            )
            pushed.extend(eng.shoved_tracks)
            moved_pairs.extend(eng.moved_pairs)
            pts = list(eng.path)
            if li == 0 and pad_a_size is not None:
                pts = _replace_pad_path(pts, pad_a_xy, pad_a_size, from_center=True)
            elif li == n_legs - 1 and pad_b_size is not None:
                pts = _replace_pad_path(pts, pad_b_xy, pad_b_size, from_center=False)

            # Per-leg rounded-corner arcs.  A leg emits its skeleton
            # fillets + straight legs straight from the trace — skipping
            # the postprocess miter, which would cut into the arc ends —
            # when walkaround/shove and the pad cleanup left the skeleton
            # anchors in place (the same rule as the single-layer
            # emit_arcs check).  The via junction itself stays a
            # straight-through connection (KiCad behavior): a leg whose
            # fillet arc would end on a via/waypoint anchor degrades to
            # the mitered/straight postprocess path below.
            emit_leg = False
            if eng.trace is not None:
                leg_pts = list(eng.trace.points)
                if li == 0 and pad_a_size is not None:
                    leg_pts = _replace_pad_path(leg_pts, pad_a_xy, pad_a_size, from_center=True)
                elif li == n_legs - 1 and pad_b_size is not None:
                    leg_pts = _replace_pad_path(leg_pts, pad_b_xy, pad_b_size, from_center=False)
                emit_leg = (
                    len(leg_pts) == len(eng.trace.points)
                    and all(_pt_eq(p, q) for p, q in zip(leg_pts, eng.trace.points))
                    and any(a is not None for a in eng.trace.arcs)
                )
                if emit_leg:
                    for arc in eng.trace.arcs:
                        if arc is None:
                            continue
                        for a_pt in (arc.start, arc.mid, arc.end):
                            if any(_pt_eq(a_pt, va) for va in via_anchors):
                                emit_leg = False
                                break
                        if not emit_leg:
                            break
            else:
                leg_pts = pts
            if emit_leg:
                segs_li: list[OutputSegment] = []
                arcs_li: list[OutputArc] = []
                for i in range(len(leg_pts) - 1):
                    arc_i = eng.trace.arcs[i] if i < len(eng.trace.arcs) else None
                    if arc_i is not None:
                        arcs_li.append(
                            OutputArc(
                                start=arc_i.start,
                                mid=arc_i.mid,
                                end=arc_i.end,
                                width=width,
                                layer=layer,
                                net=req.net,
                            )
                        )
                    else:
                        x1, y1 = leg_pts[i]
                        x2, y2 = leg_pts[i + 1]
                        if abs(x1 - x2) > 1e-6 or abs(y1 - y2) > 1e-6:
                            segs_li.append(
                                OutputSegment(
                                    x1=x1,
                                    y1=y1,
                                    x2=x2,
                                    y2=y2,
                                    width=width,
                                    layer=layer,
                                    net=req.net,
                                )
                            )
                direct_segs.append(segs_li)
                direct_arcs.append(arcs_li)
                node_pts = leg_pts
            else:
                direct_segs.append(None)
                direct_arcs.append(None)
                node_pts = pts
            for pi, (x, y) in enumerate(node_pts):
                if li > 0 and pi == 0:
                    continue  # shared via anchor, already emitted
                all_nodes.append(RouteNode(x=x, y=y, layer=layer, node_id=node_id))
                node_id += 1
            # No adjustment escapes DRC: re-audit this leg's final
            # polyline after pad replacement (the engine audit ran on
            # ``eng.path`` before that step, and the later legs are not
            # routed yet — the earlier ones are fixed copper via
            # ``extra_fixed`` and audited against this leg).  Merge any
            # sub-width tap-in stubs first (legs shorter than the track
            # width would be classified as fixed solids by a later
            # shove pass).
            node_pts = _drop_subwidth_points(node_pts, width, clearance, engine_obstacles, req.net)
            _final_path_drc(
                node_pts,
                width,
                clearance,
                req.net,
                engine_obstacles,
                extra_fixed,
                moved_pairs,
                eng.orig_obstacle_ids,
                req,
                layer,
                _pad_viz,
                buffered,
                route_bbox,
            )
            # This leg's final geometry becomes fixed copper for the shove
            # stage of every later leg (see the extra_fixed block above).
            prev_leg_polylines.append((layer, list(node_pts)))

        def finalize_legs() -> None:
            """Postprocess the emitted leg nodes and re-assemble per-leg
            direct (skeleton) emission on top of the mitered output."""
            nonlocal segs, vias, arcs_out, start_xy, end_xy, layers_used
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
            segs = [s_ for s_ in segs if abs(s_.x1 - s_.x2) > 1e-6 or abs(s_.y1 - s_.y2) > 1e-6]
            # Per-leg assembly: legs with an intact rounded skeleton keep
            # their direct skeleton segments + arcs; the rest keep the
            # postprocess output (mitered straight segments on their layer).
            if any(ls is not None for ls in direct_segs):
                post_by_layer: dict[str, list[OutputSegment]] = {}
                for s_ in segs:
                    post_by_layer.setdefault(s_.layer, []).append(s_)
                final_segs: list[OutputSegment] = []
                arcs_out = []
                # Each layer's postprocess output covers the whole chain on
                # that layer, so it must be consumed by exactly one non-direct
                # leg — otherwise every same-layer non-direct leg extends a
                # full copy of the track (N duplicate chains).
                post_used: set[str] = set()
                for li in range(len(direct_segs)):
                    layer = leg_layers[li]
                    if direct_segs[li] is not None:
                        final_segs.extend(direct_segs[li])
                        arcs_out.extend(direct_arcs[li])
                    elif layer not in post_used:
                        final_segs.extend(post_by_layer.get(layer, []))
                        post_used.add(layer)
                segs = final_segs
            else:
                # No leg emitted arcs: exactly the pre-existing multi-layer
                # output (all postprocess segments, no arcs).
                arcs_out = []
            start_xy = (all_nodes[0].x, all_nodes[0].y)
            end_xy = (all_nodes[-1].x, all_nodes[-1].y)
            layers_used = _layers_used(all_nodes)

        def apply_chain_fillets() -> None:
            """Round the FINAL waypoint-chain output into a G1 chain
            ("straight segment -> tangent arc -> straight ...") in the
            rounded corner modes.

            Every interior corner formed by two straight segments on the
            same layer -- waypoint joints and residual postprocess
            corners -- gets a tangent fillet arc whose endpoints sit
            exactly on the two edges.  Corners already carrying a per-leg
            skeleton arc are left alone (no double rounding), via joints
            stay straight-through, and each fillet is DRC-checked against
            the layer obstacles: the radius halves until the sampled arc
            centerline keeps ``clearance + width/2`` from every obstacle,
            and below the minimum radius the corner keeps its original
            sharp geometry instead of risking a DRC conflict."""
            nonlocal segs, arcs_out
            if corner_mode not in (CornerMode.ROUNDED_45, CornerMode.ROUNDED_90):
                return
            if len(segs) < 2:
                return
            obstacles_cache: dict[str, list] = {}

            def layer_obstacles(layer: str) -> list:
                if layer not in obstacles_cache:
                    obstacles_cache[layer] = _layer_engine_obstacles(
                        model,
                        data,
                        req,
                        start_layer,
                        end_layer,
                        layer,
                        pad_a_xy,
                        pad_b_xy,
                    )
                return obstacles_cache[layer]

            def arc_clear(t1, mid, t2, r, c, layer: str) -> bool:
                """True when the sampled fillet arc centerline keeps
                clearance + width/2 from every obstacle on ``layer``."""
                cx, cy = c
                tau = 2.0 * math.pi
                a0 = math.atan2(t1[1] - cy, t1[0] - cx)
                am = math.atan2(mid[1] - cy, mid[0] - cx)
                a1 = math.atan2(t2[1] - cy, t2[0] - cx)
                span = (a1 - a0) % tau
                if ((am - a0) % tau) > span:
                    span -= tau
                pts = [t1]
                for k in range(1, 32):
                    ang = a0 + span * k / 32.0
                    pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
                pts.append(t2)
                arc_line = LineString(pts)
                margin = clearance + width / 2.0
                return all(
                    float(o.shape.distance(arc_line)) >= margin - 1e-4
                    for o in layer_obstacles(layer)
                )

            out: list[OutputSegment] = []
            new_arcs: list[OutputArc] = []
            tol = 1e-6
            i = 0
            pending: OutputSegment | None = None
            while i < len(segs):
                # ``pending`` is the trimmed tail of a previous fillet: its
                # end coincides with the next segment's start, so it must be
                # re-examined against that segment (a postprocessed waypoint
                # joint produces TWO miter vertices; both need rounding).
                s1 = pending if pending is not None else segs[i]
                if pending is not None:
                    pending = None
                    s2_idx = i
                else:
                    s2_idx = i + 1
                was_pending = s1 is not segs[i]
                if s2_idx >= len(segs):
                    if not was_pending:
                        out.append(s1)
                    break
                s2 = segs[s2_idx]
                if s1.layer != s2.layer:
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                s1_pts = ((s1.x1, s1.y1), (s1.x2, s1.y2))
                s2_pts = ((s2.x1, s2.y1), (s2.x2, s2.y2))
                shared = [p for p in s1_pts if any(_pt_eq(p, q, tol) for q in s2_pts)]
                if len(shared) != 1:
                    # Not a clean segment/segment join (zero, duplicate or
                    # arc-bridged): leave the corner untouched.
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                jp = shared[0]
                if any(_pt_eq(jp, va, tol) for va in fillet_excluded):
                    # Via joint: straight-through.
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                if any(_pt_eq(jp, ap, tol) for a in arcs_out for ap in (a.start, a.mid, a.end)):
                    # The corner coincides with an already-emitted skeleton
                    # arc (its endpoint or midpoint): that arc already
                    # rounds the joint, so rounding again would create a
                    # double fillet overlapping the emitted curve (e.g. a
                    # direct leg's collapsed chord re-rounding the corner
                    # its own skeleton arc already covers).
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                a_pt = (s1.x2, s1.y2) if _pt_eq((s1.x1, s1.y1), jp, tol) else (s1.x1, s1.y1)
                b_pt = (s2.x1, s2.y1) if _pt_eq((s2.x2, s2.y2), jp, tol) else (s2.x2, s2.y2)
                first = _chain_fillet_arc(a_pt, jp, b_pt, _CHAIN_FILLET_RADIUS_MM)
                if first is None:
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                # DRC shrink: halve the radius until the arc clears the
                # layer obstacles; below the minimum radius keep the
                # sharp corner (never emit a violating arc).
                accepted = None
                r_probe = first[3]
                while r_probe >= _CHAIN_FILLET_MIN_RADIUS_MM - 1e-12:
                    res = _chain_fillet_arc(a_pt, jp, b_pt, r_probe)
                    if res is None:
                        break
                    _t1, _mid, _t2, _r, _c = res
                    if arc_clear(_t1, _mid, _t2, _r, _c, s1.layer):
                        accepted = res
                        break
                    r_probe = _r / 2.0
                if accepted is None:
                    if was_pending:
                        i += 1
                    else:
                        out.append(s1)
                        i += 1
                    continue
                t1, mid, t2, _r_use, _c_use = accepted
                if was_pending:
                    # The pending segment was the previous fillet's
                    # trimmed tail; the new fillet re-trims it, so the
                    # stale copy must be replaced by the trim below.
                    out.pop()
                if not _pt_eq(a_pt, t1, tol):
                    out.append(
                        OutputSegment(
                            x1=a_pt[0],
                            y1=a_pt[1],
                            x2=t1[0],
                            y2=t1[1],
                            width=width,
                            layer=s1.layer,
                            net=req.net,
                        )
                    )
                new_arcs.append(
                    OutputArc(
                        start=t1,
                        mid=mid,
                        end=t2,
                        width=width,
                        layer=s1.layer,
                        net=req.net,
                    )
                )
                if not _pt_eq(t2, b_pt, tol):
                    out.append(
                        OutputSegment(
                            x1=t2[0],
                            y1=t2[1],
                            x2=b_pt[0],
                            y2=b_pt[1],
                            width=width,
                            layer=s2.layer,
                            net=req.net,
                        )
                    )
                # Advance past the consumed pair but keep the trimmed tail
                # (T2->b_pt) as ``pending``: its end meets the next segment
                # at b_pt and may form another corner that needs rounding.
                i = s2_idx + 1 if not was_pending else i + 1
                pending = out[-1] if not _pt_eq(t2, b_pt, tol) else None
            segs = out
            # Rebuild arcs_out in path order: existing (per-leg skeleton)
            # arcs merged with the new fillet arcs, each anchored at the
            # segment whose end its start point sits on (stable sort keeps
            # the original relative order of the skeleton arcs).
            anchored: list[tuple[int, int, OutputArc]] = []
            for ai, a in enumerate(list(arcs_out) + new_arcs):
                anchor = None
                for si, s_ in enumerate(segs):
                    if _pt_eq((s_.x2, s_.y2), a.start, tol):
                        anchor = si
                        break
                if anchor is None:
                    anchor = len(segs) + ai
                anchored.append((anchor, ai, a))
            anchored.sort(key=lambda t: (t[0], t[1]))
            arcs_out = [a for _, _, a in anchored]

        if req.waypoints:
            # -- Anchor chain (waypoints / via anchors) ----------------
            # Each anchor consumes one leg boundary: waypoints split the
            # current leg layer, via anchors switch the leg layer at a
            # DRC-validated through-via site.  The A* planner rejects
            # waypoints at entry.
            via_forbidden = [
                poly for poly, _pl, _rf, _pn, _ctr in _same_net_pad_polygons(data, req.net)
            ]
            pending_pos = pad_a_xy
            pending_layer = start_layer
            base_seq = (
                _resolve_layer_sequence(start_layer, end_layer, req.via_pairs)
                if start_layer != end_layer
                else [start_layer]
            )
            n_legs = len(req.waypoints) + 1
            chain: list[tuple[float, float]] = [pad_a_xy]
            # -- Cocircular chain fast path ----------------------------
            # A soft-anchored single-layer waypoint chain whose anchors
            # all sit on one circle is a single continuous arc: emit one
            # 3-point OutputArc covering the whole chain (pad lead-out ->
            # waypoints -> pad lead-out) instead of the per-leg straight
            # skeletons + fillet joints, which on a ring layout render as
            # scalloped chord waves with arc/chord self-intersection leaf
            # eyes.  Falls back to the per-leg path below on any doubt:
            # fewer than 4 waypoints (any three anchors fit some circle,
            # so cocircularity is not a real signal), a non-cocircular
            # chain, an anchor outside the arc sweep, or an obstacle
            # closer than clearance + width/2 to the emitted centerline
            # -- the legacy behavior is preserved untouched.
            cocircular = None
            if (
                start_layer == end_layer
                and len(req.waypoints) >= 4
                and all(
                    spec.get("kind") == "waypoint" and float(spec.get("tol_mm", 0.0)) > 0.0
                    for spec in req.waypoints
                )
            ):
                wpt_pts = [_anchor_pos(spec, "waypoint") for spec in req.waypoints]
                chain_pts = [pad_a_xy, *wpt_pts, pad_b_xy]
                chain = list(chain_pts)  # exact-anchor chain for the render
                tol_fit = max(float(spec["tol_mm"]) for spec in req.waypoints)
                cocircular = _chain_cocircular_arc(chain_pts, tol_mm=tol_fit)
            arc_emitted = False
            if cocircular is not None:
                arc_start, arc_mid, arc_end, (cxc, cyc, rad, a0, span) = cocircular
                # DRC: the full centerline (pad lead-outs + densely
                # sampled arc) must keep clearance + width/2 from every
                # foreign obstacle on the layer -- model.obstacles is
                # already net-filtered, so only foreign copper blocks.
                # Same-net pads along the sweep are connection targets
                # of this single continuous track, not obstacles (KiCad
                # DRC never space-checks same-net copper), and are
                # excluded here.  Forbidden: blocking the arc.
                n_samples = max(256, int(math.ceil(abs(span) / (2.0 * math.pi) * 4096.0)))
                centerline: list[tuple[float, float]] = [pad_a_xy, arc_start]
                for k in range(1, n_samples):
                    ang = a0 + span * k / n_samples
                    centerline.append((cxc + rad * math.cos(ang), cyc + rad * math.sin(ang)))
                centerline.append(arc_end)
                centerline.append(pad_b_xy)
                arc_line = LineString(centerline)
                arc_obstacles = _layer_engine_obstacles(
                    model,
                    data,
                    req,
                    start_layer,
                    end_layer,
                    start_layer,
                    pad_a_xy,
                    pad_b_xy,
                    # Cocircular chain = one continuous track; same-net
                    # pads (mid-chain pads included) are its connection
                    # targets, not obstacles.  Foreign net copper still
                    # blocks via model.obstacles above.
                    include_same_net_pads=False,
                )
                margin = clearance + width / 2.0
                if all(float(o.shape.distance(arc_line)) >= margin - 1e-4 for o in arc_obstacles):
                    # Chain sweep is clear: emit the lead-out segments
                    # (only where the pad anchor floats off the circle)
                    # plus the single covering arc.
                    segs = []
                    if not _pt_eq(arc_start, pad_a_xy):
                        segs.append(
                            OutputSegment(
                                x1=pad_a_xy[0],
                                y1=pad_a_xy[1],
                                x2=arc_start[0],
                                y2=arc_start[1],
                                width=width,
                                layer=start_layer,
                                net=req.net,
                            )
                        )
                    if not _pt_eq(arc_end, pad_b_xy):
                        segs.append(
                            OutputSegment(
                                x1=arc_end[0],
                                y1=arc_end[1],
                                x2=pad_b_xy[0],
                                y2=pad_b_xy[1],
                                width=width,
                                layer=start_layer,
                                net=req.net,
                            )
                        )
                    vias = []
                    arcs_out = [
                        OutputArc(
                            start=arc_start,
                            mid=arc_mid,
                            end=arc_end,
                            width=width,
                            layer=start_layer,
                            net=req.net,
                        )
                    ]
                    pushed = []
                    used_chain = list(chain)
                    start_xy = pad_a_xy
                    end_xy = pad_b_xy
                    layers_used = [start_layer]
                    print(
                        f"  [route] cocircular chain: {len(chain_pts)} anchors on "
                        f"r={rad:.3f} circle, span {abs(math.degrees(span)):.1f} deg "
                        "-> single OutputArc"
                    )
                    arc_emitted = True
            if not arc_emitted:
                for li, spec in enumerate(req.waypoints):
                    kind = spec.get("kind")
                    if kind == "waypoint":
                        wpt = _anchor_pos(spec, "waypoint")
                        # Keep waypoint joints as clean segment/segment
                        # joins: a per-leg skeleton fillet may not END on
                        # the joint (its arc is built without the next
                        # leg's direction, so a joint-pinned arc would
                        # kink).  The joint itself is rounded afterwards
                        # by the chain tangent fillet (apply_chain_fillets).
                        via_anchors.add(wpt)
                        # Soft anchor: with an explicit tol_mm > 0 the leg
                        # endpoint floats to a DRC-clean spot inside the
                        # tolerance circle around wpt, so the leg's fillet
                        # arcs are no longer pinned onto the joint and the
                        # corner renders rounded.  A blocked circle (or no /
                        # zero tol_mm) falls back to the exact anchor:
                        # the chain is pinned through the waypoint and the
                        # chain tangent fillet rounds the joint in place.
                        end_pt = wpt
                        if "tol_mm" in spec and float(spec["tol_mm"]) > 0.0:
                            soft = _waypoint_soft_end(
                                wpt,
                                tol_mm=float(spec["tol_mm"]),
                                obstacles=_layer_engine_obstacles(
                                    model,
                                    data,
                                    req,
                                    start_layer,
                                    end_layer,
                                    pending_layer,
                                    pad_a_xy,
                                    pad_b_xy,
                                ),
                                track_width=width,
                                clearance=clearance,
                            )
                            if soft is not None:
                                end_pt = soft
                        try:
                            run_leg(pending_pos, end_pt, pending_layer, li=li, n_legs=n_legs)
                        except RouteFailure:
                            # Soft stop: record + skip the waypoint, keep going.
                            waypoint_violated = True
                            violated_waypoints.append(wpt)
                            continue
                        pending_pos = end_pt
                        continue
                    if kind == "via":
                        site_req = _anchor_pos(spec, "via")
                        if spec.get("to_layer") is None:
                            to_layer = _auto_via_target(base_seq, pending_layer, site_req)
                        else:
                            to_layer = str(spec["to_layer"])
                        _validate_via_transition(pending_layer, to_layer, site_req, req, pcb_layers)
                        try:
                            site = _pick_explicit_via_site(
                                pcb_path=req.pcb_path,
                                requested=site_req,
                                from_layer=pending_layer,
                                to_layer=to_layer,
                                via_diameter=via_diameter,
                                via_drill=via_drill,
                                clearance=clearance,
                                net=req.net,
                                via_forbidden=via_forbidden,
                                tol_mm=float(spec.get("tol_mm", 1.0)),
                            )
                        except RouteFailure as exc:
                            # No DRC-clean site: render the blocking copper
                            # around the request into the failure message.
                            msg = str(exc)
                            png = _render_route_failure_evidence(
                                req.pcb_path,
                                chain=chain,
                                attempted_end=site_req,
                                layer=pending_layer,
                                obstacles=_layer_engine_obstacles(
                                    model,
                                    data,
                                    req,
                                    start_layer,
                                    end_layer,
                                    pending_layer,
                                    pad_a_xy,
                                    pad_b_xy,
                                ),
                            )
                            if png:
                                msg += f"\nFailure evidence: {png}"
                            raise RouteFailure(msg) from exc
                        via_anchors.add(site)
                        # A via junction stays a straight-through connection
                        # (KiCad behavior): never rounded by the chain
                        # tangent fillet.
                        fillet_excluded.add(site)
                        via_sites.append({"pos": [site[0], site[1]], "to_layer": to_layer})
                        chain.append(site)
                        run_leg(
                            pending_pos,
                            site,
                            pending_layer,
                            li=li,
                            n_legs=n_legs,
                            render_evidence=True,
                            evidence_ctx={"chain": list(chain)},
                        )
                        pending_pos = site
                        pending_layer = to_layer
                        continue
                    raise RouteFailure(
                        f"unsupported anchor kind {kind!r}; expected 'waypoint' or 'via'"
                    )
                if pending_layer != end_layer:
                    # A thru-hole terminal pad carries copper on every
                    # layer, so an anchor chain ending on ANY of its
                    # copper layers is a valid terminus -- only an SMD
                    # pad (fixed to one layer) must match ``end_layer``
                    # exactly.
                    if _find_pad_center(data, req.ref_b, req.pad_b, pending_layer) is None:
                        raise RouteFailure(
                            f"anchor chain ends on layer {pending_layer!r} but pad "
                            f"{req.ref_b}/{req.pad_b} is on {end_layer!r}; add a via "
                            "anchor that reaches the target layer (allowed via_pairs "
                            f"{list(req.via_pairs)})"
                        )
                run_leg(
                    pending_pos,
                    pad_b_xy,
                    pending_layer,
                    li=len(req.waypoints),
                    n_legs=n_legs,
                    render_evidence=True,
                    evidence_ctx={"chain": list(chain)},
                )
                used_chain = list(chain)
                finalize_legs()
                # G1 chain tangent fillet on the final output: rounds the
                # waypoint joints (and any residual leg corners) with
                # tangent arcs in the rounded corner modes.
                apply_chain_fillets()
        elif start_layer == end_layer:
            engine_obstacles: list[Obstacle] = [
                o for o in model.obstacles if start_layer in o.layers
            ]
            for poly, players, _ref, _pname, center in _same_net_pad_polygons(data, req.net):
                is_end = (
                    abs(center[0] - pad_a_xy[0]) < 1e-6 and abs(center[1] - pad_a_xy[1]) < 1e-6
                ) or (abs(center[0] - pad_b_xy[0]) < 1e-6 and abs(center[1] - pad_b_xy[1]) < 1e-6)
                if is_end and start_layer in players:
                    continue  # the route terminates on the endpoint pad
                if start_layer in players:
                    engine_obstacles.append(
                        Obstacle(
                            shape=poly,
                            layers=frozenset({start_layer}),
                            net=req.net,
                            kind="pad",
                        )
                    )
            # corner_mode already validated + parsed at entry (line 236).
            try:
                eng = route_engine(
                    pad_a_xy,
                    pad_b_xy,
                    engine_obstacles,
                    track_width=width,
                    clearance=clearance,
                    corner_mode=corner_mode,
                    max_shove_depth=shove_depth,
                    net=req.net,
                )
            except PnsFailure as exc:
                # Dump the failure PROCESS so the blockage can be
                # inspected — one viz stage per walkaround iteration /
                # shove hit / promote round (``fail-pns-000`` through
                # ``fail-pns-NNN``), plus the final state.  ``last_path``
                # carries the walkaround/shove line as it stood at
                # failure — a real polyline, not an empty placeholder —
                # and ``shoved_pairs`` the tracks already displaced.  A
                # study of the failure reads the frames in order: how
                # the path crept, which track stopped the shove, and
                # which of its endpoints were pinned by pads.
                fail_frames = list(exc.frames or [])
                if fail_frames:
                    for fi, fr in enumerate(fail_frames):
                        _dump_viz(
                            f"fail-pns-{fi:03d}",
                            fr.get("path") or [],
                            _pad_viz,
                            buffered,
                            route_bbox,
                            pinned=fr.get("pinned"),
                            note=fr.get("note"),
                            hit=fr.get("hit"),
                        )
                _dump_viz(
                    "fail-pns-final",
                    exc.last_path or [],
                    _pad_viz,
                    buffered,
                    route_bbox,
                    shoved=[
                        {"net": orig.net, "from": orig.points, "to": disp.points}
                        for orig, disp in exc.shoved_pairs
                    ]
                    if exc.shoved_pairs
                    else None,
                )
                raise RouteFailure(
                    f"No obstacle-avoiding path from {req.ref_a}/{req.pad_a} to "
                    f"{req.ref_b}/{req.pad_b} at {width}mm track width on layer "
                    f"{start_layer}: {exc}"
                ) from exc
            print(
                f"  [route] PNS: {len(eng.path)} pts"
                f"  shoved={len(eng.shoved_tracks)}  arcs={len(eng.arcs)}"
            )
            pushed: list[TrackObstacle] = eng.shoved_tracks
            moved_pairs.extend(eng.moved_pairs)

            if eng.trace is not None:
                # Embargo-free skeleton: emit anchor points and arcs straight
                # from the trace (KiCad emits the skeleton for a clear route).
                best_path_pts = eng.trace.points
            else:
                best_path_pts = eng.path

            _dump_viz(
                "0-pns",
                best_path_pts,
                _pad_viz,
                buffered,
                route_bbox,
                shoved=[
                    {"net": orig.net, "from": orig.points, "to": disp.points}
                    for orig, disp in eng.moved_pairs
                ],
            )

            # ---- Replace scheme inside rectangular pads with an
            #      axis-aligned wire (fence -> centre) ----
            if pad_a_size is not None:
                n_before = len(best_path_pts)
                best_path_pts = _replace_pad_path(
                    best_path_pts, pad_a_xy, pad_a_size, from_center=True
                )
                _log_path("pad_a-replace", best_path_pts, n_before)
            if pad_b_size is not None:
                n_before = len(best_path_pts)
                best_path_pts = _replace_pad_path(
                    best_path_pts, pad_b_xy, pad_b_size, from_center=False
                )
                _log_path("pad_b-replace", best_path_pts, n_before)
            _dump_viz("1-pad-replace", best_path_pts, _pad_viz, buffered, route_bbox)

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

            # The alignment step can snap the endpoint into a sub-width
            # tap-in (a leg shorter than the track width) — merge such
            # stubs away before the final audit.
            best_path_pts = _drop_subwidth_points(
                best_path_pts,
                width,
                clearance,
                engine_obstacles,
                req.net,
            )
            _dump_viz("7-no-subwidth", best_path_pts, _pad_viz, buffered, route_bbox)

            # No adjustment escapes DRC: re-audit the final polyline
            # after pad replacement + alignment (the engine audit ran on
            # ``eng.path`` before these steps).
            _final_path_drc(
                best_path_pts,
                width,
                clearance,
                req.net,
                engine_obstacles,
                [],
                moved_pairs,
                eng.orig_obstacle_ids,
                req,
                start_layer,
                _pad_viz,
                buffered,
                route_bbox,
            )

            path_nodes = path_to_nodes(best_path_pts, start_layer)
            segs, vias = postprocess_path(
                path_nodes,
                width=width,
                net=req.net,
                max_miter_mm=req.max_miter_mm,
                _obstacles=buffered,
                _pad_rects=_pad_rects or None,
            )
            segs = [s for s in segs if abs(s.x1 - s.x2) > 1e-6 or abs(s.y1 - s.y2) > 1e-6]

            # Rounded skeleton arcs -> OutputArc nodes.  Valid only when
            # walkaround/shove/pad-cleanup left the original skeleton anchors
            # in place.  When arcs are emitted, skip mitering on the legs
            # they join (postprocess miter would cut into the arc end).
            arcs_out: list[OutputArc] = []
            emit_arcs = (
                eng.trace is not None
                and len(best_path_pts) == len(eng.trace.points)
                and all(_pt_eq(p, q) for p, q in zip(best_path_pts, eng.trace.points))
            )
            if emit_arcs:
                if any(a is not None for a in eng.trace.arcs):
                    # Skeleton with arcs: emit straight legs + arcs directly,
                    # no mitering (the rounded corner already smooths the join).
                    segs = []
                    pts = eng.trace.points
                    for i in range(len(pts) - 1):
                        arc_i = eng.trace.arcs[i] if i < len(eng.trace.arcs) else None
                        if arc_i is not None:
                            arcs_out.append(
                                OutputArc(
                                    start=arc_i.start,
                                    mid=arc_i.mid,
                                    end=arc_i.end,
                                    width=width,
                                    layer=start_layer,
                                    net=req.net,
                                )
                            )
                        else:
                            x1, y1 = pts[i]
                            x2, y2 = pts[i + 1]
                            if abs(x1 - x2) > 1e-6 or abs(y1 - y2) > 1e-6:
                                segs.append(
                                    OutputSegment(
                                        x1=x1,
                                        y1=y1,
                                        x2=x2,
                                        y2=y2,
                                        width=width,
                                        layer=start_layer,
                                        net=req.net,
                                    )
                                )
                else:
                    # Mitered skeleton (no arcs): keep the mitered postprocess
                    # output; nothing further to emit.
                    pass

            _log_output_segments("final", segs)
            _dump_viz_segments("7-final", segs, _pad_viz, buffered, route_bbox)

            start_xy = (path_nodes[0].x, path_nodes[0].y)
            end_xy = (path_nodes[-1].x, path_nodes[-1].y)
            layers_used = _layers_used(path_nodes)
        else:
            # -- Multi-layer: per-leg walkaround + shove, vias on the
            #    direct pad-to-pad line ---------------------------------
            layer_seq = _resolve_layer_sequence(start_layer, end_layer, req.via_pairs)
            n_trans = len(layer_seq) - 1
            via_forbidden = [
                poly for poly, _pl, _rf, _pn, _ctr in _same_net_pad_polygons(data, req.net)
            ]
            via_positions = _pick_via_positions(
                pcb_path=req.pcb_path,
                layer_seq=layer_seq,
                start=pad_a_xy,
                end=pad_b_xy,
                via_forbidden=via_forbidden,
                via_diameter=via_diameter,
                via_drill=via_drill,
                clearance=clearance,
                net=req.net,
            )
            anchors = [pad_a_xy, *via_positions, pad_b_xy]
            via_anchors = set(anchors[1:-1])
            for li in range(n_trans + 1):
                run_leg(anchors[li], anchors[li + 1], layer_seq[li], li=li, n_legs=n_trans + 1)
            finalize_legs()

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
        # Endpoint pads may straddle the board edge (edge connectors);
        # their copper is a legal terminus, so exempt those rectangles
        # from the board fence.
        pad_zones: list[tuple[float, float, float, float]] = []
        for pxy, psize in ((pad_a_xy, pad_a_world_size), (pad_b_xy, pad_b_world_size)):
            if psize is not None:
                pad_zones.append((pxy[0], pxy[1], psize[0] / 2.0, psize[1] / 2.0))
        _check_segments_in_board(segs, model.board_bbox, pad_zones=pad_zones or None)
        if vias:
            _check_vias_in_board(vias, model.board_bbox)

    # Best-effort single-route render (VLM feedback): recompose the
    # final polyline from the emitted geometry and render it on the
    # board.  Failure to render must never mask the route result.
    route_png = _render_route_png(
        req.pcb_path, pts=_route_polyline(segs, arcs_out), anchors=used_chain
    )

    # A foreign track shoved by two legs (all PNS legs shove against the
    # same model snapshot) has one authoritative displacement: the LAST.
    # Earlier displaced polylines must never reach the file — the write
    # path deletes the original segment once and would otherwise append
    # both forks, doubling/disconnecting the physical track.
    moved_pairs = _collapse_moved_pairs(moved_pairs)
    pushed = [disp for _orig, disp in moved_pairs]

    return RouteResult(
        segments=segs,
        vias=vias,
        arcs=arcs_out,
        shoved_tracks=pushed,
        corner_mode=req.corner_mode,
        algorithm=req.algorithm,
        start=start_xy,
        end=end_xy,
        layers_used=layers_used,
        waypoint_violated=waypoint_violated,
        violated_waypoints=violated_waypoints,
        via_sites=via_sites,
        strategy=req.strategy,
        route_png=route_png,
        moved_pairs=moved_pairs,
    )


def _collapse_moved_pairs(
    moved_pairs: Sequence[tuple[TrackObstacle, TrackObstacle]],
) -> list[tuple[TrackObstacle, TrackObstacle]]:
    """Collapse repeated shoves of the same original track to the last.

    Multi-leg PNS routes run every leg against one board snapshot, so a
    track displaced by an early leg can be re-displaced by a later leg
    starting from its ORIGINAL position.  Each original therefore appears
    in ``moved_pairs`` once per leg that hit it; only its LAST
    displacement is authoritative.  The write path deletes the original
    file segment once and would otherwise append every displaced
    polyline — a forked, disconnected track.
    """
    last: dict[tuple, tuple[TrackObstacle, TrackObstacle]] = {}
    for orig, disp in moved_pairs:
        key = (orig.start, orig.end, orig.width, orig.layer, orig.net)
        last[key] = (orig, disp)
    return list(last.values())


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

    The via is placed at the (x, y) of seg_a end. Both segments are
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
    """Segment angle in degrees, screen geometry (0=right, 90=down).

    Debug-log only; this is the raw atan2 angle in Y-down screen
    coordinates, NOT the KiCad CCW file-angle convention (90=up).
    """
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360


def _pt_eq(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-6) -> bool:
    """Point comparison within tolerance."""
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _drop_subwidth_points(
    pts: list[tuple[float, float]],
    width: float,
    clearance: float,
    obstacles: Sequence[Obstacle],
    net: str | None,
) -> list[tuple[float, float]]:
    """Merge endpoint tap-in stubs shorter than the track width.

    The planner (A* endpoint alignment, PNS pad alignment) can emit a
    tap-in leg of length <= ``width`` (a "sub-width" segment) when it
    pulls the endpoint toward a pad centre.  Such a segment is a
    degenerate stub: it prints poorly and would be classified as a
    *fixed* (non-shovable) obstacle by a later shove pass.

    Only the FIRST and LAST legs are candidates — a middle leg that
    happens to be short is usually a walkaround sampling point hugging
    an obstacle hull, and must not be merged away (the merged line
    would cut into the obstacle's clearance).  A candidate merge is
    applied only when the merged leg keeps ``clearance`` from every
    obstacle itself; otherwise the original stub is kept — it is the
    geometry the engine deliberately produced to stay clear, and a
    DRC-clean short leg beats a merged one that the final audit would
    reject.  The caller re-audits the final polyline regardless (no
    adjustment escapes it).
    """
    if len(pts) < 3:
        return list(pts)
    out = list(pts)

    def _merge_clear(a: tuple[float, float], b: tuple[float, float]) -> bool:
        from shapely.geometry import LineString
        from shapely.strtree import STRtree

        hulls = [o.shape for o in obstacles if o.shape is not None and not o.shape.is_empty]
        if not hulls:
            return True
        copper = LineString([a, b]).buffer(width / 2.0, cap_style="round", quad_segs=512)
        tree = STRtree(hulls)
        for gi in tree.query(copper.buffer(clearance)):
            other = hulls[gi]
            other_net = obstacles[gi].net
            if net is not None and other_net is not None and net == other_net:
                continue  # same net: no DRC gap required
            if copper.distance(other) < clearance - 1e-9:
                return False
        return True

    # Head: merge away leading stubs while the merged leg stays clear.
    while len(out) >= 3:
        a = out[0]
        b = out[2]
        if math.hypot(out[1][0] - a[0], out[1][1] - a[1]) > width:
            break
        if not _merge_clear(a, b):
            break
        del out[1]

    # Tail: merge away trailing stubs while the merged leg stays clear.
    while len(out) >= 3:
        a = out[-3]
        b = out[-1]
        if math.hypot(out[-2][0] - b[0], out[-2][1] - b[1]) > width:
            break
        if not _merge_clear(a, b):
            break
        del out[-2]

    return out


def _parse_corner_mode(mode: str) -> CornerMode:
    """Map a RouteRequest corner_mode string to its enum; invalid values
    raise RouteFailure with the accepted set."""
    try:
        return CornerMode(mode)
    except ValueError as exc:
        accepted = ", ".join(sorted(m.value for m in CornerMode))
        raise RouteFailure(f"Unknown corner_mode {mode!r}; expected one of: {accepted}") from exc


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
        segs.append(f"({x1:.3f},{y1:.3f})->({x2:.3f},{y2:.3f}){kind}")
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
        print(f"  [{label}] seg{i}: ({s.x1:.3f},{s.y1:.3f})->({s.x2:.3f},{s.y2:.3f}) {kind}")


# ---------------------------------------------------------------------------
# Visualization dump
# ---------------------------------------------------------------------------

import os as _os
import time as _time


def _viz_dir() -> str:
    """Return the PCB viz dump directory under the kcaa data dir kcaa_viz/pcb_viz."""
    from kcaa.utils.config import config

    return _os.path.join(config.get_kcaa_data_dir(), "kcaa_viz", "pcb_viz")


def _dump_viz(
    stage: str,
    pts: list[tuple[float, float]],
    pad_viz: list[tuple[str, tuple[float, float, float, float]]],
    obstacles: list,
    route_bbox: tuple[float, float, float, float],
    *,
    shoved: list[dict] | None = None,
    pinned: list[dict] | None = None,
    note: str | None = None,
    hit: dict | None = None,
) -> None:
    """Dump path, pad rects, and obstacles to a JSON file for rendering.

    ``shoved`` optionally carries the PNS shove displacements as a list
    of ``{"net": str, "from": [[x, y], ...], "to": [[x, y], ...]}`` —
    each moved track's original and final polyline.  ``render_viz.py``
    draws those in a distinct color next to the route's current line.

    ``pinned`` optionally carries the endpoints of the hit track that
    sit inside fixed pads — ``[{"x", "y", "pad"}]`` — so the renderer
    can mark the locked points (the reason the shove could not move the
    track) on the failure frames.

    ``note`` optionally carries the frame's stage note (e.g. which
    segments were promoted in a promote round); ``hit`` carries the
    shove hit obstacle's description.  Both are written verbatim for
    the renderer/forensics.

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
    if note:
        data["note"] = note
    if hit:
        data["hit"] = hit
    if shoved:
        data["shoved"] = [
            {
                "net": d.get("net", ""),
                "from": [(x, y) for x, y in d["from"]],
                "to": [(x, y) for x, y in d["to"]],
            }
            for d in shoved
        ]
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
    """Run simplify -> shortcut -> snap45 on a single layer's path.

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

    # Re-simplify: snap45 may create new collinear points.
    pts = simplify_path(pts)
    _log_path(f"{stage_prefix}resimplify", pts)
    _dump_viz(f"{stage_prefix}5-resimplify", pts, pad_viz, obstacles, route_bbox)

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
    """Align one endpoint to a pad centre via X->Y iterative translation.

    ``from_center=True`` aligns the start of *path*; ``False`` aligns
    the end.  See :func:`_align_path_endpoints` for the detailed algorithm.
    """
    if len(path) < 2:
        return path

    pcx, pcy = pad_center
    if from_center:
        # -- Start pad -----------------------------------------------
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
        # -- End pad -------------------------------------------------
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
    """Align both endpoints to pad centres via X->Y translation.

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


def _final_path_drc(
    pts: list[tuple[float, float]],
    width: float,
    clearance: float,
    net: str | None,
    obstacles: Sequence[Obstacle],
    extra_fixed: Sequence[Obstacle],
    moved_pairs: Sequence[tuple[TrackObstacle, TrackObstacle]],
    orig_obstacle_ids: set[int],
    req: RouteRequest,
    layer: str,
    pad_viz: list,
    buffered: list,
    route_bbox: tuple[float, float, float, float],
) -> None:
    """Audit the FINAL polyline — after every post-engine adjustment —
    against the world model.

    The engine audits its own output, but the ``_replace_pad_path`` /
    ``_align_*`` steps run afterwards and are exempt from it; their
    endpoint segments are exactly what can be pushed onto a neighbouring
    pad (e.g. a dense connector's pad).  Re-running the audit here closes
    that gap: adjusted geometry that violates DRC fails the route instead
    of being written to the board.  No adjustment escapes a final check.
    """
    try:
        _audit_final_copper(
            out_path=pts,
            width=width,
            net=net,
            obstacles=obstacles,
            extra_fixed=extra_fixed,
            moved_pairs=moved_pairs,
            orig_obstacle_ids=orig_obstacle_ids,
            clearance=clearance,
        )
    except PnsFailure as exc:
        _dump_viz("fail-final-drc", pts, pad_viz, buffered, route_bbox)
        raise RouteFailure(
            f"Final DRC check failed for {req.ref_a}/{req.pad_a} -> "
            f"{req.ref_b}/{req.pad_b} on {layer}: {exc}"
        ) from exc


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
                # keep = [projection, fence].  The caller keeps the pad
                # centre: the wire must start exactly at the centre, so a
                # subsequent endpoint alignment has no X/Y translation to
                # do.  Without this the whole leading leg gets translated
                # and can be pushed onto copper the engine already shoved
                # around (see _build_pad_wire docstring).
                wire = [(cx, cy)]
                if not _pt_eq(keep[0], center):
                    wire.append(keep[0])
                wire.append(keep[1])
                return wire + path[k:]
        return path
    else:
        for k in range(len(path) - 2, -1, -1):
            if not _inside_rect(path[k], center, hw, hh):
                fx, fy = path[k + 1]
                ox, oy = path[k]
                # Same (center, fence) order -- reverse so path reads [fence, projection].
                keep = _build_pad_wire(cx, cy, fx, fy, ox, oy, minx, maxx, miny, maxy)
                # Symmetric: pad wire must end exactly at the pad centre.
                wire = keep[::-1]
                if not _pt_eq(wire[-1], center):
                    wire.append((cx, cy))
                return path[: k + 1] + wire
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
    """Return ``[projection, fence]`` -- a single axis-aligned segment
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
        # fence on left/right edge -> horizontal wire at fence_y
        return [(x1, y2), (x2, y2)]
    else:
        # fence on top/bottom edge -> vertical wire at fence_x
        return [(x2, y1), (x2, y2)]


# ---------------------------------------------------------------------------
# Board-bounds check
# ---------------------------------------------------------------------------


def _check_segments_in_board(
    segs: list[OutputSegment],
    board_bbox: tuple[float, float, float, float],
    pad_zones: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """Raise :class:`RouteFailure` if any segment leaves the Edge.Cuts AABB.

    The check is conservative: we test the segment endpoints plus a few
    interior points against the board polygon *shrunk* by the segment's
    own ``width / 2``, so the track's copper edge is what we verify.
    A track whose centerline is exactly on the boundary is allowed (its
    copper would still touch but not cross the edge); a track whose
    centerline is on the wrong side of the shrunk boundary fails.

    ``pad_zones`` relaxes the fence around the route's endpoint pads:
    footprints mounted on the board edge (edge connectors) legitimately
    straddle the outline, so copper running onto those pads must not be
    rejected. Each zone is ``(cx, cy, half_w, half_h)`` in world coords
    and is unioned into the region a segment's copper may occupy.

    Args:
        segs: The segments produced by :func:`postprocess`.
        board_bbox: ``(minx, miny, maxx, maxy)`` from
            :func:`kcaa.router.world_model._board_bbox`.
        pad_zones: Optional endpoint-pad rectangles to exempt.

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
        allowed = Polygon(
            [
                (shrunk_bbox[0], shrunk_bbox[1]),
                (shrunk_bbox[2], shrunk_bbox[1]),
                (shrunk_bbox[2], shrunk_bbox[3]),
                (shrunk_bbox[0], shrunk_bbox[3]),
            ]
        )
        if pad_zones:
            for cx, cy, hw, hh in pad_zones:
                allowed = allowed.union(
                    Polygon(
                        [
                            (cx - hw, cy - hh),
                            (cx + hw, cy - hh),
                            (cx + hw, cy + hh),
                            (cx - hw, cy + hh),
                        ]
                    )
                )
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        if not allowed.covers(line):
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


def _routing_layers(
    req: RouteRequest,
    start_layer: str,
    end_layer: str,
) -> list[str]:
    """Return the ordered list of layers the router must consider.

    Includes ``start_layer`` and ``end_layer`` and every layer referenced
    by ``via_pairs``. Order is preserved with duplicates removed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for layer in (start_layer, end_layer):
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


def _resolve_layer_sequence(
    start_layer: str,
    end_layer: str,
    via_pairs: tuple[tuple[str, str], ...],
) -> list[str]:
    """Shortest start -> end layer path through the via-pair graph.

    Every ``via_pairs`` (top, bottom) edge is traversable in both
    directions and costs exactly one through-via.  Returns the layer
    sequence that needs the fewest vias, ties broken by layer name.
    """
    if start_layer == end_layer:
        return [start_layer]
    graph: dict[str, list[str]] = {}
    for top, bot in via_pairs:
        graph.setdefault(top, [])
        graph.setdefault(bot, [])
        for a, b in ((top, bot), (bot, top)):
            if b not in graph[a]:
                graph[a].append(b)
    for adj in graph.values():
        adj.sort()  # deterministic tie-break
    prev: dict[str, str] = {start_layer: ""}
    queue: deque[str] = deque([start_layer])
    while queue:
        cur = queue.popleft()
        if cur == end_layer:
            break
        for nxt in graph.get(cur, []):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    if end_layer not in prev:
        raise RouteFailure(
            f"no PNS layer path from {start_layer!r} to {end_layer!r} through "
            f"via_pairs {via_pairs}; allow a via pair for the missing stack "
            "transition (or switch to algorithm='astar')."
        )
    seq = [end_layer]
    while seq[-1] != start_layer:
        seq.append(prev[seq[-1]])
    seq.reverse()
    return seq


def _pick_via_positions(
    pcb_path: str,
    layer_seq: list[str],
    start: tuple[float, float],
    end: tuple[float, float],
    via_forbidden: list,
    via_diameter: float,
    via_drill: float,
    clearance: float,
    net: str,
) -> list[tuple[float, float]]:
    """Pick one through-via position per layer transition.

    Base candidates are equally spaced along the direct start -> end
    line; the first and last via sit at least ``1 / (n + 1)`` of the
    span from either pad, so vias never land on the endpoint pads.
    Candidates that fail DRC (same-net pad faces, existing copper, board
    edge, hole-to-hole) are dodged with small offsets perpendicular and
    parallel to the pad span.  Raises :class:`RouteFailure` when no
    DRC-clean spot can be found on the corridor, or when the checks
    cannot run at all (missing/malformed ``.kicad_pro`` — the via DRC
    never silently degrades).
    """
    n_trans = len(layer_seq) - 1
    if n_trans < 1:
        return []
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    span = math.hypot(dx, dy)
    if span < 1e-6:
        raise RouteFailure("cannot place PNS vias: the two pads coincide")
    ux, uy = dx / span, dy / span  # unit direction
    px, py = -uy, ux  # unit perpendicular
    step = via_diameter + max(clearance, 0.0) + 0.05
    offsets = [
        (0.0, 0.0),
        (1.0, 0.0),
        (-1.0, 0.0),
        (0.0, 1.0),
        (0.0, -1.0),
        (1.0, 1.0),
        (1.0, -1.0),
        (-1.0, 1.0),
        (-1.0, -1.0),
        (2.0, 0.0),
        (-2.0, 0.0),
        (0.0, 2.0),
        (0.0, -2.0),
    ]
    n_offsets = len(offsets)
    proposed = [
        ProposedVia(
            x=start[0] + (t + 1) * dx / (n_trans + 1) + o[0] * step * px + o[1] * step * ux,
            y=start[1] + (t + 1) * dy / (n_trans + 1) + o[0] * step * py + o[1] * step * uy,
            diameter=via_diameter,
            drill=via_drill,
            layers=(layer_seq[t], layer_seq[t + 1]),
            net=net,
        )
        for t in range(n_trans)
        for o in offsets
    ]
    violations = check_vias(pcb_path, proposed)
    hard = next((v for v in violations if v.index < 0), None)
    if hard is not None:
        # Project-level failure (missing/malformed .kicad_pro): check_vias
        # reports it once at index -1, which no candidate matches.  Fail
        # loudly instead of silently routing with unvalidated vias.
        raise RouteFailure(f"cannot DRC-check PNS vias on net {net!r}: {hard.message}")
    bad = {v.index for v in violations}
    if via_forbidden:
        from shapely.geometry import Point

        for i, pv in enumerate(proposed):
            pt = Point(pv.x, pv.y)
            if any(poly.contains(pt) or poly.touches(pt) for poly in via_forbidden):
                bad.add(i)
    out: list[tuple[float, float]] = []
    for t in range(n_trans):
        chosen = next(
            (
                (proposed[t * n_offsets + k].x, proposed[t * n_offsets + k].y)
                for k in range(n_offsets)
                if t * n_offsets + k not in bad
            ),
            None,
        )
        if chosen is None:
            raise RouteFailure(
                f"no DRC-clean via spot on the direct pad-to-pad corridor for "
                f"transition {layer_seq[t]} -> {layer_seq[t + 1]} on net "
                f"{net!r}; move a track or split the route with waypoints."
            )
        out.append(chosen)
    return out


def _layer_engine_obstacles(
    model,  # WorldModel
    data: list,
    req: RouteRequest,
    start_layer: str,
    end_layer: str,
    layer: str,
    pad_a_xy: tuple[float, float],
    pad_b_xy: tuple[float, float],
    *,
    include_same_net_pads: bool = True,
) -> list[Obstacle]:
    """PNS obstacles for one leg: copper on ``layer`` plus same-net pads
    the route may transit on that layer (endpoint pads exempted on their
    own terminal layers).

    ``include_same_net_pads=False`` skips the same-net pad set entirely:
    the cocircular fast path emits one continuous track of which the
    same-net pads (mid-chain pads included) are connection targets, not
    obstacles -- KiCad DRC never space-checks same-net copper.  Foreign
    copper (``model.obstacles``, already net-filtered to exclude this
    net) still blocks either way."""
    engine_obstacles: list[Obstacle] = [o for o in model.obstacles if layer in o.layers]
    if not include_same_net_pads:
        return engine_obstacles
    for poly, players, _ref, _pname, center in _same_net_pad_polygons(data, req.net):
        if layer not in players:
            continue
        is_end_a = abs(center[0] - pad_a_xy[0]) < 1e-6 and abs(center[1] - pad_a_xy[1]) < 1e-6
        is_end_b = abs(center[0] - pad_b_xy[0]) < 1e-6 and abs(center[1] - pad_b_xy[1]) < 1e-6
        # A thru-hole terminal pad carries copper on every layer (the
        # ``layer in players`` filter above already passed), so an
        # anchor chain may terminate on it from ANY of its copper
        # layers -- not only the layer ``_resolve_layers`` picked as
        # the nominal end.  An SMD pad only ever reaches this point on
        # its single fixed layer, so the exemption stays precise.
        if is_end_a or is_end_b:
            continue  # the route terminates on this pad
        engine_obstacles.append(
            Obstacle(shape=poly, layers=frozenset({layer}), net=req.net, kind="pad")
        )
    return engine_obstacles


# ---------------------------------------------------------------------------
# Anchor chain (waypoints / via anchors)
# ---------------------------------------------------------------------------

# Built-in micro-shift step for DRC-clean via site search.  Candidates
# walk outward ring by ring (0 -> axes -> diagonal, then the next ring),
# so the DRC-clean site closest to the request is always preferred.
_ANCHOR_SHIFT_STEP_MM = 0.2


def _anchor_pos(spec: dict, kind: str) -> tuple[float, float]:
    """Validate + return the ``pos`` of an anchor spec as (x, y)."""
    pos = spec.get("pos")
    if pos is None:
        raise RouteFailure(f"{kind} anchor is missing 'pos' (expected [x, y])")
    try:
        x, y = float(pos[0]), float(pos[1])
    except (TypeError, ValueError, IndexError):
        raise RouteFailure(f"{kind} anchor 'pos' must be a [x, y] pair, got {pos!r}") from None
    return (x, y)


def _waypoint_soft_end(
    wpt: tuple[float, float],
    tol_mm: float,
    obstacles: list[Obstacle],
    track_width: float,
    clearance: float,
) -> tuple[float, float] | None:
    """Pick a DRC-clean floating endpoint for a soft waypoint anchor.

    The leg endpoint drifts off ``wpt`` to ``wpt`` + a small offset inside
    the ``tol_mm``-radius tolerance circle, so the per-leg skeleton fillet
    arcs are no longer pinned onto the waypoint joint and the corner can
    round.  Candidates walk outward ring by ring (axes first, diagonals
    after, same order as the via-site search, so the closest clean spot
    wins) and must keep the track centerline at least ``clearance +
    track_width / 2`` away from every leg obstacle.  ``None`` when the
    whole tolerance circle is blocked -- the caller falls back to the
    exact waypoint anchor (legacy behavior)."""
    if tol_mm <= 0.0:
        return None
    from shapely.geometry import Point

    margin = clearance + track_width / 2.0
    max_ring = max(1, int(math.ceil(tol_mm / _ANCHOR_SHIFT_STEP_MM)))
    for ring in range(1, max_ring + 1):
        off = ring * _ANCHOR_SHIFT_STEP_MM
        candidates = (
            [(dx, 0.0) for dx in (-off, off)]
            + [(0.0, dy) for dy in (-off, off)]
            + [(dx, dy) for dx in (-off, off) for dy in (-off, off)]
        )
        for dx, dy in candidates:
            if math.hypot(dx, dy) > tol_mm + 1e-9:
                continue  # keep every endpoint strictly inside the circle
            cand = (wpt[0] + dx, wpt[1] + dy)
            pt = Point(cand)
            if all(float(o.shape.distance(pt)) >= margin for o in obstacles):
                return cand
    return None


def _fit_circle(pts: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares (Kasa) circle fit over ``pts``.

    Returns ``(cx, cy, r)`` of the best-fit circle.  Points that lie
    exactly on one circle recover its exact center and radius up to
    floating-point noise; fewer than 3 points, collinear runs, and
    coincident points return ``None`` (no circle is defined).
    """
    n = len(pts)
    if n < 3:
        return None
    nf = float(n)
    sx = sy = sxx = syy = sxy = 0.0
    for x, y in pts:
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    mx, my = sx / nf, sy / nf
    suu = sxx - 2.0 * mx * sx + nf * mx * mx
    svv = syy - 2.0 * my * sy + nf * my * my
    suv = sxy - mx * sy - my * sx + nf * mx * my
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    suuu = suuv = suvv2 = svvv = 0.0
    for x, y in pts:
        u = x - mx
        v = y - my
        u2 = u * u
        v2 = v * v
        suuu += u * u2
        suuv += u2 * v
        suvv2 += u * v2
        svvv += v * v2
    b = (suuu + suvv2) / 2.0
    c = (svvv + suuv) / 2.0
    cxc = mx + (b * svv - suv * c) / det
    cyc = my + (suu * c - suv * b) / det
    r = sum(math.hypot(x - cxc, y - cyc) for x, y in pts) / nf
    if r < 1e-9:
        return None
    return cxc, cyc, r


def _chain_cocircular_arc(
    chain_pts: list[tuple[float, float]],
    tol_mm: float,
) -> (
    tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        tuple[float, float, float, float, float],
    ]
    | None
):
    """Covering 3-point arc for a cocircular waypoint chain.

    ``chain_pts`` is the anchor chain in route order (start pad anchor,
    waypoints, end pad anchor).  Fits a least-squares circle and, when
    every anchor is within ``tol_mm`` of it and the start->end sweep
    through the chain's middle anchor contains every interior anchor,
    returns ``(arc_start, arc_mid, arc_end, (cx, cy, r, a0, span))``:
    ``arc_start``/``arc_end`` are the on-circle lead-out anchors (radial
    projections of the endpoint pad anchors), ``arc_mid`` the chain's
    middle anchor, and ``span`` the signed angular sweep from ``a0`` that
    the emitted arc covers (the direction chosen so it passes through
    ``arc_mid``, matching the renderer's start/mid/end convention).
    Returns ``None`` for degenerate or non-cocircular chains -- the
    caller falls back to the per-leg polygon path untouched.
    """
    if len(chain_pts) < 4:
        # Fewer than 4 anchors always fit some circle; not a meaningful
        # cocircularity signal.
        return None
    fitted = _fit_circle(chain_pts)
    if fitted is None:
        return None
    cxc, cyc, rad = fitted
    for x, y in chain_pts:
        if abs(math.hypot(x - cxc, y - cyc) - rad) > tol_mm + 1e-9:
            return None
    tau = 2.0 * math.pi
    start_pt, end_pt = chain_pts[0], chain_pts[-1]
    mid_pt = chain_pts[len(chain_pts) // 2]  # middle anchor: on the circle
    a0 = math.atan2(start_pt[1] - cyc, start_pt[0] - cxc)
    am = math.atan2(mid_pt[1] - cyc, mid_pt[0] - cxc)
    a1 = math.atan2(end_pt[1] - cyc, end_pt[0] - cxc)
    span = (a1 - a0) % tau
    mid_off = (am - a0) % tau
    if abs(span) < 1e-9:
        return None  # endpoints on the same ray: no arc direction
    if mid_off > span:
        span -= tau  # sweep the other way so the arc passes through mid
    # Endpoint anchors are the arc endpoints by construction (their
    # radial projections sit at a0/a1); every interior anchor must lie
    # inside the sweep or the arc would not cover the whole chain.
    for x, y in chain_pts[1:-1]:
        off = (math.atan2(y - cyc, x - cxc) - a0) % tau
        if span >= 0.0:
            if off > span + 1e-9:
                return None  # an anchor sits outside the sweep: invalid arc
        elif off < tau + span - 1e-9:
            return None
    arc_start = (cxc + rad * math.cos(a0), cyc + rad * math.sin(a0))
    arc_end = (cxc + rad * math.cos(a1), cyc + rad * math.sin(a1))
    return arc_start, mid_pt, arc_end, (cxc, cyc, rad, a0, span)


# ---------------------------------------------------------------------------
# Chain tangent fillets (G1): round every interior corner of a waypoint
# chain with an arc tangent to both adjacent legs
# ---------------------------------------------------------------------------

# Desired radius (mm) for the G1 chain fillet arcs.  A fillet replaces an
# interior corner of a waypoint chain by a tangent arc whose endpoints sit
# on the two legs; the radius is capped by the shorter leg (so the tangent
# points always stay on the edges) and halves on DRC conflict.
_CHAIN_FILLET_RADIUS_MM = 1.2
# Corner angles above this (degrees) count as straight: no fillet.
_CHAIN_FILLET_STRAIGHT_DEG = 170.0
# Below this radius (mm) a fillet is refused and the corner keeps its
# original sharp geometry rather than risking a DRC conflict.
_CHAIN_FILLET_MIN_RADIUS_MM = 0.01


def _chain_fillet_arc(
    a: tuple[float, float],
    joint: tuple[float, float],
    b: tuple[float, float],
    radius: float,
) -> (
    tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        float,
        tuple[float, float],
    ]
    | None
):
    """Tangent fillet arc for the corner ``a -> joint -> b``.

    Returns ``(arc_start, arc_mid, arc_end, r, center)``: the arc starts
    and ends exactly on the two legs at distance ``r * cot(theta/2)``
    from the joint (so it is tangent to both -- G1 continuous with the
    surrounding straight segments), ``arc_mid`` is the 3-point arc's mid
    (the circle point nearest the joint), ``r`` the used radius
    ``min(radius, short-leg cap)`` and ``center`` the curvature center.
    ``None`` when the corner is (near-)straight, degenerate, or too
    sharp for the requested radius -- the caller keeps the sharp corner.
    """
    la = math.hypot(a[0] - joint[0], a[1] - joint[1])
    lb = math.hypot(b[0] - joint[0], b[1] - joint[1])
    if la < 1e-9 or lb < 1e-9:
        return None
    ua = ((a[0] - joint[0]) / la, (a[1] - joint[1]) / la)
    ub = ((b[0] - joint[0]) / lb, (b[1] - joint[1]) / lb)
    cos_t = max(-1.0, min(1.0, ua[0] * ub[0] + ua[1] * ub[1]))
    theta = math.acos(cos_t)
    if math.degrees(theta) > _CHAIN_FILLET_STRAIGHT_DEG:
        return None  # near-straight: nothing to round
    half = theta / 2.0
    if math.sin(half) < 1e-12:
        return None  # (near-)backtracking: no sane tangent circle
    # Tangent offset L = r * cot(theta/2) must fit BOTH legs.
    r_cap = min(la, lb) * math.tan(half) * 0.999
    r = min(radius, r_cap)
    if r < _CHAIN_FILLET_MIN_RADIUS_MM:
        return None  # would round to a sliver: keep the sharp corner
    l_off = r * math.cos(half) / math.sin(half)
    t1 = (joint[0] + ua[0] * l_off, joint[1] + ua[1] * l_off)
    t2 = (joint[0] + ub[0] * l_off, joint[1] + ub[1] * l_off)
    # Center on the interior bisector, tangent to both legs.
    bis = (ua[0] + ub[0], ua[1] + ub[1])
    n = math.hypot(bis[0], bis[1])
    if n < 1e-12:
        return None
    bis = (bis[0] / n, bis[1] / n)
    dist = r / math.sin(half)
    c = (joint[0] + bis[0] * dist, joint[1] + bis[1] * dist)
    # Arc mid: the point on the arc closest to the joint (bisector of the
    # short arc between the two tangent points, which is the corner-side
    # arc; the radius vectors are < 180 deg apart, so the sum is nonzero).
    v1 = (t1[0] - c[0], t1[1] - c[1])
    v2 = (t2[0] - c[0], t2[1] - c[1])
    mv = (v1[0] + v2[0], v1[1] + v2[1])
    mn = math.hypot(mv[0], mv[1])
    if mn < 1e-12:
        return None
    mid = (c[0] + mv[0] * r / mn, c[1] + mv[1] * r / mn)
    return t1, mid, t2, r, c


def _auto_via_target(
    base_seq: list[str],
    pending_layer: str,
    site_req: tuple[float, float],
) -> str:
    """Pick the next layer of the resolved layer sequence after the
    current leg layer (the anchor-declared auto layer transition)."""
    if len(base_seq) < 2:
        raise RouteFailure(
            f"via anchor at {site_req} has no to_layer and the layer "
            f"sequence {base_seq} has no transition to auto-pick; pass "
            "to_layer explicitly"
        )
    if pending_layer not in base_seq:
        raise RouteFailure(
            f"via anchor at {site_req} cannot auto-pick a target layer: "
            f"current layer {pending_layer!r} is not on the layer sequence "
            f"{base_seq}; pass to_layer explicitly"
        )
    nxt = base_seq.index(pending_layer) + 1
    if nxt >= len(base_seq):
        raise RouteFailure(
            f"via anchor at {site_req} cannot auto-pick a target layer "
            f"after {pending_layer!r} on {base_seq}; pass to_layer explicitly"
        )
    return base_seq[nxt]


def _validate_via_transition(
    pending_layer: str,
    to_layer: str,
    site_req: tuple[float, float],
    req: RouteRequest,
    pcb_layers: list[str],
) -> None:
    """Reject via transitions that are not copper or not in via_pairs."""
    if to_layer not in pcb_layers:
        raise RouteFailure(
            f"via anchor at {site_req} to_layer {to_layer!r} is not a "
            f"declared PCB layer; layers are {pcb_layers}"
        )
    if to_layer == pending_layer:
        raise RouteFailure(
            f"via anchor at {site_req} has to_layer {to_layer!r} equal to the "
            "leg layer; a through-via must switch layers"
        )
    pair = (pending_layer, to_layer)
    allowed = any(pair == vp or pair[::-1] == vp for vp in req.via_pairs)
    if not allowed:
        raise RouteFailure(
            f"via anchor at {site_req} layer transition {pending_layer!r} -> "
            f"{to_layer!r} is not in via_pairs {list(req.via_pairs)}"
        )


def _pick_explicit_via_site(
    *,
    pcb_path: str,
    requested: tuple[float, float],
    from_layer: str,
    to_layer: str,
    via_diameter: float,
    via_drill: float,
    clearance: float,
    net: str,
    via_forbidden: list,
    tol_mm: float,
) -> tuple[float, float]:
    """DRC-clean an explicit via anchor site.

    Checks the requested spot first (plus same-net pad faces, which the
    DRC alone does not see); when it is not clean, micro-shifts along the
    ``_pick_via_positions`` offset order, outward ring by ring, up to
    ``tol_mm``.  Raises :class:`RouteFailure` when no spot inside the
    tolerance is clean -- the caller renders the blocking copper.
    """
    candidates: list[tuple[float, float]] = [(0.0, 0.0)]
    max_ring = max(1, int(math.ceil(tol_mm / _ANCHOR_SHIFT_STEP_MM)))
    for ring in range(1, max_ring + 1):
        off = ring * _ANCHOR_SHIFT_STEP_MM
        for dx in (-off, off):
            candidates.append((dx, 0.0))
        for dy in (-off, off):
            candidates.append((0.0, dy))
        for dx in (-off, off):
            for dy in (-off, off):
                candidates.append((dx, dy))
    from shapely.geometry import Point

    batch = [
        ProposedVia(
            x=requested[0] + dx,
            y=requested[1] + dy,
            diameter=via_diameter,
            drill=via_drill,
            layers=(from_layer, to_layer),
            net=net,
        )
        for dx, dy in candidates
    ]
    violations = check_vias(pcb_path, batch)
    bad = {v.index for v in violations}
    for idx, (dx, dy) in enumerate(candidates):
        if idx in bad:
            continue
        pt = Point(requested[0] + dx, requested[1] + dy)
        if any(poly.contains(pt) or poly.touches(pt) for poly in via_forbidden):
            continue
        return (float(pt.x), float(pt.y))
    raise RouteFailure(
        f"no DRC-clean via spot near requested ({requested[0]:.3f}, "
        f"{requested[1]:.3f}) within tolerance {tol_mm}mm on {from_layer} -> "
        f"{to_layer}; move the via anchor or raise its tol_mm"
    )


def _render_route_failure_evidence(
    pcb_path: str,
    *,
    chain: list[tuple[float, float]],
    attempted_end: tuple[float, float],
    layer: str,
    obstacles: list[Obstacle],
) -> str | None:
    """Render the failed route attempt to a PNG next to the .kicad_pcb.

    Best-effort: any rendering problem degrades to ``None`` so the
    original :class:`RouteFailure` is never masked.  Blocking items are
    the engine obstacles of the failed leg (red rings, all shapes at most
    one per footprint/track), plus the attempted chain (blue) ending at
    the goal of the failed leg.
    """
    try:
        from kcaa.tools.render_route_state import BlockingEvidence, render_route_attempt

        blockers: list[BlockingEvidence] = []
        for ob in obstacles[:40]:
            shape = ob.shape
            if shape is None or shape.is_empty:
                continue
            if shape.geom_type == "Polygon":
                pts = [(float(x), float(y)) for x, y in shape.exterior.coords]
            elif shape.geom_type == "MultiPolygon":
                biggest = max(shape.geoms, key=lambda g: g.area)
                pts = [(float(x), float(y)) for x, y in biggest.exterior.coords]
            else:
                continue
            kind = "via" if ob.kind == "via" else "footprint" if ob.kind == "pad" else "track"
            blockers.append(
                BlockingEvidence(
                    ref=ob.ref or "obstacle",
                    net=ob.net,
                    layer=layer,
                    point=pts[0],
                    kind=kind,
                    points=pts,
                )
            )
        attempted_path = [*chain, attempted_end]
        lines, png, _report = render_route_attempt(
            pcb_path,
            attempted_path=attempted_path,
            blocking_items=blockers,
            anchors=chain,
        )
        if not png:
            return None
        fname = f"kcaa_route_failure_{time.time_ns()}_{os.getpid()}.png"
        out = os.path.join(tempfile.gettempdir(), fname)
        with open(out, "wb") as fh:
            fh.write(png)
        return out
    except Exception:  # evidence rendering must never mask the real error
        return None


def _route_polyline(
    segs: list[OutputSegment],
    arcs: list[OutputArc],
) -> list[tuple[float, float]]:
    """Recompose the routed polyline from emitted output geometry.

    Output segments carry the polyline in route order; rounded-corner
    arcs (``OutputArc`` start/mid/end) sit at segment joints.  Arcs whose
    start coincides with the current chain head (or the current segment
    start) are threaded through their mid point so the render shows the
    fillet rather than a chord.  Without arcs (A*, mitered) this is a
    straight segment chain.
    """
    tol = 1e-3  # arc endpoints are aligned to the skeleton grid, not exact
    pts: list[tuple[float, float]] = []
    unused = list(arcs)
    for s in segs:
        if not pts:
            pts.append((s.x1, s.y1))
        pts.append((s.x2, s.y2))
        while unused and _pt_eq((unused[0].start[0], unused[0].start[1]), pts[-1], tol):
            a = unused.pop(0)
            pts.append((a.mid[0], a.mid[1]))
            pts.append((a.end[0], a.end[1]))
    for a in unused:  # stragglers (should not happen; keep the geometry)
        pts.append((a.start[0], a.start[1]))
        pts.append((a.mid[0], a.mid[1]))
        pts.append((a.end[0], a.end[1]))
    return pts


def _render_route_png(
    pcb_path: str,
    *,
    pts: list[tuple[float, float]],
    anchors: list[tuple[float, float]],
) -> str | None:
    """Best-effort single-route render for the VLM feedback loop.

    Renders the routed polyline on the board (grey track + green anchor
    dots) and returns the PNG path; ``None`` when rendering fails —
    success rendering must never mask the route result.
    """
    try:
        from kcaa.tools.render_route_state import render_route_attempt

        _lines, png, _report = render_route_attempt(
            pcb_path,
            attempted_path=pts,
            blocking_items=[],
            anchors=anchors,
        )
        if not png:
            return None
        fname = f"kcaa_route_{time.time_ns()}_{os.getpid()}.png"
        out = os.path.join(tempfile.gettempdir(), fname)
        with open(out, "wb") as fh:
            fh.write(png)
        return out
    except Exception:  # rendering must never mask the route result
        return None


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


def _find_pad_net(
    data: list,
    ref: str,
    pad_name: str,
    layer: str | None = None,
) -> str | None:
    """Return the net name of the named pad on the given footprint ref.

    When ``layer`` is given, only pads whose copper covers that layer are
    considered, matching :func:`_find_pad_center` semantics for footprints
    that declare several pads with the same name.
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
        if layer is not None and layer not in _pad_layers(sub):
            continue
        return _get_net(sub)
    return None


def _find_pad_center(
    data: list,
    ref: str,
    pad_name: str,
    layer: str | None = None,
) -> tuple[float, float] | None:
    """Return the (x, y) center of the named pad on the given footprint ref.

    When ``layer`` is given, only pads whose copper covers that layer are
    considered -- a footprint may declare several pads with the same name
    (e.g. edge-connector fingers sharing a net), and the center must match
    the pad whose shape :func:`_find_pad_size` would return.
    """
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
        if layer is not None and layer not in _pad_layers(sub):
            continue
        # Pad ``at`` is in footprint-local coords.
        at = _get_sub(sub, "at")
        if at is None or len(at) < 3:
            return None
        try:
            px, py = float(at[1]), float(at[2])
        except (TypeError, ValueError):
            return None
        # Transform local -> world (only translation + rotation; pads don't
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

    A footprint may declare several pads with the same name (e.g. edge-
    connector fingers sharing a net). Returns the first pad whose copper
    covers ``layer``, and ``None`` only if no same-named pad is on it.
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
            # Another pad with this name may carry the requested layer.
            continue
        size_sub = _get_sub(sub, "size")
        if size_sub is None or len(size_sub) < 3:
            continue
        try:
            return float(size_sub[1]), float(size_sub[2])
        except (TypeError, ValueError):
            continue
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


def _pad_type(pad_node: list) -> str:
    """Return the pad type: ``thru_hole``, ``smd``, or ``connect``."""
    if len(pad_node) > 2:
        return str(pad_node[2])
    return ""


def _find_pad_node(
    data: list,
    ref: str,
    pad_name: str,
    layer: str | None = None,
) -> list | None:
    """Return the raw pad node for ``ref``/``pad_name``.

    When ``layer`` is given, only pads whose copper covers that layer
    are considered (same logic as :func:`_find_pad_center`).
    """
    fp = _find_footprint(data, ref)
    if fp is None:
        return None
    for sub in fp:
        if not _is_list(sub) or str(sub[0]) != "pad":
            continue
        if _get_pad_name(sub) != pad_name:
            continue
        if layer is not None and layer not in _pad_layers(sub):
            continue
        return sub
    return None


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


def _resolve_layers(
    data: list,
    req: RouteRequest,
) -> tuple[str, str]:
    """Auto-pick start/end copper layers from pad types and ``layer_hint``.

    For SMD/connect pads the layer is fixed by the pad itself (it has
    copper on exactly one copper layer).  For thru-hole pads (``*.Cu``)
    the router picks the best shared copper layer, preferring
    ``layer_hint`` when it is among the pad's copper layers.

    When a footprint declares several pads with the same name (edge
    connectors), the union of all same-named pads' copper layers is
    used -- a THT pad named ``3v3`` makes the pad flexible across all
    copper layers even if the first same-named pad is SMD.

    Returns ``(start_layer, end_layer)``.

    Raises :class:`RouteFailure` if either pad has no copper on any layer.
    When the two pads share no copper layer and ``layer_hint`` is neither
    pad's preferred layer, each pad falls back to its first available
    copper layer; the returned layers may then differ (staggered).
    """
    fp_a = _find_footprint(data, req.ref_a)
    fp_b = _find_footprint(data, req.ref_b)
    if fp_a is None:
        raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} not found")
    if fp_b is None:
        raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} not found")

    # Collect ALL same-named pad nodes (a footprint may declare several
    # pads with one name -- edge-connector fingers + a THT pad).
    a_nodes = [
        s for s in fp_a if _is_list(s) and str(s[0]) == "pad" and _get_pad_name(s) == req.pad_a
    ]
    b_nodes = [
        s for s in fp_b if _is_list(s) and str(s[0]) == "pad" and _get_pad_name(s) == req.pad_b
    ]
    if not a_nodes:
        raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} not found")
    if not b_nodes:
        raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} not found")

    # Union of copper layers across all same-named pads, and detect
    # whether any pad is THT (flexible) vs all SMD/connect (fixed).
    pcb_copper = [l for l in _pcb_layer_names(data) if l.endswith(".Cu")]
    a_layers: list[str] = []
    b_layers: list[str] = []
    a_has_tht = b_has_tht = False
    for node in a_nodes:
        if _pad_type(node) == "thru_hole":
            a_has_tht = True
        for l in _pad_layers(node):
            if l.endswith(".Cu") and ".Mask" not in l and l not in a_layers:
                a_layers.append(l)
    for node in b_nodes:
        if _pad_type(node) == "thru_hole":
            b_has_tht = True
        for l in _pad_layers(node):
            if l.endswith(".Cu") and ".Mask" not in l and l not in b_layers:
                b_layers.append(l)
    # Filter THT layers to PCB's actual copper layers.
    if a_has_tht:
        a_layers = [l for l in a_layers if l in pcb_copper]
    if b_has_tht:
        b_layers = [l for l in b_layers if l in pcb_copper]
    if not a_layers:
        raise RouteFailure(f"Pad {req.ref_a}/{req.pad_a} has no copper layer")
    if not b_layers:
        raise RouteFailure(f"Pad {req.ref_b}/{req.pad_b} has no copper layer")

    # A pad is "fixed" only when ALL same-named pads are SMD/connect.
    a_fixed = not a_has_tht
    b_fixed = not b_has_tht

    # Determine start layer.
    if a_fixed:
        start_layer = a_layers[0]
    elif req.layer_hint and req.layer_hint in a_layers:
        start_layer = req.layer_hint
    else:
        start_layer = None

    # Determine end layer.
    if b_fixed:
        end_layer = b_layers[0]
    elif req.layer_hint and req.layer_hint in b_layers:
        end_layer = req.layer_hint
    else:
        end_layer = None

    # For THT pads without a fixed layer, pick a shared copper layer.
    if start_layer is None or end_layer is None:
        shared = [l for l in a_layers if l in b_layers]
        if not shared:
            if start_layer is None:
                start_layer = (
                    req.layer_hint
                    if (req.layer_hint and req.layer_hint in a_layers)
                    else a_layers[0]
                )
            if end_layer is None:
                end_layer = (
                    req.layer_hint
                    if (req.layer_hint and req.layer_hint in b_layers)
                    else b_layers[0]
                )
        else:
            chosen = None
            if req.layer_hint and req.layer_hint in shared:
                chosen = req.layer_hint
            else:
                chosen = shared[0]
            if start_layer is None:
                start_layer = chosen
            if end_layer is None:
                end_layer = chosen

    return start_layer, end_layer


def _same_net_pad_polygons(
    data: list,
    net: str,
) -> list[tuple[object, frozenset[str], str, str, tuple[float, float]]]:
    """Return ``(world polygon, copper layers, ref, pad name, world center)``
    for every pad carrying ``net``.

    The world model drops same-net copper so the route can land on its
    own endpoint pads; the router re-adds those pads here as *transit*
    obstacles -- a track may terminate on a pad, never run across its
    copper (a track through a thru-hole pad's hole is cut by the drill,
    and a via on a pad face is a DFM defect).  Geometry mirrors
    :func:`kcaa.router.world_model._pad_obstacle`.

    The world center lets callers distinguish the *endpoint pad instance*
    from other same-named pads by geometry -- two pads on the same net
    can share ``(ref, pad name)`` (edge connectors with multiple
    ``3v3`` fingers), so name-based skip would wrongly exempt every
    same-named pad.
    """
    out: list[tuple[object, frozenset[str], str, str, tuple[float, float]]] = []
    for item in data:
        if not _is_list(item) or str(item[0]) != "footprint":
            continue
        fp_x, fp_y, fp_rot = _node_at3(item)
        ref = ""
        for sub in item:
            if (
                _is_list(sub)
                and str(sub[0]) == "property"
                and len(sub) >= 3
                and str(sub[1]) == "Reference"
            ):
                ref = sub[2] if isinstance(sub[2], str) else str(sub[2])
        for sub in item:
            if not _is_list(sub) or str(sub[0]) != "pad":
                continue
            if _get_net(sub) != net:
                continue
            if len(sub) > 2 and str(sub[2]) == "np_thru_hole":
                continue  # bare mechanical hole -- no copper to protect
            players = _pad_layers(sub)
            if not players:
                continue
            size = _get_sub(sub, "size")
            if size is None or len(size) < 3:
                continue
            try:
                pw, ph = float(size[1]), float(size[2])
            except (TypeError, ValueError):
                continue
            at = _get_sub(sub, "at")
            if at is None or len(at) < 3:
                continue
            try:
                lx, ly = float(at[1]), float(at[2])
            except (TypeError, ValueError):
                continue
            wx_off, wy_off = _rotate(lx, ly, fp_rot)
            wx, wy = fp_x + wx_off, fp_y + wy_off
            hw, hh = pw / 2.0, ph / 2.0
            rad = math.radians(fp_rot)
            c, s = math.cos(rad), math.sin(rad)
            corners = [
                (c * -hw + s * -hh, -s * -hw + c * -hh),
                (c * hw + s * -hh, -s * hw + c * -hh),
                (c * hw + s * hh, -s * hw + c * hh),
                (c * -hw + s * hh, -s * -hw + c * hh),
            ]
            poly = Polygon([(wx + cx, wy + cy) for cx, cy in corners])
            if poly.is_empty or not poly.is_valid:
                continue
            out.append((poly, frozenset(players), ref, _get_pad_name(sub), (wx, wy)))
    return out


# ---------------------------------------------------------------------------
# DRC defaults (lightweight: read the netclass table from the .kicad_pro)
# ---------------------------------------------------------------------------


def _default_track_width(pcb_path: str, net: str) -> float:
    """Resolve track width for ``net`` from the project's netclass settings.

    Reads the matching ``.kicad_pro`` and looks up the netclass that
    ``net`` belongs to (via ``netclass_patterns``). Returns that netclass's
    ``track_width``.

    Raises:
        ProFileMissing: No ``.kicad_pro`` next to ``pcb_path`` -- pass
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

    When the board's ``min_clearance`` is 0.0 (which is common -- KiCad
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
    """Read net->netclass assignments from the JSON project file.

    KiCad's project file uses ``netclass_patterns`` with a ``pattern`` glob --
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
    # the set of net names -- but for our purposes (looking up a single
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
    """Rotate (x, y) by ``deg`` (CCW-positive on screen, KiCad PCB convention).

    A positive KiCad file rotation is counter-clockwise on screen
    (0=right, 90=up, 180=left, 270=down), i.e. the -deg rotation in y-up
    math.  Substituting cos(-d) = cos(d), sin(-d) = -sin(d) into the
    standard CCW matrix gives:

        x' =  x*cos(d) + y*sin(d)
        y' = -x*sin(d) + y*cos(d)

    This matches the formula used by
    :func:`kcaa.utils.pcb_board_utils.get_fp_courtyard_bbox` and other
    PCB geometry helpers in the codebase.
    """
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return c * x + s * y, -s * x + c * y
