"""
Schematic symbol group management and batch placement tools.

Groups are stored as a ``placement_group`` property on each symbol,
so group assignments persist in the .kicad_sch file and are visible in
the KiCad GUI's symbol properties dialog.

Workflow:
  1. ``assign_symbols_to_group``    — tag symbols with a group name (batch).
  2. ``list_symbol_groups``         — inspect all groups on the schematic.
  3. ``get_symbol_group``           — details for one group (members, bbox, anchor).
  4. ``score_symbol_group``         — intra-group proximity quality metric.
  5. ``place_symbol_group``         — two-phase automatic placement: arranges
                                      members in a grid around the anchor
                                      (Phase 1) then finds the first clear
                                      sheet position via spiral scan (Phase 2)
                                      and commits.
  6. ``move_symbol_group``          — translate a placed group as a rigid unit.
  7. ``rotate_symbol_group``        — rotate a placed group around its anchor.

Schematic coordinate convention: mm, +X right, +Y down; file rotation is
CCW-positive on screen (KiCad convention, 0=right, 90=up).
"""

import logging
import math
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

logger = logging.getLogger(__name__)

from kcaa.tools.symbol_edit_tools import _find_property_by_name
from kcaa.utils.netlist_parser import extract_netlist
from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic

log = logging.getLogger(__name__)

# KiCad symbol property key used to store the group assignment.
_GROUP_PROPERTY = "placement_group"

# ---------------------------------------------------------------------------
# Property helpers
# ---------------------------------------------------------------------------


def _get_sym_property(sym: Any, name: str) -> str | None:
    """Return the value of a named property on a symbol, or None."""
    prop = _find_property_by_name(sym, name)
    if prop is not None:
        try:
            val = prop.value
            return str(val) if val is not None else None
        except AttributeError:
            return None
    return None


def _set_sym_property(sym: Any, name: str, value: str) -> None:
    """Set a property on a symbol, creating it if absent.

    If the property does not exist it is cloned from the ``Value`` property
    to get the correct structure (at, effects).  Non-standard properties
    (anything other than Reference / Value) are hidden by default.
    """
    existing = _find_property_by_name(sym, name)
    if existing is not None:
        existing.value = value
        return

    # Clone the Value property to get correct structure.
    try:
        new_prop = sym.property.Value.clone()
        new_prop.name = name
        new_prop.value = value
    except (AttributeError, RuntimeError, TypeError) as exc:
        logger.debug("set property %r failed: %s", name, exc)
        return

    # Non-standard properties are hidden by default in KiCad.
    if name not in ("Reference", "Value"):
        raw_tree = new_prop._pv._tree
        for child in raw_tree:
            if (
                isinstance(child, list)
                and len(child) >= 1
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "effects"
            ):
                child.append([sexpdata.Symbol("hide"), sexpdata.Symbol("yes")])
                break


def _delete_sym_property(sym: Any, name: str) -> bool:
    """Delete a property from a symbol.  Returns True if deleted.

    Raises if the underlying kipy delete operation fails, matching the
    behaviour of the same ``prop._pv.delete()`` call in
    :func:`symbol_edit_tools.delete_symbol_property`.
    """
    prop = _find_property_by_name(sym, name)
    if prop is not None:
        try:
            prop._pv.delete()
            return True
        except (AttributeError, RuntimeError, TypeError) as exc:
            logger.debug("delete property %r failed: %s", name, exc)
    return False


# ---------------------------------------------------------------------------
# Symbol iteration and basic info
# ---------------------------------------------------------------------------


def _iter_symbols(sch: Any):
    """Yield each symbol object from the parsed schematic."""
    try:
        yield from sch.symbol
    except AttributeError:
        pass


def _get_sym_pin_count(sym: Any) -> int:
    """Return the number of pins on a symbol."""
    try:
        pins = sym.pins
        return len(pins) if pins is not None else 0
    except AttributeError:
        return 0


def _get_sym_at(sym: Any) -> tuple[float, float, float]:
    """Return (x, y, rotation) of a symbol, or (0, 0, 0) on failure."""
    try:
        at_val = sym.at.value
        return (float(at_val[0]), float(at_val[1]), float(at_val[2]) if len(at_val) > 2 else 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return (0.0, 0.0, 0.0)


def _get_sym_body_bbox(sym: Any, schematic_path: str) -> dict[str, float] | None:
    """Return the body_bbox dict for a symbol's reference from the netlist.

    Returns None if the bbox cannot be determined.
    """
    try:
        ref = _get_sym_property(sym, "Reference")
    except Exception:
        return None
    if not ref:
        return None
    netlist = extract_netlist(schematic_path)
    comp = (netlist.get("components") or {}).get(ref)
    if comp:
        return comp.get("body_bbox")
    return None


# ---------------------------------------------------------------------------
# Group member helpers
# ---------------------------------------------------------------------------


def _get_group_members(sch: Any, schematic_path: str, group_name: str) -> list[dict[str, Any]]:
    """Return detailed info dicts for all symbols in *group_name*."""
    members: list[dict[str, Any]] = []

    for sym in _iter_symbols(sch):
        group = _get_sym_property(sym, _GROUP_PROPERTY)
        if group != group_name:
            continue
        ref = _get_sym_property(sym, "Reference") or ""
        if not ref:
            continue
        value = _get_sym_property(sym, "Value") or ""
        x, y, rot = _get_sym_at(sym)
        pin_count = _get_sym_pin_count(sym)
        body_bbox = _get_sym_body_bbox(sym, schematic_path)

        members.append(
            {
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "rotation": rot,
                "pin_count": pin_count,
                "body_bbox": body_bbox,
            }
        )
    return members


def _find_anchor(members: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the member with the highest pin count as the anchor.

    Ties are broken by reference string sort order (stable, deterministic).
    Returns ``None`` if *members* is empty.
    """
    if not members:
        return None
    return max(members, key=lambda m: (m["pin_count"], m["reference"]))


def _compute_group_union_bbox(
    members: list[dict[str, Any]], relative_layout: list[dict[str, Any]] | None = None
) -> dict[str, float] | None:
    """Compute the union body_bbox of all group members.

    If *relative_layout* is provided (list of {reference, dx, dy} relative
    to anchor), the bbox is computed in the group-local coordinate system.
    Otherwise uses each member's current absolute position.

    Returns a dict {min_x, min_y, max_x, max_y} or None if no bbox data.
    """
    xs: list[float] = []
    ys: list[float] = []

    if relative_layout is not None:
        layout_map = {s["reference"]: s for s in relative_layout}
        for m in members:
            bb = m.get("body_bbox")
            if not bb:
                continue
            ref = m["reference"]
            dx = layout_map.get(ref, {}).get("dx", 0.0)
            dy = layout_map.get(ref, {}).get("dy", 0.0)
            xs.extend([float(bb["min_x"]) + dx, float(bb["max_x"]) + dx])
            ys.extend([float(bb["min_y"]) + dy, float(bb["max_y"]) + dy])
    else:
        for m in members:
            bb = m.get("body_bbox")
            if not bb:
                continue
            xs.extend([float(bb["min_x"]), float(bb["max_x"])])
            ys.extend([float(bb["min_y"]), float(bb["max_y"])])

    if not xs or not ys:
        return None
    return {
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
    }


# ---------------------------------------------------------------------------
# Grid layout (Phase 1)
# ---------------------------------------------------------------------------

_GRID_MM: float = 1.27


def _snap_to_grid(v: float) -> float:
    """Snap a coordinate to the nearest _GRID_MM position."""
    return round(round(v / _GRID_MM) * _GRID_MM, 9)


def _grid_arrange_relative(
    anchor: dict[str, Any],
    non_anchor_members: list[dict[str, Any]],
    gap_mm: float = 2.54,
) -> list[dict[str, Any]]:
    """Place non-anchor members in a grid around the anchor.

    Members are sorted by pin count (descending) and placed in alternating
    columns to the right (+X) and rows below (+Y) the anchor.  Each member
    is auto-rotated 90° if its body bbox is wider than tall, making the
    layout more compact.  All positions are snapped to the 1.27 mm grid.

    Coordinates returned are relative to the anchor's position (not its
    bbox origin).  The caller adds these to the anchor's world position
    to get world coordinates.

    Args:
        anchor: Anchor member dict (contains body_bbox, x, y).
        non_anchor_members: List of member dicts to place.
        gap_mm: Minimum edge-to-edge gap between bboxes (default 2.54 mm).

    Returns:
        List of {reference, dx, dy, rotation, rationale} dicts,
        with coordinates relative to the anchor's position.
    """
    anchor_bb = anchor.get("body_bbox")
    ax = anchor.get("x", 0.0)
    ay = anchor.get("y", 0.0)

    if not anchor_bb:
        # Fallback: place members 10 mm apart without bbox guidance
        result: list[dict[str, Any]] = []
        for i, m in enumerate(sorted(non_anchor_members, key=lambda x: -x["pin_count"])):
            col = i % 3
            row = i // 3
            result.append(
                {
                    "reference": m["reference"],
                    "dx": _snap_to_grid((col + 1) * 10.0),
                    "dy": _snap_to_grid(row * 10.0),
                    "rotation": 0,
                    "rationale": f"fallback grid col={col} row={row} (no anchor bbox)",
                }
            )
        return result

    # Anchor bbox relative to anchor position
    rel_min_x = float(anchor_bb["min_x"]) - ax
    rel_min_y = float(anchor_bb["min_y"]) - ay
    rel_max_x = float(anchor_bb["max_x"]) - ax
    rel_max_y = float(anchor_bb["max_y"]) - ay

    # Sort non-anchor members by pin count descending
    sorted_members = sorted(non_anchor_members, key=lambda m: -m["pin_count"])

    # Cursors: start just outside the anchor's bbox edges
    cur_right_x = rel_max_x + gap_mm  # right edge of right-column placement area
    cur_below_y = rel_max_y + gap_mm  # bottom edge of bottom-row placement area

    result = []
    for i, m in enumerate(sorted_members):
        bb = m.get("body_bbox")
        if bb:
            mw = float(bb["max_x"]) - float(bb["min_x"])
            mh = float(bb["max_y"]) - float(bb["min_y"])
            # The offset from the member's position to its body_bbox min corner
            m_off_x = float(bb["min_x"]) - m.get("x", 0.0)
            m_off_y = float(bb["min_y"]) - m.get("y", 0.0)
        else:
            mw = 10.0
            mh = 10.0
            m_off_x = -5.0
            m_off_y = -5.0

        # Auto-rotate if wider than tall (for compactness)
        rot = 0
        if mw > mh and mw > 1.0:
            mw, mh = mh, mw
            # swap offsets when rotated
            m_off_x, m_off_y = -m_off_y, m_off_x
            rot = 90

        if i % 2 == 0:
            # Right column: place so that bbox left edge = cur_right_x,
            # and bbox top edge aligned with anchor's bbox top.
            # member position dx = bbox_left - m_off_x
            # bbox_left = cur_right_x, so dx = cur_right_x - m_off_x
            dx = _snap_to_grid(cur_right_x - m_off_x)
            dy = _snap_to_grid(rel_min_y - m_off_y)
            rationale = f"right column (pos {i // 2})"
            cur_right_x += mw + gap_mm
        else:
            # Bottom row: place so that bbox top edge = cur_below_y,
            # and bbox left edge aligned with anchor's bbox left.
            dx = _snap_to_grid(rel_min_x - m_off_x)
            dy = _snap_to_grid(cur_below_y - m_off_y)
            rationale = f"bottom row (pos {i // 2})"
            cur_below_y += mh + gap_mm

        result.append(
            {
                "reference": m["reference"],
                "dx": dx,
                "dy": dy,
                "rotation": rot,
                "rationale": rationale,
            }
        )

    return result


# ---------------------------------------------------------------------------
# Proximity scoring
# ---------------------------------------------------------------------------


def _compute_proximity_score(members: list[dict[str, Any]]) -> dict[str, float]:
    """Compute proximity quality metrics for a group.

    Returns:
        dict with mean_nn_mm (mean nearest-neighbor distance, lower is better)
        and mean_spread_mm (mean distance to centroid).
    """
    if len(members) < 2:
        return {"mean_nn_mm": 0.0, "mean_spread_mm": 0.0}

    # Centroid
    cx = sum(m["x"] for m in members) / len(members)
    cy = sum(m["y"] for m in members) / len(members)

    # Nearest-neighbor distances
    nn_dists: list[float] = []
    for i, mi in enumerate(members):
        best = float("inf")
        for j, mj in enumerate(members):
            if i == j:
                continue
            d = math.hypot(mi["x"] - mj["x"], mi["y"] - mj["y"])
            if d < best:
                best = d
        if best < float("inf"):
            nn_dists.append(best)

    mean_nn = sum(nn_dists) / len(nn_dists) if nn_dists else 0.0
    mean_spread = sum(math.hypot(m["x"] - cx, m["y"] - cy) for m in members) / len(members)

    return {"mean_nn_mm": round(mean_nn, 2), "mean_spread_mm": round(mean_spread, 2)}


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_schematic_group_tools(mcp: FastMCP) -> None:
    """Register schematic symbol group management tools."""

    @mcp.tool()
    async def assign_symbols_to_group(
        schematic_path: str,
        references: list[str],
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Assign a list of symbols to a named placement group.

        The group name is stored as the ``placement_group`` property on
        each symbol, so it persists in the .kicad_sch file.  If a
        symbol is already in a different group it is reassigned.
        Pass ``group_name=""`` to remove a symbol from any group.

        A .kicad_sch.bak backup is created before writing.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            references: List of symbol reference designators.
            group_name: Name for the group (e.g. ``"power_supply"``).

        Returns:
            dict with assigned, not_found, group_name, backup_path.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        assigned: list[str] = []
        not_found: list[str] = []

        for ref in references:
            found = False
            for sym in _iter_symbols(sch):
                if _get_sym_property(sym, "Reference") == ref:
                    if group_name:
                        _set_sym_property(sym, _GROUP_PROPERTY, group_name)
                    else:
                        _delete_sym_property(sym, _GROUP_PROPERTY)
                    assigned.append(ref)
                    found = True
                    break
            if not found:
                not_found.append(ref)

        if not assigned:
            return {
                "error": "No matching symbols found.",
                "not_found": not_found,
            }

        try:
            backup_path = save_schematic(schematic_path, sch)
        except OSError as exc:
            return {"error": f"Failed to write schematic: {exc}"}

        return {
            "group_name": group_name or "(unassigned)",
            "assigned": assigned,
            "not_found": not_found,
            "backup_path": backup_path,
            "schematic_path": schematic_path,
        }

    @mcp.tool()
    async def list_symbol_groups(
        schematic_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all placement groups defined on the schematic.

        Returns one entry per unique ``placement_group`` property value,
        with the member count, anchor reference, and approximate bounding
        box for each group.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.

        Returns:
            dict with ``groups`` (list of group summaries) and
            ``ungrouped_count`` (symbols without a group assignment).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        groups_map: dict[str, list[dict]] = {}
        ungrouped = 0

        for sym in _iter_symbols(sch):
            ref = _get_sym_property(sym, "Reference") or ""
            if not ref:
                continue
            value = _get_sym_property(sym, "Value") or ""
            group_name = _get_sym_property(sym, _GROUP_PROPERTY) or ""
            x, y, rot = _get_sym_at(sym)
            pin_count = _get_sym_pin_count(sym)

            if not group_name:
                ungrouped += 1
                continue

            member = {
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "pin_count": pin_count,
            }
            groups_map.setdefault(group_name, []).append(member)

        groups = []
        for gname, members in sorted(groups_map.items()):
            anchor = _find_anchor(members)
            xs = [m["x"] for m in members]
            ys = [m["y"] for m in members]
            groups.append(
                {
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
                }
            )

        return {
            "groups": groups,
            "group_count": len(groups),
            "ungrouped_count": ungrouped,
        }

    @mcp.tool()
    async def get_symbol_group(
        schematic_path: str,
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific symbol placement group.

        Returns the full member list with positions, rotations, pin counts,
        body bboxes, the identified anchor (highest pin-count member), and
        the group's approximate bounding box.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            group_name: Name of the group to inspect.

        Returns:
            dict with group_name, anchor_ref, members (detailed), bbox.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        members = _get_group_members(sch, schematic_path, group_name)

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
    async def score_symbol_group(
        schematic_path: str,
        group_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Score the intra-group placement quality for a named group.

        Computes two proximity metrics:
          - **mean_nn_mm**: mean nearest-neighbor distance among group members.
            Lower values = more compact arrangement.
          - **mean_spread_mm**: mean distance from each member to the group
            centroid. Measures how dispersed the group is.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            group_name: Name of the group to score.

        Returns:
            dict with group_name, mean_nn_mm, mean_spread_mm,
            member_count, anchor_ref.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        members = _get_group_members(sch, schematic_path, group_name)

        if not members:
            return {"error": f"Group '{group_name}' not found or is empty."}

        scores = _compute_proximity_score(members)
        anchor = _find_anchor(members)

        return {
            "group_name": group_name,
            "mean_nn_mm": scores["mean_nn_mm"],
            "mean_spread_mm": scores["mean_spread_mm"],
            "member_count": len(members),
            "anchor_ref": anchor["reference"] if anchor else None,
        }

    @mcp.tool()
    async def place_symbol_group(
        schematic_path: str,
        group_name: str,
        gap_mm: float = 2.54,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Place all members of a group using two-phase grid layout.

        **Phase 1** arranges non-anchor members in a grid around the anchor:
        members are sorted by pin count and placed in alternating columns
        (right of anchor) and rows (below anchor) with edge-to-edge gap
        spacing.  Members wider than tall are auto-rotated 90°.

        **Phase 2** intelligently positions the group on the sheet:
        1. Starts from the anchor's current position
        2. If the group's union bbox at that position is clear, uses it
        3. If there are overlaps, searches for the nearest free area
        4. Translates the entire group to the found position

        The anchor's current rotation is preserved.  Use
        ``rotate_symbol_group`` after placement to reorient the whole group.

        A .kicad_sch.bak backup is created before writing.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            group_name: Name of the group to place.
            gap_mm: Minimum edge-to-edge gap between group members
                (default 2.54 mm).

        Returns:
            group_name, anchor_ref, anchor_position, placed_count, placed
            (list of {reference, x, y, rotation, rationale}),
            mean_nn_mm, mean_spread_mm, found_clear_position, backup_path.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        from kcaa.tools.placement_helpers import _find_free_area_impl
        from kcaa.tools.sheet_tools import _has_position_conflict

        members = _get_group_members(sch, schematic_path, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_symbols_to_group to add symbols first."
                )
            }

        anchor = _find_anchor(members)
        anchor_ref = anchor["reference"]
        anchor_x, anchor_y, anchor_rot = anchor["x"], anchor["y"], anchor["rotation"]

        # Phase 1: compute anchor-relative layout
        non_anchor = [m for m in members if m["reference"] != anchor_ref]
        relative_layout = _grid_arrange_relative(anchor, non_anchor, gap_mm=gap_mm)

        # Phase 2: find clear sheet position
        full_layout = [{"reference": anchor_ref, "dx": 0.0, "dy": 0.0}]
        full_layout += [
            {"reference": s["reference"], "dx": s["dx"], "dy": s["dy"]} for s in relative_layout
        ]
        union_bb = _compute_group_union_bbox(members, full_layout)
        found_clear = False
        # Always snap anchor to grid — downstream tools (rotate/move)
        # also snap, and mismatches cause spurious position drift.
        anchor_x = _snap_to_grid(anchor_x)
        anchor_y = _snap_to_grid(anchor_y)

        if union_bb:
            bb_w = union_bb["max_x"] - union_bb["min_x"]
            bb_h = union_bb["max_y"] - union_bb["min_y"]
            bb_x = anchor_x + union_bb["min_x"]
            bb_y = anchor_y + union_bb["min_y"]

            has_conflict = _has_position_conflict(schematic_path, bb_x, bb_y, bb_w, bb_h)
            if not has_conflict:
                found_clear = True
            else:
                free = _find_free_area_impl(
                    schematic_path=schematic_path,
                    width=bb_w,
                    height=bb_h,
                    prefer_near={"x": anchor_x, "y": anchor_y},
                    max_candidates=1,
                )
                cand = (free.get("candidates") or [{}])[0]
                origin = cand.get("origin")
                if origin is not None:
                    new_anchor_x = _snap_to_grid(float(origin["x"]) - union_bb["min_x"])
                    new_anchor_y = _snap_to_grid(float(origin["y"]) - union_bb["min_y"])
                    anchor_x, anchor_y = new_anchor_x, new_anchor_y
                    found_clear = True
        else:
            found_clear = True

        # Apply positions (use assignment, not in-place, to persist through sch.write)
        for sym in _iter_symbols(sch):
            ref = _get_sym_property(sym, "Reference") or ""
            if ref == anchor_ref:
                sym.at.value = [anchor_x, anchor_y, sym.at.value[2]]
            else:
                layout_entry = next((s for s in relative_layout if s["reference"] == ref), None)
                if layout_entry is None:
                    continue
                world_x = _snap_to_grid(anchor_x + layout_entry["dx"])
                world_y = _snap_to_grid(anchor_y + layout_entry["dy"])
                new_rot = layout_entry.get("rotation", 0) or 0
                sym.at.value = [
                    world_x,
                    world_y,
                    new_rot if new_rot else sym.at.value[2],
                ]

        try:
            backup_path = save_schematic(schematic_path, sch)
        except OSError as exc:
            return {"error": f"Failed to write schematic: {exc}"}

        # Re-read for quality metrics
        sch2 = safe_schematic(schematic_path)
        final_members = _get_group_members(sch2, schematic_path, group_name)
        scores = _compute_proximity_score(final_members)

        placed = [
            {
                "reference": anchor_ref,
                "x": anchor_x,
                "y": anchor_y,
                "rotation": anchor_rot,
                "rationale": "anchor",
            },
        ]
        placed += [
            {
                "reference": s["reference"],
                "x": _snap_to_grid(anchor_x + s["dx"]),
                "y": _snap_to_grid(anchor_y + s["dy"]),
                "rotation": s["rotation"],
                "rationale": s["rationale"],
            }
            for s in relative_layout
        ]

        return {
            "group_name": group_name,
            "anchor_ref": anchor_ref,
            "anchor_position": {"x": anchor_x, "y": anchor_y},
            "placed_count": len(placed),
            "placed": placed,
            "mean_nn_mm": scores["mean_nn_mm"],
            "mean_spread_mm": scores["mean_spread_mm"],
            "found_clear_position": found_clear,
            "backup_path": backup_path,
            "schematic_path": schematic_path,
        }

    @mcp.tool()
    async def move_symbol_group(
        schematic_path: str,
        group_name: str,
        anchor_x: float,
        anchor_y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Translate a placed group as a rigid unit to a new anchor position.

        All group members are translated by the same delta that moves the
        anchor from its current position to (anchor_x, anchor_y).  The
        relative layout of members within the group is preserved exactly.

        If the target position conflicts with existing symbols, the tool
        automatically searches for the nearest free area and adjusts.

        A .kicad_sch.bak backup is created before writing.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            group_name: Name of the group to move.
            anchor_x: Target X position for the anchor in mm.
            anchor_y: Target Y position for the anchor in mm.

        Returns:
            group_name, anchor_ref, anchor_position, moved_count,
            position_adjusted, backup_path.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        from kcaa.tools.placement_helpers import _find_free_area_impl
        from kcaa.tools.sheet_tools import _has_position_conflict

        members = _get_group_members(sch, schematic_path, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_symbols_to_group to add symbols first."
                )
            }

        anchor = _find_anchor(members)
        anchor_ref = anchor["reference"]
        delta_x = float(anchor_x) - anchor["x"]
        delta_y = float(anchor_y) - anchor["y"]

        moved_positions: dict[str, tuple[float, float]] = {}
        for m in members:
            moved_positions[m["reference"]] = (
                m["x"] + delta_x,
                m["y"] + delta_y,
            )

        # Check for conflicts and auto-adjust
        union_bb = _compute_group_union_bbox(members)
        position_adjusted = False

        if union_bb:
            bb_w = union_bb["max_x"] - union_bb["min_x"]
            bb_h = union_bb["max_y"] - union_bb["min_y"]
            # The group bbox at target position
            grp_min_x = min(p[0] for p in moved_positions.values())
            grp_min_y = min(p[1] for p in moved_positions.values())

            has_conflict = _has_position_conflict(schematic_path, grp_min_x, grp_min_y, bb_w, bb_h)
            if has_conflict:
                free = _find_free_area_impl(
                    schematic_path=schematic_path,
                    width=bb_w,
                    height=bb_h,
                    prefer_near={"x": float(anchor_x), "y": float(anchor_y)},
                    max_candidates=1,
                )
                cand = (free.get("candidates") or [{}])[0]
                origin = cand.get("origin")
                if origin is not None:
                    anchor_x = _snap_to_grid(float(origin["x"]) - union_bb["min_x"])
                    anchor_y = _snap_to_grid(float(origin["y"]) - union_bb["min_y"])
                    delta_x = anchor_x - anchor["x"]
                    delta_y = anchor_y - anchor["y"]
                    position_adjusted = True
                    for m in members:
                        moved_positions[m["reference"]] = (
                            m["x"] + delta_x,
                            m["y"] + delta_y,
                        )

        # Apply moves (use assignment, not in-place, to persist through sch.write)
        for sym in _iter_symbols(sch):
            ref = _get_sym_property(sym, "Reference") or ""
            if ref in moved_positions:
                new_x, new_y = moved_positions[ref]
                sym.at.value = [
                    _snap_to_grid(new_x),
                    _snap_to_grid(new_y),
                    sym.at.value[2],
                ]

        try:
            backup_path = save_schematic(schematic_path, sch)
        except OSError as exc:
            return {"error": f"Failed to write schematic: {exc}"}

        result: dict[str, Any] = {
            "group_name": group_name,
            "anchor_ref": anchor_ref,
            "anchor_position": {"x": anchor_x, "y": anchor_y},
            "moved_count": len(members),
            "backup_path": backup_path,
            "schematic_path": schematic_path,
        }
        if position_adjusted:
            result["position_adjusted"] = True
            result["note"] = "Position adjusted to nearest free area to avoid overlap."
        return result

    @mcp.tool()
    async def rotate_symbol_group(
        schematic_path: str,
        group_name: str,
        rotation_delta: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Rotate a placed group as a rigid unit around its anchor.

        All member positions are rotated around the current anchor position
        using the rigid-body rotation formula.  Each member's own rotation
        is also incremented by rotation_delta so symbols keep their relative
        orientation.

        Positive angles rotate the group counter-clockwise on screen,
        matching KiCad's CCW-positive file-angle convention (0=right,
        90=up).  The position sweep and each member's file rotation both
        rotate CCW, so the group stays rigid.
        Overlap with other symbols is checked before committing; if a
        conflict is detected the group is NOT rotated.

        A .kicad_sch.bak backup is created before writing.

        Args:
            schematic_path: Absolute path to the .kicad_sch file.
            group_name: Name of the group to rotate.
            rotation_delta: Rotation in degrees (counter-clockwise on screen,
                KiCad file convention) to apply to the group.  E.g. 90
                rotates the whole group 90° CCW around the anchor.

        Returns:
            group_name, anchor_ref, rotation_delta, rotated_count,
            backup_path.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        from kcaa.tools.sheet_tools import _has_position_conflict

        members = _get_group_members(sch, schematic_path, group_name)

        if not members:
            return {
                "error": (
                    f"Group '{group_name}' has no members. "
                    "Use assign_symbols_to_group to add symbols first."
                )
            }

        anchor = _find_anchor(members)
        anchor_ref = anchor["reference"]
        ax, ay = anchor["x"], anchor["y"]

        theta = math.radians(rotation_delta)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)

        new_positions: dict[str, tuple[float, float, float]] = {}
        for m in members:
            dx = m["x"] - ax
            dy = m["y"] - ay
            # +Y-down CCW-on-screen rotation (KiCad file convention):
            # (dx, dy) → (dx·cos + dy·sin, −dx·sin + dy·cos)
            new_x = round(ax + dx * cos_t + dy * sin_t, 9)
            new_y = round(ay - dx * sin_t + dy * cos_t, 9)
            # +Y-down: CCW rotation = POSITIVE rotation_delta (file angle)
            new_rot = (m["rotation"] + rotation_delta) % 360.0

            new_positions[m["reference"]] = (
                _snap_to_grid(new_x),
                _snap_to_grid(new_y),
                new_rot,
            )

        # Check for inter-group conflicts (use union body bbox, excluding group members)
        union_bb = _compute_group_union_bbox(members)
        conflict_detected = False
        if union_bb:
            bb_w = union_bb["max_x"] - union_bb["min_x"]
            bb_h = union_bb["max_y"] - union_bb["min_y"]
            bb_x = ax + union_bb["min_x"]
            bb_y = ay + union_bb["min_y"]
            if bb_w > 0 and bb_h > 0:
                conflict_detected = _has_position_conflict(
                    schematic_path,
                    bb_x,
                    bb_y,
                    bb_w,
                    bb_h,
                    exclude_refs={m["reference"] for m in members},
                )

        if conflict_detected:
            return {
                "error": (
                    "Inter-group overlap detected after rotation; "
                    "group was NOT rotated. "
                    "Try a different rotation_delta or move the group first."
                ),
                "group_name": group_name,
            }

        # Apply rotations (use assignment, not in-place, to persist through sch.write)
        for sym in _iter_symbols(sch):
            ref = _get_sym_property(sym, "Reference") or ""
            if ref in new_positions:
                nx, ny, nr = new_positions[ref]
                sym.at.value = [nx, ny, nr]

        try:
            backup_path = save_schematic(schematic_path, sch)
        except OSError as exc:
            return {"error": f"Failed to write schematic: {exc}"}

        return {
            "group_name": group_name,
            "anchor_ref": anchor_ref,
            "rotation_delta": rotation_delta,
            "rotated_count": len(members),
            "backup_path": backup_path,
            "schematic_path": schematic_path,
        }
