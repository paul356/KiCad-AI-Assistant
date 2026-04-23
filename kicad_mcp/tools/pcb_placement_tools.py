"""
PCB footprint placement tools for KiCad MCP server.

Provides tools to reposition, flip, and update properties of footprints
on a .kicad_pcb board.  All mutation tools create a .kicad_pcb.bak backup
before writing.
"""
import logging
from typing import Any, Dict, Optional

from fastmcp import Context, FastMCP

from kicad_mcp.utils.pcb_sexp_utils import load_pcb, save_pcb
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    set_fp_at,
    set_fp_property,
    flip_fp_layers,
)

log = logging.getLogger(__name__)


def register_pcb_placement_tools(mcp: FastMCP) -> None:
    """Register PCB footprint placement tools with the MCP server."""

    @mcp.tool()
    async def set_footprint_position(
        pcb_path: str,
        reference: str,
        x: Optional[float],
        y: Optional[float],
        rotation: Optional[float],
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Move and/or rotate a footprint on the PCB board.

        Any of x, y, rotation may be omitted (None) to leave that value
        unchanged.  At least one of them must be provided.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"U1"``.
            x: New X coordinate in mm, or None to keep current value.
            y: New Y coordinate in mm, or None to keep current value.
            rotation: New rotation in degrees (0–360), or None to keep current.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, previous position, new position, backup_path.
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

        set_fp_at(fp, new_x, new_y, new_rot)
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "reference": reference,
            "previous": {"x": old_x, "y": old_y, "rotation": old_rot},
            "new": {"x": new_x, "y": new_y, "rotation": new_rot},
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def flip_footprint(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
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
        flip_fp_layers(fp)
        new_layer = get_fp_layer(fp) or "unknown"
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
    ) -> Dict[str, Any]:
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
