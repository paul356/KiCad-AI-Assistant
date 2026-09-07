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
        items: list[dict[str, Any]],
        ctx: Context | None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Move and/or rotate footprints on the PCB board.

        PCB coordinates are mm with +X right, **+Y down**, and rotation
        is in degrees, **CCW-positive on screen** (KiCad PCB convention —
        the same CCW convention as the .kicad_sym library data; 0=right,
        90=up). This tool does NOT auto-snap; pass coordinates
        already aligned to your board grid (typical SMD work uses
        0.1 mm or 0.05 mm; through-hole often 1.27 mm / 50 mil).

        Each item in *items* is ``{"reference": ..., "x"?: ...,
        "y"?: ..., "rotation"?: ...}``; an omitted coordinate key leaves
        that value unchanged.  At least one of x/y/rotation must be
        provided per item.

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

        All items are processed in one parse + one save (single ``.bak``),
        each with its own coordinates.  The courtyard collision guard is
        evaluated per footprint against the board state at that point in
        the batch (earlier items in this call have already been moved).
        Partial-apply: a footprint that cannot be found or placed keeps its
        own error in ``results`` while the remaining footprints are still
        applied and saved.  Empty, duplicate, or malformed items are
        rejected up front.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            items: List of per-footprint specs, e.g. ``[{"reference":
                "U1", "x": 10.0, "y": 20.0}, {"reference": "R2",
                "rotation": 45.0}]``.  Keys: ``reference`` (str,
                required, unique), ``x``/``y`` (float, mm world),
                ``rotation`` (float, degrees, CCW-positive on screen;
                any value; KiCad normalises).  Omitted coordinate keys
                leave that value unchanged; each item needs at least one
                of x/y/rotation.
            force: Override the courtyard collision guard.  **Default
                False — only set True when overlap is genuinely
                intentional** (e.g. edge connectors, fiducials).  A
                warning is added to the result when overlaps are
                detected and force is True.
            ctx: MCP context for progress reporting.

        Returns:
            dict with:

            - ``success``: True when every footprint was placed.
            - ``results``: per-footprint dicts — success entries carry the
              single-target fields (``status``: ``"placed"`` or
              ``"placed_at_adjusted_position"``; ``reference``;
              ``moved_from`` ``{x, y, rotation}``; ``placed_at``
              ``{x, y, rotation}``; ``requested_position`` when adjusted;
              ``warnings`` when ``force=True`` and overlaps exist), failed
              entries carry ``{reference, error, ...}``.
            - ``count``, ``applied_count``, ``failure_count``.
            - ``backup_path`` (None when nothing was moved), ``pcb_path``.
        """
        _ITEM_KEYS = {"reference", "x", "y", "rotation"}
        if not items:
            return {"error": "items must not be empty"}
        for item in items:
            if not isinstance(item, dict):
                return {"error": "each item must be a dict with reference and coordinates"}
            unknown = set(item) - _ITEM_KEYS
            if unknown:
                return {
                    "error": f"Unknown item fields: {sorted(unknown)}. Valid: {sorted(_ITEM_KEYS)}"
                }
            ref = item.get("reference")
            if not isinstance(ref, str) or not ref:
                return {"error": "items must not contain empty or missing references"}
            if all(item.get(key) is None for key in ("x", "y", "rotation")):
                return {"error": f"Item for '{ref}' must provide at least one of x, y, rotation"}
        refs = [item["reference"] for item in items]
        if len(set(refs)) != len(refs):
            return {"error": "items must not contain duplicate references"}

        data = load_pcb(pcb_path)

        def place_one(item: dict[str, Any]) -> dict[str, Any]:
            ref = item["reference"]
            x = item.get("x")
            y = item.get("y")
            rotation = item.get("rotation")
            try:
                fp = find_footprint(data, ref)
            except KeyError as exc:
                return {"error": str(exc), "reference": ref}

            old_x, old_y, old_rot = get_fp_at(fp)
            new_x = old_x if x is None else float(x)
            new_y = old_y if y is None else float(y)
            new_rot = old_rot if rotation is None else float(rotation)
            req_x, req_y = new_x, new_y  # save before possible auto-adjustment

            # Collision check (footprint vs footprint only; board bounds not enforced)
            collisions = find_collisions(data, [(ref, new_x, new_y, new_rot)])
            adjusted_position: tuple[float, float] | None = None
            if collisions and not force:
                free = find_nearest_free_position(data, ref, new_x, new_y, new_rot)
                if free is None:
                    overlapping = collisions[0]["overlapping_with"]
                    return {
                        "error": "Placement rejected: courtyard would overlap at the proposed position. Footprint was NOT moved.",
                        "reference": ref,
                        "proposed_position_overlaps": overlapping,
                        "proposed_position": {"x": new_x, "y": new_y, "rotation": new_rot},
                        "current_position": {"x": old_x, "y": old_y, "rotation": old_rot},
                        "hint": "No free spot found within 20 mm. You may need to move the interfering component first.",
                    }
                adjusted_position = free
                new_x, new_y = free

            set_fp_at(fp, new_x, new_y, new_rot)

            result: dict[str, Any] = {
                "success": True,
                "status": "placed",
                "reference": ref,
                "moved_from": {"x": old_x, "y": old_y, "rotation": old_rot},
                "placed_at": {"x": new_x, "y": new_y, "rotation": new_rot},
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

        results: list[dict[str, Any]] = []
        for item in items:
            ref = item["reference"]
            try:
                results.append(place_one(item))
            except Exception as exc:
                log.warning("set_footprint_position: reference %r failed: %s", ref, exc)
                results.append({"error": f"{ref}: {exc}", "reference": ref})

        applied_count = sum(1 for r in results if r.get("success"))

        backup_path: str | None = None
        if applied_count > 0:
            try:
                backup_path = save_pcb(pcb_path, data)
            except OSError as exc:
                return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "success": applied_count == len(results) and len(results) > 0,
            "results": results,
            "count": len(results),
            "applied_count": applied_count,
            "failure_count": len(results) - applied_count,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

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
