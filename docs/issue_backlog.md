# Issue Backlog

Known issues discovered during review but not fixed in the originating PR.
Each entry records the evidence, the impact, and the proposed fix so the work
can be picked up independently.

## NPTH pad type index bug in world_model

**Status:** open (pre-existing, not introduced by PR #82)

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