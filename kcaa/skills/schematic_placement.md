---
name: schematic-placement
priority: 70
description: "Symbol placement workflow, find_free_area, bbox geometry, spacing rules"
---
# Geometry you get for free
- get_symbol and get_symbol_pins return body_bbox + per-unit bboxes in
  library coordinates so you can size new placements before inserting.
- extract_schematic_netlist returns body_bbox per placed component in
  schematic (Y-down) world coordinates. Use it as your occupancy map.
- add_symbol_to_schematic, place_symbol_relative and move_component all
  return the resulting body_bbox so you usually do NOT need to re-extract
  the netlist after a successful placement.

# Recommended placement workflow
1. Call extract_schematic_netlist to learn what already exists and where.
2. To add a new symbol:
   a. Look it up with search_symbols / get_symbol (note its body_bbox).
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
4. Wire pins using this priority order — try each in turn, stop at first
   success; if both fail, **report the failure and the coordinates to
   the user** instead of silently skipping the wire:
   a. **connect_pins_with_wire(from_ref, from_pin, to_ref, to_pin)** —
      preferred for pin-to-pin; resolves coordinates automatically, routes
      with smart orthogonal routing, and inserts junctions automatically.
      If this fails, **immediately report to the user**: the tool name
      (connect_pins_with_wire), the exact arguments used, and the full
      error message returned. Then proceed to (b).
   b. **connect_points_with_wire(start_x, start_y, end_x, end_y)** — smart
      orthogonal routing between bare coordinates; use when endpoints are
      not symbol pins (e.g. net label positions, existing wire tips).
      If this fails, **stop and report** the tool name
      (connect_points_with_wire), the exact arguments used, and the full
      error message to the user.

# Spacing & layout rules
- Keep at least one grid step (1.27 mm) of clearance between symbol body
  bboxes; 2.54 mm or more is preferred so Reference/Value labels do not
  collide with neighbours.
- Align rows of similar components on the same Y; align columns on the
  same X. Use multiples of 1.27 mm for spacing so wires stay orthogonal.
- Stay inside the recommended_area returned by get_schematic_sheet_info;
  never place inside title_block_default.
