---
name: pcb-query
priority: 50
description: "PCB query workflow: get_board_info, list_footprints, ratsnest, nets"
---
# PCB query workflow
1. Call **get_board_info** to learn layer stack, copper layer count, footprint
   count, net count, and board generator.
2. Call **list_footprints** to get every footprint's reference, value, x/y
   (world mm), rotation (CW+), and layer.
3. Call **get_footprint** for detailed info on a specific footprint: pad
   numbers/types/nets, all properties, and local pad coordinates.
   Note: pad coordinates from get_footprint are *local* (footprint-relative).
   Use **get_ratsnest** when you need world-coordinate pad positions.
4. Call **list_nets** to enumerate nets with their pad counts.
5. Call **get_ratsnest** to identify unconnected pad pairs.  An empty result
   means the board is fully routed.  Pad x/y in the ratsnest response are
   already in world coordinates (rotation applied).
