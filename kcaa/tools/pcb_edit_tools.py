"""
PCB board editing tools for KiCad MCP server.

Provides tools to:
  - Read, draw, and clear the board outline (Edge.Cuts layer).
  - Edit non-placement PCB data such as footprint properties.

PCB coordinate convention (all tools here):
  millimetres, +X right, **+Y down**, rotation **clockwise-positive**
  (KiCad PCB convention).

All mutation tools create a ``.kicad_pcb.bak`` backup before writing.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.pcb_board_utils import (
    add_gr_arc,
    add_gr_line,
    add_gr_rect,
    get_edge_cuts_items,
    remove_edge_cuts_items,
)
from kcaa.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_property,
    set_fp_property,
)
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)

# The TOOL_ACTION name used to synchronise the board from the schematic.
# Verified by live testing against KiCad 10: this action opens the
# "Update PCB from Schematic" dialog.  The action is registered under the
# common tool namespace, not eeschema, so it works regardless of which
# editor window currently has focus.
_ACTION_UPDATE_PCB = "common.Control.updatePcbFromSchematic"


def register_pcb_edit_tools(mcp: FastMCP) -> None:
    """Register PCB board-editing tools with the MCP server."""

    # ------------------------------------------------------------------
    # Board outline — query
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_board_outline(
        pcb_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Return all graphic items on the Edge.Cuts (board outline) layer.

        PCB coordinates: mm, +X right, **+Y down**, rotation
        **clockwise-positive**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused, for interface consistency).

        Returns:
            dict with:
                items: list of graphic-item dicts.  Each has ``type`` and
                    ``layer`` plus type-specific keys:

                    - ``gr_line``: ``x1``, ``y1``, ``x2``, ``y2``, ``width``
                    - ``gr_rect``: ``x1``, ``y1``, ``x2``, ``y2``, ``width``
                    - ``gr_arc``: ``start_x``, ``start_y``, ``mid_x``,
                      ``mid_y``, ``end_x``, ``end_y``, ``width``
                    - ``gr_circle``: ``cx``, ``cy``, ``ex``, ``ey``,
                      ``width`` (``ex``/``ey`` is a point on the circumference)

                count: total number of Edge.Cuts items.
        """
        data = load_pcb(pcb_path)
        items = get_edge_cuts_items(data)
        return {"items": items, "count": len(items)}

    # ------------------------------------------------------------------
    # Board outline — clear
    # ------------------------------------------------------------------

    @mcp.tool()
    async def clear_board_outline(
        pcb_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Remove all graphic items on the Edge.Cuts (board outline) layer.

        Use this before re-drawing the outline with
        ``add_board_outline_segment`` / ``set_board_outline_rect``.
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).

        Returns:
            dict with removed_count, backup_path, pcb_path.
        """
        data = load_pcb(pcb_path)
        removed = remove_edge_cuts_items(data)
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}
        return {"removed_count": removed, "backup_path": backup_path, "pcb_path": pcb_path}

    # ------------------------------------------------------------------
    # Board outline — add segment
    # ------------------------------------------------------------------

    @mcp.tool()
    async def add_board_outline_segment(
        pcb_path: str,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        width: float,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Add a straight line segment to the board outline (Edge.Cuts layer).

        PCB coordinates: mm, +X right, **+Y down**.  Build a closed
        rectangular outline by calling this four times (one per side), or
        use ``set_board_outline_rect`` as a convenience shortcut.
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            x1: Start X in mm.
            y1: Start Y in mm (+Y down).
            x2: End X in mm.
            y2: End Y in mm (+Y down).
            width: Line width in mm (KiCad default for Edge.Cuts is 0.05 mm).
            ctx: MCP context (unused).

        Returns:
            dict with added segment info, backup_path, pcb_path.
        """
        data = load_pcb(pcb_path)
        add_gr_line(data, x1, y1, x2, y2, width=width, layer="Edge.Cuts")
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}
        return {
            "added": {"type": "gr_line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": width},
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    # ------------------------------------------------------------------
    # Board outline — add arc
    # ------------------------------------------------------------------

    @mcp.tool()
    async def add_board_outline_arc(
        pcb_path: str,
        cx: float,
        cy: float,
        radius: float,
        start_angle_deg: float,
        end_angle_deg: float,
        width: float,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Add an arc to the board outline (Edge.Cuts layer).

        Angles use KiCad PCB convention: 0° is the +X direction, angles
        increase **clockwise** (because +Y is down).  To draw a 90° rounded
        corner at the top-left of a rectangular board — centre at
        ``(corner_r, corner_r)``, radius ``corner_r``, from 180° to 270°.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            cx: Arc centre X in mm.
            cy: Arc centre Y in mm (+Y down).
            radius: Arc radius in mm.
            start_angle_deg: Start angle in degrees, clockwise from +X.
            end_angle_deg: End angle in degrees, clockwise from +X.
            width: Line width in mm (default Edge.Cuts is 0.05 mm).
            ctx: MCP context (unused).

        Returns:
            dict with added arc parameters, backup_path, pcb_path.
        """
        data = load_pcb(pcb_path)
        add_gr_arc(
            data, cx, cy, radius, start_angle_deg, end_angle_deg, width=width, layer="Edge.Cuts"
        )
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}
        return {
            "added": {
                "type": "gr_arc",
                "cx": cx,
                "cy": cy,
                "radius": radius,
                "start_angle_deg": start_angle_deg,
                "end_angle_deg": end_angle_deg,
                "width": width,
            },
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    # ------------------------------------------------------------------
    # Board outline — set rectangular board
    # ------------------------------------------------------------------

    @mcp.tool()
    async def set_board_outline_rect(
        pcb_path: str,
        x: float,
        y: float,
        width: float,
        height: float,
        line_width: float,
        corner_radius: float,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Set a rectangular board outline, replacing the current Edge.Cuts.

        Removes all existing Edge.Cuts items, then draws the rectangle.
        When ``corner_radius > 0`` the four corners are replaced with 90°
        arcs (rounded rectangle outline using four lines + four arcs).
        When ``corner_radius == 0`` a single ``gr_rect`` is used.

        PCB coordinates: mm, +X right, **+Y down**.
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            x: Left edge X of the board in mm.
            y: Top edge Y of the board in mm (+Y down, so this is the
               *smaller* Y value).
            width: Board width in mm (extends right from x).
            height: Board height in mm (extends down from y).
            line_width: Edge.Cuts line width in mm (typically 0.05).
            corner_radius: Radius of rounded corners in mm.  0 for sharp
                corners (emitted as a single gr_rect).
            ctx: MCP context (unused).

        Returns:
            dict with board rect parameters, items_added, backup_path, pcb_path.
        """
        if width <= 0 or height <= 0:
            return {"error": "width and height must be positive."}
        if corner_radius < 0:
            return {"error": "corner_radius must be >= 0."}
        if corner_radius * 2 > min(width, height):
            return {"error": "corner_radius is too large for the given width/height."}

        data = load_pcb(pcb_path)
        remove_edge_cuts_items(data)

        x2 = x + width
        y2 = y + height
        items_added = 0

        if corner_radius == 0:
            add_gr_rect(data, x, y, x2, y2, width=line_width, layer="Edge.Cuts")
            items_added = 1
        else:
            r = corner_radius
            # Four straight edges (inset by corner_radius)
            add_gr_line(data, x + r, y, x2 - r, y, width=line_width)  # top
            add_gr_line(data, x2, y + r, x2, y2 - r, width=line_width)  # right
            add_gr_line(data, x2 - r, y2, x + r, y2, width=line_width)  # bottom
            add_gr_line(data, x, y2 - r, x, y + r, width=line_width)  # left
            # Four corner arcs (CW angles, +Y down)
            add_gr_arc(data, x2 - r, y + r, r, 270, 360, width=line_width)  # top-right
            add_gr_arc(data, x2 - r, y2 - r, r, 0, 90, width=line_width)  # bottom-right
            add_gr_arc(data, x + r, y2 - r, r, 90, 180, width=line_width)  # bottom-left
            add_gr_arc(data, x + r, y + r, r, 180, 270, width=line_width)  # top-left
            items_added = 8

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "board_rect": {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "x2": x2,
                "y2": y2,
                "corner_radius": corner_radius,
            },
            "items_added": items_added,
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
    ) -> dict[str, str]:
        """Update a property of a footprint on the PCB board.

        Common property names: ``Reference``, ``Value``, ``Datasheet``,
        ``Description``.  Custom user fields are also supported.

        A .kicad_pcb.bak backup is created before writing.
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

    # ------------------------------------------------------------------
    # update_pcb_from_schematic
    # ------------------------------------------------------------------

    @mcp.tool()
    async def update_pcb_from_schematic(ctx: Context | None) -> dict[str, Any]:
        """Trigger "Update PCB from Schematic" in the running KiCad instance.

        Calls ``kicad.run_action("common.Control.updatePcbFromSchematic")``
        via the KiCad IPC API.  This is equivalent to using
        **Tools → Update PCB from Schematic** inside KiCad, opening the
        interactive update dialog in the KiCad GUI.

        Because this action opens a modal dialog, KiCad does not send a reply
        back over the IPC socket until the user closes the dialog.  The tool
        therefore treats a connection timeout as a successful dispatch
        (the dialog is open and waiting for the user).

        Requires KiCad 9+ to be running with the IPC API enabled.

        WARNING: The underlying action name is part of an unstable KiCad
        internal API and may change in future KiCad versions.

        Returns:
            dict with:
                success (bool): True if the dialog was opened (or action dispatched).
                action (str): The action name that was called.
                status (str): Status description.
                error (str): Present only on failure.
        """
        try:
            from kcaa.tools.kipy_tools import _connect  # noqa: PLC0415
            from kipy.proto.common import commands as _commands  # noqa: PLC0415
            import kipy.errors  # noqa: PLC0415

            kicad = _connect()
            try:
                response = kicad.run_action(_ACTION_UPDATE_PCB)
                status_code = response.status
                log.info("run_action(%r) status: %r", _ACTION_UPDATE_PCB, status_code)
            except kipy.errors.ConnectionError as timeout_exc:
                # A timeout means KiCad opened a modal dialog and is waiting
                # for the user — this is the expected success path.
                if "timed out" in str(timeout_exc).lower():
                    log.info(
                        "run_action(%r) timed out — dialog is open in KiCad",
                        _ACTION_UPDATE_PCB,
                    )
                    return {
                        "success": True,
                        "action": _ACTION_UPDATE_PCB,
                        "status": "dialog_opened",
                    }
                raise

            if status_code == _commands.RAS_OK:
                return {
                    "success": True,
                    "action": _ACTION_UPDATE_PCB,
                    "status": "RAS_OK",
                }
            # Map known failure codes to human-readable messages.
            _status_messages = {
                _commands.RAS_UNKNOWN: "RAS_UNKNOWN — KiCad returned an unknown status",
                _commands.RAS_INVALID: (
                    "RAS_INVALID — action not found; "
                    "make sure a PCB or schematic is open in KiCad"
                ),
                _commands.RAS_FRAME_NOT_OPEN: (
                    "RAS_FRAME_NOT_OPEN — the required editor frame is not open; "
                    "open the PCB editor in KiCad and try again"
                ),
            }
            msg = _status_messages.get(
                status_code,
                f"Unexpected status code {status_code!r}",
            )
            return {
                "success": False,
                "action": _ACTION_UPDATE_PCB,
                "status": status_code,
                "error": msg,
            }
        except RuntimeError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log.exception("Unexpected error in update_pcb_from_schematic")
            return {"success": False, "error": str(exc)}
