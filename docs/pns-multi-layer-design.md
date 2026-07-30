# PNS Multi-Layer Routing — Design & Enhancement Plan

> **Status: Phase 1-4 complete, Phase 5 done** — multi-layer routing is enabled
> and usable.  Remaining work: incremental via cost (P3) requires state-space
> expansion in grid A*, deferred to a future PR.

## 1. Current State (Post-P1–P5)

### 1.1 Implemented ✅

| Component | File | Status |
|-----------|------|--------|
| `multi_layer_a_star()` | `grid_a_star.py` | ✅ called from `auto_route_pair` when layers differ |
| Gate removal | `router.py` | ✅ `if start_layer != end_layer` → uses multi-layer A* |
| Pad area clearing | `router.py` | ✅ `_subtract_pad_aabb()` clears start/end grids |
| `via_cost` configurable | `router.py` | ✅ `RouteRequest.via_cost` threaded to A* |
| Via param resolution | `router.py` | ✅ `_resolve_via_diameter/drill` uses netclass pattern matching |
| GridNode → RouteNode | `router.py` | ✅ converted before `postprocess_path()` |
| Unit tests | `test_grid_a_star.py` | ✅ 12 tests: 7 multi-layer + 5 pad subtract |
| Integration tests | `test_pcb_routing.py` | ✅ 3 previously-xfailed tests pass |

### 1.2 Deferred

| Item | Reason |
|------|--------|
| Per-count via cost (incremental) | Requires encoding via-count in grid A* state tuple, enlarges search space significantly. Flat 2.0 mm cost is sufficient for 2-3 layer boards. |
| Post-route via DRC validation | Router's internal obstacle model already prevents via-on-obstacle. External DRC via `check_vias()` flags false positives near pads (which were cleared from internal model). Final validation belongs in KiCad's native DRC. |

### 1.3 Verified — No Changes Needed ✅

| Item | Detail |
|------|--------|
| Per-layer obstacle inflation | Each `Obstacle` has a `layers: frozenset[str]`. Grouping `[o for o in buffered if rl in o.layers]` is correct. Tracks only block their own layer; vias block all touched layers. |

## 2. Completed Phases

| Phase | Description | Files Changed | Tests |
|-------|-------------|---------------|-------|
| **P1** | Remove gate, wire multi-layer | `router.py` | 3 xfail → pass |
| **P2** | `via_cost` configurable | `router.py`, `RouteRequest` | — (existing tests cover) |
| **P3** | Via diameter/drill resolution | `router.py` (`_default_via_params`) | — (integration test) |
| **P4** | Verify per-layer obstable grouping | no change (verified correct) | — |
| **P5** | Unit tests | `test_grid_a_star.py` (new) | 12 new tests |

## 3. Deferred Work

- **Per-count via cost**: requires expanding A* state tuple `(gx, gy, layer_idx, n_vias)`. Flat 2.0 mm is sufficient for typical 2-4 layer boards.
- **Post-route via DRC validation**: router's internal obstacle model suffices; native KiCad DRC is the final gate.

## 4. Non-Goals

- Microvia / blind / buried via — through-via only
- Push-and-shove — remains no-shove
- Differential pairs / length matching
- Replace visibility graph with grid A\*
