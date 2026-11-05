# Capability Inventory

This document records the **keep / defer / remove** classification for the plugin-facing profile as it stands today, not the milestone-1 snapshot it started from. The classifications below cover the schematic/symbol editing surface with its symbol and footprint library tools, plus the project and DRC rows that were formerly deferred or removed. The rest of the profile — PCB editing, query, routing, zones, placement and groups, hierarchical sheets, no-connect flags, versioning, skills, ERC and KiCad IPC — is not enumerated here. That enumeration is deliberately non-exhaustive: the README's *Available Tools* tables and `kicad_plugin/tool_registry.py` are the source of truth for the current surface.

---

## 1. Tools

### 1.1 Keep — plugin-facing schematic/symbol surface (32 tools)

These tools are skip-based and form the core of the plugin-facing editing surface. `get_project_structure` and `run_drc_check` are kept here too, having been reclassified from §1.2/§1.3 once the plugin profile began exposing them.

#### Netlist / Inspection (`netlist_tools.py`)

| Tool | Purpose |
|------|---------|
| `extract_schematic_netlist` | Read connectivity and components from one `.kicad_sch` file |
| `find_component_connections` | Find all nets connected to a specific component reference |

These are **read-before-edit** tools. The LLM must call at least one of these before making changes to understand existing connectivity.

#### Symbol Index (`symbol_tools.py`)

| Tool | Purpose |
|------|---------|
| `sync_symbol_index` | Build or refresh the SQLite symbol search index |
| `get_symbol_sync_status` | Check whether the index is current |
| `search_symbols` | Full-text search across all indexed symbol libraries |
| `get_symbol` | Get full definition of one symbol by library + name |
| `list_symbol_libraries` | Enumerate all known `.kicad_sym` libraries |
| `get_library_symbols` | List all symbols in one library |
| `get_symbol_index_stats` | Return total library / symbol counts |
| `get_symbol_pins` | Get pin list for a symbol |

These are read-only tools. They must be used when the LLM is asked to add a component so it can find the correct library name and symbol name before calling `add_symbol_to_schematic`.

#### Component and Label Editing (`symbol_edit_tools.py`)

| Tool | Purpose |
|------|---------|
| `add_symbol_to_schematic` | Place a new symbol at a given position |
| `create_symbol` | Define a new symbol into a symbol library (definition only — no schematic placement) |
| `remove_symbol_from_schematic` | Remove one or more symbols by reference |
| `set_symbol_property` | Update any property on one or more components (batch, partial-apply) |
| `list_symbol_properties` | Read all properties of one or more components (batch) |
| `delete_symbol_property` | Remove a custom property |
| `move_component` | Shift a component by a delta, optionally per unit |
| `add_label_to_schematic` | Add a net label at a position |
| `list_labels_in_schematic` | Enumerate all labels in a schematic |
| `delete_label_from_schematic` | Remove a label by text and position |

All write operations create a `.bak` backup before writing.

#### Library Management (`symbol_edit_tools.py`, `pcb_library_tools.py`)

| Tool | Purpose |
|------|---------|
| `create_symbol_library` | Create a `.kicad_sym` library (global 3rd-party or project-local) and register it in sym-lib-table |
| `delete_symbol_library` | Delete an **empty** symbol library (file + table entry + index) |
| `add_symbols_to_library` | Copy cached schematic symbols into a symbol library, re-indexing it |
| `remove_symbols_from_library` | Remove named symbol definitions from a library and re-index it |
| `find_symbols_not_in_libraries` | List schematic symbols missing from all available libraries |
| `remove_footprints_from_library` | Remove named footprints from a library directory and re-index it |
| `delete_footprint_library` | Delete an **empty** footprint library (directory + table entry + index) |

Empty-only deletion refuses to remove a library that still holds definitions. Only the library file/directory, its lib-table entry and its index rows are touched — never the schematic or board.

#### Wire Editing (`wire_edit_tools.py`)

| Tool | Purpose |
|------|---------|
| `connect_pins_with_wire` | Auto-route a wire between two named component pins |
| `connect_points_with_wire` | Route an orthogonal wire between two raw coordinates, adding junctions where needed |
| `delete_wire_from_schematic` | Remove a wire segment by start/end coordinates |

#### Project Query (`project_tools.py`)

| Tool | Purpose |
|------|---------|
| `get_project_structure` | Return project metadata, the sheet hierarchy and the local lib-table paths |

#### DRC (`drc_tools.py`)

| Tool | Purpose |
|------|---------|
| `run_drc_check` | Open the Design Rules Checker dialog in the running KiCad editor via IPC |

---

### 1.2 Defer — valid tools outside the plugin profile (1 tool)

These tools exist and are registered by the full profile, but the plugin profile does not expose them.

| Tool | Module | Reason Deferred |
|------|--------|----------------|
| `list_projects` | `project_tools.py` | Plugin provides project context directly; filesystem scanning is not needed |

---

### 1.3 Remove from the plugin-facing profile (8 tools)

These tools are excluded from the plugin profile: they depend on `kicad-cli` subprocesses, perform OS-level actions that belong to KiCad itself, or serve use cases not relevant to the plugin.

| Tool | Module | Reason |
|------|--------|--------|
| `open_project` | `project_tools.py` | Uses OS-level `open`/`xdg-open`; KiCad plugin manages project lifecycle |
| `validate_project` | `analysis_tools.py` | Superseded by netlist inspection; unclear CLI dependency |
| `get_drc_history` | `drc_tools.py` | Tool no longer exists; the resource helper is not a plugin tool |
| `generate_pcb_thumbnail` | `export_tools.py` | `kicad-cli` dependent |
| `analyze_bom` | `bom_tools.py` | `kicad-cli` dependent |
| `export_bom_csv` | `bom_tools.py` | `kicad-cli` dependent |
| `identify_circuit_patterns` | `pattern_tools.py` | Pattern recognition not needed for editing workflows |
| `validate_project_boundaries` | `validation_tools.py` | Not registered in `server.py`; not relevant to plugin editing profile |

---

## 2. Resources

**All resources are deferred from the plugin-facing profile.**

MCP resources are designed for MCP-client-driven browsing (e.g., Claude Desktop). The plugin profile is tools-only; the LLM gets data through tool calls, not resource URIs.

| Resource URI | Module | Reason Deferred |
|-------------|--------|----------------|
| `kicad://schematic/{schematic_path}` | `resources/files.py` | Replaced by `extract_schematic_netlist` tool |
| `kicad://project/{project_path}` | `resources/projects.py` | Replaced by plugin context bridge |
| `kicad://netlist/{schematic_path}` | `resources/netlist_resources.py` | Replaced by `extract_schematic_netlist` tool |
| `kicad://project_netlist/{project_path}` | `resources/netlist_resources.py` | Replaced by `extract_schematic_netlist`, which walks the full sheet hierarchy |
| `kicad://component/{schematic_path}/{component_ref}` | `resources/netlist_resources.py` | Replaced by `find_component_connections` tool |
| `kicad://bom/{project_path}` | `resources/bom_resources.py` | BOM features deferred |
| `kicad://bom/{project_path}/csv` | `resources/bom_resources.py` | BOM features deferred |
| `kicad://bom/{project_path}/json` | `resources/bom_resources.py` | BOM features deferred |
| `kicad://drc/history/{project_path}` | `resources/drc_resources.py` | DRC features deferred |
| `kicad://drc/{project_path}` | `resources/drc_resources.py` | DRC features deferred |
| `kicad://patterns/{schematic_path}` | `resources/pattern_resources.py` | Pattern features deferred |
| `kicad://patterns/project/{project_path}` | `resources/pattern_resources.py` | Pattern features deferred |

---

## 3. Prompts

**All prompts are deferred from the plugin-facing profile.**

MCP prompts are designed for standalone MCP clients (e.g., Claude Desktop) where the client selects a prompt to drive the conversation. The plugin owns the system prompt and conversation flow directly; pre-built MCP prompts are not used.

| Prompt | Module | Category |
|--------|--------|---------|
| `create_new_component` | `prompts/templates.py` | General |
| `debug_pcb_issues` | `prompts/templates.py` | PCB |
| `pcb_manufacturing_checklist` | `prompts/templates.py` | PCB |
| `fix_drc_violations` | `prompts/drc_prompt.py` | DRC |
| `custom_design_rules` | `prompts/drc_prompt.py` | DRC |
| `analyze_components` | `prompts/bom_prompts.py` | BOM |
| `cost_estimation` | `prompts/bom_prompts.py` | BOM |
| `bom_export_help` | `prompts/bom_prompts.py` | BOM |
| `component_sourcing` | `prompts/bom_prompts.py` | BOM |
| `bom_comparison` | `prompts/bom_prompts.py` | BOM |
| `analyze_circuit_patterns` | `prompts/pattern_prompts.py` | Pattern |
| `analyze_power_supplies` | `prompts/pattern_prompts.py` | Pattern |
| `analyze_sensor_interfaces` | `prompts/pattern_prompts.py` | Pattern |
| `analyze_microcontroller_connections` | `prompts/pattern_prompts.py` | Pattern |
| `find_and_improve_circuits` | `prompts/pattern_prompts.py` | Pattern |
| `compare_circuit_patterns` | `prompts/pattern_prompts.py` | Pattern |
| `explain_circuit_function` | `prompts/pattern_prompts.py` | Pattern |

---

## 4. Summary

| Category | Keep | Defer | Remove |
|----------|------|-------|--------|
| Tools | 32 | 1 | 8 |
| Resources | 0 | 12 | 0 |
| Prompts | 0 | 17 | 0 |
| **Total** | **32** | **30** | **8** |

Section 1.1 holds 32 tools: 23 were in the original milestone-1 schematic/symbol scope, the seven symbol- and footprint-library tools were added later and belong to the same surface, and `get_project_structure` and `run_drc_check` were reclassified from §1.2/§1.3 once the plugin profile started exposing them.
