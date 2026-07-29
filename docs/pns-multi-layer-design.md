# PNS Multi-Layer Routing — Design & Enhancement Plan

> **Status: design in progress** — based on previously approved design plan + current codebase state

## 1. Current State

### 1.1 Already Implemented

| Component | File | Status |
|-----------|------|--------|
| `multi_layer_a_star()` | `grid_a_star.py:462` | Fully implemented, unit-tested |
| `GridNode.layer` field | `grid_a_star.py` | Every node carries layer info |
| Per-layer obstacle grouping | `router.py` caller | `obstacles_by_layer` dict ready |
| `RouteRequest` multi-layer args | `router.py` | `start_layer`, `end_layer`, `via_pairs` |
| `RouteResult` multi-layer fields | `router.py` | `.vias: list[OutputVia]`, `.layers_used: list[str]` |
| `postprocess_path()` with vias | `path_postprocess.py` | Emits segments + vias at layer jumps |
| `OutputVia` dataclass | `path_postprocess.py` | `x, y, diameter, drill, layers: tuple, net` |
| `_ALL_COPPER` constant | `router.py` | `F.Cu, B.Cu, In1.Cu-In4.Cu` (6 layers) |
| `pcb_route_pad_to_pad` MCP args | `pcb_routing_tools.py` | `target_layer`, `via_pairs` accepted |
| `via_check.py` | `via_check.py` | DRC pre-flight for manual vias |

### 1.2 Blockers

| Blocker | Location | Detail |
|---------|----------|--------|
| **Hard gate** | `router.py` ~L440 | `if start_layer != end_layer: raise RouteFailure(...)` |
| Not calling `multi_layer_a_star()` | `router.py` | Always calls `hierarchical_a_star()`, never the multi-layer variant |
| 3 xfailed integration tests | `test_pcb_routing.py` | Tests written but guarded by hard gate |

### 1.3 Via Cost

| Router | Via Cost | Note |
|--------|----------|------|
| `multi_layer_a_star()` | Flat 2.0 mm | `_VIA_COST = 2.0` |
| `build_visibility_graph()` | Incremental: `2.0 + 0.5*n` | `DEFAULT_VIA_COST_FN` |

## 2. Implementation Plan

### Phase 1: Remove Gate — Enable Grid A\* Multi-Layer

**Goal**: `auto_route_pair` uses `multi_layer_a_star()` when `start_layer != end_layer`.

**Steps**:

1. **Remove hard gate** (`router.py` ~L440):
   ```python
   # Delete:
   if req.start_layer != req.end_layer:
       raise RouteFailure(...)
   ```

2. **Branch by layer equality**:
   ```python
   if req.start_layer != req.end_layer:
       result = multi_layer_a_star(
           obstacles_by_layer, pad_a_xy, pad_b_xy,
           req.start_layer, req.end_layer, req.via_pairs,
           route_bbox, grid_res,
       )
   else:
       result = hierarchical_a_star(buffered, pad_a_xy, pad_b_xy, ...)
   ```

3. **Postprocess multi-layer paths** — `multi_layer_a_star()` returns `list[GridNode]` with `.layer`. The existing `postprocess_path()` already handles `RouteNode` with `.layer`:
   - Convert `GridNode` → `RouteNode` before passing to `postprocess_path`
   - Or directly handle `list[GridNode]` in `postprocess_path`
   - Via emission is already implemented: at layer transitions, an `OutputVia` is emitted

4. **Resolve via diameter/drill** from `.kicad_pro` netclass:
   - `_default_via_params(data)` in `router.py` already parses Default netclass
   - `via_check.py` already reads via params per-netclass
   - Need to thread these into `postprocess_path` / `OutputVia`

5. **Pad exit on multi-layer paths** — `_replace_pad_path` and `_align_path_endpoints` currently work on `list[(x,y)]` without layer awareness. After layer-segmenting the path, run pad exit on the first and last layer segments.

**Risk**: Grid A\* flat via cost may produce unnecessary vias.

### Phase 2: Via Cost Improvements

**Goal**: Per-count via cost to prevent via stacking.

- Change `multi_layer_a_star` `via_cost` from `float` to callable `(n_vias: int) -> float`
- Or encode via count in the state tuple: `(gx, gy, layer_idx, n_vias)`
- Default: `DEFAULT_VIA_COST_FN(n) = 2.0 + 1.0 * n`

### Phase 3: Via Legality

**Current**: `multi_layer_a_star` only checks grid cell freedom on both layers.

**Needed**:
- Via pad ring must not overlap foreign-net copper (`via_check.py` logic)
- Via must be inside board outline
- Via must meet netclass diameter/drill constraints
- Can be done as post-route validation (reject and retry) or pre-filter at grid level

### Phase 4: Per-Layer Obstacle Inflation

**Current**: `_inflate_obstacles` inflates all obstacles together. For multi-layer:
- Tracks on layer A should only block layer A
- Vias block all layers they touch
- Footprint keepouts may be layer-specific

Need to inflate per-layer, using `obstacles_by_layer`.

### Phase 5: Un-xfail Tests

Three tests to restore:

| Test | File | Detail |
|------|------|--------|
| `test_multi_layer_route_inserts_via` | `test_pcb_routing.py:132` | F.Cu → B.Cu inserts a via |
| `test_multi_layer_via_in_board` | `test_pcb_routing.py:154` | Via is within board outline |
| `test_multi_layer_tool_writes_segments_and_via` | `test_pcb_routing.py:508` | MCP writes file correctly |

## 3. Non-Goals (same as original plan)

- Microvia / blind / buried via — through-via only
- Push-and-shove — remains no-shove
- Differential pairs / length matching
- Replace visibility graph with grid A\*

## 4. Implementation Order

| Phase | Description | Files Changed | Tests |
|-------|-------------|---------------|-------|
| **P1** | Remove gate, wire multi-layer | `router.py` | 3 xfail → pass |
| **P2** | Via cost per-count | `grid_a_star.py` | New unit |
| **P3** | Via legality pre-flight | `router.py`, `via_check.py` | New unit |
| **P4** | Per-layer obstacle inflation | `world_model.py` | New unit |
| **P5** | Un-xfail + add more tests | `test_pcb_routing.py` | 3 tests un-xfailed |

## 5. Dependency Graph

```
P1 (gate removal) ──→ basic multi-layer works
  ├── P4 (obstacles) ──→ correctness
  ├── P2 (via cost)  ──→ quality
  ├── P3 (via check)  ──→ safety
  └── P5 (tests)     ──→ regression coverage
```

P1 alone is the minimum viable deliverable — removing the hard gate enables multi-layer paths. Phases 2-4 can be iterated on top.
