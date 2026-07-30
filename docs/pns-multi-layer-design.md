# PNS Multi-Layer Routing — Design & Enhancement Plan

> **Status: P1–P6 complete** — multi-layer routing is enabled and shares the
> same A\* function, postprocess pipeline, and viz dump chain as single-layer.

## 1. Current State

### 1.1 Implemented

| Component | File | Detail |
|-----------|------|--------|
| Unified `grid_a_star()` | `grid_a_star.py` | Accepts `list[GridMap]` + layer/via params. Single-layer passes `[grid]` — all math degrades to original 2D encoding. `if via_from:` branch zero overhead in single-layer mode. |
| `multi_layer_a_star()` | `grid_a_star.py` | 35-line wrapper: builds grids, translates layer names to indices, decodes GridNode results. |
| Gate removal | `router.py` | `if start_layer != end_layer` → calls `multi_layer_a_star()` |
| Pad area clearing | `router.py` | `_subtract_pad_aabb()` clears start/end grids before A\* |
| `via_cost` configurable | `router.py` | `RouteRequest.via_cost` threaded to A\*. Flat default 2.0 mm. |
| Via param resolution | `router.py` | `_resolve_via_diameter/drill` uses netclass pattern matching (not just Default) |
| Postprocess pipeline reuse | `router.py` | `_postprocess_layer_segment()` shared by single/multi-layer: simplify→shortcut→snap45. Per-layer obstacles + per-layer viz dump. |
| Endpoint alignment | `router.py` | `_align_single_endpoint()` extracted from `_align_path_endpoints()`, used by multi-layer per-segment |
| Bridging segments | `path_postprocess.py` | Offset via endpoints emit a short bridging segment instead of raising |
| Unit tests | `test_grid_a_star.py` | 12 tests: 7 multi-layer + 5 pad subtract |
| Integration tests | `test_pcb_routing.py` | 3 previously-xfailed tests pass |

### 1.2 Deferred

These were in the original Phase 2/3 plan. Assessed and explicitly deferred.

| Item | Reason |
|------|--------|
| Per-count via cost (incremental) | Flat 2.0 mm is sufficient for typical 2-4 layer boards. Per-count cost would require expanding A\* state tuple to `(gx, gy, layer_idx, n_vias)`, enlarging search space significantly. |
| Post-route via DRC via `check_vias()` | Router's internal obstacle model already prevents via-on-obstacle placement. Calling `check_vias()` flags false positives near start/end pads (which were cleared from the internal model but still exist in the PCB file). Final DRC belongs in KiCad's native DRC. |

### 1.3 Verified — No Changes Needed

| Item | Detail |
|------|--------|
| Per-layer obstacle inflation | Each `Obstacle` has `layers: frozenset[str]`. Grouping `[o for o in buffered if rl in o.layers]` is correct. Tracks only block their own layer; vias block all touched layers. |

## 2. Implementation Phases

### Phase 1: Remove Gate — Enable Grid A\* Multi-Layer

**Original plan**: `auto_route_pair` uses `multi_layer_a_star()` when `start_layer != end_layer`.

1. Remove hard gate `if start_layer != end_layer: raise RouteFailure(...)`
2. Branch by layer equality: multi-layer → `multi_layer_a_star()`, single-layer → `hierarchical_a_star()`
3. Convert `GridNode` → `RouteNode` for `postprocess_path()`
4. Resolve `via_diameter`/`via_drill` from `.kicad_pro` netclass
5. Pad area clearing: `_subtract_pad_aabb()` before A\* to unblock start/end cells

**Done** — all 5 steps implemented, 3 xfailed integration tests pass.

### Phase 2: Via Cost Improvements

**Original plan**: Per-count via cost to prevent via stacking.

**Done**: `RouteRequest.via_cost` field (flat 2.0 mm default), threaded to `grid_a_star()`.

**Deferred**: Incremental via cost (`2.0 + 1.0 * n_vias`) requires expanding A\* state encoding to include via-count dimension. The visibility graph router has `DEFAULT_VIA_COST_FN(n) = 2.0 + 0.5*n` as a reference if this is needed later.

### Phase 3: Via Legality

**Original plan**: Post-route validation via `via_check.py`.

**Done**: Via diameter/drill resolved from net's *actual* netclass (not just Default). `_default_via_params()` now uses netclass pattern matching via `_net_to_netclass()` + `_resolve_netclass()`.

**Deferred**: Calling `check_vias()` inside `auto_route_pair` — produces false positives near start/end pad areas (the router's internal obstacle model subtracts pad AABBs, but the PCB file still has the pad copper). KiCad's native DRC is the appropriate final validation gate.

### Phase 4: Per-Layer Obstacle Inflation

**Verified**: each `Obstacle` carries `layers: frozenset[str]`. Grouping `[o for o in buffered if rl in o.layers]` correctly separates per-layer obstacles. Tracks only block their own layer; vias block all touched layers. No code changes needed.

### Phase 5: Tests + XFAIL Removal

Removed `@pytest.mark.xfail` from 3 integration tests. Added 12 unit tests in `test_grid_a_star.py` (7 multi-layer A\*, 5 pad subtract).

### Phase 6 (new): Unify Single/Multi-Layer Code Paths

**Goal**: One A\* function, one postprocess pipeline, one viz dump chain.

1. **`grid_a_star()` unified**: accepts `grids: list[GridMap]` + `start_layer_idx`/`end_layer_idx`/`via_from`/`via_cost`. Single-layer (`n_layers=1, via_from=None`) → all math degrades to original 2D encoding.

2. **`multi_layer_a_star()` → thin wrapper**: builds grids, translates layer names to indices, calls `grid_a_star()`, decodes result → `list[GridNode]`.

3. **`_postprocess_layer_segment()`**: shared by single/multi-layer. Runs `simplify_path → shortcut_path → snap_to_45_path_safe` with per-layer obstacles and layer-prefixed viz dump stages.

4. **`_align_single_endpoint()`**: extracted from `_align_path_endpoints()`. Multi-layer uses it per-segment; `_align_path_endpoints()` delegates to it.

5. **Multi-layer pipeline**: A\* path grouped by layer → each segment runs `_replace_pad_path` (start/end only) → `_postprocess_layer_segment` → `_align_single_endpoint` → concat as `RouteNode` list → `postprocess_path()`.

6. **`postprocess_path()` offset via handling**: instead of raising `RuntimeError`, emits the via at the previous node's position and a short bridging segment on the new layer.

7. **Viz dump**: single-layer stages unchanged. Multi-layer adds per-layer stages (`layer-F.Cu-0-astar`, `layer-F.Cu-simplify`, …).

## 3. Non-Goals

- Microvia / blind / buried via — through-via only
- Push-and-shove — remains no-shove
- Differential pairs / length matching
- Replace visibility graph router with grid A\*

## 4. Via Cost Reference

| Router | Via Cost | Note |
|--------|----------|------|
| `grid_a_star()` | Flat `via_cost` (default 2.0 mm) | Configurable via `RouteRequest.via_cost` |
| `build_visibility_graph()` | `DEFAULT_VIA_COST_FN(n) = 2.0 + 0.5*n` | Not used by grid router |
