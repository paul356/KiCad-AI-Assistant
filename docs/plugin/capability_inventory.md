# Capability Inventory

All capabilities currently registered in `kcaa/server.py` are classified here as **keep**, **defer**, or **remove** for the plugin-facing milestone-1 profile.

---

## 1. Tools

### 1.1 Keep for Milestone 1 (26 tools)

These tools are skip-based, have no `kicad-cli` dependency, and form the core of the plugin-facing editing surface.

#### Netlist / Inspection (`netlist_tools.py`)

| Tool | Purpose |
|------|---------|
| `extract_schematic_netlist` | Read connectivity and components from one `.kicad_sch` file |
| `extract_project_netlist` | Read connectivity across all sheets in a project |
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
| `remove_symbol_from_schematic` | Remove one or more symbols by reference |
| `set_symbol_property` | Update any property (value, footprint, datasheet, custom field) |
| `list_symbol_properties` | Read all properties of one component |
| `delete_symbol_property` | Remove a custom property |
| `move_component` | Reposition a component to new coordinates |
| `add_label_to_schematic` | Add a net label at a position |
| `list_labels_in_schematic` | Enumerate all labels in a schematic |
| `delete_label_from_schematic` | Remove a label by text and position |

All write operations create a `.bak` backup before writing.

#### Wire and Junction Editing (`wire_edit_tools.py`)

| Tool | Purpose |
|------|---------|
| `add_wire_to_schematic` | Draw a wire segment between two points |
| `connect_pins_with_wire` | Auto-route a wire between two named component pins |
| `delete_wire_from_schematic` | Remove a wire segment by start/end coordinates |
| `add_junction_to_schematic` | Add a junction dot at a point |
| `list_junctions_in_schematic` | Enumerate all junction positions |
| `delete_junction_from_schematic` | Remove a junction by position |

---

### 1.2 Defer — Review After Initial Plugin is Stable

These tools may be added back once the plugin is working end-to-end.

| Tool | Module | Reason Deferred |
|------|--------|----------------|
| `list_projects` | `project_tools.py` | Plugin provides project context directly; filesystem scanning is not needed |
| `get_project_structure` | `project_tools.py` | Same as above; context bridge covers this role |

**Decision needed:** once the plugin is stable, decide whether these are useful for LLM exploratory queries (e.g., "what other schematics are in this project?") or whether the plugin context always provides sufficient scope.

---

### 1.3 Remove from Plugin-Facing Profile

These tools depend on `kicad-cli`, perform OS-level actions that belong to KiCad itself, or serve use cases not relevant to the plugin.

| Tool | Module | Reason |
|------|--------|--------|
| `open_project` | `project_tools.py` | Uses OS-level `open`/`xdg-open`; KiCad plugin manages project lifecycle |
| `validate_project` | `analysis_tools.py` | Superseded by netlist inspection; unclear CLI dependency |
| `run_drc_check` | `drc_tools.py` | `kicad-cli` dependent; DRC is a separate milestone |
| `get_drc_history` | `drc_tools.py` | `kicad-cli` dependent |
| `generate_pcb_thumbnail` | `export_tools.py` | `kicad-cli` dependent |
| `generate_project_thumbnail` | `export_tools.py` | `kicad-cli` dependent |
| `analyze_bom` | `bom_tools.py` | `kicad-cli` dependent |
| `export_bom_csv` | `bom_tools.py` | `kicad-cli` dependent |
| `identify_circuit_patterns` | `pattern_tools.py` | Pattern recognition not needed for editing workflows |
| `analyze_project_circuit_patterns` | `pattern_tools.py` | Same as above |
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
| `kicad://project_netlist/{project_path}` | `resources/netlist_resources.py` | Replaced by `extract_project_netlist` tool |
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
| Tools | 26 | 2 | 11 |
| Resources | 0 | 12 | 0 |
| Prompts | 0 | 17 | 0 |
| **Total** | **26** | **31** | **11** |

The plugin-facing server profile registers exactly the 26 tools listed in section 1.1.
