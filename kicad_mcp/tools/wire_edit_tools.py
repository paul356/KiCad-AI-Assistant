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
from fastmcp import FastMCP
from fastmcp import Context

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orthogonal wire routing
# ---------------------------------------------------------------------------

def _draw_orthogonal_wire(
    sch: Any, start_x: float, start_y: float, end_x: float, end_y: float
) -> None:
    """Draw an orthogonal (horizontal-vertical-horizontal) wire path.
    
    This creates three wire segments to form a clean L-shaped or rectangular
    path from start to end point. The routing goes:
    1. Horizontal from start to (end_x, start_y)
    2. Vertical from (end_x, start_y) to end point
    
    If start and end points are already horizontally or vertically aligned,
    a single wire segment is drawn.
    
    Args:
        sch: The skip schematic object.
        start_x, start_y: Starting point coordinates.
        end_x, end_y: Ending point coordinates.
    """
    # If horizontally aligned, draw vertical wire
    if start_y == end_y:
        w = sch.wire.new()
        w.start_at([start_x, start_y])
        w.end_at([end_x, end_y])
        return
    
    # If vertically aligned, draw horizontal wire
    if start_x == end_x:
        w = sch.wire.new()
        w.start_at([start_x, start_y])
        w.end_at([end_x, end_y])
        return
    
    # Draw horizontal segment from start to middle point
    w1 = sch.wire.new()
    w1.start_at([start_x, start_y])
    w1.end_at([end_x, start_y])
    
    # Draw vertical segment from middle point to end
    w2 = sch.wire.new()
    w2.start_at([end_x, start_y])
    w2.end_at([end_x, end_y])


# ---------------------------------------------------------------------------
# Pin position resolution
# ---------------------------------------------------------------------------

def _get_pin_schematic_position(
    sch: Any, reference: str, pin_number: str
) -> tuple[float, float]:
    """Return the absolute schematic (x, y) of a named pin on a placed symbol.

    Uses skip's SymbolPin.location which accounts for the placed symbol's
    position and rotation automatically.

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
            try:
                for pin in sym.pin:
                    try:
                        if str(pin.number) == str(pin_number):
                            loc = pin.location
                            return float(loc.x), float(loc.y)
                    except AttributeError:
                        continue
            except AttributeError:
                continue
    except AttributeError:
        pass
    raise ValueError(
        f"Pin {pin_number!r} not found on symbol {reference!r}. "
        "Check that the reference designator and pin number are correct."
    )


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
        use_orthogonal_routing: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a wire segment to a KiCad schematic between two coordinates.

        Draws a wire from (start_x, start_y) to (end_x, end_y) using clean
        orthogonal (horizontal-vertical) routing by default. Optionally places 
        junction dots at either endpoint, which is required when the endpoint 
        lands in the middle of an existing wire (T-junction). A backup 
        (.kicad_sch.bak) is written before saving.

        By default, wires are drawn in an L-shape (horizontal then vertical) for
        better readability and cleaner schematics. If you prefer straight diagonal 
        wires, set use_orthogonal_routing to False.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            start_x: X coordinate of the wire start point in mm.
            start_y: Y coordinate of the wire start point in mm.
            end_x: X coordinate of the wire end point in mm.
            end_y: Y coordinate of the wire end point in mm.
            add_junction_start: Place a junction dot at the start point.
            add_junction_end: Place a junction dot at the end point.
            use_orthogonal_routing: When True (default), draws orthogonal
                (L-shaped) wires for cleaner routing. When False, draws
                single straight diagonal wires.

        Returns:
            dict with keys: success (bool), wire (start/end coords),
            junctions_added (list of coords), routing_type (str: "orthogonal" or "direct").
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
            # Use orthogonal routing by default for cleaner wires
            if use_orthogonal_routing:
                _draw_orthogonal_wire(sch, start_x, start_y, end_x, end_y)
                routing_type = "orthogonal"
            else:
                # Draw single straight wire
                w = sch.wire.new()
                w.start_at([start_x, start_y])
                w.end_at([end_x, end_y])
                routing_type = "direct"

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
            "routing_type": routing_type,
        }

    @mcp.tool()
    async def connect_pins_with_wire(
        schematic_path: str,
        from_ref: str,
        from_pin: str,
        to_ref: str,
        to_pin: str,
        add_junctions: bool = False,
        use_orthogonal_routing: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect two symbol pins with a wire, using clean orthogonal routing.

        Resolves the absolute schematic coordinates of both pins automatically
        (accounting for each symbol's placement position and rotation), then
        draws a wire between them using orthogonal (horizontal-vertical) routing
        for a cleaner, more professional appearance. Optionally places junction 
        dots at both endpoints. A backup (.kicad_sch.bak) is written before saving.

        By default, wires are drawn in an L-shape (horizontal then vertical) for
        better readability. If you prefer straight diagonal wires, set 
        use_orthogonal_routing to False.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            from_ref: Reference designator of the source symbol (e.g. "R1").
            from_pin: Pin number of the source pin (e.g. "1").
            to_ref: Reference designator of the destination symbol (e.g. "C1").
            to_pin: Pin number of the destination pin (e.g. "2").
            add_junctions: Place junction dots at both wire endpoints.
            use_orthogonal_routing: When True (default), draws orthogonal
                (L-shaped) wires for cleaner routing. When False, draws
                single straight diagonal wires.

        Returns:
            dict with keys: success (bool), wire (from/to with ref, pin, x, y),
            junctions_added (bool), routing_type (str: "orthogonal" or "direct").
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
            start_x, start_y = _get_pin_schematic_position(sch, from_ref, from_pin)
        except ValueError as exc:
            return {"error": str(exc)}

        try:
            end_x, end_y = _get_pin_schematic_position(sch, to_ref, to_pin)
        except ValueError as exc:
            return {"error": str(exc)}

        if start_x == end_x and start_y == end_y:
            return {"error": "Both pins are at the same coordinate; cannot draw a wire"}

        try:
            # Use orthogonal routing by default for cleaner wires
            if use_orthogonal_routing:
                _draw_orthogonal_wire(sch, start_x, start_y, end_x, end_y)
                routing_type = "orthogonal"
            else:
                # Draw single straight wire
                w = sch.wire.new()
                w.start_at([start_x, start_y])
                w.end_at([end_x, end_y])
                routing_type = "direct"

            if add_junctions:
                j = sch.junction.new()
                j.at.value = [start_x, start_y]
                j = sch.junction.new()
                j.at.value = [end_x, end_y]

            shutil.copy(schematic_path, schematic_path + ".bak")
            sch.write(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to add wire: {exc}"}

        return {
            "success": True,
            "wire": {
                "from": {"ref": from_ref, "pin": from_pin, "x": start_x, "y": start_y},
                "to": {"ref": to_ref, "pin": to_pin, "x": end_x, "y": end_y},
            },
            "junctions_added": add_junctions,
            "routing_type": routing_type,
        }


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
