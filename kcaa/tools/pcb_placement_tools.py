"""
PCB footprint placement tools for KiCad MCP server.

Provides tools to reposition, flip, align, distribute, and move footprints
on a .kicad_pcb board.  All mutation tools create a .kicad_pcb.bak backup
before writing.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.tools.pcb_placement_helpers import find_collisions, find_nearest_free_position
from kcaa.utils.pcb_footprint_utils import (
    find_footprint,
    flip_fp_layers,
    get_fp_at,
    get_fp_layer,
    set_fp_at,
)
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

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

        By default (``force=False``) the tool automatically adjusts the
        position when the requested coordinates would cause a courtyard
        overlap: it scans outward on a 1.27 mm grid (up to 20 mm radius)
        and places the footprint at the nearest collision-free spot.
        If no free spot is found within 20 mm, the footprint is **not**
        moved and an error is returned.
        **Do NOT set ``force=True`` as a routine workaround.** Only use
        it when overlap is genuinely intentional and unavoidable (e.g.
        edge connectors flush with the board edge, press-fit connectors,
        or fiducials deliberately placed near other features).

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
            dict with:

            - ``status``: ``"placed"`` on success, or
              ``"placed_at_adjusted_position"`` when the requested spot was
              occupied and the tool found the nearest free position.
            - ``reference``: the footprint reference.
            - ``moved_from``: ``{x, y, rotation}`` — position before this call.
            - ``placed_at``: ``{x, y, rotation}`` — position the footprint was
              actually placed at (may differ from the request when adjusted).
            - ``requested_position``: ``{x, y, rotation}`` — only present when
              ``status`` is ``"placed_at_adjusted_position"``; the original
              requested coords that caused a collision.
            - ``backup_path``, ``pcb_path``.
            - ``warnings``: only when ``force=True`` and overlaps exist;
              contains ``courtyard_overlaps`` (list of refs) and ``message``.
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
        req_x, req_y = new_x, new_y  # save before possible auto-adjustment

        # Collision check (footprint vs footprint only; board bounds not enforced)
        collisions = find_collisions(data, [(reference, new_x, new_y, new_rot)])
        adjusted_position: tuple[float, float] | None = None
        if collisions and not force:
            free = find_nearest_free_position(data, reference, new_x, new_y, new_rot)
            if free is None:
                overlapping = collisions[0]["overlapping_with"]
                return {
                    "error": "Placement rejected: courtyard would overlap at the proposed position. Footprint was NOT moved.",
                    "proposed_position_overlaps": overlapping,
                    "proposed_position": {"x": new_x, "y": new_y, "rotation": new_rot},
                    "current_position": {"x": old_x, "y": old_y, "rotation": old_rot},
                    "hint": "No free spot found within 20 mm. You may need to move the interfering component first.",
                }
            adjusted_position = free
            new_x, new_y = free

        set_fp_at(fp, new_x, new_y, new_rot)
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        result: dict[str, Any] = {
            "status": "placed",
            "reference": reference,
            "moved_from": {"x": old_x, "y": old_y, "rotation": old_rot},
            "placed_at": {"x": new_x, "y": new_y, "rotation": new_rot},
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }
        if adjusted_position is not None:
            result["status"] = "placed_at_adjusted_position"
            result["requested_position"] = {"x": req_x, "y": req_y, "rotation": new_rot}
        if collisions and force:
            result["warnings"] = {
                "courtyard_overlaps": collisions[0]["overlapping_with"],
                "message": "Footprint placed successfully at the new position. Courtyard overlaps detected (force=True was used).",
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
    async def align_footprints(
        pcb_path: str,
        references: list[str],
        axis: str,
        coordinate: float | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
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
        proposals = []
        for ref, fp in fps.items():
            ox, oy, rot = positions[ref]
            nx = target if axis == "x" else ox
            ny = target if axis == "y" else oy
            proposals.append((ref, nx, ny, rot))
            aligned.append({"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny})

        collisions = find_collisions(data, proposals)
        if collisions:
            details = [
                {"ref": c["ref"], "overlapping_with": c["overlapping_with"]} for c in collisions
            ]
            return {
                "error": "Collision detected: one or more footprints would overlap after alignment.",
                "collisions": details,
            }

        for ref, nx, ny, rot in proposals:
            set_fp_at(fps[ref], nx, ny, rot)

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

    @mcp.tool()
    async def distribute_footprints(
        pcb_path: str,
        references: list[str],
        axis: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Evenly space footprints along the X or Y axis.

        Keeps the two outermost footprint positions fixed and redistributes
        the intermediate ones at equal intervals.  At least three
        footprints are needed; two footprints are returned unchanged.

        Footprints are sorted by their current position along the chosen
        axis before spacing.

        PCB coordinates: mm, +X right, **+Y down**.
        A .kicad_pcb.bak backup is created before writing.
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

        key_idx = 0 if axis == "x" else 1
        sorted_refs = sorted(fps.keys(), key=lambda r: positions[r][key_idx])

        first_pos = positions[sorted_refs[0]][key_idx]
        last_pos = positions[sorted_refs[-1]][key_idx]
        n = len(sorted_refs)
        spacing = (last_pos - first_pos) / (n - 1) if n > 1 else 0.0

        distributed = []
        proposals = []
        for i, ref in enumerate(sorted_refs):
            ox, oy, rot = positions[ref]
            target_coord = first_pos + i * spacing
            nx = target_coord if axis == "x" else ox
            ny = target_coord if axis == "y" else oy
            proposals.append((ref, nx, ny, rot))
            distributed.append(
                {"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny}
            )

        collisions = find_collisions(data, proposals)
        if collisions:
            details = [
                {"ref": c["ref"], "overlapping_with": c["overlapping_with"]} for c in collisions
            ]
            return {
                "error": "Collision detected: one or more footprints would overlap after distribution.",
                "collisions": details,
            }

        for ref, nx, ny, rot in proposals:
            set_fp_at(fps[ref], nx, ny, rot)

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

    @mcp.tool()
    async def move_footprints_by_delta(
        pcb_path: str,
        references: list[str],
        dx: float,
        dy: float,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Move a group of footprints by the same (dx, dy) offset."""
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
        proposals = []
        for ref, fp in fps.items():
            ox, oy, rot = get_fp_at(fp)
            nx, ny = ox + dx, oy + dy
            proposals.append((ref, nx, ny, rot))
            moved.append({"reference": ref, "old_x": ox, "old_y": oy, "new_x": nx, "new_y": ny})

        collisions = find_collisions(data, proposals, check_within_group=False)
        if collisions:
            details = [
                {"ref": c["ref"], "overlapping_with": c["overlapping_with"]} for c in collisions
            ]
            return {
                "error": "Collision detected: one or more footprints would overlap after the move.",
                "collisions": details,
            }

        for ref, nx, ny, rot in proposals:
            set_fp_at(fps[ref], nx, ny, rot)

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
