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
