# Schematic Symbol Group Placement Tools

## Overview

Add group-based placement management for schematic symbols, mirroring the
PCB footprint group system. Symbols can be assigned to named groups, then
laid out, moved, rotated, and scored as rigid units.

## Architecture

**New file**: `kcaa/tools/schematic_group_tools.py`
**Modified**: `kcaa/server.py` — register new tools
**Tests**: `tests/unit/tools/test_schematic_group_tools.py`

## Data Storage

The group name is stored as a custom symbol property `placement_group`,
exactly mirroring the PCB approach. This means:
- Group assignments persist in the `.kicad_sch` file
- Symbol properties are manipulated via the existing `_find_property_by_name`
  and `set_symbol_property` clone mechanism

## Seven Tools

| # | Tool | Description | Mirrors |
|---|------|-------------|---------|
| 1 | `assign_symbols_to_group` | Batch assign/unassign symbols to a named group | `assign_footprints_to_group` |
| 2 | `list_symbol_groups` | List all groups with member count, anchor, bbox | `list_footprint_groups` |
| 3 | `get_symbol_group` | Full member details: positions, rotations, pin counts, bboxes | `get_footprint_group` |
| 4 | `score_symbol_group` | Proximity score: mean nearest-neighbor distance + centroid spread | `score_footprint_group` |
| 5 | `place_symbol_group` | Two-phase auto-layout: grid arrange + sheet free-area search | `place_footprint_group` |
| 6 | `move_symbol_group` | Rigid translate with automatic conflict avoidance | `move_footprint_group` |
| 7 | `rotate_symbol_group` | Rigid rotate around anchor with collision check | `rotate_footprint_group` |

## Key Differences from PCB

| Aspect | PCB (Footprint) | Schematic (Symbol) |
|--------|----------------|---------------------|
| Collision detection | Courtyard model | body_bbox + margin |
| Anchor selection | Tier (pin count × footprint type) | Symbol with most pins |
| Grid layout | Pad-direction-guided + edge-to-edge routing distance | Bbox-based row/column grid + margin |
| Board/sheet placement | Raster scan + HPWL optimization | `_find_free_area_impl()` spiral search with `prefer_near` |
| Quality score | Intra-group HPWL (pad-to-net) | Mean nearest-neighbor distance + centroid spread |
| Conflict on move/rotate | Reject with error | Auto-adjust to nearest free (soft strategy) |

## Phase 1: Grid Layout Algorithm

1. Identify anchor = symbol with highest pin count
2. Sort non-anchor members by pin count descending
3. Place members in a rectangular grid around the anchor:
   - Right column (+X), then bottom row (+Y), alternating
   - Spacing = max(anchor_bbox_dim, member_bbox_dim) / 2 + gap_mm
4. Auto-rotate each member: if body_bbox width > height, rotate 90° for
   more compact footprint
5. All positions snapped to 1.27mm grid

## Phase 2: Sheet Free-Area Search

1. Start from anchor's current position
2. Compute union bbox of all group members at relative layout
3. Check conflict via `_has_position_conflict()`
4. If no conflict → place at current anchor position
5. If conflict → call `_find_free_area_impl(prefer_near=anchor_current_pos)`
6. Translate entire group to the found free position

## Helper Functions

All internal to `schematic_group_tools.py`:

- `_get_sym_property(sym, name)` → `str | None` — read a symbol property
- `_set_sym_property(sym, name, value)` — update existing or add via clone
- `_iter_symbols(sch)` → yield symbol objects
- `_get_group_members(sch, group_name)` → list of member info dicts
- `_find_anchor(members)` → member with most pins
- `_collect_symbol_body_bboxes(schematic_path)` → occupied bboxes for collision
- `_compute_group_union_bbox(members)` → group-level bounding box
- `_grid_arrange_relative(anchor, non_anchor_members, gap_mm)` → relative positions
- `_compute_proximity_score(members)` → nearest-neighbor + spread metrics

## Development Tasks

1. **implementing-property-helpers** — `_get_sym_property`, `_set_sym_property`,
   `_iter_symbols` (symbol property read/write + iteration)

2. **implementing-group-helpers** — `_get_group_members`, `_find_anchor`,
   `_collect_symbol_body_bboxes`, `_compute_group_union_bbox`

3. **implementing-grid-layout** — `_grid_arrange_relative` with bbox-based
   row/column placement around anchor

4. **implementing-assign-list-get-tools** — `assign_symbols_to_group`,
   `list_symbol_groups`, `get_symbol_group`

5. **implementing-score-tool** — `score_symbol_group` with proximity metrics

6. **implementing-place-tool** — `place_symbol_group` two-phase layout

7. **implementing-move-rotate-tools** — `move_symbol_group`,
   `rotate_symbol_group`

8. **registering-tools** — update `kcaa/server.py`, `kicad_plugin/tool_registry.py`,
   `kicad_plugin/llm_client.py`

9. **writing-tests** — unit tests for all 7 tools + helper functions
