---
name: pcb-placement
priority: 60
description: "Footprint positioning, overlap check, group align/distribute operations"
---
# PCB placement workflow
- Before placing, call **get_board_info** + **list_footprints** to understand
  the current layout.
- Use **get_footprint_bbox** to get a footprint's courtyard bounding box in
  world coordinates.  Use this to check for overlaps before positioning.
- Use **get_board_bounding_box** to get the union bbox of all footprint
  courtyards — useful for sizing the board outline around all components.
- Move or rotate a single footprint: **set_footprint_position(pcb_path,
  reference, x, y, rotation)**.  Any argument may be ``null`` to leave it
  unchanged; at least one must be provided.  If the requested position causes
  a courtyard collision, the tool automatically adjusts to the nearest free
  spot; if none is found within 20 mm, an error is returned.
- Flip a footprint between F.Cu and B.Cu: **flip_footprint(pcb_path,
  reference)**.  All child layer items are updated automatically.
- Update a footprint property (Reference, Value, Datasheet, or custom field):
  **set_footprint_property(pcb_path, reference, property_name, value)**.

# PCB group operations
- **align_footprints(pcb_path, references, axis, coordinate)** — align all
  listed footprints to the same X or Y.  ``coordinate=null`` uses the mean.
- **distribute_footprints(pcb_path, references, axis)** — evenly space ≥3
  footprints along X or Y; outermost positions are fixed.
- **move_footprints_by_delta(pcb_path, references, dx, dy)** — shift a group
  by the same offset without changing their relative positions.
