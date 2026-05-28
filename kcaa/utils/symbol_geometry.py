"""Geometry helpers for KiCad library symbols.

Computes per-unit bounding boxes in **library coordinate space** (Y-up,
mm) and transforms them into **world / schematic coordinate space**
(Y-down, mm) for placed symbol instances.

The bbox returned by :func:`compute_unit_bboxes` is the union of:

* graphic primitives (rectangle, polyline, arc, circle) of the requested
  body style, and
* pin connection points (the ``(at x y angle)`` of each pin).

Pin connection points are included so that callers using the bbox for
placement clearance never overlap a neighbour's pin tip.

All numeric values are rounded to 4 decimals (sub-micron) to keep
serialised output stable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

import sexpdata

# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return round(self.max_x - self.min_x, 4)

    @property
    def height(self) -> float:
        return round(self.max_y - self.min_y, 4)

    def to_dict(self) -> dict:
        return {
            "min_x": round(self.min_x, 4),
            "min_y": round(self.min_y, 4),
            "max_x": round(self.max_x, 4),
            "max_y": round(self.max_y, 4),
            "width": self.width,
            "height": self.height,
        }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_node(x) -> bool:
    return isinstance(x, list) and len(x) >= 1 and isinstance(x[0], sexpdata.Symbol)


def _tag(node: list) -> str:
    return node[0].value()


def _xy_pairs_from_pts(pts_node: list) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for child in pts_node[1:]:
        if _is_node(child) and _tag(child) == "xy" and len(child) >= 3:
            try:
                out.append((float(child[1]), float(child[2])))
            except (TypeError, ValueError):
                continue
    return out


def _find_xy(node: list, tag: str) -> tuple[float, float] | None:
    """Return (x, y) of the first child with the given tag, or None."""
    for child in node[1:]:
        if _is_node(child) and _tag(child) == tag and len(child) >= 3:
            try:
                return (float(child[1]), float(child[2]))
            except (TypeError, ValueError):
                return None
    return None


def _find_at(node: list) -> tuple[float, float, float] | None:
    """Return (x, y, angle_deg) of the first ``(at ...)`` child, or None."""
    for child in node[1:]:
        if _is_node(child) and _tag(child) == "at" and len(child) >= 3:
            try:
                x = float(child[1])
                y = float(child[2])
                angle = float(child[3]) if len(child) >= 4 else 0.0
                return (x, y, angle)
            except (TypeError, ValueError):
                return None
    return None


def _find_number(node: list, tag: str) -> float | None:
    for child in node[1:]:
        if _is_node(child) and _tag(child) == tag and len(child) >= 2:
            try:
                return float(child[1])
            except (TypeError, ValueError):
                return None
    return None


def _arc_extrema(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Return the points needed to bound an arc defined by start/mid/end.

    Includes the three defining points plus any cardinal extrema (rightmost,
    topmost, leftmost, bottommost points of the circle) that lie within the
    swept arc range. This matters because an arc can reach maximum X/Y at
    angles that are not at start, mid, or end.
    """
    sx, sy = start
    mx, my = mid
    ex, ey = end

    pts: list[tuple[float, float]] = [start, mid, end]

    # Solve circle through three points.
    # Using perpendicular bisector intersection.
    ax, ay = sx, sy
    bx, by = mx, my
    cx_, cy_ = ex, ey
    d = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
    if abs(d) < 1e-12:
        # Collinear — three points already bound the segment.
        return pts

    ux = (
        (ax * ax + ay * ay) * (by - cy_)
        + (bx * bx + by * by) * (cy_ - ay)
        + (cx_ * cx_ + cy_ * cy_) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx_ - bx)
        + (bx * bx + by * by) * (ax - cx_)
        + (cx_ * cx_ + cy_ * cy_) * (bx - ax)
    ) / d
    cx, cy = ux, uy
    r = math.hypot(ax - cx, ay - cy)

    # Angles of start, mid, end in (-pi, pi]
    a_s = math.atan2(sy - cy, sx - cx)
    a_m = math.atan2(my - cy, mx - cx)
    a_e = math.atan2(ey - cy, ex - cx)

    # Determine sweep direction: choose direction (CCW or CW) where mid lies
    # between start and end.
    #
    # A point at angle ``theta`` lies on the CCW arc from a0 to a1 iff the
    # CCW angular distance a0→theta is no greater than a0→a1.  The CW arc
    # from a0 to a1 is identical to the CCW arc from a1 to a0, so we just
    # swap the endpoints to test CW membership.
    def _ccw_dist(theta: float, anchor: float) -> float:
        return (theta - anchor) % (2 * math.pi)

    def _in_sweep(theta: float, a0: float, a1: float, ccw: bool) -> bool:
        if ccw:
            return _ccw_dist(theta, a0) <= _ccw_dist(a1, a0) + 1e-9
        return _ccw_dist(theta, a1) <= _ccw_dist(a0, a1) + 1e-9

    ccw = _in_sweep(a_m, a_s, a_e, True)
    if not ccw and not _in_sweep(a_m, a_s, a_e, False):
        # Degenerate: mid not between either way — fall back to defining pts only.
        return pts

    # Cardinal extrema candidates.
    for theta in (0.0, math.pi / 2, math.pi, -math.pi / 2):
        if _in_sweep(theta, a_s, a_e, ccw):
            pts.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))

    return pts


def _bbox_from_points(points: Iterable[tuple[float, float]]) -> BBox | None:
    pts = list(points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def _walk_unit_geometry(unit_node: list) -> list[tuple[float, float]]:
    """Collect bounding-box points from one sub-symbol (unit) node.

    Includes graphic primitives and pin connection points.
    """
    points: list[tuple[float, float]] = []

    for child in unit_node[2:]:
        if not _is_node(child):
            continue
        tag = _tag(child)

        if tag == "rectangle":
            s = _find_xy(child, "start")
            e = _find_xy(child, "end")
            if s and e:
                points.extend([s, e])

        elif tag == "polyline":
            for sub in child[1:]:
                if _is_node(sub) and _tag(sub) == "pts":
                    points.extend(_xy_pairs_from_pts(sub))

        elif tag == "arc":
            s = _find_xy(child, "start")
            m = _find_xy(child, "mid")
            e = _find_xy(child, "end")
            if s and m and e:
                points.extend(_arc_extrema(s, m, e))

        elif tag == "circle":
            c = _find_xy(child, "center")
            r = _find_number(child, "radius")
            if c is not None and r is not None:
                points.extend(
                    [
                        (c[0] - r, c[1] - r),
                        (c[0] + r, c[1] + r),
                    ]
                )

        elif tag == "pin":
            # (pin <type> <shape> (at x y angle) (length l) ...)
            # Include both the connection endpoint (at) and the body end
            # (at + length along the pin direction) so that the bbox covers
            # the full pin stub.  This prevents placement candidates from
            # landing on top of a neighbour's exposed pin wire.
            pin_at = _find_at(child)
            length = _find_number(child, "length")
            if pin_at is not None:
                px, py, angle_deg = pin_at
                points.append((px, py))
                if length is not None and length > 0:
                    rad = math.radians(angle_deg)
                    points.append(
                        (
                            px + length * math.cos(rad),
                            py + length * math.sin(rad),
                        )
                    )

    return points


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_unit_bboxes(
    lib_sym_raw: list,
    style: int = 1,
) -> dict[int, BBox]:
    """Compute per-unit bounding boxes (library Y-up coords, mm).

    Sub-symbols inside a KiCad library symbol are named
    ``"<SYMNAME>_<UNIT>_<STYLE>"``.  Unit ``0`` holds graphics shared by
    every unit (and every style); all other units hold per-unit graphics.
    The returned dict is keyed by unit number (excluding 0); each entry's
    bbox is the union of unit ``N`` graphics, the common unit-0 graphics,
    and pin connection points for the requested ``style``.

    Args:
        lib_sym_raw: Raw S-expression list for one library symbol, as
            returned by ``extract_lib_symbol_raw``.
        style: Body style index. ``1`` is the standard body; ``2`` is the
            De Morgan alternate when present.

    Returns:
        Mapping ``{unit_number: BBox}``. If the symbol has no per-unit
        sub-symbols (e.g. a power symbol), returns ``{1: BBox}`` so
        callers can always look up unit 1.
    """
    if not (
        isinstance(lib_sym_raw, list) and len(lib_sym_raw) >= 2 and isinstance(lib_sym_raw[1], str)
    ):
        return {}

    sym_name = lib_sym_raw[1]
    # Schematic-embedded lib symbols use qualified names like "Device:R" but
    # their sub-symbols are still named with the bare local part ("R_0_1"),
    # so strip any "Lib:" prefix to derive the matching prefix.
    local_name = sym_name.split(":", 1)[-1]
    prefix = local_name + "_"

    # Group sub-symbols by (unit, style).
    by_unit: dict[int, list[tuple[float, float]]] = {}
    common_pts: list[tuple[float, float]] = []

    for entry in lib_sym_raw[2:]:
        if not (_is_node(entry) and _tag(entry) == "symbol"):
            continue
        if not (len(entry) >= 2 and isinstance(entry[1], str)):
            continue
        name = entry[1]
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        parts = suffix.split("_")
        if len(parts) < 2:
            continue
        try:
            u = int(parts[0])
            s = int(parts[1])
        except ValueError:
            continue

        # Style 0 means "applies to all styles".
        if s != 0 and s != style:
            continue

        pts = _walk_unit_geometry(entry)
        if u == 0:
            common_pts.extend(pts)
        else:
            by_unit.setdefault(u, []).extend(pts)

    # Combine common + per-unit and build bboxes.
    result: dict[int, BBox] = {}
    if not by_unit:
        # Some power symbols stash everything in unit-0 only — treat as unit 1.
        bbox = _bbox_from_points(common_pts)
        if bbox is not None:
            result[1] = bbox
        return result

    for u, pts in by_unit.items():
        bbox = _bbox_from_points(pts + common_pts)
        if bbox is not None:
            result[u] = bbox

    return result


def compute_lib_bbox(lib_sym_raw: list, style: int = 1) -> BBox | None:
    """Convenience: bbox of unit 1 (the most common LLM-facing case).

    For multi-unit symbols this is the bbox of unit 1 only.  Callers that
    need every unit should use :func:`compute_unit_bboxes` and union the
    placed-world bboxes themselves.
    """
    by_unit = compute_unit_bboxes(lib_sym_raw, style=style)
    return by_unit.get(1) if by_unit else None


# ---------------------------------------------------------------------------
# Lib-space → world-space bbox transform
# ---------------------------------------------------------------------------

_VALID_ROTS = (0, 90, 180, 270)


def _rotate_lib_point(x: float, y: float, rot_deg: int) -> tuple[float, float]:
    """Rotate a point around the lib origin by rot_deg (CCW in lib Y-up)."""
    r = rot_deg % 360
    if r == 0:
        return (x, y)
    if r == 90:
        return (-y, x)
    if r == 180:
        return (-x, -y)
    if r == 270:
        return (y, -x)
    rad = math.radians(r)
    return (
        x * math.cos(rad) - y * math.sin(rad),
        x * math.sin(rad) + y * math.cos(rad),
    )


def lib_bbox_to_world(
    bbox: BBox,
    sym_x: float,
    sym_y: float,
    rotation: int = 0,
    mirror: str | None = None,
) -> BBox:
    """Transform a library-space bbox into world (schematic) coordinates.

    Applies, in order:
      1. mirror (``"x"`` flips lib y → -y; ``"y"`` flips lib x → -x),
      2. rotation around the lib origin (CCW in Y-up space),
      3. translation to ``(sym_x, sym_y)``,
      4. Y-axis flip (lib Y-up → schematic Y-down): ``world_y = sym_y - rel_y``.

    All four corners of the lib bbox are transformed and re-bounded so that
    rotations of 90°/270° correctly swap width and height even for
    asymmetric symbols.
    """
    corners = [
        (bbox.min_x, bbox.min_y),
        (bbox.max_x, bbox.min_y),
        (bbox.max_x, bbox.max_y),
        (bbox.min_x, bbox.max_y),
    ]

    if mirror == "y":
        corners = [(-x, y) for (x, y) in corners]
    elif mirror == "x":
        corners = [(x, -y) for (x, y) in corners]

    rot = int(rotation) % 360
    if rot not in _VALID_ROTS:
        # Snap unusual angles to nearest valid; bbox stays a conservative AABB.
        rot = min(_VALID_ROTS, key=lambda r: abs(((r - rot + 180) % 360) - 180))

    corners = [_rotate_lib_point(x, y, rot) for (x, y) in corners]

    world = [(sym_x + x, sym_y - y) for (x, y) in corners]
    return _bbox_from_points(world) or bbox


def union_bboxes(bboxes: Iterable[BBox]) -> BBox | None:
    """Return the AABB that contains every input bbox, or None if empty."""
    items = list(bboxes)
    if not items:
        return None
    return BBox(
        min(b.min_x for b in items),
        min(b.min_y for b in items),
        max(b.max_x for b in items),
        max(b.max_y for b in items),
    )


def inflate_bbox(bbox: BBox, margin: float) -> BBox:
    """Return a bbox expanded outward by ``margin`` mm on all sides."""
    return BBox(
        bbox.min_x - margin,
        bbox.min_y - margin,
        bbox.max_x + margin,
        bbox.max_y + margin,
    )


def bboxes_overlap(a: BBox, b: BBox, tol: float = 1e-6) -> bool:
    """True if two bboxes overlap (touching edges do not count as overlap)."""
    return not (
        a.max_x <= b.min_x + tol
        or b.max_x <= a.min_x + tol
        or a.max_y <= b.min_y + tol
        or b.max_y <= a.min_y + tol
    )
