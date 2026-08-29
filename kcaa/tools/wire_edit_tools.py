"""Wire and junction editing tools for KiCad MCP server.

Provides tools to draw, list, and delete wire segments and junction dots
in KiCad schematics using the skip library.
"""

from collections.abc import Sequence
import json
import logging
import math
import os
import re
import time
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic
from kcaa.utils.skip_helpers import sym_pin_world_coords

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing constants
# ---------------------------------------------------------------------------

_LEAD_OUT_DIST: float = 2.54  # mm — one KiCad grid step, pulls wire into open space
_PIN_COLLISION_TOL: float = 0.5  # mm — clearance radius around each obstacle pin
_DUMP_MAX_REJECTED: int = 64  # cap rejected-candidate records per angle pair


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _dir_vec(angle_deg: float) -> tuple[float, float]:
    """Return the unit direction vector for a pin wire-exit angle.

    Angles use the KiCad file-angle convention (CCW on screen, Y-down
    schematic coordinates, 0=right 90=up):
      0°   → pointing right  (+X)
      90°  → pointing up     (−Y, screen)
      180° → pointing left   (−X)
      270° → pointing down   (+Y, screen)
    """
    a = int(round(angle_deg)) % 360
    return {
        0: (1.0, 0.0),
        90: (0.0, -1.0),
        180: (-1.0, 0.0),
        270: (0.0, 1.0),
    }.get(a, (math.cos(math.radians(a)), -math.sin(math.radians(a))))


def _point_on_open_segment(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    tol: float,
) -> bool:
    """Return True if point P lies strictly inside axis-aligned segment A→B.

    'Strictly inside' means the point is not at either endpoint, so that the
    two connected pins do not self-collide with their own wire.
    Works only for horizontal or vertical segments.
    """
    if abs(ay - by) < 1e-9:  # horizontal segment
        if abs(py - ay) > tol:
            return False
        lo = min(ax, bx) + tol
        hi = max(ax, bx) - tol
        return lo <= px <= hi
    if abs(ax - bx) < 1e-9:  # vertical segment
        if abs(px - ax) > tol:
            return False
        lo = min(ay, by) + tol
        hi = max(ay, by) - tol
        return lo <= py <= hi
    return False


def _route_collides(
    segments: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float]],
    tol: float,
) -> bool:
    """Return True if any obstacle pin lands on the interior of any segment."""
    for ax, ay, bx, by in segments:
        for px, py in obstacles:
            if _point_on_open_segment(px, py, ax, ay, bx, by, tol):
                return True
    return False


def _route_collides_at_corners(
    segments: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float]],
    tol: float,
    route_sx: float,
    route_sy: float,
    route_ex: float,
    route_ey: float,
) -> bool:
    """Return True if any obstacle pin coincides with an intermediate corner.

    ``_route_collides`` uses ``_point_on_open_segment`` which excludes segment
    endpoints.  A pin sitting exactly at a corner waypoint shared by two
    consecutive segments is therefore missed.  This helper collects every
    segment endpoint that is *not* the overall route start (route_sx, route_sy)
    or end (route_ex, route_ey) and checks whether any obstacle coincides with
    it.  Pins at the overall start/end are legitimate connections and are
    intentionally excluded.
    """
    for i, (ax, ay, bx, by) in enumerate(segments):
        for cx, cy in ((ax, ay), (bx, by)):
            # Skip the overall route start and end — those are the connected pins.
            if abs(cx - route_sx) <= tol and abs(cy - route_sy) <= tol:
                continue
            if abs(cx - route_ex) <= tol and abs(cy - route_ey) <= tol:
                continue
            for px, py in obstacles:
                if abs(cx - px) <= tol and abs(cy - py) <= tol:
                    return True
    return False


def _route_check_failures(
    segments: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float]],
    existing_wires: list[tuple[float, float, float, float]],
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    tol: float,
    pin_symbols: list[tuple[float, float, float, float]] | None = None,
    lead_segs: list[tuple[float, float, float, float]] | None = None,
) -> list[str]:
    """Return the list of checks that reject *segments*; empty list = pass.

    Runs the four gates used by ``_try_angle_config`` — pin on segment
    interior, collinear overlap with pin symbol stubs, collinear overlap
    with existing wires, pin on a corner — and reports which ones fail.
    Kept as its own function so the visualisation dump can record the exact
    rejection reason per candidate.

    Only the actual lead-out segments (``lead_segs``, in forward electrical
    point → tip direction) running outward on their own pin stub are exempt
    from the stub gate; every other overlap with a pin symbol is rejected.
    """
    fails: list[str] = []
    if _route_collides(segments, obstacles, tol):
        fails.append("pin-on-interior")
    if pin_symbols and _intersects_any(
        segments,
        pin_symbols,
        tol,
        exempt=(
            (lambda seg, line, tol: seg in (lead_segs or []) and _lead_on_own_stub(seg, line, tol))
            if lead_segs
            else None
        ),
    ):
        fails.append("pin-overlap")
    if _route_overlaps_wires(segments, existing_wires, tol):
        fails.append("wire-overlap")
    if _route_collides_at_corners(segments, obstacles, tol, sx, sy, ex, ey):
        fails.append("pin-at-corner")
    return fails


def _intersects_any(
    segments: list[tuple[float, float, float, float]],
    lines: list[tuple[float, float, float, float]],
    tol: float,
    exempt=None,
) -> bool:
    """Return True if any segment intersects (overlaps or crosses) any line.

    *exempt* is an optional predicate ``(segment, line, tol) -> bool`` that
    excuses specific pairs (e.g. a lead running outward on its own pin stub).
    """
    for seg in segments:
        for line in lines:
            if _segments_intersect(seg, line, tol):
                if exempt is not None and exempt(seg, line, tol):
                    continue
                return True
    return False


def _lead_on_own_stub(
    seg: tuple[float, float, float, float],
    stub: tuple[float, float, float, float],
    tol: float,
) -> bool:
    """True when *seg* is the outgoing lead of *stub*'s own pin.

    The legitimate lead-out starts at the pin's electrical point and runs
    outward along the stub direction (stub = electrical point → stub tip).
    A segment that returns from outside onto the stub (or crosses it at
    length) is *not* exempt — that is a wire drawn on top of the pin symbol.
    """
    ex, ey, tx, ty = stub
    sdx, sdy = tx - ex, ty - ey
    for sx0, sy0, sx1, sy1 in (
        (seg[0], seg[1], seg[2], seg[3]),
        (seg[2], seg[3], seg[0], seg[1]),  # end_lead is stored inner→pin
    ):
        if abs(sx0 - ex) <= tol and abs(sy0 - ey) <= tol:
            if (sx1 - sx0) * sdx + (sy1 - sy0) * sdy > 0:
                return True
    return False


def _point_in_interior(
    px: float,
    py: float,
    sx0: float,
    sy0: float,
    sx1: float,
    sy1: float,
    tol: float,
) -> bool:
    """True when (px,py) lies strictly inside axis-aligned segment (s0→s1)."""
    if abs(sy0 - sy1) < 1e-9:
        return abs(py - sy0) <= tol and min(sx0, sx1) + tol < px < max(sx0, sx1) - tol
    if abs(sx0 - sx1) < 1e-9:
        return abs(px - sx0) <= tol and min(sy0, sy1) + tol < py < max(sy0, sy1) - tol
    return False


def _segments_intersect(
    seg: tuple[float, float, float, float],
    line: tuple[float, float, float, float],
    tol: float,
) -> bool:
    """True when the two axis-aligned segments share more than an endpoint.

    Covers both collinear interval overlap (the wire-overlap gate) and a
    T/perpendicular crossing where one segment passes *through* the other's
    interior — e.g. a route running across the middle of a pin symbol.
    Endpoint touches (lead on its own stub, bridge landing on a wire end)
    are not flagged.
    """
    if _segments_overlap(*seg, *line, tol):
        return True
    ax, ay, bx, by = seg
    cx, cy, dx, dy = line
    # One horizontal, one vertical, crossing strictly interior to both.
    if abs(ay - by) < 1e-9 and abs(ax - bx) > 1e-9 and abs(cx - dx) < 1e-9 and abs(cy - dy) > 1e-9:
        vx, hy = cx, ay
        return _point_in_interior(vx, hy, ax, ay, bx, by, tol) and _point_in_interior(
            vx, hy, cx, cy, cx, dy, tol
        )
    if abs(cx - dx) < 1e-9 and abs(cy - dy) > 1e-9 and abs(ay - by) < 1e-9 and abs(ax - bx) > 1e-9:
        vx, hy = ax, cy
        return _point_in_interior(vx, hy, cx, cy, dx, dy, tol) and _point_in_interior(
            vx, hy, ax, ay, ax, by, tol
        )
    return False


def _route_lanes(
    x1: float,
    x2: float,
    delta: float = 0.635,
    n_anchors: int = 8,
    n_offsets: int = 2,
) -> list[float]:
    """Ordered lane positions inside the open span between x1 and x2.

    Direction-independent: works with x2 < x1 (a downwards/leftwards run).
    The evenly spaced anchors come first (kept from the original 8th
    fractions), then each anchor perturbed by ±k·*delta* (k=1..n_offsets)
    so a blocked anchor can be escaped by a small local move instead of
    jumping to a distant lane.  Values outside the span are clipped away
    and duplicates removed.  n_offsets=0 returns plain anchors.
    """
    if abs(x2 - x1) <= 1e-9:
        return []
    span_lo, span_hi = min(x1, x2), max(x1, x2)
    lanes: list[float] = []
    seen: set[float] = set()

    def _add(v: float) -> None:
        r = round(v, 4)
        if span_lo < r < span_hi and r not in seen:
            seen.add(r)
            lanes.append(r)

    for k in range(1, n_anchors):
        a = span_lo + (span_hi - span_lo) * k / n_anchors
        _add(a)
        for kd in range(1, n_offsets + 1):
            _add(a - kd * delta)
            _add(a + kd * delta)
    return lanes


def _route_candidates(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> list[list[tuple[float, float, float, float]]]:
    """Return a ranked list of candidate segment-lists connecting (x1,y1)→(x2,y2).

    Each candidate is a list of (ax, ay, bx, by) axis-aligned segments.
    Candidates are tried in order; the first collision-free one wins.

    Order:
      1. Direct (1 segment) — only when points are axis-aligned.
      2. L-A: horizontal-first via corner (x2, y1).
      3. L-B: vertical-first via corner (x1, y2).
      4-79.  Horizontal Z-routes: jog the vertical column to every lane x
             (8 anchors + ±0.635/±1.27 micro-offsets).
      80-155. Vertical Z-routes: jog the horizontal row to every lane y.
      156-253. 4-seg W-routes (H-V-H-V / V-H-V-H) on the 8 anchors, so the
             middle vertical can stop short of the far edge and dodge a
             wall in the terminal column.
      254-547. 5-seg snakes (H-V-H-V-H / V-H-V-H-V) on the 8 anchors: the
             two jogs (xa, xb) / (ya, yb) are independent, letting the
             final stub on the endpoint row stay short and clean.
      548+.  U-detours for the collinear case (same axis, direct route
             blocked): jog ±2.54, ±5.08, ±7.62 mm perpendicular, crossing
             full span, then back.
    """
    candidates: list[list[tuple[float, float, float, float]]] = []
    dx = x2 - x1
    dy = y2 - y1

    # Direct single segment (only when already axis-aligned)
    if abs(dy) < 1e-9 or abs(dx) < 1e-9:
        candidates.append([(x1, y1, x2, y2)])

    # L-A: horizontal then vertical
    candidates.append([(x1, y1, x2, y1), (x2, y1, x2, y2)])

    # L-B: vertical then horizontal
    candidates.append([(x1, y1, x1, y2), (x1, y2, x2, y2)])

    if abs(dx) > 1e-9 and abs(dy) > 1e-9:
        xlanes = _route_lanes(x1, x2)
        ylanes = _route_lanes(y1, y2)

        # Horizontal Z-routes: jog the vertical column to a lane x
        for xj in xlanes:
            candidates.append(
                [
                    (x1, y1, xj, y1),
                    (xj, y1, xj, y2),
                    (xj, y2, x2, y2),
                ]
            )
        # Vertical Z-routes: jog the horizontal row to a lane y
        for yj in ylanes:
            candidates.append(
                [
                    (x1, y1, x1, yj),
                    (x1, yj, x2, yj),
                    (x2, yj, x2, y2),
                ]
            )

        # 4-seg W-routes: the middle vertical stops at a lane y instead of
        # crossing the full span, so the last run is on the terminal column
        # only (escapes a wall sitting on the start/end columns).
        x_anchors = _route_lanes(x1, x2, n_offsets=0)
        y_anchors = _route_lanes(y1, y2, n_offsets=0)
        for xa in x_anchors:
            for yw in y_anchors:
                candidates.append(
                    [
                        (x1, y1, xa, y1),
                        (xa, y1, xa, yw),
                        (xa, yw, x2, yw),
                        (x2, yw, x2, y2),
                    ]
                )
        for ya in y_anchors:
            for xw in x_anchors:
                candidates.append(
                    [
                        (x1, y1, x1, ya),
                        (x1, ya, xw, ya),
                        (xw, ya, xw, y2),
                        (xw, y2, x2, y2),
                    ]
                )

        # 5-seg snakes: two independent jogs let the final stub on the
        # endpoint row stay short (e.g. land left of a stub starting right
        # at the pin instead of crossing it).
        for i, xa in enumerate(x_anchors):
            for xb in x_anchors[i + 1 :]:
                for yw in y_anchors:
                    candidates.append(
                        [
                            (x1, y1, xa, y1),
                            (xa, y1, xa, yw),
                            (xa, yw, xb, yw),
                            (xb, yw, xb, y2),
                            (xb, y2, x2, y2),
                        ]
                    )
        for i, ya in enumerate(y_anchors):
            for yb in y_anchors[i + 1 :]:
                for xw in x_anchors:
                    candidates.append(
                        [
                            (x1, y1, x1, ya),
                            (x1, ya, xw, ya),
                            (xw, ya, xw, yb),
                            (xw, yb, x2, yb),
                            (x2, yb, x2, y2),
                        ]
                    )

    # U-detours: used when the direct axis-aligned route is blocked by an
    # existing wire.  Jog perpendicular to the connecting axis by multiples
    # of one KiCad grid step (2.54 mm), then bridge the full span, then
    # return.  Both perpendicular directions are tried at each offset so the
    # router can pick whichever side has open space.
    _GRID = 2.54
    _U_STEPS = [1, 2, 3]  # grid multiples to try: 2.54, 5.08, 7.62 mm
    if abs(dy) < 1e-9 and abs(dx) > 1e-9:
        # Horizontal collinear: jog in ±y
        for n in _U_STEPS:
            for yoff in (n * _GRID, -n * _GRID):
                yj = y1 + yoff
                candidates.append(
                    [
                        (x1, y1, x1, yj),
                        (x1, yj, x2, yj),
                        (x2, yj, x2, y2),
                    ]
                )
    elif abs(dx) < 1e-9 and abs(dy) > 1e-9:
        # Vertical collinear: jog in ±x
        for n in _U_STEPS:
            for xoff in (n * _GRID, -n * _GRID):
                xj = x1 + xoff
                candidates.append(
                    [
                        (x1, y1, xj, y1),
                        (xj, y1, xj, y2),
                        (xj, y2, x2, y2),
                    ]
                )

    return candidates


def _merge_collinear_segments(
    segments: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Merge successive collinear axis-aligned segments into fewer segments.

    Prevents redundant wire nodes when a Z-route jog lands on the same axis as
    the adjacent lead-out stub.
    """
    if not segments:
        return segments
    result = list(segments)
    changed = True
    while changed:
        changed = False
        merged: list[tuple[float, float, float, float]] = []
        i = 0
        while i < len(result):
            if i + 1 < len(result):
                ax, ay, bx, by = result[i]
                cx, cy, dx, dy = result[i + 1]
                # Segments share the middle point
                if abs(bx - cx) < 1e-9 and abs(by - cy) < 1e-9:
                    # Horizontal merge: all four y values the same
                    if abs(ay - by) < 1e-9 and abs(cy - dy) < 1e-9 and abs(ay - dy) < 1e-9:
                        merged.append((ax, ay, dx, dy))
                        i += 2
                        changed = True
                        continue
                    # Vertical merge: all four x values the same
                    if abs(ax - bx) < 1e-9 and abs(cx - dx) < 1e-9 and abs(ax - dx) < 1e-9:
                        merged.append((ax, ay, dx, dy))
                        i += 2
                        changed = True
                        continue
            merged.append(result[i])
            i += 1
        result = merged
    return result


def _segments_overlap(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
    tol: float,
) -> bool:
    """Return True if two axis-aligned segments are collinear and share more than a point.

    A point-touch (endpoint meeting endpoint, or a T-junction at a shared
    endpoint) is *not* considered an overlap.  Only collinear segments whose
    projected intervals intersect with length > tol are flagged.
    """
    # Both horizontal
    if abs(ay - by) < 1e-9 and abs(cy - dy) < 1e-9 and abs(ay - cy) < tol:
        lo1, hi1 = min(ax, bx), max(ax, bx)
        lo2, hi2 = min(cx, dx), max(cx, dx)
        return min(hi1, hi2) - max(lo1, lo2) > tol
    # Both vertical
    if abs(ax - bx) < 1e-9 and abs(cx - dx) < 1e-9 and abs(ax - cx) < tol:
        lo1, hi1 = min(ay, by), max(ay, by)
        lo2, hi2 = min(cy, dy), max(cy, dy)
        return min(hi1, hi2) - max(lo1, lo2) > tol
    return False


def _route_overlaps_wires(
    segments: list[tuple[float, float, float, float]],
    existing_wires: list[tuple[float, float, float, float]],
    tol: float,
) -> bool:
    """Return True if any segment in *segments* overlaps any existing wire."""
    for ax, ay, bx, by in segments:
        for cx, cy, dx, dy in existing_wires:
            if _segments_overlap(ax, ay, bx, by, cx, cy, dx, dy, tol):
                return True
    return False


def _collect_existing_wires(sch: Any) -> list[tuple[float, float, float, float]]:
    """Return all wire segments currently in the schematic as (ax, ay, bx, by) tuples."""
    wires: list[tuple[float, float, float, float]] = []
    try:
        for w in sch.wire:
            wires.append(
                (
                    float(w.start.value[0]),
                    float(w.start.value[1]),
                    float(w.end.value[0]),
                    float(w.end.value[1]),
                )
            )
    except AttributeError:
        pass
    return wires


def _follow_wire_extent(
    sx: float,
    sy: float,
    angle: float,
    existing_wires: list[tuple[float, float, float, float]],
    tol: float,
    obstacle_pins: list[tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """Return the farthest point reachable from (sx, sy) along connected wire
    segments in the given angle direction without any gap.

    Used when a lead-out would overlap an existing wire: instead of starting the
    inner route just one lead-out step from the pin, we follow the existing wire
    network all the way to where it ends so the inner route begins past the
    entire existing segment chain.

    Args:
        obstacle_pins: If provided, traversal stops before stepping onto any of
            these pin positions.  The current position (not the blocked pin) is
            returned.
    """
    dvx, dvy = _dir_vec(angle)
    cx, cy = sx, sy
    visited: set[int] = set()
    while True:
        found = False
        for idx, (ax, ay, bx, by) in enumerate(existing_wires):
            if idx in visited:
                continue
            for ex, ey, ox, oy in ((ax, ay, bx, by), (bx, by, ax, ay)):
                if abs(ex - cx) > tol or abs(ey - cy) > tol:
                    continue
                dx, dy = ox - cx, oy - cy
                dist = math.sqrt(dx * dx + dy * dy)
                if dist < tol:
                    continue
                if abs(dx / dist - dvx) < 0.1 and abs(dy / dist - dvy) < 0.1:
                    # Stop before landing on a component pin
                    if obstacle_pins and any(
                        abs(ox - px) <= tol and abs(oy - py) <= tol for px, py in obstacle_pins
                    ):
                        continue
                    visited.add(idx)
                    cx, cy = ox, oy
                    found = True
                    break
            if found:
                break
        if not found:
            break
    return cx, cy


def _follow_connected_wires(
    px: float,
    py: float,
    existing_wires: list[tuple[float, float, float, float]],
    obstacle_pins: list[tuple[float, float]] | None,
    tol: float,
) -> tuple[float, float] | None:
    """Follow any wire connected at (px, py) to its far end.

    Called as a fallback when a pin's own lead-out is blocked by another
    component pin (``*_lead_pin_blocked``).  Rather than routing from the pin
    tip directly, we detect any existing wire that *already* leaves the pin in
    some direction and follow the entire chain to where it ends.

    Returns the far-end coordinate, or None if no wire is connected at (px, py).
    The direction is inferred from the first wire found at the point.
    """
    for ax, ay, bx, by in existing_wires:
        for near_x, near_y, far_x, far_y in ((ax, ay, bx, by), (bx, by, ax, ay)):
            if abs(near_x - px) > tol or abs(near_y - py) > tol:
                continue
            wdx, wdy = far_x - near_x, far_y - near_y
            wdist = math.sqrt(wdx * wdx + wdy * wdy)
            if wdist < tol:
                continue
            # Determine axis-aligned wire angle
            if abs(wdy) < 1e-9:
                wire_angle = 0.0 if wdx > 0 else 180.0
            else:
                wire_angle = 90.0 if wdy > 0 else 270.0
            return _follow_wire_extent(px, py, wire_angle, existing_wires, tol, obstacle_pins)
    return None


def _infer_angles_toward(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> list[float]:
    """Return axis-aligned angles from (x1,y1) that point toward (x2,y2).

    For axis-aligned targets (same row or column), returns a single angle.
    For diagonal targets, returns both the horizontal and vertical component
    angles so callers can try each direction independently.
    """
    angles: list[float] = []
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) > 1e-9:
        angles.append(0.0 if dx > 0 else 180.0)
    if abs(dy) > 1e-9:
        angles.append(90.0 if dy > 0 else 270.0)
    return angles


def _ordered_exit_angles(
    natural: float | None,
    sx: float,
    sy: float,
    tx: float,
    ty: float,
) -> list[float]:
    """Return all 4 cardinal angles ordered for routing from (sx,sy) toward (tx,ty).

    All 4 cardinals are ranked by their dot product with the target vector
    (tx-sx, ty-sy), giving:
      rank 0 — toward   (highest dot product, most faces the target)
      rank 1,2 — perpendiculars (sorted so the one with higher dot product
                  comes first, i.e. the one that leans toward the target)
      rank 3 — away     (lowest dot product, directly opposite)

    The natural pin exit angle (if supplied) is inserted at rank 0 before
    the dot-product-ranked list.  Duplicates are suppressed so the returned
    list always contains each unique cardinal exactly once.
    """
    _CARDINALS = [0.0, 90.0, 180.0, 270.0]

    dx, dy = tx - sx, ty - sy
    ranked = sorted(_CARDINALS, key=lambda a: -(_dir_vec(a)[0] * dx + _dir_vec(a)[1] * dy))

    result: list[float] = []
    seen: set[float] = set()

    def _add(a: float) -> None:
        k = a % 360.0
        if k not in seen:
            seen.add(k)
            result.append(k)

    if natural is not None:
        _add(natural % 360.0)
    for a in ranked:
        _add(a)

    return result


# ---------------------------------------------------------------------------
# Smart wire router
# ---------------------------------------------------------------------------

_RouteConfig = tuple[
    list[tuple[float, float, float, float]],  # start_lead
    list[tuple[float, float, float, float]],  # end_lead  (stored inner→pin)
    list[tuple[float, float, float, float]],  # chosen inner segments
    float,
    float,  # p1x, p1y  (start lead tip)
    float,
    float,  # p2x, p2y  (end lead tip)
    bool,  # start_suppressed
    bool,  # end_suppressed
]


def _try_angle_config(
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    sa: float | None,
    ea: float | None,
    existing_wires: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float]],
    lead_lengths: Sequence[float],
    start_natural: float | None = None,
    end_natural: float | None = None,
    diag: dict[str, Any] | None = None,
    pin_symbols: list[tuple[float, float, float, float]] | None = None,
) -> _RouteConfig | None:
    """Try to find a valid route from (sx,sy) to (ex,ey) using the given
    lead-out angles sa (start) and ea (end).

    Lead-out evaluation rules for each endpoint:
    * If the lead segment overlaps an existing wire: suppress the segment,
      advance the inner endpoint to the far end of the wire chain
      (T-tap mode), and mark as suppressed so a junction is placed later.
    * If a component pin blocks the lead segment at every tried length:
      return None — this angle direction is unusable, try the next one.
    * If the lead direction is exactly opposite to the pin's natural exit
      angle and does not follow an existing wire: return None — this
      direction routes into the component body (e.g. through a GND triangle).
    * Otherwise: emit the lead segment at the shortest tried length whose
      tip is not blocked, and place the inner endpoint at that tip.

    ``lead_lengths`` is tried in order; the first entry is the anchor
    (caller-disrupting) length and remaining entries are fallbacks tried
    only when a longer lead is blocked by a pin, keeping the exit point
    as close to the anchor as possible.
    None for an angle means no lead-out from that endpoint (route directly
    from the pin position).

    start_natural / end_natural are the natural (pin-body-defined) exit
    angles of the start and end pins respectively.  When provided, leads in
    the exactly-opposite direction are rejected unless they follow an
    existing wire.

    The end lead is stored reversed (inner→pin) so that the sequence
    start_lead + chosen + end_lead always forms a continuous chain, which is
    required by _merge_collinear_segments.

    diag: optional dict filled with leads outcome ("start_lead"/"end_lead":
    ok | t-tap | opposite-natural | pin-blocked) and, per rejected candidate,
    an entry in "rejected_candidates" with the segments and failing checks.
    Only used by the visualisation dump; None disables the bookkeeping.

    Returns a _RouteConfig tuple on success, or None if no candidate inner
    route passes the collision / overlap checks.
    """
    # --- start lead options ---
    # Angle-level state is length-independent and decided once:
    #  * t-tap: lead overlaps an existing wire → tip absorbed into the
    #    followed chain (any length does the same);
    #  * opposite-natural: direction rejected outright;
    #  * plain lead: each length has its own tip; lengths are swept by the
    #    search below so a route blocked at the anchor can be rescued by
    #    moving the exit point minimally (conflict-driven).
    dvsx = dvsy = 0.0
    if sa is not None:
        dvsx, dvsy = _dir_vec(sa)

    start_opts: list[
        tuple[float | None, float, float, list[tuple[float, float, float, float]]]
    ] = []
    start_suppressed = False
    if sa is not None:
        anchor = lead_lengths[0]
        seg_anchor = (sx, sy, round(sx + dvsx * anchor, 4), round(sy + dvsy * anchor, 4))
        if _route_overlaps_wires([seg_anchor], existing_wires, _PIN_COLLISION_TOL):
            p1x, p1y = _follow_wire_extent(sx, sy, sa, existing_wires, _PIN_COLLISION_TOL)
            start_suppressed = True
            start_opts = [(anchor, p1x, p1y, [])]
            if diag is not None:
                diag["start_lead"] = "t-tap"
        elif (
            start_natural is not None and abs((sa - (start_natural + 180.0) % 360.0) % 360.0) < 1e-9
        ):
            if diag is not None:
                diag["start_lead"] = "opposite-natural"
            return None  # lead goes into component body (opposite to natural exit)
        else:
            for length in lead_lengths:
                tip = (round(sx + dvsx * length, 4), round(sy + dvsy * length, 4))
                seg = (sx, sy, tip[0], tip[1])
                if _route_collides([seg], obstacles, _PIN_COLLISION_TOL) or any(
                    abs(tip[0] - ox) <= _PIN_COLLISION_TOL
                    and abs(tip[1] - oy) <= _PIN_COLLISION_TOL
                    for ox, oy in obstacles
                ):
                    continue  # lead blocked at this length; try a neighbour
                start_opts.append((length, tip[0], tip[1], [seg]))
            if not start_opts:
                if diag is not None:
                    diag["start_lead"] = "pin-blocked"
                return None  # every lead length blocked by a component pin
            if diag is not None:
                diag["start_lead"] = "ok"
    else:
        start_opts = [(None, sx, sy, [])]

    # --- end lead options ---
    dvex = dvey = 0.0
    if ea is not None:
        dvex, dvey = _dir_vec(ea)

    end_opts: list[tuple[float | None, float, float, list[tuple[float, float, float, float]]]] = []
    end_suppressed = False
    if ea is not None:
        anchor = lead_lengths[0]
        seg_anchor = (ex, ey, round(ex + dvex * anchor, 4), round(ey + dvey * anchor, 4))
        if _route_overlaps_wires([seg_anchor], existing_wires, _PIN_COLLISION_TOL):
            p2x, p2y = _follow_wire_extent(ex, ey, ea, existing_wires, _PIN_COLLISION_TOL)
            end_suppressed = True
            end_opts = [(anchor, p2x, p2y, [])]
            if diag is not None:
                diag["end_lead"] = "t-tap"
        elif end_natural is not None and abs((ea - (end_natural + 180.0) % 360.0) % 360.0) < 1e-9:
            if diag is not None:
                diag["end_lead"] = "opposite-natural"
            return None  # lead goes into component body (opposite to natural exit)
        else:
            for length in lead_lengths:
                tip = (round(ex + dvex * length, 4), round(ey + dvey * length, 4))
                seg = (ex, ey, tip[0], tip[1])
                if _route_collides([seg], obstacles, _PIN_COLLISION_TOL) or any(
                    abs(tip[0] - ox) <= _PIN_COLLISION_TOL
                    and abs(tip[1] - oy) <= _PIN_COLLISION_TOL
                    for ox, oy in obstacles
                ):
                    continue
                end_opts.append((length, tip[0], tip[1], [(tip[0], tip[1], ex, ey)]))  # inner→pin
            if not end_opts:
                if diag is not None:
                    diag["end_lead"] = "pin-blocked"
                return None  # every lead length blocked by a component pin
            if diag is not None:
                diag["end_lead"] = "ok"
    else:
        end_opts = [(None, ex, ey, [])]

    # Conflict-driven length sweep: the anchor length pair is tried first so
    # previously-working routes are untouched; further lengths are swept only
    # because every candidate at the current length was rejected, moving the
    # exit point as little as necessary.
    anchor_l = lead_lengths[0]
    combos = sorted(
        (
            (sl, s1x, s1y, sseg, el, e1x, e1y, eseg)
            for sl, s1x, s1y, sseg in start_opts
            for el, e1x, e1y, eseg in end_opts
        ),
        key=lambda c: (
            (0.0 if c[0] is None else abs(c[0] - anchor_l))
            + (0.0 if c[4] is None else abs(c[4] - anchor_l)),
            c[0] or 0.0,
            c[4] or 0.0,
        ),
    )

    for cur_sl, p1x, p1y, sseg, cur_el, p2x, p2y, eseg in combos:
        # Lead segments in canonical forward direction for collision checking
        lead_segs = sseg + [(a, b, c, d) for (c, d, a, b) in eseg]
        for candidate in _route_candidates(p1x, p1y, p2x, p2y):
            all_segs = lead_segs + candidate
            fails = _route_check_failures(
                all_segs,
                obstacles,
                existing_wires,
                sx,
                sy,
                ex,
                ey,
                _PIN_COLLISION_TOL,
                pin_symbols=pin_symbols,
                lead_segs=lead_segs,
            )
            if not fails:
                if diag is not None:
                    diag["valid"] = True
                return (
                    sseg,
                    eseg,
                    candidate,
                    p1x,
                    p1y,
                    p2x,
                    p2y,
                    start_suppressed,
                    end_suppressed,
                )
            if diag is not None:
                rejected = diag.setdefault("rejected_candidates", [])
                # Keep the record bounded: candidate shapes are checked in
                # ranked order, so the first entries carry the diagnostic
                # value; later W/snake variants repeat the same failure modes.
                if len(rejected) < _DUMP_MAX_REJECTED:
                    rejected.append(
                        {
                            "segments": [list(seg) for seg in all_segs],
                            "reasons": fails,
                            "lead": [cur_sl, cur_el],
                        }
                    )

    return None


# ---------------------------------------------------------------------------
# Visualisation dump (schematic)
# ---------------------------------------------------------------------------


def _sch_viz_dir() -> str:
    """Return the schematic viz dump directory under the kcaa data dir."""
    from kcaa.utils.config import config

    # KCAA_VIZ_DIR overrides the dump root so test/debug runs can be written
    # to a scratch location instead of polluting the live viz directory the
    # KiCad plugin renders from.
    override = os.environ.get("KCAA_VIZ_DIR")
    if override:
        return os.path.join(os.path.expanduser(override), "sch_viz")
    return os.path.join(config.get_kcaa_data_dir(), "kcaa_viz", "sch_viz")


def _flatten_segments(
    segments: list[tuple[float, float, float, float]],
) -> list[list[float]]:
    """Collapse a connected chain of segments into a deduplicated point list."""
    pts: list[list[float]] = []
    for ax, ay, bx, by in segments:
        if not pts:
            pts.append([ax, ay])
        elif abs(pts[-1][0] - ax) > 1e-9 or abs(pts[-1][1] - ay) > 1e-9:
            pts.append([ax, ay])
        pts.append([bx, by])
    return pts


def _wire_rect(ax: float, ay: float, bx: float, by: float, half: float = 0.25) -> list[list[float]]:
    """Thin rectangle polygon around an axis-aligned wire segment (mm)."""
    if abs(ay - by) < 1e-9:
        return [[ax, ay - half], [bx, by - half], [bx, by + half], [ax, ay + half]]
    return [[ax - half, ay], [bx - half, by], [bx + half, by], [ax + half, ay]]


def _dump_viz_schematic(
    stage: str,
    segments: list[tuple[float, float, float, float]] | None = None,
    pins: list[tuple[float, float]] | None = None,
    wires: list[tuple[float, float, float, float]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    notes: str | None = None,
    pin_stubs: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """Dump a schematic routing intermediate state for rendering.

    Mirrors the PCB router pipeline dump (``kcaa/router/router.py``) but with
    schematic geometry: every symbol pin becomes a small pad AABB (the
    0.5 mm collision radius), every existing wire becomes a thin obstacle
    rectangle, and the candidate routes under evaluation are stored alongside
    their rejection reasons.  Only writes when ``config.viz_dump_enabled`` is
    set via ``KCAA_DUMP_ROUTE_PIPELINE=1`` in ``.env``.
    """
    from kcaa.utils.config import config

    if not config.viz_dump_enabled:
        return
    d = _sch_viz_dir()
    os.makedirs(d, exist_ok=True)
    ts = time.strftime("%H%M%S")
    fname = os.path.join(d, f"{ts}_{stage}.json")

    pins = pins or []
    wires = wires or []
    tol = _PIN_COLLISION_TOL
    data: dict[str, Any] = {
        "stage": stage,
        "path": _flatten_segments(segments or []),
        "pads": [["", [px - tol, py - tol, px + tol, py + tol]] for px, py in pins],
        "obstacles": [(_wire_rect(cx, cy, dx, dy), "wire", "") for cx, cy, dx, dy in wires],
        "pin_stubs": [list(s) for s in (pin_stubs or [])],
        "candidates": candidates or [],
    }
    if notes:
        data["notes"] = notes
    with open(fname, "w") as f:
        json.dump(data, f)
    log.info("[viz] dumped %s", fname)


def _config_from_result(
    result: _RouteConfig,
) -> list[tuple[float, float, float, float]]:
    """Extract the drawable segment chain (start_lead + inner + end inner→pin)."""
    start_lead, end_lead, chosen, *_ = result
    return list(start_lead) + list(chosen) + list(end_lead)


def _draw_smart_wire(
    sch: Any,
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    existing_wires: list[tuple[float, float, float, float]],
    start_angle: float | None = None,
    end_angle: float | None = None,
    obstacle_pins: list[tuple[float, float]] | None = None,
    lead_out_dist: float = _LEAD_OUT_DIST,
) -> bool:
    """Draw a smart wire from (sx,sy) to (ex,ey) avoiding pins and existing wires.

    Algorithm:
      1. Build an ordered list of (start_angle, end_angle) pairs to try.
         The natural pin exit angles come first; all 4 cardinal directions
         (0°/90°/180°/270°) are tried for each endpoint, so the router can
         escape trapped configurations by routing out a perpendicular or
         opposite side.
      2. For each angle pair, evaluate lead-out stubs.  A lead that overlaps
         an existing wire is suppressed (the inner endpoint advances to the
         wire chain's far end; a T-junction is placed there).  A lead blocked
         by a component pin skips that pair.
      3. Try all candidate inner routes between the lead tips.  Pick the first
         that has no pin in any segment interior and no overlap with existing
         wires.
      4. Draw the chosen route and return True, or return False if every pair
         and every candidate is blocked.

    Args:
        sch: The skip schematic object.
        sx, sy: Start point (pin position).
        ex, ey: End point (pin position).
        existing_wires: All wire segments already in the schematic.
        start_angle: Natural exit angle of the start pin (0=right, 90=up,
            180=left, 270=down, KiCad CCW file-angle convention).  None
            means no preferred direction.
        end_angle: Natural exit angle of the end pin.
        obstacle_pins: List of (x, y) positions to avoid.
        lead_out_dist: Lead-out stub length in mm (default 2.54 mm).

    Returns:
        True if a route was found and drawn; False otherwise (nothing drawn).
    """
    obstacles = obstacle_pins or []

    # Pin symbols are 2.54–3.81 mm lines on the schematic; treat them as
    # collision bodies so routes cannot run on top of a pin.  Outgoing
    # leads on their own stub stay exempt inside the collision gate.
    pin_symbols = _collect_pin_symbol_stubs(sch)

    # Build the ordered list of (sa, ea) pairs to try.
    # Natural angles are tried first; then all 4 cardinals for each endpoint
    # (excluding duplicates already covered by the natural pair) are tried in
    # every combination so the router escapes trapped configurations.
    # Ordered angle lists for each endpoint.
    # natural exit angle → toward target → perpendiculars → away from target.
    _start_angles: list[float | None] = _ordered_exit_angles(start_angle, sx, sy, ex, ey)
    _end_angles: list[float | None] = _ordered_exit_angles(end_angle, ex, ey, sx, sy)

    # Iterate angle pairs in diagonal / rank-sum order:
    #   rank-sum 0: (sa[0], ea[0])
    #   rank-sum 1: (sa[0], ea[1]), (sa[1], ea[0])
    #   rank-sum 2: (sa[0], ea[2]), (sa[1], ea[1]), (sa[2], ea[0])
    #   …
    # This tries the most natural/optimal combination first, then gradually
    # relaxes both endpoints together rather than exhausting all end angles
    # for one start angle before moving to the next start angle.
    result: _RouteConfig | None = None
    succeeded_stage: str | None = None
    from kcaa.utils.config import config

    # Two-stage exploration.  Stage 1 tries every angle pair at the anchor
    # lead length only — bit-identical to previous behaviour whenever it
    # succeeds.  Stage 2 repeats with neighbourhood lengths, so lead length
    # changes are purely conflict-driven and never explored while an
    # anchor-length route exists.  Candidate shapes (including micro-adjusted
    # jog lanes) are the same for every angle pair in both stages.
    delta = lead_out_dist / 4.0  # 0.635 mm for the default 2.54 anchor
    lead_neighbourhood = sorted(
        {round(lead_out_dist + k * delta, 4) for k in range(-3, 5)},
        key=lambda length: (abs(length - lead_out_dist), length),
    )
    lead_neighbourhood = [length for length in lead_neighbourhood if length > 0]
    stages: list[tuple[str, list[float]]] = [
        ("s1", [lead_out_dist]),
        ("s2", lead_neighbourhood),
    ]

    max_rank = len(_start_angles) + len(_end_angles) - 2
    for stage_name, lead_lengths in stages:
        for rank_sum in range(max_rank + 1):
            for si in range(min(rank_sum + 1, len(_start_angles))):
                ei = rank_sum - si
                if ei < 0 or ei >= len(_end_angles):
                    continue
                sa = _start_angles[si]
                ea = _end_angles[ei]
                diag: dict[str, Any] | None = {} if config.viz_dump_enabled else None
                result = _try_angle_config(
                    sx,
                    sy,
                    ex,
                    ey,
                    sa,
                    ea,
                    existing_wires,
                    obstacles,
                    lead_lengths,
                    start_natural=start_angle,
                    end_natural=end_angle,
                    diag=diag,
                    pin_symbols=pin_symbols,
                )
                pair_name = f"{stage_name}-pair-sa{int(sa)}-ea{int(ea)}"
                if result is not None:
                    succeeded_stage = stage_name
                    try:
                        _dump_viz_schematic(
                            pair_name,
                            segments=_config_from_result(result),
                            pins=obstacles,
                            wires=existing_wires,
                            candidates=diag.get("rejected_candidates") if diag else None,
                            pin_stubs=pin_symbols,
                            notes=f"stage={stage_name} valid",
                        )
                    except Exception:
                        log.warning("[viz] failed to dump pair %s", pair_name, exc_info=True)
                    break
                try:
                    _dump_viz_schematic(
                        pair_name,
                        segments=None,
                        pins=obstacles,
                        wires=existing_wires,
                        candidates=diag.get("rejected_candidates") if diag else None,
                        pin_stubs=pin_symbols,
                        notes=(
                            f"stage={stage_name} lead start={diag.get('start_lead')} "
                            f"end={diag.get('end_lead')}"
                            if diag
                            else f"stage={stage_name}"
                        ),
                    )
                except Exception:
                    log.warning("[viz] failed to dump pair %s", pair_name, exc_info=True)
            if result is not None:
                break
        if result is not None:
            break

    if result is None:
        try:
            _dump_viz_schematic(
                "7-failed",
                segments=None,
                pins=obstacles,
                wires=existing_wires,
                pin_stubs=pin_symbols,
                notes=(
                    f"no route after {len(stages)} stages x "
                    f"{len(_start_angles) * len(_end_angles)} angle pairs tried"
                ),
            )
        except Exception:
            log.warning("[viz] failed to dump 7-failed", exc_info=True)
        log.warning(
            "smart_wire: no valid route found between (%.3f,%.3f) and "
            "(%.3f,%.3f); all angle/candidate combinations are blocked.",
            sx,
            sy,
            ex,
            ey,
        )
        return False

    start_lead, end_lead, chosen, p1x, p1y, p2x, p2y, start_suppressed, end_suppressed = result

    # Draw all segments, merging collinear neighbours first.
    # Order: start_lead → inner segments → end_lead (inner→pin) ensures that
    # consecutive segments always share an endpoint, which is required for
    # _merge_collinear_segments to collapse collinear pairs correctly.
    all_draw = _merge_collinear_segments(start_lead + chosen + end_lead)
    for ax, ay, bx, by in all_draw:
        if abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9:
            continue  # skip zero-length
        w = sch.wire.new()
        w.start_at([ax, ay])
        w.end_at([bx, by])

    # Place junction dots at suppressed-lead endpoints (T-tap points).
    for jx, jy in ([(p1x, p1y)] if start_suppressed else []) + (
        [(p2x, p2y)] if end_suppressed else []
    ):
        _add_junction_and_split(sch, jx, jy)

    try:
        _dump_viz_schematic(
            "7-final",
            segments=all_draw,
            pins=obstacles,
            wires=existing_wires,
            pin_stubs=pin_symbols,
            notes=f"stage={succeeded_stage} route drawn",
        )
    except Exception:
        log.warning("[viz] failed to dump 7-final", exc_info=True)

    return True


# ---------------------------------------------------------------------------
# Pin position resolution
# ---------------------------------------------------------------------------


def _get_pin_schematic_position(sch: Any, reference: str, pin_number: str) -> tuple[float, float]:
    """Return the absolute schematic (x, y) of a named pin on a placed symbol.

    Kept for backward compatibility.  Prefer :func:`_get_pin_position_and_direction`
    when the exit angle is also needed.

    Raises ValueError if the reference or pin number cannot be found.
    """
    x, y, _ = _get_pin_position_and_direction(sch, reference, pin_number)
    return x, y


def _get_pin_position_and_direction(
    sch: Any, reference: str, pin_number: str
) -> tuple[float, float, float]:
    """Return the absolute schematic (x, y, angle°) of a named pin.

    The angle is the direction the wire should leave the pin body
    (KiCad CCW file-angle convention on screen):
      0° → right,  90° → up,  180° → left,  270° → down.

    Handles the skip library bug for single-pin symbols (power symbols,
    TestPoint) via :func:`~kcaa.utils.skip_helpers.sym_pin_world_coords`.

    Raises ValueError if the reference or pin number cannot be found.
    """
    try:
        for sym in sch.symbol:
            try:
                ref_val = sym.property.Reference.value
            except AttributeError:
                continue
            if ref_val != reference:
                continue
            for pin in sym_pin_world_coords(sym):
                if pin.number == str(pin_number):
                    return pin.x, pin.y, pin.angle
    except AttributeError:
        pass
    raise ValueError(
        f"Pin {pin_number!r} not found on symbol {reference!r}. "
        "Check that the reference designator and pin number are correct."
    )


def _collect_all_pin_positions(sch: Any) -> list[tuple[float, float]]:
    """Return the absolute schematic position of every pin of every placed symbol.

    Uses :func:`~kcaa.utils.skip_helpers.sym_pin_world_coords` which
    handles the skip library bug for power symbols (VCC, GND, PWR_FLAG) and
    single-pin symbols (TestPoint).

    Returns:
        List of (x, y) tuples, one per pin.
    """
    positions: list[tuple[float, float]] = []
    try:
        for sym in sch.symbol:
            for pin in sym_pin_world_coords(sym):
                positions.append((pin.x, pin.y))
    except AttributeError:
        pass
    return positions


def _collect_all_pin_data(sch: Any) -> list[tuple[float, float, float]]:
    """Return the absolute schematic position and exit angle of every pin.

    Returns:
        List of (x, y, angle) tuples, one per pin.
    """
    data: list[tuple[float, float, float]] = []
    try:
        for sym in sch.symbol:
            for pin in sym_pin_world_coords(sym):
                data.append((pin.x, pin.y, pin.angle))
    except AttributeError:
        pass
    return data


def _symbol_pin_lengths(sch: Any) -> dict[str, dict[str, float]]:
    """{sanitized lib_id: {pin number: symbol stub length in mm}}.

    Reads the embedded ``lib_symbols`` of the schematic.  A KiCad pin symbol
    is drawn as a line from its electrical point along the pin's direction;
    its length (typically 2.54 or 3.81 mm) is part of the lib definition
    and is not copied into the placed symbol instance, so the router needs
    it from here to treat pin symbols as collision bodies.  Pins are
    exposed as ``Pin_1``..``Pin_n`` attributes (skip nodes don't support
    ``len()``/iteration on the wrapper).
    """
    if not hasattr(sch, "lib_symbols"):
        return {}
    libs = sch.lib_symbols
    result: dict[str, dict[str, float]] = {}
    for attr in dir(libs):
        if attr.startswith("_"):
            continue
        lib = getattr(libs, attr, None)
        pins = getattr(lib, "pin", None) if lib is not None else None
        if pins is None:
            continue
        lengths: dict[str, float] = {}
        for name in dir(pins):
            if not name.startswith("Pin_"):
                continue
            m = re.search(r"\d+", name)
            if not m:
                continue
            raw_len = str(
                getattr(pins, name, "").length
                if hasattr(getattr(pins, name, None), "length")
                else ""
            )
            lm = re.search(r"[\d.]+", raw_len)
            if lm:
                lengths[m.group(0)] = float(lm.group(0))
        if lengths:
            result[attr] = lengths
    return result


def _sanitize_lib_id(lib_id: str) -> str:
    """Map a lib id (``Vendor:Part``) to skip's sanitised attribute name."""
    return lib_id.replace(":", "_").replace("/", "_").replace(".", "_").replace("-", "_")


def _collect_pin_symbol_stubs(
    sch: Any,
) -> list[tuple[float, float, float, float]]:
    """World-space stub segments for every pin symbol.

    A KiCad pin symbol is drawn from its electrical point *into the
    component body* — the opposite of the wire-exit direction that
    ``sym_pin_world_coords`` reports.  These lines span the lib-defined pin
    length and are the collision bodies a wire must not run on top of (a
    plain point check against the electrical point alone misses e.g. a
    route tail drawn on the far side of the pin).  The stub gate exempts
    only outgoing leads on their own stub; all other overlaps are rejected.
    """
    lengths_by_lib = _symbol_pin_lengths(sch)
    stubs: list[tuple[float, float, float, float]] = []
    try:
        for sym in sch.symbol:
            raw_lib = getattr(sym, "lib_id", "")
            lib_id = getattr(raw_lib, "value", raw_lib)
            lib_lengths = lengths_by_lib.get(_sanitize_lib_id(str(lib_id)), {})
            try:
                for pin in sym_pin_world_coords(sym):
                    length = lib_lengths.get(str(pin.number), 2.54)
                    # Symbol line runs into the body: opposite the exit dir.
                    dx, dy = _dir_vec((pin.angle + 180.0) % 360.0)
                    stubs.append(
                        (
                            pin.x,
                            pin.y,
                            round(pin.x + dx * length, 4),
                            round(pin.y + dy * length, 4),
                        )
                    )
            except AttributeError:
                continue
    except AttributeError:
        pass
    return stubs


def _junction_exists_at(sch: Any, px: float, py: float, tol: float = 0.01) -> bool:
    """Return True if a junction already exists at (px, py) within tolerance."""
    try:
        for j in sch.junction:
            coords = j.at.value
            if abs(float(coords[0]) - px) <= tol and abs(float(coords[1]) - py) <= tol:
                return True
    except AttributeError:
        pass
    return False


def _wire_connected_at(sch: Any, px: float, py: float, tol: float = 0.01) -> bool:
    """Return True if any existing wire has an endpoint at (px, py) within tolerance."""
    try:
        for w in sch.wire:
            sx, sy = float(w.start.value[0]), float(w.start.value[1])
            ex, ey = float(w.end.value[0]), float(w.end.value[1])
            if (abs(sx - px) <= tol and abs(sy - py) <= tol) or (
                abs(ex - px) <= tol and abs(ey - py) <= tol
            ):
                return True
    except AttributeError:
        pass
    return False


def _points_already_connected(
    existing_wires: list[tuple[float, float, float, float]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tol: float,
) -> bool:
    """Return True if (x1,y1) and (x2,y2) are already electrically connected
    through the existing wire network.

    Uses a BFS over wire endpoints.  Two points are considered the same node
    when their coordinates are within *tol* mm of each other.  A point that
    lies on the interior of a wire segment (not at an endpoint) is also
    reachable via that wire.
    """
    if abs(x1 - x2) <= tol and abs(y1 - y2) <= tol:
        return True

    def _neighbors(px: float, py: float) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for ax, ay, bx, by in existing_wires:
            a_match = abs(ax - px) <= tol and abs(ay - py) <= tol
            b_match = abs(bx - px) <= tol and abs(by - py) <= tol
            interior = _point_on_open_segment(px, py, ax, ay, bx, by, tol)
            if a_match or interior:
                result.append((bx, by))
            if b_match or interior:
                result.append((ax, ay))
        return result

    visited: list[tuple[float, float]] = [(x1, y1)]
    queue: list[tuple[float, float]] = [(x1, y1)]
    while queue:
        cx, cy = queue.pop()
        for nx, ny in _neighbors(cx, cy):
            if abs(nx - x2) <= tol and abs(ny - y2) <= tol:
                return True
            if not any(abs(nx - vx) <= tol and abs(ny - vy) <= tol for vx, vy in visited):
                visited.append((nx, ny))
                queue.append((nx, ny))
    return False


def _split_wires_at_point(sch: Any, px: float, py: float, tol: float = 0.01) -> int:
    """Split any wire whose interior contains (px, py) into two segments.

    KiCad ≥ 10 silently deletes a junction that does not coincide with at
    least one wire endpoint when the schematic is opened. To make junctions
    at T-taps persist, the underlying wire must be split into two segments
    that share the junction coordinate as a common endpoint.

    A point at a wire endpoint is NOT treated as interior — no split is
    performed in that case (the existing endpoint already anchors the
    junction).

    Args:
        sch: kicad-skip Schematic object.
        px: X coordinate of the junction in mm.
        py: Y coordinate of the junction in mm.
        tol: Tolerance in mm for collinearity / endpoint matching.

    Returns:
        Number of wires that were split.
    """
    splits = 0
    try:
        wires_to_split: list[tuple[Any, float, float, float, float]] = []
        for w in sch.wire:
            try:
                ax = float(w.start.value[0])
                ay = float(w.start.value[1])
                bx = float(w.end.value[0])
                by = float(w.end.value[1])
            except (AttributeError, IndexError, TypeError):
                continue
            if _point_on_open_segment(px, py, ax, ay, bx, by, tol):
                wires_to_split.append((w, ax, ay, bx, by))
        for w, _ax, _ay, bx, by in wires_to_split:
            # Shorten the existing wire to (start)→(px,py); add a new wire
            # (px,py)→(original end) so the junction sits on a shared endpoint.
            w.end_at([px, py])
            nw = sch.wire.new()
            nw.start_at([px, py])
            nw.end_at([bx, by])
            splits += 1
    except AttributeError:
        pass
    return splits


def _add_junction_and_split(sch: Any, px: float, py: float, tol: float = 0.01) -> bool:
    """Add a junction at (px, py) and split any wire passing through it.

    No-op (returns False) if a junction already exists at (px, py); in that
    case wires are still split so the existing junction becomes anchored.

    Returns True if a new junction was created.
    """
    created = False
    if not _junction_exists_at(sch, px, py, tol):
        j = sch.junction.new()
        j.at.value = [px, py]
        created = True
    _split_wires_at_point(sch, px, py, tol)
    return created


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_wire_edit_tools(mcp: FastMCP) -> None:
    """Register all wire and junction editing tools with the MCP server."""

    @mcp.tool()
    async def connect_points_with_wire(
        schematic_path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Route a smart orthogonal wire between two raw schematic coordinates.

        Use this when endpoints are known bare coordinates (e.g. a net label
        position or an existing wire tip).  When both endpoints are symbol
        pins, prefer ``connect_pins_with_wire`` — it resolves pin coordinates
        automatically.  If this tool fails, fall back to
        ``add_wire_to_schematic`` (horizontal/vertical only).

        Routing is orthogonal (horizontal-vertical). Coordinates are mm in
        KiCad screen convention (**+Y is down**); align to the 1.27 mm grid.

        Junction behaviour:

        * If an endpoint lies on the **interior** of an existing wire, a
          junction is placed there and that wire is split at the endpoint
          (required so KiCad ≥ 10 keeps the junction on reload).
        * If an endpoint coincides with an existing wire endpoint or a pin
          that already has a wire, a junction is placed automatically.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start in mm.
            start_y: Y coordinate of the wire start in mm.
            end_x: X coordinate of the wire end in mm.
            end_y: Y coordinate of the wire end in mm.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of {x, y} for every junction inserted).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        for name, val in [
            ("start_x", start_x),
            ("start_y", start_y),
            ("end_x", end_x),
            ("end_y", end_y),
        ]:
            if not math.isfinite(val):
                return {"error": f"Coordinate '{name}' must be a finite number (got {val})"}
        if start_x == end_x and start_y == end_y:
            return {"error": "Wire start and end points are identical (zero-length wire)"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            all_pin_data = _collect_all_pin_data(sch)
            obstacles = [(x, y) for x, y, _ in all_pin_data]
            existing_wires = _collect_existing_wires(sch)
            tol = _PIN_COLLISION_TOL

            if _points_already_connected(existing_wires, start_x, start_y, end_x, end_y, tol):
                return {
                    "error": (
                        f"Points ({start_x}, {start_y}) and ({end_x}, {end_y}) are already "
                        "connected through existing wires — no new wire needed."
                    )
                }

            def _find_pin_angle(px: float, py: float) -> float | None:
                for x, y, angle in all_pin_data:
                    if abs(x - px) <= tol and abs(y - py) <= tol:
                        return angle
                return None

            def _is_on_wire_interior(px: float, py: float) -> bool:
                return any(
                    _point_on_open_segment(px, py, ax, ay, bx, by, tol)
                    for (ax, ay, bx, by) in existing_wires
                )

            junctions_added: list[dict[str, float]] = []
            for jx, jy in [(start_x, start_y), (end_x, end_y)]:
                needs = _is_on_wire_interior(jx, jy) or (
                    _wire_connected_at(sch, jx, jy) and not _junction_exists_at(sch, jx, jy)
                )
                if needs and _add_junction_and_split(sch, jx, jy):
                    junctions_added.append({"x": jx, "y": jy})

            start_angle_wire = _find_pin_angle(start_x, start_y)
            end_angle_wire = _find_pin_angle(end_x, end_y)

            # Refresh after any splits so _draw_smart_wire sees the current
            # wire topology (not the pre-split long wire which would cause
            # false overlap rejections for collinear routes).
            existing_wires = _collect_existing_wires(sch)

            # If splitting created the exact segment we need (both endpoints
            # were on the same wire's interior), the connection already exists
            # — skip routing to avoid drawing a redundant U-detour path.
            direct_exists = any(
                (
                    abs(ax - start_x) <= tol
                    and abs(ay - start_y) <= tol
                    and abs(bx - end_x) <= tol
                    and abs(by - end_y) <= tol
                )
                or (
                    abs(ax - end_x) <= tol
                    and abs(ay - end_y) <= tol
                    and abs(bx - start_x) <= tol
                    and abs(by - start_y) <= tol
                )
                for ax, ay, bx, by in existing_wires
            )
            if not direct_exists:
                ok = _draw_smart_wire(
                    sch,
                    start_x,
                    start_y,
                    end_x,
                    end_y,
                    existing_wires=existing_wires,
                    start_angle=start_angle_wire,
                    end_angle=end_angle_wire,
                    obstacle_pins=obstacles,
                )
                if not ok:
                    return {
                        "error": "No valid route found: all routing candidates overlap existing wires or collide with component pins"
                    }

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to add wire: {exc}"}

        return {
            "success": True,
            "wire": {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
            },
            "junctions_added": junctions_added,
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }

    async def add_wire_to_schematic(
        schematic_path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Draw a single horizontal or vertical wire segment (naive fallback).

        **Use this tool only if connect_pins_with_wire and
        connect_points_with_wire have both failed.** If this tool also fails,
        stop and report the failure and the coordinates to the user.

        Only horizontal (same Y) or vertical (same X) segments are supported.
        Returns an error for diagonal endpoints — use
        ``connect_points_with_wire`` for those.

        Junction behaviour:

        * If an endpoint lies on the **interior** of an existing wire, a
          junction is placed and that wire is split at the endpoint.
        * If an endpoint coincides with an existing wire endpoint or a pin
          that already has a wire, a junction is placed automatically.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start in mm.
            start_y: Y coordinate of the wire start in mm.
            end_x: X coordinate of the wire end in mm.
            end_y: Y coordinate of the wire end in mm.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of {x, y} for every junction inserted).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        for name, val in [
            ("start_x", start_x),
            ("start_y", start_y),
            ("end_x", end_x),
            ("end_y", end_y),
        ]:
            if not math.isfinite(val):
                return {"error": f"Coordinate '{name}' must be a finite number (got {val})"}
        if start_x == end_x and start_y == end_y:
            return {"error": "Wire start and end points are identical (zero-length wire)"}
        if abs(start_x - end_x) > 1e-9 and abs(start_y - end_y) > 1e-9:
            return {
                "error": (
                    "add_wire_to_schematic only supports horizontal or vertical segments. "
                    f"Got start=({start_x}, {start_y}) end=({end_x}, {end_y}). "
                    "Use connect_points_with_wire for orthogonal routing."
                )
            }

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            existing_wires = _collect_existing_wires(sch)
            tol = _PIN_COLLISION_TOL

            if _points_already_connected(existing_wires, start_x, start_y, end_x, end_y, tol):
                return {
                    "error": (
                        f"Points ({start_x}, {start_y}) and ({end_x}, {end_y}) are already "
                        "connected through existing wires — no new wire needed."
                    )
                }

            def _is_on_wire_interior(px: float, py: float) -> bool:
                return any(
                    _point_on_open_segment(px, py, ax, ay, bx, by, tol)
                    for (ax, ay, bx, by) in existing_wires
                )

            junctions_added: list[dict[str, float]] = []
            for jx, jy in [(start_x, start_y), (end_x, end_y)]:
                needs = _is_on_wire_interior(jx, jy) or (
                    _wire_connected_at(sch, jx, jy) and not _junction_exists_at(sch, jx, jy)
                )
                if needs and _add_junction_and_split(sch, jx, jy):
                    junctions_added.append({"x": jx, "y": jy})

            # Refresh after any splits: the pre-split long wire is gone,
            # replaced by shorter segments.  If splitting already created the
            # exact segment we need, skip drawing to avoid a duplicate wire.
            existing_wires = _collect_existing_wires(sch)
            segment_exists = any(
                (
                    abs(ax - start_x) <= tol
                    and abs(ay - start_y) <= tol
                    and abs(bx - end_x) <= tol
                    and abs(by - end_y) <= tol
                )
                or (
                    abs(ax - end_x) <= tol
                    and abs(ay - end_y) <= tol
                    and abs(bx - start_x) <= tol
                    and abs(by - start_y) <= tol
                )
                for ax, ay, bx, by in existing_wires
            )
            if not segment_exists:
                w = sch.wire.new()
                w.start_at([start_x, start_y])
                w.end_at([end_x, end_y])

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to add wire: {exc}"}

        return {
            "success": True,
            "wire": {
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
            },
            "junctions_added": junctions_added,
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }

    @mcp.tool()
    async def connect_pins_with_wire(
        schematic_path: str,
        from_ref: str,
        from_pin: str,
        to_ref: str,
        to_pin: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect two symbol pins with a wire, using smart orthogonal routing.

        Resolves the absolute schematic coordinates of both pins automatically
        (accounting for each symbol's placement position and rotation), then
        draws a wire between them using smart orthogonal routing that follows
        pin exit directions and avoids other component pins. If either pin is
        already connected to a wire and has no junction yet, a junction is
        automatically placed there before drawing the new wire. A backup
        (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            from_ref: Reference designator of the source symbol (e.g. "R1").
            from_pin: Pin number of the source pin (e.g. "1").
            to_ref: Reference designator of the destination symbol (e.g. "C1").
            to_pin: Pin number of the destination pin (e.g. "2").

        Returns:
            dict with keys: success (bool), wire (from/to with ref, pin, x, y),
            collision_free (bool), auto_junctions_added (list of {x, y}, only
            when junctions were automatically placed).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            start_x, start_y, start_angle = _get_pin_position_and_direction(sch, from_ref, from_pin)
        except ValueError as exc:
            return {"error": str(exc)}

        try:
            end_x, end_y, end_angle = _get_pin_position_and_direction(sch, to_ref, to_pin)
        except ValueError as exc:
            return {"error": str(exc)}

        if start_x == end_x and start_y == end_y:
            return {
                "success": True,
                "wire": {
                    "from": {"ref": from_ref, "pin": from_pin, "x": start_x, "y": start_y},
                    "to": {"ref": to_ref, "pin": to_pin, "x": end_x, "y": end_y},
                },
                "note": (
                    f"{from_ref} pin {from_pin} and {to_ref} pin {to_pin} share the same "
                    "schematic coordinate — they are co-located on a shared stub and are "
                    "already connected. No wire was drawn."
                ),
            }

        existing_wires_precheck = _collect_existing_wires(sch)
        if _points_already_connected(
            existing_wires_precheck, start_x, start_y, end_x, end_y, _PIN_COLLISION_TOL
        ):
            return {
                "error": (
                    f"{from_ref} pin {from_pin} and {to_ref} pin {to_pin} are already "
                    "connected through existing wires — no new wire needed."
                )
            }

        try:
            # Auto-junction: if a pin already connects to a wire, add a junction
            # before drawing the new wire so the T-connection is explicit.
            auto_junctions: list[dict[str, float]] = []
            for jx, jy in [(start_x, start_y), (end_x, end_y)]:
                if _wire_connected_at(sch, jx, jy) and not _junction_exists_at(sch, jx, jy):
                    j = sch.junction.new()
                    j.at.value = [jx, jy]
                    auto_junctions.append({"x": jx, "y": jy})

            # All pins are obstacles — _point_on_open_segment uses a strict
            # interior check (lo = min+tol, hi = max−tol) so the two endpoint
            # pins at (start_x,start_y) and (end_x,end_y) are never flagged as
            # interior points on their own lead-out stubs.  Including them lets
            # the router correctly reject any inner-route candidate that would
            # pass *through* the end pin, which would otherwise produce a
            # self-overlapping backtrack wire.
            obstacles = _collect_all_pin_positions(sch)
            existing_wires = _collect_existing_wires(sch)

            # Smart routing: follow pin exit directions, avoid all other pins
            # and existing wire segments
            ok = _draw_smart_wire(
                sch,
                start_x,
                start_y,
                end_x,
                end_y,
                existing_wires=existing_wires,
                start_angle=start_angle,
                end_angle=end_angle,
                obstacle_pins=obstacles,
            )
            if not ok:
                return {
                    "error": f"No valid route found between {from_ref} pin {from_pin} and {to_ref} pin {to_pin}: all routing candidates overlap existing wires or collide with component pins"
                }

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to add wire: {exc}"}

        result: dict[str, Any] = {
            "success": True,
            "wire": {
                "from": {"ref": from_ref, "pin": from_pin, "x": start_x, "y": start_y},
                "to": {"ref": to_ref, "pin": to_pin, "x": end_x, "y": end_y},
            },
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }
        if auto_junctions:
            result["auto_junctions_added"] = auto_junctions
        return result

    @mcp.tool()
    async def delete_wire_from_schematic(
        schematic_path: str,
        wires: list[dict],
        tolerance: float = 0.01,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete one or more wire segments from a KiCad schematic by their endpoints.

        Opens the schematic once, removes all matching wire segments in a single
        pass, then writes the file once — making batch deletions efficient.

        Each entry in ``wires`` must be a dict with keys:
            ``start_x``, ``start_y``, ``end_x``, ``end_y`` (all floats, in mm).

        Both directions of a segment are matched (A→B or B→A).  Use
        analyze_schematic_connections(include_wire_topology=True) first to
        obtain exact wire coordinates (connected wires appear under each net's
        ``wires`` list; unconnected stubs appear under ``unconnected_wires``).
        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            wires: List of wire specs, each a dict with start_x, start_y,
                end_x, end_y (floats in mm).
            tolerance: Maximum coordinate difference considered a match
                (default 0.01 mm).

        Returns:
            dict with keys:
                success (bool),
                deleted_count (int) — total wire objects removed,
                not_found (list[int]) — 0-based indices of wire specs that
                    had no match in the schematic.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not wires:
            return {"error": "The 'wires' list must not be empty"}

        # Validate all wire specs up front.
        parsed: list[tuple[float, float, float, float]] = []
        for i, spec in enumerate(wires):
            try:
                sx = float(spec["start_x"])
                sy = float(spec["start_y"])
                ex = float(spec["end_x"])
                ey = float(spec["end_y"])
            except (KeyError, TypeError, ValueError) as exc:
                return {"error": f"Wire spec at index {i} is invalid: {exc}"}
            for name, val in [("start_x", sx), ("start_y", sy), ("end_x", ex), ("end_y", ey)]:
                if not math.isfinite(val):
                    return {
                        "error": f"Wire spec at index {i}: '{name}' must be a finite number (got {val})"
                    }
            parsed.append((sx, sy, ex, ey))

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Collect schematic wires once.
            try:
                all_wires = list(sch.wire)
            except AttributeError:
                all_wires = []

            to_delete: list = []
            matched = [False] * len(parsed)

            for w in all_wires:
                wx0 = float(w.start.value[0])
                wy0 = float(w.start.value[1])
                wx1 = float(w.end.value[0])
                wy1 = float(w.end.value[1])
                for i, (sx, sy, ex, ey) in enumerate(parsed):
                    forward = (
                        abs(wx0 - sx) <= tolerance
                        and abs(wy0 - sy) <= tolerance
                        and abs(wx1 - ex) <= tolerance
                        and abs(wy1 - ey) <= tolerance
                    )
                    backward = (
                        abs(wx0 - ex) <= tolerance
                        and abs(wy0 - ey) <= tolerance
                        and abs(wx1 - sx) <= tolerance
                        and abs(wy1 - sy) <= tolerance
                    )
                    if forward or backward:
                        to_delete.append(w)
                        matched[i] = True
                        break  # each schematic wire can only match one spec

            not_found = [i for i, m in enumerate(matched) if not m]

            if not to_delete:
                return {
                    "error": "No wire matched any of the provided specs within tolerance",
                    "not_found": not_found,
                }

            for w in to_delete:
                w.delete()

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to delete wire: {exc}"}

        result: dict[str, Any] = {
            "success": True,
            "deleted_count": len(to_delete),
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }
        if not_found:
            result["not_found"] = not_found
        return result
