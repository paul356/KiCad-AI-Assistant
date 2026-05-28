"""
Helpers for working with footprint S-expression nodes inside a PCB tree.

Handles: locating footprints, reading/writing the `at` position, and
reading/writing named `property` entries.  The layer-flip map used by
flip_footprint also lives here.
"""

from typing import Any

import sexpdata

# ---------------------------------------------------------------------------
# Layer flip map — F.* ↔ B.*
# ---------------------------------------------------------------------------

LAYER_FLIP_MAP: dict[str, str] = {
    # KiCad 6 dot-separated names
    "F.Cu": "B.Cu",
    "B.Cu": "F.Cu",
    "F.Adhes": "B.Adhes",
    "B.Adhes": "F.Adhes",
    "F.Paste": "B.Paste",
    "B.Paste": "F.Paste",
    "F.SilkS": "B.SilkS",
    "B.SilkS": "F.SilkS",
    "F.Mask": "B.Mask",
    "B.Mask": "F.Mask",
    "F.Fab": "B.Fab",
    "B.Fab": "F.Fab",
    "F.Courtyard": "B.Courtyard",
    "B.Courtyard": "F.Courtyard",
    # KiCad 7+ underscore names
    "F_Cu": "B_Cu",
    "B_Cu": "F_Cu",
    "F_Fab": "B_Fab",
    "B_Fab": "F_Fab",
    "F_SilkS": "B_SilkS",
    "B_SilkS": "F_SilkS",
    "F_Mask": "B_Mask",
    "B_Mask": "F_Mask",
    "F_Paste": "B_Paste",
    "B_Paste": "F_Paste",
    "F_Courtyard": "B_Courtyard",
    "B_Courtyard": "F_Courtyard",
    "F_Adhes": "B_Adhes",
    "B_Adhes": "F_Adhes",
}


def _sym(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain string."""
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    return str(value)


def find_footprint(data: list[Any], reference: str) -> list[Any]:
    """Return the footprint S-expression node matching *reference*.

    :param data: Parsed PCB S-expression tree (from load_pcb).
    :param reference: Footprint reference designator, e.g. ``"R1"``.
    :returns: The footprint list node.
    :raises KeyError: If no footprint with that reference is found.
    """
    for item in data:
        if not (isinstance(item, list) and len(item) > 0):
            continue
        if _sym(item[0]) != "footprint":
            continue
        ref = _get_fp_reference(item)
        if ref == reference:
            return item
    raise KeyError(f"Footprint '{reference}' not found in PCB")


def _get_fp_reference(fp_node: list[Any]) -> str | None:
    """Extract the Reference property value from a footprint node."""
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
            name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            if name == "Reference":
                return sub[2] if isinstance(sub[2], str) else _sym(sub[2])
    return None


def get_fp_at(fp_node: list[Any]) -> tuple[float, float, float]:
    """Return ``(x, y, rotation)`` from a footprint's ``at`` entry.

    Rotation defaults to 0.0 if absent.
    """
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            x = float(sub[1])
            y = float(sub[2])
            rot = float(sub[3]) if len(sub) > 3 else 0.0
            return x, y, rot
    return 0.0, 0.0, 0.0


def set_fp_at(fp_node: list[Any], x: float, y: float, rotation: float) -> None:
    """Update the ``at`` entry of a footprint node in-place.

    Also propagates any rotation change to child ``pad``, ``property``, and
    ``fp_text`` nodes, which store their orientation as absolute board-space
    degrees (clockwise-positive) in KiCad 10 PCB format.
    """
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            old_rot = float(sub[3]) if len(sub) > 3 else 0.0
            delta = rotation - old_rot
            sub[1] = x
            sub[2] = y
            if len(sub) > 3:
                sub[3] = rotation
            elif rotation != 0.0:
                sub.append(rotation)
            _propagate_rotation_delta(fp_node, delta)
            return
    # No `at` found — create one; treat old rotation as 0
    fp_node.append([sexpdata.Symbol("at"), x, y, rotation])
    _propagate_rotation_delta(fp_node, rotation)


def get_fp_property(fp_node: list[Any], name: str) -> str | None:
    """Return the value of a named ``property`` in a footprint node, or None."""
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
            prop_name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            if prop_name == name:
                return sub[2] if isinstance(sub[2], str) else _sym(sub[2])
    return None


def set_fp_property(fp_node: list[Any], name: str, value: str) -> bool:
    """Update a named ``property`` in a footprint node in-place.

    :returns: True if the property was found and updated; False if not found.
    """
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
            prop_name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            if prop_name == name:
                sub[2] = value
                return True
    return False


def upsert_fp_property(fp_node: list[Any], name: str, value: str) -> None:
    """Update a named property in a footprint node, or create it if absent.

    Unlike ``set_fp_property``, this never fails: if the property does not
    exist it is appended as a minimal ``(property name value)`` node.
    """
    if not set_fp_property(fp_node, name, value):
        fp_node.append([sexpdata.Symbol("property"), name, value])


def get_fp_layer(fp_node: list[Any]) -> str | None:
    """Return the primary layer of a footprint (e.g. ``'F.Cu'``)."""
    for sub in fp_node:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
            return sub[1] if isinstance(sub[1], str) else _sym(sub[1])
    return None


# Child node types whose `at` rotation must track footprint rotation.
# In KiCad 10 PCB format the rotation convention differs by child type:
#
#   pad       — stored rotation is *absolute board-space* (CW+).
#               When footprint rotates by delta, pad rotation += delta.
#
#   property / fp_text — stored rotation is expressed so that the displayed
#               text keeps the same absolute board orientation as the footprint
#               moves.  When footprint rotates by delta, text rotation -= delta
#               (this keeps text_stored + fp_rotation = constant, i.e. the
#               label stays readable at the same angle in board space).
_PAD_TYPES = {"pad"}
_TEXT_TYPES = {"property", "fp_text"}


def _propagate_rotation_delta(fp_node: list[Any], delta: float) -> None:
    """Add *delta* degrees to the ``at`` rotation of all oriented children.

    Pads receive ``+delta`` (absolute board orientation tracks footprint).
    Text children (property, fp_text) receive ``-delta`` (they compensate so
    the displayed text keeps the same absolute orientation).
    The result is normalised to [0, 360).
    """
    if delta == 0.0:
        return
    for child in fp_node:
        if not (isinstance(child, list) and len(child) >= 1):
            continue
        child_type = _sym(child[0])
        if child_type in _PAD_TYPES:
            sign = 1.0
        elif child_type in _TEXT_TYPES:
            sign = -1.0
        else:
            continue
        for sub in child:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
                old_rot = float(sub[3]) if len(sub) > 3 else 0.0
                new_rot = (old_rot + sign * delta) % 360
                if len(sub) > 3:
                    sub[3] = new_rot
                elif new_rot != 0.0:
                    sub.append(new_rot)
                break


def flip_fp_layers(fp_node: list[Any]) -> None:
    """Flip all layer references in a footprint node from F.* to B.* and vice-versa."""
    _flip_layers_recursive(fp_node)


def _flip_layers_recursive(node: Any) -> None:
    """Recursively walk node and flip every layer string using LAYER_FLIP_MAP."""
    if not isinstance(node, list):
        return
    for i, sub in enumerate(node):
        if isinstance(sub, list):
            if len(sub) >= 2 and _sym(sub[0]) == "layer":
                # (layer "F.Cu") — singular layer reference
                current = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                flipped = LAYER_FLIP_MAP.get(current)
                if flipped:
                    sub[1] = flipped
            elif len(sub) >= 2 and _sym(sub[0]) == "layers":
                # (layers "F.Cu" "F.Paste" "F.Mask") — SMD pad multi-layer list
                for j in range(1, len(sub)):
                    layer_str = sub[j] if isinstance(sub[j], str) else _sym(sub[j])
                    flipped = LAYER_FLIP_MAP.get(layer_str)
                    if flipped:
                        sub[j] = flipped
            else:
                _flip_layers_recursive(sub)
