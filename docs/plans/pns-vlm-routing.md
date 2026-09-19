# VLM + Self-Built PNS Routing — Design Study (v2: engine replacement)

> Status: design study (no code yet). v2 supersedes the "A\* + shove fallback"
> plan of v1: the goal is to **replace grid A\* with a KiCad-style PNS engine**
> (walkaround + shove + 45°/arc skeleton), not to patch A\*.
> Complements `docs/plans/vlm-feedback-routing.md` (VLM loop shipped).

## 1. Decision: replace, don't patch

User directive: "not patching on top of A* — replace the current A* algorithm
with the KiCad algorithm."

Current pipeline in `auto_route_pair`:

```
A* grid search → simplify → shortcut → snap_to_45 → pad replace → align → emit segments
```

Target pipeline (KiCad architecture, pure Python):

```
skeleton trace (DIRECTION_45::BuildInitialTrace port)
  → walkaround fixed solids (collide → follow hull boundary, CW/CCW)
  → shove movable tracks (collide → roll obstacle line along hull set, recurse)
  → cleanup (merge collinear; skip arc segments)
  → emit segments + arcs
```

No grid, no `grid_resolution`, no cell-visit heuristics, no snap-back-to-grid
jitter. Paths live in continuous coordinates; corners are 45° mitered or
rounded arcs from the start.

## 2. KiCad PNS engine anatomy (verified against master source)

### 2.1 Skeleton generation — `DIRECTION_45::BuildInitialTrace`

`libs/kimath/src/geometry/direction_45.cpp:24-260`. Given P0→P1 and a corner
mode, emits a 2-3 point chain:

- `MITERED_45`: straight + diagonal legs (current behavior).
- `ROUNDED_45`: legs with an **arc fillet** at the change point —
  `arcRadius = diagLength / (2·cos(67.5°))`, four construction cases
  (start/end × tangent sign), `ConstructFromStartEndAngle` with
  `±ANGLE_45·rotationSign`.
- `ROUNDED_90`: 90° legs with fillet; `w == h` degenerates to a single arc.

This is where KiCad's "arc routing" actually lives — the engine never
searches over arcs; the **skeleton is arc-aware from construction**.

### 2.2 Walkaround — `pns_walkaround.cpp`

For each path colliding with a fixed item:

1. `NearestObstacle(path)` — rtree-backed nearest collision (clearance epsilon).
2. `AssembleCluster` — group same-net touching items into one hull.
3. Hull = item buffered by `clearance + line_width` (KiCad
   `SHAPE_LINE_CHAIN hull = seg.Hull(clearance + expansion, obstacleWidth, layer)`).
4. Re-walk the path **around the hull boundary**, both CW and CCW policies;
   pick shortest; iterate `singleStep()` until no collision or
   length-expansion limit (`lengthFactor > m_lengthExpansionFactor` → fail).

Key facts: walkaround output is always a **polyline** (arcs only survive
inside untouched regions via `restoreUntouchedArcs`, pns_line.cpp:255).

### 2.3 Shove — `pns_shove.cpp`

For a path colliding with a **movable track** (not a solid):

1. Build `HULL_SET`: every segment of the *current* line buffered by
   `clearance + obstacle_width` (+ via hull if head ends in via).
2. `shoveLineToHullSet` — re-walk the obstacle line along the **outside of the
   hull set** ("roll a wheel along the hull").
3. 3 attempts with increasing `extraHullExpansion`; endpoints movable only
   after attempt 2 and only if not pad/via anchored (`permitMovingStart/End`).
4. Pushed line becomes new current line on a stack (`pushLineStack`) →
   **recursive propagation** until no collisions or a fixed item stops it.
5. Failure (`SH_INCOMPLETE`) → unwind the whole shove set.

Arc collision in shove is **unfinished in KiCad itself** (`//TODO(snh): Handle
Arc shove`, `//TODO(snh): Customize Arc collide`). v2 mirrors that: shove
operates on polylines; arcs are emitted only in non-shoved regions.

### 2.4 Optimizer — `pns_optimizer.cpp`

Post-shove cleanup (`Simplicity2`, merge collinear). Arcs cause most optimizer
passes to be skipped (`//TODO: Fix for arcs` ×4) — same trade-off to mirror.

### 2.5 Obstacle hulls incl. arcs — `pns_utils.cpp:74`

`ArcHull(arc, clearance, walkaroundThickness)`: arcs buffered to a closed hull
(octagonal if central angle > 180° and chord < clearance). Needed for DRC and
for arc-as-obstacle queries.

## 3. Design: `kcaa/router/pns/` engine

### 3.0 Non-negotiables

- **Pure Python + shapely**, zero KiCad linkage, MIT-clean (GPL reference
  only, re-implemented from algorithm description).
- **A\* fully leaves `auto_route_pair`'s path search**; `grid_a_star.py` stays
  in the repo only for its geometry helpers (`snap_to_45_path_safe`,
  `validate_path_clear`, `simplify_path` reuse) and for the legacy route tool
  path until the engine is proven.
- Arc support = **rounded-corner skeleton** (KiCad's actual scope). Free-form
  arc paths are explicitly out of scope (KiCad hasn't finished arc shove).

### 3.1 Module layout

```
kcaa/router/pns/
  __init__.py
  direction45.py   # BuildInitialTrace port (MITERED_45/ROUNDED_45/ROUNDED_90)
  line.py          # Line: points list + width + net + layer; arc segments list
  node.py          # obstacle space: rtree over shapely shapes; NearestObstacle
  hull.py          # seg/arc hulls: buffer(clearance + width); ArcHull port
  walkaround.py    # collide→follow hull boundary CW/CCW; cluster; iteration cap
  shove.py         # hull-set roll; recursion stack; depth cap; anchors
  optimizer.py     # merge collinear (skip arc sections)
  engine.py        # top-level: skeleton → walkaround → shove → cleanup
kcaa/router/route_engine.py   # adapter: RouteRequest → RouteResult (replaces A* branch)
tests/unit/router/test_pns_*.py
```

### 3.2 Geometry primitives needed (shapely mapping)

| KiCad primitive | Shapely counterpart |
|---|---|
| `SEGMENT::Hull(cl, w)` | `LineString(buffer=caps?)` → use `buffer(distance, cap_style='round')` then simplify; hull for rolling is the **offset curve** |
| `SHAPE_LINE_CHAIN::Walkaround(hull)` | split obstacle line by hull polygon → take outside sub-lines (CW/CCW), stitch, pick shorter |
| `NearestObstacle` | STRtree nearest with clearance epsilon |
| `AssembleCluster` | union same-net touching obstacles (`unary_union` + nets check) |
| `ArcHull` | arc→polyline via `np.sin/cos` sampling, then buffer (octagon fallback) |
| `restoreUntouchedArcs` | keep arc-flagged segments outside walked regions |

Detail to port carefully: `BuildInitialTrace` signed math (`sw/sh/rotationSign`,
`tangentLength`, `ConstructFromStartEndAngle` semantics) — our arc is stored as
KiCad `(arc (start)(mid)(end))` 3-point form; the roundtrip through a sampled
polyline for DRC is mandatory.

### 3.3 Engine flow (replaces A\* in `auto_route_pair`)

```
resolve layers/width/clearance/via params   (unchanged, router.py front half)
build world model + buffer obstacle space   (unchanged)
  try:
      skeleton = direction45.BuildInitialTrace(pad_a, pad_b, corner_mode)
      path = walkaround(skeleton, fixed_obstacles)      # solids only
      path, shoved = shove(path, movable_tracks)        # tracks; depth ≤ 4
      path = optimizer.cleanup(path)                    # merge collinear
      emit segments (+arcs) + vias; RouteResult(shoved_tracks=shoved)
  except PnsFailure as exc:
      raise RouteFailure(str(exc))                      # VLM sees real cause
```

`corner_mode` is a new `RouteRequest` field (`mitered45` default,
`rounded45`/`rounded90` opt-in; `grid_resolution` becomes inert).

### 3.4 World-model gap: arcs as obstacles

`world_model.py` currently ignores `(arc ...)` nodes (verified: zero matches).
Must parse arcs → `Obstacle` (polyline-sampled polygon) so existing arc tracks
block correctly. Vias/tracks/pads unchanged.

### 3.5 Output changes

`path_postprocess.py` emits `OutputSegment`; needs an `OutputArc` sibling
(start/mid/end/width/layer/net) written as `(arc ...)` sexp. Renderer
(`render_board_tools.py`) must draw arcs when present (sampled polyline).

### 3.6 VLM collaboration (unchanged from v1)

Division of labor stays: VLM decides pair/layer/strategy; engine executes.
`allow_shove` becomes default-on (engine *is* the shove); tool response gains
`shoved`, `corner_mode`. `pcb-routing` skill updated: render after failure,
retry with different corner mode / layer — no A\* concepts in the prompt.

## 4. Milestones

| M | Scope | Exit criteria |
|---|---|---|
| M0 | `direction45.py` + `hull.py` + `line.py` skeleton; unit tests on 3 corner modes incl. degenerate (`w==h`, tangent signs) | skeleton paths match KiCad docs' geometry cases |
| M1 | `walkaround.py` (solids only) + `node.py` rtree + engine adapter replaces A\* in `auto_route_pair`; `OutputArc` writing | existing blocked test fixture routes; no A\* call in happy path |
| M2 | `shove.py` (tracks, depth cap, anchors) + `shoved_tracks` in result; arc-obstacle parsing in world_model | blocked-by-track fixture routes with one & two shoved tracks |
| M3 | rounded-corner arcs in output + renderer arc drawing + VLM skill update | end-to-end VLM loop demo with rounded route; regression suite green |

## 5. Risks & watchpoints

- **`Walkaround` geometry is the hard 20%**: rolling the obstacle line along a
  hull means signed offset curves and stitching CW/CCW candidates with
  shapely; KiCad spends ~430 lines + a shared geometry lib. Budget: iterate
  hull-runner on synthetic cases first (line↔polygon, concave hull).
- **Arc correctness**: `(arc)` 3-point storage vs internal center/angle model;
  validate tangency at joints (`SHAPE_ARC::MIN_PRECISION_IU` port).
- **Behavior delta**: A\* can route around *any* reachable region; walkaround
  fails when no hull-following path exists in ≤ iteration/length limits
  (KiCad `ST_STUCK`). Expect different success distribution; measure on the
  fixture board before minor-patching walkaround.
- **Viz**: `_dump_viz` pipeline stages reference A\* stages; keep stage names
  but switch payloads to engine states.
- **Regression**: all current router tests assert grid semantics indirectly;
  some fail by design after replacement. Update tests to assert the *contract*
  (path exists, DRC-clean, endpoints correct) not the grid.

## 6. Files

- New: `kcaa/router/pns/*` modules, `kcaa/router/route_engine.py`,
  `tests/unit/router/test_pns_*.py`
- Edit: `kcaa/router/router.py` (engine adapter, `corner_mode`,
  `RouteResult.shoved_tracks`), `kcaa/router/world_model.py` (arc obstacles),
  `kcaa/router/path_postprocess.py` (`OutputArc` + sexp),
  `kcaa/tools/pcb_routing_tools.py` (`corner_mode`, drop `grid_resolution`),
  `kcaa/tools/render_board_tools.py` (arc drawing),
  `kicad_plugin/skills/pcb-routing.md` (corner mode strategy)
- Docs: this plan (v2)