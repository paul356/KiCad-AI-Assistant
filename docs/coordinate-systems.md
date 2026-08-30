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
2. **Screen notation (kcaa output)**: identical to the file notation —
   CCW, 0=right 90=up 180=left 270=down.  Used by `PinWorldCoords.angle`,
   `netlist_parser _angle_to_direction_screen`, `wire_edit_tools._dir_vec`
   (was a separate CW notation; unified in the CCW-angle change).
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
- **Angle conversion** (lib stub angle → kcaa wire-exit angle, both in the
  same CCW file-angle notation):

```
exit_angle = (angle_lib + 180) % 360      # stub is tip→body; exit is body→tip
```

  0=right, 90=up, 180=left, 270=down in both.  (An earlier draft encoded
  90/270 with a mirrored CW table via `(540 − angle_lib) % 360`; the
  output was visually identical, and the code now uses the plain
  stub-reversal form above.)

### Verified example (ODrive v3 `two_ax_PCB.kicad_sch`)

U2A pin 52 (PC11): lib `(−78.74, 5.08, 270°)`, placement
`(101.6, 93.8022, 90°)`.

```
offset = (−y, −x) = (−5.08, +78.74)
world  = (101.6 − 5.08, 93.8022 + 78.74) = (96.52, 172.54)
exit angle: rotation = 270 + 90 ≡ 0 → (0 + 180) % 360 = 180° = "left"
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
  `kcaa/router/world_model.py::_rotate_ccw_on_screen`: both kcaa formulas
  rotate counter-clockwise on screen (right → up at +90°), matching the
  KiCad file convention.
- Verified against the real board `two_ax_PCB.kicad_pcb`: of pads whose
  two candidate positions hit track/via endpoints for exactly one matrix,
  math-CCW won **81 : 30** (rot 90°: 17 : 7, rot 270°: 64 : 23; rot 180°
  degenerate-equal as expected).  Consistent with the official manual
  (PCBNew `R` hotkey = counter-clockwise, Shift+R = clockwise).
- Pad angles: KiCad stores pad rotation as an absolute board-space angle
  in the same CCW file convention (`pcb_query_tools.get_footprint` and
  `pcb_board_utils.get_fp_edge_cuts_items` docstrings use "CCW+" for it,
  matching the matrix above).
- Arc drawing helpers (`pcb_board_utils.add_gr_arc`) take angles in the
  same CCW file convention (0°=+X, 90°=up; point = `cx + r·cosθ,
  cy − r·sinθ`).

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

File-angle notation (kcaa output: `PinWorldCoords.angle`,
`netlist_parser`, `wire_edit_tools`):

| angle | direction |
|---|---|
| 0   | right |
| 90  | up    |
| 180 | left  |
| 270 | down  |

Lib notation (symbol editing contexts, `symbol_tools._lib_angle_to_direction`
— also CCW, in the Y-up lib frame):

| angle | direction |
|---|---|
| 0   | right |
| 90  | up    |
| 180 | left  |
| 270 | down  |

Both tables use the same CCW angle values; the lib one is interpreted in
the Y-up library frame before placement.

## 6. Rules of thumb for new code

1. Convert at the boundary, once: all downstream consumers should receive
   direction **strings** or a single documented notation.
2. Never reuse skip's `rotate90degrees()` for positions (see
   `docs/skip_library_notes.md` §6); use `symbol_geometry._rotate_lib_point`.
3. State the notation of every angle you store or pass; a bare `90` is
   ambiguous between lib (up) and screen (down).
4. Y-flip belongs to the **position** transform only; angle conversion is a
   numeric notation swap — keep them separate.
5. **Resolved (CCW unification)**: kcaa once had a separate CW screen
   notation (`PinWorldCoords.angle`, 0=right, 90=down) as the sole CW
   island; it was unified to the CCW file-angle convention.  Every angle
   value still states its notation.
6. Pin exit angle = stub angle reversed: lib pin `(at … angle)` is
   tip→body, the wire-exit direction is body→tip, i.e.
   `exit = (stub_angle + 180) % 360` in the same CCW notation.