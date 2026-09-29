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
   ``width`` when a non-netclass width is needed, and ``algorithm`` to
   pick the engine: ``astar`` (default) for grid A*, ``pns`` for the
   walkaround + shove engine.  The VLM control knobs are top-level:
   ``strategy="shove"|"walkaround"`` (PNS shove policy; ``"shove"``
   default, ``"auto"`` removed 2026-09-27),
   ``waypoints=[...]`` (waypoint/via anchor chain), ``dry_run=True``
   (route + render without writing).  Board-stable config and rare
   tweaks go in the optional ``options`` dict — omit it for defaults:
   ``options={"corner_mode": ...}`` (default ``rounded45``),
   ``options={"layer_hint": ...}`` for thru-hole pads,
   ``options={"via_pairs": (("F.Cu", "B.Cu"),)}`` to allow layer
   transitions, ``options={"turn_penalty": 0.0}`` for pure
   shortest-path routing.
4. On a route failure, read the error message, look at the latest layer
   render, and retry with a different ``options["layer_hint"]``, a
   different pair, or a via transition.  Do not silently repeat the
   same call.
5. Optionally add or delete vias with ``pcb_add_vias`` / ``pcb_delete_vias``
   after routing (e.g. ground stitching).

## How routing works (algorithm switch)
``pcb_route_pad_to_pad`` takes an ``algorithm`` argument; a single route
always uses exactly one algorithm.  The response echoes ``algorithm``.

Which one should you use?  Two callers, two flows:

- **Non-vision callers** (human engineers, scripts, text-only agents):
  keep the default ``algorithm="astar"`` — route the exact pad pairs from
  ``get_ratsnest`` and read the structured response.  Straight segments,
  no render loop required.
- **Vision-capable agents** (a VLM that reads render images): use
  ``algorithm="pns"`` for the walkaround + shove engine and drive it with
  the visual loop below — read the board render, emit an anchor chain,
  iterate on the rendered evidence.

- ``astar`` (default): grid-based A* planner.  Single-layer routes run
  hierarchical grid A* (coarse pass + fine band); multi-layer routes run
  multi-layer A* with via edges.  This is the classic router behaviour and
  emits straight segments only — rounded-corner arcs are a PNS feature.
  It is the recommended choice for non-vision callers: call it straight
  pad-to-pad and read the result, no render loop needed.
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

## Visual-aided routing loop (VLM / vision-capable agents)
This loop is for vision-capable callers — a VLM or any agent that reads
render images.  Non-vision callers should stick with the default
``algorithm="astar"`` and straight pad-to-pad calls (above); this loop
buys nothing without the render.  The vision caller reads the rendered
board and makes the global decisions (anchor chain, layers, order,
accept or retry), while the routing engine owns the precise geometry
between the anchors — the same split as interactive routing in KiCad,
where the human clicks the anchors and the engine fills in the track
between them.  Do not hand the router every track segment; hand it a
route intent.

1. **See the whole board first** — call ``export_pcb_layer_image`` on the
   current board state (pad labels like ``R5.1`` are rendered) and pick
   which pads of the net to connect and in what order.
2. **Emit an anchor chain** — call ``pcb_route_pad_to_pad`` with
   ``waypoints=[pad_a, wp1, via1, pad_b]`` (the waypoint/via anchor chain),
   the working layer and width, and ``dry_run=True``.  The waypoints split
   the route into legs, and the vias land where you put them, not where
   the engine guessed.
3. **The engine routes leg by leg** — each leg is planned in order
   (skeleton → walkaround → shove), with every between-leg via checked
   against the design rules before the next leg starts.
4. **Read the rendered evidence** — the image returned is the verdict:
   green = the routed path, grey = the skeleton it attempted, red = the
   obstacles that blocked it, green dots = your anchors.  Treat the
   structured fields as caption text on the image and decide from the
   picture.
5. **Adjust, then commit** — failure feedback is image-first: see where
   the grey attempt collides with a red blocker, then change the plan at
   the decision level — move or insert waypoints, drop an anchor to
   change the via position or the leg split, switch layers or
   ``via_pairs``, reorder the chain — and re-run the loop.  Repeat until
   the render shows the route you want, then re-send the same call with
   ``dry_run=False`` to write it to the board.