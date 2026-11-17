# Context Bridge Design

## 1. Purpose

The context bridge turns the plugin's canonical project state into the text
block injected into every LLM request. Collection happens in
`kicad_plugin/project_state.py` (see [project_state.md](project_state.md));
`kicad_plugin/context_bridge.py` is a pure formatter. It never imports
`pcbnew`, never reads window titles and keeps no cache.

## 2. Input

The formatter receives the validated schema-v2 record:

```json
{
  "version": 2,
  "written_at": "2026-11-17T09:12:33Z",
  "project_root": "/path/to/project",
  "profile_file": "project.kicad_pro",
  "pcb_files": ["hardware/main.kicad_pcb"],
  "schematic_files": ["project.kicad_sch", "hardware/main_bus.kicad_sch"]
}
```

## 3. Output

```
## Active KiCad Context
Project root: /path/to/project
Project file: /path/to/project/project.kicad_pro
Schematic files:
- /path/to/project/project.kicad_sch
- /path/to/project/hardware/main_bus.kicad_sch
PCB files:
- /path/to/project/hardware/main.kicad_pcb
```

Manifest entries are rendered as absolute paths under `Project root`, so the
assistant can pass them directly as `schematic_path` / `pcb_path` arguments.

The block names the project and the files that belong to it. It deliberately
does **not** name a "current" file: the state tracks the project, not which
editor has focus, and the plugin does not infer that the only schematic is
the one being edited.

## 4. Invalid State

When the record is missing, corrupt, `null`, or fails schema validation, the
formatter returns an empty string. The request carries no context block at
all — there is no `Active project: (none)` placeholder.

## 5. How Context Reaches MCP Tool Calls

The tools take explicit path arguments (`schematic_path`, `project_path`). A
tool call searches global libraries unless the caller passes `project_path`;
nothing is filled in on the way, so each call means exactly what its
arguments say. Because of that, the system prompt and the symbol/footprint
skills tell the assistant to pass the `Project root` shown in this block as
`project_path` whenever it works inside a project. Omitting `project_path`
therefore means global libraries only — it will not find project-local
libraries — and another project's libraries are never searched.

## 6. Multi-Sheet Schematics

- All editing tools operate on a single `.kicad_sch` file at a time.
- `schematic_files` lists every sheet file discovered under the project root;
  the engineer or the netlist tools identify which one to edit.
- `extract_schematic_netlist` recursively follows sheet references and returns
  connectivity for the full hierarchy.
- `find_component_connections(project_path, component_ref)` searches across
  all sheets in the project.
- If the engineer is working on a specific subsheet, they should tell the LLM
  the subsheet name or path; the LLM can look up the file path from the
  netlist result.

### Sheet path convention
KiCad uses a sheet path like `/Sheet1/U1` to identify components in
hierarchical designs. The netlist tools return these paths. When the LLM
needs to edit a component in a subsheet, it should:
1. Look up the `.kicad_sch` file for that sheet from the
   `extract_schematic_netlist` result — sub-sheets appear as `type="sheet"`
   components carrying their file paths
2. Use that file as `schematic_path` for editing tools