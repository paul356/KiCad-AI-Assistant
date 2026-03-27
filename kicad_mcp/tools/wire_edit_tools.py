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


# ---------------------------------------------------------------------------
# Smart wire router
# ---------------------------------------------------------------------------

def _draw_smart_wire(
    sch: Any,
    sx: float, sy: float,
    ex: float, ey: float,
    start_angle: float | None = None,
    end_angle: float | None = None,
    obstacle_pins: list[tuple[float, float]] | None = None,
    lead_out_dist: float = _LEAD_OUT_DIST,
) -> bool:
    """Draw a smart wire from (sx,sy) to (ex,ey) avoiding component pins.

    Algorithm:
      1. Compute a lead-out stub from each endpoint following the pin's exit
         direction (if angle is supplied), moving the route into open space.
      2. Try up to 16 candidate inner routes between the lead-out endpoints.
      3. Pick the first candidate where no obstacle pin falls on the interior
         of any segment.
      4. Fall back to L-A (horizontal-first) with a logged warning when all
         candidates collide.

    Args:
        sch: The skip schematic object.
        sx, sy: Start point (pin position).
        ex, ey: End point (pin position).
        start_angle: Absolute exit angle of the start pin in degrees
            (0=right, 90=down, 180=left, 270=up).  None to skip lead-out.
        end_angle: Absolute exit angle of the end pin in degrees.
        obstacle_pins: List of (x, y) positions to avoid.
        lead_out_dist: Length of each lead-out stub in mm (default 2.54 mm).

    Returns:
        True if a collision-free route was found, False if the fallback was used.
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

    # Build lead-out segments
    lead_segs: list[tuple[float, float, float, float]] = []
    if start_angle is not None and (abs(p1x - sx) > 1e-9 or abs(p1y - sy) > 1e-9):
        lead_segs.append((sx, sy, p1x, p1y))
    if end_angle is not None and (abs(p2x - ex) > 1e-9 or abs(p2y - ey) > 1e-9):
        lead_segs.append((ex, ey, p2x, p2y))

    # Try each inner route candidate
    inner_candidates = _route_candidates(p1x, p1y, p2x, p2y)
    chosen: list[tuple[float, float, float, float]] | None = None
    for candidate in inner_candidates:
        if not _route_collides(lead_segs + candidate, obstacles, _PIN_COLLISION_TOL):
            chosen = candidate
            break

    collision_free = True
    if chosen is None:
        log.warning(
            "smart_wire: all %d route candidates collide with obstacle pins "
            "between (%.3f,%.3f) and (%.3f,%.3f); using L-A fallback.",
            len(inner_candidates), sx, sy, ex, ey,
        )
        # Fallback: use first (L-A or direct) candidate
        chosen = inner_candidates[0] if inner_candidates else [(p1x, p1y, p2x, p2y)]
        collision_free = False

    # Draw all segments, merging collinear neighbours first
    all_draw = _merge_collinear_segments(lead_segs + chosen)
    for (ax, ay, bx, by) in all_draw:
        if abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9:
            continue  # skip zero-length
        w = sch.wire.new()
        w.start_at([ax, ay])
        w.end_at([bx, by])

    return collision_free


# ---------------------------------------------------------------------------
# Orthogonal wire routing (thin wrapper — backward compatibility)
# ---------------------------------------------------------------------------

def _draw_orthogonal_wire(
    sch: Any,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    obstacle_pins: list[tuple[float, float]] | None = None,
) -> bool:
    """Draw an orthogonal wire, avoiding obstacle pins when supplied.

    Thin wrapper around :func:`_draw_smart_wire` with no pin-direction
    lead-outs.  Returns True when a collision-free route was found.
    """
    return _draw_smart_wire(
        sch, start_x, start_y, end_x, end_y,
        obstacle_pins=obstacle_pins,
    )


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


def _collect_all_pin_positions(
    sch: Any,
    exclude_pins: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Return the absolute schematic position of every pin of every placed symbol.

    Uses :func:`~kicad_mcp.utils.skip_helpers.sym_pin_world_coords` which
    handles the skip library bug for power symbols (VCC, GND, PWR_FLAG) and
    single-pin symbols (TestPoint).

    Args:
        sch: The skip schematic object.
        exclude_pins: Optional list of (x, y) positions to omit (e.g. the two
            endpoint pins that are intentionally being connected).

    Returns:
        List of (x, y) tuples, one per pin, with excluded pins removed.
    """
    exclusions: set[tuple[float, float]] = set()
    if exclude_pins:
        for px, py in exclude_pins:
            exclusions.add((round(px, 4), round(py, 4)))

    positions: list[tuple[float, float]] = []
    try:
        for sym in sch.symbol:
            for pin in sym_pin_world_coords(sym):
                pt = (pin.x, pin.y)
                if pt not in exclusions:
                    positions.append(pt)
    except AttributeError:
        pass
    return positions


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_wire_edit_tools(mcp: FastMCP) -> None:
    """Register all wire and junction editing tools with the MCP server."""

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
        """Add a wire segment to a KiCad schematic between two coordinates.

        Draws a wire from (start_x, start_y) to (end_x, end_y) using
        orthogonal (horizontal-vertical) routing. Optionally places junction
        dots at either endpoint, which is required when the endpoint lands in
        the middle of an existing wire (T-junction). A backup
        (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start point in mm.
            start_y: Y coordinate of the wire start point in mm.
            end_x: X coordinate of the wire end point in mm.
            end_y: Y coordinate of the wire end point in mm.
            add_junction_start: Place a junction dot at the start point.
            add_junction_end: Place a junction dot at the end point.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of coords).
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
            # Collect all pin positions to avoid routing wires through them
            obstacles = _collect_all_pin_positions(sch)
            _draw_orthogonal_wire(sch, start_x, start_y, end_x, end_y,
                                  obstacle_pins=obstacles)

            junctions_added = []
            if add_junction_start:
                j = sch.junction.new()
                j.at.value = [start_x, start_y]
                junctions_added.append({"x": start_x, "y": start_y})
            if add_junction_end:
                j = sch.junction.new()
                j.at.value = [end_x, end_y]
                junctions_added.append({"x": end_x, "y": end_y})

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
        pin exit directions and avoids other component pins. A backup
        (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            from_ref: Reference designator of the source symbol (e.g. "R1").
            from_pin: Pin number of the source pin (e.g. "1").
            to_ref: Reference designator of the destination symbol (e.g. "C1").
            to_pin: Pin number of the destination pin (e.g. "2").

        Returns:
            dict with keys: success (bool), wire (from/to with ref, pin, x, y),
            collision_free (bool).
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
            # Collect obstacle pins excluding the two endpoints being connected
            obstacles = _collect_all_pin_positions(
                sch, exclude_pins=[(start_x, start_y), (end_x, end_y)]
            )

            collision_free: bool | None = None
            # Smart routing: follow pin exit directions, avoid all other pins
            collision_free = _draw_smart_wire(
                sch, start_x, start_y, end_x, end_y,
                start_angle=start_angle,
                end_angle=end_angle,
                obstacle_pins=obstacles,
            )

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
        }
        if collision_free is not None:
            result["collision_free"] = collision_free
        return result


    @mcp.tool()
    async def delete_wire_from_schematic(
        schematic_path: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        tolerance: float = 0.01,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete a wire segment from a KiCad schematic by its endpoints.

        Removes all wire segments whose start/end coordinates match
        (start_x, start_y) → (end_x, end_y) or the reverse direction,
        within the specified tolerance.  Use
        analyze_schematic_connections(include_wire_topology=True) first to
        obtain exact wire coordinates (connected wires appear under each net's
        ``wires`` list; unconnected stubs appear under ``unconnected_wires``).
        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start point in mm.
            start_y: Y coordinate of the wire start point in mm.
            end_x: X coordinate of the wire end point in mm.
            end_y: Y coordinate of the wire end point in mm.
            tolerance: Maximum coordinate difference considered a match
                (default 0.01 mm).

        Returns:
            dict with keys: success (bool), deleted_count (int).
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

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            to_delete = []
            try:
                for w in sch.wire:
                    sx = float(w.start.value[0])
                    sy = float(w.start.value[1])
                    ex = float(w.end.value[0])
                    ey = float(w.end.value[1])
                    forward = (
                        abs(sx - start_x) <= tolerance and abs(sy - start_y) <= tolerance
                        and abs(ex - end_x) <= tolerance and abs(ey - end_y) <= tolerance
                    )
                    backward = (
                        abs(sx - end_x) <= tolerance and abs(sy - end_y) <= tolerance
                        and abs(ex - start_x) <= tolerance and abs(ey - start_y) <= tolerance
                    )
                    if forward or backward:
                        to_delete.append(w)
            except AttributeError:
                pass  # no wires

            if not to_delete:
                return {
                    "error": (
                        f"No wire found matching ({start_x}, {start_y}) → "
                        f"({end_x}, {end_y}) within tolerance {tolerance}"
                    )
                }

            for w in to_delete:
                w.delete()

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to delete wire: {exc}"}

        return {"success": True, "deleted_count": len(to_delete)}

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
        connection explicitly.  A backup (.kicad_sch.bak) is written before
        saving.

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
            j = sch.junction.new()
            j.at.value = [x, y]

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to add junction: {exc}"}

        return {"success": True, "junction": {"x": x, "y": y}}

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

        return {"success": True, "deleted_count": len(to_delete)}
