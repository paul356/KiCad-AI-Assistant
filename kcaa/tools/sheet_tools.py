"""Hierarchical sheet tools for KiCad schematic manipulation.

Tools for creating, reading, updating, and deleting hierarchical sheet symbols
in ``.kicad_sch`` files.  All coordinates are mm, +Y down (KiCad screen
convention), snapped to 1.27 mm (50-mil) grid.

File-mutation tools create a ``.kicad_sch.bak`` backup before saving.
"""

from __future__ import annotations

import logging
import os
from typing import Any
import uuid

from fastmcp import Context, FastMCP

from kcaa.utils.skip_compat import safe_schematic

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAPER_SIZES = frozenset(
    {
        "A4",
        "A3",
        "A2",
        "A5",
        "A",
        "B",
        "C",
        "D",
        "E",
        "USLetter",
        "USLegal",
        "USLedger",
    }
)

GRID_MM = 1.27


def _align_to_grid(value: float) -> float:
    """Snap a coordinate to the nearest 1.27 mm grid point."""
    return round(value / GRID_MM) * GRID_MM


# ---------------------------------------------------------------------------
# S-expression helpers for manual sheet construction
# ---------------------------------------------------------------------------


def _sexp_line_uuid(tag: str, u: str, indent: int = 2) -> str:
    return f'{" " * indent}({tag} "{u}")'


def _sexp_property(name: str, value: str, indent: int = 2) -> str:
    return (
        f'{" " * indent}(property "{name}" "{value}"'
        f" (at 0 0 0)"
        f" (show_name no)"
        f" (do_not_autoplace yes)"
        f" (effects (font (size 1.27 1.27)) (justify left)))"
    )


# ---------------------------------------------------------------------------
# Child schematic file generation
# ---------------------------------------------------------------------------


def _generate_child_schematic(
    parent_path: str,
    child_filename: str,
    paper: str = "A4",
    title: str | None = None,
) -> str:
    """Create a minimal valid ``.kicad_sch`` file for a hierarchical child sheet.

    The file is created in the same directory as *parent_path* (unless
    *child_filename* is absolute).  It contains the required boilerplate
    that KiCad expects: ``(kicad_sch ...)`` header, empty ``(lib_symbols)``,
    ``(sheet_instances)`` with the root path, and ``(embedded_fonts no)``.

    :param parent_path: Absolute path to the parent ``.kicad_sch`` file, used
        to resolve a relative *child_filename* and to infer the project name.
    :param child_filename: Name of the new ``.kicad_sch`` file.  The
        ``.kicad_sch`` extension is appended if missing.  Relative paths are
        resolved against the directory of *parent_path*.
    :param paper: Paper size name.  Must be one of: A4, A3, A2, A5, A, B,
        C, D, E, USLetter, USLegal, USLedger.  Defaults to ``"A4"``.
    :param title: Optional title.  When provided, a ``(title_block (title
        ...))`` token is included for KiCad's title block.
    :returns: Absolute path to the created ``.kicad_sch`` file.
    :raises ValueError: If *paper* is not a recognised paper size.
    :raises OSError: If the file cannot be written.
    :raises FileExistsError: If the target file already exists.

    .. note::

        This function does **not** modify the parent schematic — it only
        creates the child file.  Use ``add_sheet_symbol`` with
        ``create_child=True`` to create the file and add the sheet symbol
        in one step.

    Example generated output::

        (kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")
          (uuid "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee")
          (paper "A4")
          (title_block
            (title "My Sheet")
          )
          (lib_symbols)
          (sheet_instances
            (path "/aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee" (page "1"))
          )
          (embedded_fonts no)
        )
    """
    if paper not in _PAPER_SIZES:
        raise ValueError(f"Unknown paper size {paper!r}.  Valid: {', '.join(sorted(_PAPER_SIZES))}")

    # Resolve path
    if not child_filename.endswith(".kicad_sch"):
        child_filename += ".kicad_sch"

    if os.path.isabs(child_filename):
        child_path = child_filename
    else:
        parent_dir = os.path.dirname(os.path.abspath(parent_path))
        child_path = os.path.join(parent_dir, child_filename)

    if os.path.exists(child_path):
        raise FileExistsError(f"Child schematic already exists: {child_path!r}")

    root_uuid = str(uuid.uuid4())

    # Build S-expression lines
    lines: list[str] = []
    lines.append('(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")')
    lines.append(f'  (uuid "{root_uuid}")')
    lines.append(f'  (paper "{paper}")')

    if title:
        lines.append("  (title_block")
        lines.append(f'    (title "{title}")')
        lines.append("  )")

    lines.append("  (lib_symbols)")
    lines.append("  (sheet_instances")
    lines.append(f'    (path "/{root_uuid}" (page "1"))')
    lines.append("  )")
    lines.append("  (embedded_fonts no)")
    lines.append(")")

    with open(child_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")

    return child_path


# ---------------------------------------------------------------------------
# Sheet reading helpers
# ---------------------------------------------------------------------------


def _normalize_collection(value) -> list:
    """Return *value* as a regular list for skip wrappers and collections."""
    if value is None:
        return []
    elements = getattr(value, "_elements", None)
    if elements is not None:
        return list(elements)
    if isinstance(value, list):
        return value
    if hasattr(value, "entity_type"):
        return [value]
    try:
        length = len(value)
    except TypeError:
        return [value]
    return [value[i] for i in range(length)]


def _sheet_dict_from_wrapper(sheet) -> dict[str, Any]:
    """Extract fields from a ``skip.SheetWrapper`` into a plain dict."""
    info: dict[str, Any] = {}

    # UUID — use .value to get the clean string
    try:
        info["uuid"] = sheet.uuid.value
    except (AttributeError, KeyError):
        info["uuid"] = None

    # Properties: Sheet name & Sheet file
    sheet_name = None
    sheet_file = None
    try:
        props = sheet.property
    except AttributeError:
        props = None
    if props is not None:
        for attr_name, target_key in (
            ("Sheet_name", "sheet_name"),
            ("Sheetname", "sheet_name"),
            ("Sheet_file", "sheet_file"),
            ("Sheetfile", "sheet_file"),
        ):
            with_value = getattr(props, attr_name, None)
            if with_value is None:
                continue
            try:
                if target_key == "sheet_name" and sheet_name is None:
                    sheet_name = with_value.value
                elif target_key == "sheet_file" and sheet_file is None:
                    sheet_file = with_value.value
            except (AttributeError, KeyError):
                continue

        for prop in _normalize_collection(props):
            raw_tree = getattr(getattr(prop, "_pv", None), "_tree", None)
            if not isinstance(raw_tree, list) or len(raw_tree) < 3:
                continue
            prop_name = raw_tree[1]
            if not isinstance(prop_name, str):
                continue
            normalized_name = prop_name.replace(" ", "").replace("_", "").lower()
            if normalized_name == "sheetname" and sheet_name is None:
                sheet_name = raw_tree[2]
            elif normalized_name == "sheetfile" and sheet_file is None:
                sheet_file = raw_tree[2]
    info["sheet_name"] = sheet_name
    info["sheet_file"] = sheet_file

    # Position (at)
    try:
        atvals = list(sheet.at)
        info["position"] = {"x": float(atvals[0]), "y": float(atvals[1])}
    except (AttributeError, IndexError, ValueError, TypeError):
        info["position"] = None

    # Size
    try:
        sizevals = list(sheet.size)
        info["size"] = {"width": float(sizevals[0]), "height": float(sizevals[1])}
    except (AttributeError, IndexError, ValueError, TypeError):
        info["size"] = None

    # Pins — normalise single/multi, extract name from value[0]
    pins: list[dict[str, Any]] = []
    try:
        raw_pins = sheet.pin
    except AttributeError:
        raw_pins = None
    for p in _normalize_collection(raw_pins):
        pin_info: dict[str, Any] = {}
        try:
            # p.value returns ['PIN_NAME', Symbol('direction')]
            pin_info["name"] = p.value[0] if isinstance(p.value, list) else str(p.value)
        except (AttributeError, KeyError, IndexError):
            pin_info["name"] = None
        try:
            pin_at = list(p.at)
            pin_info["at"] = [float(v) for v in pin_at[:3]]
        except (AttributeError, IndexError, ValueError, TypeError):
            pin_info["at"] = None
        try:
            pin_info["uuid"] = p.uuid.value
        except (AttributeError, KeyError):
            pin_info["uuid"] = None
        pins.append(pin_info)
    info["pins"] = pins

    return info


def _list_sheet_symbols_impl(schematic_path: str) -> dict[str, Any]:
    """Implementation of list_sheet_symbols."""
    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    sch = safe_schematic(schematic_path)

    raw_sheets = None
    try:
        raw_sheets = sch.sheet
    except AttributeError:
        pass

    sheets: list[dict[str, Any]] = []
    for s in _normalize_collection(raw_sheets):
        sheets.append(_sheet_dict_from_wrapper(s))

    return {
        "schematic_path": schematic_path,
        "sheet_count": len(sheets),
        "sheets": sheets,
    }


def _get_sheet_hierarchy_impl(
    schematic_path: str,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Implementation of get_sheet_hierarchy — recursive tree walk."""
    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    _visited: set[str] = set()

    def _resolve_child_path(parent: str, child_file: str) -> str | None:
        """Resolve a child .kicad_sch relative to its parent directory."""
        if os.path.isabs(child_file):
            return child_file
        return os.path.join(os.path.dirname(parent), child_file)

    def _walk(path: str, depth: int, sheet_name: str | None) -> dict[str, Any] | None:
        real = os.path.realpath(path)
        if real in _visited:
            return {"file": path, "cycle_detected": True}
        if depth > max_depth:
            return {"file": path, "max_depth_reached": True}

        _visited.add(real)

        node: dict[str, Any] = {"file": path}
        if sheet_name:
            node["sheet_name"] = sheet_name

        if not os.path.isfile(path):
            node["error"] = f"File not found: {path!r}"
            return node

        try:
            sch = safe_schematic(path)
        except Exception as exc:
            node["error"] = f"Failed to parse: {exc}"
            return node

        children: list[dict[str, Any]] = []
        raw_sheets = None
        try:
            raw_sheets = sch.sheet
        except AttributeError:
            pass

        for s in _normalize_collection(raw_sheets):
            info = _sheet_dict_from_wrapper(s)
            child_file = info.get("sheet_file")
            if child_file:
                child_path = _resolve_child_path(path, child_file)
                child_node = _walk(
                    child_path,
                    depth + 1,
                    sheet_name=info.get("sheet_name"),
                )
                if child_node:
                    children.append(child_node)

        node["children"] = children
        node["sheet_count"] = len(children)
        return node

    root = _walk(schematic_path, 0, None)
    # Remove internal tracking
    _visited.clear()

    return {
        "root_schematic": schematic_path,
        "hierarchy": root,
    }


# ---------------------------------------------------------------------------
# Sheet CRUD – core implementations (read tools)
# ---------------------------------------------------------------------------


def _do_add_sheet_symbol(
    schematic_path: str,
    sheet_name: str,
    sheet_file: str,
    x: float,
    y: float,
    width: float,
    height: float,
    pins: list[dict[str, Any]] | None,
    create_child: bool,
    child_paper: str,
    child_title: str | None,
) -> dict[str, Any]:
    """Implementation of add_sheet_symbol (delegated from the MCP tool)."""
    import sexpdata

    from kcaa.utils.schematic_sexp_utils import save_schematic

    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    # Optionally create child file first
    child_path = None
    if create_child:
        try:
            child_path = _generate_child_schematic(
                schematic_path, sheet_file, paper=child_paper, title=child_title
            )
        except FileExistsError:
            # Child already exists, that's fine
            parent_dir = os.path.dirname(os.path.abspath(schematic_path))
            child_path = os.path.join(parent_dir, sheet_file)
        except Exception as e:
            return {"error": f"Failed to create child schematic: {e}"}

    # Snap coordinates to grid
    x = _align_to_grid(x)
    y = _align_to_grid(y)
    width = _align_to_grid(width)
    height = _align_to_grid(height)

    # Generate UUID for the sheet symbol
    sheet_uuid = str(uuid.uuid4())

    # Build the (sheet ...) S-expression as a nested list
    sheet_entry: list = [
        sexpdata.Symbol("sheet"),
        [sexpdata.Symbol("at"), x, y],
        [sexpdata.Symbol("size"), width, height],
        [
            sexpdata.Symbol("stroke"),
            [sexpdata.Symbol("width"), 0],
            [sexpdata.Symbol("type"), sexpdata.Symbol("dash")],
        ],
        [sexpdata.Symbol("fill"), [sexpdata.Symbol("type"), sexpdata.Symbol("none")]],
        [sexpdata.Symbol("uuid"), sheet_uuid],
        [
            sexpdata.Symbol("property"),
            "Sheet name",
            sheet_name,
            [sexpdata.Symbol("at"), 0, 0, 0],
            [sexpdata.Symbol("show_name"), sexpdata.Symbol("no")],
            [sexpdata.Symbol("do_not_autoplace"), sexpdata.Symbol("yes")],
            [
                sexpdata.Symbol("effects"),
                [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
                [sexpdata.Symbol("justify"), sexpdata.Symbol("left")],
            ],
        ],
        [
            sexpdata.Symbol("property"),
            "Sheet file",
            sheet_file,
            [sexpdata.Symbol("at"), 0, 0, 0],
            [sexpdata.Symbol("show_name"), sexpdata.Symbol("no")],
            [sexpdata.Symbol("do_not_autoplace"), sexpdata.Symbol("yes")],
            [
                sexpdata.Symbol("effects"),
                [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
                [sexpdata.Symbol("justify"), sexpdata.Symbol("left")],
            ],
        ],
    ]

    # Add pins if provided
    pins_created = 0
    if pins:
        edge_to_rotation = {"right": 0, "left": 180, "bottom": 270, "top": 90}
        # justify follows the angle: angle 0/90 (right/top) → justify right;
        # angle 180/270 (left/bottom) → justify left.
        edge_to_justify = {"right": "right", "top": "right", "left": "left", "bottom": "left"}

        for pin_def in pins:
            pin_name = pin_def.get("name")
            edge = pin_def.get("edge", "right")
            distance_mm = float(pin_def.get("distance_mm", 0.0))
            if not pin_name:
                continue

            rot = edge_to_rotation.get(edge, 0)
            justify = edge_to_justify.get(edge, "left")
            d = _align_to_grid(distance_mm)

            # Absolute pin connection point on the sheet boundary.
            if edge == "right":
                pin_x, pin_y = x + width, y + d
            elif edge == "left":
                pin_x, pin_y = x, y + d
            elif edge == "top":
                pin_x, pin_y = x + d, y
            else:  # bottom
                pin_x, pin_y = x + d, y + height

            pin_uuid = str(uuid.uuid4())
            pin_entry = [
                sexpdata.Symbol("pin"),
                pin_name,
                sexpdata.Symbol("input"),
                [sexpdata.Symbol("at"), pin_x, pin_y, rot],
                [sexpdata.Symbol("uuid"), pin_uuid],
                [
                    sexpdata.Symbol("effects"),
                    [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
                    [sexpdata.Symbol("justify"), sexpdata.Symbol(justify)],
                ],
            ]
            sheet_entry.append(pin_entry)
            pins_created += 1

    # Add instances block
    # Derive project name from schematic filename
    project_name = os.path.splitext(os.path.basename(schematic_path))[0]
    instances_entry = [
        sexpdata.Symbol("instances"),
        [
            sexpdata.Symbol("project"),
            project_name,
            [
                sexpdata.Symbol("path"),
                f"/{sheet_uuid}",
                [sexpdata.Symbol("page"), "1"],
            ],
        ],
    ]
    sheet_entry.append(instances_entry)

    # Load schematic and insert the new sheet
    sch = safe_schematic(schematic_path)
    sch.new_from_list(sheet_entry)

    # Save the modified schematic
    backup_path = save_schematic(schematic_path, sch)

    return {
        "success": True,
        "sheet_uuid": sheet_uuid,
        "sheet_name": sheet_name,
        "sheet_file": sheet_file,
        "position": {"x": x, "y": y},
        "size": {"width": width, "height": height},
        "pins_created": pins_created,
        "child_path": child_path,
        "file_modified": schematic_path,
        "backup_path": backup_path,
    }


def _do_remove_sheet_symbol(
    schematic_path: str,
    sheet_identifier: str,
    delete_child: bool = False,
) -> dict[str, Any]:
    """Implementation of remove_sheet_symbol."""
    import sexpdata

    from kcaa.utils.schematic_sexp_utils import save_schematic

    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    sch = safe_schematic(schematic_path)

    # Find the sheet to remove
    raw_sheets = None
    try:
        raw_sheets = sch.sheet
    except AttributeError:
        return {"error": "No sheet symbols found in schematic"}

    sheets_list = _normalize_collection(raw_sheets)
    target_sheet = None
    target_index = None

    for i, sheet in enumerate(sheets_list):
        info = _sheet_dict_from_wrapper(sheet)
        # Match by UUID or name
        if info.get("uuid") == sheet_identifier or info.get("sheet_name") == sheet_identifier:
            target_sheet = info
            target_index = i
            break

    if target_sheet is None:
        return {"error": f"Sheet symbol not found: {sheet_identifier!r}"}

    # Remove from the tree
    tree = sch.tree
    sheet_entries = []
    for i, entry in enumerate(tree):
        if isinstance(entry, list) and len(entry) > 0:
            tag = entry[0]
            if isinstance(tag, sexpdata.Symbol) and tag.value() == "sheet":
                sheet_entries.append((i, entry))

    if target_index >= len(sheet_entries):
        return {"error": "Internal error: sheet index mismatch"}

    tree_index, removed_entry = sheet_entries[target_index]
    tree.pop(tree_index)

    # Save the modified schematic
    backup_path = save_schematic(schematic_path, sch)

    result: dict[str, Any] = {
        "success": True,
        "removed_uuid": target_sheet.get("uuid"),
        "removed_name": target_sheet.get("sheet_name"),
        "removed_file": target_sheet.get("sheet_file"),
        "file_modified": schematic_path,
        "backup_path": backup_path,
    }

    # Optionally delete the child .kicad_sch file
    if delete_child:
        child_file = target_sheet.get("sheet_file")
        if child_file:
            parent_dir = os.path.dirname(os.path.abspath(schematic_path))
            child_path = (
                child_file if os.path.isabs(child_file) else os.path.join(parent_dir, child_file)
            )
            if os.path.isfile(child_path):
                try:
                    os.remove(child_path)
                    result["deleted_child_path"] = child_path
                except OSError as exc:
                    result["child_delete_error"] = str(exc)
            else:
                result["child_delete_skipped"] = f"File not found: {child_path}"

    return result


def _do_update_sheet_symbol(
    schematic_path: str,
    sheet_identifier: str,
    sheet_name: str | None,
    sheet_file: str | None,
    x: float | None,
    y: float | None,
    width: float | None,
    height: float | None,
) -> dict[str, Any]:
    """Implementation of update_sheet_symbol."""
    import sexpdata

    from kcaa.utils.schematic_sexp_utils import save_schematic

    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    sch = safe_schematic(schematic_path)

    # Find the sheet to update
    raw_sheets = None
    try:
        raw_sheets = sch.sheet
    except AttributeError:
        return {"error": "No sheet symbols found in schematic"}

    sheets_list = _normalize_collection(raw_sheets)
    target_sheet = None
    target_index = None

    for i, sheet in enumerate(sheets_list):
        info = _sheet_dict_from_wrapper(sheet)
        # Match by UUID or name
        if info.get("uuid") == sheet_identifier or info.get("sheet_name") == sheet_identifier:
            target_sheet = info
            target_index = i
            break

    if target_sheet is None:
        return {"error": f"Sheet symbol not found: {sheet_identifier!r}"}

    # Find the sheet entry in the tree
    tree = sch.tree
    sheet_entries = []
    for i, entry in enumerate(tree):
        if isinstance(entry, list) and len(entry) > 0:
            tag = entry[0]
            if isinstance(tag, sexpdata.Symbol) and tag.value() == "sheet":
                sheet_entries.append((i, entry))

    if target_index >= len(sheet_entries):
        return {"error": "Internal error: sheet index mismatch"}

    tree_index, sheet_entry = sheet_entries[target_index]

    # Track what we updated
    updated_fields = []
    final_position = dict(target_sheet.get("position") or {})
    final_size = dict(target_sheet.get("size") or {})
    final_sheet_name = target_sheet.get("sheet_name")
    final_sheet_file = target_sheet.get("sheet_file")

    # Update position if provided
    if x is not None or y is not None:
        new_x = _align_to_grid(x) if x is not None else target_sheet.get("position", {}).get("x", 0)
        new_y = _align_to_grid(y) if y is not None else target_sheet.get("position", {}).get("y", 0)

        # Find and update the (at ...) entry
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "at":
                    child[1] = new_x
                    child[2] = new_y
                    final_position = {"x": new_x, "y": new_y}
                    updated_fields.append("position")
                    break

    # Update size if provided
    if width is not None or height is not None:
        new_width = (
            _align_to_grid(width)
            if width is not None
            else target_sheet.get("size", {}).get("width", 0)
        )
        new_height = (
            _align_to_grid(height)
            if height is not None
            else target_sheet.get("size", {}).get("height", 0)
        )

        # Find and update the (size ...) entry
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "size":
                    child[1] = new_width
                    child[2] = new_height
                    final_size = {"width": new_width, "height": new_height}
                    updated_fields.append("size")
                    break

    # Update sheet name if provided
    if sheet_name is not None:
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "property":
                    if len(child) > 1 and child[1] == "Sheet name":
                        child[2] = sheet_name
                        final_sheet_name = sheet_name
                        updated_fields.append("sheet_name")
                        break

    # Update sheet file if provided
    if sheet_file is not None:
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "property":
                    if len(child) > 1 and child[1] == "Sheet file":
                        child[2] = sheet_file
                        final_sheet_file = sheet_file
                        updated_fields.append("sheet_file")
                        break

    if not updated_fields:
        return {"error": "No fields to update"}

    # Save the modified schematic
    backup_path = save_schematic(schematic_path, sch)

    return {
        "success": True,
        "sheet_uuid": target_sheet.get("uuid"),
        "sheet_name": final_sheet_name,
        "sheet_file": final_sheet_file,
        "position": final_position,
        "size": final_size,
        "updated_fields": updated_fields,
        "file_modified": schematic_path,
        "backup_path": backup_path,
    }


def _do_add_sheet_pin(
    schematic_path: str,
    sheet_identifier: str,
    pin_name: str,
    edge: str,
    distance_mm: float,
) -> dict[str, Any]:
    """Implementation of add_sheet_pin."""
    import sexpdata

    from kcaa.utils.schematic_sexp_utils import save_schematic

    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    sch = safe_schematic(schematic_path)

    # Find the sheet to add the pin to
    raw_sheets = None
    try:
        raw_sheets = sch.sheet
    except AttributeError:
        return {"error": "No sheet symbols found in schematic"}

    sheets_list = _normalize_collection(raw_sheets)
    target_sheet = None
    target_index = None

    for i, sheet in enumerate(sheets_list):
        info = _sheet_dict_from_wrapper(sheet)
        # Match by UUID or name
        if info.get("uuid") == sheet_identifier or info.get("sheet_name") == sheet_identifier:
            target_sheet = info
            target_index = i
            break

    if target_sheet is None:
        return {"error": f"Sheet symbol not found: {sheet_identifier!r}"}

    # Find the sheet entry in the tree
    tree = sch.tree
    sheet_entries = []
    for i, entry in enumerate(tree):
        if isinstance(entry, list) and len(entry) > 0:
            tag = entry[0]
            if isinstance(tag, sexpdata.Symbol) and tag.value() == "sheet":
                sheet_entries.append((i, entry))

    if target_index >= len(sheet_entries):
        return {"error": "Internal error: sheet index mismatch"}

    tree_index, sheet_entry = sheet_entries[target_index]

    # Get sheet position and size
    sheet_x = target_sheet.get("position", {}).get("x", 0)
    sheet_y = target_sheet.get("position", {}).get("y", 0)
    sheet_width = target_sheet.get("size", {}).get("width", 0)
    sheet_height = target_sheet.get("size", {}).get("height", 0)

    # Calculate pin position based on edge
    if edge == "right":
        pin_x = sheet_x + sheet_width
        pin_y = sheet_y + distance_mm
        pin_angle = 0
    elif edge == "left":
        pin_x = sheet_x
        pin_y = sheet_y + distance_mm
        pin_angle = 180
    elif edge == "top":
        pin_x = sheet_x + distance_mm
        pin_y = sheet_y
        pin_angle = 90
    elif edge == "bottom":
        pin_x = sheet_x + distance_mm
        pin_y = sheet_y + sheet_height
        pin_angle = 270
    else:
        return {"error": f"Invalid edge: {edge!r}. Must be right/left/top/bottom"}

    # Generate UUID for the pin
    pin_uuid = str(uuid.uuid4())

    # Build the pin S-expression
    pin_entry = [
        sexpdata.Symbol("pin"),
        pin_name,
        sexpdata.Symbol("input"),  # default shape
        [sexpdata.Symbol("at"), pin_x, pin_y, pin_angle],
        [sexpdata.Symbol("uuid"), pin_uuid],
        [
            sexpdata.Symbol("effects"),
            [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
            [sexpdata.Symbol("justify"), sexpdata.Symbol("left")],
        ],
    ]

    # Find the instances block and insert the pin before it
    instances_index = None
    for i, child in enumerate(sheet_entry):
        if isinstance(child, list) and len(child) > 0:
            if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "instances":
                instances_index = i
                break

    if instances_index is None:
        # No instances block, append to end
        sheet_entry.append(pin_entry)
    else:
        # Insert before instances block
        sheet_entry.insert(instances_index, pin_entry)

    # Save the modified schematic
    backup_path = save_schematic(schematic_path, sch)

    return {
        "success": True,
        "pin_uuid": pin_uuid,
        "sheet_uuid": target_sheet.get("uuid"),
        "pin_name": pin_name,
        "edge": edge,
        "position": {"x": pin_x, "y": pin_y},
        "file_modified": schematic_path,
        "backup_path": backup_path,
    }


def _do_remove_sheet_pin(
    schematic_path: str,
    sheet_identifier: str,
    pin_name: str,
) -> dict[str, Any]:
    """Implementation of remove_sheet_pin."""
    import sexpdata

    from kcaa.utils.schematic_sexp_utils import save_schematic

    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    sch = safe_schematic(schematic_path)

    # Find the sheet to remove the pin from
    raw_sheets = None
    try:
        raw_sheets = sch.sheet
    except AttributeError:
        return {"error": "No sheet symbols found in schematic"}

    sheets_list = _normalize_collection(raw_sheets)
    target_sheet = None
    target_index = None

    for i, sheet in enumerate(sheets_list):
        info = _sheet_dict_from_wrapper(sheet)
        # Match by UUID or name
        if info.get("uuid") == sheet_identifier or info.get("sheet_name") == sheet_identifier:
            target_sheet = info
            target_index = i
            break

    if target_sheet is None:
        return {"error": f"Sheet symbol not found: {sheet_identifier!r}"}

    # Find the sheet entry in the tree
    tree = sch.tree
    sheet_entries = []
    for i, entry in enumerate(tree):
        if isinstance(entry, list) and len(entry) > 0:
            tag = entry[0]
            if isinstance(tag, sexpdata.Symbol) and tag.value() == "sheet":
                sheet_entries.append((i, entry))

    if target_index >= len(sheet_entries):
        return {"error": "Internal error: sheet index mismatch"}

    tree_index, sheet_entry = sheet_entries[target_index]

    # Find the pin to remove
    pin_index = None
    for i, child in enumerate(sheet_entry):
        if isinstance(child, list) and len(child) > 0:
            if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "pin":
                # Pin name is at index 1
                if len(child) > 1 and child[1] == pin_name:
                    pin_index = i
                    break

    if pin_index is None:
        return {"error": f"Pin not found: {pin_name!r}"}

    # Remove the pin
    sheet_entry.pop(pin_index)

    # Save the modified schematic
    backup_path = save_schematic(schematic_path, sch)

    return {
        "success": True,
        "removed_pin_name": pin_name,
        "sheet_uuid": target_sheet.get("uuid"),
        "file_modified": schematic_path,
        "backup_path": backup_path,
    }


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_sheet_tools(mcp: FastMCP) -> None:
    """Register all hierarchical sheet tools with the MCP server."""

    # ---- Read tools ----

    @mcp.tool()
    async def list_sheet_symbols(
        schematic_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all sheet symbols on a schematic.

        Reads sheet symbols from the ``.kicad_sch`` file at *schematic_path*
        and returns a flat list with each sheet's UUID, name, file reference,
        position, size, and pins.

        **Read-only** — does not modify the schematic.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.

        Returns:
            dict with keys:
                - ``schematic_path`` (str): the file that was read
                - ``sheet_count`` (int): number of sheet symbols found
                - ``sheets`` (list[dict]): each sheet dict contains
                  ``uuid``, ``sheet_name``, ``sheet_file``,
                  ``position`` (``{"x": ..., "y": ...}`` in mm),
                  ``size`` (``{"width": ..., "height": ...}`` in mm),
                  and ``pins`` (list of ``{name, at, uuid}`` dicts)
        """
        return _list_sheet_symbols_impl(schematic_path)

    @mcp.tool()
    async def get_sheet_hierarchy(
        schematic_path: str,
        max_depth: int = 10,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Recursively walk the sheet hierarchy starting from a root schematic.

        Opens the schematic at *schematic_path*, reads its sheet symbols,
        then follows each sheet's ``sheet_file`` reference to recurse into
        child schematics.  The result is a tree structure where each node
        lists its children and their file paths.

        Cycle detection prevents infinite loops (detected by real path).

        **Read-only** — does not modify any files.

        Args:
            schematic_path: Absolute path to the root ``.kicad_sch`` file.
            max_depth: Maximum recursion depth (default 10).

        Returns:
            dict with keys:
                - ``root_schematic`` (str): the root file that was read
                - ``hierarchy`` (dict): tree node with ``file``,
                  ``children`` (list of child nodes), and ``sheet_count``
                  (number of direct children).  Each child node also
                  carries ``sheet_name`` and ``file``.
        """
        return _get_sheet_hierarchy_impl(schematic_path, max_depth)

    # ---- Create / Update / Delete tools ----

    @mcp.tool()
    async def add_sheet_symbol(
        schematic_path: str,
        sheet_name: str,
        sheet_file: str,
        x: float,
        y: float,
        width: float = 50.8,
        height: float = 50.8,
        pins: list[dict[str, Any]] | None = None,
        create_child: bool = False,
        child_paper: str = "A4",
        child_title: str | None = None,
        auto_place: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical sheet symbol to a schematic.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_name: Display name for the sheet symbol.
            sheet_file: Filename of the child schematic (e.g.
                ``"sub-sheet.kicad_sch"``).  Relative paths are resolved
                against the parent's directory.
            x: X position in mm (snapped to 1.27 mm grid).
            y: Y position in mm (snapped to 1.27 mm grid).
            width: Sheet symbol width in mm (default 50.8 = 2 inches).
            height: Sheet symbol height in mm (default 50.8 = 2 inches).
            pins: Optional list of pin dicts, each with ``name`` (str),
                ``edge`` (str: right/left/bottom/top), ``distance_mm`` (float).
            create_child: If True, create the child ``.kicad_sch`` file on
                disk before adding the sheet symbol.
            child_paper: Paper size for the child file (default ``"A4"``).
            child_title: Optional title for the child file's title block.
            auto_place: When True (default), move the sheet to the nearest free
                area if the requested position would overlap an existing symbol
                or sheet.

        Returns:
            dict with keys: success, sheet_uuid, position, size, pins_created,
            child_path (if create_child was True), and ``position_adjusted``
            when auto-placement changes the requested location.
        """
        requested_position = {"x": x, "y": y}
        snapped_x = _align_to_grid(x)
        snapped_y = _align_to_grid(y)
        place_x = x
        place_y = y
        position_adjusted = False

        if auto_place:
            from kcaa.tools.placement_helpers import PlacementHelpers

            free_area = PlacementHelpers.find_free_area(
                schematic_path=schematic_path,
                width=_align_to_grid(width),
                height=_align_to_grid(height),
                prefer_near={"x": snapped_x, "y": snapped_y},
                max_candidates=1,
            )
            candidate = (free_area.get("candidates") or [{}])[0]
            origin = candidate.get("origin")
            if origin is not None:
                candidate_x = float(origin["x"])
                candidate_y = float(origin["y"])
                if candidate_x != snapped_x or candidate_y != snapped_y:
                    place_x = candidate_x
                    place_y = candidate_y
                    position_adjusted = True

        result = _do_add_sheet_symbol(
            schematic_path=schematic_path,
            sheet_name=sheet_name,
            sheet_file=sheet_file,
            x=place_x,
            y=place_y,
            width=width,
            height=height,
            pins=pins,
            create_child=create_child,
            child_paper=child_paper,
            child_title=child_title,
        )
        if result.get("success"):
            result["position_adjusted"] = position_adjusted
            if position_adjusted:
                result["requested_position"] = requested_position
                result["note"] = "Position adjusted to nearest free area."
        return result

    @mcp.tool()
    async def remove_sheet_symbol(
        schematic_path: str,
        sheet_identifier: str,
        delete_child: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove a sheet symbol from a schematic.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the sheet symbol to remove.
            delete_child: If True, also delete the referenced child
                ``.kicad_sch`` file from disk. Default is False (only removes
                the symbol from the parent schematic).

        Returns:
            dict with keys: success, removed_uuid, removed_name, removed_file,
            file_modified, backup_path. When delete_child is True, also
            deleted_child_path (on success) or child_delete_error (on failure).
        """
        return _do_remove_sheet_symbol(
            schematic_path=schematic_path,
            sheet_identifier=sheet_identifier,
            delete_child=delete_child,
        )

    @mcp.tool()
    async def update_sheet_symbol(
        schematic_path: str,
        sheet_identifier: str,
        sheet_name: str | None = None,
        sheet_file: str | None = None,
        x: float | None = None,
        y: float | None = None,
        width: float | None = None,
        height: float | None = None,
        auto_place: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a sheet symbol's properties.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the sheet symbol to update.
            sheet_name: New display name (optional).
            sheet_file: New child file reference (optional).
            x: New X position in mm (optional).
            y: New Y position in mm (optional).
            width: New width in mm (optional).
            height: New height in mm (optional).
            auto_place: When True (default), if a position change is requested,
                move the sheet to the nearest free area when the requested
                position would overlap an existing symbol or sheet. The moved
                sheet itself is excluded from the obstacle list.

        Returns:
            dict with keys: success, uuid, updated_fields, and optionally
            ``position_adjusted`` / ``requested_position`` / ``note`` when
            auto-placement changes the requested location.
        """
        place_x = x
        place_y = y
        position_adjusted = False
        requested_position: dict[str, Any] | None = None

        if auto_place and (x is not None or y is not None):
            from kcaa.tools.placement_helpers import PlacementHelpers

            # Look up current sheet to fill in missing axis and get UUID/size.
            sheet_info = _list_sheet_symbols_impl(schematic_path)
            target_info: dict[str, Any] | None = None
            for s in sheet_info.get("sheets", []):
                if s.get("uuid") == sheet_identifier or s.get("sheet_name") == sheet_identifier:
                    target_info = s
                    break

            if target_info is not None:
                cur_pos = target_info.get("position") or {}
                cur_size = target_info.get("size") or {}
                cur_x = float(cur_pos.get("x", 0))
                cur_y = float(cur_pos.get("y", 0))
                cur_w = float(cur_size.get("width", 50.8))
                cur_h = float(cur_size.get("height", 50.8))
                sheet_uuid = target_info.get("uuid")

                req_x = _align_to_grid(x if x is not None else cur_x)
                req_y = _align_to_grid(y if y is not None else cur_y)
                eff_w = _align_to_grid(width if width is not None else cur_w)
                eff_h = _align_to_grid(height if height is not None else cur_h)

                free_area = PlacementHelpers.find_free_area(
                    schematic_path=schematic_path,
                    width=eff_w,
                    height=eff_h,
                    prefer_near={"x": req_x, "y": req_y},
                    max_candidates=1,
                    exclude_uuid=sheet_uuid,
                )
                candidate = (free_area.get("candidates") or [{}])[0]
                origin = candidate.get("origin")
                if origin is not None:
                    cand_x = float(origin["x"])
                    cand_y = float(origin["y"])
                    if cand_x != req_x or cand_y != req_y:
                        requested_position = {"x": x, "y": y}
                        place_x = cand_x
                        place_y = cand_y
                        position_adjusted = True

        result = _do_update_sheet_symbol(
            schematic_path=schematic_path,
            sheet_identifier=sheet_identifier,
            sheet_name=sheet_name,
            sheet_file=sheet_file,
            x=place_x,
            y=place_y,
            width=width,
            height=height,
        )
        if result.get("success") and (x is not None or y is not None):
            result["position_adjusted"] = position_adjusted
            if position_adjusted and requested_position is not None:
                result["requested_position"] = requested_position
                result["note"] = "Position adjusted to nearest free area."
        return result

    @mcp.tool()
    async def add_sheet_pin(
        schematic_path: str,
        sheet_identifier: str,
        pin_name: str,
        edge: str,
        distance_mm: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical pin to a sheet symbol.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the target sheet symbol.
            pin_name: Name for the new pin (e.g. "VCC", "GND").
            edge: One of ``"right"``, ``"left"``, ``"bottom"``, ``"top"``.
            distance_mm: Distance along the edge from the origin corner.

        Returns:
            dict with keys: success, pin_uuid, pin_name, edge, distance_mm.
        """
        return _do_add_sheet_pin(schematic_path, sheet_identifier, pin_name, edge, distance_mm)

    @mcp.tool()
    async def remove_sheet_pin(
        schematic_path: str,
        sheet_identifier: str,
        pin_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove a hierarchical pin from a sheet symbol.

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the target sheet symbol.
            pin_name: Name of the pin to remove.

        Returns:
            dict with keys: success, removed_pin_name.
        """
        return _do_remove_sheet_pin(schematic_path, sheet_identifier, pin_name)
