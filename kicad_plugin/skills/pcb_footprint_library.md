---
name: pcb-footprint-library
priority: 40
description: "Footprint index sync, library listing, search, and inspection"
---
- Build/refresh the footprint index (background) for a project:
  **sync_footprint_index(project_path, force)** — ``project_path`` is
  required.  Indexes global user/system libraries plus the project's own
  fp-lib-table entries, recorded per-project.  Other projects' indexed
  libraries are never touched.  Check progress with
  **get_footprint_sync_status**.
- List libraries (scoped to a project): **list_footprint_libraries(project_path)**.
- Search footprints (scoped to a project): **search_footprints(query, project_path, limit)**.
- Inspect pads/courtyard of a specific footprint (scoped to a project):
  **get_footprint_details(library, name, project_path)**; a project-owned
  library with the same nickname takes precedence over the global one.
- Find board footprints missing from every indexed library:
  **find_missing_footprints(pcb_path)**.  Read-only: compares the footprints
  embedded in the board against the indexed footprint database scoped to the
  PCB's project directory (global + project libraries; falls back to a live
  fp-lib-table scan when the index is empty) and reports footprints with no
  same-named match.  Nothing is written to the footprint database.
- Create and register a 3rdparty library:
  **create_3rdparty_footprint_library(name)**.  Creates
  ``<name>.pretty`` under ``${KICAD10_3RD_PARTY}/footprints``, registers it in
  the global user fp-lib-table (``.bak`` backup, idempotent), and indexes
  exactly that library in the footprint database so library-list/search
  tools see it immediately.  Library nicknames are globally unique:
  creating a name that already exists (in the index, any project, or the
  global fp-lib-table) or whose directory already exists is refused.
  Project-local fp-lib-table files are never modified.
- Export board footprints missing from libraries into a 3rdparty library:
  **add_footprints_to_3rdparty_library(pcb_path, library)**.  Writes each
  missing footprint as a ``.kicad_mod`` file into the target library
  directory (footprints already present are skipped, never overwritten),
  then updates the footprint database for exactly that library.  The board
  file is never modified.  "Already present" is judged from the same indexed
  database as ``find_missing_footprints`` (build the index first).  Typical
  flow: ``find_missing_footprints`` to preview,
  ``create_3rdparty_footprint_library`` once to set up a new library, then
  ``add_footprints_to_3rdparty_library`` to populate it.
- Project paths may be given as ``.kicad_pro`` or ``.kicad_pcb`` files; the
  project identity is always the realpath of the parent directory, so all
  tools agree on which project a library belongs to.
