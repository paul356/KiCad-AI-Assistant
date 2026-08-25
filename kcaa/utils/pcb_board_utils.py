"""
Utility helpers for PCB board-level geometry: Edge.Cuts management and
footprint courtyard bounding boxes.

Coordinate convention (all functions here and in callers):
  - Millimetres, +X right, **+Y down**, rotation **clockwise-positive**
    (KiCad PCB convention).
"""

import math
from typing import Any

import sexpdata

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sym(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain string."""
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    return str(value)


def _make_sym(name: str) -> sexpdata.Symbol:
    return sexpdata.Symbol(name)


def _get_layer(node: list[Any]) -> str | None:
    """Return the layer string of a graphic node, or None."""
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
            return sub[1] if isinstance(sub[1], str) else _sym(sub[1])
    return None


_GRAPHIC_TYPES = {"gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_curve"}
_EDGE_CUTS_NAMES = {"Edge.Cuts", "Edge_Cuts"}


def _is_edge_cuts(layer: str | None) -> bool:
    return layer in _EDGE_CUTS_NAMES


# ---------------------------------------------------------------------------
# Edge.Cuts read / write
# ---------------------------------------------------------------------------


def get_edge_cuts_items(data: list[Any]) -> list[dict[str, Any]]:
    """Return a list of dicts describing all graphic items on Edge.Cuts.

    Each dict has at minimum ``type`` (e.g. ``"gr_line"``) and ``layer``
    (``"Edge.Cuts"``).  Additional keys depend on the item type:

    - ``gr_line``: ``x1``, ``y1``, ``x2``, ``y2``, ``width``
    - ``gr_rect``: ``x1``, ``y1``, ``x2``, ``y2``, ``width``
    - ``gr_arc``: ``start_x``, ``start_y``, ``mid_x``, ``mid_y``,
      ``end_x``, ``end_y``, ``width``
    - ``gr_circle``: ``cx``, ``cy``, ``ex``, ``ey``, ``width`` (end point
      on circumference as stored by KiCad)

    :param data: Parsed PCB S-expression tree (from load_pcb).
    :returns: List of graphic-item dicts on Edge.Cuts.
    """
    result = []
    for item in data:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        kind = _sym(item[0])
        if kind not in _GRAPHIC_TYPES:
            continue
        layer = _get_layer(item)
        if not _is_edge_cuts(layer):
            continue

        info: dict[str, Any] = {"type": kind, "layer": layer}

        if kind == "gr_line" or kind == "gr_rect":
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "start":
                        info["x1"], info["y1"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["x2"], info["y2"] = float(sub[1]), float(sub[2])
                    elif k == "stroke":
                        for ssub in sub:
                            if isinstance(ssub, list) and _sym(ssub[0]) == "width":
                                info["width"] = float(ssub[1])
                elif isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "width":
                    info.setdefault("width", float(sub[1]))

        elif kind == "gr_arc":
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "start":
                        info["start_x"], info["start_y"] = float(sub[1]), float(sub[2])
                    elif k == "mid":
                        info["mid_x"], info["mid_y"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["end_x"], info["end_y"] = float(sub[1]), float(sub[2])
                    elif k == "stroke":
                        for ssub in sub:
                            if isinstance(ssub, list) and _sym(ssub[0]) == "width":
                                info["width"] = float(ssub[1])
                elif isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "width":
                    info.setdefault("width", float(sub[1]))

        elif kind == "gr_circle":
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "center":
                        info["cx"], info["cy"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["ex"], info["ey"] = float(sub[1]), float(sub[2])
                    elif k == "stroke":
                        for ssub in sub:
                            if isinstance(ssub, list) and _sym(ssub[0]) == "width":
                                info["width"] = float(ssub[1])
                elif isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "width":
                    info.setdefault("width", float(sub[1]))

        result.append(info)
    return result


def remove_edge_cuts_items(data: list[Any]) -> int:
    """Remove all graphic items on the Edge.Cuts layer from *data* in-place.

    :param data: Parsed PCB S-expression tree (mutated in place).
    :returns: Number of items removed.
    """
    to_remove = []
    for item in data:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        if _sym(item[0]) not in _GRAPHIC_TYPES:
            continue
        if _is_edge_cuts(_get_layer(item)):
            to_remove.append(item)
    for item in to_remove:
        data.remove(item)
    return len(to_remove)


def add_gr_line(
    data: list[Any],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 0.05,
    layer: str = "Edge.Cuts",
) -> None:
    """Append a ``gr_line`` node to the PCB S-expression tree.

    :param data: Parsed PCB tree (mutated in place).
    :param x1: Start X in mm.
    :param y1: Start Y in mm (+Y down).
    :param x2: End X in mm.
    :param y2: End Y in mm (+Y down).
    :param width: Line width in mm (default 0.05 mm for Edge.Cuts).
    :param layer: Target layer (default ``"Edge.Cuts"``).
    """
    node = [
        _make_sym("gr_line"),
        [_make_sym("start"), x1, y1],
        [_make_sym("end"), x2, y2],
        [_make_sym("stroke"), [_make_sym("width"), width], [_make_sym("type"), _make_sym("solid")]],
        [_make_sym("layer"), layer],
    ]
    data.append(node)


def add_gr_rect(
    data: list[Any],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 0.05,
    layer: str = "Edge.Cuts",
) -> None:
    """Append a ``gr_rect`` node (axis-aligned rectangle) to the PCB tree.

    :param data: Parsed PCB tree (mutated in place).
    :param x1: Top-left X in mm.
    :param y1: Top-left Y in mm (+Y down, so y1 < y2 for top).
    :param x2: Bottom-right X in mm.
    :param y2: Bottom-right Y in mm.
    :param width: Line width in mm.
    :param layer: Target layer (default ``"Edge.Cuts"``).
    """
    node = [
        _make_sym("gr_rect"),
        [_make_sym("start"), x1, y1],
        [_make_sym("end"), x2, y2],
        [_make_sym("stroke"), [_make_sym("width"), width], [_make_sym("type"), _make_sym("solid")]],
        [_make_sym("fill"), _make_sym("none")],
        [_make_sym("layer"), layer],
    ]
    data.append(node)


def add_gr_arc(
    data: list[Any],
    cx: float,
    cy: float,
    radius: float,
    start_angle_deg: float,
    end_angle_deg: float,
    width: float = 0.05,
    layer: str = "Edge.Cuts",
) -> None:
    """Append a ``gr_arc`` node using KiCad 6+ 3-point (start/mid/end) format.

    Angles follow KiCad PCB convention: 0° is the positive-X direction;
    angles increase **clockwise** (+Y down).

    :param data: Parsed PCB tree (mutated in place).
    :param cx: Arc centre X in mm.
    :param cy: Arc centre Y in mm.
    :param radius: Arc radius in mm.
    :param start_angle_deg: Arc start angle in degrees (clockwise from +X).
    :param end_angle_deg: Arc end angle in degrees (clockwise from +X).
    :param width: Line width in mm.
    :param layer: Target layer.
    """

    # In KiCad's +Y-down coordinate system CW angles map directly to standard
    # trig: a point at θ° CW from +X lies at (cx + r·cosθ, cy + r·sinθ).
    # No negation is needed; sin(θ) is positive downward, matching +Y-down.
    def _pt(angle_cw_deg: float) -> tuple[float, float]:
        rad = math.radians(angle_cw_deg)
        return cx + radius * math.cos(rad), cy + radius * math.sin(rad)

    # Midpoint angle going CW from start to end.
    # Normalize the arc span to [0°, 360°) so wrap-around arcs (e.g.
    # start=315°, end=45°) pick the midpoint of the short arc, not the
    # complementary 270° arc.
    span = (end_angle_deg - start_angle_deg) % 360.0
    if span == 0.0:
        span = 360.0
    mid_angle_cw = start_angle_deg + span / 2.0

    sx, sy = _pt(start_angle_deg)
    mx, my = _pt(mid_angle_cw)
    ex, ey = _pt(end_angle_deg)

    node = [
        _make_sym("gr_arc"),
        [_make_sym("start"), round(sx, 6), round(sy, 6)],
        [_make_sym("mid"), round(mx, 6), round(my, 6)],
        [_make_sym("end"), round(ex, 6), round(ey, 6)],
        [_make_sym("stroke"), [_make_sym("width"), width], [_make_sym("type"), _make_sym("solid")]],
        [_make_sym("layer"), layer],
    ]
    data.append(node)


# ---------------------------------------------------------------------------
# Footprint courtyard / bounding box
# ---------------------------------------------------------------------------

_COURTYARD_LAYERS = {
    "F.Courtyard",
    "B.Courtyard",
    "F_Courtyard",
    "B_Courtyard",
    "F.CrtYd",
    "B.CrtYd",
}
_FP_GRAPHIC_TYPES = {"fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_curve"}


def get_fp_courtyard_bbox(
    fp_node: list[Any],
    fp_x: float,
    fp_y: float,
    fp_rot_deg: float,
) -> dict[str, float] | None:
    """Compute the world-coordinate axis-aligned bounding box of a footprint's courtyard.

    Transforms all courtyard graphic endpoints by the footprint rotation
    (clockwise-positive, Y-down) and returns an AABB in board world coordinates.

    Falls back to scanning *all* ``fp_line``/``fp_rect``/``fp_circle`` items if
    no courtyard-layer items are found.  Returns ``None`` if the footprint has
    no usable geometry.

    :param fp_node: A footprint S-expression list node.
    :param fp_x: Footprint anchor X in mm (world).
    :param fp_y: Footprint anchor Y in mm (world, +Y down).
    :param fp_rot_deg: Footprint rotation in degrees, clockwise-positive.
    :returns: Dict ``{min_x, min_y, max_x, max_y, width, height}`` in mm, or None.
    """
    theta = math.radians(fp_rot_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    def _to_world(lx: float, ly: float) -> tuple[float, float]:
        # Convert a footprint-local point (lx, ly) to board world coordinates.
        #
        # Variables:
        #   fp_x, fp_y — footprint anchor in world mm (the "(at X Y rot)" field).
        #                +X is right, +Y is DOWN (KiCad screen convention).
        #   lx, ly     — coordinates of the point in the footprint's LOCAL frame,
        #                i.e. relative to the footprint anchor before any rotation.
        #                +lx is "right when the footprint is at 0°",
        #                +ly is "down when the footprint is at 0°".
        #   fp_rot_deg — KiCad rotation in CLOCKWISE-positive degrees
        #                (0° = no rotation; 90° = rotated 90° clockwise on screen).
        #
        # Why the standard CCW formula is WRONG here:
        #   The textbook CCW formula is:
        #       wx = fp_x + lx*cos(θ) - ly*sin(θ)
        #       wy = fp_y + lx*sin(θ) + ly*cos(θ)
        #   KiCad's Y axis points DOWN, so a positive (clockwise) rotation of θ
        #   is equivalent to a COUNTER-clockwise rotation of −θ in standard
        #   right-handed math.  Substituting −θ:
        #       cos(−θ) =  cos(θ)   sin(−θ) = −sin(θ)
        #   gives the correct CW formula:
        #       wx = fp_x + lx*cos(θ) + ly*sin(θ)   ← sign on ly*sin flipped
        #       wy = fp_y - lx*sin(θ) + ly*cos(θ)   ← sign on lx*sin flipped
        #
        # Quick sanity check — footprint at (101,95), rotation=270°:
        #   cos(270°)≈0, sin(270°)≈−1
        #   CW formula:  wx = 101 + lx*0 + ly*(−1) = 101 − ly
        #                wy =  95 − lx*(−1) + ly*0 =  95 + lx
        #   For a corner at local (5.32, −5.27):
        #       wx = 101 − (−5.27) = 106.27   ← correctly to the right of centre
        #       wy =  95 +   5.32  = 100.32
        wx = fp_x + lx * cos_t + ly * sin_t
        wy = fp_y - lx * sin_t + ly * cos_t
        return wx, wy

    points: list[tuple[float, float]] = []

    def _collect_fp_item(sub: list[Any], require_courtyard: bool) -> None:
        if not (isinstance(sub, list) and len(sub) > 0):
            return
        kind = _sym(sub[0])
        if kind not in _FP_GRAPHIC_TYPES:
            return
        layer: str | None = None
        for ssub in sub:
            if isinstance(ssub, list) and len(ssub) >= 2 and _sym(ssub[0]) == "layer":
                layer = ssub[1] if isinstance(ssub[1], str) else _sym(ssub[1])
        if require_courtyard and layer not in _COURTYARD_LAYERS:
            return

        if kind in ("fp_line", "fp_rect"):
            start_x = start_y = end_x = end_y = 0.0
            for ssub in sub:
                if isinstance(ssub, list) and len(ssub) >= 3:
                    k = _sym(ssub[0])
                    if k == "start":
                        start_x, start_y = float(ssub[1]), float(ssub[2])
                    elif k == "end":
                        end_x, end_y = float(ssub[1]), float(ssub[2])
            points.append(_to_world(start_x, start_y))
            points.append(_to_world(end_x, end_y))

        elif kind == "fp_circle":
            cx = cy = ex = ey = 0.0
            for ssub in sub:
                if isinstance(ssub, list) and len(ssub) >= 3:
                    k = _sym(ssub[0])
                    if k == "center":
                        cx, cy = float(ssub[1]), float(ssub[2])
                    elif k == "end":
                        ex, ey = float(ssub[1]), float(ssub[2])
            r = math.hypot(ex - cx, ey - cy)
            # A circle of radius r centred at (cx, cy) in footprint-local space
            # always extends exactly ±r in world space regardless of footprint
            # rotation.  Rotate only the centre and then add the world-space
            # cardinal offsets directly.
            wcx, wcy = _to_world(cx, cy)
            for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
                points.append((wcx + dx, wcy + dy))

        elif kind == "fp_arc":
            for ssub in sub:
                if isinstance(ssub, list) and len(ssub) >= 3:
                    k = _sym(ssub[0])
                    if k in ("start", "mid", "end"):
                        points.append(_to_world(float(ssub[1]), float(ssub[2])))

    # First pass: courtyard only
    for sub in fp_node:
        _collect_fp_item(sub, require_courtyard=True)

    # Fallback: all fp graphics
    if not points:
        for sub in fp_node:
            _collect_fp_item(sub, require_courtyard=False)

    if not points:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return {
        "min_x": round(min_x, 6),
        "min_y": round(min_y, 6),
        "max_x": round(max_x, 6),
        "max_y": round(max_y, 6),
        "width": round(max_x - min_x, 6),
        "height": round(max_y - min_y, 6),
    }


# ---------------------------------------------------------------------------
# Footprint Edge.Cuts graphic items
# ---------------------------------------------------------------------------


def get_fp_edge_cuts_items(fp_node: list[Any]) -> list[dict[str, Any]]:
    """Return all ``fp_*`` graphic items on Edge.Cuts inside a footprint.

    Scans the footprint's content items for ``fp_line``/``fp_rect``/
    ``fp_arc``/``fp_circle``/``fp_curve`` whose layer is ``Edge.Cuts`` (or
    the legacy ``Edge_Cuts`` spelling) and returns their geometry in
    **footprint-local coordinates** (mm, same frame as pads' ``local_x``/
    ``local_y``).  Transform to board world coordinates the same way as pads:
    ``world = fp.(x,y) + rotation(local)`` (clockwise-positive, +Y down).

    Additional keys depend on the item type, mirroring
    :func:`get_edge_cuts_items`:

    - ``fp_line`` / ``fp_rect``: ``x1``, ``y1``, ``x2``, ``y2``, ``width``
    - ``fp_arc``: ``start_x``, ``start_y``, ``mid_x``, ``mid_y``,
      ``end_x``, ``end_y``, ``width``
    - ``fp_circle``: ``cx``, ``cy``, ``ex``, ``ey``, ``width``
      (``(end ...)`` is a point on the circumference as stored by KiCad)
    - ``fp_curve``: ``pts`` (list of ``(x, y)`` tuples), ``width``

    :param fp_node: A footprint S-expression list node — i.e. the list of
        content items such as a placed footprint found via ``find_footprint``
        or the parsed children of a ``.kicad_mod`` file.
    :returns: List of graphic-item dicts on the footprint's Edge.Cuts layer.
    """

    def _stroke_width(node: list[Any]) -> float | None:
        """Return the stroke width of a graphic node, or None."""
        for sub in node:
            if isinstance(sub, list) and _sym(sub[0]) == "stroke":
                for ssub in sub:
                    if isinstance(ssub, list) and len(ssub) >= 2 and _sym(ssub[0]) == "width":
                        return float(ssub[1])
            elif isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "width":
                return float(sub[1])
        return None

    result: list[dict[str, Any]] = []
    for item in fp_node:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        kind = _sym(item[0])
        if kind not in _FP_GRAPHIC_TYPES:
            continue
        layer = _get_layer(item)
        if not _is_edge_cuts(layer):
            continue

        info: dict[str, Any] = {"type": kind, "layer": layer}

        if kind in ("fp_line", "fp_rect"):
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "start":
                        info["x1"], info["y1"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["x2"], info["y2"] = float(sub[1]), float(sub[2])

        elif kind == "fp_arc":
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "start":
                        info["start_x"], info["start_y"] = float(sub[1]), float(sub[2])
                    elif k == "mid":
                        info["mid_x"], info["mid_y"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["end_x"], info["end_y"] = float(sub[1]), float(sub[2])

        elif kind == "fp_circle":
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3:
                    k = _sym(sub[0])
                    if k == "center":
                        info["cx"], info["cy"] = float(sub[1]), float(sub[2])
                    elif k == "end":
                        info["ex"], info["ey"] = float(sub[1]), float(sub[2])

        elif kind == "fp_curve":
            pts: list[tuple[float, float]] = []
            for sub in item:
                if not (isinstance(sub, list) and _sym(sub[0]) == "pts"):
                    continue
                for pt in sub[1:]:
                    if isinstance(pt, list) and len(pt) >= 3 and _sym(pt[0]) == "xy":
                        pts.append((float(pt[1]), float(pt[2])))
            if pts:
                info["pts"] = pts

        width = _stroke_width(item)
        if width is not None:
            info["width"] = width
        result.append(info)
    return result
