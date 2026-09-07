---
name: pcb-query
priority: 50
description: "PCB query workflow: get_board_info, list_footprints, ratsnest, nets"
---
# PCB query workflow
1. Call **get_board_info** to learn layer stack, copper layer count, footprint
   count, net count, and board generator.
2. Call **list_footprints** to get every footprint's reference, value, x/y
   (world mm), rotation (CCW+), and layer.  Filter with ``ref_prefix``
   (e.g. ``"R"``), ``bbox`` (``[xmin, ymin, xmax, ymax]``), or ``fields``
   (e.g. ``["reference", "x"]``); unknown field names are rejected.
3. Call **get_footprint** (batch: `references` list) for detailed info on one
   or more footprints: pad numbers/types/nets, all properties, local pad
   coordinates, and
   `edge_cuts` (fp_line/fp_arc/fp_circle/fp_curve items on the
   footprint's Edge.Cuts layer, in footprint-local mm — transform to
   world the same way as pads: CCW+ rotation, +Y down).
   Note: pad coordinates from get_footprint are *local* (footprint-relative).
   Use **get_ratsnest** when you need world-coordinate pad positions.
4. Call **list_nets** to enumerate nets with their pad counts. Filter with
   ``name_prefix`` or ``netclass``; ``fields`` picks a subset of
   net_id/name/pad_count/netclass/type.  **list_vias** accepts ``net`` and
   ``fields`` filters.
5. Call **get_ratsnest** to identify unconnected pad pairs.  An empty result
   means the board is fully routed.  Pad x/y in the ratsnest response are
   already in world coordinates (rotation applied).
