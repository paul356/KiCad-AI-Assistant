# KiCad MCP Plugin — Tool Contract (Milestone 1)

> **Scope:** This document defines the exact set of MCP tools exposed to the LLM in the milestone-1 plugin integration. Tools not listed here are either deferred or inapplicable to the plugin profile.

---

## 1. Milestone-1 Tool Surface

| Tool | Module | Description |
|---|---|---|
| `extract_project_netlist` | `netlist_tools` | Extract netlist from the schematic associated with a `.kicad_pro` file |
| `extract_schematic_netlist` | `netlist_tools` | Extract component inventory, nets, pin positions, and optionally wire topology from a `.kicad_sch` file |
| `find_component_connections` | `netlist_tools` | List all electrical connections for a single reference designator |
| `sync_symbol_index` | `symbol_tools` | Build or refresh the full-text symbol search index from installed KiCad libraries |
| `get_symbol_sync_status` | `symbol_tools` | Return the last-sync timestamp and index size |
| `search_symbols` | `symbol_tools` | Full-text search across all indexed symbols |
| `get_symbol` | `symbol_tools` | Retrieve full definition and metadata for one symbol |
| `list_symbol_libraries` | `symbol_tools` | List all known symbol libraries (names and paths) |
| `get_library_symbols` | `symbol_tools` | List every symbol in a specific library |
| `get_symbol_index_stats` | `symbol_tools` | Return aggregate counts for the index |
| `get_symbol_pins` | `symbol_tools` | Return pin list (number, name, type, direction) for one symbol |
| `add_symbol_to_schematic` | `component_edit_tools` | Place a library symbol onto a schematic |
| `remove_symbol_from_schematic` | `component_edit_tools` | Remove one or more components by reference designator |
| `set_component_property` | `component_edit_tools` | Set or create a named property (e.g. `Value`, `Footprint`) on a component |
| `list_component_properties` | `component_edit_tools` | Return all properties for a component |
| `delete_component_property` | `component_edit_tools` | Delete a named property from a component |
| `move_component` | `component_edit_tools` | Move a component to new coordinates, optionally changing rotation |
| `add_label_to_schematic` | `component_edit_tools` | Place a net label at specific coordinates |
| `list_labels_in_schematic` | `component_edit_tools` | Return all net labels and their positions |
| `delete_label_from_schematic` | `component_edit_tools` | Remove a net label by text and coordinates |
| `add_wire_to_schematic` | `wire_edit_tools` | Draw a wire segment between two coordinates (orthogonal routing) |
| `connect_pins_with_wire` | `wire_edit_tools` | Connect two component pins automatically, resolving their schematic positions |
| `delete_wire_from_schematic` | `wire_edit_tools` | Remove a wire segment matching given start/end coordinates |
| `add_junction_to_schematic` | `wire_edit_tools` | Place a junction dot at a coordinate |
| `list_junctions_in_schematic` | `wire_edit_tools` | Return all junction coordinates |
| `delete_junction_from_schematic` | `wire_edit_tools` | Remove a junction at a given coordinate |

---

## 2. Recommended Usage Order

A typical LLM editing session follows this pattern:

1. **Read current state** — call `extract_schematic_netlist` with the `active_schematic` path from the plugin context. Inspect `analysis.components`, `analysis.power_nets`, and `analysis.signal_nets` to understand what is already present.
2. **Find symbols** — if adding a new component, call `search_symbols` first to obtain the exact `library_name` and `name` values required by `add_symbol_to_schematic`. Never guess library names.
3. **Place / edit / connect** — use the component-editing and wire-editing tools to make changes. After placing a symbol, call `extract_schematic_netlist` to get the new pin positions before routing wires.
4. **Verify** — call `extract_schematic_netlist` again after all edits. Confirm expected nets exist in `analysis.signal_nets` or `analysis.power_nets` and that `analysis.floating_nets` is empty.

> **Tip:** `connect_pins_with_wire` is the preferred wiring tool when the two endpoints are component pins — it resolves positions automatically. Use `add_wire_to_schematic` only when routing to/from a known coordinate (e.g. a label or an existing wire endpoint).

---

## 3. Tool Groups

### 3.1 Netlist / Inspection Tools

#### `extract_project_netlist(project_path)`

**Purpose:** Convenience wrapper that locates the primary schematic for a `.kicad_pro` project and delegates to `extract_schematic_netlist`. Useful when the engineer provides a project path instead of a schematic path.

**Key parameters:**
- `project_path` (`str`) — absolute path to a `.kicad_pro` file.

**Return shape:** Same as `extract_schematic_netlist` (see below).

**When to use:** When only a project path is available; prefer `extract_schematic_netlist` directly when `active_schematic` is present in context.

---

#### `extract_schematic_netlist(schematic_path, include_wire_topology=False)`

**Purpose:** Primary inspection tool. Returns the complete component inventory, net classification, pin positions, and optional wire geometry for a schematic file.

**Key parameters:**
- `schematic_path` (`str`) — absolute path to a `.kicad_sch` file.
- `include_wire_topology` (`bool`, default `False`) — when `True`, adds a `wires` dict to `analysis` with per-wire net membership, start/end coordinates, and which pins touch each endpoint.

**Return shape (`analysis` object):**

```json
{
  "component_count": 12,
  "net_count": 8,
  "component_types": {"R": 4, "C": 2, "U": 1},
  "components": {
    "R1": {
      "value": "10k",
      "position": {"x": 100.0, "y": 50.0, "rotation": 0},
      "pins": [
        {"num": "1", "x": 101.27, "y": 50.0, "direction": "right", "net": "VCC"}
      ]
    }
  },
  "power_nets": [{"name": "GND", "pin_count": 6}],
  "signal_nets": [{"name": "SDA", "pin_count": 3}],
  "floating_nets": [{"net": "Net-(R2-Pad1)", "description": "..."}]
}
```

`direction` is the wire-exit direction in screen coordinates: `"right"`, `"down"`, `"left"`, or `"up"`.

**When to use:** At the start of every session and after any structural edit to verify the result.

---

#### `find_component_connections(project_path, component_ref)`

**Purpose:** Returns all nets and connected pins for a single reference designator. Faster than parsing the full netlist when the LLM only needs to understand one component's connectivity.

**Key parameters:**
- `project_path` (`str`) — absolute path to a `.kicad_pro` file.
- `component_ref` (`str`) — reference designator (e.g. `"R1"`).

**Return shape:** `{"success": true, "component": "R1", "connections": [{"pin": "1", "net": "VCC", "connected_pins": [...]}, ...]}`

**When to use:** When the engineer asks "what is R1 connected to?" without needing the full schematic state.

---

### 3.2 Symbol Index Tools

#### `sync_symbol_index(force=False)`

**Purpose:** Scans all installed KiCad symbol libraries and populates the local full-text search index. Must be called at least once before `search_symbols` will return results.

**Key parameters:** `force` (`bool`) — when `True`, rebuilds even if the index appears current.

---

#### `get_symbol_sync_status()`

**Purpose:** Returns the last-sync timestamp and the number of indexed symbols. Use to decide whether `sync_symbol_index` is needed.

---

#### `search_symbols(query, limit=50)`

**Purpose:** Full-text search across symbol name, description, and keywords in the index.

**Key parameters:**
- `query` (`str`) — search string (e.g. `"NPN transistor"`, `"STM32F4"`).
- `limit` (`int`, default `50`) — maximum results.

**Return shape:** `{"success": true, "count": N, "symbols": [{"library_name": "...", "name": "...", "description": "...", "pin_count": N}, ...]}`

> **Important:** Always use the `library_name` value returned here verbatim as the `library_name` argument to other tools. For KiCad 10 symdir-style libraries the format is `"TableName/FileBaseName"` (e.g. `"Device/R_Small"`), **not** the bare table name (`"Device"`).

---

#### `get_symbol(library_name, symbol_name)`

**Purpose:** Retrieve the full symbol definition including all properties and pin details.

**Key parameters:** `library_name` and `symbol_name` exactly as returned by `search_symbols`.

---

#### `list_symbol_libraries()`

**Purpose:** Returns names and file paths for all known symbol libraries. Useful for exploration when `search_symbols` returns no results.

---

#### `get_library_symbols(library_name)`

**Purpose:** Lists every symbol inside a specific library. Use to browse a library after identifying it via `list_symbol_libraries`.

**Key parameters:** `library_name` — same format as above (`"TableName/FileBaseName"`).

---

#### `get_symbol_index_stats()`

**Purpose:** Returns aggregate statistics (total symbols, total libraries, index size). Diagnostic only.

---

#### `get_symbol_pins(library_name, symbol_name)`

**Purpose:** Returns the pin list (number, name, electrical type, direction) for a symbol without fetching the full definition. Use when planning wiring before placing a component.

---

### 3.3 Component Editing Tools

All tools in this group write a backup to `<schematic_path>.bak` before saving.

#### `add_symbol_to_schematic(schematic_path, library_name, symbol_name, x, y, rotation=0, value=None, fields_autoplaced=True)`

**Purpose:** Places a symbol from a KiCad library onto a schematic. Injects the library definition, places one instance per symbol unit, and auto-aligns to the 1.27 mm (50-mil) grid.

**Key parameters:**
- `schematic_path` (`str`) — absolute path to `.kicad_sch`.
- `library_name` (`str`) — as returned by `search_symbols`.
- `symbol_name` (`str`) — as returned by `search_symbols`.
- `x`, `y` (`float`) — placement coordinates in mm; snapped to 1.27 mm grid.
- `rotation` (`int`) — `0`, `90`, `180`, or `270`.
- `value` (`str | None`) — overrides the `Value` property; defaults to `symbol_name`.
- `fields_autoplaced` (`bool`) — when `True`, KiCad will reflow field positions on next open.

**Success response:** `{"success": true, "reference": "R3", "units_placed": 1, "position": {"x": ..., "y": ...}}`

**Failure response:** `{"success": false, "error": "<message>"}`

---

#### `remove_symbol_from_schematic(schematic_path, references)`

**Purpose:** Removes all placed units for one or more reference designators. Also removes the `lib_symbols` entry if no other placed symbol uses the same library ID.

**Key parameters:**
- `references` (`list[str]`) — one or more reference designators (e.g. `["C1", "R3"]`).

**Success response:** `{"success": true, "total_removed_units": 2, "results": {"C1": {...}, "R3": {...}}, "warnings": []}`

---

#### `set_component_property(schematic_path, reference, property_name, property_value)`

**Purpose:** Sets or creates a named property on a placed component (e.g. `"Footprint"`, `"Value"`, `"MPN"`).

**Key parameters:** All `str`. `property_name` is case-sensitive and must match the KiCad field name exactly.

**Success response:** `{"success": true, "reference": "U1", "property": "Footprint", "value": "..."}`

---

#### `list_component_properties(schematic_path, reference)`

**Purpose:** Returns all properties and their values for one component. Use before `set_component_property` to discover existing field names.

**Success response:** `{"success": true, "reference": "U1", "properties": {"Reference": "U1", "Value": "ATmega328P", ...}}`

---

#### `delete_component_property(schematic_path, reference, property_name)`

**Purpose:** Deletes a non-mandatory property from a component. Cannot delete `Reference` or `Value`.

---

#### `move_component(schematic_path, reference, x, y, rotation=None)`

**Purpose:** Moves a component to a new position, optionally updating rotation. Coordinates are in mm and are snapped to the 1.27 mm grid.

**Key parameters:**
- `x`, `y` (`float`) — new position in mm.
- `rotation` (`int | None`) — if `None`, preserves the existing rotation.

**Success response:** `{"success": true, "reference": "R1", "position": {"x": ..., "y": ..., "rotation": ...}}`

---

### 3.4 Label Editing Tools

All label tools write `.kicad_sch.bak` before saving.

#### `add_label_to_schematic(schematic_path, label_text, x, y, rotation=0)`

**Purpose:** Places a net label at the given coordinates. A label connects all pins that touch the same label text.

**Key parameters:** `label_text` (`str`), `x`/`y` (`float`, mm), `rotation` (`int`).

**Success response:** `{"success": true, "label": "SDA", "position": {"x": ..., "y": ...}}`

---

#### `list_labels_in_schematic(schematic_path)`

**Purpose:** Returns every net label in the schematic with its text and position. Use before placing a new label to avoid duplicates or to find a label's coordinates for deletion.

**Success response:** `{"success": true, "labels": [{"text": "SDA", "x": ..., "y": ..., "rotation": 0}, ...]}`

---

#### `delete_label_from_schematic(schematic_path, label_text, x, y)`

**Purpose:** Removes a specific label instance identified by text and position. If two labels share the same text, only the one at the given coordinates is removed.

---

### 3.5 Wire and Junction Tools

All tools in this group write `.kicad_sch.bak` before saving.

#### `add_wire_to_schematic(schematic_path, start_x, start_y, end_x, end_y, add_junction_start=False, add_junction_end=False)`

**Purpose:** Draws a wire segment using orthogonal routing. Set `add_junction_start` or `add_junction_end` to `True` when an endpoint lands in the middle of an existing wire (T-junction).

**Key parameters:** All coordinates in mm (`float`).

**Success response:** `{"success": true, "wire": {"start": {...}, "end": {...}}, "junctions_added": [...]}`

---

#### `connect_pins_with_wire(schematic_path, from_ref, from_pin, to_ref, to_pin)`

**Purpose:** Resolves pin schematic positions automatically (accounting for placement and rotation), then routes a smart orthogonal wire between them. Automatically places junction dots when either endpoint is already wired. Preferred over `add_wire_to_schematic` for pin-to-pin connections.

**Key parameters:**
- `from_ref` / `to_ref` (`str`) — reference designators.
- `from_pin` / `to_pin` (`str`) — pin numbers as strings (e.g. `"1"`, `"A2"`).

**Success response:** `{"success": true, "wire": {"from": {...}, "to": {...}}, "collision_free": true, "auto_junctions_added": [...]}`

---

#### `delete_wire_from_schematic(schematic_path, start_x, start_y, end_x, end_y)`

**Purpose:** Removes a wire segment whose start and end coordinates match the given values (within floating-point tolerance). Use `extract_schematic_netlist` with `include_wire_topology=True` to obtain exact wire coordinates before calling.

---

#### `add_junction_to_schematic(schematic_path, x, y)`

**Purpose:** Places a junction dot at a specific coordinate. Required at T-intersections where three or more wires meet.

**Success response:** `{"success": true, "junction": {"x": ..., "y": ...}}`

---

#### `list_junctions_in_schematic(schematic_path)`

**Purpose:** Returns all existing junction positions. Use to avoid placing duplicate junctions.

**Success response:** `{"success": true, "junctions": [{"x": ..., "y": ...}, ...]}`

---

#### `delete_junction_from_schematic(schematic_path, x, y)`

**Purpose:** Removes a junction at the given coordinate.

---

## 4. Deferred Capabilities

| Capability | Reason deferred | Target milestone |
|---|---|---|
| All MCP resources (`kicad://...`) | Resources are not part of the tool-only plugin profile | Post-milestone-1 |
| All MCP prompts | Prompts assume a standalone MCP client (e.g. Claude Desktop) | Post-milestone-1 |
| `list_projects`, `get_project_structure` | Plugin provides active-project context directly | Review after initial plugin is stable |
| `open_project` | KiCad plugin manages project opening directly | Not applicable |
| `get_drc_history`, `run_drc_check` | `kicad-cli` dependency; not skip-based | Separate milestone after `kicad-cli` removal |
| `generate_pcb_thumbnail`, `generate_thumbnail_with_cli` | `kicad-cli` dependency | Separate milestone |
| `get_bom`, `export_bom_csv` | `kicad-cli` dependency | Separate milestone |
| All pattern tools (`pattern_tools.py`) | Not needed for direct editing workflows | Optional future addition |
| `validate_project` | Superseded by netlist inspection tools for editing validation | Review later |
| `validate_project_boundaries`, `generate_validation_report` | `kicad-cli` dependency | Separate milestone |
| PCB edit tools | Planned for future milestone; see `docs/plugin/pcb_feasibility.md` | Future |

---

## 5. Context Passed by the Plugin

The plugin inserts a context object into every MCP request:

```json
{
  "active_project": "/path/to/project.kicad_pro",
  "active_schematic": "/path/to/schematic.kicad_sch",
  "active_editor": "schematic",
  "selected_refs": ["R1", "C3"]
}
```

The LLM **must** use `active_schematic` as the `schematic_path` argument for all editing and inspection tools unless the engineer explicitly specifies a different file. `selected_refs` contains the reference designators currently selected in the KiCad editor and can be used to infer which components the engineer wants to modify.

---

## 6. Error Handling Contract

- **All tools** return `{"success": false, "error": "<human-readable message>"}` on failure. A missing `success` key should be treated as a failure.
- The LLM **must check `success`** before using any other fields in the response. Do not assume a tool succeeded because it returned a dict.
- If a file write fails mid-save, the backup at `<schematic_path>.bak` remains valid and can be used for recovery.
- The LLM should **report errors clearly to the engineer** rather than silently retrying or continuing with a partial state. Include the `error` string verbatim in the message to the user.
- If `success` is `false` after `add_symbol_to_schematic` or a wire edit, do not attempt further edits that depend on that operation.
