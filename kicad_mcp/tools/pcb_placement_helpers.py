"""
PCB placement helpers: spatial query tool and shared collision utilities.

Provides:
  - ``find_free_pcb_area`` MCP tool — find valid non-overlapping placement
    positions on a 1.27 mm grid inside the board outline.
  - ``find_collisions`` — module-level function imported by all five
    footprint-positioning tools to enforce the collision guard.

PCB coordinate convention: mm, +X right, **+Y down**,
rotation **clockwise-positive**.
"""

import logging
import math
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

from kicad_mcp.utils.pcb_board_utils import get_edge_cuts_items, get_fp_courtyard_bbox
from kicad_mcp.utils.pcb_sexp_utils import load_pcb

log = logging.getLogger(__name__)

# 1.27 mm = 50 mil — standard KiCad PCB layout grid (matches schematic GRID_MM)
_GRID_MM: float = 1.27


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sym(value: Any) -> str:
    """Return the string representation of a sexpdata Symbol or plain value."""
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    return str(value)


def _get_board_bounds(data: list[Any]) -> dict[str, float] | None:
    """Return min/max bounding box of the board Edge.Cuts outline.

    Handles all Edge.Cuts item types:
    - ``gr_line``, ``gr_rect``: start/end points
    - ``gr_arc``: start/mid/end points (sufficient for corner arcs)
    - ``gr_circle``: center ± radius

    Returns ``None`` if no Edge.Cuts items are present (caller should fall back).
    """
    items = get_edge_cuts_items(data)
    if not items:
        return None

    xs: list[float] = []
    ys: list[float] = []

    for item in items:
        kind = item.get("type", "")
        if kind in ("gr_line", "gr_rect"):
            for k in ("x1", "x2"):
                if k in item:
                    xs.append(item[k])
            for k in ("y1", "y2"):
                if k in item:
                    ys.append(item[k])
        elif kind == "gr_arc":
            for k in ("start_x", "mid_x", "end_x"):
                if k in item:
                    xs.append(item[k])
            for k in ("start_y", "mid_y", "end_y"):
                if k in item:
                    ys.append(item[k])
        elif kind == "gr_circle":
            cx = item.get("cx", 0.0)
            cy = item.get("cy", 0.0)
            ex = item.get("ex", cx)
            ey = item.get("ey", cy)
            r = math.hypot(ex - cx, ey - cy)
            xs.extend([cx - r, cx + r])
            ys.extend([cy - r, cy + r])

    if not xs:
        return None

    return {
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
    }


def _get_board_bounds_or_fallback(data: list[Any]) -> dict[str, float]:
    """Return board bounds from Edge.Cuts; fall back to footprint union + 5 mm."""
    bounds = _get_board_bounds(data)
    if bounds:
        return bounds

    # Fallback: union of all footprint courtyard bboxes + 5 mm padding
    all_bboxes = _get_all_footprint_bboxes(data)
    if not all_bboxes:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 100.0, "max_y": 100.0}

    return {
        "min_x": min(b["min_x"] for b in all_bboxes) - 5.0,
        "min_y": min(b["min_y"] for b in all_bboxes) - 5.0,
        "max_x": max(b["max_x"] for b in all_bboxes) + 5.0,
        "max_y": max(b["max_y"] for b in all_bboxes) + 5.0,
    }


def _get_all_footprint_bboxes(
    data: list[Any],
    exclude_refs: set[str] | None = None,
    layer: str | None = None,
) -> list[dict[str, Any]]:
    """Return world-coordinate courtyard bboxes for all footprints.

    Args:
        data: Parsed PCB S-expression tree.
        exclude_refs: References to skip (e.g. the footprint being repositioned).
        layer: If given, only include footprints whose primary layer matches
               (e.g. ``"F.Cu"`` or ``"B.Cu"``).

    Returns:
        List of ``{ref, min_x, min_y, max_x, max_y}`` dicts.
    """
    result: list[dict[str, Any]] = []
    for item in data:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        if _sym(item[0]) != "footprint":
            continue

        # Extract reference
        ref = ""
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                prop_name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                if prop_name == "Reference":
                    ref = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                    break

        if exclude_refs and ref in exclude_refs:
            continue

        # Filter by layer if requested
        if layer is not None:
            fp_layer: str | None = None
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
                    fp_layer = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                    break
            if fp_layer != layer:
                continue

        # Extract position
        fp_x, fp_y, fp_rot = 0.0, 0.0, 0.0
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
                fp_x, fp_y = float(sub[1]), float(sub[2])
                fp_rot = float(sub[3]) if len(sub) > 3 else 0.0
                break

        bbox = get_fp_courtyard_bbox(item, fp_x, fp_y, fp_rot)
        if bbox is None:
            continue

        result.append(
            {
                "ref": ref,
                "min_x": bbox["min_x"],
                "min_y": bbox["min_y"],
                "max_x": bbox["max_x"],
                "max_y": bbox["max_y"],
            }
        )

    return result


def _get_footprint_bbox_at(
    data: list[Any],
    reference: str,
    x: float,
    y: float,
    rotation: float,
) -> dict[str, float] | None:
    """Compute the courtyard bbox for a footprint placed at a hypothetical position.

    Args:
        data: Parsed PCB S-expression tree.
        reference: Footprint reference designator.
        x: Hypothetical X anchor in mm.
        y: Hypothetical Y anchor in mm.
        rotation: Hypothetical rotation in degrees (clockwise-positive).

    Returns:
        ``{min_x, min_y, max_x, max_y, width, height}`` dict, or ``None`` if
        the footprint has no usable courtyard geometry.
    """
    for item in data:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        if _sym(item[0]) != "footprint":
            continue
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                prop_name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                if prop_name == "Reference":
                    ref_val = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                    if ref_val == reference:
                        return get_fp_courtyard_bbox(item, x, y, rotation)
    return None


def _bboxes_overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    """Return True if two axis-aligned bboxes overlap (touching edges = overlap)."""
    return not (
        a["max_x"] <= b["min_x"]
        or b["max_x"] <= a["min_x"]
        or a["max_y"] <= b["min_y"]
        or b["max_y"] <= a["min_y"]
    )


# ---------------------------------------------------------------------------
# Public collision-check function (imported by positioning tools)
# ---------------------------------------------------------------------------


def find_collisions(
    data: list[Any],
    proposals: list[tuple[str, float, float, float]],
    extra_exclude_refs: set[str] | None = None,
    layer: str | None = None,
    check_within_group: bool = True,
) -> list[dict[str, Any]]:
    """Check a list of proposed footprint positions for courtyard collisions.

    Args:
        data: Parsed PCB S-expression tree.
        proposals: List of ``(reference, x, y, rotation)`` proposed positions.
        extra_exclude_refs: Additional refs to exclude from the static footprint
            check (besides the refs already present in ``proposals``).
        layer: If given, only check collisions against footprints on this layer.
            Used by ``flip_footprint`` to limit the check to the destination layer.
        check_within_group: If False, skip collision checks between proposals
            themselves.  Use this when moving a group as a rigid unit — their
            relative positions are unchanged so any pre-existing intra-group
            overlaps should not block the move.

    Returns:
        List of ``{ref: str, overlapping_with: [str, ...]}`` for every proposal
        that overlaps an existing footprint or another proposal.  An empty list
        means no collisions detected.
    """
    # All refs being moved are always excluded from the static set
    proposal_refs: set[str] = {ref for ref, _, _, _ in proposals}
    all_excluded = proposal_refs.copy()
    if extra_exclude_refs:
        all_excluded.update(extra_exclude_refs)

    # Static footprint bboxes (everything not in the proposal set)
    static_bboxes = _get_all_footprint_bboxes(data, exclude_refs=all_excluded, layer=layer)

    # Compute proposed bboxes
    proposed: list[tuple[str, dict[str, float] | None]] = [
        (ref, _get_footprint_bbox_at(data, ref, x, y, rot)) for ref, x, y, rot in proposals
    ]

    collisions: list[dict[str, Any]] = []
    for i, (ref, bbox) in enumerate(proposed):
        if bbox is None:
            continue  # No courtyard geometry — skip

        overlapping_with: list[str] = []

        # Check against static footprints
        for sb in static_bboxes:
            if _bboxes_overlap(bbox, sb):
                overlapping_with.append(sb["ref"])

        # Check against other proposals (use their proposed bboxes)
        if check_within_group:
            for j, (other_ref, other_bbox) in enumerate(proposed):
                if i != j and other_bbox is not None and _bboxes_overlap(bbox, other_bbox):
                    overlapping_with.append(other_ref)

        if overlapping_with:
            collisions.append({"ref": ref, "overlapping_with": overlapping_with})

    return collisions


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_pcb_placement_helper_tools(mcp: FastMCP) -> None:
    """Register PCB placement spatial query tools with the MCP server."""

    @mcp.tool()
    async def find_free_pcb_area(
        pcb_path: str,
        footprint_ref: str | None = None,
        width: float | None = None,
        height: float | None = None,
        prefer_near_ref: str | None = None,
        margin: float = 0.5,
        max_candidates: int = 5,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Find valid non-overlapping positions for a footprint on the PCB.

        Scans the board area on a **1.27 mm (50 mil) grid** and returns
        candidate anchor positions where the footprint's courtyard fits
        without overlapping any existing component courtyard.

        The board Edge.Cuts outline is used as the candidate search area
        (soft boundary — footprints may still be placed outside the
        outline intentionally; this tool simply confines the search).

        PCB coordinates: mm, +X right, **+Y down**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            footprint_ref: Reference of an existing footprint to use as the
                size template (its courtyard bbox determines width/height).
                Provide this **or** explicit ``width``/``height``.
            width: Footprint width in mm.  Used when ``footprint_ref`` is
                not supplied.
            height: Footprint height in mm.  Used when ``footprint_ref`` is
                not supplied.
            prefer_near_ref: If given, sort candidates by ascending distance
                to this footprint's courtyard centre.
            margin: Extra clearance in mm around each existing footprint
                courtyard before testing overlap (default 0.5 mm).
            max_candidates: Maximum number of candidate positions to return
                (default 5).
            ctx: MCP context (unused).

        Returns:
            dict with:
                candidates: list of ``{x, y, rank}`` (and ``distance_mm``
                    when ``prefer_near_ref`` is set).
                board_bounds: ``{min_x, min_y, max_x, max_y}`` of the
                    search area.
                footprint_size: ``{width, height}`` used for candidate bbox.
                search_grid_mm: grid step used (always 1.27).
        """
        data = load_pcb(pcb_path)

        # ------------------------------------------------------------------
        # Determine footprint courtyard offsets from its anchor
        # ------------------------------------------------------------------
        _off_min_x: float | None = None
        _off_min_y: float | None = None
        _off_max_x: float | None = None
        _off_max_y: float | None = None

        if footprint_ref:
            bbox_at_origin = _get_footprint_bbox_at(data, footprint_ref, 0.0, 0.0, 0.0)
            if bbox_at_origin:
                _off_min_x = bbox_at_origin["min_x"]
                _off_min_y = bbox_at_origin["min_y"]
                _off_max_x = bbox_at_origin["max_x"]
                _off_max_y = bbox_at_origin["max_y"]

        if _off_min_x is None or _off_min_y is None or _off_max_x is None or _off_max_y is None:
            # Fall back to explicit width / height (centred on anchor)
            fw = float(width) if width is not None else None
            fh = float(height) if height is not None else None
            if fw is None or fh is None:
                return {
                    "error": (
                        "Footprint size could not be determined. "
                        "Provide footprint_ref (to auto-detect size from its "
                        "courtyard) or explicit width and height."
                    )
                }
            _off_min_x = -fw / 2.0
            _off_min_y = -fh / 2.0
            _off_max_x = fw / 2.0
            _off_max_y = fh / 2.0

        fp_off_min_x: float = _off_min_x
        fp_off_min_y: float = _off_min_y
        fp_off_max_x: float = _off_max_x
        fp_off_max_y: float = _off_max_y

        fp_width = fp_off_max_x - fp_off_min_x
        fp_height = fp_off_max_y - fp_off_min_y

        # ------------------------------------------------------------------
        # Board search bounds (soft constraint)
        # ------------------------------------------------------------------
        bounds = _get_board_bounds_or_fallback(data)

        # ------------------------------------------------------------------
        # Static footprint bboxes, inflated by margin
        # ------------------------------------------------------------------
        exclude: set[str] | None = {footprint_ref} if footprint_ref else None
        static_bboxes = _get_all_footprint_bboxes(data, exclude_refs=exclude)
        inflated_bboxes = [
            {
                "ref": sb["ref"],
                "min_x": sb["min_x"] - margin,
                "min_y": sb["min_y"] - margin,
                "max_x": sb["max_x"] + margin,
                "max_y": sb["max_y"] + margin,
            }
            for sb in static_bboxes
        ]

        # ------------------------------------------------------------------
        # prefer_near centre (from static_bboxes)
        # ------------------------------------------------------------------
        prefer_cx: float | None = None
        prefer_cy: float | None = None
        if prefer_near_ref:
            for sb in static_bboxes:
                if sb["ref"] == prefer_near_ref:
                    prefer_cx = (sb["min_x"] + sb["max_x"]) / 2.0
                    prefer_cy = (sb["min_y"] + sb["max_y"]) / 2.0
                    break

        # ------------------------------------------------------------------
        # Scan grid — ensure candidate courtyard stays inside board bounds
        # ------------------------------------------------------------------
        scan_min_x = bounds["min_x"] - fp_off_min_x
        scan_max_x = bounds["max_x"] - fp_off_max_x
        scan_min_y = bounds["min_y"] - fp_off_min_y
        scan_max_y = bounds["max_y"] - fp_off_max_y

        # Snap scan start to grid
        def _snap_up(v: float) -> float:
            return math.ceil(v / _GRID_MM) * _GRID_MM

        def _snap_down(v: float) -> float:
            return math.floor(v / _GRID_MM) * _GRID_MM

        scan_min_x = _snap_up(scan_min_x)
        scan_min_y = _snap_up(scan_min_y)
        scan_max_x = _snap_down(scan_max_x)
        scan_max_y = _snap_down(scan_max_y)

        if scan_min_x > scan_max_x or scan_min_y > scan_max_y:
            return {
                "candidates": [],
                "board_bounds": {k: round(v, 4) for k, v in bounds.items()},
                "footprint_size": {
                    "width": round(fp_width, 4),
                    "height": round(fp_height, 4),
                },
                "search_grid_mm": _GRID_MM,
                "note": "Board area too small for this footprint size.",
            }

        valid: list[dict[str, Any]] = []
        x = scan_min_x
        while x <= scan_max_x + 1e-9:
            y = scan_min_y
            while y <= scan_max_y + 1e-9:
                candidate_bbox = {
                    "min_x": x + fp_off_min_x,
                    "min_y": y + fp_off_min_y,
                    "max_x": x + fp_off_max_x,
                    "max_y": y + fp_off_max_y,
                }
                if not any(_bboxes_overlap(candidate_bbox, ib) for ib in inflated_bboxes):
                    entry: dict[str, Any] = {"x": round(x, 4), "y": round(y, 4)}
                    if prefer_cx is not None and prefer_cy is not None:
                        entry["distance_mm"] = round(math.hypot(x - prefer_cx, y - prefer_cy), 4)
                    valid.append(entry)
                y = round(y + _GRID_MM, 9)
            x = round(x + _GRID_MM, 9)

        # Sort: by distance when prefer given, else top-left first
        if prefer_cx is not None:
            valid.sort(key=lambda e: e.get("distance_mm", 0.0))
        else:
            valid.sort(key=lambda e: (e["y"], e["x"]))

        candidates = [
            {**entry, "rank": rank} for rank, entry in enumerate(valid[:max_candidates], start=1)
        ]

        return {
            "candidates": candidates,
            "board_bounds": {k: round(v, 4) for k, v in bounds.items()},
            "footprint_size": {
                "width": round(fp_width, 4),
                "height": round(fp_height, 4),
            },
            "search_grid_mm": _GRID_MM,
        }
