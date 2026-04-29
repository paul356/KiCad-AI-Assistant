"""
PCB board editing tools for KiCad MCP server.

Provides tools to:
  - Read, draw, and clear the board outline (Edge.Cuts layer).
  - Query footprint and board bounding boxes in world coordinates.
  - Perform group footprint operations (align, distribute, move by delta).

PCB coordinate convention (all tools here):
  millimetres, +X right, **+Y down**, rotation **clockwise-positive**
  (KiCad PCB convention).

All mutation tools create a ``.kicad_pcb.bak`` backup before writing.
"""
import logging
from typing import Any, Dict, List, Optional

from fastmcp import Context, FastMCP

from kicad_mcp.utils.pcb_sexp_utils import load_pcb, save_pcb
from kicad_mcp.utils.pcb_footprint_utils import find_footprint, get_fp_at, set_fp_at
from kicad_mcp.utils.pcb_board_utils import (
    get_edge_cuts_items,
    remove_edge_cuts_items,
    add_gr_line,
    add_gr_rect,
    add_gr_arc,
    get_fp_courtyard_bbox,
)

log = logging.getLogger(__name__)


def register_pcb_edit_tools(mcp: FastMCP) -> None:
    """Register PCB board-editing tools with the MCP server."""

    # ------------------------------------------------------------------
    # Board outline — query
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_board_outline(
        pcb_path: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
        add_gr_arc(data, cx, cy, radius, start_angle_deg, end_angle_deg, width=width, layer="Edge.Cuts")
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}
        return {
            "added": {
                "type": "gr_arc", "cx": cx, "cy": cy, "radius": radius,
                "start_angle_deg": start_angle_deg, "end_angle_deg": end_angle_deg,
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
    ) -> Dict[str, Any]:
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
            add_gr_line(data, x + r, y, x2 - r, y, width=line_width)       # top
            add_gr_line(data, x2, y + r, x2, y2 - r, width=line_width)     # right
            add_gr_line(data, x2 - r, y2, x + r, y2, width=line_width)     # bottom
            add_gr_line(data, x, y2 - r, x, y + r, width=line_width)       # left
            # Four corner arcs (CW angles, +Y down)
            add_gr_arc(data, x2 - r, y + r, r, 270, 360, width=line_width)  # top-right
            add_gr_arc(data, x2 - r, y2 - r, r, 0, 90, width=line_width)   # bottom-right
            add_gr_arc(data, x + r, y2 - r, r, 90, 180, width=line_width)  # bottom-left
            add_gr_arc(data, x + r, y + r, r, 180, 270, width=line_width)  # top-left
            items_added = 8

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "board_rect": {"x": x, "y": y, "width": width, "height": height,
                           "x2": x2, "y2": y2, "corner_radius": corner_radius},
            "items_added": items_added,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    # ------------------------------------------------------------------
    # Footprint bounding box
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_footprint_bbox(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Return the world-coordinate bounding box of a footprint's courtyard.

        The bounding box is computed from ``F.Courtyard`` / ``B.Courtyard``
        graphic items in the footprint, transformed to board world
        coordinates by applying the footprint's position and rotation
        (clockwise-positive).  If the footprint has no courtyard items the
        tool falls back to all ``fp_line``/``fp_rect``/``fp_circle`` items.

        Use this to check for footprint overlaps before placement or to
        size the board outline around all components.

        PCB coordinates: mm, +X right, **+Y down**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"U1"``.
            ctx: MCP context (unused).

        Returns:
            dict with reference, x/y/rotation (anchor), bbox
            {min_x, min_y, max_x, max_y, width, height} in world mm,
            or ``error`` if not found / no geometry.
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        fp_x, fp_y, fp_rot = get_fp_at(fp)
        bbox = get_fp_courtyard_bbox(fp, fp_x, fp_y, fp_rot)
        if bbox is None:
            return {"error": f"No courtyard or graphic geometry found for '{reference}'."}

        return {
            "reference": reference,
            "x": fp_x,
            "y": fp_y,
            "rotation": fp_rot,
            "bbox": bbox,
        }

    # ------------------------------------------------------------------
    # Board bounding box (union of all footprint courtyards)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_board_bounding_box(
        pcb_path: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Return the union bounding box of all footprint courtyards on the board.

        Useful for determining the minimum board size needed to contain all
        placed footprints, and for checking whether all footprints fit
        within the current board outline.

        PCB coordinates: mm, +X right, **+Y down**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).

        Returns:
            dict with bbox {min_x, min_y, max_x, max_y, width, height}
            in world mm covering all footprints, footprint_count,
            footprints_without_courtyard (list of references that had to
            fall back to raw graphics or were skipped).
        """
        import sexpdata as _sx

        def _sym_local(v: Any) -> str:
            return str(v) if isinstance(v, _sx.Symbol) else str(v)

        data = load_pcb(pcb_path)
        all_min_x: List[float] = []
        all_min_y: List[float] = []
        all_max_x: List[float] = []
        all_max_y: List[float] = []
        fp_count = 0
        no_courtyard: List[str] = []

        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            if _sym_local(item[0]) != "footprint":
                continue
            fp_count += 1
            # Get reference
            ref = ""
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3 and _sym_local(sub[0]) == "property":
                    if (sub[1] if isinstance(sub[1], str) else _sym_local(sub[1])) == "Reference":
                        ref = sub[2] if isinstance(sub[2], str) else _sym_local(sub[2])
            # Get position
            fp_x, fp_y, fp_rot = 0.0, 0.0, 0.0
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3 and _sym_local(sub[0]) == "at":
                    fp_x, fp_y = float(sub[1]), float(sub[2])
                    fp_rot = float(sub[3]) if len(sub) > 3 else 0.0

            bbox = get_fp_courtyard_bbox(item, fp_x, fp_y, fp_rot)
            if bbox is None:
                no_courtyard.append(ref)
                continue
            all_min_x.append(bbox["min_x"])
            all_min_y.append(bbox["min_y"])
            all_max_x.append(bbox["max_x"])
            all_max_y.append(bbox["max_y"])

        if not all_min_x:
            return {
                "error": "No footprint geometry found.",
                "footprint_count": fp_count,
                "footprints_without_courtyard": no_courtyard,
            }

        min_x = min(all_min_x)
        min_y = min(all_min_y)
        max_x = max(all_max_x)
        max_y = max(all_max_y)

        return {
            "bbox": {
                "min_x": round(min_x, 4),
                "min_y": round(min_y, 4),
                "max_x": round(max_x, 4),
                "max_y": round(max_y, 4),
                "width": round(max_x - min_x, 4),
                "height": round(max_y - min_y, 4),
            },
            "footprint_count": fp_count,
            "footprints_without_courtyard": no_courtyard,
        }

    # ------------------------------------------------------------------
    # Group operations — align
    # ------------------------------------------------------------------

    @mcp.tool()
    async def align_footprints(
        pcb_path: str,
        references: List[str],
        axis: str,
        coordinate: Optional[float],
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Align a list of footprints to the same X or Y coordinate.

        Sets all listed footprints to the same ``x`` (if ``axis="x"``) or
        the same ``y`` (if ``axis="y"``).  The target coordinate may be
        specified explicitly, or omitted (``None``) to use the mean of the
        current positions.

        PCB coordinates: mm, +X right, **+Y down**.
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            references: List of reference designators to align,
                e.g. ``["C1", "C2", "C3"]``.
            axis: ``"x"`` to align horizontally (same X) or ``"y"`` to
                align vertically (same Y).
            coordinate: Target coordinate in mm.  Pass ``null`` to use the
                mean of the current footprint positions along the chosen axis.
            ctx: MCP context (unused).

        Returns:
            dict with aligned (list of {reference, old_x, old_y, new_x,
            new_y}), target_coordinate, backup_path, pcb_path, and any
            not_found references.
        """
        if axis not in ("x", "y"):
            return {"error": "axis must be 'x' or 'y'."}
        if not references:
            return {"error": "references list must not be empty."}

        data = load_pcb(pcb_path)

        fps = {}
        not_found = []
        for ref in references:
            try:
                fps[ref] = find_footprint(data, ref)
            except KeyError:
                not_found.append(ref)

        if not fps:
            return {"error": "None of the specified footprints were found.", "not_found": not_found}

        positions = {ref: get_fp_at(fp) for ref, fp in fps.items()}

        if coordinate is None:
            if axis == "x":
                target = sum(p[0] for p in positions.values()) / len(positions)
            else:
                target = sum(p[1] for p in positions.values()) / len(positions)
        else:
            target = float(coordinate)

        aligned = []
        for ref, fp in fps.items():
            ox, oy, rot = positions[ref]
            nx = target if axis == "x" else ox
            ny = target if axis == "y" else oy
            set_fp_at(fp, nx, ny, rot)
            aligned.append({"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny})

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "aligned": aligned,
            "target_coordinate": round(target, 4),
            "axis": axis,
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    # ------------------------------------------------------------------
    # Group operations — distribute
    # ------------------------------------------------------------------

    @mcp.tool()
    async def distribute_footprints(
        pcb_path: str,
        references: List[str],
        axis: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Evenly space footprints along the X or Y axis.

        Keeps the two outermost footprint positions fixed and redistributes
        the intermediate ones at equal intervals.  At least three
        footprints are needed; two footprints are returned unchanged.

        Footprints are sorted by their current position along the chosen
        axis before spacing.

        PCB coordinates: mm, +X right, **+Y down**.
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            references: List of reference designators, e.g.
                ``["C1", "C2", "C3", "C4"]``.
            axis: ``"x"`` to distribute horizontally or ``"y"`` to
                distribute vertically.
            ctx: MCP context (unused).

        Returns:
            dict with distributed (list of {reference, old_x, old_y,
            new_x, new_y}), spacing_mm, backup_path, pcb_path, not_found.
        """
        if axis not in ("x", "y"):
            return {"error": "axis must be 'x' or 'y'."}
        if len(references) < 2:
            return {"error": "At least 2 references are required."}

        data = load_pcb(pcb_path)

        fps = {}
        not_found = []
        for ref in references:
            try:
                fps[ref] = find_footprint(data, ref)
            except KeyError:
                not_found.append(ref)

        if len(fps) < 2:
            return {"error": "Fewer than 2 footprints found.", "not_found": not_found}

        positions = {ref: get_fp_at(fp) for ref, fp in fps.items()}

        # Sort by position along chosen axis
        key_idx = 0 if axis == "x" else 1
        sorted_refs = sorted(fps.keys(), key=lambda r: positions[r][key_idx])

        first_pos = positions[sorted_refs[0]][key_idx]
        last_pos = positions[sorted_refs[-1]][key_idx]
        n = len(sorted_refs)
        spacing = (last_pos - first_pos) / (n - 1) if n > 1 else 0.0

        distributed = []
        for i, ref in enumerate(sorted_refs):
            ox, oy, rot = positions[ref]
            target_coord = first_pos + i * spacing
            nx = target_coord if axis == "x" else ox
            ny = target_coord if axis == "y" else oy
            set_fp_at(fps[ref], nx, ny, rot)
            distributed.append({"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny})

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "distributed": distributed,
            "axis": axis,
            "spacing_mm": round(spacing, 4),
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    # ------------------------------------------------------------------
    # Group operations — move by delta
    # ------------------------------------------------------------------

    @mcp.tool()
    async def move_footprints_by_delta(
        pcb_path: str,
        references: List[str],
        dx: float,
        dy: float,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Move a group of footprints by the same (dx, dy) offset.

        Useful for shifting a functional block after the board outline
        changes, without altering the relative positions of components
        within the group.

        PCB coordinates: mm, +X right, **+Y down** (so positive ``dy``
        moves footprints downward on screen).
        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            references: List of reference designators to move, e.g.
                ``["U1", "C1", "C2", "R1"]``.
            dx: X offset in mm (positive → right).
            dy: Y offset in mm (positive → down on screen).
            ctx: MCP context (unused).

        Returns:
            dict with moved (list of {reference, old_x, old_y, new_x,
            new_y}), dx, dy, backup_path, pcb_path, not_found.
        """
        if dx == 0 and dy == 0:
            return {"error": "dx and dy are both zero — nothing to do."}
        if not references:
            return {"error": "references list must not be empty."}

        data = load_pcb(pcb_path)

        fps = {}
        not_found = []
        for ref in references:
            try:
                fps[ref] = find_footprint(data, ref)
            except KeyError:
                not_found.append(ref)

        if not fps:
            return {"error": "None of the specified footprints were found.", "not_found": not_found}

        moved = []
        for ref, fp in fps.items():
            ox, oy, rot = get_fp_at(fp)
            nx, ny = ox + dx, oy + dy
            set_fp_at(fp, nx, ny, rot)
            moved.append({"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny})

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "moved": moved,
            "dx": dx,
            "dy": dy,
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }
