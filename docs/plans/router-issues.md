# Router Known Issues

## 1. Pad rotation in `_pad_obstacle` — DONE (verified 2026-09-08)

**Fixed**: `total_angle` was `pad_angle + fp_rot`. KiCad stores pad size in the
footprint's coordinate system, not the pad's. Now uses `fp_rot` only. This
also fixed the "pad center blocked by buffer" issue.

## 2. Pads with no assigned net (net=None) — DONE (verified 2026-09-08)

**Fixed**: `_pad_obstacle` now only skips pads with non-None net matching the
route net. Pads with no net (like J3/B8) are treated as real copper obstacles.

---

## 3. Multi-layer routing not yet implemented — DONE (verified 2026-09-08)

**Fixed**: `multi_layer_a_star` (grid_a_star.py:560) is back; `router.py`
routes `start_layer != end_layer` through it with via edges
(`via_pairs`, `via_cost`, `turn_penalty`, via-forbidden zones over
same-net pad polygons). MCP tool `pcb_route_pad_to_pad` exposes
`via_pairs` / `turn_penalty`.

## 4. Smaller grid resolution for fine-pitch routing — OPEN (verified 2026-09-08)

0.1mm grid may miss narrow gaps between tightly packed pads.
`RouteRequest.grid_resolution` (router.py:164, default None -> 0.025 mm) is
honoured by the router (router.py:453), but `pcb_route_pad_to_pad`
(pcb_routing_tools.py:33-44) still exposes no `grid_resolution` parameter.
Tracked in docs/develop_backlog.md.

## 5. `_find_pad_size` returns raw unrotated size — OPEN, partially mitigated (verified 2026-09-08)

`_find_pad_size` (router.py:1439) still returns the raw footprint-local
(w, h). Callers now apply `_world_size` (±90° w/h swap only, router.py:338-342),
and `_pad_exit_points` was replaced by `_replace_pad_path`/`_build_pad_wire`
(router.py:1104/1150), which use an axis-aligned AABB of that size. For
footprints rotated at arbitrary angles the AABB overestimates the real copper
and the exit direction is suboptimal. Obstacles are exact (`_pad_obstacle`
rotates by `fp_rot`, world_model.py:407) — only exit-point heuristics affected.
Tracked in docs/develop_backlog.md.
