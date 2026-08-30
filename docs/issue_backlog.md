# Issue Backlog

Known issues discovered during review but not fixed in the originating PR.
Each entry records the evidence, the impact, and the proposed fix so the work
can be picked up independently.

## PCB angle convention stale in skills/README after #76

**Status:** fixed on `fix/pcb-angle-docs` (issue #94)

### Symptom

PR #76 introduced the footprint Edge.Cuts `local → world` transform docs
under a **"clockwise-positive"** angle label, but the angle convention was
later unified to **CCW+ on screen** (0°=right, 90°=up) in the code
docstrings (`get_footprint`, `get_fp_edge_cuts_items`, `add_gr_arc`,
`docs/coordinate-systems.md` §4). The skill/README docs kept the old
labels:

- `kicad_plugin/skills/pcb_query.md`: `rotation (CW+)`
- `kicad_plugin/skills/pcb_zone.md`: "rotation clockwise-positive"
- `kicad_plugin/skills/pcb_outline.md`: "Arc angles ... increase clockwise"
  (contradicts the `add_gr_arc` / `add_board_outline_arc` CCW
  implementation)
- `README.md` tool table: `get_footprint` row did not mention the new
  `edge_cuts` field
- `docs/coordinate-systems.md` still warned "trust the matrix, not the
  label" about a `pcb_query_tools` docstring that had already been fixed

### Impact

The LLM reads these skills; trusting them means passing/reading PCB
rotations (footprint rotation, pad angles, Edge.Cuts arc angles) with the
wrong sign, and missing the `edge_cuts` geometry that #76 added.

### Fix (implemented)

- `pcb_query.md`: `rotation (CW+)` → `(CCW+)`; documents the `edge_cuts`
  field (fp_line/fp_arc/fp_circle/fp_curve in footprint-local mm,
  CCW+-transformed like pads).
- `pcb_zone.md` / `pcb_outline.md`: "clockwise-positive" → CCW-positive-on-
  screen; arc angles → counter-clockwise (KiCad file convention, 90°=up).
- `docs/coordinate-systems.md`: stale "docstring calls it CW+" note
  replaced with the current "CCW+" wording.
- `README.md`: `get_footprint` row mentions Edge.Cuts geometry.

### Validation

- Grep over `README.md`, `kicad_plugin/README.md`, `kicad_plugin/skills/`,
  `docs/`, `llm_client.py` prompt, and the PCB tool modules finds no
  remaining `CW+` / `clockwise-positive` angle claims; the only
  "clockwise" mentions left are in correct CCW-contexts (schematic/symbol
  notes, the "why not the textbook CW matrix" implementation comment).

---

## Multi-unit netlist output has no unit dimension

**Status:** fixed on `fix/issue-89-multi-unit-netlist` (issue #89)

### Symptom

`SchematicParser` merges a multi-unit symbol into a single
`components[ref]` entry with a flat pin list:

- `position`/`rotation` belong to whichever unit skip yields first —
  **not necessarily unit 1**. The entry's position is not derived from the
  merged pins and cannot be used to anchor them.
- Pins carry **no `unit` field**; unit membership must be guessed by
  clustering coordinates.

### Evidence

Real board `two_ax_PCB.kicad_sch` (U2, two units):

- unit=1 @ `(101.6, 93.8022, 90)`, unit=2 @ `(185.42, 223.3422, 90)`
- `components["U2"]["position"]` = `(185.42, 223.3422, 90)` — the anchor of
  **unit 2** (skip iterates unit 2 first), not U2A
- pins of both units are in one flat list: unit1's column at `x≈96.52`
  (PC8–PC12 …), unit2's 9 power pins at `x≈180.34/210.82`; no field
  distinguishes them
- 64 pins merged, deduped by `(num, x, y)`; `body_bbox` is the union of all
  units (correct)

### Impact

- Consumers cannot per-unit analyze (e.g. "which unit carries the power
  pins"), cannot re-verify a pin against its unit's lib definition and
  anchor, and cannot derive one unit's pins from the reported `position`.
- The pin x/y values themselves are correct per-unit world coordinates
  (each unit's pins were computed with its own `(at …)`); the gap is unit
  attribution and anchors, not geometry.

### Fix (implemented)

`SchematicParser._extract_components` now nests every placed unit under its
reference:

```json
"components[ref]": {
  "units": {
    "1": {"position": {"x", "y", "rotation"}, "body_bbox": {...}, "pins": [...]},
    "2": {...}
  }
}
```

- Every `units[unit]` carries that unit's own anchor, pins, and world
  `body_bbox`; the ambiguous top-level `position`, flat `pins`, and the
  merged union `body_bbox` are gone (a union of far-apart units is
  meaningless for per-unit reasoning). Sheets are opaque single-unit
  placeholders nested under `units["1"]`.
- `netlist_parser.iter_component_pins()` flattens the nested pins for
  netlist tracing, tools, and reports; `first_unit_position()` gives the
  lowest-numbered unit's anchor; `component_body_bbox()` fuses the unit
  bboxes for callers that need the whole occupied region (overlap
  avoidance, group membership).
- `move_component` takes **deltas**: `x`/`y` are shifts in mm (snapped to
  the 1.27 mm grid), `rotation` is an incremental angle applied as
  `old + rotation` per unit — reading an absolute target from the netlist
  and subtracting gives the delta. `unit=N` moves/rotates one unit
  individually; omitted moves every unit by the same delta, preserving
  relative layout (the old absolute write collapsed all units onto the
  target anchor). The overlap-avoidance search excludes the moved
  reference itself, so a unit's stale position never blocks the move.

### Validation

- Unit: `tests/unit/utils/test_netlist_multi_unit.py` — units nested with
  per-unit anchors, pins, and body_bbox, no top-level pins/position/bbox,
  single-unit components nested under unit 1;
  `tests/unit/tools/test_symbol_edit_tools.py` `TestMoveComponentMultiUnit` —
  whole delta move preserves offsets and rotations, unit-scoped
  move/rotate touches only that unit, validation errors.
- Real board: `two_ax_PCB.kicad_sch` U2 — `units["1"]` @ (101.6, 93.8022,
  90°) with 55 pins, `units["2"]` @ (185.42, 223.3422, 90°) with 9 power
  pins; 64 pins total, matching the coordinate-cluster split above.

---

## NPTH pad type index bug in world_model

**Status:** fixed for circular NPTH on `fix/duplicate-pad-routing`
(commit after `77e8301`); oval/slot drill support remains open (see
sub-entry below)

### Symptom

`kcaa/router/world_model.py` uses the wrong index when checking whether a pad
node is a non-plated through-hole (NPTH) pad:

- `_pad_obstacle` checks `pad_node[1] == "np_thru_hole"` to decide whether to
  skip the pad
- `_npth_obstacle` checks `pad_node[1] != "np_thru_hole"` to decide whether to
  skip the hole

### Evidence

Pad node structure (confirmed from real KiCad files, e.g.
`/home/user1/pcb/ninja-keyboard/ninja-keyboard.kicad_pcb`, 309 NPTH pads):

```
['pad', '', 'np_thru_hole', 'circle', [at ...], [size ...], [drill ...], [layers '*.Cu' '*.Mask']]
    [0]   [1]      [2]          [3]
   token  name    type        shape
```

- Pad **name** is at `[1]` (empty string for NPTH), pad **type** is at `[2]`.
- All 309 ninja-keyboard NPTH pads have the same layout; `sub[2] ==
  'np_thru_hole'` matched all 309, `sub[1]` matched none.
- Behavioral check: `_npth_obstacle` created 0/309 obstacles on
  ninja-keyboard; world model reported 0 `drill`-kind obstacles for a board
  with 309 NPTH holes.

### Impact

- `_npth_obstacle` always returns `None`: NPTH drill holes are never
  registered as obstacles.
- `_pad_obstacle` never skips NPTH pads: they are treated as ordinary plated
  pads. In practice the KiCad-written `layers "*.Cu" "*.Mask"` makes them
  rectangular pad-kind obstacles across all copper layers, so holes are
  still roughly blocked — but as axis-aligned rectangles from `size`, not as
  circular drill obstacles.
- Legacy/hand-written files without a `layers` node (e.g. the fixture in
  `tests/unit/tools/fixtures/test_group_placement.kicad_pcb`) produce **no**
  obstacle at all for NPTH holes — tracks could cross them.
- Side effect: `pad-kind` count inflates (~309 on ninja-keyboard) and
  clearance semantics use the rectangular pad envelope rather than the
  drilled circle.

### Fix

In `_pad_obstacle` and `_npth_obstacle`, read the type from `pad_node[2]`
with `str()` conversion (the values are `sexpdata.Symbol`, which does not
compare equal to `str`):

```python
pad_type = str(pad_node[2]) if len(pad_node) > 2 else ""
```

Also note the equivalent check at `router.py` (~line 1634,
`sub[2] == "np_thru_hole"`) already uses the correct index.

### Validation

- Unit: fixture with NPTH pads (`test_group_placement.kicad_pcb`) should
  yield `drill`-kind obstacles for its two NPTH holes.
- Real board: ninja-keyboard world model should report ~309 `drill`-kind
  obstacles instead of 0.
---

## Edge.Cuts internal openings are not routing obstacles

**Status:** fixed on `fix/duplicate-pad-routing` (commit after `de8ff31`);
implemented as `_edge_cuts_openings` + `opening`-kind world-model obstacles
(see below for the original analysis)

### Symptom

`kcaa/router/world_model.py` only uses Edge.Cuts for two things:

- `_board_bbox` — the AABB of all Edge.Cuts items (outer envelope),
- `router._check_segments_in_board` — verifies segments stay inside that
  AABB rectangle.

Neither models **internal openings**: closed Edge.Cuts loops fully inside
the board (cutouts, routing slots, mounting windows). The router treats
the board as a solid rectangle and can place tracks across a physical
void — a real fabrication defect.

### Evidence

Real board `/home/user1/pcb/ninja-keyboard/ninja-keyboard.kicad_pcb`:

- 76 Edge.Cuts items total; **61 internal `fp_rect` openings** (3.4 x 3.0 mm,
  fully inside the board AABB, e.g. SL61 at x[430.6, 434.0] y[251.0, 254.0])
  plus 7 `gr_circle` items.
- `world_model` builds **0** obstacles from any of them; only the outer
  AABB `(158.475, 139.475, 444.225, 256.725)` is used for the board fence.
- A* would happily route across any of those 61 holes.

### Impact

Tracks routed across a cutout are floating copper — electrically valid,
fabrication-invalid (the copper has no substrate, and the DRC clearance
rules around the cutout edge are not honored). KiCad will flag it in DRC,
but the router should avoid producing it.

### Fix (proposed)

In `world_model.py`:

1. Collect all Edge.Cuts graphics (board-level `gr_*` and footprint-level
   `fp_*`, transformed into world coordinates like `_board_bbox` does) as
   shapely linework.
2. `unary_union` the linework, `polygonize` it, take the outer face (the
   one with maximum area), and read its `interiors` — each interior ring
   is a closed opening loop.
3. Add one obstacle per opening: polygon = the ring, layers = ALL copper
   layers (a cutout severs every layer), kind = `"opening"`, net = None.

The router's existing `_inflate_obstacles` (width/2 + clearance) then
applies automatically — tracks keep full clearance from the opening edge.

Open loops (notches open to the board edge, like the micro:bit card bay)
are NOT openings — they stay part of the outline AABB, and the
`_check_segments_in_board` fence still applies.

### Validation

- Unit: fixture board with an internal rectangle + circle opening →
  `world_model` yields `opening`-kind obstacles at the right position;
  route that would cross the opening detours around it.
- Real board: ninja-keyboard world model reports 61+ `opening` obstacles
  (one per internal fp_rect); regression-check matrix-bit-card J2/3 ->
  U11/3v3 route is still produced (bay is an open notch, not an opening).

## NPTH oval/slot drill shapes are not supported

**Status:** open (discovered while fixing the NPTH index bug)

### Symptom

`_npth_obstacle` only reads a single drill value:

```python
drill = float(drill_sub[1])
```

A KiCad oval drill node is `(drill oval <width> <length>)` — `drill_sub[1]`
is the `Symbol('oval')` tag, so `float()` raises `TypeError` and the
function returns `None`. Oval/slot NPTH holes are silently dropped from the
world model.

### Evidence

Real KiCad file `/home/user1/pcb/ninja-keyboard/.history/ninja-keyboard.kicad_pcb`:

```
(pad "MP" thru_hole rect (at 7.5 -3.1 180) (size 3 2.5)
    (drill oval 2.5 2) (layers "*.Cu" "*.Mask") ...)
```

Multiple `(drill oval w l)` nodes exist in the file history. The router
would generate no obstacle for any of them. (The current
`ninja-keyboard.kicad_pcb` uses 309 circular NPTH drills only.)

### Unresolved question

KiCad's oval-drill semantics for `(drill oval w l)` is not confirmed from
primary source at write time:
- Is `l` the total slot length (outer ends) or the center-to-center
  distance of the two end circles?
- Does the slot's major axis follow the pad's own `at` rotation, the
  footprint rotation, or neither (fixed along one axis)?

The pad examples show mixed data (`(size 3 2.5)` with `(drill oval 2.5 2)`;
`(size 1 1.6)` with `(drill oval 0.6 1.2)`) — size and drill axes do not
obviously align, so the axis rule needs verification before implementing.

### Fix (proposed)

Shape the obstacle as a stadium/capsule: `LineString` between the two end
circle centers, buffered by `width / 2` (shapely:
`LineString([(-d, 0), (d, 0)]).buffer(w / 2, cap_style="round")`), where
`d` depends on the resolved `l` semantics. Rotate by the pad's own `at`
rotation plus the footprint rotation (verify which KiCad actually applies).

### Validation

- Unit: NPTH oval pad node -> obstacle shape whose bounding box matches
  the resolved slot extent.
- Real board: ninja-keyboard history file with `(drill oval ...)` pads
  yields `drill`-kind obstacles.
