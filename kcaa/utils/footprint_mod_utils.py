"""
Helpers for creating and editing standalone KiCad footprint files (.kicad_mod).

These functions operate on the S-expression tree of a single footprint file, not
on footprint instances inside a PCB.  Every mutation creates a ``.kicad_mod.bak``
backup before writing.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

import sexpdata

log = logging.getLogger(__name__)

_KICAD_MOD_VERSION = 20240108


def _sym(name: str) -> sexpdata.Symbol:
    return sexpdata.Symbol(name)


def load_footprint_mod(path: str) -> list[Any]:
    """Parse a ``.kicad_mod`` file and return its S-expression tree.

    :param path: Absolute path to the footprint file.
    :returns: Parsed S-expression data.
    :raises FileNotFoundError: If the file does not exist.
    :raises ValueError: If the file cannot be parsed.
    """
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    data = sexpdata.loads(raw)
    if not isinstance(data, list) or not data or _str(data[0]) != "footprint":
        raise ValueError(f"File {path} is not a valid KiCad footprint")
    return data


def save_footprint_mod(path: str, data: list[Any]) -> str:
    """Write a ``.kicad_mod`` file, creating a ``.bak`` backup first.

    :param path: Absolute path to write.
    :param data: S-expression tree to serialize.
    :returns: Path to the backup file.
    """
    path = os.path.abspath(path)
    backup_path = f"{path}.bak"
    if os.path.exists(path):
        shutil.copy2(path, backup_path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(sexpdata.dumps(data, pretty_print=True, indent_as="  "))
    return backup_path


def _str(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain value."""
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    return str(value)


def _find_subnode(data: list[Any], key: str) -> list[Any] | None:
    """Return the first direct child list whose first element is *key*."""
    for item in data:
        if isinstance(item, list) and len(item) > 0 and _str(item[0]) == key:
            return item
    return None


def create_footprint_mod(
    name: str,
    layer: str = "F.Cu",
    description: str = "",
    tags: str = "",
    attr: str = "smd",
) -> list[Any]:
    """Return a minimal footprint S-expression tree for *name*.

    :param name: Footprint name.
    :param layer: Primary layer, e.g. ``"F.Cu"`` or ``"B.Cu"``.
    :param description: Footprint description.
    :param tags: Space-separated tags.
    :param attr: Footprint attribute: ``"smd"``, ``"through_hole"``, or ``""``.
    :returns: New footprint S-expression tree.
    """
    data: list[Any] = [
        _sym("footprint"),
        name,
        [_sym("version"), _KICAD_MOD_VERSION],
        [_sym("generator"), "kcaa"],
        [_sym("layer"), layer],
    ]
    if description:
        data.append([_sym("descr"), description])
    if tags:
        data.append([_sym("tags"), tags])
    if attr:
        data.append([_sym("attr"), _sym(attr)])
    return data


def set_footprint_mod_attr(
    data: list[Any],
    key: str,
    value: str | None,
) -> None:
    """Set a top-level footprint attribute.

    Supported keys: ``"layer"``, ``"descr"``, ``"tags"``.
    Pass ``None`` or ``""`` to remove the attribute.

    :param data: Footprint S-expression tree.
    :param key: Attribute name.
    :param value: New value, or ``None``/``""`` to remove.
    """
    for i, item in enumerate(data):
        if isinstance(item, list) and len(item) > 0 and _str(item[0]) == key:
            if not value:
                del data[i]
            else:
                item[1] = value
            return
    if value:
        data.append([_sym(key), value])


def set_footprint_mod_attr_flag(
    data: list[Any],
    key: str,
    value: str | None,
) -> None:
    """Set a top-level flag attribute stored as a bare symbol, e.g. ``(attr smd)``.

    Pass ``None`` or ``""`` to remove the flag.

    :param data: Footprint S-expression tree.
    :param key: Attribute name (e.g. ``"attr"``).
    :param value: Symbol value (e.g. ``"smd"`` or ``"through_hole"``).
    """
    for i, item in enumerate(data):
        if isinstance(item, list) and len(item) > 0 and _str(item[0]) == key:
            if not value:
                del data[i]
            else:
                item[1] = _sym(value)
            return
    if value:
        data.append([_sym(key), _sym(value)])


def add_pad(
    data: list[Any],
    number: str,
    pad_type: str,
    shape: str,
    at: tuple[float, float, float] | tuple[float, float],
    size: tuple[float, float],
    layers: list[str],
    drill: float | tuple[float, float] | None = None,
) -> None:
    """Add a pad to the footprint.

    :param data: Footprint S-expression tree.
    :param number: Pad number or name.
    :param pad_type: ``"smd"``, ``"thru_hole"``, ``"np_thru_hole"``, ``"connect"``.
    :param shape: ``"rect"``, ``"circle"``, ``"oval"``, ``"roundrect"``, ``"trapezoid"``.
    :param at: ``(x, y)`` or ``(x, y, rotation)``.
    :param size: ``(width, height)``.
    :param layers: List of layer names.
    :param drill: Drill diameter (float) or slot size ``(width, height)`` for through-hole.
    """
    at_node = [_sym("at"), float(at[0]), float(at[1])]
    if len(at) >= 3:
        at_node.append(float(at[2]))

    pad_node: list[Any] = [
        _sym("pad"),
        number,
        _sym(pad_type),
        _sym(shape),
        at_node,
        [_sym("size"), float(size[0]), float(size[1])],
        [_sym("layers"), *layers],
    ]

    if drill is not None:
        if isinstance(drill, (int, float)):
            pad_node.append([_sym("drill"), float(drill)])
        else:
            pad_node.append([_sym("drill"), _sym("oval"), float(drill[0]), float(drill[1])])

    data.append(pad_node)


def _stroke(width: float) -> list[Any]:
    return [_sym("stroke"), [_sym("width"), float(width)], [_sym("type"), _sym("default")]]


def add_fp_line(
    data: list[Any],
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float = 0.12,
) -> None:
    """Add a line segment to the footprint."""
    data.append(
        [
            _sym("fp_line"),
            [_sym("start"), float(start[0]), float(start[1])],
            [_sym("end"), float(end[0]), float(end[1])],
            _stroke(width),
            [_sym("layer"), layer],
        ]
    )


def add_fp_arc(
    data: list[Any],
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float = 0.12,
) -> None:
    """Add an arc to the footprint."""
    data.append(
        [
            _sym("fp_arc"),
            [_sym("start"), float(start[0]), float(start[1])],
            [_sym("mid"), float(mid[0]), float(mid[1])],
            [_sym("end"), float(end[0]), float(end[1])],
            _stroke(width),
            [_sym("layer"), layer],
        ]
    )


def add_fp_circle(
    data: list[Any],
    center: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float = 0.12,
) -> None:
    """Add a circle to the footprint.

    KiCad stores a circle as ``(fp_circle (center x y) (end x y) ...)`` where
    ``end`` is a point on the circumference.
    """
    data.append(
        [
            _sym("fp_circle"),
            [_sym("center"), float(center[0]), float(center[1])],
            [_sym("end"), float(end[0]), float(end[1])],
            _stroke(width),
            [_sym("layer"), layer],
        ]
    )


def add_fp_rect(
    data: list[Any],
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float = 0.12,
) -> None:
    """Add a rectangle to the footprint."""
    data.append(
        [
            _sym("fp_rect"),
            [_sym("start"), float(start[0]), float(start[1])],
            [_sym("end"), float(end[0]), float(end[1])],
            _stroke(width),
            [_sym("layer"), layer],
        ]
    )


def add_fp_text(
    data: list[Any],
    text_type: str,
    text: str,
    at: tuple[float, float, float] | tuple[float, float],
    layer: str,
    size: tuple[float, float] = (1.0, 1.0),
    thickness: float = 0.15,
) -> None:
    """Add a text item to the footprint.

    :param text_type: ``"reference"``, ``"value"``, or ``"user"``.
    :param text: Text content.
    :param at: ``(x, y)`` or ``(x, y, rotation)``.
    :param layer: Layer name.
    :param size: Font size ``(width, height)``.
    :param thickness: Stroke thickness.
    """
    at_node = [_sym("at"), float(at[0]), float(at[1])]
    if len(at) >= 3:
        at_node.append(float(at[2]))

    data.append(
        [
            _sym("fp_text"),
            _sym(text_type),
            text,
            at_node,
            [_sym("layer"), layer],
            [
                _sym("effects"),
                [_sym("font"), [_sym("size"), float(size[0]), float(size[1])], [_sym("thickness"), float(thickness)]],
            ],
        ]
    )


def delete_element_from_footprint(
    data: list[Any],
    element_type: str,
    index: int,
) -> bool:
    """Delete the *index*-th occurrence of *element_type* from the footprint.

    :param data: Footprint S-expression tree.
    :param element_type: Element type: ``"pad"``, ``"fp_line"``, ``"fp_arc"``,
        ``"fp_circle"``, ``"fp_rect"``, ``"fp_text"``.
    :param index: Zero-based index among elements of that type.
    :returns: True if an element was deleted.
    """
    count = 0
    for i, item in enumerate(data):
        if isinstance(item, list) and len(item) > 0 and _str(item[0]) == element_type:
            if count == index:
                del data[i]
                return True
            count += 1
    return False


def get_footprint_mod_info(data: list[Any]) -> dict[str, Any]:
    """Return a summary of a footprint S-expression tree.

    :param data: Footprint S-expression tree.
    :returns: Dict with name, layer, description, tags, attr, and counts of
        pads, lines, arcs, circles, rectangles, and text items.
    """
    name = data[1] if len(data) > 1 and isinstance(data[1], str) else ""
    info: dict[str, Any] = {
        "name": name,
        "layer": "",
        "description": "",
        "tags": "",
        "attr": "",
        "pad_count": 0,
        "line_count": 0,
        "arc_count": 0,
        "circle_count": 0,
        "rect_count": 0,
        "text_count": 0,
    }
    for item in data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        key = _str(item[0])
        if key == "layer":
            info["layer"] = item[1] if isinstance(item[1], str) else _str(item[1])
        elif key == "descr":
            info["description"] = item[1] if isinstance(item[1], str) else _str(item[1])
        elif key == "tags":
            info["tags"] = item[1] if isinstance(item[1], str) else _str(item[1])
        elif key == "attr":
            info["attr"] = _str(item[1])
        elif key == "pad":
            info["pad_count"] += 1
        elif key == "fp_line":
            info["line_count"] += 1
        elif key == "fp_arc":
            info["arc_count"] += 1
        elif key == "fp_circle":
            info["circle_count"] += 1
        elif key == "fp_rect":
            info["rect_count"] += 1
        elif key == "fp_text":
            info["text_count"] += 1
    return info
