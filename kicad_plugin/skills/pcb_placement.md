---
name: pcb-placement
priority: 60
description: "Footprint positioning, overlap check, group align/distribute operations"
---
# PCB placement workflow
- Before placing, call **get_board_info** + **list_footprints** to understand
  the current layout.
- Use **get_footprint_bbox** (batch: `references` list) to get the courtyard
  bounding box of one or more footprints in world coordinates.  Use this to
  check for overlaps before positioning.
- Use **get_board_bounding_box** to get the union bbox of all footprint
  courtyards — useful for sizing the board outline around all components.
- Move or rotate footprints: **set_footprint_position(pcb_path, items)**.
  Each ``items`` entry is ``{"reference", "x"?, "y"?, "rotation"?}`` (e.g.
  ``[{"reference": "U1", "x": 100.0, "y": 50.0}, {"reference": "R2",
  "rotation": 90}]``).  Any of x/y/rotation omitted (or ``null``) in an
  entry leaves that coordinate unchanged; at least one must be provided per
  entry.  If a requested position causes a courtyard collision, the tool
  automatically adjusts to the nearest free spot; if none is found within
  20 mm, that footprint keeps a per-reference error while the rest of the
  batch is still applied.
- Flip a footprint between F.Cu and B.Cu: **flip_footprint(pcb_path,
  reference)**.  All child layer items are updated automatically.
- Update a footprint property (Reference, Value, Datasheet, or custom field)
  on one or more footprints: **set_footprint_property(pcb_path, items)**.
  Each ``items`` entry is ``{"reference", "property_name", "value"}``, so
  targets can carry different property/value pairs in one call.

# PCB group operations
- **align_footprints(pcb_path, references, axis, coordinate)** — align all
  listed footprints to the same X or Y.  ``coordinate=null`` uses the mean.
- **distribute_footprints(pcb_path, references, axis)** — evenly space ≥3
  footprints along X or Y; outermost positions are fixed.
- **move_footprints_by_delta(pcb_path, references, dx, dy)** — shift a group
  by the same offset without changing their relative positions.

# Adding / removing footprints
- **add_footprints_to_pcb(pcb_path, footprints)** — place several footprints
  in one call.  Each ``footprints`` entry is ``{"footprint", "reference",
  "x", "y", "nets", "rotation"?: 0}`` where ``footprint`` is
  ``"Library:Name"`` or a bare ``"Name"`` searched across fp-lib-table
  libraries, and ``nets`` maps EVERY pad number (``"1"``, ``"2"``, ...) to
  that pad's net name (``""`` = net 0 / unconnected).  A pad missing from
  ``nets`` is a hard error for that item — a partial nets dict can never
  silently land an uncovered pad on net 0 and short the part.  A net name
  not in the board's net list is auto-added; unknown nets are never
  created silently.  Unresolvable footprint, duplicate reference, missing
  x/y, or unsafe names fail that item only; successful items are written
  one at a time (atomic save + .bak).  Returns ``{success, results[],
  placed_count, failed_count, failed[]}``.
- **remove_footprints_from_pcb(pcb_path, references)** — remove footprints
  by reference designator, all in one call.  Board loaded once; each
  reference removed once in order; a repeated reference counts as not
  found.  One atomic save + .bak only when at least one footprint was
  removed; an all-not-found batch leaves the file untouched (``backup_path:
  None``).  Dangling net definitions are left in place (KiCad tolerates
  them; rewriting the net table risks breaking surviving footprints).
  Returns ``{success, results[], removed_count, not_found_count,
  not_found[], backup_path, pcb_path}``.
