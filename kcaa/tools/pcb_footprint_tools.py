"""
Footprint-file creation and editing tools for KiCad MCP server.

These tools operate on standalone ``.kicad_mod`` footprint files inside
``.pretty`` libraries.  They let the agent create new footprints, add pads,
graphic shapes, and text, and inspect existing footprints.

All mutations create a ``.kicad_mod.bak`` backup before writing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.footprint_mod_utils import (
    add_fp_arc,
    add_fp_circle,
    add_fp_line,
    add_fp_rect,
    add_fp_text,
    add_pad,
    create_footprint_mod,
    delete_element_from_footprint,
    get_footprint_mod_info,
    load_footprint_mod,
    save_footprint_mod,
    set_footprint_mod_attr,
    set_footprint_mod_attr_flag,
)

log = logging.getLogger(__name__)


def _ensure_pretty_dir(footprint_path: str) -> None:
    """Create the parent ``.pretty`` directory if it does not exist."""
    parent = os.path.dirname(footprint_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_or_create(
    footprint_path: str,
    create: bool,
    name: str | None = None,
    layer: str = "F.Cu",
) -> list[Any]:
    """Load an existing footprint or create a new one if *create* is True."""
    if os.path.exists(footprint_path):
        return load_footprint_mod(footprint_path)
    if not create:
        raise FileNotFoundError(f"Footprint file not found: {footprint_path}")
    fp_name = name or os.path.splitext(os.path.basename(footprint_path))[0]
    return create_footprint_mod(fp_name, layer=layer)


def register_pcb_footprint_tools(mcp: FastMCP) -> None:
    """Register footprint-file editing tools with the MCP server."""

    @mcp.tool()
    async def create_footprint(
        footprint_path: str,
        layer: str = "F.Cu",
        description: str = "",
        tags: str = "",
        attr: str = "smd",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new ``.kicad_mod`` footprint file.

        The parent directory should be a ``.pretty`` footprint library folder.
        If the directory does not exist it is created automatically.

        Args:
            footprint_path: Absolute path to the new ``.kicad_mod`` file.
            layer: Primary layer, e.g. ``"F.Cu"`` or ``"B.Cu"``.
            description: Footprint description.
            tags: Space-separated search tags.
            attr: Footprint attribute: ``"smd"``, ``"through_hole"``, or ``""``.
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path`` and ``name``.
        """
        if not footprint_path.endswith(".kicad_mod"):
            return {"error": "Path must end with .kicad_mod"}
        if os.path.exists(footprint_path):
            return {"error": f"Footprint already exists: {footprint_path}"}

        _ensure_pretty_dir(footprint_path)
        name = os.path.splitext(os.path.basename(footprint_path))[0]
        data = create_footprint_mod(name, layer, description, tags, attr)
        save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "name": name}

    @mcp.tool()
    async def add_pad_to_footprint(
        footprint_path: str,
        number: str,
        pad_type: str,
        shape: str,
        x: float,
        y: float,
        width: float,
        height: float,
        layers: list[str],
        rotation: float = 0.0,
        drill: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a pad to an existing footprint file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            number: Pad number or name.
            pad_type: ``"smd"``, ``"thru_hole"``, ``"np_thru_hole"``.
            shape: ``"rect"``, ``"circle"``, ``"oval"``, ``"roundrect"``.
            x: X position in mm.
            y: Y position in mm.
            width: Pad width in mm.
            height: Pad height in mm.
            layers: List of layer names, e.g. ``["F.Cu", "F.Paste", "F.Mask"]``.
            rotation: Pad rotation in degrees, clockwise-positive.
            drill: Drill diameter for through-hole pads (mm).
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path`` and ``backup_path``.
        """
        try:
            data = _load_or_create(footprint_path, create=False)
        except Exception as exc:
            return {"error": str(exc)}

        at: tuple[float, float, float] | tuple[float, float] = (x, y, rotation) if rotation else (x, y)
        drill_arg: float | tuple[float, float] | None = drill
        add_pad(data, number, pad_type, shape, at, (width, height), layers, drill=drill_arg)
        backup_path = save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "backup_path": backup_path}

    @mcp.tool()
    async def add_graphic_to_footprint(
        footprint_path: str,
        graphic_type: str,
        layer: str,
        width: float,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        mid_x: float | None = None,
        mid_y: float | None = None,
        center_x: float | None = None,
        center_y: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a graphic shape to a footprint file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            graphic_type: ``"line"``, ``"arc"``, ``"circle"``, or ``"rect"``.
            layer: Layer name, e.g. ``"F.SilkS"`` or ``"F.Fab"``.
            width: Stroke width in mm.
            start_x: Start X (or rectangle corner).
            start_y: Start Y.
            end_x: End X (or opposite rectangle corner / point on circumference for circle).
            end_y: End Y.
            mid_x: Arc mid-point X (required for arcs).
            mid_y: Arc mid-point Y (required for arcs).
            center_x: Circle center X (required for circles).
            center_y: Circle center Y (required for circles).
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path`` and ``backup_path``.
        """
        try:
            data = _load_or_create(footprint_path, create=False)
        except Exception as exc:
            return {"error": str(exc)}

        try:
            if graphic_type == "line":
                add_fp_line(data, (start_x, start_y), (end_x, end_y), layer, width)
            elif graphic_type == "arc":
                if mid_x is None or mid_y is None:
                    return {"error": "Arc requires mid_x and mid_y"}
                add_fp_arc(data, (start_x, start_y), (mid_x, mid_y), (end_x, end_y), layer, width)
            elif graphic_type == "circle":
                if center_x is None or center_y is None:
                    return {"error": "Circle requires center_x and center_y"}
                add_fp_circle(data, (center_x, center_y), (end_x, end_y), layer, width)
            elif graphic_type == "rect":
                add_fp_rect(data, (start_x, start_y), (end_x, end_y), layer, width)
            else:
                return {"error": f"Unknown graphic_type: {graphic_type}"}
        except Exception as exc:
            return {"error": f"Failed to add graphic: {exc}"}

        backup_path = save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "backup_path": backup_path}

    @mcp.tool()
    async def add_text_to_footprint(
        footprint_path: str,
        text_type: str,
        text: str,
        x: float,
        y: float,
        layer: str,
        rotation: float = 0.0,
        size_x: float = 1.0,
        size_y: float = 1.0,
        thickness: float = 0.15,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add text (reference, value, or user) to a footprint file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            text_type: ``"reference"``, ``"value"``, or ``"user"``.
            text: Text content.
            x: X position in mm.
            y: Y position in mm.
            layer: Layer name.
            rotation: Text rotation in degrees.
            size_x: Font width in mm.
            size_y: Font height in mm.
            thickness: Stroke thickness in mm.
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path`` and ``backup_path``.
        """
        try:
            data = _load_or_create(footprint_path, create=False)
        except Exception as exc:
            return {"error": str(exc)}

        at: tuple[float, float, float] | tuple[float, float] = (x, y, rotation) if rotation else (x, y)
        add_fp_text(data, text_type, text, at, layer, (size_x, size_y), thickness)
        backup_path = save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "backup_path": backup_path}

    @mcp.tool()
    async def set_footprint_attribute(
        footprint_path: str,
        attribute: str,
        value: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set a top-level attribute of a footprint file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            attribute: ``"layer"``, ``"descr"``, ``"tags"``, or ``"attr"``.
            value: New value. Use an empty string to remove the attribute.
                For ``"attr"`` use ``"smd"``, ``"through_hole"``, or ``""``.
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path`` and ``backup_path``.
        """
        try:
            data = _load_or_create(footprint_path, create=False)
        except Exception as exc:
            return {"error": str(exc)}

        if attribute == "attr":
            set_footprint_mod_attr_flag(data, attribute, value or None)
        else:
            set_footprint_mod_attr(data, attribute, value or None)

        backup_path = save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "backup_path": backup_path}

    @mcp.tool()
    async def delete_footprint_element(
        footprint_path: str,
        element_type: str,
        index: int,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete an element from a footprint file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            element_type: ``"pad"``, ``"fp_line"``, ``"fp_arc"``, ``"fp_circle"``,
                ``"fp_rect"``, or ``"fp_text"``.
            index: Zero-based index among elements of that type.
            ctx: MCP context (unused).

        Returns:
            dict with ``footprint_path``, ``backup_path``, and ``deleted``.
        """
        try:
            data = _load_or_create(footprint_path, create=False)
        except Exception as exc:
            return {"error": str(exc)}

        deleted = delete_element_from_footprint(data, element_type, index)
        backup_path = save_footprint_mod(footprint_path, data)
        return {"footprint_path": footprint_path, "backup_path": backup_path, "deleted": deleted}

    @mcp.tool()
    async def get_footprint_info(
        footprint_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return a summary of a ``.kicad_mod`` file.

        Args:
            footprint_path: Absolute path to the ``.kicad_mod`` file.
            ctx: MCP context (unused).

        Returns:
            dict with name, layer, description, tags, attr, and element counts.
        """
        try:
            data = load_footprint_mod(footprint_path)
        except Exception as exc:
            return {"error": str(exc)}
        return {"footprint_path": footprint_path, **get_footprint_mod_info(data)}

    @mcp.tool()
    async def list_footprints_in_pretty_library(
        library_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all footprint names in a ``.pretty`` library directory.

        Args:
            library_path: Absolute path to a ``.pretty`` directory.
            ctx: MCP context (unused).

        Returns:
            dict with ``library_path`` and ``footprints`` (list of names).
        """
        if not os.path.isdir(library_path):
            return {"error": f"Library directory not found: {library_path}"}
        names = sorted(
            f[:-len(".kicad_mod")]
            for f in os.listdir(library_path)
            if f.endswith(".kicad_mod")
        )
        return {"library_path": library_path, "footprints": names}
