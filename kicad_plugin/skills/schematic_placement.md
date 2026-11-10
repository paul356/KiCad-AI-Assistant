---
name: schematic-placement
priority: 70
description: "Symbol placement workflow, find_free_area, bbox geometry, spacing rules"
tools: [extract_schematic_netlist, add_symbol_to_schematic, place_symbol_relative, move_component, find_free_area, get_schematic_sheet_info, update_schematic_symbol_pin]
---
# Geometry you get for free
- get_library_symbol and get_library_symbol_pins return body_bbox + per-unit bboxes in
  library coordinates so you can size new placements before inserting.
- extract_schematic_netlist returns a body_bbox per placed **unit** in
  schematic (Y-down) world coordinates. Union every unit's bbox to get a
  component's occupied region and use that as your occupancy map.
- add_symbol_to_schematic, place_symbol_relative and move_component all
  return the resulting body_bbox so you usually do NOT need to re-extract
  the netlist after a successful placement.

# Recommended placement workflow
1. Call extract_schematic_netlist to learn what already exists and where.
2. To add a new symbol:
   a. Look it up with search_library_symbols / get_library_symbol (note its body_bbox).
   b. PREFER place_symbol_relative when you can describe the position
      relative to an existing reference (e.g. "right of U1, gap 2.54").
   c. Otherwise call find_free_area(for_library=..., for_symbol=...,
      prefer_near=...) — always supply ``for_library`` and ``for_symbol``
      so each candidate includes a **placement** ``{x, y}`` field. Pass
      ``placement.x`` / ``placement.y`` directly to add_symbol_to_schematic.
      (``origin`` is the bbox top-left, NOT the symbol anchor; do not use
      it for placement coordinates.)
   d. Only fall back to absolute add_symbol_to_schematic(x, y, ...) if the
      helpers above cannot satisfy the request.
3. After moves/inserts, prefer using the returned body_bbox; only call
   extract_schematic_netlist again if you need to refresh net info.

# Moving / rotating components
- move_component takes **deltas**: `x`/`y` are mm to shift by (each snapped
  to the 1.27 mm grid), `rotation` is an increment applied per unit as
  `(old + rotation) % 360` (restricted to 0/90/180/270). Read the current
  anchor from extract_schematic_netlist and subtract to express an
  absolute move as a delta; `position`/`rotation` in the result are the
  new **absolute** values of the first moved unit.
- Omitting `unit` moves **every** unit of a multi-unit symbol together by
  the same shift, preserving relative layout. Pass `unit=N` to move/rotate
  one unit individually (no overlap-avoidance search).
- Whole-move deltas may be nudged to the nearest free area when the target
  region is occupied: `position_adjusted` is then `True` and
  `requested_position` holds the grid-snapped target.

# Fixing pin numbering before footprint assignment
- Inspect the symbol with **get_library_symbol_pins**. If its pins are numbered
  `P$1`, `P$2`, … but the footprint pads are `1`, `2`, …, fix the definition
  with **update_library_symbol_pin(project_dir, lib_id, pins)**
  (library file) and, for parts already placed,
  **update_schematic_symbol_pin(schematic_path, lib_id, pins)** (the
  schematic's cached `lib_symbols` copy that KiCad actually renders). For the
  library tool `project_dir` is required (pass `active_project`): its
  `sym-lib-table` is searched first, then the global table. `lib_id` there is
  `"Library:Symbol"` and is split on the first colon; for the schematic tool
  `lib_id` is matched by exact cached entry name, so no colon is required.
  `pins` is a batch list of `{pin_number, new_number?, new_name?}` entries;
  renames in a batch are applied simultaneously and validated against the
  final state. A pin number is the identity of a physical connection, so every
  definition carrying it and every placement's instance pin keys are updated
  together. Numbers stay unique where they were unique: a batch that would
  give two pins the same number is rejected, while duplicates that already
  existed (stacked pins) stay legal. Pin names may repeat or be empty; a new
  number already used by an untouched pin produces a warning, not an error.
- Do this **before** `set_symbol_property` assigns the `Footprint`; the two
  tools are a pair — the library edit covers future placements, the schematic
  edit covers the placed copies. Both report the changes and write `.bak`
  backups, and the schematic tool also rewrites the placements' instance pin
  keys.

# Spacing & layout rules
- Keep at least one grid step (1.27 mm) of clearance between symbol body
  bboxes; 2.54 mm or more is preferred so Reference/Value labels do not
  collide with neighbours.
- Align rows of similar components on the same Y; align columns on the
  same X. Use multiples of 1.27 mm for spacing so wires stay orthogonal.
- Stay inside the recommended_area returned by get_schematic_sheet_info;
  never place inside title_block_default.
