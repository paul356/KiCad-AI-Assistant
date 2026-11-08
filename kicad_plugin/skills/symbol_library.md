---
name: symbol-library
priority: 80
description: "Symbol index sync, full-text search, pin details, bbox geometry in library space"
tools: [sync_symbol_index, get_symbol_sync_status, search_library_symbols, get_library_symbol, get_library_symbol_pins, list_symbol_libraries, list_library_symbols, get_symbol_index_stats]
---
# Tools overview
- **sync_symbol_index(project_path?)** — Start background sync of the symbol index database.
- **get_symbol_sync_status()** — Poll progress and completion of the background sync.
- **search_library_symbols(query, project_path?)** — Full-text search across all indexed KiCad symbols (names, descriptions, keywords).
- **get_library_symbol(library_name, symbol_name, project_path?)** — Look up a single symbol's metadata, pin count, and body_bbox (library Y-up space).
- **get_library_symbol_pins(library_name, symbol_name, project_path?)** — Return detailed pin info: number, name, electrical type, wire-exit direction.
- **list_symbol_libraries(project_path?)** — List all indexed symbol libraries (flat, paginated).
- **list_library_symbols(library_name, project_path?)** — Enumerate symbols within a specific library.
- **get_symbol_index_stats(project_path?)** — Summary statistics (library count, symbol count, last sync).

# Project scope (do this first)
When working inside a project, pass **`project_path` = the path shown as `Active project`**
in the conversation context to every symbol lookup and to the index sync. Omitting
`project_path` searches global libraries only and will not find libraries declared in the
project's own `sym-lib-table`; other projects are never searched. Call tools again with
`project_path` when a search returns nothing but the part should be project-local.

# Recommended workflow
1. **First time only**: call **sync_symbol_index(project_path=active_project)** to build the index. Returns immediately;
   the sync runs in a background thread. The first sync can take several minutes (parses all
   `.kicad_sym` files); subsequent calls are incremental.
2. Call **get_symbol_sync_status()** ONCE — the plugin client blocks on that
   call until the background sync finishes (progress notices appear in the UI)
   and returns the final snapshot, so there is no need to poll repeatedly.
   If the returned snapshot still shows `running: true` (e.g. past a long
   client timeout), you may call it again.  Completion: `running` false with
   `last_result` present.  Failure: `error` present.  Do NOT call
   `sync_symbol_index` again while a sync is already running.
3. **Always prefer search_library_symbols** for finding components:
   `search_library_symbols(query="NPN transistor", project_path=active_project, limit=50)`
   returns `library_name` and `name` fields ready to pass to placement tools.
4. When you need clearance or `body_bbox` geometry in **library space** (Y-up),
   call **get_library_symbol(library_name, symbol_name, project_path=active_project)**. Returns `body_bbox`
   (union of graphics + pin points, library Y-up coordinates) and
   `unit_bboxes` for multi-unit symbols. This is the library **definition**;
   it returns **no** board or schematic placement.
5. When you need pin-level detail (direction, electrical type), call
   **get_library_symbol_pins(library_name, symbol_name, project_path=active_project)**. Also returns `body_bbox` and
   `unit_bboxes`. Pin directions are **wire-exit** directions in library Y-up space
   (KiCad raw pin angles are stub directions; this tool adds 180° automatically).
6. Use **list_symbol_libraries(project_path=active_project)** and
   **list_library_symbols(library_name, project_path=active_project)** only for browsing —
   they are less token-efficient than `search_library_symbols` for finding specific parts.

# Coordinate space conventions
- `get_library_symbol` and `get_library_symbol_pins` return geometry in **library coordinate space**
  (Y-up, mm). This is NOT the same as schematic world coordinates (Y-down).
- Pin direction strings: `"right"`, `"up"`, `"left"`, `"down"` describe wire-exit
  direction in library space (→ ↑ ← ↓).
- For world-space pin directions after placement, use `extract_schematic_netlist` and
  look at the `direction` field in each component's `pins` list.

# Library name format (critical)
- For KiCad 10 symdir-style libraries, `library_name` is `"TableName/FileBaseName"`
  (e.g. `"Device/R_Small"`), **not** just the table name (e.g. not `"Device"`).
- All library-name fields returned by search/lookup tools use this format and are
  ready to pass directly to `add_symbol_to_schematic`, `get_library_symbol`, etc.

# Caveats & gotchas
- `sync_symbol_index` returns `status: "already_running"` if called while a sync is
  in progress; use that response to check progress rather than spinning up a duplicate.
- `force=True` on `sync_symbol_index` reparses every library regardless of timestamp —
  only use when the database is corrupted.
- `get_library_symbol` only returns `body_bbox` when the raw `.kicad_sym` file can be parsed;
  symbols in some older library formats may not include geometry.
