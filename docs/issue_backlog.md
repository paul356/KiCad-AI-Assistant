# Issue Backlog

Known issues discovered during review but not fixed in the originating PR.
Each entry records the evidence, the impact, and the proposed fix so the work
can be picked up independently.

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
