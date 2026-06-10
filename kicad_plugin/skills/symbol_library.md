---
name: symbol-library
priority: 80
description: "Symbol index sync, full-text search, pin details, bbox geometry in library space"
---
# Tools overview
- **sync_symbol_index** — Start background sync of the symbol index database.
- **get_symbol_sync_status** — Poll progress and completion of the background sync.
- **search_symbols** — Full-text search across all indexed KiCad symbols (names, descriptions, keywords).
- **get_symbol** — Look up a single symbol's metadata, pin count, and body_bbox (library Y-up space).
- **get_symbol_pins** — Return detailed pin info: number, name, electrical type, wire-exit direction.
- **list_symbol_libraries** — Browse the library tree (tables → libraries).
- **get_library_symbols** — Enumerate symbols within a specific library.
- **get_symbol_index_stats** — Summary statistics (library count, symbol count, last sync).

# Recommended workflow
1. **First time only**: call **sync_symbol_index()** to build the index. Returns immediately;
   the sync runs in a background thread. The first sync can take several minutes (parses all
   `.kicad_sym` files); subsequent calls are incremental.
2. Poll with **get_symbol_sync_status()** every few seconds. When `running` is false and
   `last_result` is present, the sync completed successfully. If `error` is present, it failed.
   Do NOT call `sync_symbol_index` again while a sync is already running.
3. **Always prefer search_symbols** for finding components:
   `search_symbols(query="NPN transistor", limit=50)` returns `library_name` and `name`
   fields ready to pass to placement tools.
4. When you need detailed placement geometry, call **get_symbol(library_name, symbol_name)**.
   Returns `body_bbox` (union of graphics + pin points, library Y-up coordinates) and
   `unit_bboxes` for multi-unit symbols.
5. When you need pin-level detail (direction, electrical type), call
   **get_symbol_pins(library_name, symbol_name)**. Also returns `body_bbox` and
   `unit_bboxes`. Pin directions are **wire-exit** directions in library Y-up space
   (KiCad raw pin angles are stub directions; this tool adds 180° automatically).
6. Use **list_symbol_libraries** and **get_library_symbols** only for browsing —
   they are less token-efficient than `search_symbols` for finding specific parts.

# Coordinate space conventions
- `get_symbol` and `get_symbol_pins` return geometry in **library coordinate space**
  (Y-up, mm). This is NOT the same as schematic world coordinates (Y-down).
- Pin direction strings: `"right"`, `"up"`, `"left"`, `"down"` describe wire-exit
  direction in library space (→ ↑ ← ↓).
- For world-space pin directions after placement, use `extract_schematic_netlist` and
  look at the `direction` field in each component's `pins` list.

# Library name format (critical)
- For KiCad 10 symdir-style libraries, `library_name` is `"TableName/FileBaseName"`
  (e.g. `"Device/R_Small"`), **not** just the table name (e.g. not `"Device"`).
- All library-name fields returned by search/lookup tools use this format and are
  ready to pass directly to `add_symbol_to_schematic`, `get_symbol`, etc.

# Caveats & gotchas
- `sync_symbol_index` returns `status: "already_running"` if called while a sync is
  in progress; use that response to check progress rather than spinning up a duplicate.
- `force=True` on `sync_symbol_index` reparses every library regardless of timestamp —
  only use when the database is corrupted.
- `get_symbol` only returns `body_bbox` when the raw `.kicad_sym` file can be parsed;
  symbols in some older library formats may not include geometry.
