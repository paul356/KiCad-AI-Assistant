"""
PCB footprint placement tools for KiCad MCP server.

Provides tools to reposition, flip, and update properties of footprints
on a .kicad_pcb board.  All mutation tools create a .kicad_pcb.bak backup
before writing.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from kicad_mcp.tools.pcb_placement_helpers import find_collisions
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    flip_fp_layers,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    set_fp_at,
    set_fp_property,
)
from kicad_mcp.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)


def register_pcb_placement_tools(mcp: FastMCP) -> None:
    """Register PCB footprint placement tools with the MCP server."""

    @mcp.tool()
    async def set_footprint_position(
        pcb_path: str,
        reference: str,
        x: float | None,
        y: float | None,
        rotation: float | None,
        ctx: Context | None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Move and/or rotate a footprint on the PCB board.

        PCB coordinates are mm with +X right, **+Y down**, and rotation
        is in degrees, **clockwise-positive** (KiCad PCB convention —
        opposite to the math/Y-up CCW convention used by .kicad_sym
        library data). This tool does NOT auto-snap; pass coordinates
        already aligned to your board grid (typical SMD work uses
        0.1 mm or 0.05 mm; through-hole often 1.27 mm / 50 mil).

        Any of x, y, rotation may be omitted (None) to leave that value
        unchanged.  At least one of them must be provided.

        By default (``force=False``) the tool refuses to place a
        footprint if its courtyard would overlap any other footprint's
        courtyard and returns an error without modifying the file.
        **Do NOT set ``force=True`` as a routine workaround.** Only use
        it when overlap is genuinely intentional and unavoidable (e.g.
        edge connectors flush with the board edge, press-fit connectors,
        or fiducials deliberately placed near other features). In all
        other cases, call ``find_free_pcb_area`` first to obtain a
        collision-free position.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"U1"``.
            x: New X coordinate in mm (world), or None to keep current.
            y: New Y coordinate in mm (world), or None to keep current.
            rotation: New rotation in degrees clockwise-positive (any
                value; KiCad normalises). None to keep current.
            force: Override the courtyard collision guard.  **Default
                False — only set True when overlap is genuinely
                intentional** (e.g. edge connectors, fiducials).  A
                warning is added to the result when overlaps are
                detected and force is True.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, previous {x, y, rotation}, new
            {x, y, rotation}, backup_path, pcb_path, and an optional
            ``warnings`` key when ``force=True`` and overlaps exist.
        """
        if x is None and y is None and rotation is None:
            return {"error": "At least one of x, y, rotation must be provided."}

        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        old_x, old_y, old_rot = get_fp_at(fp)
        new_x = old_x if x is None else float(x)
        new_y = old_y if y is None else float(y)
        new_rot = old_rot if rotation is None else float(rotation)

        # Collision check (footprint vs footprint only; board bounds not enforced)
        collisions = find_collisions(data, [(reference, new_x, new_y, new_rot)])
        if collisions and not force:
            overlapping = collisions[0]["overlapping_with"]
            return {
                "error": "Placement rejected: courtyard would overlap at the proposed position. Footprint was NOT moved.",
                "overlapping_with": overlapping,
                "proposed_position": {"x": new_x, "y": new_y, "rotation": new_rot},
                "hint": "You may need to move the interfering component to another position or call find_free_pcb_area to obtain a free spot.",
            }

        set_fp_at(fp, new_x, new_y, new_rot)
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        result: dict[str, Any] = {
            "reference": reference,
            "previous": {"x": old_x, "y": old_y, "rotation": old_rot},
            "new": {"x": new_x, "y": new_y, "rotation": new_rot},
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }
        if collisions and force:
            result["warnings"] = {
                "overlapping_with": collisions[0]["overlapping_with"],
                "message": "Placed with force=True; courtyard overlaps detected.",
            }
        return result

    @mcp.tool()
    async def flip_footprint(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Flip a footprint from the front copper layer to the back, or vice-versa.

        Toggles the primary layer (F.Cu ↔ B.Cu) and flips all child element
        layers (silkscreen, courtyard, fab, mask, paste) accordingly.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"U1"``.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, previous_layer, new_layer, backup_path.
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        old_layer = get_fp_layer(fp) or "unknown"
        fp_x, fp_y, fp_rot = get_fp_at(fp)
        flip_fp_layers(fp)
        new_layer = get_fp_layer(fp) or "unknown"

        # Collision check: compare against footprints on the destination layer only
        collisions = find_collisions(
            data,
            [(reference, fp_x, fp_y, fp_rot)],
            layer=new_layer,
        )
        if collisions:
            overlapping = collisions[0]["overlapping_with"]
            return {
                "error": (
                    f"Collision detected: flipping '{reference}' to {new_layer} would "
                    "overlap existing footprint(s) on that layer."
                ),
                "overlapping_with": overlapping,
            }

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "reference": reference,
            "previous_layer": old_layer,
            "new_layer": new_layer,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def set_footprint_property(
        pcb_path: str,
        reference: str,
        property_name: str,
        value: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Update a property of a footprint on the PCB board.

        Common property names: ``Reference``, ``Value``, ``Datasheet``,
        ``Description``.  Custom user fields are also supported.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"R1"``.
            property_name: Property name to update, e.g. ``"Value"``.
            value: New property value.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, property_name, previous_value, new_value,
            backup_path.
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        old_value = get_fp_property(fp, property_name)
        if old_value is None:
            return {
                "error": (
                    f"Property '{property_name}' not found on footprint '{reference}'. "
                    f"Use get_footprint to see available properties."
                )
            }

        updated = set_fp_property(fp, property_name, value)
        if not updated:
            return {"error": f"Failed to update property '{property_name}' on '{reference}'."}

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "reference": reference,
            "property_name": property_name,
            "previous_value": old_value,
            "new_value": value,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }
