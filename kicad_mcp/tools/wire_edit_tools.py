"""Wire and junction editing tools for KiCad MCP server.

Provides tools to draw, list, and delete wire segments and junction dots
in KiCad schematics using the skip library.
"""

import logging
import math
import os
import shutil
from typing import Any

import skip
from fastmcp import Context
from fastmcp import FastMCP

from kicad_mcp.utils.skip_helpers import sym_pin_world_coords

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing constants
# ---------------------------------------------------------------------------

_LEAD_OUT_DIST: float = 2.54   # mm — one KiCad grid step, pulls wire into open space
_PIN_COLLISION_TOL: float = 0.5  # mm — clearance radius around each obstacle pin


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _dir_vec(angle_deg: float) -> tuple[float, float]:
    """Return the unit direction vector for a KiCad pin exit angle.

    KiCad schematic coordinates have Y pointing **down** on screen.
    Pin angle semantics (KiCad / skip convention):
      0°   → pointing right  (+X)
      90°  → pointing down   (+Y, screen)
      180° → pointing left   (−X)
      270° → pointing up     (−Y, screen)
    """
    a = int(round(angle_deg)) % 360
    return {
        0: (1.0, 0.0),
        90: (0.0, 1.0),
        180: (-1.0, 0.0),
        270: (0.0, -1.0),
    }.get(a, (math.cos(math.radians(a)), math.sin(math.radians(a))))


def _point_on_open_segment(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
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
    for (ax, ay, bx, by) in segments:
        for (px, py) in obstacles:
            if _point_on_open_segment(px, py, ax, ay, bx, by, tol):
                return True
    return False


def _route_collides_at_corners(
    segments: list[tuple[float, float, float, float]],
    obstacles: list[tuple[float, float]],
    tol: float,
    route_sx: float, route_sy: float,
    route_ex: float, route_ey: float,
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
            if (abs(cx - route_sx) <= tol and abs(cy - route_sy) <= tol):
                continue
            if (abs(cx - route_ex) <= tol and abs(cy - route_ey) <= tol):
                continue
            for px, py in obstacles:
                if abs(cx - px) <= tol and abs(cy - py) <= tol:
                    return True
    return False


def _route_candidates(
    x1: float, y1: float, x2: float, y2: float,
) -> list[list[tuple[float, float, float, float]]]:
    """Return a ranked list of candidate segment-lists connecting (x1,y1)→(x2,y2).

    Each candidate is a list of (ax, ay, bx, by) axis-aligned segments.
    Candidates are tried in order; the first collision-free one wins.

    Order:
      1. Direct (1 segment) — only when points are axis-aligned.
      2. L-A: horizontal-first via corner (x2, y1).
      3. L-B: vertical-first via corner (x1, y2).
      4-10.  7 horizontal Z-routes: jog at x = x1 + k*(x2−x1)/8 for k=1..7.
      11-17. 7 vertical Z-routes:   jog at y = y1 + k*(y2−y1)/8 for k=1..7.
      18-29. U-detours for the collinear case (same axis, direct route blocked):
             jog ±2.54, ±5.08, ±7.62 mm perpendicular, crossing full span, then back.
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
        # Horizontal Z-routes: jog the vertical column to a fractional x
        for k in range(1, 8):
            xj = x1 + dx * k / 8.0
            candidates.append([
                (x1, y1, xj, y1),
                (xj, y1, xj, y2),
                (xj, y2, x2, y2),
            ])
        # Vertical Z-routes: jog the horizontal row to a fractional y
        for k in range(1, 8):
            yj = y1 + dy * k / 8.0
            candidates.append([
                (x1, y1, x1, yj),
                (x1, yj, x2, yj),
                (x2, yj, x2, y2),
            ])

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
                candidates.append([
                    (x1, y1, x1, yj),
                    (x1, yj, x2, yj),
                    (x2, yj, x2, y2),
                ])
    elif abs(dx) < 1e-9 and abs(dy) > 1e-9:
        # Vertical collinear: jog in ±x
        for n in _U_STEPS:
            for xoff in (n * _GRID, -n * _GRID):
                xj = x1 + xoff
                candidates.append([
                    (x1, y1, xj, y1),
                    (xj, y1, xj, y2),
                    (xj, y2, x2, y2),
                ])

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
                    if (abs(ay - by) < 1e-9 and abs(cy - dy) < 1e-9
                            and abs(ay - dy) < 1e-9):
                        merged.append((ax, ay, dx, dy))
                        i += 2
                        changed = True
                        continue
                    # Vertical merge: all four x values the same
                    if (abs(ax - bx) < 1e-9 and abs(cx - dx) < 1e-9
                            and abs(ax - dx) < 1e-9):
                        merged.append((ax, ay, dx, dy))
                        i += 2
                        changed = True
                        continue
            merged.append(result[i])
            i += 1
        result = merged
    return result


def _segments_overlap(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
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
    for (ax, ay, bx, by) in segments:
        for (cx, cy, dx, dy) in existing_wires:
            if _segments_overlap(ax, ay, bx, by, cx, cy, dx, dy, tol):
                return True
    return False


def _collect_existing_wires(sch: Any) -> list[tuple[float, float, float, float]]:
    """Return all wire segments currently in the schematic as (ax, ay, bx, by) tuples."""
    wires: list[tuple[float, float, float, float]] = []
    try:
        for w in sch.wire:
            wires.append((
                float(w.start.value[0]), float(w.start.value[1]),
                float(w.end.value[0]),   float(w.end.value[1]),
            ))
    except AttributeError:
        pass
    return wires


def _follow_wire_extent(
    sx: float, sy: float,
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
            for (ex, ey, ox, oy) in ((ax, ay, bx, by), (bx, by, ax, ay)):
                if abs(ex - cx) > tol or abs(ey - cy) > tol:
                    continue
                dx, dy = ox - cx, oy - cy
                dist = math.sqrt(dx * dx + dy * dy)
                if dist < tol:
                    continue
                if abs(dx / dist - dvx) < 0.1 and abs(dy / dist - dvy) < 0.1:
                    # Stop before landing on a component pin
                    if obstacle_pins and any(
                        abs(ox - px) <= tol and abs(oy - py) <= tol
                        for px, py in obstacle_pins
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


def _infer_angles_toward(
    x1: float, y1: float, x2: float, y2: float,
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


# ---------------------------------------------------------------------------
# Smart wire router
# ---------------------------------------------------------------------------

def _draw_smart_wire(
    sch: Any,
    sx: float, sy: float,
    ex: float, ey: float,
    existing_wires: list[tuple[float, float, float, float]],
    start_angle: float | None = None,
    end_angle: float | None = None,
    obstacle_pins: list[tuple[float, float]] | None = None,
    lead_out_dist: float = _LEAD_OUT_DIST,
) -> bool:
    """Draw a smart wire from (sx,sy) to (ex,ey) avoiding pins and existing wires.

    Algorithm:
      1. Compute a lead-out stub from each endpoint following the pin's exit
         direction (if angle is supplied), moving the route into open space.
         If a lead would overlap an existing wire (pin already connected in
         that direction), the lead is suppressed and the inner route starts
         from the lead endpoint — no duplicate segment is drawn.
      2. Try up to 16 candidate inner routes between the lead-out endpoints.
      3. Pick the first candidate where no obstacle pin falls on the interior
         of any segment AND no segment overlaps an existing wire.
      4. If all candidates are blocked, draw nothing and return False.

    When a lead-out is suppressed (an existing wire already covers that
    direction from the pin), the routing start/end point is advanced to the
    far end of the existing wire chain via _follow_wire_extent, and a junction
    dot is placed there to mark the new T-branch.

    Args:
        sch: The skip schematic object.
        sx, sy: Start point (pin position).
        ex, ey: End point (pin position).
        existing_wires: All wire segments already in the schematic, as
            (ax, ay, bx, by) tuples.  Used to prevent overlapping routes.
        start_angle: Absolute exit angle of the start pin in degrees
            (0=right, 90=down, 180=left, 270=up).  None to skip lead-out.
        end_angle: Absolute exit angle of the end pin in degrees.
        obstacle_pins: List of (x, y) positions to avoid.
        lead_out_dist: Length of each lead-out stub in mm (default 2.54 mm).

    Returns:
        True if a valid route was found and drawn, False if no valid route
        exists (nothing is drawn in that case).
    """
    obstacles = obstacle_pins or []

    # Compute lead-out inner endpoints
    if start_angle is not None:
        dvx, dvy = _dir_vec(start_angle)
        p1x = round(sx + dvx * lead_out_dist, 4)
        p1y = round(sy + dvy * lead_out_dist, 4)
    else:
        p1x, p1y = sx, sy

    if end_angle is not None:
        dvx, dvy = _dir_vec(end_angle)
        p2x = round(ex + dvx * lead_out_dist, 4)
        p2y = round(ey + dvy * lead_out_dist, 4)
    else:
        p2x, p2y = ex, ey

    # Build lead-out segments.  If a lead would overlap an existing wire
    # (the pin already has a wire leaving in that direction), suppress the
    # lead segment — the existing wire covers that path — but keep p1/p2 at
    # the lead endpoint so the inner route still starts/ends in open space.
    # This avoids duplicate wire segments when a pin has multiple connections.
    #
    # If the lead-out is blocked by a component pin (not a wire), it means
    # there is insufficient clearance for a stub in that direction.  In that
    # case skip the lead entirely and route directly from the pin tip.  Route
    # candidates that would immediately head toward the pin body (stub
    # direction) are then filtered out to avoid routing into the component.
    #
    # The end lead-out is stored reversed (inner-endpoint → pin) so that when
    # all segments are concatenated the end of one segment always touches the
    # start of the next — a prerequisite for _merge_collinear_segments.
    start_lead: list[tuple[float, float, float, float]] = []
    end_lead: list[tuple[float, float, float, float]] = []
    start_suppressed = False
    end_suppressed = False
    start_lead_pin_blocked = False  # True when lead-out skipped due to pin obstacle
    end_lead_pin_blocked = False
    if start_angle is not None and (abs(p1x - sx) > 1e-9 or abs(p1y - sy) > 1e-9):
        seg = (sx, sy, p1x, p1y)
        if _route_overlaps_wires([seg], existing_wires, _PIN_COLLISION_TOL):
            # Existing wire already covers the lead direction.  Advance p1 to
            # the far end of the existing wire chain so the inner route starts
            # past the entire covered segment.  A junction will be placed at
            # p1 after drawing to mark the new T-branch.
            p1x, p1y = _follow_wire_extent(sx, sy, start_angle, existing_wires, _PIN_COLLISION_TOL)
            start_suppressed = True
        elif (_route_collides([seg], obstacles, _PIN_COLLISION_TOL)
              or any(abs(p1x - ox) <= _PIN_COLLISION_TOL and abs(p1y - oy) <= _PIN_COLLISION_TOL
                     for ox, oy in obstacles)):
            # A component pin blocks the lead-out stub (either in its interior
            # or exactly at the lead tip) — no room in that direction.  Route
            # directly from the pin tip and remember to filter candidates that
            # head toward the pin body.
            p1x, p1y = sx, sy
            start_lead_pin_blocked = True
        else:
            start_lead.append(seg)
    if end_angle is not None and (abs(p2x - ex) > 1e-9 or abs(p2y - ey) > 1e-9):
        seg = (ex, ey, p2x, p2y)  # canonical direction for overlap check
        if _route_overlaps_wires([seg], existing_wires, _PIN_COLLISION_TOL):
            # Same: advance p2 to the far end of the existing wire chain.
            p2x, p2y = _follow_wire_extent(ex, ey, end_angle, existing_wires, _PIN_COLLISION_TOL)
            end_suppressed = True
        elif (_route_collides([seg], obstacles, _PIN_COLLISION_TOL)
              or any(abs(p2x - ox) <= _PIN_COLLISION_TOL and abs(p2y - oy) <= _PIN_COLLISION_TOL
                     for ox, oy in obstacles)):
            # A component pin blocks the end lead-out stub (interior or tip).
            p2x, p2y = ex, ey
            end_lead_pin_blocked = True
        else:
            # Reversed: inner-endpoint → pin, so it continues naturally from chosen
            end_lead.append((p2x, p2y, ex, ey))

    # For collision detection the direction of each segment doesn't matter
    lead_segs = start_lead + [(a, b, c, d) for (c, d, a, b) in end_lead]

    # Try each inner route candidate
    inner_candidates = _route_candidates(p1x, p1y, p2x, p2y)

    # When a lead-out was skipped because a component pin blocked it, filter
    # out route candidates that immediately head in the pin body direction
    # (stub direction = opposite of exit angle).  Those directions are blocked
    # by the component and would produce invalid routes.
    if start_lead_pin_blocked and start_angle is not None:
        _body_dvx, _body_dvy = _dir_vec((start_angle + 180) % 360)
        inner_candidates = [
            c for c in inner_candidates
            if not (
                c
                and abs(c[0][0] - p1x) < 1e-6
                and abs(c[0][1] - p1y) < 1e-6
                and _body_dvx * (c[0][2] - c[0][0]) + _body_dvy * (c[0][3] - c[0][1]) > 1e-6
            )
        ]
    if end_lead_pin_blocked and end_angle is not None:
        _body_dvx, _body_dvy = _dir_vec((end_angle + 180) % 360)
        inner_candidates = [
            c for c in inner_candidates
            if not (
                c
                and abs(c[-1][2] - p2x) < 1e-6
                and abs(c[-1][3] - p2y) < 1e-6
                and _body_dvx * (c[-1][0] - p2x) + _body_dvy * (c[-1][1] - p2y) > 1e-6
            )
        ]

    chosen: list[tuple[float, float, float, float]] | None = None
    for candidate in inner_candidates:
        all_segs = lead_segs + candidate
        if (not _route_collides(all_segs, obstacles, _PIN_COLLISION_TOL)
                and not _route_overlaps_wires(all_segs, existing_wires, _PIN_COLLISION_TOL)
                and not _route_collides_at_corners(
                    all_segs, obstacles, _PIN_COLLISION_TOL,
                    sx, sy, ex, ey,
                )):
            chosen = candidate
            break

    if chosen is None:
        log.warning(
            "smart_wire: all %d route candidates are blocked (pin collision or "
            "wire overlap) between (%.3f,%.3f) and (%.3f,%.3f); no wire drawn.",
            len(inner_candidates), sx, sy, ex, ey,
        )
        return False

    # Draw all segments, merging collinear neighbours first.
    # Order: start_lead → inner segments → end_lead (reversed) ensures that
    # consecutive segments always share an endpoint, which is required for
    # _merge_collinear_segments to collapse collinear pairs correctly.
    all_draw = _merge_collinear_segments(start_lead + chosen + end_lead)
    for (ax, ay, bx, by) in all_draw:
        if abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9:
            continue  # skip zero-length
        w = sch.wire.new()
        w.start_at([ax, ay])
        w.end_at([bx, by])

    # Place junction dots at suppressed-lead endpoints.  These are T-branch
    # points where the new route branches off an existing wire.
    for jx, jy in (
        [(p1x, p1y)] if start_suppressed else []
    ) + (
        [(p2x, p2y)] if end_suppressed else []
    ):
        _add_junction_and_split(sch, jx, jy)

    return True


# ---------------------------------------------------------------------------
# Pin position resolution
# ---------------------------------------------------------------------------

def _get_pin_schematic_position(
    sch: Any, reference: str, pin_number: str
) -> tuple[float, float]:
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

    The angle is the direction the wire should leave the pin body:
      0° → right,  90° → down (screen),  180° → left,  270° → up (screen).

    Handles the skip library bug for single-pin symbols (power symbols,
    TestPoint) via :func:`~kicad_mcp.utils.skip_helpers.sym_pin_world_coords`.

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

    Uses :func:`~kicad_mcp.utils.skip_helpers.sym_pin_world_coords` which
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
            if (abs(sx - px) <= tol and abs(sy - py) <= tol) or \
               (abs(ex - px) <= tol and abs(ey - py) <= tol):
                return True
    except AttributeError:
        pass
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


def _add_junction_and_split(
    sch: Any, px: float, py: float, tol: float = 0.01
) -> bool:
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
        add_junction_start: bool = False,
        add_junction_end: bool = False,
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
        * ``add_junction_start`` / ``add_junction_end`` force a junction at
          that endpoint regardless of the heuristics above.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start in mm.
            start_y: Y coordinate of the wire start in mm.
            end_x: X coordinate of the wire end in mm.
            end_y: Y coordinate of the wire end in mm.
            add_junction_start: Force a junction dot at the start point.
            add_junction_end: Force a junction dot at the end point.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of {x, y} for every junction inserted).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        for name, val in [
            ("start_x", start_x), ("start_y", start_y),
            ("end_x", end_x), ("end_y", end_y),
        ]:
            if not math.isfinite(val):
                return {"error": f"Coordinate '{name}' must be a finite number (got {val})"}
        if start_x == end_x and start_y == end_y:
            return {"error": "Wire start and end points are identical (zero-length wire)"}

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            all_pin_data = _collect_all_pin_data(sch)
            obstacles = [(x, y) for x, y, _ in all_pin_data]
            existing_wires = _collect_existing_wires(sch)
            tol = _PIN_COLLISION_TOL

            def _find_pin_angle(px: float, py: float) -> float | None:
                for (x, y, angle) in all_pin_data:
                    if abs(x - px) <= tol and abs(y - py) <= tol:
                        return angle
                return None

            def _is_on_wire_interior(px: float, py: float) -> bool:
                return any(
                    _point_on_open_segment(px, py, ax, ay, bx, by, tol)
                    for (ax, ay, bx, by) in existing_wires
                )

            junctions_added: list[dict[str, float]] = []
            for jx, jy, flag in [
                (start_x, start_y, add_junction_start),
                (end_x, end_y, add_junction_end),
            ]:
                needs = (
                    flag
                    or _is_on_wire_interior(jx, jy)
                    or (_wire_connected_at(sch, jx, jy) and not _junction_exists_at(sch, jx, jy))
                )
                if needs:
                    if _add_junction_and_split(sch, jx, jy):
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
                    abs(ax - start_x) <= tol and abs(ay - start_y) <= tol
                    and abs(bx - end_x) <= tol and abs(by - end_y) <= tol
                ) or (
                    abs(ax - end_x) <= tol and abs(ay - end_y) <= tol
                    and abs(bx - start_x) <= tol and abs(by - start_y) <= tol
                )
                for ax, ay, bx, by in existing_wires
            )
            if not direct_exists:
                ok = _draw_smart_wire(
                    sch, start_x, start_y, end_x, end_y,
                    existing_wires=existing_wires,
                    start_angle=start_angle_wire,
                    end_angle=end_angle_wire,
                    obstacle_pins=obstacles,
                )
                if not ok:
                    return {"error": "No valid route found: all routing candidates overlap existing wires or collide with component pins"}

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
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
    async def add_wire_to_schematic(
        schematic_path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        add_junction_start: bool = False,
        add_junction_end: bool = False,
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
        * ``add_junction_start`` / ``add_junction_end`` force a junction at
          that endpoint.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start in mm.
            start_y: Y coordinate of the wire start in mm.
            end_x: X coordinate of the wire end in mm.
            end_y: Y coordinate of the wire end in mm.
            add_junction_start: Force a junction dot at the start point.
            add_junction_end: Force a junction dot at the end point.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of {x, y} for every junction inserted).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        for name, val in [
            ("start_x", start_x), ("start_y", start_y),
            ("end_x", end_x), ("end_y", end_y),
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
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            existing_wires = _collect_existing_wires(sch)
            tol = _PIN_COLLISION_TOL

            def _is_on_wire_interior(px: float, py: float) -> bool:
                return any(
                    _point_on_open_segment(px, py, ax, ay, bx, by, tol)
                    for (ax, ay, bx, by) in existing_wires
                )

            junctions_added: list[dict[str, float]] = []
            for jx, jy, flag in [
                (start_x, start_y, add_junction_start),
                (end_x, end_y, add_junction_end),
            ]:
                needs = (
                    flag
                    or _is_on_wire_interior(jx, jy)
                    or (_wire_connected_at(sch, jx, jy) and not _junction_exists_at(sch, jx, jy))
                )
                if needs:
                    if _add_junction_and_split(sch, jx, jy):
                        junctions_added.append({"x": jx, "y": jy})

            # Refresh after any splits: the pre-split long wire is gone,
            # replaced by shorter segments.  If splitting already created the
            # exact segment we need, skip drawing to avoid a duplicate wire.
            existing_wires = _collect_existing_wires(sch)
            segment_exists = any(
                (
                    abs(ax - start_x) <= tol and abs(ay - start_y) <= tol
                    and abs(bx - end_x) <= tol and abs(by - end_y) <= tol
                ) or (
                    abs(ax - end_x) <= tol and abs(ay - end_y) <= tol
                    and abs(bx - start_x) <= tol and abs(by - start_y) <= tol
                )
                for ax, ay, bx, by in existing_wires
            )
            if not segment_exists:
                w = sch.wire.new()
                w.start_at([start_x, start_y])
                w.end_at([end_x, end_y])

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
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
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            start_x, start_y, start_angle = _get_pin_position_and_direction(
                sch, from_ref, from_pin
            )
        except ValueError as exc:
            return {"error": str(exc)}

        try:
            end_x, end_y, end_angle = _get_pin_position_and_direction(
                sch, to_ref, to_pin
            )
        except ValueError as exc:
            return {"error": str(exc)}

        if start_x == end_x and start_y == end_y:
            return {"error": "Both pins are at the same coordinate; cannot draw a wire"}

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
                sch, start_x, start_y, end_x, end_y,
                existing_wires=existing_wires,
                start_angle=start_angle,
                end_angle=end_angle,
                obstacle_pins=obstacles,
            )
            if not ok:
                return {"error": f"No valid route found between {from_ref} pin {from_pin} and {to_ref} pin {to_pin}: all routing candidates overlap existing wires or collide with component pins"}

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
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
                    return {"error": f"Wire spec at index {i}: '{name}' must be a finite number (got {val})"}
            parsed.append((sx, sy, ex, ey))

        try:
            sch = skip.Schematic(schematic_path)
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
                        abs(wx0 - sx) <= tolerance and abs(wy0 - sy) <= tolerance
                        and abs(wx1 - ex) <= tolerance and abs(wy1 - ey) <= tolerance
                    )
                    backward = (
                        abs(wx0 - ex) <= tolerance and abs(wy0 - ey) <= tolerance
                        and abs(wx1 - sx) <= tolerance and abs(wy1 - sy) <= tolerance
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

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to delete wire: {exc}"}

        result: dict[str, Any] = {"success": True, "deleted_count": len(to_delete), "file_modified": schematic_path, "backup_path": schematic_path + ".bak"}
        if not_found:
            result["not_found"] = not_found
        return result

    @mcp.tool()
    async def add_junction_to_schematic(
        schematic_path: str,
        x: float,
        y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a junction dot to a KiCad schematic at a given coordinate.

        A junction is required wherever a wire endpoint or pin lies on the
        interior of another wire (T-junction), to mark the electrical
        connection explicitly.

        If the requested point lies on the **interior** of an existing wire
        segment, that wire is automatically split into two segments meeting
        at the junction coordinate. This is required because KiCad ≥ 10
        silently deletes any junction that does not coincide with at least
        one wire endpoint when the schematic is reopened.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            x: X coordinate for the junction in mm.
            y: Y coordinate for the junction in mm.

        Returns:
            dict with keys: success (bool), junction (x, y coords).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            _add_junction_and_split(sch, x, y)

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to add junction: {exc}"}

        return {"success": True, "junction": {"x": x, "y": y}, "file_modified": schematic_path, "backup_path": schematic_path + ".bak"}

    @mcp.tool()
    async def list_junctions_in_schematic(
        schematic_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all junction dots present in a KiCad schematic.

        Returns every junction's coordinates (in mm).  Use the returned
        coordinates with delete_junction_from_schematic to remove a specific
        junction.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.

        Returns:
            dict with keys: success (bool), junctions (list of {x, y} dicts),
            count (int).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        junctions = []
        try:
            for j in sch.junction:
                coords = j.at.value
                junctions.append({"x": float(coords[0]), "y": float(coords[1])})
        except AttributeError:
            pass  # no junctions in schematic

        return {"success": True, "junctions": junctions, "count": len(junctions)}

    @mcp.tool()
    async def delete_junction_from_schematic(
        schematic_path: str,
        x: float,
        y: float,
        tolerance: float = 0.01,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete a junction dot from a KiCad schematic by its coordinates.

        Removes all junctions whose position matches (x, y) within the
        specified tolerance.  Use list_junctions_in_schematic first to obtain
        the exact coordinates.  A backup (.kicad_sch.bak) is written before
        saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            x: X coordinate of the junction in mm.
            y: Y coordinate of the junction in mm.
            tolerance: Maximum coordinate difference considered a match
                (default 0.01 mm).

        Returns:
            dict with keys: success (bool), deleted_count (int).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            to_delete = []
            try:
                for j in sch.junction:
                    coords = j.at.value
                    jx, jy = float(coords[0]), float(coords[1])
                    if abs(jx - x) <= tolerance and abs(jy - y) <= tolerance:
                        to_delete.append(j)
            except AttributeError:
                pass  # no junctions

            if not to_delete:
                return {
                    "error": (
                        f"No junction found at ({x}, {y}) within tolerance {tolerance}"
                    )
                }

            for j in to_delete:
                j.delete()

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to delete junction: {exc}"}

        return {"success": True, "deleted_count": len(to_delete), "file_modified": schematic_path, "backup_path": schematic_path + ".bak"}
