---
name: pcb-footprint-library
priority: 40
description: "Footprint index sync, library listing, search, and inspection"
---
# Footprint library workflow
- Build/refresh the footprint index (background):
  **sync_footprint_index(project_path, force)**.  Check progress with
  **get_footprint_sync_status**.
- List libraries: **list_footprint_libraries(project_path)**.
- Search footprints: **search_footprints(query, project_path, limit)**.
- Inspect pads/courtyard of a specific footprint:
  **get_footprint_details(library, name)**.
- Find board footprints missing from every indexed library:
  **find_missing_footprints(pcb_path)**.  Read-only: compares the footprints
  embedded in the board against the effective fp-lib-table library list
  (project-local table if present, plus the global user table, system
  libraries included) and reports footprints with no same-named match
  anywhere.  The comparison is done in memory — nothing is written to the
  footprint database.
- Create and register a 3rdparty library:
  **create_3rdparty_footprint_library(name)**.  Creates
  ``<name>.pretty`` under ``${KICAD10_3RD_PARTY}/footprints``, registers it in
  the global user fp-lib-table (``.bak`` backup, idempotent), and indexes
  exactly that library in the footprint database so library-list/search
  tools see it immediately.  Project-local fp-lib-table files are never
  modified.
- Export board footprints missing from libraries into a 3rdparty library:
  **add_footprints_to_3rdparty_library(pcb_path, library)**.  Writes each
  missing footprint as a ``.kicad_mod`` file into the target library
  directory (footprints already present are skipped, never overwritten),
  then updates the footprint database for exactly that library.  The board
  file is never modified.  Typical flow: ``find_missing_footprints`` to
  preview, ``create_3rdparty_footprint_library`` once to set up a new
  library, then ``add_footprints_to_3rdparty_library`` to populate it.
- Project-local fp-lib-table libraries are only ever *read* by these tools
  (so footprints they contain are not reported as missing); they are never
  indexed into the footprint database unless you explicitly run
  **sync_footprint_index(project_path, force)** with the project path.
