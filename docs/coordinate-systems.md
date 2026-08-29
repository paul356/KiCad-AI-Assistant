# Coordinate Systems and Angle Conventions

Canonical reference for KiCad coordinate systems and how kicad-mcp code
converts between them.  Read this before writing any geometry code.

## 1. Overview

| Space | Data source | Axis directions | Angle convention | Reference point |
|---|---|---|---|---|
| **Library (symbol)** | `lib_symbols` in `.kicad_sch`, `.kicad_sym` | +X right, **+Y up** | **CCW** (math, `atan2`): 0=right, 90=up, 180=left, 270=down | symbol anchor |
| **Schematic (world)** | `(at x y rot)` of placed symbols | +X right, **+Y down** | File stores the same math/CCW numbers; screen shows CCW visually | sheet origin |
| **PCB / footprint** | `.kicad_pcb` `(at x y rot)` | +X right, **+Y down** | **CCW-positive** (0=right, 90=up visually); rot 90° → offset `(y, −x)` | board origin |

Three distinct angle notations appear in this codebase — always state which
one a value is in:

1. **Lib / file notation**: CCW, 0=right 90=up 180=left 270=down. This is
   what `EDA_ANGLE` (`libs/kimath/include/geometry/eda_angle.h`,
   `atan2(y, x)`) and all `.kicad_sym` / `.kicad_sch` angle fields use.
2. **Screen notation (kcaa output)**: clockwise, 0=right **90=down**
   180=left **270=up**. Used by `PinWorldCoords.angle`, `netlist_parser`
   `_angle_to_direction_screen`, `wire_edit_tools._dir_vec`.
3. **Direction strings**: `"right"|"down"|"left"|"up"` — screen-space visual
   meaning, no degrees, hence no ambiguity.  Prefer these across API
   boundaries.

## 2. Library symbol space (+Y up, CCW)

- Symbol body and pins are defined relative to the symbol anchor.
- **Pin `(at x y angle)`**: `(x, y)` is the **wire endpoint (connection
  point)**; the pin graphic runs from that point toward the body.
- **Pin angle semantics**: the **stub direction** (connection point → body),
  NOT the wire-exit direction.  Example: `STM32F405RGT6` PC11
  `(at -78.74 5.08 270)` sits above the body and points down into it.
- Mirroring (`mirror x|y`) flips one axis of every point and adds ±180° to
  pin angles.

## 3. Schematic (world) space (+Y down)

- A placed symbol `(at x y rot)` anchors its lib-space origin at `(x, y)`.
- KiCad rotates symbols **counter-clockwise** (`R` key; Shift+R clockwise,
  per the official manual).  Combined with the lib→screen Y flip, the net
  point transform for a 90° placement is:

```
world offset = (−y_lib, −x_lib)
```

- Full lib → world transform (order matters), as implemented in
  `kcaa/utils/symbol_geometry.py::lib_bbox_to_world`:

  1. mirror (flip lib x or y, adjust angle),
  2. rotate CCW in lib space (`_rotate_lib_point`: 0°→(x,y), 90°→(−y,x),
     180°→(−x,−y), 270°→(y,−x)),
  3. translate by the anchor,
  4. **Y-flip**: `world_y = anchor_y − rel_y` (this is where lib +Y up
     becomes screen +Y down).

- Pin world position therefore = `(ax + rx, ay − ry)` with `(rx, ry)` from
  step 2.
- **Angle conversion** (skip lib angle → kcaa screen exit angle):

```
exit_angle_screen = (540 − angle_lib) % 360      # (+180 stub→exit, then 360− for CCW→CW)
```

  `0/180°` are unaffected by the CCW→CW step (left/right same in both
  notations); only `90/270` swap, so test real up/down pins.

### Verified example (ODrive v3 `two_ax_PCB.kicad_sch`)

U2A pin 52 (PC11): lib `(−78.74, 5.08, 270°)`, placement
`(101.6, 93.8022, 90°)`.

```
offset = (−y, −x) = (−5.08, +78.74)
world  = (101.6 − 5.08, 93.8022 + 78.74) = (96.52, 172.54)
exit angle: rotation = 270 + 90 ≡ 0 → (540 − 0) % 360 = 180° = "left"
```

Matches KiCad's own wire endpoints on disk (all 64 pins of U2 hit wire
endpoints only with this transform — see `docs/skip_library_notes.md` §6).

## 4. PCB / footprint space (+Y down, CCW file angle)

- Board and footprint coordinates are mm, +X right, **+Y down**, and are
  the **front-view (F.Cu) projection**: the file coordinate plane maps
  1:1 to the screen in PCBNew's default front view.  The back view is only
  the camera flipped — the file never stores back-side coordinates, even
  for B.Cu items.
- B.Cu items share the same front-projection plane: footprint anchor,
  pads, and B.Cu tracks/vias all live in the one file coordinate system.
  That is why back-side parts look mirrored in the front view and why a
  B.Cu pad's world position matches a B.Cu track endpoint under the same
  section-4 matrix (no mirror/back-projection term exists).
- The file rotation is **CCW-positive**, the same convention as schematic
  symbols: on screen 0=right, 90=up, 180=left, 270=down.  The offset
  transform for a pad at local `(x, y)` is math CCW applied in the Y-down
  space, i.e. **math −d under the y-up convention**:

```
x' =  x·cos(d) + y·sin(d)
y' = −x·sin(d) + y·cos(d)        # d=90 → (x, y) → (y, −x)   → screen CCW
```

  Implemented in `kcaa/router/router.py::_rotate` and
  `kcaa/router/world_model.py::_rotate_cw_on_screen`.  Note that the kcaa
  name `_rotate_cw_on_screen` is a **misnomer**: the formula rotates
  counter-clockwise on screen (right → up at +90°); the code is right, the
  label is wrong.
- Verified against the real board `two_ax_PCB.kicad_pcb`: of pads whose
  two candidate positions hit track/via endpoints for exactly one matrix,
  math-CCW won **81 : 30** (rot 90°: 17 : 7, rot 270°: 64 : 23; rot 180°
  degenerate-equal as expected).  Consistent with the official manual
  (PCBNew `R` hotkey = counter-clockwise, Shift+R = clockwise).
- Pad angles: KiCad stores pad rotation as an absolute board-space angle
  in the same CCW file convention (the `pcb_query_tools` docstring calls
  it "CW+" — trust the matrix above, not the label).
- Arc drawing helpers (`pcb_board_utils`) expose a local **CW** angle API
  (0°=+X, angles increase clockwise; `sin(θ)` positive downward) — a
  drawing-helper convention, unrelated to the file-angle convention.

### B-side (B.Cu) footprints

Footprints are drawn for the F.Cu side.  KiCad's **flip** shortcut `F`
mirrors the part's elements vertically (the Y direction) while moving it
F.Cu ↔ B.Cu, so the geometry coordinates change: a B.Cu pad's local `ly`
is the negation of the same part's F.Cu `ly` (verified — same-name rot-0
parts on both sides of `two_ax_PCB.kicad_pcb` match exactly under ly
negation).  The flip only leaves the `(at … rot)` rotation field alone
(no +180 is stored).  The file therefore already contains the mirrored
geometry; kcaa reads it once and applies no further mirror or angle step
when computing world coordinates.

## 5. Direction-string mapping tables

Screen notation (kcaa output):

| angle | direction |
|---|---|
| 0   | right |
| 90  | down  |
| 180 | left  |
| 270 | up    |

Lib notation (symbol editing contexts, `symbol_tools._lib_angle_to_direction`):

| angle | direction |
|---|---|
| 0   | right |
| 90  | up    |
| 180 | left  |
| 270 | down  |

These two tables look inverted for 90/270 — that is **intentional**: they
consume angles in different notations.  If you consume one table where the
other is expected, up/down flip.

## 6. Rules of thumb for new code

1. Convert at the boundary, once: all downstream consumers should receive
   direction **strings** or a single documented notation.
2. Never reuse skip's `rotate90degrees()` for positions (see
   `docs/skip_library_notes.md` §6); use `symbol_geometry._rotate_lib_point`.
3. State the notation of every angle you store or pass; a bare `90` is
   ambiguous between lib (up) and screen (down).
4. Y-flip belongs to the **position** transform only; angle conversion is a
   numeric notation swap — keep them separate.
5. **Known inconsistency (TODO)**: KiCad uses CCW everywhere (symbols,
   schematic, footprints).  kcaa's own screen-notation output
   (`PinWorldCoords.angle`, 0=right, 90=down) is the sole CW island; it
   matches the direction strings used by netlist exports but is a constant
   source of confusion (see §5 tables).  Long-term direction: emit
   direction strings / unit vectors across API boundaries, or switch the
   angle notation to CCW; until then every angle value must declare its
   notation.