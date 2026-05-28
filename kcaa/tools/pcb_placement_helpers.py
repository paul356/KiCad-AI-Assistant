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

from collections import defaultdict
import contextlib
import logging
import math
import re
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.utils.pcb_board_utils import get_edge_cuts_items, get_fp_courtyard_bbox
from kcaa.utils.pcb_sexp_utils import load_pcb

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
# Footprint classification and HPWL helpers
# ---------------------------------------------------------------------------


def _classify_footprint(ref: str, pad_count: int, value: str = "") -> int:
    """Classify a footprint into a priority tier for placement ordering.

    Returns:
        1 — Anchor (connectors, mounting holes): placed first, never shoved.
        2 — Semi-fixed (ICs, transistors, high-pin-count): placed after anchors.
        3 — Flexible (crystals, relays, large passives): default tier.
        4 — Free (small 2-pad passives): placed last, easiest to reposition.
    """
    m = re.match(r"[A-Za-z]+", ref)
    prefix = m.group(0).upper() if m else ""

    # Tier 1: connectors, mounting holes, test points, antennas
    if pad_count == 1 or prefix in ("J", "P", "CN", "TP", "MH", "H", "ANT", "FIDUCIAL"):
        return 1

    # Tier 2: ICs, transistors, voltage regulators, switches, high-pad-count parts
    if prefix in ("U", "IC", "Q", "T", "VR", "PS", "SW", "RLY", "K") or pad_count > 8:
        return 2

    # Tier 4: small 2-pad passives (resistors, caps, inductors, diodes, fuses)
    if prefix in ("R", "C", "L", "D", "LED", "F", "FB", "Z", "ZD") and pad_count <= 2:
        return 4

    # Tier 3: everything else (crystals, batteries, relays, larger passives)
    return 3


_TIER_NAMES = {1: "anchor", 2: "semi-fixed", 3: "flexible", 4: "free"}


def _get_fp_pads_world(fp_node: list[Any]) -> list[dict[str, Any]]:
    """Return all pads of a footprint with world coordinates and net names.

    Applies the footprint's rotation (clockwise-positive) to transform
    local pad coordinates into board world coordinates.

    Returns:
        List of ``{x, y, net}`` dicts in world mm.
    """
    fp_x, fp_y, fp_rot_deg = 0.0, 0.0, 0.0
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            fp_x, fp_y = float(sub[1]), float(sub[2])
            fp_rot_deg = float(sub[3]) if len(sub) > 3 else 0.0
            break

    theta = math.radians(fp_rot_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    pads: list[dict[str, Any]] = []
    for sub in fp_node:
        if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
            continue
        rel_x, rel_y = 0.0, 0.0
        net_name = ""
        for psub in sub:
            if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                with contextlib.suppress(ValueError, TypeError):
                    rel_x, rel_y = float(psub[1]), float(psub[2])
            elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                # KiCad 8.x: (net "name") — 2 elements
                # KiCad <8:  (net N "name") — 3 elements
                if len(psub) >= 3 and not isinstance(psub[1], str):
                    net_name = psub[2] if isinstance(psub[2], str) else _sym(psub[2])
                else:
                    net_name = psub[1] if isinstance(psub[1], str) else _sym(psub[1])
        abs_x = fp_x + rel_x * cos_t + rel_y * sin_t
        abs_y = fp_y - rel_x * sin_t + rel_y * cos_t
        pads.append({"x": abs_x, "y": abs_y, "net": net_name})
    return pads


def _get_fp_local_pads(fp_node: list[Any]) -> list[dict[str, Any]]:
    """Return all pads with raw local coordinates (no fp position/rotation applied).

    Unlike ``_get_fp_pads_world``, this does NOT apply the footprint's
    position or rotation — it returns pad coordinates in the footprint's
    own reference frame (origin at the footprint anchor, rotation = 0).
    Callers apply hypothetical rotations and offsets for scoring purposes.

    Returns:
        List of ``{lx, ly, net}`` dicts in local mm.
    """
    pads: list[dict[str, Any]] = []
    for sub in fp_node:
        if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
            continue
        lx, ly = 0.0, 0.0
        pad_rot = 0.0
        pad_w, pad_h = 0.0, 0.0
        net_name = ""
        for psub in sub:
            if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                try:
                    lx, ly = float(psub[1]), float(psub[2])
                    if len(psub) >= 4:
                        pad_rot = float(psub[3])
                except (ValueError, TypeError):
                    pass
            elif isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "size":
                with contextlib.suppress(ValueError, TypeError):
                    pad_w, pad_h = float(psub[1]), float(psub[2])
            elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                if len(psub) >= 3 and not isinstance(psub[1], str):
                    net_name = psub[2] if isinstance(psub[2], str) else _sym(psub[2])
                else:
                    net_name = psub[1] if isinstance(psub[1], str) else _sym(psub[1])
        pads.append(
            {
                "lx": lx,
                "ly": ly,
                "net": net_name,
                "pad_rot": pad_rot,
                "pad_w": pad_w,
                "pad_h": pad_h,
            }
        )
    return pads


def _compute_hpwl(data: list[Any]) -> float:
    """Compute total Half-Perimeter Wirelength (HPWL) for the board.

    For each net, sums the bounding-box half-perimeter of all pad world
    positions in that net.  Lower is better — it estimates the minimum
    total copper length needed to route the board.

    Args:
        data: Parsed PCB S-expression tree.

    Returns:
        Total HPWL in mm.
    """
    net_pads: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
            continue
        for pad in _get_fp_pads_world(item):
            if pad["net"]:
                net_pads[pad["net"]].append((pad["x"], pad["y"]))

    total = 0.0
    for positions in net_pads.values():
        if len(positions) < 2:
            continue
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _compute_group_hpwl(data: list[Any], refs: set[str]) -> float:
    """Compute HPWL for the subset of nets internal to a component group.

    Only counts nets where at least two of the pads belong to footprints
    in *refs*.  This measures how efficiently group members are arranged
    relative to each other, independent of external connections.

    Args:
        data: Parsed PCB S-expression tree.
        refs: Set of footprint reference designators in the group.

    Returns:
        Intra-group HPWL in mm.
    """
    net_pads: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
            continue
        # extract reference
        ref = ""
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                pname = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                if pname == "Reference":
                    ref = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                    break
        if ref not in refs:
            continue
        for pad in _get_fp_pads_world(item):
            if pad["net"]:
                net_pads[pad["net"]].append((pad["x"], pad["y"]))

    total = 0.0
    for positions in net_pads.values():
        if len(positions) < 2:
            continue
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _compute_layout_hpwl(
    fp_cache: dict[str, list[Any]],
    layout: list[dict[str, Any]],
) -> float:
    """Compute intra-group HPWL for a hypothetical relative layout.

    All positions are given as anchor-relative offsets; no world coordinates
    are read from the PCB tree.  This is translation-invariant and suitable
    for scoring candidate group orientations before board placement.

    Args:
        fp_cache: ``{ref: fp_node}`` mapping for all group members.
        layout: List of ``{ref, dx, dy, rotation}`` dicts.  The anchor
            should be included at (dx=0, dy=0).

    Returns:
        Intra-group HPWL in mm.  Only counts nets where ≥2 pads from
        different layout members share the same net name.
    """
    net_pads: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for pos in layout:
        fp_node = fp_cache.get(pos["ref"])
        if fp_node is None:
            continue
        local_pads = _get_fp_local_pads(fp_node)
        theta = math.radians(pos.get("rotation", 0.0))
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        dx = pos.get("dx", 0.0)
        dy = pos.get("dy", 0.0)
        for pad in local_pads:
            if not pad["net"]:
                continue
            # KiCad CW convention: x' = dx + lx*cos + ly*sin
            #                      y' = dy - lx*sin + ly*cos
            wx = dx + pad["lx"] * cos_t + pad["ly"] * sin_t
            wy = dy - pad["lx"] * sin_t + pad["ly"] * cos_t
            net_pads[pad["net"]].append((wx, wy))

    total = 0.0
    for positions in net_pads.values():
        if len(positions) < 2:
            continue
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


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


def find_nearest_free_position(
    data: list[Any],
    reference: str,
    target_x: float,
    target_y: float,
    rotation: float,
    search_radius_mm: float = 20.0,
) -> tuple[float, float] | None:
    """Find the nearest grid-aligned free position for a footprint near a target.

    Scans a ``_GRID_MM`` grid centred on ``(target_x, target_y)`` within
    ``search_radius_mm``, returning the closest anchor position where the
    footprint's courtyard does not overlap any existing footprint courtyard.

    Args:
        data: Parsed PCB S-expression tree.
        reference: Footprint reference to check (excluded from static set).
        target_x: Desired X anchor in mm.
        target_y: Desired Y anchor in mm.
        rotation: Footprint rotation in degrees (clockwise-positive).
        search_radius_mm: Maximum search radius in mm (default 20 mm).

    Returns:
        ``(x, y)`` of the nearest free anchor, or ``None`` if none found
        within ``search_radius_mm``.
    """
    # Courtyard offsets at the requested rotation anchored at origin
    bbox_at_origin = _get_footprint_bbox_at(data, reference, 0.0, 0.0, rotation)
    if bbox_at_origin is None:
        # No courtyard geometry — any position is free; snap to grid
        snap_x = round(round(target_x / _GRID_MM) * _GRID_MM, 9)
        snap_y = round(round(target_y / _GRID_MM) * _GRID_MM, 9)
        return (snap_x, snap_y)

    off_min_x = bbox_at_origin["min_x"]
    off_min_y = bbox_at_origin["min_y"]
    off_max_x = bbox_at_origin["max_x"]
    off_max_y = bbox_at_origin["max_y"]

    static_bboxes = _get_all_footprint_bboxes(data, exclude_refs={reference})

    # Snap target to nearest grid point, then scan outward sorted by distance
    snap_x = round(round(target_x / _GRID_MM) * _GRID_MM, 9)
    snap_y = round(round(target_y / _GRID_MM) * _GRID_MM, 9)
    steps = math.ceil(search_radius_mm / _GRID_MM)

    candidates: list[tuple[float, float, float]] = []
    for di in range(-steps, steps + 1):
        for dj in range(-steps, steps + 1):
            cx = round(snap_x + di * _GRID_MM, 9)
            cy = round(snap_y + dj * _GRID_MM, 9)
            dist = math.hypot(cx - target_x, cy - target_y)
            if dist <= search_radius_mm + 1e-9:
                candidates.append((dist, cx, cy))

    candidates.sort()

    for _dist, cx, cy in candidates:
        candidate_bbox = {
            "min_x": cx + off_min_x,
            "min_y": cy + off_min_y,
            "max_x": cx + off_max_x,
            "max_y": cy + off_max_y,
        }
        if not any(_bboxes_overlap(candidate_bbox, sb) for sb in static_bboxes):
            return (cx, cy)

    return None


def _compute_min_push(
    static_box: dict[str, float],
    member_bboxes: list[dict[str, float]],
) -> tuple[float, float] | None:
    """Compute the minimum axis-aligned push to clear *static_box* from all *member_bboxes*.

    The axis (X or Y) with the smaller maximum required displacement is
    chosen.  Returns ``(push_dx, push_dy)`` or ``None`` if the overlaps
    demand pushes in opposite directions on both axes (unresolvable with a
    single rigid translation).
    """
    s_cx = (static_box["min_x"] + static_box["max_x"]) / 2.0
    s_cy = (static_box["min_y"] + static_box["max_y"]) / 2.0

    best_dx = 0.0
    best_dy = 0.0
    x_conflict = False
    y_conflict = False

    for mb in member_bboxes:
        ov_x = min(static_box["max_x"], mb["max_x"]) - max(static_box["min_x"], mb["min_x"])
        ov_y = min(static_box["max_y"], mb["max_y"]) - max(static_box["min_y"], mb["min_y"])
        if ov_x <= 0.0 or ov_y <= 0.0:
            continue  # not actually overlapping

        m_cx = (mb["min_x"] + mb["max_x"]) / 2.0
        m_cy = (mb["min_y"] + mb["max_y"]) / 2.0

        dx = ov_x if s_cx >= m_cx else -ov_x
        dy = ov_y if s_cy >= m_cy else -ov_y

        if best_dx == 0.0:
            best_dx = dx
        elif (best_dx > 0) != (dx > 0):
            x_conflict = True
        elif abs(dx) > abs(best_dx):
            best_dx = dx

        if best_dy == 0.0:
            best_dy = dy
        elif (best_dy > 0) != (dy > 0):
            y_conflict = True
        elif abs(dy) > abs(best_dy):
            best_dy = dy

    if best_dx == 0.0 and best_dy == 0.0:
        return (0.0, 0.0)  # no actual overlap

    # Choose the axis with the smaller required displacement; skip axes
    # where members sit on both sides (conflicting directions).
    if not x_conflict and not y_conflict:
        if abs(best_dx) <= abs(best_dy):
            return (best_dx, 0.0)
        return (0.0, best_dy)
    if not x_conflict:
        return (best_dx, 0.0)
    if not y_conflict:
        return (0.0, best_dy)
    return None  # both axes conflict — unresolvable


def _resolve_push_shove(
    conflicting: dict[str, list[dict[str, float]]],
    all_static_bboxes: list[dict[str, Any]],
    max_push_mm: float,
) -> list[dict[str, Any]] | None:
    """Attempt to resolve group-vs-static overlaps by pushing static footprints.

    For each conflicting static footprint the minimum axis-aligned
    displacement needed to clear all overlapping group member courtyards is
    computed.  The push is accepted only when:

    * the displacement does not exceed *max_push_mm*,
    * the new position does not overlap any other (non-pushed) static
      footprint, and
    * after all pushes are applied the pushed footprints do not overlap
      each other.

    Args:
        conflicting: ``{static_ref: [member_bboxes_that_overlap_it]}``.
        all_static_bboxes: World-coordinate bboxes for all non-group footprints.
        max_push_mm: Maximum allowed push magnitude in mm.

    Returns:
        List of ``{ref, push_dx, push_dy}`` push actions (grid-snapped) on
        success, or ``None`` if any conflict cannot be resolved.
    """
    static_bbox_map: dict[str, dict[str, float]] = {b["ref"]: b for b in all_static_bboxes}
    # Track tentatively updated bboxes to detect newly introduced conflicts.
    updated: dict[str, dict[str, float]] = {ref: dict(b) for ref, b in static_bbox_map.items()}
    push_list: list[dict[str, Any]] = []

    for s_ref, member_bboxes in conflicting.items():
        s_box = static_bbox_map.get(s_ref)
        if s_box is None:
            return None

        push = _compute_min_push(s_box, member_bboxes)
        if push is None:
            return None  # unresolvable direction conflict

        push_dx, push_dy = push

        # Round up to the nearest full grid step so the courtyard fully clears.
        if push_dx != 0.0:
            push_dx = math.copysign(math.ceil(abs(push_dx) / _GRID_MM) * _GRID_MM, push_dx)
        if push_dy != 0.0:
            push_dy = math.copysign(math.ceil(abs(push_dy) / _GRID_MM) * _GRID_MM, push_dy)

        if math.hypot(push_dx, push_dy) > max_push_mm:
            return None  # push exceeds the allowed limit

        new_box: dict[str, float] = {
            "min_x": s_box["min_x"] + push_dx,
            "min_y": s_box["min_y"] + push_dy,
            "max_x": s_box["max_x"] + push_dx,
            "max_y": s_box["max_y"] + push_dy,
        }

        # Ensure the push does not create a new conflict with any footprint
        # that is neither this one nor another footprint also being pushed.
        for other_ref, other_box in updated.items():
            if other_ref == s_ref or other_ref in conflicting:
                continue
            if _bboxes_overlap(new_box, other_box):
                return None  # would introduce a new collision

        updated[s_ref] = new_box
        push_list.append({"ref": s_ref, "push_dx": push_dx, "push_dy": push_dy})

    # Cross-check: pushed footprints must not overlap each other after all pushes.
    pushed_refs = [p["ref"] for p in push_list]
    for i in range(len(pushed_refs)):
        for j in range(i + 1, len(pushed_refs)):
            if _bboxes_overlap(updated[pushed_refs[i]], updated[pushed_refs[j]]):
                return None

    return push_list


def _compute_hpwl_with_hypothetical_group(
    data: list[Any],
    group_refs: set[str],
    fp_node_cache: dict[str, Any],
    relative_layout: list[dict[str, Any]],
    anchor_x: float,
    anchor_y: float,
) -> float:
    """Compute total board HPWL with a hypothetical group placement.

    Merges the hypothetical group member pad positions at (anchor_x, anchor_y)
    with all static footprint pads and computes the total HPWL.

    Args:
        data: Parsed PCB S-expression tree.
        group_refs: Set of group member references (to be excluded from static).
        fp_node_cache: ``{ref: fp_node}`` for group members.
        relative_layout: List of ``{ref, dx, dy, rotation}`` for the group.
        anchor_x: Hypothetical anchor world X position (mm).
        anchor_y: Hypothetical anchor world Y position (mm).

    Returns:
        Total board HPWL in mm.
    """
    net_pads: dict[str, list[tuple[float, float]]] = defaultdict(list)

    # 1. Collect all static footprint pads (not in the group).
    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
            continue
        # Extract reference
        ref = ""
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                pname = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                if pname == "Reference":
                    ref = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                    break
        if ref in group_refs:
            continue  # Skip group members — we'll add them from the layout
        for pad in _get_fp_pads_world(item):
            if pad["net"]:
                net_pads[pad["net"]].append((pad["x"], pad["y"]))

    # 2. Add hypothetical group member pads.
    for pos in relative_layout:
        fp_node = fp_node_cache.get(pos["ref"])
        if fp_node is None:
            continue
        local_pads = _get_fp_local_pads(fp_node)
        theta = math.radians(pos.get("rotation", 0.0))
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        dx = anchor_x + pos.get("dx", 0.0)
        dy = anchor_y + pos.get("dy", 0.0)
        for pad in local_pads:
            if not pad["net"]:
                continue
            # KiCad CW convention: x' = dx + lx*cos + ly*sin
            #                      y' = dy - lx*sin + ly*cos
            wx = dx + pad["lx"] * cos_t + pad["ly"] * sin_t
            wy = dy - pad["lx"] * sin_t + pad["ly"] * cos_t
            net_pads[pad["net"]].append((wx, wy))

    # 3. Compute HPWL.
    total = 0.0
    for positions in net_pads.values():
        if len(positions) < 2:
            continue
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _find_group_board_position(
    data: list[Any],
    group_refs: set[str],
    relative_layout: list[dict[str, Any]],
    prefer_near: tuple[float, float] | None = None,
    extra_exclude_refs: set[str] | None = None,
    push_shove_mm: float = 0.0,
    anchor_current_pos: tuple[float, float] | None = None,
) -> tuple[float, float, bool, list[dict[str, Any]]]:
    """Find the optimal board position for a group.

    **New behavior (when anchor_current_pos is provided):**
    1. Start from the anchor's current position
    2. If there are overlaps, calculate the direction that minimizes overlap
    3. Search in that direction for a non-overlap position
    4. If no clear position found, fall back to full board scan

    **Legacy behavior (when anchor_current_pos is None):**
    Scans the board at 2.54 mm steps, collects all collision-free positions,
    and returns the position that minimizes total board HPWL (Half-Perimeter
    Wirelength).  This places the group where it minimizes track length to
    the rest of the circuit.

    **Issue 1 — excluding unoptimised footprints**: beyond the group members
    (always excluded), any refs in *extra_exclude_refs* are also removed from
    the static obstacle set.  Pass the refs of footprints that have not yet
    been placed in the current optimisation pass so their stale board
    positions do not unnecessarily block the scan.

    **Issue 3 — push and shove**: when *push_shove_mm* > 0, candidate
    positions that are only partially blocked are accepted if every
    conflicting static footprint can be pushed clear with an axis-aligned
    displacement ≤ *push_shove_mm*.  The required push actions are returned
    so the caller can apply them to the board data.

    Args:
        data: Parsed PCB S-expression tree.
        group_refs: Set of group member references (excluded from static check).
        relative_layout: List of ``{ref, dx, dy, rotation}`` dicts, where
            (dx, dy) are anchor-relative offsets.  The anchor should be
            included at (dx=0, dy=0).
        prefer_near: Deprecated (ignored) - position is now chosen by HPWL.
        extra_exclude_refs: Additional footprint refs to exclude from the
            static obstacle set (e.g. members of other groups that will be
            placed later and whose current positions are stale).
        push_shove_mm: Maximum push distance (mm) to allow for push-and-shove
            conflict resolution.  ``0.0`` (the default) disables push-and-shove.
        anchor_current_pos: Current (x, y) position of the anchor. If provided,
            the search will start from this position and intelligently find
            nearby non-overlapping positions.

    Returns:
        ``(anchor_x, anchor_y, found_clear, push_list)`` where *found_clear*
        is ``True`` when a collision-free (or push-resolved) position was
        found, and *push_list* is a list of ``{ref, push_dx, push_dy}`` dicts
        for any footprints that must be displaced to make room.
    """
    STEP = 2.54  # 2 × 1.27 mm grid — coarse scan
    bounds = _get_board_bounds_or_fallback(data)

    # Issue 1: exclude group members AND any caller-specified pending refs.
    _exclude = set(group_refs)
    if extra_exclude_refs:
        _exclude.update(extra_exclude_refs)
    static_bboxes = _get_all_footprint_bboxes(data, exclude_refs=_exclude)

    # Pre-build {ref → fp_node} for group members so the inner scan loop
    # uses O(1) lookups instead of re-scanning all of data on every step.
    fp_node_cache: dict[str, Any] = {}
    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
            continue
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                prop_name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                if prop_name == "Reference":
                    ref_val = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                    if ref_val in group_refs:
                        fp_node_cache[ref_val] = item
                    break

    # Helper to check overlap for a given position
    def check_position(
        cx: float, cy: float
    ) -> tuple[bool, dict[str, list[dict[str, float]]], list[dict[str, float]]]:
        """Check if position (cx, cy) is collision-free.

        Returns:
            (found_clear, conflicting, placed_member_bboxes) where:
            - found_clear: True if no overlaps
            - conflicting: dict of {static_ref: [overlapping_member_bboxes]}
            - placed_member_bboxes: list of all member bboxes at this position
        """
        conflicting: dict[str, list[dict[str, float]]] = {}
        found_clear = True
        placed_member_bboxes: list[dict[str, float]] = []

        for pos in relative_layout:
            fp_node = fp_node_cache.get(pos["ref"])
            if fp_node is None:
                continue
            mx = round(cx + pos["dx"], 9)
            my = round(cy + pos["dy"], 9)
            member_bbox = get_fp_courtyard_bbox(fp_node, mx, my, pos["rotation"])
            if member_bbox is None:
                continue
            member_clear = True
            # Check against external (non-group) static footprints.
            for sb in static_bboxes:
                if _bboxes_overlap(member_bbox, sb):
                    found_clear = False
                    member_clear = False
                    if push_shove_mm > 0.0:
                        conflicting.setdefault(sb["ref"], []).append(member_bbox)
                    else:
                        break
            if not member_clear and push_shove_mm <= 0.0:
                break
            # Check against other group members
            if member_clear:
                for pm in placed_member_bboxes:
                    if _bboxes_overlap(member_bbox, pm):
                        found_clear = False
                        member_clear = False
                        break
            if member_clear:
                placed_member_bboxes.append(member_bbox)

        return found_clear, conflicting, placed_member_bboxes

    # Helper to calculate overlap area between two bboxes
    def overlap_area(bb1: dict[str, float], bb2: dict[str, float]) -> float:
        """Calculate the overlap area between two bounding boxes."""
        if not _bboxes_overlap(bb1, bb2):
            return 0.0
        x_overlap = min(bb1["max_x"], bb2["max_x"]) - max(bb1["min_x"], bb2["min_x"])
        y_overlap = min(bb1["max_y"], bb2["max_y"]) - max(bb1["min_y"], bb2["min_y"])
        return x_overlap * y_overlap

    # New smart search: start from current anchor position if provided
    if anchor_current_pos is not None:
        current_x, current_y = anchor_current_pos
        # Snap to grid
        current_x = round(round(current_x / _GRID_MM) * _GRID_MM, 9)
        current_y = round(round(current_y / _GRID_MM) * _GRID_MM, 9)

        log.info(
            f"Smart placement: starting from anchor position ({current_x:.1f}, {current_y:.1f})"
        )

        # Check if current position is clear
        found_clear, conflicting, placed_bboxes = check_position(current_x, current_y)

        if found_clear:
            log.info("Current position is clear, using it")
            hpwl = _compute_hpwl_with_hypothetical_group(
                data, group_refs, fp_node_cache, relative_layout, current_x, current_y
            )
            log.info(f"Position ({current_x:.1f}, {current_y:.1f}) HPWL: {hpwl:.1f} mm")
            return current_x, current_y, True, []

        if push_shove_mm > 0.0 and conflicting:
            push_list = _resolve_push_shove(conflicting, static_bboxes, push_shove_mm)
            if push_list is not None:
                log.info("Current position viable with push-and-shove")
                return current_x, current_y, True, push_list

        # Current position has overlaps - calculate best direction to move
        log.info(
            f"Current position has overlaps with {len(conflicting) if conflicting else 'members'}"
        )

        # Calculate total overlap for each direction
        # We'll try 8 cardinal/diagonal directions
        directions = [
            (1.0, 0.0),  # Right
            (1.0, 1.0),  # Down-right
            (0.0, 1.0),  # Down
            (-1.0, 1.0),  # Down-left
            (-1.0, 0.0),  # Left
            (-1.0, -1.0),  # Up-left
            (0.0, -1.0),  # Up
            (1.0, -1.0),  # Up-right
        ]

        # For each direction, calculate total overlap reduction
        direction_scores: list[tuple[float, float, float]] = []

        for dx_norm, dy_norm in directions:
            # Calculate overlap at current position in this direction
            total_overlap = 0.0
            for pos in relative_layout:
                fp_node = fp_node_cache.get(pos["ref"])
                if fp_node is None:
                    continue
                mx = round(current_x + pos["dx"], 9)
                my = round(current_y + pos["dy"], 9)
                member_bbox = get_fp_courtyard_bbox(fp_node, mx, my, pos["rotation"])
                if member_bbox is None:
                    continue

                # Check overlap with all static components
                for sb in static_bboxes:
                    area = overlap_area(member_bbox, sb)
                    if area > 0:
                        # Calculate centroid of overlap region
                        overlap_cx = (
                            max(member_bbox["min_x"], sb["min_x"])
                            + min(member_bbox["max_x"], sb["max_x"])
                        ) / 2
                        overlap_cy = (
                            max(member_bbox["min_y"], sb["min_y"])
                            + min(member_bbox["max_y"], sb["max_y"])
                        ) / 2
                        member_cx = (member_bbox["min_x"] + member_bbox["max_x"]) / 2
                        member_cy = (member_bbox["min_y"] + member_bbox["max_y"]) / 2

                        # Direction from overlap center to member center
                        away_dx = member_cx - overlap_cx
                        away_dy = member_cy - overlap_cy

                        # Normalize if non-zero
                        mag = math.hypot(away_dx, away_dy)
                        if mag > 0.001:
                            away_dx /= mag
                            away_dy /= mag
                            # Dot product with direction: positive means moving away
                            alignment = away_dx * dx_norm + away_dy * dy_norm
                            # Negative alignment means we're reducing overlap
                            total_overlap += area * (1.0 - alignment)

            direction_scores.append((total_overlap, dx_norm, dy_norm))

        # Sort by overlap score (lower is better - means moving away from overlaps)
        direction_scores.sort(key=lambda x: x[0])

        log.info(
            f"Best direction: ({direction_scores[0][1]:.1f}, {direction_scores[0][2]:.1f}) "
            f"with score {direction_scores[0][0]:.1f}"
        )

        # Search in the best directions for a clear position
        max_search_steps = 100  # Search up to 100 grid steps
        for overlap_score, best_dx, best_dy in direction_scores[:3]:  # Try top 3 directions
            for step in range(1, max_search_steps + 1):
                test_x = current_x + step * STEP * best_dx
                test_y = current_y + step * STEP * best_dy
                test_x = round(round(test_x / _GRID_MM) * _GRID_MM, 9)
                test_y = round(round(test_y / _GRID_MM) * _GRID_MM, 9)

                # Check bounds
                if not (
                    bounds["min_x"] <= test_x <= bounds["max_x"]
                    and bounds["min_y"] <= test_y <= bounds["max_y"]
                ):
                    break

                found_clear, conflicting, _ = check_position(test_x, test_y)

                if found_clear:
                    hpwl = _compute_hpwl_with_hypothetical_group(
                        data, group_refs, fp_node_cache, relative_layout, test_x, test_y
                    )
                    log.info(
                        f"Found clear position at ({test_x:.1f}, {test_y:.1f}) "
                        f"after {step} steps, HPWL: {hpwl:.1f} mm"
                    )
                    return test_x, test_y, True, []

                if push_shove_mm > 0.0 and conflicting:
                    push_list = _resolve_push_shove(conflicting, static_bboxes, push_shove_mm)
                    if push_list is not None:
                        log.info(
                            f"Found push-shove position at ({test_x:.1f}, {test_y:.1f}) "
                            f"after {step} steps"
                        )
                        return test_x, test_y, True, push_list

        log.info("No clear position found in preferred directions, falling back to full scan")

    # Fallback: full board scan (legacy behavior)
    last_cx, last_cy = bounds["min_x"], bounds["min_y"]
    # Fallback: full board scan (legacy behavior)
    last_cx, last_cy = bounds["min_x"], bounds["min_y"]

    # Build list of candidate (cx, cy) positions.
    candidates: list[tuple[float, float]] = []
    y = bounds["min_y"]
    while y <= bounds["max_y"]:
        x = bounds["min_x"]
        while x <= bounds["max_x"]:
            cx = round(round(x / _GRID_MM) * _GRID_MM, 9)
            cy = round(round(y / _GRID_MM) * _GRID_MM, 9)
            candidates.append((cx, cy))
            x += STEP
        y += STEP

    # Collect all clear positions with their HPWL scores.
    clear_positions: list[tuple[float, float, float, list[dict[str, Any]]]] = []

    for cx, cy in candidates:
        found_clear, conflicting, _ = check_position(cx, cy)

        if found_clear:
            # Compute HPWL with group placed at this position.
            hpwl = _compute_hpwl_with_hypothetical_group(
                data, group_refs, fp_node_cache, relative_layout, cx, cy
            )
            clear_positions.append((cx, cy, hpwl, []))
        elif push_shove_mm > 0.0 and conflicting:
            # Issue 3: try push-and-shove when enabled and conflicts are present.
            push_list = _resolve_push_shove(conflicting, static_bboxes, push_shove_mm)
            if push_list is not None:
                hpwl = _compute_hpwl_with_hypothetical_group(
                    data, group_refs, fp_node_cache, relative_layout, cx, cy
                )
                clear_positions.append((cx, cy, hpwl, push_list))

        last_cx, last_cy = cx, cy

    # Return position with minimum HPWL.
    if clear_positions:
        best = min(clear_positions, key=lambda p: p[2])
        log.info(
            f"Evaluated {len(clear_positions)} clear positions. "
            f"HPWL range: {min(p[2] for p in clear_positions):.1f} - {max(p[2] for p in clear_positions):.1f} mm. "
            f"Best position: ({best[0]:.1f}, {best[1]:.1f}) with HPWL {best[2]:.1f} mm"
        )
        return best[0], best[1], True, best[3]

    return last_cx, last_cy, False, []


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
