# PNS Multi-Layer Routing Design

> **Status: ✅ Design fully approved** — all defaults resolved, implementation tasks queued as `pns-ml-*` todos.

## Background

The current PNS router (`kcaa/router/`) is single-layer only:

- `RouteRequest.layer` takes one layer name (default `"F.Cu"`).
- The visibility graph (`visibility_graph.py`) builds candidate nodes on a single layer.
- `auto_route_pair` finds one path; if blocked, raises `RouteFailure`.
- The MCP tool `pcb_connect_with_via` exists but **only inserts a via at a user-supplied (x, y)** — it does not pick the via location, validate it sits at a segment endpoint, or coordinate with the routing tool.

To route two pads that sit on different layers (or to escape a blocked layer), the AI assistant currently has to:

1. Call `pcb_route_pad_to_pad` on layer A from pad A → arbitrary point P.
2. Call `pcb_route_pad_to_pad` on layer B from pad B → another arbitrary point Q.
3. Call `pcb_connect_with_via` at (P, Q) — but P must equal Q for the via to land at both segment endpoints, which the AI has to ensure by hand.

This is fragile and verbose. We want a single tool call: **"connect pad A on F.Cu to pad B on B.Cu, insert vias as needed"**.

This document specifies how to extend the PNS router with native multi-layer support.

---

## Design Goals

1. Single `auto_route_pair` call connects pads on **different layers**, automatically inserting vias where needed.
2. No new user-facing tool required — `pcb_route_pad_to_pad` gains optional `start_layer` / `end_layer` parameters and decides whether vias are necessary.
3. The router decides via placement algorithmically: at every candidate node where switching layers is legal, A\* weighs the cost of "go around" against the cost of "insert via here".
4. Existing single-layer callers continue to work unchanged (default layer pair is `[("F.Cu", "B.Cu")]` for layers, but routing on the same layer still emits zero vias).
5. All new logic preserves the existing fail-loud contract: missing boards warn (don't fail), missing pads fail, malformed inputs fail with hints.

## Non-Goals

- **Autorouter competition with FreeRouting**: this is still a no-shove router for "I know roughly where to go, help me draw the tracks" cases. It is not meant to replace the full push-and-shove or FreeRouting pipelines.
- **Microvia / blind via / buried via support**: only through-vias (`layers = (top, bottom)`) are emitted in v1. Microvias can come later.
- **Differential pairs / length matching / meanders**: out of scope; would need a different cost model.
- **Interactive routing during kipy IPC session**: same as today; we emit segments and write to file.

---

## Core Idea: Via Nodes in the Visibility Graph

The visibility graph already has a `layer` field on every node. To add multi-layer routing, we:

1. Build the graph over a **set of layers** instead of one layer.
2. For every (x, y) candidate node that exists on layer A, **also** instantiate the same node on every other layer in the routing set — provided the (x, y) position is not inside an obstacle on those other layers.
3. Connect same-layer nodes with **track edges** (cost = Euclidean distance + clearance cost, as today).
4. Connect the same (x, y) on different layers with **via edges** (cost = `via_penalty`).
5. A\* then naturally picks "go around" or "insert via" based on total cost.

The candidate (x, y) positions are unchanged: pad centers, pad exit points, obstacle bounding-box corners. The only thing new is **layer multiplicity** at those points.

### Why this works

- **No new geometric search**: A\* still walks among a discrete set of points; the only expansion is per-point × per-layer.
- **Via legality is local**: a via at (x, y) is legal iff every layer in the routing set has that (x, y) free of obstacles. We precompute this when instantiating the cross-layer nodes.
- **A\* is greedy but optimal** for the visibility graph: with a fixed `via_penalty`, the chosen path is the shortest path that respects the obstacle layout. There is no combinatorial explosion. A flat penalty is wrong, however: it ignores that additional vias compound cost. See "Cost model" for the per-via-count function approach.

---

## Architecture

```
                RouteRequest
                     │
                     ▼
            ┌──────────────────┐
            │   router.py      │   load_pcb + DRC
            │   auto_route_    │
            │     pair         │
            └────────┬─────────┘
                     │
                     ▼
            ┌──────────────────┐    layers: list[str]
            │ visibility_graph │    connect_layers: list[(str, str)]
            │   .py            │    obstacles: list[Obstacle]
            │                  │
            │ for each layer:  │    per-layer nodes + same-layer edges
            │   same-layer     │
            │   visibility     │
            │                  │
            │ for each candidate (x, y)
            │   across all layers:
            │   via edges if   │
            │   (x, y) free    │
            └────────┬─────────┘
                     │
                     ▼  multi-layer path = [RouteNode, ...]
            ┌──────────────────┐
            │   a_star.py      │   edge cost = track_dist OR via_penalty
            └────────┬─────────┘
                     │
                     ▼  path
            ┌──────────────────┐
            │ path_postprocess │   emit segments per layer
            │   .py            │   + vias at layer transitions
            └────────┬─────────┘
                     │
                     ▼  RouteResult {segments, vias}
            ┌──────────────────┐
            │   router.py      │   board-bounds check
            │                  │   returns RouteResult
            └──────────────────┘
```

---

## API Changes

### `RouteRequest`

```python
@dataclass
class RouteRequest:
    pcb_path: str
    ref_a: str
    pad_a: str
    ref_b: str
    pad_b: str
    net: str

    # New: replaces `layer`. Default = "F.Cu" on both ends.
    start_layer: str = "F.Cu"
    end_layer: str = "F.Cu"

    # New: which (top, bottom) pairs may carry a through-via.
    # Default: F.Cu ↔ B.Cu (covers the 2-layer board case, which is the
    # vast majority of projects). 4+ layer boards where the user wants to
    # route through an inner layer must pass an explicit list.
    via_pairs: tuple[tuple[str, str], ...] = (("F.Cu", "B.Cu"),)

    # Existing fields unchanged
    width: float | None = None
    clearance: float | None = None
    via_diameter: float | None = None
    via_drill: float | None = None
    max_miter_mm: float = 1.0
```

**Removed**: the old single `layer: str` field. Callers must switch to
`start_layer=` / `end_layer=`. The internal MCP tool `pcb_route_pad_to_pad`
will accept `layer=` as a deprecated alias for one release cycle
(maps to both ends with a one-time `DeprecationWarning`), then drop it.
There is no in-process caller of `RouteRequest.layer=` today — only the
MCP tool wrapper — so removing the field is cheap.

### `RouteResult`

```python
@dataclass
class RouteResult:
    segments: list[OutputSegment] = field(default_factory=list)
    vias: list[OutputVia] = field(default_factory=list)
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    # New: the layers the route actually used, for caller inspection.
    layers_used: list[str] = field(default_factory=list)
```

### `pcb_route_pad_to_pad` MCP tool

```python
async def pcb_route_pad_to_pad(
    pcb_path: str,
    ref_a: str, pad_a: str,
    ref_b: str, pad_b: str,
    net: str,
    ctx: Context,
    start_layer: str = "F.Cu",     # new
    end_layer: str = "F.Cu",       # new
    via_pairs: list[list[str]] | None = None,  # new; null → default [("F.Cu","B.Cu")]
    width: float | None = None,
    clearance: float | None = None,
    via_diameter: float | None = None,
    via_drill: float | None = None,
    max_miter_mm: float = 1.0,
    layer: str | None = None,      # deprecated; if set, warns + maps to start/end
) -> dict:
```

When `start_layer == end_layer` and the pair is not in `via_pairs`, no vias are emitted (single-layer route, identical behavior to today).

---

## Implementation Detail

### `visibility_graph.py`

**Signature change**:

```python
def build_visibility_graph(
    obstacles: list[Obstacle],
    layers: list[str],                          # was: layer: str
    start: tuple[float, float],
    end: tuple[float, float],
    via_pairs: list[tuple[str, str]] = [],      # new
    via_cost_fn: Callable[[int], float] = ...,  # new; see "Cost model"
) -> VisibilityGraph:
```

**Algorithm**:

1. **Per-layer node construction**: for each layer in `layers`, build candidate nodes (start, end, obstacle vertices) and per-layer rtree. This is the existing algorithm, just done per layer.
2. **Cross-layer via nodes**: take the union of (x, y) coordinates across all per-layer candidate sets. For each (x, y), check whether **every** layer in `layers` has that (x, y) **outside** all obstacles (with the same inflation as track edges). If yes, instantiate the (x, y) on every layer it's missing from. If no, the (x, y) can only exist on the layers it's currently in — via edges will only exist among those layers.
3. **Via edges**: for each via-legal (x, y), for every pair `(l_a, l_b)` in `via_pairs` where both layers have a node at (x, y), add a zero-length edge with weight `via_cost_fn(n_vias_so_far)`. The graph stays undirected. The edge's cost is computed lazily at A\* expansion time so the running via count is current.
4. **Graph cost**: track edges cost `euclidean(a, b)` (as today); via edges cost `via_cost_fn(via_count_so_far)`. Add a separate `edge_cost(a, b, via_count_so_far) -> float` function so A\* doesn't have to know about layers.

### `a_star.py`

**Signature change**:

```python
def a_star(
    graph: VisibilityGraph,
    start_id: int,
    goal_id: int,
    edge_cost: Callable[[RouteNode, RouteNode], float] | None = None,  # new
) -> list[int] | None:
```

Default `edge_cost` is `euclidean distance` (today's behavior). Multi-layer callers pass a function that returns `via_cost_fn(via_count_so_far)` for via edges and `euclidean(a, b)` for track edges.

**No other changes needed.** The heuristic `distance()` is already layer-agnostic (it's pure Euclidean), which is admissible for both edge types — `via_cost_fn(n) ≥ 0` for all n, so we never over-estimate the cost to goal via a via.

### `path_postprocess.py`

**Algorithm**:

1. Walk the path. Group consecutive nodes by `layer`.
2. Within each group, run the existing `postprocess` (miter + segments) and emit `OutputSegment`s on that layer.
3. At every layer transition `node_i.layer != node_{i+1}.layer`:
   - If `(node_i.layer, node_{i+1}.layer)` is in the requested via pairs → emit one `OutputVia` at `(node_i.x, node_i.y)` with `layers = (node_i.layer, node_{i+1}.layer)`. The current assumption is that via nodes in the graph always have `node_i.x == node_{i+1}.x` and same for y (by construction).
   - Else → raise `RuntimeError` (this is a programming error in the router — the caller asked for layers that aren't in `via_pairs`).

**Signature stays compatible**:

```python
def postprocess(
    path: list[RouteNode],
    width: float,
    layer: str,            # unchanged; only used for OutputSegment.layer
    net: str,
    max_miter_mm: float,
    via_diameter: float | None = None,    # new, optional
    via_drill: float | None = None,       # new, optional
) -> tuple[list[OutputSegment], list[OutputVia]]:   # returns 2-tuple, was just list[OutputSegment]
```

(Caller wraps the segments+vias into `RouteResult`.)

### `router.py`

- `auto_route_pair` reads `req.start_layer` / `req.end_layer`. Validates they exist in the PCB's `(layers ...)` list.
- Calls `build_visibility_graph(..., layers=relevant_layers, via_pairs=req.via_pairs, ...)`.
- A\* uses the multi-layer-aware edge cost.
- `postprocess` returns `(segments, vias)`.
- Board-bounds check applies to **both** segments (existing `_check_segments_in_board`) **and** vias (new `_check_vias_in_board`). A via is a circle at `(x, y)` with radius `via_diameter / 2`; the circle must be entirely inside Edge.Cuts. Missing Edge.Cuts → warning + skip (same policy as today).

---

## Cost Model

The via penalty matters: too small → router inserts unnecessary vias (ugly); too large → router takes absurdly long detours.

**Default `via_penalty_mm = 2.0`** (= 2 mm extra distance equivalent). Rationale:

- A real via on a 0.5 mm grid board adds ~1–2 mm of path length in practice (manufacturing, current capacity).
- KiCad's own autorouter uses a configurable via cost (default 1.0, but in distance units).
- This default can be overridden per-request if the user knows better.

**Future**: read `via_penalty_mm` from the project's `(netclass via_cost)` if set. v1 leaves this as a constant.

---

## Failure Modes

| Condition | Behavior | When checked |
|---|---|---|
| `start_layer` or `end_layer` not in PCB `(layers ...)` | `RouteFailure("layer 'In3.Cu' is not present in PCB; PCB only has ['F.Cu', 'B.Cu', ...]")` | **Early** (before A\*) — the layer query would otherwise crash inside the visibility graph |
| Pad shape not defined on `start_layer` / `end_layer` | (no early check) — A\* will fail with "no path" because no candidate point at pad center exists on that layer. Error message mentions the missing layer so the cause is obvious | **Deferred** to A\* failure |
| No path exists even with vias allowed | `RouteFailure("No path from ... to ... using via_pairs [...]")` (existing message, extended with the via list) | A\* returns None |
| Path requires a layer transition that is not in `via_pairs` | `RouteFailure("path needs via In1.Cu↔B.Cu but via_pairs=[('F.Cu','B.Cu')]; add ('In1.Cu','B.Cu') to via_pairs")` | A\* returns None (cannot reach goal without a non-allowed transition) |
| Via count exceeds expected | **Not enforced** in v1; could add `max_vias: int \| None = None` later | n/a |
| Via falls outside Edge.Cuts | `RouteFailure("via at (x, y) would extend outside Edge.Cuts boundary")` (router bug; never papered over) | After path postprocess, in `_check_vias_in_board` |

---

## Test Plan

### Unit tests (`tests/unit/router/`)

| Test | What it verifies |
|---|---|
| `test_visibility_graph_multi_layer_builds_nodes_on_each_layer` | Building graph with `layers=["F.Cu", "B.Cu"]` produces nodes on both layers at the same coordinates |
| `test_visibility_graph_via_edge_added_when_position_clear` | A candidate (x, y) free on both layers gets a via edge; the same (x, y) blocked on one layer does not |
| `test_visibility_graph_no_via_edge_when_position_blocked` | Via legality requires freedom on **all** routing layers |
| `test_visibility_graph_respects_via_pairs` | A via edge is only added between layers in `via_pairs` |
| `test_a_star_multi_layer_uses_via_when_shorter` | Constructed graph where detour > via_penalty → A\* picks the via path |
| `test_a_star_multi_layer_avoids_via_when_detour_shorter` | Constructed graph where detour < via_penalty → A\* avoids via |
| `test_postprocess_emits_via_at_layer_transition` | Path with one layer change → exactly one `OutputVia`, both segments correct |
| `test_postprocess_emits_multiple_vias_for_multiple_transitions` | Path with two layer changes → two vias, three segment runs |
| `test_postprocess_emits_no_vias_for_single_layer_path` | Path all on one layer → zero vias (today's behavior preserved) |
| `test_postprocess_raises_if_transition_layer_pair_not_allowed` | Internal guard against bad caller input |
| `test_check_vias_in_board_accepts_via_inside` | Via circle fully inside Edge.Cuts → no error |
| `test_check_vias_in_board_rejects_via_outside` | Via circle extends past board boundary → `RouteFailure` |
| `test_check_vias_in_board_accepts_via_on_boundary` | Via circle just touching boundary → no error |
| `test_check_vias_in_board_respects_via_diameter` | Larger via diameter shrinks the legal region accordingly |

### Integration tests (`tests/integration/`)

Add to `test_routing_board.kicad_pcb`:

- U1's pad 1 sits on `In1.Cu` instead of `F.Cu` (currently all pads are F.Cu).

New tests:

| Test | What it verifies |
|---|---|
| `test_routes_across_two_layers_inserts_one_via` | R1 pad (F.Cu) → C1 pad (B.Cu), single via emitted at the optimal location |
| `test_routes_on_same_layer_emits_no_vias` | R1 (F.Cu) → C1 (F.Cu) — current fixture, must still produce zero vias |
| `test_routes_when_only_detour_option_requires_via` | Blocked F.Cu path between R1 and C1 forces a B.Cu detour + via |
| `test_invalid_layer_pair_raises_route_failure` | Requesting F.Cu → In1.Cu without including it in `via_pairs` → `RouteFailure` |

### Regression

- All 73 existing router tests must still pass without modification (backward compatibility).
- The fixture's existing tests (single-layer, all on F.Cu) must continue to emit zero vias.

---

## Work Breakdown

| # | Task | File(s) | Estimate |
|---|---|---|---|
| 1 | Extend `RouteRequest` / `RouteResult`; deprecate `layer`; back-compat shim | `router.py` | 30 min |
| 2 | Refactor `build_visibility_graph` to accept `layers: list[str]`; add via-edge construction | `visibility_graph.py` | 2 hours |
| 3 | Add `edge_cost` parameter to `a_star`; verify heuristic remains admissible | `a_star.py` | 30 min |
| 4 | Teach `postprocess` to detect layer transitions and emit `OutputVia`s | `path_postprocess.py` | 1.5 hours |
| 5 | Wire `auto_route_pair` to multi-layer flow; add `_check_vias_in_board` | `router.py` | 1.5 hours |
| 6 | Extend `pcb_route_pad_to_pad` MCP tool signature | `pcb_routing_tools.py` | 20 min |
| 7 | Unit tests (14 new: 10 multi-layer + 4 via-board-bounds) | `tests/unit/router/test_*` | 1.5 hours |
| 8 | Integration tests (4 new) + fixture update (move U1.1 to In1.Cu) | `tests/integration/*` | 1 hour |
| 9 | Documentation: update `docs/routing_guide.md` (or create) with multi-layer examples | `docs/` | 30 min |
| **Total** | | | **~10 hours** |

---

## Resolved Design Decisions

1. **Via cost as a function**: `via_cost_fn: Callable[[int], float]` with default `lambda n: 2.0 + 0.5 * (n - 1)`. User can override.
2. **No `max_vias` cap in v1**.
3. **Backward-compat shim for `layer=`**: removed outright. Both `RouteRequest.layer` and the MCP tool's `layer=` parameter are deleted. There are no external callers yet (the routing tools were added in commit `0bbdaa9`); the integration tests get updated in the same commit. The docstring tells users to switch to `start_layer` / `end_layer`.
4. **Pad-layer validation**: **early throw** in `auto_route_pair`. Both "layer not in PCB `(layers ...)`" and "pad has no copper shape on requested layer" raise `RouteFailure` before A\* starts. Saves an indirect failure path; error messages stay specific.
5. **Vias and board bounds**: vias are checked by a new `_check_vias_in_board`. A via is a circle at `(x, y)` radius `via_diameter / 2`; the circle must lie entirely inside Edge.Cuts (after inflation). Missing Edge.Cuts → warning + skip, same policy as segments today.

## Default Values (Awaiting Reviewer) — **RESOLVED**

These are the conventions the implementation will use unless overridden:

| Knob | Default | Why |
|---|---|---|
| `via_pairs` (when caller doesn't specify) | `[("F.Cu", "B.Cu")]` (outer-pair only) | A real PCB via is a through-hole that drills every layer, but the *annular ring* (copper pad) only exists on the two layers being switched. Restricting the default to F.Cu↔B.Cu covers >90% of projects (2-layer boards). Users with 4+ layer boards must explicitly pass inner-layer pairs when they want them. |
| `via_pairs` JSON shape (MCP tool) | nested list: `[["F.Cu", "B.Cu"]]` | Standard JSON, AI assistants parse nested lists fine. Pass `null` to accept the default. |
| `via_diameter` / `via_drill` when caller doesn't specify | **resolved via netclass**, same path as `width` — fail-loud on missing/malformed `.kicad_pro` or unresolved netclass | Consistency with track-width DRC lookup. The netclass provides `via_diameter` and `via_drill`; we look up the net the route belongs to. |
| `via_diameter` netclass fallback if user passes `width=` but no via values | inherit the netclass values; do not silently fall back to 0.8/0.4 | Same fail-loud principle. |

---

## Implementation Notes

_Filled in after implementation completes._