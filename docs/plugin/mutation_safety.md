# Mutation Safety Design

## 1. Purpose

Every write operation performed by the MCP server modifies a KiCad source file on disk. This document defines the backup strategy, rollback expectations, user-visible reporting, and the preview/diff approach for the milestone-1 plugin integration.

---

## 2. Backup Behaviour

### 2.1 Current implementation

All skip-based edit tools in `symbol_edit_tools.py` and `wire_edit_tools.py` follow this pattern before every write:

```python
import shutil
backup_path = schematic_path + ".bak"
shutil.copy2(schematic_path, backup_path)
sch.write(schematic_path)
```

The backup is written **unconditionally** before every save, regardless of whether the mutation succeeds. This means:
- One backup per mutation (not a stack; newer calls overwrite the `.bak`)
- If the write fails, the original `.bak` still exists and is valid
- If multiple tool calls are made in one LLM turn, each overwrites the previous backup

### 2.2 Backup naming

| Original file | Backup |
|--------------|--------|
| `schematic.kicad_sch` | `schematic.kicad_sch.bak` |

### 2.3 Limitations

- Only one level of backup is kept per file. If the engineer runs three tool calls, only the state before the third call is recoverable without using a version control system.
- Backups are written to the same directory as the source file. If the project is on a read-only filesystem, the write will fail before any mutation occurs.

### 2.4 Recommendation

For milestone 1, the single-level `.bak` is sufficient. A future improvement would write timestamped backups (e.g., `.bak.20250101_143022`) to provide a short history without requiring git integration.

---

## 3. Rollback Expectations

### 3.1 What rollback means in milestone 1

"Rollback" means: restore the `.bak` file to replace the modified `.kicad_sch`, then reload KiCad.

The plugin does **not** provide an automated rollback button in milestone 1. The engineer must:
1. Close the reload prompt (do not reload KiCad's in-memory copy)
2. Manually rename `.kicad_sch.bak` to `.kicad_sch` in their file manager or terminal
3. Reload the project in KiCad

### 3.2 What the plugin should tell the engineer

When any mutation is applied, the plugin shows:
```
⚠ Schematic modified: schematic.kicad_sch
  Backup saved at: schematic.kicad_sch.bak
  [Reload Schematic]
```

This ensures the engineer always knows where the backup is.

### 3.3 Future: plugin-assisted rollback

A "Undo last edit" button can be added that:
1. Copies `.bak` back over the modified file
2. Triggers a KiCad reload

This is deferred because it requires tracking which file was last mutated and handling the case where the backup was overwritten by a subsequent call.

---

## 4. Partial Success

If an LLM turn involves multiple tool calls and one of them fails mid-sequence:

- All writes that succeeded before the failure are already on disk
- The backup from the **last successful write** is the best available restore point
- The LLM receives the failure result and should report it clearly: "I placed R5 successfully but the wire connection failed. The schematic has been partially modified."

The plugin shows each tool call result in the tool log so the engineer can see exactly which steps completed.

---

## 5. Dry-Run / Preview Mode

### 5.1 Milestone 1 decision: no preview

Milestone 1 does **not** implement a dry-run or diff preview. Reasons:
- KiCad's S-expression format does not have a simple human-readable diff representation
- Generating a meaningful preview (e.g., a rendered schematic thumbnail) would require `kicad-cli`, which is being removed
- The `.bak`-before-write pattern provides a sufficient safety net for an initial release

### 5.2 Future: structured change report

A later milestone can return a structured change report from each tool call:

```json
{
  "success": true,
  "changes": [
    {"type": "add_symbol", "reference": "C3", "position": [120, 95]},
    {"type": "add_wire", "from": [120, 95], "to": [130, 95]}
  ],
  "backup_path": "schematic.kicad_sch.bak"
}
```

The plugin would display this as a bulleted list in the conversation. This is achievable within the current `skip`-based architecture but requires each tool to return a richer response structure.

---

## 6. User-Visible Change Reporting

### 6.1 Current tool response shape

All tools return at minimum:
```json
{
  "success": true | false,
  "message": "Human-readable summary",
  "error": "<only present on failure>"
}
```

Some tools return additional fields (e.g., `reference`, `position`, `backup_path`). The standardized fields for milestone 1 should be:

```json
{
  "success": true,
  "message": "Added symbol C3 at position (120, 95)",
  "reference": "C3",
  "position": [120, 95],
  "backup_path": "/path/to/schematic.kicad_sch.bak",
  "file_modified": "/path/to/schematic.kicad_sch"
}
```

### 6.2 Plugin rendering

The plugin shows the `message` field in the tool call indicator in the conversation view. The `backup_path` and `file_modified` fields trigger the reload notice.

---

## 7. Audit of Existing Tools for Mutation Safety

| Module | Backup written? | File path in response? | Notes |
|--------|----------------|----------------------|-------|
| `symbol_edit_tools.py` | ✓ Yes (`.bak`) | Partial | Needs `backup_path` field added to all responses |
| `wire_edit_tools.py` | ✓ Yes (`.bak`) | Partial | Same as above |
| `netlist_tools.py` | N/A (read-only) | N/A | No mutations |
| `symbol_tools.py` | N/A (read-only) | N/A | No mutations; writes only to the symbol index SQLite DB |

Action items (Phase 3 of the implementation plan):
1. Standardize `backup_path` and `file_modified` fields in all write-tool responses
2. Confirm all write tools check for file existence before attempting backup
3. Add a check that backup write succeeded before proceeding to mutation

---

## 8. Concurrency and Locking

The MCP server does not implement file locking. If two tool calls attempt to write the same file simultaneously, the last write wins. In the milestone-1 design this is not a concern because:
- The LLM makes tool calls sequentially (not in parallel)
- Only one plugin instance connects to one server instance
- The engineer is working in one file at a time

Multi-user or multi-instance scenarios are out of scope for milestone 1.
