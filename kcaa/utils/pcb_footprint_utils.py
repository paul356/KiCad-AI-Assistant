"""
Helpers for working with footprint S-expression nodes inside a PCB tree.

Handles: locating footprints, reading/writing the `at` position, and
reading/writing named `property` entries.  The layer-flip map used by
flip_footprint also lives here.

Also includes the PCB → library export helpers: iterating board-embedded
footprint nodes, normalizing them into library-ready `.kicad_mod` nodes,
and serializing/writing them without overwriting existing files.
"""

from collections.abc import Iterator
import copy
import logging
import os
import re
from typing import Any

import sexpdata

log = logging.getLogger(__name__)

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
    degrees (CCW-positive, same convention as the footprint) in KiCad 10
    PCB format.
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
#   pad       — stored rotation is *absolute board-space* (CCW+, same
#               convention as the footprint's own file rotation).
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


# ---------------------------------------------------------------------------
# PCB → library export
# ---------------------------------------------------------------------------

# Node keys that are placement/identity data — never legal in a library file.
_INSTANCE_ONLY_KEYS = {"at", "uuid", "tstamp", "tedit", "path"}
# Properties that carry per-board-instance values.
_INSTANCE_PROPERTIES = {"Reference", "Value"}


def iter_footprint_nodes(data: list[Any]) -> Iterator[list[Any]]:
    """Yield each top-level ``(footprint ...)`` node from a parsed PCB tree."""
    for item in data:
        if isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint":
            yield item


def get_pcb_version(data: list[Any]) -> int:
    """Return the board-format version from the ``(version N)`` header."""
    if len(data) > 1 and isinstance(data[1], list) and len(data[1]) > 1:
        if _sym(data[1][0]) == "version":
            try:
                return int(data[1][1])
            except (TypeError, ValueError):
                pass
    return 0


def split_footprint_header(node: list[Any]) -> tuple[str | None, str]:
    """Return ``(library_nickname, footprint_name)`` from a footprint node.

    The header string is ``"Library:Name"`` when the footprint came from a
    library, or just ``"Name"`` for footprints created on the board.
    """
    header = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
    if ":" in header:
        lib, _, name = header.rpartition(":")
        return (lib or None), name
    return None, header


_FOOTPRINT_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


def is_safe_footprint_name(name: str) -> bool:
    """True when *name* is safe to use as a ``<name>.kicad_mod`` filename.

    Rejects empty names plus anything containing path separators, ``..``, or
    characters outside ``[A-Za-z0-9_.+-]`` — the same set KiCad accepts in
    footprint names.  Boards from collaborators can be crafted; never build a
    target path from an unvalidated header string.
    """
    if not name:
        return False
    if name in ("..", "."):
        return False
    return _FOOTPRINT_NAME_RE.match(name) is not None


def _child_at_rotation(child: list[Any]) -> float | None:
    """Return the explicit rotation of a child's ``at`` node, or None."""
    for sub in child:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            return float(sub[3]) if len(sub) > 3 else None
    return None


def _set_child_at_rotation(child: list[Any], rotation: float) -> None:
    """Set (or append) the rotation component of a child's ``at`` node."""
    rotation %= 360.0
    for sub in child:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            if len(sub) > 3:
                sub[3] = rotation
            elif rotation != 0.0:
                sub.append(rotation)
            return


def _readjust_rotation(child: list[Any], fp_rotation: float) -> None:
    """Re-express one child's rotation in the footprint's own frame.

    Pads carry absolute board-space rotation (local + fp_rotation); text nodes
    carry the angle that keeps their label readable in board space (so their
    stored value tracks ``-fp_rotation``).  Only children with an explicit
    rotation component need adjustment; implied rotations rotate with the
    footprint and are already relative.
    """
    if fp_rotation == 0.0:
        return
    stored = _child_at_rotation(child)
    if stored is None:
        return
    child_type = _sym(child[0]) if len(child) > 0 else ""
    if child_type in _PAD_TYPES:
        _set_child_at_rotation(child, stored - fp_rotation)
    elif child_type in _TEXT_TYPES:
        _set_child_at_rotation(child, stored + fp_rotation)


def normalize_footprint_for_library(
    fp_node: list[Any],
    pcb_version: int,
    new_library: str,
) -> list[Any]:
    """Return a library-ready copy of a board footprint node.

    :param fp_node: Parsed ``(footprint ...)`` node from the board.
    :param pcb_version: Board file format version (becomes the library
        footprint's ``version``).
    :param new_library: Nickname of the target library; used for the
        ``Footprint`` property.
    :returns: A deep copy suitable for writing as ``.kicad_mod``.
    """
    node = copy.deepcopy(fp_node)

    _, name = split_footprint_header(node)
    # Header: bare footprint name (no "Library:" prefix).
    node[1] = name

    # Capture placement rotation, then drop instance-only nodes.
    fp_rotation = 0.0
    child_at_nodes: list[list[Any]] = []
    for i in list(range(len(node) - 1, 1, -1)):
        child = node[i]
        if not (isinstance(child, list) and len(child) > 0):
            continue
        key = _sym(child[0])
        if key == "at" and len(child) >= 3:
            try:
                fp_rotation = float(child[3]) if len(child) > 3 else 0.0
            except (TypeError, ValueError):
                fp_rotation = 0.0
            del node[i]
        elif key in _INSTANCE_ONLY_KEYS:
            del node[i]
        elif key == "property":
            prop_name = child[1] if len(child) > 1 and isinstance(child[1], str) else ""
            if prop_name in _INSTANCE_PROPERTIES:
                del node[i]
            elif prop_name == "Footprint":
                # Re-point at the new library (KiCad "Save Copy As" behavior).
                child[2] = f"{new_library}:{name}"
        elif key in ("pad", "fp_text"):
            child_at_nodes.append(child)

    # Re-express child rotations in the footprint's own frame.
    for child in child_at_nodes:
        _readjust_rotation(child, fp_rotation)
        if _sym(child[0]) == "pad":
            # Strip net connections (per-instance routing data).
            for j in list(range(len(child) - 1, -1, -1)):
                sub = child[j]
                if isinstance(sub, list) and len(sub) > 0 and _sym(sub[0]) == "net":
                    del child[j]
        elif _sym(child[0]) == "fp_text":
            text_type = _sym(child[1]) if len(child) > 1 else ""
            if text_type == "reference" and len(child) > 2:
                child[2] = "REF**"
            elif text_type == "value" and len(child) > 2:
                child[2] = name

    # Insert the format version right after the header name.
    node.insert(2, [sexpdata.Symbol("version"), pcb_version])
    return node


def serialize_footprint_mod(node: list[Any]) -> str:
    """Serialize a footprint node as ``.kicad_mod`` text.

    KiCad layout: ``(footprint "Name"`` and the closing ``)`` on their own
    lines, each child element on one tab-indented line — matching the format
    KiCad itself writes for ``.kicad_mod`` files.
    """
    name = node[1] if len(node) > 1 else ""
    lines = [f"(footprint {sexpdata.dumps(name)}"]
    for child in node[2:]:
        lines.append("\t" + sexpdata.dumps(child))
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_footprint_mod(library_dir: str, name: str, node: list[Any]) -> str:
    """Write *node* to ``<library_dir>/<name>.kicad_mod`` (no overwrite).

    :returns: Path written.
    :raises FileExistsError: When the file already exists (never overwrite
        an existing footprint, per design decision).
    :raises ValueError: When *name* is unsafe for a filename (path traversal
        guard — see :func:`is_safe_footprint_name`).
    """
    if not is_safe_footprint_name(name):
        raise ValueError(f"Unsafe footprint name: {name!r}")
    os.makedirs(library_dir, exist_ok=True)
    path = os.path.join(library_dir, f"{name}.kicad_mod")
    if os.path.exists(path):
        raise FileExistsError(f"Footprint already exists: {path}")
    text = serialize_footprint_mod(node)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp_path, path)
    log.info("Exported footprint %s -> %s", name, path)
    return path
