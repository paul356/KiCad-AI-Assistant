---
name: pcb-footprint-library
priority: 40
description: "Footprint index sync, library listing, search, and inspection"
---
- Build/refresh the footprint index (background) for a project:
  **sync_footprint_index(project_path?, force)** — ``project_path`` is
  optional: omit it to index only the global user/system libraries (useful
  when the user has no project fp-lib-table); pass a ``.kicad_pro``/``.kicad_pcb``
  to also index that project's own fp-lib-table entries, recorded per-project.
  Other projects' indexed libraries are never touched.  Check progress with
  **get_footprint_sync_status**.
- List libraries: **list_footprint_libraries(project_path?)** — omit
  ``project_path`` for global libraries only.
- Search footprints: **search_footprints(query, project_path?, limit)** —
  omit ``project_path`` to search global libraries only.
- Inspect pads/courtyard of a specific footprint:
  **get_footprint_details(library, name, project_path?)**; a project-owned
  library with the same nickname takes precedence over the global one.
  Omit ``project_path`` to look in global libraries only.
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
