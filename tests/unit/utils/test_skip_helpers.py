"""Tests for sym_pin_world_coords (skip rotate90degrees position workaround).

Regression for docs/skip_library_notes.md §6: skip's
``AtValue.rotate90degrees`` rotates the wrong way (CW in lib Y-up space), so
its ``SymbolPin.location`` misplaces every pin of 90°/270°-rotated symbols.
The world position must come from the CCW matrix
(``symbol_geometry._rotate_lib_point``); angles use the CCW file-angle
exit conversion ``(angle + 180) % 360`` (wire-exit = stub reversed).
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from kcaa.utils.skip_helpers import sym_pin_world_coords

SINGLE_PIN_FIXTURE = str(Path(__file__).parent / "fixtures" / "single_pin_symbol.kicad_sch")


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


class _RaisingPinIter:
    """A ``sym.pin`` iterator that yields one wrapper, then raises."""

    def __init__(self, pin):
        self._pin = pin

    def __iter__(self):
        yield self._pin
        raise TypeError("pin iterator exploded")


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
    def test_mirror_x_flips_lx(self):
        # KiCad (mirror x) negates lib x; angle 180 → +180 → 0 →
        # exit (0+180)%360 = 180 (left).
        pin = _one(_sym(0.0, 0.0, 0, [_lib_pin("1", "P", 4.0, 2.0, 180)], mirror="x"))
        assert (pin.x, pin.y) == pytest.approx((-4.0, -2.0))
        assert pin.angle == 180.0

    def test_mirror_y_flips_ly(self):
        # KiCad (mirror y) negates lib y; angle 180 lies along the
        # unaffected axis (0/180 stay put under a y-flip) → exit 0 (right).
        pin = _one(_sym(0.0, 0.0, 0, [_lib_pin("1", "P", 4.0, 2.0, 180)], mirror="y"))
        assert (pin.x, pin.y) == pytest.approx((4.0, 2.0))  # ly 2 → -2; sy-(-2)=2
        assert pin.angle == 0.0

    def test_mirror_y_flips_angle_90_to_270(self):
        # A y-flip swaps the 90°/270° directions (up ↔ down).
        pin = _one(_sym(0.0, 0.0, 0, [_lib_pin("1", "P", 4.0, 2.0, 90)], mirror="y"))
        assert (pin.x, pin.y) == pytest.approx((4.0, 2.0))
        assert pin.angle == 90.0  # stub 90 → 270 → exit (270+180)%360 = 90 (up)

    def test_j14_mirror_x_rot90_matches_kicad(self):
        """Regression: MotorCell.kicad_sch J14 — (at 368.3 178.8922 90)
        (mirror x), lib pin 1 at (1.27, 2.54, 270°).

        KiCad's own wire endpoints (and skip's SymbolPin.location) put the
        pin at (365.76, 180.1622) exiting left — i.e. lib x is NEGATED.
        The label-swapped implementation emitted (370.84, 177.6222) "right".
        """
        pin = _one(_sym(368.3, 178.8922, 90, [_lib_pin("1", "P", 1.27, 2.54, 270)], mirror="x"))
        assert (pin.x, pin.y) == pytest.approx((365.76, 180.1622))
        assert pin.angle == 180.0  # wire-exit left, towards the wires


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

    def test_single_pin_fallback_rotation_90(self):
        """Fallback pins use the same CCW placement transform as wrappers."""
        pin = _one(_sym(100.0, 100.0, 90, [_lib_pin("1", "P", 0.0, 2.54, 270)], pins=False))
        # lib (0, 2.54) rot90 → (-2.54, 0); world (100-2.54, 100) = (97.46, 100)
        assert (pin.x, pin.y) == pytest.approx((97.46, 100.0))
        assert pin.angle == 180.0

    def test_single_pin_fallback_mirror_x(self):
        """Fallback pins honour ``(mirror x)``."""
        pin = _one(
            _sym(
                100.0,
                100.0,
                0,
                [_lib_pin("1", "P", 0.0, 2.54, 270)],
                pins=False,
                mirror="x",
            )
        )
        # lib (0, 2.54) mirror x → (0, 2.54); world (100, 100-2.54) = (100, 97.46)
        assert (pin.x, pin.y) == pytest.approx((100.0, 97.46))
        assert pin.angle == 90.0

    def test_multi_pin_fallback_warns_about_lost_unit_filtering(self, caplog):
        """A multi-pin lib definition reached through the fallback may mix
        units, so the degradation must remain visible."""
        lib_pins = [
            _lib_pin("1", "P", 0.0, 2.54, 270),
            _lib_pin("2", "", 0.0, -2.54, 90),
        ]
        sym = _sym(
            100.0,
            100.0,
            0,
            lib_pins,
            pins=[SimpleNamespace(), SimpleNamespace()],
        )
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert len(coords) == 2
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno >= logging.WARNING
        ]
        assert any("per-unit filtering lost" in message for message in warnings)

    def test_wrapperless_multi_pin_fallback_warns(self, caplog):
        """The multi-unit warning must also fire when no wrappers exist."""
        lib_pins = [
            _lib_pin("1", "P", 0.0, 2.54, 270),
            _lib_pin("2", "", 0.0, -2.54, 90),
        ]
        sym = _sym(100.0, 100.0, 0, lib_pins, pins=False)
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert len(coords) == 2
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno >= logging.WARNING
        ]
        assert any("per-unit filtering lost" in message for message in warnings)

    def test_fallback_failure_emits_single_summary_warning(self, caplog):
        """One root cause -> exactly one warning, with ref and first error."""
        bad_pin = _lib_pin("2", "Q", 0.0, -2.54, 90)
        bad_pin.at = SimpleNamespace(value=["bad", 0.0, 0.0])
        sym = _sym(
            100.0,
            100.0,
            0,
            [_lib_pin("1", "P", 0.0, 2.54, 270), bad_pin],
            pins=False,
        )
        sym.property = SimpleNamespace(Reference=SimpleNamespace(value="U1"))
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert len(coords) == 1
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "failed to resolve 1 pin(s) of U1" in warnings[0]
        assert "could not convert string to float: 'bad'" in warnings[0]
        assert "per-unit filtering lost" not in warnings[0]

    def test_wrapper_pin_without_lib_at_logs_debug(self, caplog):
        """A wrapper whose lib pin has no ``at`` must not vanish silently."""
        good_pin = _lib_pin("1", "P", 0.0, 2.54, 270)
        broken_lib_pin = SimpleNamespace(
            number=SimpleNamespace(value="2"),
            name=SimpleNamespace(value="Q"),
            wrapped_parsed_value=SimpleNamespace(value=["passive"]),
        )  # no ``at``
        sym = _sym(
            100.0,
            100.0,
            0,
            [good_pin],
            pins=[_SymbolPin(good_pin, "1", "P"), _SymbolPin(broken_lib_pin, "2", "Q")],
        )
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert len(coords) == 1
        debug_messages = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno == logging.DEBUG
        ]
        assert any("failed to resolve wrapper pin" in message for message in debug_messages)

    def test_pin_iteration_failure_logs_debug(self, caplog):
        """A ``sym.pin`` iterator that stops early must log the drop."""
        lib_pin = _lib_pin("1", "P", 0.0, 2.54, 270)
        sym = _sym(
            100.0,
            100.0,
            0,
            [lib_pin],
            pins=_RaisingPinIter(_SymbolPin(lib_pin, "1", "P")),
        )
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert len(coords) == 1
        debug_messages = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno == logging.DEBUG
        ]
        assert any("stopped early" in message for message in debug_messages)

    def test_none_position_returns_empty_without_raising(self, caplog):
        """``sym.at.value is None`` degrades to no pins plus a DEBUG line."""
        sym = _sym(100.0, 100.0, 0, [_lib_pin("1", "P", 0.0, 2.54, 270)])
        sym.at = SimpleNamespace(value=None)
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(sym)
        assert coords == []
        debug_messages = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno == logging.DEBUG
        ]
        assert any("position of" in message and "is None" in message for message in debug_messages)


class TestSinglePinSymbolFixture:
    """Regression for #251: a one-pin part must still report its pin.

    ``Mechanical:MountingHole_Pad`` is a single-pin symbol.  skip yields a
    raw ``ParsedValue`` for ``sym.pin`` and exposes the lib pin's ``name`` /
    ``number`` as ``ParsedValue`` objects whose truthiness raises
    ``TypeError``; the fallback must read their ``.value`` instead.
    """

    @pytest.fixture(scope="class")
    def mgt1(self):
        from kcaa.utils.skip_compat import safe_schematic

        sch = safe_schematic(SINGLE_PIN_FIXTURE)
        return next(sym for sym in sch.symbol if sym.property.Reference.value == "MGT1")

    def test_single_pin_symbol_yields_exactly_one_pin(self, mgt1):
        coords = sym_pin_world_coords(mgt1)
        assert len(coords) == 1
        pin = coords[0]
        assert pin.number == "1"
        assert pin.name == "~"  # unnamed-pin marker, per the tool contract
        assert (pin.x, pin.y) == pytest.approx((100.0, 100.0))
        assert pin.electrical_type == "passive"
        assert pin.angle == 180.0  # non-None wire-exit direction: left

    def test_single_pin_fixture_does_not_warn(self, mgt1, caplog):
        with caplog.at_level(logging.DEBUG, logger="kcaa.utils.skip_helpers"):
            coords = sym_pin_world_coords(mgt1)
        assert len(coords) == 1
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kcaa.utils.skip_helpers" and r.levelno >= logging.WARNING
        ]
        assert warnings == []
