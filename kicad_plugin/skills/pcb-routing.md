---
name: pcb-routing
priority: 95
description: "PCB pad-to-pad routing workflow: get_ratsnest, export_pcb_layer_image, pcb_route_pad_to_pad, failure retry"
---
# PCB routing workflow
Connect pads belonging to the same net with DRC-clean tracks.

1. Call **get_ratsnest** to list the real unrouted pad pairs (world
   coordinates, net names).  Never guess pairs — use this data source.
2. Call **export_pcb_layer_image** to see the current board state: by default
   it renders all copper layers stacked in physical order (F.Cu red, B.Cu
   blue, inner layers green/amber/purple) plus courtyards, edge, and
   silkscreen on a dark KiCad-style background.  Pass ``layer="F.Cu"`` etc.
   for a single copper layer, or ``connect_pads=["J1.2", "J2.2"]`` to overlay
   green ratsnest lines on the pads that still need connecting.  The tool
   returns the board PNG — inspect the image before routing.
   **If the active model is not a vision model (vision disabled), skip this
   render step entirely** — you cannot see the image, and its data is not
   needed to route; rely on the get_ratsnest coordinates and the error
   messages instead.
3. Connect ONE pad pair at a time with **pcb_route_pad_to_pad**:
   ``ref_a``/``pad_a``/``ref_b``/``pad_b``/``net`` are required; pass
   ``layer_hint`` for thru-hole pads, ``via_pairs`` to allow layer
   transitions, ``width`` when a non-netclass width is needed.
4. On a route failure, read the error message, look at the latest layer
   render, and retry with a different ``layer_hint``, a different pair, or a
   via transition.  Do not silently repeat the same call.
5. Optionally add or delete vias with ``pcb_add_vias`` / ``pcb_delete_vias``
   after routing (e.g. ground stitching).