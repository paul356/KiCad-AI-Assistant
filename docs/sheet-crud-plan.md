# Sheet Symbol CRUD: Implementation Plan

**Status:** Draft  
**Scope:** Add MCP tools for creating, reading, updating, and deleting hierarchical sheet symbols in `.kicad_sch` files.  
**Assumption:** We work at the S-expression level via `kicad-skip` (and raw S-expression construction where skip doesn't support sheets). IPC API integration is deferred—see research report for context.

---

## Architecture Summary

```
MCP Tool Layer (kcaa/tools/sheet_tools.py)
    │
    ├── safe_schematic(path)         ← kicad-skip (utf-8 compat)
    ├── S-expression construction    ← manual (skip has no sheet factory)
    ├── save_schematic(path, sch)    ← atomic write + .bak backup
    └── _generate_child_schematic()  ← creates empty .kicad_sch boilerplate

Plugin Integration
    ├── kicad_plugin/tool_registry.py   ← ToolPolicy entries
    └── kcaa/server.py                  ← register_sheet_tools(mcp)
```

---

## Tools Overview

| Tool | Kind | Purpose |
|------|------|---------|
| `list_sheet_symbols` | query | List all (sheet ...) entries in a schematic |
| `get_sheet_hierarchy` | query | Recursive tree of sheets starting from root |
| `add_sheet_symbol` | file_mutation | Create a sheet symbol referencing a child file |
| `remove_sheet_symbol` | file_mutation | Delete a sheet symbol by UUID or name |
| `update_sheet_symbol` | file_mutation | Modify sheet name, filename, position, or size |
| `add_sheet_pin` | file_mutation | Add a hierarchical pin to a sheet symbol |
| `remove_sheet_pin` | file_mutation | Delete a pin from a sheet symbol |
| `create_child_sheet` | file_mutation | Generate an empty child `.kicad_sch` file |

All mutation tools create a `.kicad_sch.bak` backup before writing. All coordinates are **mm, +Y down** (KiCad screen convention), snapped to the 1.27 mm grid.

---

## Tool Contracts

### 1. `list_sheet_symbols`

```python
async def list_sheet_symbols(
    schematic_path: str,
) -> dict[str, Any]:
    """List all hierarchical sheet symbols in a schematic.

    Iterates ``sch.sheet`` to extract every ``(sheet ...)`` S-expression
    entry.  For each sheet returns its UUID, name, referenced file, position,
    size, and pin count.

    Args:
        schematic_path: Absolute path to the target .kicad_sch file.

    Returns:
        dict with keys:
            success (bool)
            sheets (list[dict]): each entry has keys:
                uuid (str)           -- KIID of the sheet symbol
                sheet_name (str)     -- from ``property "Sheet name"``
                sheet_file (str)     -- from ``property "Sheet file"``
                position (dict)      -- ``{x, y}`` in mm
                size (dict)          -- ``{width, height}`` in mm
                pin_count (int)      -- number of hierarchical pins
            count (int)              -- total number of sheets found
            warnings (list[str])
    """
```

### 2. `get_sheet_hierarchy`

```python
async def get_sheet_hierarchy(
    schematic_path: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Build a recursive hierarchy tree of all sheets in a project.

    Starts at *schematic_path* (the root), reads its ``(sheet ...)`` entries,
    and recursively follows ``Sheet file`` properties to child ``.kicad_sch``
    files.  Resolves relative sheet-file paths against *project_dir* when
    provided; otherwise resolves relative to the parent file's directory.

    The returned tree is suitable for LLM navigation: it identifies which
    physical ``.kicad_sch`` file each sheet belongs to, lists all sheet pins
    (the connection points between parent and child), and reports reused sheets.

    Args:
        schematic_path: Absolute path to the root .kicad_sch file.
        project_dir: Directory to resolve relative sheet-file paths against.
            Defaults to the parent directory of *schematic_path*.

    Returns:
        dict with keys:
            success (bool)
            tree (dict): recursive structure
                root_name (str)       -- root sheet file basename
                children (list[dict]) -- top-level sheet instances:
                    sheet_name (str)
                    sheet_file (str)       -- target .kicad_sch filename
                    full_path (str)        -- resolved absolute path
                    sheet_path (str)       -- hierarchical path ("/root-uuid/sheet-uuid")
                    uuid (str)
                    position (dict)        -- {x, y} mm
                    size (dict)            -- {width, height} mm
                    pins (list[dict])      -- {name, shape, edge, position_mm}
                    page (str | None)
                    children (list[dict])  -- recursive subsheets
            flat_sheets (list[dict])  -- all sheets flattened
            reused_sheets (list[dict]) -- sheets referenced by multiple symbols
            max_depth (int)
            warnings (list[str])
    """
```

### 3. `add_sheet_symbol`

```python
async def add_sheet_symbol(
    schematic_path: str,
    sheet_name: str,
    sheet_file: str,
    x: float,
    y: float,
    width: float = 200.0,
    height: float = 150.0,
    pins: list[dict[str, Any]] | None = None,
    page_number: str | None = None,
    create_child: bool = False,
    project_name: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add a hierarchical sheet symbol to a schematic.

    Constructs a ``(sheet ...)`` S-expression and inserts it into the
    schematic.  The tool assigns a UUID, writes the ``Sheet name`` and
    ``Sheet file`` properties, and sets up the ``(instances ...)`` block.

    Coordinates are mm in KiCad screen convention (**+Y is down**) and are
    auto-snapped to the 1.27 mm (50-mil) grid.

    If *create_child* is True and the *sheet_file* does not exist, an empty
    child ``.kicad_sch`` file is generated automatically.  The child file is
    created relative to *schematic_path*.

    Sheet pins can be supplied via *pins*.  Each pin is positioned on a sheet
    edge using edge+distance rather than raw coordinates, making the tool
    LLM-friendly.  Use ``right``, ``left``, ``top``, or ``bottom`` edges.

    Args:
        schematic_path: Absolute path to the parent .kicad_sch file.
        sheet_name: Display name for the sheet symbol (becomes ``Sheet name``
            property, e.g. "Power Supply").
        sheet_file: Target .kicad_sch filename (e.g. "power.kicad_sch").
            Resolved relative to the parent file's directory.
        x: X position of the sheet symbol's top-left corner, in mm.
        y: Y position of the sheet symbol's top-left corner, in mm.
        width: Width of the sheet rectangle in mm.  Defaults to 200 mm
            (approximately KiCad's default sheet symbol width).
        height: Height of the sheet rectangle in mm.  Defaults to 150 mm.
        pins: Optional list of pin definitions for hierarchical connections.
            Each pin is a dict with:
                name (str)           -- pin name, must match a hierarchical
                                        label inside the child sheet
                shape (str)          -- "input", "output", "bidirectional",
                                        "tri_state", or "passive"
                edge (str)           -- "right", "left", "top", or "bottom"
                position_mm (float)  -- distance along the edge from the
                                        start (top/left corner), in mm; the
                                        tool snaps to the nearest valid grid
                                        position on that edge
        page_number: Optional page number string (e.g. "2") for the
            sheet instance.
        create_child: If True, automatically create the child .kicad_sch file
            when it does not exist.  Defaults to False.
        project_name: Project name for the sheet instance.  If omitted,
            derived from the project file name when possible.

    Returns:
        dict with keys:
            success (bool)
            sheet_uuid (str)       -- UUID of the created sheet symbol
            sheet_name (str)
            sheet_file (str)
            child_path (str | None) -- absolute path to child file if created
            position (dict)        -- {x, y} in mm (grid-snapped)
            size (dict)            -- {width, height} in mm
            pins_added (int)
            warnings (list[str])
            file_modified (str)
            backup_path (str)
    """
```

### 4. `remove_sheet_symbol`

```python
async def remove_sheet_symbol(
    schematic_path: str,
    identifier: str,
    by: str = "uuid",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Remove a hierarchical sheet symbol from a schematic.

    Deletes the ``(sheet ...)`` S-expression matching the given identifier.
    Does NOT delete the child ``.kicad_sch`` file — only removes the reference
    on the parent schematic.

    The sheet's pins and instance data are removed along with the symbol.
    A warning is issued if other sheet symbols in the same file still
    reference the same child file.

    Args:
        schematic_path: Absolute path to the target .kicad_sch file.
        identifier: The sheet to remove.  Can be a UUID or a sheet name.
        by: How to interpret *identifier* — ``"uuid"`` (default, matches the
            sheet symbol's ``(uuid ...)`` token) or ``"name"`` (matches the
            ``Sheet name`` property value, case-sensitive).

    Returns:
        dict with keys:
            success (bool)
            removed (dict)         -- the deleted sheet's pre-removal data
                uuid (str)
                sheet_name (str)
                sheet_file (str)
                pin_count (int)
            other_references (list[str] | None)
                -- UUIDs of other sheet symbols referencing the same file
            warnings (list[str])
            file_modified (str)
            backup_path (str)
    """
```

### 5. `update_sheet_symbol`

```python
async def update_sheet_symbol(
    schematic_path: str,
    sheet_uuid: str,
    sheet_name: str | None = None,
    sheet_file: str | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    page_number: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update properties of an existing hierarchical sheet symbol.

    Only the fields explicitly provided are changed; all others are preserved.
    When size changes, existing pins are repositioned proportionally to stay
    on the same relative edge positions.

    Args:
        schematic_path: Absolute path to the target .kicad_sch file.
        sheet_uuid: UUID of the sheet symbol to update.
        sheet_name: New display name (``Sheet name`` property).  If None,
            unchanged.
        sheet_file: New target filename (``Sheet file`` property).  If None,
            unchanged.  WARNING: if the file does not exist and *sheet_name*
            does not change, the tool will still accept the new filename but
            issue a warning.
        x: New X position in mm.  If None, unchanged.
        y: New Y position in mm.  If None, unchanged.
        width: New width in mm.  If None, unchanged.
        height: New height in mm.  If None, unchanged.
        page_number: New page number.  If None, unchanged.

    Returns:
        dict with keys:
            success (bool)
            sheet_uuid (str)
            changed_fields (list[str])
            pins_repositioned (int)   -- number of pins adjusted for new size
            warnings (list[str])
            file_modified (str)
            backup_path (str)
    """
```

### 6. `add_sheet_pin`

```python
async def add_sheet_pin(
    schematic_path: str,
    sheet_uuid: str,
    name: str,
    shape: str = "bidirectional",
    edge: str = "right",
    position_mm: float = 0.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add a hierarchical pin to an existing sheet symbol.

    Positions the pin on the specified edge of the sheet rectangle.
    *position_mm* is the distance along the edge from the start
    (left edge for top/bottom, top edge for left/right), snapped to
    1.27 mm grid.

    Args:
        schematic_path: Absolute path to the target .kicad_sch file.
        sheet_uuid: UUID of the sheet symbol to add the pin to.
        name: Pin name — must match a hierarchical label inside the child
            sheet for correct connectivity.
        shape: Electrical shape — one of ``"input"``, ``"output"``,
            ``"bidirectional"``, ``"tri_state"``, ``"passive"``.
            Defaults to ``"bidirectional"``.
        edge: Which edge of the sheet rectangle to place the pin on —
            ``"right"``, ``"left"``, ``"top"``, or ``"bottom"``.
            Defaults to ``"right"``.
        position_mm: Distance along the edge in mm.  On left/right edges this
            is distance from the top; on top/bottom edges, distance from the
            left.  Auto-snapped to 1.27 mm grid.

    Returns:
        dict with keys:
            success (bool)
            pin_uuid (str)
            sheet_uuid (str)
            pin_name (str)
            edge (str)
            position (dict)  -- absolute {x, y} mm
            warnings (list[str])
            file_modified (str)
            backup_path (str)
    """
```

### 7. `remove_sheet_pin`

```python
async def remove_sheet_pin(
    schematic_path: str,
    sheet_uuid: str,
    pin_name: str | None = None,
    pin_uuid: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Remove a hierarchical pin from a sheet symbol.

    Identify the pin by either its *pin_name* (case-sensitive) or *pin_uuid*.
    If both are provided, *pin_uuid* takes precedence.

    Args:
        schematic_path: Absolute path to the target .kicad_sch file.
        sheet_uuid: UUID of the sheet symbol containing the pin.
        pin_name: Name of the pin to remove.  Case-sensitive.
            Use when pins have unique names.  Ignored if *pin_uuid* is set.
        pin_uuid: UUID of the pin to remove.  Preferred when available.

    Returns:
        dict with keys:
            success (bool)
            removed_pin (dict | None) -- {name, uuid, edge, position}
            warnings (list[str])
            file_modified (str)
            backup_path (str)
    """
```

### 8. `create_child_sheet`

```python
async def create_child_sheet(
    schematic_path: str,
    child_filename: str,
    paper: str = "A4",
    title: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create an empty child .kicad_sch file suitable for hierarchical sheets.

    Generates a minimal valid ``.kicad_sch`` file with the required boilerplate:
    ``(kicad_sch ...)`` header, empty ``(lib_symbols)``, ``(sheet_instances)``
    with root path, and ``(embedded_fonts no)``.  The file is created adjacent
    to *schematic_path* unless *child_filename* is an absolute path.

    Use this before calling ``add_sheet_symbol`` when the child schematic does
    not yet exist — or set ``create_child=True`` on ``add_sheet_symbol`` to
    handle both steps at once.

    Args:
        schematic_path: Absolute path to the parent .kicad_sch file (used to
            resolve relative *child_filename* directories).
        child_filename: Name of the new .kicad_sch file.  If it does not end
            with ``.kicad_sch`` the extension is appended.  Relative paths are
            resolved against the directory of *schematic_path*.
        paper: Paper size — one of ``"A4"``, ``"A3"``, ``"A2"``, ``"USLetter"``,
            ``"USLegal"``, ``"A"``, ``"B"``, ``"C"``, ``"D"``, ``"E"``.
            Defaults to ``"A4"``.
        title: Optional title for the sheet.  When provided, a ``title_block``
            token is included.

    Returns:
        dict with keys:
            success (bool)
            child_path (str)       -- absolute path to the created file
            uuid (str)             -- the root UUID of the new schematic
            paper (str)
            warnings (list[str])
    """
```

---

## Edge-Based Pin Positioning

Sheet pins are placed on edges rather than raw coordinates.  This is the same approach used by `kicad-sch-api`'s `SheetManager` and is more LLM-friendly.

```
       position_mm →
    ┌──────────────────┐  ← top edge
    │  pin "CLK"       │
    │                  │
    │  ↑               │  ↑
    │  position_mm     │  │ position_mm
    │  ↓               │  │ (from top)
 l  │                  │  r
 e  │                  │  i
 f  │                  │  g
 t  │                  │  h
    │                  │  t
    │          pin "D" │
    └──────────────────┘  ← bottom edge
       position_mm →
```

| Edge | Pin Angle | Position_mm measured from | Coordinate mapping |
|------|-----------|--------------------------|--------------------|
| `right` | 0° | Top of sheet | (x + width, y + position_mm) |
| `left` | 180° | Top of sheet | (x, y + position_mm) |
| `bottom` | 270° | Left of sheet | (x + position_mm, y + height) |
| `top` | 90° | Left of sheet | (x + position_mm, y) |

---

## S-Expression: Sheet Template

```lisp
(sheet (at <x_mm> <y_mm>) (size <w_mm> <h_mm>)
  (stroke (width 0) (type default))
  (fill (type none))
  (uuid "<generated-uuid>")
  (property "Sheet name" "<sheet_name>" (at <px> <py> 0)
    (effects (font (size 1.27 1.27)) (justify left)))
  (property "Sheet file" "<filename>" (at <fx> <fy> 0)
    (effects (font (size 1.27 1.27)) (justify left)))
  (pin "<name>" <shape> (at <px> <py> <angle>) (uuid "<generated>")
    (effects (font (size 1.27 1.27)) (justify <justify>)))
  (instances
    (project "<project_name>"
      (path "<uuid-path>" (page "<page_num>"))))
)
```

Property label positions are offset from the sheet symbol's top-left corner:
- `Sheet name`: (x + 5, y + 5) mm
- `Sheet file`: (x + 5, y + 10) mm

---

## Child File Template

```lisp
(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")
  (uuid "<generated-root-uuid>")
  (paper "<paper>")
  (lib_symbols)
  (sheet_instances
    (path "/<root-uuid>" (page "1"))
  )
  (embedded_fonts no)
)
```

---

## File Organization

```
kcaa/
├── tools/
│   └── sheet_tools.py          ← NEW: all sheet CRUD tool functions
│       ├── register_sheet_tools(mcp)
│       ├── _do_add_sheet()
│       ├── _do_remove_sheet()
│       ├── _do_update_sheet()
│       ├── _do_add_sheet_pin()
│       ├── _do_remove_sheet_pin()
│       ├── _generate_child_schematic()
│       ├── _build_hierarchy_tree()
│       ├── _pin_edge_to_coords()
│       ├── _parse_sheet_from_sexp()
│       └── _construct_sheet_sexp()
│
├── server.py                   ← add: register_sheet_tools(mcp)
│
kicad_plugin/
└── tool_registry.py            ← add: ToolPolicy entries for all sheet tools

tests/
└── integration/
    ├── fixtures/
    │   ├── parent_sheets.kicad_sch   ← NEW: parent with (sheet ...) entries
    │   └── child_sheet.kicad_sch      ← NEW: minimal child schematic
    └── test_sheet_tools.py            ← NEW: integration tests
```

---

## Integration Checklist

### `kcaa/server.py` — Register new module

In `_register_plugin_profile()` (after `register_placement_helpers`):

```python
register_sheet_tools(mcp)
```

### `kicad_plugin/tool_registry.py` — Add policies

```python
"list_sheet_symbols": ToolPolicy(kind="query"),
"get_sheet_hierarchy": ToolPolicy(kind="query"),
"add_sheet_symbol": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=True,
    mark_dirty=True,
),
"remove_sheet_symbol": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=True,
    mark_dirty=True,
),
"update_sheet_symbol": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=True,
    mark_dirty=True,
),
"add_sheet_pin": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=True,
    mark_dirty=True,
),
"remove_sheet_pin": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=True,
    mark_dirty=True,
),
"create_child_sheet": ToolPolicy(
    kind="file_mutation",
    path_arg="schematic_path",
    auto_snapshot=False,
    mark_dirty=False,
),
```

---



## Todo Items

### Phase 1: Read-Only Query Tools

| # | ID | Task | Dependencies |
|---|-----|------|-------------|
| 1 | `sheet-file-gen` | Building child sheet file generation helper | — |
| 2 | `sheet-read-tools` | Building sheet query tools (`list_sheet_symbols`, `get_sheet_hierarchy`) | `sheet-file-gen` |

### Phase 2: Sheet CRUD

| # | ID | Task | Dependencies |
|---|-----|------|-------------|
| 3 | `sheet-create-tool` | Building `add_sheet_symbol` tool | `sheet-read-tools`, `sheet-file-gen` |
| 4 | `sheet-remove-tool` | Building `remove_sheet_symbol` tool | `sheet-create-tool` |
| 5 | `sheet-update-tool` | Building `update_sheet_symbol` tool | `sheet-create-tool` |

### Phase 3: Sheet Pin Management

| # | ID | Task | Dependencies |
|---|-----|------|-------------|
| 6 | `sheet-pin-tools` | Building sheet pin management tools (`add_sheet_pin`, `remove_sheet_pin`) | `sheet-create-tool` |

### Phase 4: Integration & Tests

| # | ID | Task | Dependencies |
|---|-----|------|-------------|
| 7 | `sheet-integration` | Integrating sheet tools into server and plugin | `sheet-create-tool` |
| 8 | `sheet-tests` | Writing integration tests for sheet tools | All of the above |

---

## Design Decisions

1. **No IPC API dependency.** All tools operate on `.kicad_sch` S-expression files directly via kicad-skip for reading + manual S-expression construction for writing.  This avoids waiting for the upstream IPC API (which has no timeline).

2. **Edge-based pin positioning.** Sheet pin positions are specified by edge name + distance, not raw coordinates.  This is proven by `kicad-sch-api` and is more usable for LLM-driven placement.

3. **Child file generation is explicit.** `create_child_sheet` is a separate tool from `add_sheet_symbol` (with `create_child=True` convenience flag).  This lets the LLM create sheets without committing to children, or generate children independently for reuse workflows.

4. **Single-file mutations only.** Each tool operates on one `.kicad_sch` file.  Multi-file coordination (create parent + add sheet + populate child) is the LLM's responsibility — this matches the existing pattern where the LLM chains tools.

5. **Pattern match on existing codebase.** All tools follow the same patterns as `symbol_edit_tools.py`: async def, `@mcp.tool()`, `dict[str, Any]` return, `safe_schematic` + `save_schematic`, error-first returns.
