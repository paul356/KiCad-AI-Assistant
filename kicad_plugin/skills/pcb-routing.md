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
   ``layer_hint`` for thru-hole pads, ``width`` when a non-netclass
   width is needed, and ``algorithm`` to pick the engine: ``astar``
   (default) for grid A*, ``pns`` for the walkaround + shove engine.
   Advanced knobs go in the optional ``options`` dict — omit it for
   defaults: ``options={"corner_mode": ...}`` (default ``rounded45``),
   ``options={"via_pairs": (("F.Cu", "B.Cu"),)}`` to allow layer
   transitions, ``options={"turn_penalty": 0.0}`` for pure
   shortest-path routing.
4. On a route failure, read the error message, look at the latest layer
   render, and retry with a different ``layer_hint``, a different pair, or a
   via transition.  Do not silently repeat the same call.
5. Optionally add or delete vias with ``pcb_add_vias`` / ``pcb_delete_vias``
   after routing (e.g. ground stitching).

## How routing works (algorithm switch)
``pcb_route_pad_to_pad`` takes an ``algorithm`` argument; a single route
always uses exactly one algorithm.  The response echoes ``algorithm``.

- ``astar`` (default): grid-based A* planner.  Single-layer routes run
  hierarchical grid A* (coarse pass + fine band); multi-layer routes run
  multi-layer A* with via edges.  This is the classic router behaviour and
  emits straight segments only — rounded-corner arcs are a PNS feature.
- ``pns``: walkaround + shove engine (no A* grid).  The route walks around
  fixed obstacles (pads, vias, keepouts, other nets) and shoves movable
  tracks out of the way with chain propagation.  A single-layer route may
  emit rounded-corner arcs.  A multi-layer ``pns`` route resolves the
  shortest start -> end layer path through ``via_pairs`` and routes one
  walkaround + shove leg per layer, joined by through-vias DRC-validated
  along the direct pad-to-pad line.  Each leg emits its rounded-corner
  arcs when the skeleton survives walkaround/shove and the corner sits
  away from a via junction; legs whose skeleton was disturbed, or whose
  fillet would end on a via, fall back to straight segments — via
  junctions stay straight-through connections.

### corner_mode strategy (options["corner_mode"])
- ``rounded45`` (default): short rounded fillets that hug the 45-degree
  miter—corner looks rounded but deviates little from a miter; emits
  ``(arc ...)`` track nodes on an unobstructed skeleton.
- ``mitered45``: sharp 45-degree miter corners, straight segments.  Use
  when a rounded corner would fail or when pure straight geometry is
  wanted.
- ``rounded90``: quarter-circle radius arcs — the most rounded look, and
  the longest arc eaten by any detour or shove.
- ``mitered90``: Manhattan corners.
- A detour or shove linearizes an arc back to segments (the response's
  ``arc_count`` drops to 0), so rounded modes rarely fail outright —
  retry with ``mitered45`` only when you must keep the route straight.
- The response echoes ``corner_mode`` and reports ``arc_count``/``arcs``
  (start/mid/end/width/layer/net) and ``shoved`` (pushed tracks as
  net/layer/width/points).

### options["via_pairs"] (layer transitions)
Each ``(from_layer, to_layer)`` pair is one allowed through-via jump,
traversable in both directions.  Default ``(("F.Cu", "B.Cu"),)``.  On a
4-layer board this default forbids landing on inner layers; pass
``(("F.Cu", "In1.Cu"), ("In1.Cu", "B.Cu"))`` to allow routing through
the inner stack instead of jumping straight F<->B.