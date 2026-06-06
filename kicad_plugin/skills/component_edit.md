---
name: component-edit
priority: 65
description: "Property CRUD, reference renaming, label management, reference conflict checking"
---
# Tools overview
- **set_component_property** — Set or add a property on a placed schematic component.
- **rename_symbol** — Rename a symbol's reference designator (auto-assign available).
- **list_component_properties** — List all properties of a placed schematic component.
- **delete_component_property** — Delete a non-essential property from a component.
- **add_label_to_schematic** — Add a local, global, or hierarchical net label.
- **list_labels_in_schematic** — List all labels with coordinates, type, and shape.
- **delete_label_from_schematic** — Delete labels by position (single or batch mode).
- **check_reference_conflicts** — Find duplicate reference designators across the project hierarchy.

# Recommended property workflow
1. Call **list_component_properties(schematic_path, reference)** to see existing properties
   (returns `{name, value}` pairs from the first unit of a multi-unit symbol).
2. Call **set_component_property(schematic_path, reference, property_name, property_value)**
   to add or update. All units sharing that reference are updated together. Non-standard
   properties (anything besides Reference and Value) are hidden on the canvas by default.
   Returns `action` ("added", "updated", or "mixed") to tell you what happened per-unit.
3. To remove, call **delete_component_property(schematic_path, reference, property_name)**.
   `Reference` and `Value` are KiCad-required and **cannot be deleted**; attempting to do so
   returns an error without modifying the file.

# Reference renaming workflow
1. Call **check_reference_conflicts(schematic_path)** to scan all schematics in the
   sheet hierarchy for duplicates before bulk renames.
2. Call **rename_symbol(schematic_path, symbol_uuid)** using the UUID from
   `extract_schematic_netlist` → component `uuid` field. Omitting `target_reference`
   auto-assigns the next free reference for the same prefix (scans all project
   `*.kicad_sch` files to avoid cross-sheet conflicts).
3. Verify with **check_reference_conflicts** afterward.

# Label workflow
1. Call **list_labels_in_schematic(schematic_path, label_type)** (optional filter
   `"local"`, `"global"`, `"hierarchical"`) to get every label's text, coordinates
   (x, y in mm), direction, and shape.
2. Call **add_label_to_schematic(schematic_path, text, x, y, angle, label_type, shape)**
   to add a new label. Coordinates are in mm, +Y down. CRITICAL: the label **must sit
   exactly on a wire or pin endpoint** to attach. This tool does NOT auto-snap; use
   coordinates from `extract_schematic_netlist` (pin x/y) or wire endpoints (from
   `include_wire_topology=True`), and ensure they are aligned to the 1.27 mm grid.
   Valid `angle`: 0, 90, 180, 270. Valid `shape` (for global/hierarchical): `"input"`,
   `"output"`, `"bidirectional"`, `"tri_state"`, `"passive"`.
3. Call **delete_label_from_schematic** in either:
   - **Single mode**: `(schematic_path, x, y, text=..., tolerance=0.01, label_type=...)`
   - **Batch mode**: `(schematic_path, positions=[{x, y, text?}, ...], label_type=...)`
   Use coordinates from `list_labels_in_schematic`.

# Caveats & gotchas
- All mutation tools create a `.kicad_sch.bak` backup before saving.
- `rename_symbol` also updates `instances.path.reference` (KiCad's authoritative display
  reference). Omitting `target_reference` sets `auto_assigned: true`.
- `set_component_property` clones the existing Value property structure when creating a
  new property. Non-standard properties get `(hide yes)` injected automatically.
- `delete_label_from_schematic` with `positions` (batch mode) collects all targets then
  saves once — far more efficient than calling it multiple times.
- `check_reference_conflicts` only scans schematics reachable from the root schematic
  (following the sheet hierarchy). It skips backup/history folders.
