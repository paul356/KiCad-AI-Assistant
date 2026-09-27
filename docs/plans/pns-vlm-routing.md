# VLM + Self-Built PNS Routing — Design Study (v3: VLM+PNS collaboration)

> Status: v2 engine **implemented** (multi-layer PNS + leg-internal arcs +
> `options`/`corner_mode=rounded45` default, issue #143 / PR #144). v3 defines
> the **VLM ↔ PNS collaboration interface**: anchor-chain control surface,
> render-first failure feedback, an explicit `strategy` knob
> (auto/walkaround/shove) and an always-on `route_png` render.
> Complements `docs/plans/vlm-feedback-routing.md` (v1 closed loop shipped).

> **v3 changelog (2026-09-25)**
> - Failure feedback is **render-first**: the image is the primary channel;
>   structured data is only annotation on the image. Structured `RouteFailure`
>   fields alone are not intuitive enough for a visual model.
> - Control surface **collapsed to the anchor chain**: `waypoints` + explicit
>   `vias` are the only spatial knobs the VLM needs. All engine-internal knobs
>   (shove depth/nets, max_length, max_vias, net_kind, keepouts, preferred
>   region) are dropped from the VLM-facing interface.
> - Interaction paradigm: **explicit strategy + always-on evidence** — the
>   VLM picks the `strategy` knob (`shove` / `walkaround`)
>   deliberately; every result carries a rendered `route_png` the VLM
>   inspects instead of reading coordinates.  (The earlier
>   "PNS proposes candidates, VLM selects" `candidates` design was dropped —
>   see §10.)

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

---

# Part II — v3: VLM ↔ PNS collaboration design

## 7. Collaboration model (anchored loop)

Division of labor, restated with the anchor chain as the interface:

- **VLM** (global, semantic, ~1mm positional precision): reads the rendered
  board, emits an **ordered anchor chain** (pads, waypoints, vias), picks
  layers/order, decides accept/retry/abandon from rendered evidence.
- **PNS** (local, exact, DRC-guaranteed): consumes the anchor chain, performs
  per-leg `BuildInitialTrace → walkaround → shove`, emits legal routes (or
  rendered failure evidence).

```
① VLM sees whole-board render (pad labels)
② emits waypoint chain [pad_a, wp1, via1, pad_b] + layer/width + dry_run
③ PNS routes leg by leg (waypoints split legs — existing waypoints[] mechanism,
   router.py:913)
④ result rendered back: success = green path; failure = grey attempted
   skeleton + red-blocked obstacles + green anchors
⑤ VLM reads the picture: adjust waypoints / layer / order, or commit (dry_run=False)
```

This matches KiCad interactive routing itself: the human (VLM) clicks anchors,
PNS owns the precise geometry between them.

## 8. Render-first failure feedback (W1)

Failure feedback is **image-primary**. Structured fields survive only as
annotation text on the image. Rendering additions (all in existing renderers,
zero routing changes):

| Element | Visual | Source |
|---|---|---|
| Pad labels | `R5.1`, `C3.2` text at each pad (size ~0.4mm, leader to pad) | `render_board_tools.py` — implemented (W1) |
| Attempted skeleton | grey semi-transparent polyline | engine stages / failure trace (`_dump_viz`) |
| Blocking obstacles | red highlight ring/circle on the colliding track/footprint | `RouteFailure.blocking_items` or `_dump_viz` obstacles |
| Anchors | green dots at pad/waypoint/via positions | anchor chain |

Rendering entry points:

- `kcaa/tools/render_board_tools.py::render_board` — pad labels (always on
  for VLM-facing boards; optional via param, default on).
- `scripts/render_viz.py` — already renders stage JSON with `obstacles` and
  optional `candidates`; add failure-highlight mode (grey path + red ring).
  (The W3 `candidates` side-by-side render was dropped — see §10.)
- Implemented (W1): `kcaa/tools/render_route_state.py` — one-call render of
  "route attempt with failure evidence" from a `RouteResult`/`RouteFailure`
  + anchor chain — reused by the MCP tool and the VLM driver script.  Every
  successful route renders through this path into `RouteResult.route_png`.

Acceptance criteria (W1): board render shows pad labels; a forced-failure
fixture produces a PNG with grey attempted path + red-highlighted blocker +
green anchors; existing render tests stay green.

## 9. Anchor-chain control surface (W2)

VLM-facing parameters — **the complete spatial vocabulary**:

```python
# pcb_route_pad_to_pad (interface v3): VLM control knobs are TOP-LEVEL
#   waypoints: ordered list of waypoint specs; None/empty = straight pad-to-pad
#   dry_run: bool (default False) — compute + render, do NOT write
#   strategy: "shove" | "walkaround" (PNS shove policy)
#   layer_hint moved INTO options (rare tweak); options keeps
#   corner_mode / via_pairs / turn_penalty
Anchorspec = (
    {"kind": "waypoint", "pos": (x, y), "tol_mm": 1.0}   # pass near (x,y) ± tol
  | {"kind": "via",      "pos": (x, y), "to_layer": "B.Cu"}  # explicit via site
)
```

- **waypoint**: preferred pass-through zone. PNS routes the leg toward it;
  if the tolerance circle is unreachable, drop the `tol_mm` violation into the
  failure render (grey marker at the requested pos) instead of hard-failing.
- **via**: explicit layer-switch anchor. PNS DRC-validates, micro-shifts if the
  exact spot is blocked (shift ≤ tol_mm), reports the actual site. The engine
  already splits legs at `anchors = [pad_a, *vias, pad_b]` (router.py:913) —
  waypoints are just non-layer-switching members of the same chain.
- **dry_run**: route + render + return, no board write. v1's
  `pcb_route_pad_to_pad` saves unconditionally; dry_run guards VLM experiments.

**Dropped from the VLM-facing surface** (were proposed in earlier drafts, now
out): keepouts, preferred_region, shove_nets/shove_depth, max_length_mm,
max_vias, net_kind. Waypoints express intent; PNS owns engine internals.

> **Implemented (W2)**: `RouteRequest.waypoints` / `RouteRequest.dry_run`
> (router.py) + top-level `waypoints` / `dry_run` on
> `pcb_route_pad_to_pad` (v3: promoted out of `options`, same as
> `strategy`).  Each anchor consumes one leg boundary (N anchors
> → N+1 legs, one `run_leg` per pair); waypoints are soft (unreachable →
> skipped, echoed via `waypoint_violated` / `violated_waypoints`);
> via anchors DRC-clean + micro-shift within `tol_mm` (nearest clean site
> reported in `via_sites`); `dry_run` skips the tool's load/save entirely
> (byte-identical file).  A blocked pinned leg (incl. no DRC-clean via
> spot) raises `RouteFailure` carrying the rendered evidence PNG
> (`render_route_attempt` + `BlockingEvidence`).  Anchor kinds are
> exactly ``waypoint`` and ``via``; ``{"kind": "pad", ...}`` was removed
> by decision (2026-09-27) — it fails validation with the generic
> ``unsupported anchor kind`` error (no reserved half-state).

## 10. Explicit strategy knob + always-on render (W3) — candidates dropped

> **Decision (2026-09-27)**: the W3 `candidates: int` multi-route knob was
> dropped.  `candidates` was opaque — the VLM passing "3" could not know
> what the engine varied (shove on/off? which tol scales?).  Replaced by
> an explicit `strategy` knob the VLM controls deliberately, plus an
> always-on rendered image of the single route, so the VLM sees what it
> got (in dry_run previews and commits alike).

- `strategy: str` on `RouteRequest` and as a top-level
  `pcb_route_pad_to_pad` parameter (v3; was inside `options`) — exactly
  two values: `"shove"` (default; walkaround + shove, engine default
  depth), `"walkaround"` (no movable push —
  `route_engine(max_shove_depth=0)`; foreign tracks are fixed solids
  and the route detours, `shoved == []`).  `"auto"` was removed
  (2026-09-27): it mapped to the identical path as `"shove"` and only
  confused the VLM.  Values are validated up front; A* has no shove
  stage and ignores the value (still validated).  Implemented.
- `RouteResult.route_png` / response `route_png` — **every** successful
  route (PNS and A*, dry_run and commit) carries a best-effort render of
  the routed track: the emitted segments/arcs are recomposed into a
  polyline and drawn through the W1 `render_route_attempt` (grey track +
  green anchor dots on the board layers).  Render failure never masks
  the route result (`None`).  Implemented.
- A* failure evidence rendering (user-requested W3 feature) stays as-is:
  failed single-/multi-layer searches append the blocking-copper
  evidence PNG to the `RouteFailure` message.

## 11. Multi-pair planning (W4) — **dropped**

> **Decision (2026-09-26)**: W4 `plan_routes` is removed in favor of the
> existing single-pair loop.  Rationale: a pure "N pairs in one call" API
> would be over-engineering — the VLM already has `dry_run` + `candidates`
> + anchor chains per pair and can route pairs sequentially, re-ordering by
> rendered evidence between calls. The only genuine W4 value-add was
> global-channel conflict visibility and transactional undo, both of which
> add a whole plan-state machine (tee/rollback/reorder) for a benefit the
> per-pair loop already covers for typical board sizes.  Keep the design
> simple; revisit only if paired routing proves order-fragile in practice.

## 12. Milestones (v3)

| W | Scope | Exit criteria |
|---|---|---|
| W1 | Rendering upgrade: pad labels in `render_board`; failure-evidence render (grey skeleton + red blockers + green anchors) | labelled board render; forced-failure fixture → evidence PNG; render tests green |
| W2 | `waypoints` (waypoint/via specs) + `dry_run` on `pcb_route_pad_to_pad` (top-level since v3; were in `options`); failure render wired to raise | waypoint-routed fixtures pass; dry_run leaves file byte-identical; failure PNG on blocked waypoint leg — **implemented** (1001 passed / 15 skipped baseline; 9 new anchor/dry-run tests; `pad` anchor kind removed 2026-09-27) |
| W3 | `strategy` knob (shove/walkaround; `auto` removed 2026-09-27) + always-on `route_png` + A* failure evidence; interface v3 promotes strategy/waypoints/dry_run to top-level params, `layer_hint` into `options` | strategy fixtures: walkaround detours a foreign track without pushing (`shoved == []`), shove pushes it; `auto` rejected; invalid values rejected; `route_png` present on PNS and A* success — **implemented** (the W3 `candidates` multi-route + side-by-side render was **dropped**, see §10; 13 candidates tests removed).  Tests: strategy/render tests across `test_router.py` / `test_pcb_routing_tools.py` / `test_render_route_state.py` (multi-layer A* failure fixture skipped: search box auto-expands and via edges escape single-layer rings — see test comment); suite after v3 interface: 1023 passed / 16 skipped → new tool tests keep suite green.  A* failure evidence kept |
| ~~W4~~ | ~~`plan_routes` multi-pair, dry-run tee, undo/reorder~~ — **dropped** 2026-09-26 (see §11): per-pair loop suffices | n/a |

W1 is renderer-only (no routing changes) — safe first step; W2–W3 build on it.