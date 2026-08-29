"""Tests for sym_pin_world_coords (skip rotate90degrees position workaround).

Regression for docs/skip_library_notes.md §6: skip's
``AtValue.rotate90degrees`` rotates the wrong way (CW in lib Y-up space), so
its ``SymbolPin.location`` misplaces every pin of 90°/270°-rotated symbols.
The world position must come from the CCW matrix
(``symbol_geometry._rotate_lib_point``); angles use the CCW file-angle
exit conversion ``(angle + 180) % 360`` (wire-exit = stub reversed).
"""

from types import SimpleNamespace

import pytest

from kcaa.utils.skip_helpers import sym_pin_world_coords


def _lib_pin(number, name, x, y, angle, etype="passive"):
    return SimpleNamespace(
        number=SimpleNamespace(value=number),
        name=SimpleNamespace(value=name),
        at=SimpleNamespace(value=[x, y, angle]),
        wrapped_parsed_value=SimpleNamespace(value=[etype]),
    )


class _SymbolPin:
    """Mimic skip's per-unit SymbolPin wrapper (multi-pin path)."""

    def __init__(self, lib_pin, number, name):
        self._lib_sym_pin = lib_pin
        self.number = number
        self.name = name


def _sym(x, y, rot, lib_pins, pins=None, mirror=None):
    """Build a mock placed symbol.

    ``pins`` defaults to one ``_SymbolPin`` per lib pin (normal path);
    passing ``pins=False`` omits the attribute so the fallback path runs.
    """
    resolved = (
        [_SymbolPin(lp, lp.number.value, lp.name.value) for lp in lib_pins]
        if pins is None
        else pins
    )
    kwargs = {
        "at": SimpleNamespace(value=[x, y, rot]),
        "lib_symbol": SimpleNamespace(pin=lib_pins),
        "mirror": SimpleNamespace(value=mirror),
    }
    if resolved is not False:
        kwargs["pin"] = resolved
    return SimpleNamespace(**kwargs)


def _one(sym):
    coords = sym_pin_world_coords(sym)
    assert len(coords) == 1, f"expected exactly 1 pin, got {len(coords)}"
    return coords[0]


class TestNormalPath:
    @pytest.mark.parametrize(
        "lib_x,lib_y,lib_angle,sym_x,sym_y,sym_rot,ex_x,ex_y,ex_angle",
        [
            # 0°: identity (sx+lx, sy-ly)
            (4.0, 2.0, 180.0, 10.0, 20.0, 0, 14.0, 18.0, 0.0),
            # 90°: CCW in lib (x,y) -> (-y,x)
            (4.0, 2.0, 180.0, 10.0, 20.0, 90, 8.0, 16.0, 90.0),
            # 180°: (x,y) -> (-x,-y) — identical to the old rotate90degrees
            # path, guarded so the CW/CCW change cannot regress it
            (4.0, 2.0, 180.0, 10.0, 20.0, 180, 6.0, 22.0, 180.0),
            # 270°: (x,y) -> (y,-x)
            (4.0, 2.0, 180.0, 10.0, 20.0, 270, 12.0, 24.0, 270.0),
        ],
    )
    def test_four_directions(
        self, lib_x, lib_y, lib_angle, sym_x, sym_y, sym_rot, ex_x, ex_y, ex_angle
    ):
        pin = _one(_sym(sym_x, sym_y, sym_rot, [_lib_pin("1", "P", lib_x, lib_y, lib_angle)]))
        assert (pin.x, pin.y) == pytest.approx((ex_x, ex_y))
        assert pin.angle == pytest.approx(ex_angle)

    def test_u2a_pc11_world_position(self):
        """U2A pin 52 (PC11) on two_ax_PCB.kicad_sch.

        lib at (-78.74, 5.08, 270°), placement (101.6, 93.8022, 90°).
        CCW transform gives (96.52, 172.5422); skip's CW rotate90degrees
        emitted (106.68, 15.0622).
        """
        pin = _one(_sym(101.6, 93.8022, 90, [_lib_pin("52", "PC11", -78.74, 5.08, 270)]))
        assert (pin.x, pin.y) == pytest.approx((96.52, 172.5422), abs=1e-3)
        assert pin.angle == 180.0  # (270+90)=0 → (0+180)%360 → exit left


class TestMirroredPath:
    def test_mirror_y_flips_lx(self):
        # mirror "y" flips lib x; angle 180 → +180 → 0 → exit (0+180)%360=180.
        pin = _one(_sym(0.0, 0.0, 0, [_lib_pin("1", "P", 4.0, 2.0, 180)], mirror="y"))
        assert (pin.x, pin.y) == pytest.approx((-4.0, -2.0))
        assert pin.angle == 180.0

    def test_mirror_x_flips_ly(self):
        pin = _one(_sym(0.0, 0.0, 0, [_lib_pin("1", "P", 4.0, 2.0, 180)], mirror="x"))
        assert (pin.x, pin.y) == pytest.approx((4.0, 2.0))  # ly 2 → -2; sy-(-2)=2
        assert pin.angle == 180.0


class TestFallbackPath:
    def test_single_pin_symbol_uses_lib_definition(self):
        """Skip yields no SymbolPin wrappers for VCC/GND-style single-pin
        symbols; the raw lib definition must still produce the pin."""
        sym = _sym(5.0, 6.0, 90, [_lib_pin("1", "VCC", 0.0, 2.54, 270)], pins=False)
        pin = _one(sym)
        # lib (0, 2.54) rot90 → (-2.54, 0); world (5-2.54, 6) = (2.46, 6)
        assert (pin.x, pin.y) == pytest.approx((2.46, 6.0))
        assert pin.angle == 180.0  # (270+90)=0 → (0+180)%360 → exit left
        assert pin.name == "VCC"
        assert pin.electrical_type == "passive"
