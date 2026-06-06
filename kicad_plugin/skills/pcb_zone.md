---
name: pcb-zone
priority: 50
description: "Copper pour & keepout zone CRUD, zone refill via IPC, polygon geometry"
---
# Tools overview
- **list_zones** — List all zones in a PCB file, distinguished by type (copper_pour / keepout).
- **add_zone** — Add a copper-pour or keepout zone with polygon outline.
- **delete_zone** — Delete a zone by UUID (works for both types).
- **refill_zones** — Refill all copper pours on the currently open PCB via IPC API.

# Recommended workflow
1. **Understand existing zones**:
   Call **list_zones(pcb_path)**. Returns `zones` (list with uuid, zone_type, net, net_name,
   layer, hatch_style, polygon_pts, keepout_rules, fill), `count`, `copper_pour_count`,
   `keepout_count`. Use the returned `uuid` to target specific zones for deletion.
2. **Add a zone**:
   Call **add_zone(pcb_path, layer, polygon_pts, zone_type, net_name, ...)**. Key points:
   - `polygon_pts` is `[{x, y}, ...]` — minimum 3 points in board coordinates (mm, +X right, +Y down).
   - For `zone_type="copper_pour"`: `net_name` is required and must match an existing board net.
     Supply `clearance`, `min_thickness`, `fill`, `thermal_gap`, `thermal_bridge_width` as needed.
   - For `zone_type="keepout"`: `net_name` is ignored (created on net 0). Supply
     `keepout_tracks`, `keepout_vias`, `keepout_copperpour`, `keepout_footprints`, `keepout_text`
     (each `"allowed"` or `"not_allowed"`).
   - `hatch_style` must be `"edge"` or `"full"` (default `"edge"`, pitch 0.508 mm).
   Returns `zone_uuid` (auto-generated UUIDv4) for future reference.
3. **Refill after file-based changes**:
   After `add_zone` or any zone modifications via file-based tools, call **refill_zones()**.
   This reverts the board in KiCad to sync with the latest disk state, recalculates all
   copper pours, and saves back to disk. Blocks up to 30 seconds.
4. **Delete a zone**:
   Call **delete_zone(pcb_path, zone_uuid)** with the UUID from `list_zones`. Returns
   `deleted: true` on success.

# Coordinate convention
- PCB coordinates: **millimetres, +X right, +Y down**, rotation **clockwise-positive**
  (standard KiCad PCB convention). This is consistent with the rest of the PCB tools.

# Caveats & gotchas
- All mutation tools create a `.kicad_pcb.bak` backup before writing.
- `add_zone` validates that the layer exists in the PCB file's layer declarations.
  Pass a layer name from `get_board_info` → `layer_names` or `list_zones` output.
- For copper-pour zones, `net_name` is resolved against both the top-level net table
  and footprint pad net references (handles KiCad 9/10 boards that omit the net table).
- `refill_zones` must be called when KiCad is running with the PCB open. If no board
  is open, it returns `success: false` with an error.
- `delete_zone` looks up the zone by UUID. If the UUID is not found, it returns
  `deleted: false` — double-check the UUID from `list_zones` output.
- Keepout rules fields default to `"not_allowed"` for tracks/vias/copperpour and
  `"allowed"` for footprints/text — matching typical KiCad keepout defaults.
