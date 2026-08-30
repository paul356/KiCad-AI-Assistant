"""Cross-cutting checks that every angle/coordinate convention agrees.

The KiCad file angle is CCW-positive on screen (0=right, 90=up, 180=left,
270=down) in every kcaa subsystem.  These tests pin down the *relations*
between the representations, not individual numbers:

1. PCB-side ``_rotate_ccw_on_screen`` (and router ``_rotate``) is the same
   transform as lib-side ``_rotate_lib_point`` followed by the Y-flip that
   the schematic code applies (``world_y = sym_y - ry``).
2. The wire-exit angle ``exit = (stub + 180) % 360`` points exactly
   opposite the stub: ``_dir_vec(exit) == -_dir_vec(stub)``, for any angle
   (including non-90° increments, which exercise the trig fallback).
3. The direction string mapping matches the unit vector the same angle
   produces on screen: right → (1,0), up → (0,-1), left → (-1,0),
   down → (0,1).
4. Schematic and PCB direction/angle tables all use the same CCW values.

Regression guard: if any subsystem reintroduces the old CW notation
(90 = down), a test here flips.
"""

import math

import pytest

from kcaa.router.router import _rotate
from kcaa.router.world_model import _rotate_ccw_on_screen
from kcaa.tools.symbol_edit_tools import _angle_to_direction
from kcaa.tools.wire_edit_tools import _dir_vec
from kcaa.utils.netlist_parser import _angle_to_direction_screen
from kcaa.utils.symbol_geometry import _rotate_lib_point

_ANGLES = (0, 90, 180, 270)
_ALL_ANGLES = (0, 45, 90, 135, 180, 225, 270, 315, -90)


@pytest.mark.parametrize("rot", _ANGLES)
@pytest.mark.parametrize("lx,ly", [(4.0, 2.0), (-4.0, 2.0), (0.0, 0.0), (1.5, -3.5)])
def test_pcb_rotate_equals_lib_rotate_plus_yflip(rot: int, lx: float, ly: float) -> None:
    """PCB screen-CCW matrix == lib Y-up CCW matrix then Y-flip.

    The schematic chain is: (rx, ry) = _rotate_lib_point(lx, ly, rot),
    then world = (sx + rx, sy - ry) — the Y-flip mirrors lib-up to
    screen-down.  A PCB footprint's local frame is already +Y down (same
    as board screen space), so the equivalent PCB-local point of a lib
    point is (lx, -ly); rotating that with the PCB screen-CCW matrix
    must equal the lib result (rx, -ry).
    """
    rx, ry = _rotate_lib_point(lx, ly, rot)
    assert _rotate_ccw_on_screen(lx, -ly, rot) == pytest.approx((rx, -ry))
    assert _rotate(lx, -ly, rot) == pytest.approx((rx, -ry))


@pytest.mark.parametrize("stub", _ALL_ANGLES)
def test_exit_angle_points_opposite_stub(stub: float) -> None:
    """exit = (stub + 180) % 360 must give the exact opposite vector.

    The lib pin angle is tip→body (stub); the wire-leave direction is
    body→tip.  For any stub angle the exit unit vector must be the
    negated stub unit vector — this is what makes the CW/CCW tables and
    the (a + 180) % 360 formula equivalent to the geometry.
    """
    exit_angle = (stub + 180.0) % 360.0
    stub_vec = _dir_vec(stub)
    exit_vec = _dir_vec(exit_angle)
    assert exit_vec == pytest.approx((-stub_vec[0], -stub_vec[1]))


@pytest.mark.parametrize(
    "angle,vector",
    [
        (0, (1.0, 0.0)),
        (90, (0.0, -1.0)),  # CCW on screen: up
        (180, (-1.0, 0.0)),
        (270, (0.0, 1.0)),  # CCW on screen: down
    ],
)
def test_direction_string_matches_unit_vector(angle: float, vector: tuple[float, float]) -> None:
    """The direction string and the screen unit vector must agree."""
    assert _dir_vec(angle) == pytest.approx(vector)
    if angle in _ANGLES:
        # Both tables are the same CCW mapping (schematic labels and pin
        # wire-exit directions consume angles identically).
        assert _angle_to_direction_screen(angle) == _angle_to_direction(angle)


@pytest.mark.parametrize("angle", _ALL_ANGLES)
def test_dir_vec_fallback_is_trig_consistent(angle: float) -> None:
    """Non-cardinal angles take the trig fallback, still CCW on screen.

    On screen (+Y down) a CCW angle θ points at (cos θ, -sin θ): +90°
    goes up (-Y), +45° goes up-right.
    """
    rad = math.radians(angle % 360.0)
    assert _dir_vec(angle) == pytest.approx((math.cos(rad), -math.sin(rad)))


def test_angle_tables_are_ccw_not_cw() -> None:
    """Guard against reintroducing the old CW notation (90 = down)."""
    assert _angle_to_direction_screen(90) == "up"
    assert _angle_to_direction_screen(270) == "down"
    assert _angle_to_direction(90) == "up"
    assert _angle_to_direction(270) == "down"
