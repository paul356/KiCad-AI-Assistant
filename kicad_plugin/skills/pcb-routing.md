---
name: pcb-routing
priority: 95
description: "PCB pad-to-pad routing workflow: get_ratsnest, export_pcb_layer_image, pcb_route_pad_to_pad, corner_mode, failure retry"
---
# PCB routing workflow
Connect pads belonging to the same net with DRC-clean tracks.

> **If the active model is not a vision model, do NOT call export_pcb_layer_image.**

1. Call **get_ratsnest** to list the real unrouted pad pairs (world
   coordinates, net names).  Never guess pairs — use this data source.
2. Call **export_pcb_layer_image** to see the current board state: by default
   it renders all copper layers stacked in physical order (F.Cu red, B.Cu
   blue, inner layers green/amber/purple) plus courtyards, edge, and
   silkscreen on a dark KiCad-style background.  Pass ``layer="F.Cu"`` etc.
   for a single copper layer, or ``connect_pads=["J1.2", "J2.2"]`` to overlay
   green ratsnest lines on the pads that still need connecting.  The tool
   returns the board PNG — inspect the image before routing.
3. Connect ONE pad pair at a time with **pcb_route_pad_to_pad**:
   ``ref_a``/``pad_a``/``ref_b``/``pad_b``/``net`` are required; pass
   ``layer_hint`` for thru-hole pads, ``via_pairs`` to allow layer
   transitions, ``width`` when a non-netclass width is needed, and
   ``corner_mode`` to pick the corner style (``mitered45`` default,
   ``rounded45``/``rounded90``/``mitered90`` available).
4. On a route failure, read the error message, look at the latest layer
   render, and retry with a different ``layer_hint``, a different pair, or a
   via transition.  Do not silently repeat the same call.
5. Optionally add or delete vias with ``pcb_add_vias`` / ``pcb_delete_vias``
   after routing (e.g. ground stitching).

## How routing works (PNS engine, no grid)
Single-layer routing uses the built-in PNS engine (no A* grid path, no path
grid concept).  The route walks around fixed obstacles (pads, vias,
keepouts, other nets) and shoves movable tracks out of the way with chain
propagation; multi-layer routes still use the layer-stack A* planner.

### corner_mode strategy
- ``mitered45`` (default): 45-degree miter corners, straight segments.
- ``rounded45`` / ``rounded90``: rounded-corner arcs emitted as ``(arc ...)``
  track nodes on an unobstructed skeleton.  A detour or shove linearizes an
  arc back to segments (the response's ``arc_count`` drops to 0).
- ``mitered90``: Manhattan corners.
- The response echoes ``corner_mode`` and reports ``arc_count``/``arcs``
  (start/mid/end/width/layer/net) and ``shoved`` (pushed tracks as
  net/layer/width/points).
- If a rounded mode fails, retry with ``mitered45`` (arcs require the
  unobstructed skeleton that a detour destroys) or a different ``layer_hint``.