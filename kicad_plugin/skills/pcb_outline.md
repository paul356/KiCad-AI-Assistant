---
name: pcb-outline
priority: 40
description: "Board Edge.Cuts creation and editing workflow"
---
# Board outline workflow
- Query current Edge.Cuts geometry: **get_board_outline**.
- Replace the entire outline with a rectangle: **set_board_outline_rect(
  pcb_path, x, y, width, height, line_width, corner_radius)**.
  ``corner_radius=0`` emits a single gr_rect; positive value draws four
  lines + four 90° arcs.  Edge.Cuts line width is typically 0.05 mm.
- Add individual segments or arcs: **add_board_outline_segment** /
  **add_board_outline_arc**.  Wipe first with **clear_board_outline**.
- Arc angles: 0° is +X, angles increase clockwise.
