"""
PCB component group management and batch placement tools.

Groups are stored as a ``placement_group`` property on each footprint,
so group assignments persist in the .kicad_pcb file and are visible in
the KiCad GUI's footprint properties dialog.

Workflow:
  1. ``assign_to_group``       — tag footprints with a group name (batch).
  2. ``list_groups``           — inspect all groups on the board.
  3. ``get_group``             — details for one group (members, bbox, anchor).
  4. ``score_group``           — intra-group HPWL quality metric.
  5. ``place_component_group`` — two-phase automatic placement: arranges
                                 members radially (Phase 1) then finds the
                                 first clear board position via raster scan
                                 (Phase 2) and commits.
  6. ``move_group``             — translate a placed group as a rigid unit.
  7. ``rotate_group``           — rotate a placed group around its anchor.

PCB coordinate convention: mm, +X right, +Y down, rotation CW-positive.
"""

import logging
import math
import re
from typing import Any

from fastmcp import Context, FastMCP

from kicad_mcp.tools.pcb_placement_helpers import (
    _bboxes_overlap,
    _classify_footprint,
    _compute_group_hpwl,
    _find_group_board_position,
    _get_fp_local_pads,
    _get_fp_pads_world,
    _GRID_MM,
    _TIER_NAMES,
    find_collisions,
)
from kicad_mcp.utils.pcb_board_utils import get_fp_courtyard_bbox
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    set_fp_at,
    upsert_fp_property,
    _sym,
)
from kicad_mcp.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)

# KiCad footprint property key used to store the group assignment.
_GROUP_PROPERTY = "placement_group"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iter_footprints(data: list[Any]):
    """Yield each footprint S-expression node from the parsed PCB tree."""
    for item in data:
        if isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint":
            yield item


def _fp_pad_count(fp_node: list[Any]) -> int:
    """Return the number of pad nodes in a footprint."""
    return sum(
        1 for sub in fp_node
        if isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"
    )


def _get_group_members(data: list[Any], group_name: str) -> list[dict[str, Any]]:
    """Return footprint info dicts for all members of *group_name*."""
    members = []
    for fp in _iter_footprints(data):
        if get_fp_property(fp, _GROUP_PROPERTY) == group_name:
            ref = get_fp_property(fp, "Reference") or ""
            value = get_fp_property(fp, "Value") or ""
            x, y, rot = get_fp_at(fp)
            layer = get_fp_layer(fp) or ""
            pad_count = _fp_pad_count(fp)
            members.append({
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "rotation": rot,
                "layer": layer,
                "pad_count": pad_count,
                "tier": _classify_footprint(ref, pad_count, value),
                "tier_name": _TIER_NAMES.get(_classify_footprint(ref, pad_count, value), "unknown"),
            })
    return members


def _find_anchor(members: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the member with the lowest (highest-priority) tier.

    Ties are broken by reference string sort order (stable, deterministic).
    Returns ``None`` if *members* is empty.
    """
    if not members:
        return None
    return min(members, key=lambda m: (m["tier"], m["reference"]))


# ---------------------------------------------------------------------------
# Grid-based layout helpers
# ---------------------------------------------------------------------------


def _is_ground_net(net: str) -> bool:
    """Return True when *net* is a ground net (GND in the name, case-insensitive)."""
    return "GND" in net.upper()


def _get_edge_normal_direction(
    pad_x: float,
    pad_y: float,
    courtyard: dict[str, float] | None
) -> tuple[float, float]:
    """Classify pad to nearest courtyard edge and return unit normal direction.
    
    For elongated connectors (e.g. long double-row DIN), this ensures all pads
    in the same column/row get the same directional preference, creating better
    clustering than using raw pad positions.
    
    Args:
        pad_x, pad_y: Pad position relative to footprint center
        courtyard: Bounding box dict with keys min_x, max_x, min_y, max_y
        
    Returns:
        Unit vector pointing away from the nearest edge:
        LEFT edge   → (-1, 0)
        RIGHT edge  → (+1, 0)
        ABOVE edge  → (0, -1)
        BELOW edge  → (0, +1)
        
    If courtyard is None, falls back to raw pad position (normalized).
    """
    if courtyard is None:
        # No courtyard: use raw pad direction
        dist = math.sqrt(pad_x * pad_x + pad_y * pad_y)
        if dist < 0.001:
            return (1.0, 0.0)  # Default to RIGHT
        return (pad_x / dist, pad_y / dist)
    
    # Calculate distance to each courtyard edge
    dist_to_left = abs(pad_x - courtyard["min_x"])
    dist_to_right = abs(pad_x - courtyard["max_x"])
    dist_to_top = abs(pad_y - courtyard["min_y"])
    dist_to_bottom = abs(pad_y - courtyard["max_y"])
    
    # Find nearest edge
    min_dist = min(dist_to_left, dist_to_right, dist_to_top, dist_to_bottom)
    
    if min_dist == dist_to_left:
        return (-1.0, 0.0)  # LEFT edge
    elif min_dist == dist_to_right:
        return (+1.0, 0.0)  # RIGHT edge
    elif min_dist == dist_to_top:
        return (0.0, -1.0)  # ABOVE edge
    else:
        return (0.0, +1.0)  # BELOW edge


def _generate_grid_candidates(
    radius_mm: float,
    center_x: float = 0.0,
    center_y: float = 0.0,
    prefer_direction: tuple[float, float] | None = None,
    anchor_pad_size: tuple[float, float] | None = None,
    member_pad_offset: tuple[float, float] | None = None,
    member_pad_size: tuple[float, float] | None = None,
) -> list[tuple[float, float]]:
    """Return all _GRID_MM-spaced positions within *radius_mm* of the origin,
    sorted to minimize edge-to-edge routing distance between pads.

    When prefer_direction is provided, candidates are scored by the actual
    edge-to-edge routing distance, NOT centroid-to-centroid. We route from
    the facing edges of the two pads:
    
    - If member pad is RIGHT of anchor: anchor.right_edge → member.left_edge
    - If member pad is LEFT of anchor: anchor.left_edge → member.right_edge  
    - If member pad is BELOW anchor: anchor.bottom_edge → member.top_edge
    - If member pad is ABOVE anchor: anchor.top_edge → member.bottom_edge
    
    This gives the true minimum routing length between the pad edges, accounting
    for the rectangular extents of both pads in the actual routing direction.

    Callers are responsible for skipping the anchor position (0, 0) if it
    must not be used as a placement target.

    Args:
        radius_mm: Search radius around the board origin in mm.
        center_x: X coordinate of the sort centre (anchor-relative, mm).
            Pass the X position of the connecting anchor pad so that
            candidates nearest that pad are tried first.
        center_y: Y coordinate of the sort centre (anchor-relative, mm).
        prefer_direction: Optional (dx, dy) vector pointing to anchor pad.
            Candidates in this direction are strongly prioritized.
        anchor_pad_size: Optional (width, height) of the anchor pad in mm.
            Used to calculate edge positions.
        member_pad_offset: Optional (lx, ly) local offset of the member pad
            from the footprint center. Used to calculate where the member
            pad would actually be placed.
        member_pad_size: Optional (width, height) of the member pad in mm.
            Used to calculate edge positions.

    Returns:
        List of (x, y) tuples on the _GRID_MM grid, sorted by edge-to-edge
        routing distance between the pads.
    """
    steps = int(math.ceil(radius_mm / _GRID_MM))
    candidates: list[tuple[float, float, float]] = []
    
    # Normalize preferred direction if provided
    prefer_dx, prefer_dy = 0.0, 0.0
    if prefer_direction:
        mag = math.hypot(prefer_direction[0], prefer_direction[1])
        if mag > 0.01:
            prefer_dx = prefer_direction[0] / mag
            prefer_dy = prefer_direction[1] / mag
    
    for row in range(-steps, steps + 1):
        for col in range(-steps, steps + 1):
            x = round(col * _GRID_MM, 9)
            y = round(row * _GRID_MM, 9)
            dx = x - center_x
            dy = y - center_y
            dist_sq = dx * dx + dy * dy
            
            # Calculate directional score if prefer_direction provided
            if prefer_direction and mag > 0.01:
                # Member pad position (where it would be at this candidate)
                if member_pad_offset:
                    member_pad_cx = x + member_pad_offset[0]
                    member_pad_cy = y + member_pad_offset[1]
                else:
                    member_pad_cx, member_pad_cy = x, y
                
                # Anchor pad position (from prefer_direction, which is anchor_pad_cx, anchor_pad_cy)
                anchor_pad_cx = prefer_direction[0]
                anchor_pad_cy = prefer_direction[1]
                
                # Calculate edge-to-edge routing distance
                # We route from the facing edges, not from centroids
                
                # Get pad dimensions (default to 0 if not provided)
                aw = anchor_pad_size[0] if anchor_pad_size else 0.0
                ah = anchor_pad_size[1] if anchor_pad_size else 0.0
                mw = member_pad_size[0] if member_pad_size else 0.0
                mh = member_pad_size[1] if member_pad_size else 0.0
                
                # Calculate distances between edges
                # Horizontal distance: distance between left/right edges
                if member_pad_cx >= anchor_pad_cx:
                    # Member is to the RIGHT of anchor
                    # Connect anchor RIGHT edge to member LEFT edge
                    horiz_dist = (member_pad_cx - mw/2) - (anchor_pad_cx + aw/2)
                else:
                    # Member is to the LEFT of anchor
                    # Connect anchor LEFT edge to member RIGHT edge
                    horiz_dist = (anchor_pad_cx - aw/2) - (member_pad_cx + mw/2)
                
                # Vertical distance: distance between top/bottom edges
                if member_pad_cy >= anchor_pad_cy:
                    # Member is BELOW anchor (remember +Y is down)
                    # Connect anchor BOTTOM edge to member TOP edge
                    vert_dist = (member_pad_cy - mh/2) - (anchor_pad_cy + ah/2)
                else:
                    # Member is ABOVE anchor
                    # Connect anchor TOP edge to member BOTTOM edge
                    vert_dist = (anchor_pad_cy - ah/2) - (member_pad_cy + mh/2)
                
                # The effective routing distance is the larger of the two
                # (We need to route in both directions if both are positive)
                # If either is negative, pads overlap in that dimension
                horiz_dist = max(0.0, horiz_dist)
                vert_dist = max(0.0, vert_dist)
                
                # Use Euclidean distance for diagonal routing
                # (Manhattan distance would be horiz_dist + vert_dist)
                effective_dist = math.sqrt(horiz_dist * horiz_dist + vert_dist * vert_dist)
                
                # Apply directional preference: favor positions in same direction as anchor pad
                # Calculate direction from anchor origin to member pad
                member_dist = math.sqrt(member_pad_cx * member_pad_cx + member_pad_cy * member_pad_cy)
                if member_dist > 0.001:
                    # Normalized direction to member pad
                    member_dir_x = member_pad_cx / member_dist
                    member_dir_y = member_pad_cy / member_dist
                    
                    # Dot product with preferred direction (already normalized earlier)
                    # prefer_dx, prefer_dy are normalized direction to anchor pad
                    dot = member_dir_x * prefer_dx + member_dir_y * prefer_dy
                    
                    # Map dot product [-1, 1] to multiplier [1.5, 0.7]
                    # dot = +1 (same direction): multiplier = 0.7 (30% discount)
                    # dot =  0 (perpendicular): multiplier = 1.1 (10% penalty)
                    # dot = -1 (opposite): multiplier = 1.5 (50% penalty)
                    direction_multiplier = 1.1 - 0.4 * dot
                    
                    effective_dist = effective_dist * direction_multiplier
                
                # Use effective distance as primary sort key
                score = (effective_dist * effective_dist, dist_sq)
            else:
                score = (dist_sq, 0.0)
            
            candidates.append((x, y, score[0], score[1]))
    
    # Sort by effective distance (directional), then absolute distance (tie-breaker)
    candidates.sort(key=lambda c: (c[2], c[3]))
    return [(c[0], c[1]) for c in candidates]


def _choose_rotation_for_connection(
    member_pad_lx: float,
    member_pad_ly: float,
    anchor_pad_cx: float,
    anchor_pad_cy: float,
    anchor_bbox: dict[str, float] | None,
    member_footprint: list[Any] | None = None,
) -> tuple[float, float, float]:
    """Determine rotation that places member component body OUTSIDE anchor courtyard.
    
    Strategy:
    1. Determine which direction the anchor_pad is from anchor center (0, 0)
    2. Choose rotation so member pad faces that direction
    3. Calculate ideal footprint center that clears anchor courtyard (considering member courtyard)
    4. Derive ideal pad position from ideal footprint center
    5. Return (rotation, ideal_pad_x, ideal_pad_y)
    
    This ensures the component body extends AWAY from the anchor and fully clears its courtyard.
    """
    if not anchor_bbox:
        # No courtyard - place at anchor pad with 0° rotation
        return 0.0, anchor_pad_cx, anchor_pad_cy
    
    # Determine which direction the anchor pad is from anchor center
    # This tells us which SIDE of the anchor the pad is on
    angle_from_center = math.degrees(math.atan2(anchor_pad_cy, anchor_pad_cx))
    
    # Normalize to [0, 360) and round to nearest 90°
    direction_90 = round(angle_from_center / 90) * 90
    direction_90 = direction_90 % 360
    
    # Calculate pad angle at 0° rotation
    pad_angle_0 = math.degrees(math.atan2(member_pad_ly, member_pad_lx))
    
    # Determine rotation and target angle based on direction
    gap = 1.0  # mm clearance outside courtyard
    
    if direction_90 == 0:  # Anchor pad on RIGHT side
        # Place member to RIGHT, pad faces LEFT (180°)
        target_angle = 180.0
    elif direction_90 == 180:  # Anchor pad on LEFT side
        # Place member to LEFT, pad faces RIGHT (0°)
        target_angle = 0.0
    elif direction_90 == 90:  # Anchor pad on BOTTOM side (+Y down)
        # Place member below, pad faces UP (270°)
        target_angle = 270.0
    else:  # direction_90 == 270, Anchor pad on TOP side
        # Place member above, pad faces DOWN (90°)
        target_angle = 90.0
    
    # Calculate rotation needed
    needed_rotation = target_angle - pad_angle_0
    best_rot = round(needed_rotation / 90) * 90
    best_rot = best_rot % 360
    
    # Get member courtyard at this rotation to calculate clearances
    member_cy_half_width = 0.0
    member_cy_half_height = 0.0
    if member_footprint:
        member_bbox = get_fp_courtyard_bbox(member_footprint, 0.0, 0.0, best_rot)
        if member_bbox:
            member_cy_half_width = (member_bbox["max_x"] - member_bbox["min_x"]) / 2.0
            member_cy_half_height = (member_bbox["max_y"] - member_bbox["min_y"]) / 2.0
    
    # Calculate ideal FOOTPRINT CENTER that clears anchor courtyard
    # For horizontal (RIGHT/LEFT) placement, only move in X to clear
    # For vertical (TOP/BOTTOM) placement, only move in Y to clear
    if direction_90 == 0:  # RIGHT side
        ideal_fp_cx = anchor_bbox["max_x"] + gap + member_cy_half_width
        ideal_fp_cy = anchor_pad_cy  # Match anchor pad Y position
    elif direction_90 == 180:  # LEFT side
        ideal_fp_cx = anchor_bbox["min_x"] - gap - member_cy_half_width
        ideal_fp_cy = anchor_pad_cy  # Match anchor pad Y position
    elif direction_90 == 90:  # BOTTOM side
        ideal_fp_cx = anchor_pad_cx  # Match anchor pad X position
        ideal_fp_cy = anchor_bbox["max_y"] + gap + member_cy_half_height
    else:  # TOP side (direction_90 == 270)
        ideal_fp_cx = anchor_pad_cx  # Match anchor pad X position
        ideal_fp_cy = anchor_bbox["min_y"] - gap - member_cy_half_height
    
    # Convert ideal footprint center to ideal pad position
    rot_rad = math.radians(best_rot)
    rotated_pad_lx = member_pad_lx * math.cos(rot_rad) - member_pad_ly * math.sin(rot_rad)
    rotated_pad_ly = member_pad_lx * math.sin(rot_rad) + member_pad_ly * math.cos(rot_rad)
    ideal_pad_x = ideal_fp_cx + rotated_pad_lx
    ideal_pad_y = ideal_fp_cy + rotated_pad_ly
    
    return best_rot, ideal_pad_x, ideal_pad_y


def _choose_rotation_for_grid(
    mfp: list[Any],
    cx: float,
    cy: float,
    base_rot: float,
    connecting_pairs: list[tuple[float, float, float, float, float, float]],
) -> float:
    """Calculate the rotation that orients connecting pad(s) toward anchor pad(s).

    **Goal**: Orient the component so its connecting pad(s) face toward the
    anchor pad(s) they connect to, minimizing track length.

    **Algorithm**:
    1. For the primary connecting pad, calculate the direction from component
       position to its anchor pad target.
    2. Calculate which rotation (0°, 90°, 180°, or 270°) best aligns the
       pad's direction with the target direction.
    3. For components with multiple pads, use the first connecting pair.

    Args:
        mfp: Footprint S-expression node (unused, kept for compatibility).
        cx: Candidate X position (anchor-relative, mm).
        cy: Candidate Y position (anchor-relative, mm).
        base_rot: Component's current rotation (degrees, used if no connections).
        connecting_pairs: List of (lx, ly, ax, ay, aw, ah, mw, mh) tuples where
            lx,ly are member pad local coordinates, ax,ay are anchor pad
            coordinates. Empty for unconnected members.

    Returns:
        Rotation in degrees (one of 0, 90, 180, 270) that orients the
        connecting pad toward its anchor pad.
    """
    # If no connecting pads, use base rotation
    if not connecting_pairs:
        return 0

    # Use the first connecting pair (primary connection)
    lx, ly, ax, ay, _, _, _, _ = connecting_pairs[0]
    
    # Direction from component position to anchor pad
    target_dx = ax - cx
    target_dy = ay - cy
    
    # Angle from component center to anchor pad
    target_angle = math.degrees(math.atan2(target_dy, target_dx))
    
    # Current angle of the pad at 0° rotation
    pad_angle = math.degrees(math.atan2(ly, lx))
    
    # Rotation needed to align pad with target
    needed_rotation = target_angle - pad_angle
    
    # Round to nearest 90° and normalize to [0, 360)
    best_rot = round(needed_rotation / 90) * 90
    best_rot = best_rot % 360
    
    return best_rot


def _grid_layout(
    data: list[Any],
    anchor_ref: str,
    member_refs: list[str],
    gap_mm: float = 1.0,
    grid_radius_mm: float = 100.0,
) -> list[dict[str, Any]]:
    """Compute anchor-relative placement offsets using closest-first grid search.

    Algorithm
    ---------
    1. Pre-generate all KiCad-grid positions within *grid_radius_mm* of the
       anchor, sorted by Euclidean distance (closest first).
    2. For each member (connected-to-anchor members placed first, then
       unconnected), try candidate positions from closest outward.  At each
       position, choose the rotation that orients the component's long axis
       perpendicular to the nearest anchor edge (see ``_choose_rotation_for_grid``).
    3. Accept the first position where the component's courtyard (inflated by
       *gap_mm*) does not overlap the anchor courtyard or any already-placed
       member's courtyard.
    4. No cascade is required — only free slots are ever claimed.

    Args:
        data: Parsed PCB S-expression tree.
        anchor_ref: Reference of the group anchor (fixed at origin).
        member_refs: References of non-anchor group members to arrange.
        gap_mm: Minimum courtyard-to-courtyard gap in mm (default 1 mm).
        grid_radius_mm: Search radius around the anchor (default 100 mm).

    Returns:
        List of ``{reference, dx, dy, rotation, rationale}`` dicts with
        grid-snapped anchor-relative offsets.  A ``"warning"`` key is
        added when no clear position was found within the search radius.
    """
    # ── Anchor geometry ──────────────────────────────────────────────────
    anchor_fp = find_footprint(data, anchor_ref)
    anchor_wx, anchor_wy, anchor_rot = get_fp_at(anchor_fp)
    anchor_bbox = get_fp_courtyard_bbox(anchor_fp, 0.0, 0.0, anchor_rot)
    anchor_check = anchor_bbox

    if anchor_bbox:
        aw = anchor_bbox["max_x"] - anchor_bbox["min_x"]
        ah = anchor_bbox["max_y"] - anchor_bbox["min_y"]
        log.debug(f"Anchor {anchor_ref} courtyard: {aw:.1f} × {ah:.1f} mm")

    # Anchor signal pads in anchor-relative space (GND excluded).
    # Get local pads to extract dimensions, then transform to anchor-relative space
    anchor_local_pads = _get_fp_local_pads(anchor_fp)
    anchor_world_pads = _get_fp_pads_world(anchor_fp)
    anchor_pads_rel = []
    for lp, wp in zip(anchor_local_pads, anchor_world_pads):
        anchor_pads_rel.append({
            "x": wp["x"] - anchor_wx,
            "y": wp["y"] - anchor_wy,
            "net": wp["net"],
            "w": lp["pad_w"],
            "h": lp["pad_h"]
        })
    
    # Store anchor pads with dimensions: {net: [(x, y, w, h), ...]}
    anchor_net_pts: dict[str, list[tuple[float, float, float, float]]] = {}
    for _p in anchor_pads_rel:
        if _p["net"] and not _is_ground_net(_p["net"]):
            anchor_net_pts.setdefault(_p["net"], []).append(
                (_p["x"], _p["y"], _p["w"], _p["h"])
            )

    # ── Classify members ─────────────────────────────────────────────────
    connected: list[str] = []
    unconnected: list[str] = []
    for member_ref in member_refs:
        mfp = find_footprint(data, member_ref)
        mnets = {p["net"] for p in _get_fp_local_pads(mfp) if p["net"]}
        if any(net in anchor_net_pts for net in mnets if not _is_ground_net(net)):
            connected.append(member_ref)
        else:
            unconnected.append(member_ref)

    log.debug(
        f"Grid layout for anchor {anchor_ref}: {len(connected)} connected members, "
        f"{len(unconnected)} unconnected, gap={gap_mm}mm, radius={grid_radius_mm}mm"
    )
    log.debug(f"  Connected: {connected}")
    log.debug(f"  Unconnected: {unconnected}")

    # ── Per-member candidate cache ────────────────────────────────────────
    # Each member sorts grid positions by distance from its own anchor-pad
    # centroid, not from the anchor centre.  Components sharing the same
    # anchor pad therefore compete for the same nearby slots, clustering
    # naturally regardless of the order they are examined.
    # The cache avoids re-sorting when multiple members share identical pad
    # centroids (e.g. several decoupling caps on the same power pin).
    _cand_cache: dict[tuple[float, float], list[tuple[float, float]]] = {}

    def _candidates_for_pad(
        grid_cx: float, grid_cy: float, anchor_pad_cx: float, anchor_pad_cy: float,
        anchor_pad_w: float, anchor_pad_h: float,
        member_pad_lx: float, member_pad_ly: float,
        member_pad_w: float, member_pad_h: float,
        anchor_courtyard: dict[str, float] | None,
        best_rotation: float = 0.0
    ) -> list[tuple[float, float]]:
        """Generate grid of test points centered at ANCHOR PAD and sorted by pad-to-pad distance.
        
        Per user specification:
        1. Generate grid centered at target anchor pad
        2. Filter out points inside anchor courtyard (done in placement loop)
        3. Calculate track distance from member pad to anchor pad
        4. Sort by distance (closest first)
        
        Args:
            grid_cx, grid_cy: Ideal footprint center (for reference only)
            anchor_pad_cx, anchor_pad_cy: Anchor pad position (GRID CENTER)
            anchor_pad_w, anchor_pad_h: Anchor pad dimensions (mm)
            member_pad_lx, member_pad_ly: Member pad local offset
            member_pad_w, member_pad_h: Member pad dimensions
            anchor_courtyard: Anchor courtyard bbox (for cache key)
            best_rotation: Pre-calculated best rotation (degrees)
        """
        # Cache key based on anchor pad position and rotation
        courtyard_key = (
            round(anchor_courtyard["min_x"], 3) if anchor_courtyard else 0,
            round(anchor_courtyard["max_x"], 3) if anchor_courtyard else 0,
            round(anchor_courtyard["min_y"], 3) if anchor_courtyard else 0,
            round(anchor_courtyard["max_y"], 3) if anchor_courtyard else 0,
        )
        key = (round(anchor_pad_cx, 3), round(anchor_pad_cy, 3),
               round(member_pad_lx, 3), round(member_pad_ly, 3), round(best_rotation, 3), courtyard_key)
        
        if key not in _cand_cache:
            # Calculate rotated pad offset
            rot_rad = math.radians(best_rotation)
            rotated_pad_lx = member_pad_lx * math.cos(rot_rad) - member_pad_ly * math.sin(rot_rad)
            rotated_pad_ly = member_pad_lx * math.sin(rot_rad) + member_pad_ly * math.cos(rot_rad)
            
            # Step 1: Generate grid of TEST POINTS (member pad positions) centered at ANCHOR PAD
            steps = int(math.ceil(grid_radius_mm / _GRID_MM))
            candidates = []
            
            # Determine ideal perpendicular direction based on rotation
            # Rotation tells us where component BODY goes (from _choose_rotation_for_connection):
            # 0° → component body to RIGHT (+X)
            # 180° → component body to LEFT (-X)
            # 90° → component body to BOTTOM (+Y)
            # 270° → component body to TOP (-Y)
            rotation_normalized = best_rotation % 360
            if rotation_normalized == 0:
                # Component body to RIGHT
                ideal_perp_x, ideal_perp_y = 1.0, 0.0
            elif rotation_normalized == 180:
                # Component body to LEFT
                ideal_perp_x, ideal_perp_y = -1.0, 0.0
            elif rotation_normalized == 90:
                # Component body to BOTTOM
                ideal_perp_x, ideal_perp_y = 0.0, 1.0
            else:  # 270
                # Component body to TOP
                ideal_perp_x, ideal_perp_y = 0.0, -1.0
            
            for row in range(-steps, steps + 1):
                for col in range(-steps, steps + 1):
                    # Test point = member pad position (grid origin at ANCHOR PAD)
                    test_point_x = anchor_pad_cx + round(col * _GRID_MM, 9)
                    test_point_y = anchor_pad_cy + round(row * _GRID_MM, 9)
                    
                    # Step 3: Calculate track distance (test point to anchor pad)
                    dx = test_point_x - anchor_pad_cx
                    dy = test_point_y - anchor_pad_cy
                    dist_sq = dx * dx + dy * dy
                    
                    # Calculate footprint center from test point (pad position)
                    # footprint_center = pad_position - rotated_pad_offset
                    footprint_x = test_point_x - rotated_pad_lx
                    footprint_y = test_point_y - rotated_pad_ly
                    
                    # Apply angular penalty based on alignment with perpendicular direction
                    # Use cosine of angle between (anchor→grid) and ideal perpendicular direction
                    # multiplier = 1 - 0.8 * cos(angle)
                    # cos=1 (perfectly aligned) → multiplier=0.2 (favor strongly)
                    # cos=0 (90° off) → multiplier=1.0 (neutral)
                    # cos=-1 (opposite) → multiplier=1.8 (penalize)
                    multiplier = 1.0
                    if dist_sq > 0.001:  # Avoid division by zero at anchor position
                        dist = math.sqrt(dist_sq)
                        # Normalize direction from anchor to grid point
                        norm_dx = dx / dist
                        norm_dy = dy / dist
                        # Dot product gives cos(angle)
                        cos_angle = norm_dx * ideal_perp_x + norm_dy * ideal_perp_y
                        # Apply multiplier formula
                        multiplier = 1.0 - 0.8 * cos_angle
                    
                    effective_dist_sq = dist_sq * multiplier
                    
                    candidates.append((footprint_x, footprint_y, effective_dist_sq))
            
            # Step 4: Sort by effective distance (prioritizes preferred direction)
            candidates.sort(key=lambda c: c[2])
            _cand_cache[key] = [(c[0], c[1]) for c in candidates]
        
        return _cand_cache[key]

    # ── Running obstacle list: inflated bboxes of already-placed members ──
    # Stored as plain bbox dicts (already gap-inflated) for fast overlap test.
    placed_inflated: list[dict[str, float]] = []

    suggestions: list[dict[str, Any]] = []

    def _place_member(member_ref: str, rationale: str) -> None:
        """Attempt to place one member; append a suggestion entry."""
        mfp = find_footprint(data, member_ref)
        _, _, base_rot = get_fp_at(mfp)

        # Build connecting pairs (lx, ly, ax, ay, aw, ah, mw, mh) for anchor-net pads.
        # Includes both anchor and member pad dimensions for pad-edge to pad-edge distance.
        connecting_pairs: list[tuple[float, float, float, float, float, float, float, float]] = []
        shared_nets: set[str] = set()
        for mpad in _get_fp_local_pads(mfp):
            net = mpad["net"]
            if not net or _is_ground_net(net):
                continue
            apts = anchor_net_pts.get(net)
            if not apts:
                continue
            # Find closest anchor pad (by centroid distance)
            ax_best, ay_best, aw_best, ah_best = min(
                apts,
                key=lambda p, lx=mpad["lx"], ly=mpad["ly"]: math.hypot(lx - p[0], ly - p[1]),
            )
            # Store: (member_lx, member_ly, anchor_x, anchor_y, anchor_w, anchor_h, member_w, member_h)
            connecting_pairs.append(
                (mpad["lx"], mpad["ly"], ax_best, ay_best, aw_best, ah_best, mpad["pad_w"], mpad["pad_h"])
            )
            shared_nets.add(net)

        if shared_nets:
            rationale = "net(s): " + ", ".join(sorted(shared_nets)[:2])

        # Sort candidates by distance from the centroid of the connecting
        # anchor pads.  This pulls every member toward the anchor pad(s) it
        # shares a net with, grouping same-pad-connected components together
        # independent of the order they are examined.
        #
        # CRITICAL: We want the component's CONNECTING PAD (not footprint center)
        # to be near the anchor pad. So we offset the grid candidates by the
        # member pad's local position.
        if connecting_pairs:
            # Anchor pad target centroid
            anchor_pad_cx = sum(ax for _, _, ax, _, _, _, _, _ in connecting_pairs) / len(connecting_pairs)
            anchor_pad_cy = sum(ay for _, _, _, ay, _, _, _, _ in connecting_pairs) / len(connecting_pairs)
            # Anchor pad average dimensions
            anchor_pad_w = sum(aw for _, _, _, _, aw, _, _, _ in connecting_pairs) / len(connecting_pairs)
            anchor_pad_h = sum(ah for _, _, _, _, _, ah, _, _ in connecting_pairs) / len(connecting_pairs)
            # Member pad centroid in local coordinates
            member_pad_lx = sum(lx for lx, _, _, _, _, _, _, _ in connecting_pairs) / len(connecting_pairs)
            member_pad_ly = sum(ly for _, ly, _, _, _, _, _, _ in connecting_pairs) / len(connecting_pairs)
            # Member pad average dimensions
            member_pad_w = sum(mw for _, _, _, _, _, _, mw, _ in connecting_pairs) / len(connecting_pairs)
            member_pad_h = sum(mh for _, _, _, _, _, _, _, mh in connecting_pairs) / len(connecting_pairs)
            
            # Calculate best rotation and ideal position based on anchor edge
            best_rot, ideal_pad_x, ideal_pad_y = _choose_rotation_for_connection(
                member_pad_lx, member_pad_ly, anchor_pad_cx, anchor_pad_cy, anchor_bbox, mfp
            )
            
            # Calculate rotated pad offset
            rot_rad = math.radians(best_rot)
            rotated_pad_lx = member_pad_lx * math.cos(rot_rad) - member_pad_ly * math.sin(rot_rad)
            rotated_pad_ly = member_pad_lx * math.sin(rot_rad) + member_pad_ly * math.cos(rot_rad)
            
            # Grid center = ideal pad position - rotated pad offset
            grid_center_x = ideal_pad_x - rotated_pad_lx
            grid_center_y = ideal_pad_y - rotated_pad_ly
        else:
            anchor_pad_cx, anchor_pad_cy = 0.0, 0.0
            anchor_pad_w, anchor_pad_h = 0.0, 0.0
            member_pad_lx, member_pad_ly = 0.0, 0.0
            member_pad_w, member_pad_h = 0.0, 0.0
            grid_center_x, grid_center_y = 0.0, 0.0
            best_rot = base_rot
            ideal_pad_x, ideal_pad_y = 0.0, 0.0

        # Calculate ideal member pad world position for logging
        ideal_member_pad_world_x = ideal_pad_x
        ideal_member_pad_world_y = ideal_pad_y
        
        log.info(
            f"\n{'='*70}\n"
            f"PLACING {member_ref}\n"
            f"{'='*70}"
        )
        log.info(f"Connecting pairs: {len(connecting_pairs)}")
        for i, (mlx, mly, ax, ay, aw, ah, mw, mh) in enumerate(connecting_pairs):
            log.info(
                f"  Pair {i+1}: member pad local ({mlx:+.1f}, {mly:+.1f}) [{mw:.1f}×{mh:.1f}mm] "
                f"→ anchor pad WORLD ({ax:.1f}, {ay:.1f}) [{aw:.1f}×{ah:.1f}mm]"
            )
        log.info(
            f"\nCoordinate summary (all in WORLD coordinates, anchor-relative):\n"
            f"  Anchor pad centroid WORLD: ({anchor_pad_cx:.1f}, {anchor_pad_cy:.1f})\n"
            f"  Calculated best rotation: {best_rot}° (places component body OUTSIDE anchor courtyard)\n"
            f"  Ideal member pad WORLD: ({ideal_member_pad_world_x:.1f}, {ideal_member_pad_world_y:.1f}) [just outside anchor courtyard]\n"
            f"  Ideal footprint center WORLD: ({grid_center_x:.1f}, {grid_center_y:.1f}) [= ideal_pad - rotated_pad_offset]\n"
            f"  Member pad local: ({member_pad_lx:+.1f}, {member_pad_ly:+.1f}) at 0° rotation\n"
            f"  Base rotation: {base_rot}°\n"
            f"  Rationale: {rationale}"
        )

        found = False
        chosen_dx, chosen_dy, chosen_rot = 0.0, 0.0, base_rot
        tries = 0

        # Get member courtyard for logging
        test_bb = get_fp_courtyard_bbox(mfp, 0.0, 0.0, base_rot)
        member_bbox_at_origin = test_bb  # Save for retract algorithm
        if test_bb:
            cy_width = test_bb["max_x"] - test_bb["min_x"]
            cy_height = test_bb["max_y"] - test_bb["min_y"]
            log.info(f"Member courtyard: {cy_width:.1f}×{cy_height:.1f}mm")
        
        log.info("\nSearching for valid placement position...")
        
        # Add detailed logging for R4 to show grid search
        show_details = member_ref == "R4"
        
        # Pre-calculate ideal perpendicular direction for show_details
        if show_details:
            rot_rad = math.radians(best_rot)
            rotated_pad_lx_show = member_pad_lx * math.cos(rot_rad) - member_pad_ly * math.sin(rot_rad)
            rotated_pad_ly_show = member_pad_lx * math.sin(rot_rad) + member_pad_ly * math.cos(rot_rad)
            rotation_normalized = best_rot % 360
            if rotation_normalized == 0:
                ideal_perp_x_show, ideal_perp_y_show = 1.0, 0.0
            elif rotation_normalized == 180:
                ideal_perp_x_show, ideal_perp_y_show = -1.0, 0.0
            elif rotation_normalized == 90:
                ideal_perp_x_show, ideal_perp_y_show = 0.0, 1.0
            else:
                ideal_perp_x_show, ideal_perp_y_show = 0.0, -1.0
        
        for cx, cy in _candidates_for_pad(grid_center_x, grid_center_y, anchor_pad_cx, anchor_pad_cy,
                                          anchor_pad_w, anchor_pad_h, member_pad_lx, member_pad_ly,
                                          member_pad_w, member_pad_h, anchor_bbox, best_rot):
            
            tries += 1
            
            if show_details and tries <= 25:
                # Show grid position and angular alignment
                actual_pad_x = cx + rotated_pad_lx_show
                actual_pad_y = cy + rotated_pad_ly_show
                dx = actual_pad_x - anchor_pad_cx
                dy = actual_pad_y - anchor_pad_cy
                dist = math.hypot(dx, dy)
                # Calculate cosine angle
                cos_angle = 0.0
                if dist > 0.001:
                    norm_dx = dx / dist
                    norm_dy = dy / dist
                    cos_angle = norm_dx * ideal_perp_x_show + norm_dy * ideal_perp_y_show
                multiplier_show = 1.0 - 0.8 * cos_angle
                effective_dist_sq = dist * dist * multiplier_show
                log.info(f"  Try {tries}: fp({cx:.1f},{cy:.1f}) pad({actual_pad_x:.1f},{actual_pad_y:.1f}) "
                        f"dx={dx:.1f} dy={dy:.1f} dist={dist:.1f}mm cos={cos_angle:.3f} mult={multiplier_show:.3f} eff={effective_dist_sq:.2f}")
            # The anchor occupies (0, 0); never place a member there.
            if cx == 0.0 and cy == 0.0:
                if show_details and tries <= 25:
                    log.info(f"    → SKIP (anchor origin)")
                else:
                    log.info(f"  Try {tries}: WORLD ({cx:.1f}, {cy:.1f}) - SKIP (anchor origin)")
                continue

            # Use pre-calculated rotation (based on anchor edge and desired pad direction)
            rot = best_rot

            member_bb = get_fp_courtyard_bbox(mfp, 0.0, 0.0, rot)
            if member_bb is None:
                # No courtyard: accept position if anchor check also skipped.
                if anchor_check is None:
                    if show_details and tries <= 25:
                        log.info(f"    → ✓ ACCEPTED (no courtyard)")
                    else:
                        log.info(f"  Try {tries}: WORLD ({cx:.1f}, {cy:.1f}) rot={rot}° - ✓ ACCEPTED (no courtyard)")
                    chosen_dx, chosen_dy, chosen_rot = cx, cy, rot
                    found = True
                    break
                # Fall through to anchor check below using a zero-size box.
                member_bb = {"min_x": cx, "min_y": cy, "max_x": cx, "max_y": cy}

            # Translate member bbox to this candidate position.
            placed_bb = {
                "min_x": member_bb["min_x"] + cx,
                "min_y": member_bb["min_y"] + cy,
                "max_x": member_bb["max_x"] + cx,
                "max_y": member_bb["max_y"] + cy,
            }

            # Check against anchor (with gap).
            if anchor_check and _bboxes_overlap(placed_bb, anchor_check):
                if show_details and tries <= 25:
                    log.info(f"    → ✗ ANCHOR OVERLAP")
                else:
                    log.debug(f"  Try {tries}: WORLD ({cx:.1f}, {cy:.1f}) rot={rot}° - ✗ ANCHOR OVERLAP")
                continue  # Try next grid position instead of breaking

            # Check against already-placed members (already gap-inflated).
            overlapping_member = False
            for obs_idx, obs in enumerate(placed_inflated):
                if _bboxes_overlap(placed_bb, obs):
                    if show_details and tries <= 25:
                        log.info(f"    → ✗ MEMBER OVERLAP #{obs_idx+1}")
                    else:
                        log.debug(
                            f"  Try {tries}: WORLD ({cx:.1f}, {cy:.1f}) rot={rot}° - ✗ MEMBER OVERLAP #{obs_idx+1}"
                        )
                    overlapping_member = True
                    break
            if overlapping_member:
                continue

            # Success!
            # Calculate actual pad position using rotated offset
            rot_rad = math.radians(rot)
            rotated_pad_lx = member_pad_lx * math.cos(rot_rad) - member_pad_ly * math.sin(rot_rad)
            rotated_pad_ly = member_pad_lx * math.sin(rot_rad) + member_pad_ly * math.cos(rot_rad)
            actual_pad_x = cx + rotated_pad_lx
            actual_pad_y = cy + rotated_pad_ly
            pad_dist = math.hypot(actual_pad_x - anchor_pad_cx, actual_pad_y - anchor_pad_cy)
            if show_details:
                log.info(f"    → ✓ ACCEPTED")
            log.info(
                f"  Try {tries}: WORLD ({cx:.1f}, {cy:.1f}) rot={rot}° - ✓ ACCEPTED\n"
                f"    Footprint center WORLD: ({cx:.1f}, {cy:.1f})\n"
                f"    Courtyard WORLD: [{placed_bb['min_x']:.1f}, {placed_bb['max_x']:.1f}] × "
                f"[{placed_bb['min_y']:.1f}, {placed_bb['max_y']:.1f}]\n"
                f"    Member pad WORLD: ({actual_pad_x:.1f}, {actual_pad_y:.1f})\n"
                f"    Anchor pad WORLD: ({anchor_pad_cx:.1f}, {anchor_pad_cy:.1f})\n"
                f"    Pad-to-pad distance: {pad_dist:.1f}mm"
            )
            chosen_dx, chosen_dy, chosen_rot = cx, cy, rot
            found = True
            break

        entry: dict[str, Any] = {
            "reference": member_ref,
            "dx":        chosen_dx,
            "dy":        chosen_dy,
            "rotation":  chosen_rot,
            "rationale": rationale,
        }
        if not found:
            entry["warning"] = "no clear position within grid radius; position may overlap"
            log.warning(
                f"\n{'='*70}\n"
                f"✗ {member_ref}: NO CLEAR POSITION FOUND\n"
                f"  Tried {tries} positions\n"
                f"  Ideal grid center was ({grid_center_x:.1f}, {grid_center_y:.1f})\n"
                f"  All positions had collisions!\n"
                f"{'='*70}\n"
            )
        suggestions.append(entry)
        log.info(f"\n{'='*70}\n")

        # Record the placed bbox (gap-inflated) for subsequent members.
        if found:
            placed_bb_raw = get_fp_courtyard_bbox(mfp, 0.0, 0.0, chosen_rot)
            if placed_bb_raw:
                placed_inflated.append({
                    "min_x": placed_bb_raw["min_x"] + chosen_dx - gap_mm,
                    "min_y": placed_bb_raw["min_y"] + chosen_dy - gap_mm,
                    "max_x": placed_bb_raw["max_x"] + chosen_dx + gap_mm,
                    "max_y": placed_bb_raw["max_y"] + chosen_dy + gap_mm,
                })

    # Place connected members first (closest to anchor by connectivity).
    for ref in connected:
        _place_member(ref, "connected")

    # Then unconnected members.
    for ref in unconnected:
        _place_member(ref, "no direct anchor connection")

    return suggestions


# ---------------------------------------------------------------------------
# Layout rotation helper
# ---------------------------------------------------------------------------


def _rotate_layout(
    layout: list[dict[str, Any]],
    angle_deg: float,
) -> list[dict[str, Any]]:
    """Return a copy of *layout* with all positions rotated by *angle_deg*.

    Rotation is clockwise-positive (KiCad convention), around the origin
    (0, 0).  The ``rotation`` field of each entry is incremented by
    *angle_deg*.  The input list is not mutated.

    Args:
        layout: List of placement dicts with ``ref``, ``dx``, ``dy``,
            ``rotation`` keys.
        angle_deg: Clockwise rotation in degrees to apply.

    Returns:
        New list with rotated positions and updated ``rotation`` fields.
    """
    theta = math.radians(angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    result = []
    for entry in layout:
        new_entry = dict(entry)
        x, y = entry["dx"], entry["dy"]
        new_entry["dx"] = x * cos_t + y * sin_t
        new_entry["dy"] = -x * sin_t + y * cos_t
        new_entry["rotation"] = (entry["rotation"] + angle_deg) % 360.0
        result.append(new_entry)
    return result


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_pcb_group_tools(mcp: FastMCP) -> None:
    """Register PCB component group management and placement tools."""

    @mcp.tool()
    async def assign_to_group(
        pcb_path: str,
        references: list[str],
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Assign a list of footprints to a named placement group.

        The group name is stored as the ``placement_group`` property on
        each footprint, so it persists in the .kicad_pcb file.  If a
        footprint is already in a different group it is reassigned.
        Pass ``group_name=""`` to remove a footprint from any group.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            references: List of footprint reference designators.
            group_name: Name for the group (e.g. ``"power_supply"``).

        Returns:
            dict with assigned, not_found, group_name, backup_path.
        """
        data = load_pcb(pcb_path)
        assigned: list[str] = []
        not_found: list[str] = []

        for ref in references:
            try:
                fp = find_footprint(data, ref)
            except KeyError:
                not_found.append(ref)
                continue
            upsert_fp_property(fp, _GROUP_PROPERTY, group_name)
            assigned.append(ref)

        if not assigned:
            return {
                "error": "No matching footprints found.",
                "not_found": not_found,
            }

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "group_name": group_name,
            "assigned": assigned,
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def list_groups(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all placement groups defined on the PCB board.

        Returns one entry per unique ``placement_group`` property value,
        with the member count, anchor reference, and approximate bounding
        box for each group.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.

        Returns:
            dict with ``groups`` (list of group summaries) and
            ``ungrouped_count`` (footprints without a group assignment).
        """
        data = load_pcb(pcb_path)

        groups_map: dict[str, list[dict]] = {}
        ungrouped = 0

        for fp in _iter_footprints(data):
            ref = get_fp_property(fp, "Reference") or ""
            value = get_fp_property(fp, "Value") or ""
            group_name = get_fp_property(fp, _GROUP_PROPERTY) or ""
            x, y, rot = get_fp_at(fp)
            layer = get_fp_layer(fp) or ""
            pad_count = _fp_pad_count(fp)

            if not group_name:
                ungrouped += 1
                continue

            member = {
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "layer": layer,
                "pad_count": pad_count,
                "tier": _classify_footprint(ref, pad_count, value),
            }
            groups_map.setdefault(group_name, []).append(member)

        groups = []
        for gname, members in sorted(groups_map.items()):
            anchor = _find_anchor(members)
            xs = [m["x"] for m in members]
            ys = [m["y"] for m in members]
            groups.append({
                "group_name": gname,
                "member_count": len(members),
                "anchor_ref": anchor["reference"] if anchor else None,
                "members": [m["reference"] for m in members],
                "bbox": {
                    "min_x": round(min(xs), 3),
                    "min_y": round(min(ys), 3),
                    "max_x": round(max(xs), 3),
                    "max_y": round(max(ys), 3),
                },
            })

        return {
            "groups": groups,
            "group_count": len(groups),
            "ungrouped_count": ungrouped,
        }

    @mcp.tool()
    async def get_group(
        pcb_path: str,
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific placement group.

        Returns the full member list with positions, the identified anchor
        (highest-priority member), and the group's approximate bounding box.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            group_name: Name of the group to inspect.

        Returns:
            dict with group_name, anchor_ref, members (detailed), bbox.
        """
        data = load_pcb(pcb_path)
        members = _get_group_members(data, group_name)

        if not members:
            return {"error": f"Group '{group_name}' not found or is empty."}

        anchor = _find_anchor(members)
        xs = [m["x"] for m in members]
        ys = [m["y"] for m in members]

        return {
            "group_name": group_name,
            "anchor_ref": anchor["reference"] if anchor else None,
            "member_count": len(members),
            "members": members,
            "bbox": {
                "min_x": round(min(xs), 3),
                "min_y": round(min(ys), 3),
                "max_x": round(max(xs), 3),
                "max_y": round(max(ys), 3),
            },
        }

    @mcp.tool()
    async def score_group(
        pcb_path: str,
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Compute the intra-group placement quality for a named group.

        Calculates the Half-Perimeter Wirelength (HPWL) using only the pads
        of group members, for nets where at least two group members share the
        net.  Lower is better — it measures how efficiently the group's
        members are arranged relative to each other, independent of external
        connections.

        Also reports the mean distance between each member and the group
        centroid as a spread metric.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            group_name: Name of the group to score.

        Returns:
            dict with group_name, intra_hpwl_mm, mean_spread_mm,
            member_count, anchor_ref.
        """
        data = load_pcb(pcb_path)
        members = _get_group_members(data, group_name)

        if not members:
            return {"error": f"Group '{group_name}' not found or is empty."}

        refs = {m["reference"] for m in members}
        intra_hpwl = _compute_group_hpwl(data, refs)

        # Mean distance from each member to the group centroid
        cx = sum(m["x"] for m in members) / len(members)
        cy = sum(m["y"] for m in members) / len(members)
        mean_spread = sum(math.hypot(m["x"] - cx, m["y"] - cy) for m in members) / len(members)

        anchor = _find_anchor(members)
        return {
            "group_name": group_name,
            "intra_hpwl_mm": round(intra_hpwl, 2),
            "mean_spread_mm": round(mean_spread, 2),
            "member_count": len(members),
            "anchor_ref": anchor["reference"] if anchor else None,
        }

    @mcp.tool()
    async def place_component_group(
        pcb_path: str,
        group_name: str,
        gap_mm: float = 1.0,
        grid_radius_mm: float = 100.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Place all members of a group using the two-phase grid layout.

        **Phase 1** arranges non-anchor members on the KiCad snap grid around
        the anchor, placing each component toward the anchor pads it shares a
        net with and rotating it so its long axis is perpendicular to the
        nearest anchor edge.

        **Phase 2** intelligently finds a board position for the group:
        1. Starts from the anchor's current position
        2. If the current position is clear, uses it
        3. If there are overlaps, calculates which direction minimizes overlap
        4. Searches in that direction for a non-overlapping position
        5. If no clear position found nearby, falls back to full board scan
           to find the position that minimizes total wirelength (HPWL)

        This approach keeps the group near its current location when possible,
        moving only as far as needed to resolve overlaps.

        The anchor's current rotation is preserved.  Use ``rotate_group``
        after placement to reorient the whole group.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            group_name: Name of the group to place.
            gap_mm: Minimum courtyard-to-courtyard gap between group members
                (default 1.0 mm).  Increase for thermal isolation; reduce to
                0.5 mm for very dense layouts.  Only affects intra-group
                spacing — the board scan checks courtyards at zero clearance.
            grid_radius_mm: Search radius for the grid layout in mm
                (default 100.0 mm).

        Returns:
            group_name, anchor_ref, anchor_position, placed_count, placed
            (list of {reference, x, y, rotation, rationale}), intra_hpwl_mm,
            mean_spread_mm, found_clear_position, backup_path.
        """
        data = load_pcb(pcb_path)
        members = _get_group_members(data, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_to_group to add footprints first."
                )
            }

        anchor = _find_anchor(members)
        anchor_ref = anchor["reference"]

        # Preserve current anchor rotation and position.
        anchor_fp = find_footprint(data, anchor_ref)
        anchor_current_x, anchor_current_y, anchor_rot = get_fp_at(anchor_fp)

        # Phase 1: compute anchor-relative layout.
        member_refs = [m["reference"] for m in members if m["reference"] != anchor_ref]
        relative_layout = _grid_layout(
            data, anchor_ref, member_refs, gap_mm=gap_mm, grid_radius_mm=grid_radius_mm
        )

        # Build full layout including anchor at origin.
        group_refs = {m["reference"] for m in members}
        full_layout = [{"ref": anchor_ref, "dx": 0.0, "dy": 0.0, "rotation": anchor_rot}]
        full_layout += [
            {"ref": s["reference"], "dx": s["dx"], "dy": s["dy"], "rotation": s["rotation"]}
            for s in relative_layout
        ]

        # Phase 2: smart search starting from current anchor position.
        anchor_x, anchor_y, found_clear, _ = _find_group_board_position(
            data, group_refs, full_layout, anchor_current_pos=(anchor_current_x, anchor_current_y)
        )

        # Apply positions (anchor + members at anchor + relative offset).
        set_fp_at(anchor_fp, anchor_x, anchor_y, anchor_rot)
        for s in relative_layout:
            fp = find_footprint(data, s["reference"])
            world_x = round(anchor_x + s["dx"], 9)
            world_y = round(anchor_y + s["dy"], 9)
            set_fp_at(fp, world_x, world_y, s["rotation"])

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        # Quality metrics on the committed layout.
        intra_hpwl = _compute_group_hpwl(data, group_refs)
        all_x = [anchor_x] + [round(anchor_x + s["dx"], 9) for s in relative_layout]
        all_y = [anchor_y] + [round(anchor_y + s["dy"], 9) for s in relative_layout]
        cx_mean = sum(all_x) / len(all_x)
        cy_mean = sum(all_y) / len(all_y)
        mean_spread = (
            sum(math.hypot(x - cx_mean, y - cy_mean) for x, y in zip(all_x, all_y))
            / len(all_x)
        )

        placed = [
            {"reference": anchor_ref, "x": anchor_x, "y": anchor_y,
             "rotation": anchor_rot, "rationale": "anchor"},
        ]
        placed += [
            {"reference": s["reference"],
             "x": round(anchor_x + s["dx"], 9),
             "y": round(anchor_y + s["dy"], 9),
             "rotation": s["rotation"],
             "rationale": s["rationale"]}
            for s in relative_layout
        ]

        return {
            "group_name": group_name,
            "anchor_ref": anchor_ref,
            "anchor_position": {"x": anchor_x, "y": anchor_y},
            "placed_count": len(placed),
            "placed": placed,
            "intra_hpwl_mm": round(intra_hpwl, 2),
            "mean_spread_mm": round(mean_spread, 2),
            "found_clear_position": found_clear,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def move_group(
        pcb_path: str,
        group_name: str,
        anchor_x: float,
        anchor_y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Translate a placed group as a rigid unit to a new anchor position.

        All group members are translated by the same delta that moves the
        anchor from its current position to (anchor_x, anchor_y).  The
        relative layout of members within the group is preserved exactly.

        Inter-group collisions are checked before writing.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            group_name: Name of the group to move.
            anchor_x: Target X position for the anchor in mm.
            anchor_y: Target Y position for the anchor in mm.

        Returns:
            group_name, anchor_ref, anchor_position, moved_count,
            inter_group_collisions (if any), backup_path.
        """
        data = load_pcb(pcb_path)
        members = _get_group_members(data, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_to_group to add footprints first."
                )
            }

        anchor = _find_anchor(members)
        delta_x = float(anchor_x) - anchor["x"]
        delta_y = float(anchor_y) - anchor["y"]

        proposals = [
            (m["reference"], m["x"] + delta_x, m["y"] + delta_y, m["rotation"])
            for m in members
        ]

        collisions = find_collisions(data, proposals, check_within_group=False)
        if collisions:
            return {
                "error": (
                    "Inter-group collision detected; group was NOT moved. "
                    "Use move_group with a different target position."
                ),
                "inter_group_collisions": [
                    {"ref": c["ref"], "overlapping_with": c["overlapping_with"]}
                    for c in collisions
                ],
                "group_name": group_name,
            }

        for ref, x, y, rot in proposals:
            fp = find_footprint(data, ref)
            set_fp_at(fp, x, y, rot)

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "group_name": group_name,
            "anchor_ref": anchor["reference"],
            "anchor_position": {"x": float(anchor_x), "y": float(anchor_y)},
            "moved_count": len(members),
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def rotate_group(
        pcb_path: str,
        group_name: str,
        rotation_delta: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Rotate a placed group as a rigid unit around its anchor.

        All member positions are rotated around the current anchor position
        using the rigid-body rotation formula.  Each member's own rotation
        is also incremented by rotation_delta so parts keep their board
        orientation.

        Positive angles rotate clockwise (KiCad convention).
        Inter-group collisions are checked before writing.

        A .kicad_pcb.bak backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            group_name: Name of the group to rotate.
            rotation_delta: Rotation in degrees (clockwise positive) to
                apply to the group.  E.g. 90 rotates the whole group
                90° clockwise around the anchor.

        Returns:
            group_name, anchor_ref, rotation_delta, rotated_count,
            inter_group_collisions (if any), backup_path.
        """
        data = load_pcb(pcb_path)
        members = _get_group_members(data, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_to_group to add footprints first."
                )
            }

        anchor = _find_anchor(members)
        anchor_ref = anchor["reference"]
        ax, ay = anchor["x"], anchor["y"]

        # KiCad PCB coordinate system: +X right, +Y DOWN, rotation CW-positive
        # In +Y down coords, the standard rotation matrix gives CW rotation
        # when using positive angles (no negation needed)
        theta = math.radians(rotation_delta)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)

        proposals = []
        for m in members:
            # Component orientations: SUBTRACT rotation_delta to maintain relative orientation
            #
            # QUIRK: Footprint rotation vs PCB position rotation have opposite conventions:
            # - PCB positions use +Y DOWN (screen coords): rotating +90° CW moves RIGHT → DOWN
            # - Footprint rotation appears to use +Y UP convention: rotating +90° turns RIGHT → UP
            #
            # When group rotates 90° CW (positions move RIGHT → DOWN in PCB coords),
            # we must SUBTRACT 90° from component orientations to keep them aligned with
            # the group's local coordinate frame (e.g., USB-C pads continue facing toward
            # resistors, diode marks maintain polarity direction relative to circuit).
            #
            # Example: Component at 0° pointing RIGHT, group rotates 90° CW
            #   - Position moves from RIGHT to BELOW anchor (correct)
            #   - Orientation: 0° - 90° = 270° = component now points UP
            #   - Result: component still points toward anchor (relative orientation preserved)
            new_rot = (m["rotation"] - rotation_delta) % 360.0
            if m["reference"] == anchor_ref:
                proposals.append((anchor_ref, ax, ay, new_rot))
            else:
                dx = m["x"] - ax
                dy = m["y"] - ay
                new_x = round(ax + dx * cos_t - dy * sin_t, 9)
                new_y = round(ay + dx * sin_t + dy * cos_t, 9)
                proposals.append((m["reference"], new_x, new_y, new_rot))

        collisions = find_collisions(data, proposals, check_within_group=False)
        if collisions:
            return {
                "error": (
                    "Inter-group collision detected; group was NOT rotated. "
                    "Try a different rotation_delta or move the group first."
                ),
                "inter_group_collisions": [
                    {"ref": c["ref"], "overlapping_with": c["overlapping_with"]}
                    for c in collisions
                ],
                "group_name": group_name,
            }

        for ref, x, y, rot in proposals:
            fp = find_footprint(data, ref)
            set_fp_at(fp, x, y, rot)

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "group_name": group_name,
            "anchor_ref": anchor_ref,
            "rotation_delta": rotation_delta,
            "rotated_count": len(members),
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }
