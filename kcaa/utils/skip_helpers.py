"""Helpers for working around known bugs in the skip library.

The skip library fails to expose ``SymbolPin`` wrapper objects for
single-pin symbols (power nets like VCC/GND/PWR_FLAG and test-point
footprints).  When ``sym.pin`` is iterated on such symbols Python falls back
via ``__getattr__`` to the raw ``ParsedValue``, yielding the pin's children
(number string and UUID) instead of ``SymbolPin`` objects.  Every
``pin.number`` / ``pin.location`` access then raises ``AttributeError``
silently, leaving callers with no pin data.

:func:`sym_pin_world_coords` provides a single canonical implementation.
It iterates the per-unit ``SymbolPin`` wrappers when skip provides them and
falls back to the raw lib-symbol definition for single-pin symbols; in both
cases the pin position is computed here with the correct CCW rotation,
because skip's own ``SymbolPin.location`` uses ``AtValue.rotate90degrees``,
which rotates the wrong way (CW in lib space) and misplaces every pin of
90°/270°-rotated symbols (see ``docs/skip_library_notes.md`` §6).
"""

from __future__ import annotations

import copy
import logging
from typing import Any, NamedTuple

log = logging.getLogger(__name__)


class PinWorldCoords(NamedTuple):
    """Absolute schematic position and exit direction of one pin."""

    number: str  # pin number label, e.g. "1", "2", "A3"
    name: str  # pin name, e.g. "VCC", "GND", "GPIO1"
    electrical_type: str  # e.g. "input", "output", "bidirectional", "power_in"
    x: float  # absolute schematic X (mm)
    y: float  # absolute schematic Y (mm)
    angle: float  # wire-exit direction in degrees (KiCad file-angle
    #   convention, CCW on screen): 0=right, 90=up, 180=left, 270=down


def _pin_electrical_type(pin: Any) -> str:
    """Extract the electrical type from a SymbolPin.

    Accesses the pin's underlying library symbol definition to read the
    electrical type string (e.g. "input", "output", "power_in").
    """
    try:
        return str(pin._lib_sym_pin.wrapped_parsed_value.value[0])
    except Exception:
        return ""


def _lib_pin_electrical_type(lib_pin: Any) -> str:
    """Extract the electrical type from a LibSymbolPin."""
    try:
        return str(lib_pin.wrapped_parsed_value.value[0])
    except Exception:
        return ""


def _pin_world_from_lib(
    lib_pin: Any,
    sym_at: Any,
    mirror_val: str | None,
    placement_rot: int,
    num: str,
    name: str,
    etype: str,
) -> PinWorldCoords:
    """Compute one pin's world coords from its lib-space definition.

    Applies the same chain as
    :func:`kcaa.utils.symbol_geometry.lib_bbox_to_world`, in lib (Y-up, CCW)
    space:

      1. mirror (``"x"`` flips lib x → -x; ``"y"`` flips lib y → -y),
      2. placement rotation (CCW, via ``symbol_geometry._rotate_lib_point``),
      3. translate by the anchor,
      4. Y-flip: ``world_y = sym_y - rel_y``.

    The pin exit angle (wire-leave direction) is the stub angle reversed:
    the lib pin angle is tip-to-body in the CCW lib frame, so the
    body-to-tip exit direction in the same CCW file-angle convention is
    ``exit = (angle + 180) % 360`` (0=right, 90=up, 180=left, 270=down on
    screen).

    Mirror axis semantics: KiCad's ``(mirror x)`` negates lib *x* (flip
    through the vertical axis) and ``(mirror y)`` negates lib *y*.  A
    negated axis reverses that axis' cardinal directions: x-flip swaps
    0°/180°, y-flip swaps 90°/270° (angles along the flipped axis get
    +180°, the other two are unchanged).  Verified against
    ``MotorCell.kicad_sch`` J14 (``(mirror x)`` at 90°): the three pin
    wire endpoints sit at x = anchor − 2.54, i.e. lib x is negated —
    the earlier label-swapped implementation put them at x = anchor + 2.54.
    """
    from kcaa.utils.symbol_geometry import _rotate_lib_point

    rel_raw = [float(v) for v in copy.deepcopy(lib_pin.at.value)]
    lx = rel_raw[0]
    ly = rel_raw[1]
    lib_angle = float(rel_raw[2]) if len(rel_raw) > 2 else 0.0

    if mirror_val == "x":
        lx = -lx
        if lib_angle % 180 == 0:
            lib_angle = (lib_angle + 180) % 360
    elif mirror_val == "y":
        ly = -ly
        if lib_angle % 180 != 0:
            lib_angle = (lib_angle + 180) % 360

    # KiCad rotates symbols counter-clockwise in the lib Y-up frame.  Note:
    # skip's rotate90degrees() is CW and wrong for 90°/270° positions (error
    # (2·y, 2·x) — see docs/skip_library_notes.md §6); the angle accumulates
    # the same number of 90° steps either way, so only x/y are affected here.
    rx, ry = _rotate_lib_point(lx, ly, placement_rot)

    wx = round(sym_at.x + rx, 4)
    wy = round(sym_at.y - ry, 4)  # lib Y axis is flipped

    stub_angle = (lib_angle + placement_rot) % 360.0
    return PinWorldCoords(
        number=num,
        name=name,
        electrical_type=etype,
        x=wx,
        y=wy,
        # wire-exit = stub angle reversed, CCW file-angle convention
        angle=(stub_angle + 180.0) % 360.0,
    )


def sym_pin_world_coords(sym: Any) -> list[PinWorldCoords]:
    """Return world coordinates and exit angle for every pin of a placed symbol.

    Handles the known skip library bug for single-pin symbols (power symbols
    such as VCC, GND, PWR_FLAG and TestPoint footprints) by falling back to
    the raw lib-symbol definition when the ``SymbolPin`` wrapper path yields
    no results.

    Positions are always computed here from lib pin definitions — skip's
    ``SymbolPin.location`` relies on ``AtValue.rotate90degrees``, which is CW
    and misplaces pins of 90°/270°-rotated symbols (docs/skip_library_notes.md
    §6).  Angles use the CCW file-angle exit conversion ``(angle + 180) % 360``.

    Args:
        sym: A placed symbol object from a ``skip.Schematic`` (the items
            yielded by ``sch.symbol``).

    Returns:
        List of :class:`PinWorldCoords` named tuples, one per pin.  Empty on
        any unrecoverable error.
    """
    from skip.at_location import AtValue  # local to avoid circular imports

    results: list[PinWorldCoords] = []

    try:
        sym_at = AtValue(sym.at.value)
    except Exception:
        return results

    # Determine mirroring (if any)
    mirror_val: str | None = None
    try:
        mv = sym.mirror.value
        mirror_val = mv.value() if hasattr(mv, "value") else mv
    except AttributeError:
        pass

    placement_rot = int(round(float(sym_at.rotation))) % 360

    # ---- Normal path: per-unit SymbolPin wrappers -------------------------
    # Works for multi-pin components.  Each SymbolPin carries its own
    # per-unit lib definition (``_lib_sym_pin``), which we use to recompute
    # the position with the correct CCW matrix instead of skip's
    # ``pin.location`` (wrong for 90°/270°).
    had_wrappers = False
    try:
        for pin in sym.pin:
            had_wrappers = True
            try:
                results.append(
                    _pin_world_from_lib(
                        pin._lib_sym_pin,
                        sym_at,
                        mirror_val,
                        placement_rot,
                        str(pin.number),
                        str(pin.name) if pin.name else "",
                        _pin_electrical_type(pin),
                    )
                )
            except AttributeError:
                continue
    except (AttributeError, TypeError):
        pass

    if not results and had_wrappers:
        # Skip provided SymbolPin wrappers but every one failed — the raw
        # lib-symbol fallback below drops per-unit filtering, which would
        # mix the units of a multi-unit symbol.  Make the degradation
        # visible instead of returning silently wrong pin data.
        try:
            ref = sym.property.Reference.value
        except AttributeError:
            ref = "<unknown>"
        log.warning(
            "sym_pin_world_coords: no pin data from SymbolPin wrappers for %s; "
            "falling back to the raw lib definition (per-unit filtering lost)",
            ref,
        )

    if not results:
        # ---- Fallback path: raw lib-symbol definition ---------------------
        # Triggered for power symbols, PWR_FLAG, TestPoint, etc., where skip's
        # SymbolPin wrapper is not produced by sym.pin iteration (single-pin
        # symbols degrade to a raw ParsedValue).
        try:
            lib_sym = sym.lib_symbol
            if lib_sym is None:
                return results
            for lib_pin in lib_sym.pin:
                try:
                    num = str(lib_pin.number.value)
                    name = str(lib_pin.name.value) if lib_pin.name else ""
                    etype = _lib_pin_electrical_type(lib_pin)
                    results.append(
                        _pin_world_from_lib(
                            lib_pin,
                            sym_at,
                            mirror_val,
                            placement_rot,
                            num,
                            name,
                            etype,
                        )
                    )
                except Exception:
                    log.debug("Failed to get world coordinates for pin %s", num)
                    continue
        except Exception:
            log.debug("Failed to get pin world coordinates")

    return results
