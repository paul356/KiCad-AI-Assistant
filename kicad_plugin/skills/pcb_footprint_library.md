---
name: pcb-footprint-library
priority: 40
description: "Footprint index sync, library listing, search, and inspection"
tools: [sync_footprint_index, get_footprint_sync_status, list_footprint_libraries, list_library_footprints, search_library_footprints, get_library_footprint, find_footprints_not_in_libraries, create_footprint_library, add_footprints_to_library]
---
# Project scope (do this first)
When working inside a project, pass **`project_path` = the path shown as `Active project`**
in the conversation context to every footprint index build and lookup. Omitting
`project_path` searches global footprint libraries only and will not find libraries declared
in the project's own `fp-lib-table`; other projects are never searched.

- Build/refresh the footprint index (background) for a project:
  **sync_footprint_index(project_path=active_project, force=false)** — pass the
  project path to index global plus that project's own fp-lib-table entries,
  recorded per-project.  Omit ``project_path`` only for a global-only index
  (e.g. the user has no project fp-lib-table).  Other projects' indexed
  libraries are never touched.  Check progress with
  **get_footprint_sync_status**.
- List libraries: **list_footprint_libraries(project_path=active_project)** —
  omit ``project_path`` only for a global-only listing.
- List every footprint in one library: **list_library_footprints(library_name,
  project_path=active_project, max_results?, offset?)** — names plus light metadata
  (description, tags, attr, pad count, 3D model), name-ordered and pageable with
  ``max_results``/``offset`` (``next_offset`` is where the next page starts);
  use **get_library_footprint(library_name, footprint_name)** to inspect ONE
  footprint's pads/courtyard, and never pass a list of names to
  ``list_library_footprints``.
- Search footprints: **search_library_footprints(query,
  project_path=active_project, max_results?)** — omit ``project_path`` only
  for a global-only search.
- Inspect pads/courtyard of a specific footprint:
  **get_library_footprint(library_name, footprint_name,
  project_path=active_project)**; a project-owned library with the same
  nickname takes precedence over the global one.  Omit ``project_path`` only
  to look in global libraries.
- Find board footprints missing from every indexed library:
  **find_footprints_not_in_libraries(pcb_path)**.  Read-only: compares the footprints
  embedded in the board against the indexed footprint database scoped to the
  PCB's project directory (global + project libraries; falls back to a live
  fp-lib-table scan when the index is empty) and reports footprints with no
  same-named match.  Nothing is written to the footprint database.
- Create and register a footprint library:
  **create_footprint_library(name, project_dir?)**.  Without
  *project_dir*: creates ``<name>.pretty`` under
  ``${KICAD10_3RD_PARTY}/footprints`` and registers it in the global user
  fp-lib-table (``.bak`` backup, idempotent).  With *project_dir* (a project
  directory): creates ``<project_dir>/<name>.pretty`` and registers it in
  the project's ``fp-lib-table`` (created if absent) at
  ``${KIPRJMOD}/<name>.pretty``.  Either way the library is indexed
  immediately so library-list/search tools see it.  Library nicknames are
  globally unique: creating a name that already exists (in the index, any
  project, or any fp-lib-table) or whose directory already exists is
  refused.
- Export named board footprints into a footprint library:
  **add_footprints_to_library(pcb_path, footprints, library)**.  Writes
  each requested footprint (list from ``find_footprints_not_in_libraries``) as a
  ``.kicad_mod`` file into the target library directory, then updates the
  footprint database for exactly that library.  Only the listed footprints
  are considered — there is no "export everything" mode; call
  ``find_footprints_not_in_libraries`` first.  A footprint already present in
  another indexed library (or not on the board) is ``skipped``; if the
  target ``.kicad_mod`` file already exists in the library directory the
  export is ``failed`` and never overwrites it.  The board file is never
  modified.  Typical flow: ``find_footprints_not_in_libraries`` to preview,
  ``create_footprint_library`` once to set up a new library, then
  ``add_footprints_to_library`` to populate it.
- Project paths may be given as ``.kicad_pro`` or ``.kicad_pcb`` files; the
  project identity is always the realpath of the parent directory, so all
  tools agree on which project a library belongs to.  For
  ``create_footprint_library`` pass the project *directory* directly.
