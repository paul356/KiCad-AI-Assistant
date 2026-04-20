# Context Bridge Design

## 1. Purpose

The context bridge defines the minimum information the KiCad plugin collects from the active KiCad session and passes to the MCP server on every tool call. Without this, the MCP server has no reliable way to locate the correct schematic file, and the LLM cannot make contextually accurate edits.

---

## 2. Context Object

Every LLM request includes the following context in the system prompt:

```json
{
  "active_project": "/path/to/project.kicad_pro",
  "active_schematic": "/path/to/schematic.kicad_sch",
  "active_editor": "schematic",
  "selected_refs": ["R1", "C3"],
  "active_sheet": null
}
```

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `active_project` | `string \| null` | `pcbnew.GetBoard().GetFileName()` or scripting API | Absolute path to `.kicad_pro` |
| `active_schematic` | `string \| null` | Eeschema scripting API or derived from project path | Absolute path to `.kicad_sch` |
| `active_editor` | `"schematic" \| "pcb" \| "unknown"` | Plugin detects which KiCad editor is focused | Determines which tools the LLM should use |
| `selected_refs` | `string[]` | Scripting API selection query | Component references currently selected in the editor; may be empty |
| `active_sheet` | `string \| null` | Sheet path in hierarchical schematics | `null` for flat schematics; `"/"` for root; `"/Sheet1"` for subsheets |

---

## 3. How Context Is Collected

### 3.1 Project path
KiCad's scripting API exposes the active project. From Python inside KiCad:

```python
import pcbnew
board = pcbnew.GetBoard()
project_path = board.GetFileName()  # .kicad_pcb path; derive .kicad_pro from directory
```

For the schematic editor, the plugin registers as an `EeschemaPlugin` and uses the scripting console to query the active frame.

### 3.2 Active schematic path
Derived from the project directory: `<project_dir>/<project_name>.kicad_sch`. For multi-sheet schematics, this is always the top-level (root) schematic file. Hierarchical sheets are discovered by the MCP server via `skip` after the root path is supplied.

### 3.3 Selected components
The KiCad scripting API allows querying the current selection. On each request, the plugin calls:

```python
selected = [item.GetReference() for item in selection if hasattr(item, 'GetReference')]
```

If the scripting API is not available, `selected_refs` is an empty list.

### 3.4 Active editor detection
The plugin checks which KiCad frame is active and sets `active_editor` to `"schematic"` or `"pcb"`. This is used to filter which tool groups the LLM is told about.

---

## 4. How Context Is Passed to the LLM

The plugin inserts a context block into the system prompt before every request:

```
You are a KiCad schematic assistant. The engineer is working in the schematic editor.

Active project: /path/to/project.kicad_pro
Active schematic: /path/to/schematic.kicad_sch
Selected components: R1, C3

When editing the schematic, use /path/to/schematic.kicad_sch as the schematic_path
argument for all editing tools unless the engineer specifies a different file.
```

The full context JSON is also available as a `context` field if the LLM needs to reference it programmatically.

---

## 5. How Context Reaches MCP Tool Calls

The MCP tools accept explicit path arguments (`schematic_path`, `project_path`). The LLM is instructed by the system prompt to use the active paths from the context. The plugin does **not** inject context as MCP session metadata; it relies on the LLM to pass paths correctly as arguments.

This model:
- Keeps the MCP server stateless (no session state on the server side)
- Makes every tool call self-contained and auditable in the tool log
- Allows the engineer to override paths by asking the LLM explicitly

---

## 6. Multi-Sheet Schematics

KiCad schematics can have hierarchical sheets. The tool surface works as follows:

- All editing tools operate on a single `.kicad_sch` file at a time
- The `active_schematic` context always points to the **root** schematic
- The `extract_schematic_netlist` tool recursively follows sheet references and returns connectivity for the full hierarchy
- `find_component_connections(project_path, component_ref)` searches across all sheets in the project
- If the engineer is working on a specific subsheet, they should tell the LLM the subsheet name or path; the LLM can look up the file path from the netlist result

### Sheet path convention
KiCad uses a sheet path like `/Sheet1/U1` to identify components in hierarchical designs. The netlist tools return these paths. When the LLM needs to edit a component in a subsheet, it should:
1. Call `extract_project_netlist` to find which `.kicad_sch` file contains the component
2. Use that file as `schematic_path` for editing tools

---

## 7. Context When No Project Is Open

If the plugin cannot determine the active project (e.g., KiCad is open but no project is loaded):
- `active_project` and `active_schematic` are `null`
- The plugin shows a notice: "No active project detected. Open a project to use the assistant."
- Tool calls are blocked until context is available

---

## 8. Future Extension: PCB Context

When PCB editing support is added, the context object will extend to include:

```json
{
  "active_pcb": "/path/to/board.kicad_pcb",
  "active_editor": "pcb",
  "selected_refs": ["U1"],
  "pcb_layers": ["F.Cu", "B.Cu"]
}
```

The system prompt will switch to PCB-focused instructions and expose PCB tool groups. No changes to the context bridge protocol are required; the existing fields remain and new fields are added.

---

## 9. Replacing Directory-Scan Assumptions

The current MCP server uses `KICAD_SEARCH_PATHS` to scan for projects. In the plugin-facing profile:
- `KICAD_SEARCH_PATHS` is not used for tool calls; paths come from the plugin context instead
- `list_projects` and `get_project_structure` are deferred; the plugin provides project identity directly
- Symbol library discovery still uses `KICAD_USER_DIR` and `KICAD_SEARCH_PATHS` for the symbol index, which is acceptable (the symbol index is read-only discovery, not project-specific)
