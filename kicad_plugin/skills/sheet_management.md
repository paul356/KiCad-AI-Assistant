---
name: sheet-management
priority: 55
description: "Hierarchical sheet CRUD, sheet pins, hierarchy traversal, auto-placement on conflict"
---
# Tools overview
- **list_sheet_symbols** — List all sheet symbols on a schematic (read-only).
- **get_sheet_hierarchy** — Recursively walk the sheet hierarchy from a root schematic (read-only).
- **add_sheet_symbol** — Add a hierarchical sheet symbol (optionally create child file).
- **remove_sheet_symbol** — Remove a sheet symbol (optionally delete child file).
- **update_sheet_symbol** — Update a sheet symbol's name, file reference, position, or size.
- **add_sheet_pin** — Add a hierarchical pin to a sheet symbol.
- **remove_sheet_pin** — Remove a hierarchical pin from a sheet symbol.

# Recommended workflow
1. **Discover existing structure**:
   - Call **list_sheet_symbols(schematic_path)** to get a flat list: uuid, sheet_name,
     sheet_file, position (x, y in mm), size (width, height in mm), and pins.
   - Call **get_sheet_hierarchy(schematic_path, max_depth=10)** for the full tree.
     Each node has `file`, `sheet_name`, `children`, and `sheet_count`. Cycle detection
     prevents infinite loops.
2. **Add a sheet**:
   - Call **add_sheet_symbol(schematic_path, sheet_name, sheet_file, x, y, width, height)**.
     Coordinates are mm, +Y down, auto-snapped to 1.27 mm (50-mil) grid. If the requested
     position conflicts with existing symbols or title block, the tool auto-adjusts to the
     nearest free area and returns `position_adjusted: true`.
   - Set `create_child=True` to create the child `.kicad_sch` file on disk with optional
     `child_paper` and `child_title`.
   - Optionally provide `pins` as a list of `{name, edge, distance_mm}` dicts (edge is
     `"right"`, `"left"`, `"bottom"`, or `"top"`).
3. **Modify a sheet**:
   - Call **update_sheet_symbol(schematic_path, sheet_identifier, sheet_name, sheet_file,
     x, y, width, height)**. All arguments are optional; only provided fields change.
     `sheet_identifier` can be the UUID or sheet name. Position changes also auto-adjust
     on conflict.
4. **Manage pins**:
   - Call **add_sheet_pin(schematic_path, sheet_identifier, pin_name, edge, distance_mm)**.
   - Call **remove_sheet_pin(schematic_path, sheet_identifier, pin_name)**.
5. **Remove a sheet**:
   - Call **remove_sheet_symbol(schematic_path, sheet_identifier, delete_child=False)**.
     Set `delete_child=True` to also delete the referenced `.kicad_sch` file from disk.

# Caveats & gotchas
- All mutation tools (add/update/remove sheet/pin) create a `.kicad_sch.bak` backup.
- `sheet_identifier` accepts either the UUID or the sheet display name, making it
  easy to target sheets by human-readable names.
- `sheet_file` with relative paths is resolved against the parent schematic's directory.
- Position conflict detection checks against sheet symbols, symbol components, and the
  title block, each inflated by a 3.81 mm margin for clearance.
- `get_sheet_hierarchy` detects cycles by real path, so it handles symlinks correctly.
- `remove_sheet_symbol` with `delete_child=True` logs but does not fail if the child
  file cannot be deleted — it returns `child_delete_error` alongside success.
