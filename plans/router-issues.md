# Router Known Issues

## 1. Pad rotation in `_pad_obstacle` — DONE

**Fixed**: `total_angle` was `pad_angle + fp_rot`. KiCad stores pad size in the
footprint's coordinate system, not the pad's. Now uses `fp_rot` only. This
also fixed the "pad center blocked by buffer" issue.

## 2. Pads with no assigned net (net=None) — DONE

**Fixed**: `_pad_obstacle` now only skips pads with non-None net matching the
route net. Pads with no net (like J3/B8) are treated as real copper obstacles.

---

## 3. Multi-layer routing not yet implemented

Center-to-center routing only supports single-layer (start_layer == end_layer).
Multi-layer routing via `multi_layer_a_star` was removed during simplification.

## 4. Smaller grid resolution for fine-pitch routing

0.1mm grid may miss narrow gaps between tightly packed pads.
A configurable `grid_resolution` field exists in RouteRequest but is not
exposed through the MCP tool API.

## 5. `_find_pad_size` returns raw unrotated size

`_find_pad_size` returns size from PCB as-is. `_pad_exit_points` uses this to
compute exit positions. For rotated footprints, exit points could be suboptimal.
The polygon in `_pad_obstacle` correctly uses only `fp_rot`, so obstacles are
fine — only exit-point heuristics are affected.
