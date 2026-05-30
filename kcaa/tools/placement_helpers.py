"""
Placement helper tools: sheet metadata and free-area search.

These tools give the LLM an explicit view of the schematic drawing area and
let it ask "where can I put a W x H mm box?" instead of inventing
coordinates. Coordinates are in **mm** with **+Y pointing down** to match
KiCad schematic placement.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
import sexpdata

from kcaa.utils.netlist_parser import extract_netlist
from kcaa.utils.symbol_geometry import (
    BBox,
    bboxes_overlap,
    compute_unit_bboxes,
    inflate_bbox,
    lib_bbox_to_world,
    union_bboxes,
)

log = logging.getLogger(__name__)


# Standard KiCad paper sizes, width x height in mm, landscape orientation.
_PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
    "A5": (210.0, 148.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}

# Approximate default KiCad title-block footprint (anchored bottom-right of
# the page). Marked clearly as a default assumption — the real worksheet may
# differ if the user has a custom .kicad_wks.
_DEFAULT_TITLE_BLOCK_W = 105.0
_DEFAULT_TITLE_BLOCK_H = 30.0

GRID_MM = 1.27


def _parse_paper_size(schematic_path: str) -> tuple[str, float, float, bool]:
    """Read the (paper ...) clause from a .kicad_sch.

    Returns (name, width_mm, height_mm, portrait).
    Falls back to ("A4", 297, 210, False) on any error.
    """
    try:
        with open(schematic_path, encoding="utf-8") as fh:
            text = fh.read()
        tree = sexpdata.loads(text)
    except Exception as exc:
        log.warning("Could not parse schematic %s: %s", schematic_path, exc)
        return ("A4", 297.0, 210.0, False)

    paper_name = "A4"
    portrait = False
    custom_w: float | None = None
    custom_h: float | None = None
    if isinstance(tree, list):
        for item in tree:
            if (
                isinstance(item, list)
                and item
                and isinstance(item[0], sexpdata.Symbol)
                and item[0].value() == "paper"
            ):
                # (paper "A4") or (paper "User" 200 150) or (paper "A4" portrait)
                if len(item) >= 2 and isinstance(item[1], str):
                    paper_name = item[1]
                for extra in item[2:]:
                    if isinstance(extra, sexpdata.Symbol) and extra.value() == "portrait":
                        portrait = True
                    elif isinstance(extra, int | float) and custom_w is None:
                        custom_w = float(extra)
                    elif isinstance(extra, int | float) and custom_h is None:
                        custom_h = float(extra)
                break

    if custom_w is not None and custom_h is not None:
        return (paper_name, custom_w, custom_h, portrait)

    w, h = _PAPER_SIZES_MM.get(paper_name, (297.0, 210.0))
    if portrait:
        w, h = h, w
    return (paper_name, w, h, portrait)


def _default_title_block_bbox(sheet_w: float, sheet_h: float) -> BBox:
    """Return the default title-block exclusion bbox (bottom-right corner)."""
    tw = min(_DEFAULT_TITLE_BLOCK_W, sheet_w)
    th = min(_DEFAULT_TITLE_BLOCK_H, sheet_h)
    return BBox(
        min_x=sheet_w - tw,
        min_y=sheet_h - th,
        max_x=sheet_w,
        max_y=sheet_h,
    )


def register_placement_helpers(mcp: FastMCP) -> None:
    """Register schematic placement helper tools."""

    @mcp.tool()
    def get_schematic_sheet_info(schematic_path: str) -> dict[str, Any]:
        """Return drawing area, paper size, and grid for a schematic.

        Use this BEFORE placing new symbols so you know:
          * the legal coordinate range (``drawing_area``),
          * where the title block lives (``title_block_default`` —
            a conservative bottom-right reservation; treat as a default
            assumption, not authoritative),
          * the grid size (1.27 mm) — every coordinate you pass to
            ``add_symbol_to_schematic`` and ``move_component`` is auto-snapped
            to this grid.

        Coordinates are mm with +Y DOWN (KiCad screen convention).

        Returns:
            Dictionary with paper, drawing_area, title_block_default,
            grid_mm, and a usable ``recommended_area`` (drawing area minus
            10 mm margin and title block).
        """
        if not os.path.exists(schematic_path):
            return {"error": f"Schematic not found: {schematic_path}"}

        name, w, h, portrait = _parse_paper_size(schematic_path)
        title_bb = _default_title_block_bbox(w, h)

        margin = 10.0
        rec = {
            "min_x": margin,
            "min_y": margin,
            "max_x": max(margin, w - margin),
            "max_y": max(margin, title_bb.min_y - margin),
        }

        return {
            "paper": {"name": name, "width_mm": w, "height_mm": h, "portrait": portrait},
            "drawing_area": {"min_x": 0.0, "min_y": 0.0, "max_x": w, "max_y": h},
            "title_block_default": {
                "note": "Approximate default KiCad title-block footprint; "
                "not parsed from a custom .kicad_wks if any.",
                **title_bb.to_dict(),
            },
            "grid_mm": GRID_MM,
            "recommended_area": rec,
            "axis_convention": "mm, +Y is down (KiCad schematic screen coords)",
        }

    @mcp.tool()
    def find_free_area(
        schematic_path: str,
        width: float | None = None,
        height: float | None = None,
        prefer_near: dict[str, Any] | None = None,
        margin: float = 3.81,
        max_candidates: int = 5,
        for_library: str | None = None,
        for_symbol: str | None = None,
        rotation: int = 0,
    ) -> dict[str, Any]:
        """Find candidate top-left anchors where a ``width`` x ``height`` mm
        rectangle fits without overlapping placed symbols or the title block.

        If ``for_library`` and ``for_symbol`` are supplied the tool also
        returns a ready-to-use ``placement`` ``{x, y}`` for each candidate –
        the value to pass directly as the ``x``/``y`` of
        ``add_symbol_to_schematic``. ``width``/``height`` may be omitted in
        that case; the symbol's own bbox (rotated) is used. This avoids the
        common pitfall of treating ``origin`` (a bbox top-left) as if it
        were the symbol's anchor point.

        Args:
            schematic_path: Path to .kicad_sch.
            width: Width of the rectangle to fit, in mm. Required unless
                ``for_library``/``for_symbol`` are given.
            height: Height of the rectangle to fit, in mm. Same rule.
            prefer_near: Optional bias. Either ``{"x": float, "y": float}``
                or ``{"reference": "R1"}``. Candidates are sorted by
                Euclidean distance from this point (or from the centre of
                that component's bbox).
            margin: Clearance to add around every existing component bbox
                before testing overlap (mm). Default 3.81 mm (1.5 × 2.54).
                Each existing symbol's bbox already includes its full
                pin stubs, so this margin is pure extra breathing room for
                wires and Reference/Value labels.
            max_candidates: Maximum number of candidate anchors to return.
            for_library: Optional library name of the symbol to be placed.
                When provided together with ``for_symbol`` the result
                includes ``placement`` (anchor coordinates) per candidate.
            for_symbol: Optional symbol name within ``for_library``.
            rotation: Rotation (0/90/180/270) the symbol will be placed at.
                Used only when computing ``placement``.

        Returns:
            ``{"candidates": [{"origin": {"x": ..., "y": ...},
                                "bbox": {min_x, min_y, max_x, max_y},
                                "placement": {"x": ..., "y": ...}? }, ...]}``
            sorted by preference. Empty list if nothing fits.
        """
        if not os.path.exists(schematic_path):
            return {"error": f"Schematic not found: {schematic_path}"}

        # Optional symbol-aware mode: derive size + sym→bbox offset.
        sym_bbox_offset: tuple[float, float] | None = None
        if for_library and for_symbol:
            try:
                from kcaa.tools.component_edit_tools import _get_index_manager
                from kcaa.utils.symbol_extractor import extract_lib_symbol_raw

                mgr = _get_index_manager()
                lib_rec = mgr.get_library_by_name(for_library)
                if lib_rec is None:
                    return {"error": f"Library '{for_library}' not found in index"}
                sym_rec = mgr.get_symbol(for_library, for_symbol)
                if sym_rec is None:
                    return {
                        "error": (f"Symbol '{for_symbol}' not found in library '{for_library}'")
                    }
                lib_raw = extract_lib_symbol_raw(
                    lib_rec.file_path,
                    sym_rec.file_index,
                    for_symbol,
                    lib_rec.mtime,
                    lib_rec.file_size,
                )
                unit_bbs = compute_unit_bboxes(lib_raw)
                if not unit_bbs:
                    return {
                        "error": (
                            f"Symbol '{for_symbol}' has no graphics; "
                            "cannot derive size for placement"
                        )
                    }
                # Predict union over every unit at sym=(0, (N-1)*10) — same
                # offsets used by add_symbol_to_schematic.
                per_unit = []
                for unit, lib_bb in sorted(unit_bbs.items()):
                    per_unit.append(
                        lib_bbox_to_world(lib_bb, 0.0, (unit - 1) * 10.0, int(rotation), None)
                    )
                ref_at_origin = union_bboxes(per_unit)
                if ref_at_origin is None:
                    raise ValueError(
                        "ref_at_origin is None, cannot compute bounding box dimensions"
                    )
                derived_w = ref_at_origin.max_x - ref_at_origin.min_x
                derived_h = ref_at_origin.max_y - ref_at_origin.min_y
                sym_bbox_offset = (ref_at_origin.min_x, ref_at_origin.min_y)
                if width is None:
                    width = derived_w
                if height is None:
                    height = derived_h
            except Exception as exc:
                return {"error": f"Failed to inspect symbol for placement: {exc}"}

        if width is None or height is None:
            return {
                "error": (
                    "width and height are required unless for_library/for_symbol are provided"
                )
            }
        if width <= 0 or height <= 0:
            return {"error": "width and height must be positive"}

        # Collect occupied bboxes (already mm, +Y down).
        netlist = extract_netlist(schematic_path)
        components: dict[str, Any] = netlist.get("components", {}) or {}

        occupied: list[BBox] = []
        ref_bboxes: dict[str, BBox] = {}
        for ref, comp in components.items():
            bb_d = comp.get("body_bbox")
            if not bb_d:
                continue
            try:
                bb = BBox(
                    float(bb_d["min_x"]),
                    float(bb_d["min_y"]),
                    float(bb_d["max_x"]),
                    float(bb_d["max_y"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            ref_bboxes[ref] = bb
            occupied.append(inflate_bbox(bb, margin))

        # Sheet bounds and exclusions.
        _, sheet_w, sheet_h, _ = _parse_paper_size(schematic_path)
        title_bb = _default_title_block_bbox(sheet_w, sheet_h)
        occupied.append(title_bb)

        # Drawing area minus 10 mm margin so the bbox fits fully on-sheet.
        edge = 10.0
        x_lo = edge
        y_lo = edge
        x_hi = sheet_w - edge - width
        y_hi = sheet_h - edge - height
        if x_hi < x_lo or y_hi < y_lo:
            return {"candidates": [], "error": "Requested area larger than drawing area."}

        # Resolve prefer_near to a point.
        bias_x: float | None = None
        bias_y: float | None = None
        if prefer_near:
            if "reference" in prefer_near:
                ref_bb = ref_bboxes.get(prefer_near["reference"])
                if ref_bb is not None:
                    bias_x = (ref_bb.min_x + ref_bb.max_x) / 2.0
                    bias_y = (ref_bb.min_y + ref_bb.max_y) / 2.0
            elif "x" in prefer_near and "y" in prefer_near:
                try:
                    bias_x = float(prefer_near["x"])
                    bias_y = float(prefer_near["y"])
                except (TypeError, ValueError):
                    bias_x = bias_y = None

        # Snap scan to grid.
        def _snap_up(v: float) -> float:
            n = int(v / GRID_MM)
            while n * GRID_MM < v - 1e-9:
                n += 1
            return n * GRID_MM

        x0 = _snap_up(x_lo)
        y0 = _snap_up(y_lo)

        candidates: list[dict[str, Any]] = []
        # Cap to keep scan bounded on huge sheets / very small targets.
        max_collect = max(50, max_candidates * 50)
        x = x0
        outer_break = False
        while x <= x_hi + 1e-9 and not outer_break:
            y = y0
            while y <= y_hi + 1e-9:
                cand = BBox(x, y, x + width, y + height)
                conflict = False
                for occ in occupied:
                    if bboxes_overlap(cand, occ):
                        conflict = True
                        break
                if not conflict:
                    if bias_x is not None and bias_y is not None:
                        cx = (cand.min_x + cand.max_x) / 2.0
                        cy = (cand.min_y + cand.max_y) / 2.0
                        dist = ((cx - bias_x) ** 2 + (cy - bias_y) ** 2) ** 0.5
                    else:
                        # Prefer top-left when no bias is given.
                        dist = (x - x_lo) + (y - y_lo)
                    cand_dict: dict[str, Any] = {
                        "origin": {"x": x, "y": y},
                        "bbox": cand.to_dict(),
                        "_dist": dist,
                    }
                    if sym_bbox_offset is not None:
                        # bbox.min = sym + offset → sym = bbox.min - offset.
                        cand_dict["placement"] = {
                            "x": round(x - sym_bbox_offset[0], 4),
                            "y": round(y - sym_bbox_offset[1], 4),
                        }
                    candidates.append(cand_dict)
                    if len(candidates) >= max_collect:
                        outer_break = True
                        break
                y += GRID_MM
            x += GRID_MM

        candidates.sort(key=lambda c: c["_dist"])
        out = []
        for c in candidates[: max(1, max_candidates)]:
            c.pop("_dist", None)
            out.append(c)
        return {
            "candidates": out,
            "margin_mm": margin,
            "grid_mm": GRID_MM,
            "axis_convention": "mm, +Y is down",
        }
