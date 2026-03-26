"""Helpers for working around known bugs in the skip library.

The skip library fails to expose ``SymbolPin`` wrapper objects for
single-pin symbols (power nets like VCC/GND/PWR_FLAG and test-point
footprints).  When ``sym.pin`` is iterated on such symbols Python falls back
via ``__getattr__`` to the raw ``ParsedValue``, yielding the pin's children
(number string and UUID) instead of ``SymbolPin`` objects.  Every
``pin.number`` / ``pin.location`` access then raises ``AttributeError``
silently, leaving callers with no pin data.

:func:`sym_pin_world_coords` provides a single canonical implementation that
tries the normal ``SymbolPin`` path first and falls back to manual
rotation/mirror math against the lib-symbol definition when the normal path
yields nothing.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple


class PinWorldCoords(NamedTuple):
    """Absolute schematic position and exit direction of one pin."""

    number: str    # pin number label, e.g. "1", "2", "A3"
    x: float       # absolute schematic X (mm)
    y: float       # absolute schematic Y (mm)
    angle: float   # wire-exit direction in degrees:
                   #   0=right, 90=down (screen), 180=left, 270=up (screen)


def sym_pin_world_coords(sym: Any) -> list[PinWorldCoords]:
    """Return world coordinates and exit angle for every pin of a placed symbol.

    Handles the known skip library bug for single-pin symbols (power symbols
    such as VCC, GND, PWR_FLAG and TestPoint footprints) by falling back to
    manual rotation/mirror math when the normal ``SymbolPin`` wrapper path
    yields no results.

    Args:
        sym: A placed symbol object from a ``skip.Schematic`` (the items
            yielded by ``sch.symbol``).

    Returns:
        List of :class:`PinWorldCoords` named tuples, one per pin.  Empty on
        any unrecoverable error.
    """
    from skip.at_location import AtValue  # local to avoid circular imports

    results: list[PinWorldCoords] = []

    # ---- Normal path via SymbolPin.location ---------------------------------
    # Works for multi-pin components where skip correctly wraps each pin as a
    # SymbolPin object with a .number attribute and a .location property that
    # accounts for the symbol's placement rotation and mirroring.
    try:
        for pin in sym.pin:
            try:
                num = str(pin.number)
                loc = pin.location
                results.append(PinWorldCoords(
                    number=num,
                    x=round(float(loc.x), 4),
                    y=round(float(loc.y), 4),
                    angle=float(loc.rotation),
                ))
            except AttributeError:
                continue
    except (AttributeError, TypeError):
        pass

    if results:
        return results

    # ---- Fallback path: manual rotation/mirror math -------------------------
    # Triggered for power symbols, PWR_FLAG, TestPoint, etc., where skip's
    # SymbolPin wrapper is not produced by sym.pin iteration.  We read the
    # lib-symbol pin definitions directly and replicate the same transform that
    # skip's SymbolPin.location property applies internally.
    try:
        lib_sym = sym.lib_symbol
        if lib_sym is None:
            return results

        sym_at = AtValue(sym.at.value)  # placed symbol: (x, y, rotation°)

        # Determine mirroring (if any)
        mirror_val: str | None = None
        try:
            mv = sym.mirror.value
            mirror_val = mv.value() if hasattr(mv, "value") else mv
        except AttributeError:
            pass

        for lib_pin in lib_sym.pin:
            try:
                num = str(lib_pin.number.value)
                rel_raw: list = copy.deepcopy(lib_pin.at.value)  # [x, y, angle]

                # Apply mirroring to lib-pin relative position
                rot = rel_raw[2]
                if mirror_val == "y":
                    rel_raw[0] = -rel_raw[0]
                    if rot % 180 == 0:
                        rel_raw[2] = (rot + 180) % 360
                elif mirror_val == "x":
                    rel_raw[1] = -rel_raw[1]
                    if rot % 90 == 0:
                        rel_raw[2] = (rot + 180) % 360

                rel_at = AtValue(rel_raw)
                manip_at = AtValue(copy.deepcopy(rel_raw))
                manip_at.rotation = 0

                # Rotate lib-pin position/angle to match placed symbol's rotation
                while manip_at.rotation != sym_at.rotation:
                    manip_at.rotate90degrees()
                    rel_at.rotate90degrees()

                wx = round(sym_at.x + rel_at.x, 4)
                wy = round(sym_at.y - rel_at.y, 4)  # lib Y axis is flipped
                results.append(PinWorldCoords(
                    number=num,
                    x=wx,
                    y=wy,
                    angle=float(rel_at.rotation),
                ))
            except Exception:
                continue
    except Exception:
        pass

    return results
