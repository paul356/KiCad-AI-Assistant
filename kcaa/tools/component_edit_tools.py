"""Schematic editing tools for KiCad MCP server.

Provides tools to add symbols to KiCad schematics by combining
the symbol index DB, the streaming extractor, and the skip library.
"""

import contextlib
import copy
import logging
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.tools.sheet_tools import _normalize_collection, _sheet_dict_from_wrapper
from kcaa.utils.config import LibraryPathConfig
from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic
from kcaa.utils.symbol_extractor import extract_lib_symbol_raw
from kcaa.utils.symbol_geometry import (
    BBox,
    compute_unit_bboxes,
    lib_bbox_to_world,
    union_bboxes,
)
from kcaa.utils.symbol_index_manager import SymbolIndexManager
from kcaa.utils.symbol_index_reader import SymbolIndexReader

log = logging.getLogger(__name__)

VALID_LABEL_TYPES = ("local", "global", "hierarchical")
VALID_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")


def _angle_to_direction(angle_deg: int | float) -> str:
    """Convert a screen-space label/pin angle to a human-readable direction string.

    KiCad schematic uses Y-down screen coordinates:
      0   → "right"  (+X)
      90  → "down"   (+Y screen)
      180 → "left"   (-X)
      270 → "up"     (-Y screen)
    """
    a = int(round(float(angle_deg))) % 360
    return {0: "right", 90: "down", 180: "left", 270: "up"}.get(a, f"{a}deg")


def _iter_schematic_labels(sch: Any, attr_name: str) -> list[Any]:
    """Safely iterate label-like elements from a schematic attribute."""
    try:
        coll = getattr(sch, attr_name)
    except AttributeError:
        return []

    elements = getattr(coll, "_elements", None)
    if elements is not None:
        return list(elements)

    return [coll]


# ---------------------------------------------------------------------------
# Symbol index manager singleton
# ---------------------------------------------------------------------------

_index_manager: SymbolIndexManager | None = None


def _get_index_manager() -> SymbolIndexManager:
    global _index_manager
    if _index_manager is None:
        config = LibraryPathConfig()
        library_manager = SymbolIndexReader(config)
        _index_manager = SymbolIndexManager(library_manager)
    return _index_manager


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _align_to_grid(value: float, grid_size: float = 1.27) -> float:
    """Align a coordinate to the nearest grid point.

    Args:
        value: The coordinate value in mm.
        grid_size: The grid size in mm (default 1.27 mm = 50 mils, the
            standard KiCad schematic grid on which all symbol pins are placed).

    Returns:
        The coordinate rounded to the nearest grid point.
    """
    return round(value / grid_size) * grid_size


# ---------------------------------------------------------------------------
# Pin-on-wire conflict detection for safe symbol placement
# ---------------------------------------------------------------------------

_PLACE_GRID: float = 2.54  # mm — standard KiCad schematic grid
_PLACE_TOL: float = 0.5  # mm — tolerance for pin-on-wire detection


def _extract_lib_pin_positions(lib_sym_raw: list) -> list[tuple[float, float]]:
    """Return the (x, y) connection-point of every pin in library coordinates (Y-up).

    Reads every ``(pin ...)`` node from all sub-symbol unit nodes in
    *lib_sym_raw* and extracts the ``(at x y ...)`` coordinate.
    """
    positions: list[tuple[float, float]] = []
    for entry in lib_sym_raw[2:]:
        if not (
            isinstance(entry, list)
            and len(entry) >= 2
            and isinstance(entry[0], sexpdata.Symbol)
            and entry[0].value() == "symbol"
        ):
            continue
        for child in entry[2:]:
            if not (
                isinstance(child, list)
                and len(child) >= 1
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "pin"
            ):
                continue
            for sub in child[1:]:
                if (
                    isinstance(sub, list)
                    and len(sub) >= 3
                    and isinstance(sub[0], sexpdata.Symbol)
                    and sub[0].value() == "at"
                ):
                    with contextlib.suppress(TypeError, ValueError):
                        positions.append((float(sub[1]), float(sub[2])))
                    break
    return positions


def _lib_pins_world(
    lib_sym_raw: list,
    sym_x: float,
    sym_y: float,
    rotation: int,
) -> list[tuple[float, float]]:
    """Return world-space (x, y) for every pin of *lib_sym_raw* placed at
    (sym_x, sym_y) with the given rotation.

    Uses the same transform as the skip library fallback: each 90° CW step
    rotates the lib position as ``(x, y) → (y, −x)``, then
    ``world_x = sym_x + rx``, ``world_y = sym_y − ry`` (lib Y axis is flipped).
    """
    steps = (rotation // 90) % 4
    world: list[tuple[float, float]] = []
    for lx, ly in _extract_lib_pin_positions(lib_sym_raw):
        rx, ry = lx, ly
        for _ in range(steps):
            rx, ry = ry, -rx
        world.append((round(sym_x + rx, 4), round(sym_y - ry, 4)))
    return world


def _pin_on_wire(
    px: float,
    py: float,
    wires: list[tuple[float, float, float, float]],
    tol: float,
) -> bool:
    """Return True if (px, py) coincides with the interior or an endpoint of
    any wire segment.

    Both wire endpoints and axis-aligned interiors are treated as conflicts so
    that newly placed symbol pins are fully isolated from all existing wiring.
    """
    for ax, ay, bx, by in wires:
        if abs(px - ax) <= tol and abs(py - ay) <= tol:
            return True
        if abs(px - bx) <= tol and abs(py - by) <= tol:
            return True
        # Interior of an axis-aligned segment
        if abs(ay - by) < 1e-9:  # horizontal
            if abs(py - ay) <= tol:
                lo = min(ax, bx) + tol
                hi = max(ax, bx) - tol
                if lo <= px <= hi:
                    return True
        elif abs(ax - bx) < 1e-9:  # vertical
            if abs(px - ax) <= tol:
                lo = min(ay, by) + tol
                hi = max(ay, by) - tol
                if lo <= py <= hi:
                    return True
    return False


def _find_safe_placement(
    lib_sym_raw: list,
    x: float,
    y: float,
    rotation: int,
    wires: list[tuple[float, float, float, float]],
) -> tuple[float, float]:
    """Return the nearest 2.54 mm grid position to (x, y) where no pin of the
    placed symbol lands on any existing wire (interior or endpoint).

    Expands outward in Chebyshev shells of 1–5 grid steps (up to 12.7 mm).
    Returns the original coordinates unchanged if no conflict exists or if no
    conflict-free position is found within the search radius.
    """

    def _conflicts(cx: float, cy: float) -> bool:
        return any(
            _pin_on_wire(px, py, wires, _PLACE_TOL)
            for px, py in _lib_pins_world(lib_sym_raw, cx, cy, rotation)
        )

    if not _conflicts(x, y):
        return x, y

    for shell in range(1, 6):
        candidates: list[tuple[float, float]] = [
            (x + dx * _PLACE_GRID, y + dy * _PLACE_GRID)
            for dx in range(-shell, shell + 1)
            for dy in range(-shell, shell + 1)
            if max(abs(dx), abs(dy)) == shell
        ]
        candidates.sort(key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
        for cx, cy in candidates:
            if not _conflicts(cx, cy):
                return cx, cy

    return x, y


def _do_add_symbol(
    schematic_path: str,
    library_name: str,
    symbol_name: str,
    x: float,
    y: float,
    rotation: int,
    value: str | None,
    fields_autoplaced: bool,
) -> dict[str, Any]:
    """Core implementation of ``add_symbol_to_schematic``.

    Validates inputs, opens the schematic, injects the lib symbol if needed,
    inserts one placed instance per electrical unit, writes the file (with a
    .bak backup) and returns a result dict including the world-space
    ``body_bbox``.
    """
    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}
    if not math.isfinite(x) or not math.isfinite(y):
        return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}
    if rotation not in (0, 90, 180, 270):
        return {"error": f"rotation must be 0, 90, 180, or 270 (got {rotation})"}

    x = _align_to_grid(x)
    y = _align_to_grid(y)
    try:
        table_name = library_name.split("/")[0]
        lib_id_str = f"{table_name}:{symbol_name}"
        effective_value = value or symbol_name

        mgr = _get_index_manager()
        lib_rec = mgr.get_library_by_name(library_name)
        if lib_rec is None:
            return {
                "error": (
                    f"Library '{library_name}' not found in index. "
                    "Verify the library name is correct."
                )
            }

        sym_rec = mgr.get_symbol(library_name, symbol_name)
        if sym_rec is None:
            return {
                "error": (
                    f"Symbol '{symbol_name}' not found in library "
                    f"'{library_name}'. Verify the symbol name."
                )
            }

        try:
            lib_sym_raw = extract_lib_symbol_raw(
                lib_rec.file_path,
                sym_rec.file_index,
                symbol_name,
                lib_rec.mtime,
                lib_rec.file_size,
            )
        except Exception as exc:
            return {"error": f"Failed to extract lib symbol: {exc}"}

        # Expand (extends ...) symbols into self-contained definitions so that
        # the injected lib_symbol has full geometry and pin sub-symbols.
        lib_sym_raw = _resolve_extends_symbol(lib_sym_raw, library_name)

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        sch_uuid_obj = getattr(sch, "uuid", None)
        if sch_uuid_obj is not None:
            sch_uuid = str(sch_uuid_obj.value).lstrip("/")
        else:
            sch_uuid = str(uuid.uuid4())

        try:
            if lib_id_str not in sch.lib_symbols:
                _add_lib_symbol(sch.lib_symbols, lib_sym_raw, table_name)
        except Exception as exc:
            return {"error": f"Failed to inject lib symbol: {exc}"}

        unit_count = _get_unit_count(lib_sym_raw)
        prefix = "U"
        for child in lib_sym_raw[2:]:
            if (
                isinstance(child, list)
                and len(child) >= 3
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "property"
                and child[1] == "Reference"
            ):
                prefix = child[2] if isinstance(child[2], str) else "U"
                break
        reference = _next_reference(sch, prefix, schematic_path=schematic_path)

        project_name = _find_project_name(schematic_path)

        # Collect existing wires and adjust placement if any pin would land
        # on a wire interior or endpoint.
        existing_wires: list[tuple[float, float, float, float]] = []
        try:
            for w in sch.wire:
                existing_wires.append(
                    (
                        float(w.start.value[0]),
                        float(w.start.value[1]),
                        float(w.end.value[0]),
                        float(w.end.value[1]),
                    )
                )
        except AttributeError:
            pass
        original_x, original_y = x, y
        x, y = _find_safe_placement(lib_sym_raw, x, y, rotation, existing_wires)

        placements: list[tuple[int, float, float, int, str | None]] = []
        for unit in range(1, unit_count + 1):
            unit_y = y + (unit - 1) * 10.0
            placed_raw = _build_placed_symbol(
                lib_id_str,
                x,
                unit_y,
                rotation,
                unit,
                reference,
                effective_value,
                sch_uuid,
                project_name,
                lib_sym_raw,
                fields_autoplaced=fields_autoplaced,
            )
            sch.new_from_list(placed_raw)
            placements.append((unit, x, unit_y, rotation, None))

        try:
            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to save schematic: {exc}"}

        body_bbox = _placed_world_bbox(lib_sym_raw, placements)

        result: dict[str, Any] = {
            "success": True,
            "reference_assigned": reference,
            "lib_id": lib_id_str,
            "units_added": unit_count,
            "position": {"x": x, "y": y},
            "position_adjusted": x != original_x or y != original_y,
            "body_bbox": body_bbox,
            "warnings": [],
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }
        return result
    except Exception as exc:
        log.exception("Unexpected error in _do_add_symbol")
        return {"error": str(exc), "success": False}


def _placed_world_bbox(
    lib_sym_raw: list,
    placements: list[tuple[int, float, float, int, str | None]],
) -> dict | None:
    """Compute the world-space body bbox covering one or more placed units.

    Each placement is ``(unit, x, y, rotation_deg, mirror)`` where mirror is
    ``"x"``, ``"y"`` or ``None``. Returns a ``{min_x, min_y, max_x, max_y,
    width, height}`` dict in mm with +Y down, or ``None`` if the lib symbol
    has no drawable geometry.
    """
    try:
        unit_bbs = compute_unit_bboxes(lib_sym_raw)
    except Exception:
        return None
    if not unit_bbs:
        return None
    world_bbs: list[BBox] = []
    for unit, sx, sy, rot, mirror in placements:
        lib_bb = unit_bbs.get(unit) or unit_bbs.get(1)
        if lib_bb is None:
            continue
        world_bbs.append(lib_bbox_to_world(lib_bb, sx, sy, int(rot), mirror))
    merged = union_bboxes(world_bbs)
    return merged.to_dict() if merged is not None else None


def _find_project_name(schematic_path: str) -> str:
    """
    Find the KiCad project name by locating the .kicad_pro file near the
    schematic.  Checks the schematic's directory first, then the parent.
    Returns the file stem, or "project" if nothing is found.
    """
    sch_dir = Path(schematic_path).parent
    for search_dir in (sch_dir, sch_dir.parent):
        matches = list(search_dir.glob("*.kicad_pro"))
        if matches:
            return matches[0].stem
    return "project"


def _find_project_dir(schematic_path: str) -> Path | None:
    """Find the KiCad project directory containing a .kicad_pro file.

    Searches the schematic's directory first, then the parent directory.
    Returns the directory Path, or None if no .kicad_pro is found.
    """
    sch_dir = Path(schematic_path).parent
    for search_dir in (sch_dir, sch_dir.parent):
        if list(search_dir.glob("*.kicad_pro")):
            return search_dir
    return None


def _find_root_schematic(schematic_path: str) -> str | None:
    """Return the root .kicad_sch path for the project containing *schematic_path*.

    Finds the .kicad_pro file via ``_find_project_dir``, then looks for a
    .kicad_sch with the same stem in the project directory.  Returns None if
    no project or matching root schematic is found.
    """
    project_dir = _find_project_dir(schematic_path)
    if project_dir is None:
        return None
    pro_files = list(project_dir.glob("*.kicad_pro"))
    if not pro_files:
        return None
    root_sch = project_dir / (pro_files[0].stem + ".kicad_sch")
    return str(root_sch) if root_sch.is_file() else None


def _read_instance_reference(sym: Any) -> str | None:
    """Extract the reference from ``sym.instances``, which is KiCad's authoritative
    reference for placed symbols.  Returns ``None`` when the instances block is
    missing or unparseable.
    """
    try:
        for item in sym.instances:
            return item.path.reference.value
    except (AttributeError, TypeError, StopIteration):
        return None
    return None


def _update_instance_reference(sym: Any, new_ref: str) -> None:
    """Update ``sym.instances.path.reference`` if it exists.  This keeps the
    instances block in sync after a rename so KiCad displays the correct value.
    """
    try:
        for item in sym.instances:
            item.path.reference.value = new_ref
    except (AttributeError, TypeError):
        pass


def _collect_hierarchy_references(schematic_path: str) -> dict[str, dict[str, str]]:
    """Collect symbol references with anchor UUIDs by following the sheet hierarchy.

    Uses the ``skip`` library to parse each schematic.  ``sch.symbol``
    provides only placed symbol instances — ``lib_symbols`` template
    definitions are automatically excluded.

    For multi-unit symbols (e.g. ``U1A`` / ``U1B``) only the UUID of unit 1
    (the anchor unit) is recorded.

    Returns:
        ``{absolute_file_path: {reference_designator: anchor_uuid}}``.
    """
    root = os.path.realpath(schematic_path)
    refs_by_file: dict[str, dict[str, str]] = {}
    visited: set[str] = set()
    queue: list[str] = [root]

    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)

        if not os.path.isfile(current):
            continue

        try:
            sch = safe_schematic(current)
        except Exception:
            continue

        # Collect refs from placed symbols — sch.symbol excludes lib_symbols.
        ref_to_uuid: dict[str, str] = {}
        for sym in sch.symbol:
            try:
                # Prefer instances.path.reference (KiCad's authoritative reference)
                # over property.Reference which can be stale after manual edits.
                ref = _read_instance_reference(sym)
                if ref is None:
                    ref = sym.property.Reference.value
                sym_uuid = sym.uuid.value
            except AttributeError:
                continue
            unit = getattr(getattr(sym, "unit", None), "value", 1)
            # For multi-unit symbols, keep only the anchor unit's UUID.
            if ref not in ref_to_uuid or unit == 1:
                ref_to_uuid[ref] = sym_uuid
        refs_by_file[current] = ref_to_uuid

        # Follow sheet hierarchy via skip's sheet wrapper.
        parent_dir = os.path.dirname(current)
        raw_sheets = getattr(sch, "sheet", None)
        for s in _normalize_collection(raw_sheets):
            info = _sheet_dict_from_wrapper(s)
            sheet_file = info.get("sheet_file")
            if sheet_file:
                if not os.path.isabs(sheet_file):
                    sheet_file = os.path.join(parent_dir, sheet_file)
                child_real = os.path.realpath(sheet_file)
                if child_real not in visited:
                    queue.append(child_real)

    return refs_by_file


def _next_reference(sch: Any, prefix: str, schematic_path: str | None = None) -> str:
    """Auto-assign the next available reference designator for a given prefix.

    Scans ``sch.symbol`` for references that start with *prefix* followed by
    digits, finds the maximum integer suffix, and returns ``prefix + (max+1)``.
    When *schematic_path* is provided, also scans all ``*.kicad_sch`` files
    under the project directory to avoid conflicts across sub-sheets.

    Returns ``prefix + "1"`` if no existing references match.
    """
    suffix_re = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    max_n = 0

    # Scan the current schematic's symbols first.
    try:
        for sym in sch.symbol:
            try:
                ref_val = _read_instance_reference(sym) or sym.property.Reference.value
                m = suffix_re.match(ref_val)
                if m:
                    max_n = max(max_n, int(m.group(1)))
            except AttributeError:
                continue
    except AttributeError:
        pass

    # Also scan all schematics reachable from the project root hierarchy.
    if schematic_path is not None:
        root_sch = _find_root_schematic(schematic_path)
        if root_sch is not None:
            hierarchy = _collect_hierarchy_references(root_sch)
        else:
            hierarchy = {}
        for file_path, ref_set in hierarchy.items():
            # Skip the current file — we already scanned its refs above.
            if os.path.normpath(file_path) == os.path.normpath(schematic_path):
                continue
            for ref_str in ref_set.keys():
                m = suffix_re.match(ref_str)
                if m:
                    max_n = max(max_n, int(m.group(1)))

    return f"{prefix}{max_n + 1}"


def _get_unit_count(lib_sym_raw: list) -> int:
    """
    Count the number of electrical units in a lib symbol raw S-expression.

    Sub-symbol names follow the pattern "SYMNAME_N_M" where N is the unit
    number (1-based) and M is the body style.  Unit 0 is decorative/shared
    and is excluded from the count.

    Returns at least 1.
    """
    sym_name = lib_sym_raw[1]  # e.g. "R" or "TL072"
    prefix = sym_name + "_"
    units: set[int] = set()

    for child in lib_sym_raw[2:]:
        if not (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "symbol"
        ):
            continue
        sub_name = child[1]
        if not sub_name.startswith(prefix):
            continue
        rest = sub_name[len(prefix) :]  # e.g. "1_1" or "0_1"
        parts = rest.split("_")
        if len(parts) >= 2:
            try:
                unit_n = int(parts[0])
                if unit_n >= 1:
                    units.add(unit_n)
            except ValueError:
                pass

    return max(len(units), 1)


def _collect_lib_properties(lib_sym_raw: list) -> list[list]:
    """
    Return the direct (property ...) children of a lib symbol raw list.
    Each entry is the raw sexpdata list for that property.
    """
    props = []
    for child in lib_sym_raw[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "property"
        ):
            props.append(child)
    return props


def _collect_unit_pin_numbers(lib_sym_raw: list, unit: int) -> list[str]:
    """
    Collect pin number strings for the given unit from a lib symbol raw list.

    Pins live under sub-symbols named "SYMNAME_UNIT_STYLE" or "SYMNAME_0_STYLE"
    (shared decorative unit).  Only pins from the requested unit are returned.
    """
    sym_name = lib_sym_raw[1]
    prefix = sym_name + "_"
    pin_numbers: list[str] = []

    for child in lib_sym_raw[2:]:
        if not (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "symbol"
        ):
            continue
        sub_name = child[1]
        if not sub_name.startswith(prefix):
            continue
        rest = sub_name[len(prefix) :]
        parts = rest.split("_")
        if len(parts) < 2:
            continue
        try:
            sub_unit = int(parts[0])
        except ValueError:
            continue
        # Include pins from the requested unit AND the shared unit (0).
        if sub_unit != unit and sub_unit != 0:
            continue

        # Crawl sub-symbol children for (pin ...) entries.
        for pin_entry in child[2:]:
            if not (
                isinstance(pin_entry, list)
                and len(pin_entry) >= 1
                and isinstance(pin_entry[0], sexpdata.Symbol)
                and pin_entry[0].value() == "pin"
            ):
                continue
            # Find (number "N" ...) child.
            for pin_child in pin_entry[1:]:
                if (
                    isinstance(pin_child, list)
                    and len(pin_child) >= 2
                    and isinstance(pin_child[0], sexpdata.Symbol)
                    and pin_child[0].value() == "number"
                ):
                    pin_numbers.append(str(pin_child[1]))
                    break

    return pin_numbers


def _get_property_at(prop_raw: list) -> tuple[float, float, int]:
    """
    Extract the (at x y rot) from a property raw list.
    Returns (0.0, 0.0, 0) if not found.
    """
    for child in prop_raw[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 3
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "at"
        ):
            try:
                px = float(child[1])
                py = float(child[2])
                rot = int(child[3]) if len(child) >= 4 else 0
                return px, py, rot
            except (ValueError, TypeError):
                pass
    return 0.0, 0.0, 0


# Base field offsets at rotation=0, derived from KiCad's native auto-placement
# output (R1 example: ref at +2.54,-1.27 and value at +2.54,+1.27 from center).
_BASE_REF_OFFSET: tuple[float, float] = (2.54, -1.27)
_BASE_VAL_OFFSET: tuple[float, float] = (2.54, 1.27)

# Properties that are conventionally visible on the schematic canvas.
# Footprint, Datasheet, Description, and all custom properties (MPN,
# Manufacturer, LCSC, etc.) are hidden by KiCad convention on placed
# symbols and should have (hide yes) injected when added as new properties.
_STANDARD_VISIBLE_PROPERTIES: frozenset[str] = frozenset({"Reference", "Value"})


def _rotate_offset(dx: float, dy: float, angle_deg: int) -> tuple[float, float]:
    """Rotate a 2-D offset by angle_deg degrees (CCW positive, KiCad convention)."""
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    return round(dx * c - dy * s, 4), round(dx * s + dy * c, 4)


def _build_property(
    name: str,
    value: str,
    abs_x: float,
    abs_y: float,
    rot: int,
    hide: bool = False,
    justify: str | None = None,
    do_not_autoplace: bool = False,
) -> list:
    """Build a (property "name" "value" (at x y rot) (effects ...)) raw list.

    When *do_not_autoplace* is True, ``(do_not_autoplace yes)`` is appended
    after the effects node so that KiCad's field auto-placer leaves this
    property at its specified coordinates.
    """
    effects: list = [
        sexpdata.Symbol("effects"),
        [
            sexpdata.Symbol("font"),
            [sexpdata.Symbol("size"), 1.27, 1.27],
        ],
    ]
    if justify:
        effects.append([sexpdata.Symbol("justify"), sexpdata.Symbol(justify)])
    if hide:
        effects.append([sexpdata.Symbol("hide"), sexpdata.Symbol("yes")])
    node = [
        sexpdata.Symbol("property"),
        name,
        value,
        [
            sexpdata.Symbol("at"),
            abs_x,
            abs_y,
            rot,
        ],
        effects,
    ]
    if do_not_autoplace:
        node.append([sexpdata.Symbol("do_not_autoplace"), sexpdata.Symbol("yes")])
    return node


def _build_placed_symbol(
    lib_id_str: str,
    x: float,
    y: float,
    rotation: int,
    unit: int,
    reference: str,
    value: str,
    sch_uuid: str,
    project_name: str,
    lib_sym_raw: list,
    fields_autoplaced: bool = True,
) -> list:
    """
    Build the raw sexpdata list for one placed (symbol ...) entry.

    Parameters
    ----------
    lib_id_str   : qualified lib_id e.g. "Device:R"
    x, y         : placement position in mm
    rotation     : rotation in degrees (0/90/180/270)
    unit         : unit number (1-based)
    reference    : reference designator string e.g. "R5"
    value        : value string e.g. "10k"
    sch_uuid     : schematic top-level UUID (without leading "/")
    project_name : project name from .kicad_pro stem
    lib_sym_raw  : raw lib symbol list from extract_lib_symbol_raw()
    """
    sym_uuid = str(uuid.uuid4())

    # Collect library properties and build the Reference / Value entries first,
    # then the remaining properties.
    lib_props = _collect_lib_properties(lib_sym_raw)

    # Compute Reference / Value positions using rotation-based base offsets.
    # These match KiCad's native auto-placement style: fields appear beside the
    # symbol body rather than overlapping it, regardless of placement rotation.
    ref_dx, ref_dy = _rotate_offset(*_BASE_REF_OFFSET, rotation)
    val_dx, val_dy = _rotate_offset(*_BASE_VAL_OFFSET, rotation)

    # Collect extra properties (Footprint, Datasheet, Description …),
    # rotating their library-relative offsets by the placement rotation.
    extra_props: list[list] = []
    for prop in lib_props:
        if len(prop) < 2:
            continue
        pname = prop[1]
        if pname in ("Reference", "Value", "ki_keywords", "ki_fp_filters", "ki_description"):
            continue
        prop_x, prop_y, prop_rot = _get_property_at(prop)
        pdx, pdy = _rotate_offset(prop_x, prop_y, rotation)
        pval = prop[2] if len(prop) >= 3 else ""
        extra_props.append(
            _build_property(
                pname,
                pval,
                x + pdx,
                y + pdy,
                prop_rot,
                hide=(pname not in _STANDARD_VISIBLE_PROPERTIES),
                do_not_autoplace=not fields_autoplaced,
            )
        )

    # Collect pin numbers for this unit.
    pin_numbers = _collect_unit_pin_numbers(lib_sym_raw, unit)

    # Build the placed symbol list.
    entry: list = [
        sexpdata.Symbol("symbol"),
        [sexpdata.Symbol("lib_id"), lib_id_str],
        [sexpdata.Symbol("at"), x, y, rotation],
        [sexpdata.Symbol("unit"), unit],
        [sexpdata.Symbol("exclude_from_sim"), sexpdata.Symbol("no")],
        [sexpdata.Symbol("in_bom"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("on_board"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("dnp"), sexpdata.Symbol("no")],
        *(
            [[sexpdata.Symbol("fields_autoplaced"), sexpdata.Symbol("yes")]]
            if fields_autoplaced
            else []
        ),
        [sexpdata.Symbol("uuid"), sym_uuid],
        _build_property(
            "Reference",
            reference,
            x + ref_dx,
            y + ref_dy,
            0,
            justify="left",
            do_not_autoplace=not fields_autoplaced,
            hide=reference.startswith("#"),
        ),
        _build_property(
            "Value",
            value,
            x + val_dx,
            y + val_dy,
            0,
            justify="left",
            do_not_autoplace=not fields_autoplaced,
        ),
    ]

    for prop in extra_props:
        entry.append(prop)

    for pin_num in pin_numbers:
        entry.append(
            [
                sexpdata.Symbol("pin"),
                pin_num,
                [sexpdata.Symbol("uuid"), str(uuid.uuid4())],
            ]
        )

    # instances block.
    entry.append(
        [
            sexpdata.Symbol("instances"),
            [
                sexpdata.Symbol("project"),
                project_name,
                [
                    sexpdata.Symbol("path"),
                    f"/{sch_uuid}",
                    [sexpdata.Symbol("reference"), reference],
                    [sexpdata.Symbol("unit"), unit],
                ],
            ],
        ]
    )

    return entry


def _find_property_by_name(sym: Any, name: str) -> Any | None:
    """Return the first property on *sym* whose raw name matches *name*.

    Uses ``prop.children[0]`` (the original, un-sanitised name as it appears
    in the S-expression) rather than skip's cleansed attribute key so that
    names containing spaces, hyphens, etc. are matched correctly.
    Returns ``None`` if no matching property exists.
    """
    try:
        for prop in sym.property:
            try:
                if prop.children[0] == name:
                    return prop
            except (AttributeError, IndexError):
                continue
    except AttributeError:
        pass
    return None


def _get_extends_base_name(lib_sym_raw: list) -> str | None:
    """Return the base symbol name from an ``(extends ...)`` clause, or ``None``."""
    for child in lib_sym_raw[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "extends"
        ):
            return str(child[1])
    return None


def _resolve_extends_symbol(lib_sym_raw: list, library_name: str) -> list:
    """Resolve an ``(extends ...)`` lib symbol into a fully self-contained definition.

    KiCad's ``.kicad_symdir`` format stores variant symbols (e.g.
    ``AP2112K-3.3``) as thin wrappers that reference a base symbol
    (e.g. ``AP2204K-1.5``) for all geometry and pin definitions via an
    ``(extends ...)`` clause.  When such a symbol is injected into a
    schematic's ``lib_symbols`` block it must be self-contained — there is no
    external library present at parse time — otherwise tools like ``skip`` that
    parse the schematic will crash with ``AttributeError`` on the missing
    sub-symbol children.

    This function:

    1. Detects the ``(extends "BaseName")`` child of *lib_sym_raw*.
    2. Looks the base symbol up in the index DB (same library table).
    3. Builds a merged raw list:
       - Structural attributes (``pin_numbers``, ``pin_names``, etc.) from the
         base, overridden by any that appear in the extending symbol.
       - Properties: base as default, extending symbol overrides.
       - Sub-symbols (``_N_M`` geometry/pin blocks) from the base, with their
         names re-prefixed to match the extending symbol.

    Returns *lib_sym_raw* unchanged if no ``(extends ...)`` clause is present
    or if the base symbol cannot be resolved (a warning is logged).
    """
    base_name = _get_extends_base_name(lib_sym_raw)
    if base_name is None:
        return lib_sym_raw

    extending_name = lib_sym_raw[1]  # e.g. "AP2112K-3.3"

    # For symdir libraries the DB key is "TableName/SymbolFileName".  The base
    # symbol lives in a sibling file, so replace just the file stem.
    parts = library_name.split("/", 1)
    base_lib_name = f"{parts[0]}/{base_name}" if len(parts) >= 2 else library_name

    mgr = _get_index_manager()
    base_sym_rec = mgr.get_symbol(base_lib_name, base_name)
    base_lib_rec = mgr.get_library_by_name(base_lib_name)

    if base_sym_rec is None or base_lib_rec is None:
        log.warning(
            "extends base symbol %r not found under library %r; "
            "injecting as-is (schematic may be incomplete)",
            base_name,
            base_lib_name,
        )
        return lib_sym_raw

    try:
        base_raw = extract_lib_symbol_raw(
            base_lib_rec.file_path,
            base_sym_rec.file_index,
            base_name,
            base_lib_rec.mtime,
            base_lib_rec.file_size,
        )
    except Exception as exc:
        log.warning(
            "Could not extract base symbol %r: %s; injecting as-is",
            base_name,
            exc,
        )
        return lib_sym_raw

    base_raw = copy.deepcopy(base_raw)

    # Tags that are not structural config attributes.
    _META_TAGS = frozenset({"symbol", "property", "extends"})

    # Collect structural attributes and properties from the *extending* symbol.
    ext_structural: dict[str, list] = {}
    ext_props: dict[str, list] = {}
    for child in lib_sym_raw[2:]:
        if not (
            isinstance(child, list) and len(child) >= 1 and isinstance(child[0], sexpdata.Symbol)
        ):
            continue
        tag = child[0].value()
        if tag == "extends":
            continue
        if tag == "property" and len(child) >= 2:
            ext_props[child[1]] = child
        elif tag not in _META_TAGS:
            ext_structural[tag] = child

    # Build merged symbol.
    merged: list = [sexpdata.Symbol("symbol"), extending_name]

    # 1. Structural attributes: base as default, extending overrides.
    seen_structural: set[str] = set()
    for child in base_raw[2:]:
        if not (
            isinstance(child, list) and len(child) >= 1 and isinstance(child[0], sexpdata.Symbol)
        ):
            continue
        tag = child[0].value()
        if tag in _META_TAGS or tag in seen_structural:
            continue
        merged.append(copy.deepcopy(ext_structural.get(tag, child)))
        seen_structural.add(tag)
    for tag, child in ext_structural.items():
        if tag not in seen_structural:
            merged.append(copy.deepcopy(child))

    # 2. Properties: base as default, extending overrides.
    seen_props: set[str] = set()
    for child in base_raw[2:]:
        if not (
            isinstance(child, list)
            and len(child) >= 1
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "property"
            and len(child) >= 2
        ):
            continue
        pname = child[1]
        if pname in seen_props:
            continue
        merged.append(copy.deepcopy(ext_props.get(pname, child)))
        seen_props.add(pname)
    for pname, child in ext_props.items():
        if pname not in seen_props:
            merged.append(copy.deepcopy(child))

    # 3. Sub-symbols: from base, with names re-prefixed to the extending name.
    base_prefix = base_name + "_"
    ext_prefix = extending_name + "_"
    for child in base_raw[2:]:
        if not (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "symbol"
        ):
            continue
        sub_name = child[1]
        if isinstance(sub_name, str) and sub_name.startswith(base_prefix):
            sub_copy = copy.deepcopy(child)
            sub_copy[1] = ext_prefix + sub_name[len(base_prefix) :]
            merged.append(sub_copy)

    return merged


def _add_lib_symbol(lib_symbols_wrapper: Any, lib_sym_raw: list, table_name: str) -> None:
    """Inject a lib symbol raw S-expression into a schematic's lib_symbols block.

    The skip library's ``LibSymbolsListWrapper`` does not provide a method for
    adding new symbols at runtime, so this function manipulates the underlying
    ``ParsedValue`` tree directly.

    The top-level symbol name in *lib_sym_raw* (e.g. ``"R"``) is prefixed with
    *table_name* to produce the qualified lib-id stored in the schematic
    (e.g. ``"Device:R"``).  Sub-symbols (e.g. ``"R_0_1"``) are left as-is,
    matching the format KiCad uses natively.

    Args:
        lib_symbols_wrapper: ``sch.lib_symbols`` (a ``LibSymbolsListWrapper``).
        lib_sym_raw: Raw S-expression list as returned by
            ``extract_lib_symbol_raw()``.
        table_name: Library table name, e.g. ``"Device"``.
    """
    sym_name = lib_sym_raw[1]  # e.g. "R"
    lib_id_str = f"{table_name}:{sym_name}"  # e.g. "Device:R"

    # Deep-copy to avoid mutating the caller's original list.
    raw_copy = copy.deepcopy(lib_sym_raw)
    # Only the top-level name needs the qualifier; sub-symbol names stay plain.
    raw_copy[1] = lib_id_str

    # lib_symbols_wrapper._pv._tree IS the same list object that lives in the
    # source tree (verified: _pv._tree is sourceTree[_pv._base_coords[0]]).
    # Appending here is therefore sufficient for sch.write() to serialise it.
    pv = lib_symbols_wrapper._pv
    pv._tree.append(raw_copy)

    # Keep the wrapper's internal lookup table consistent so that subsequent
    # `lib_id_str in sch.lib_symbols` checks return True immediately.
    lib_symbols_wrapper._libsyms_by_id[lib_id_str] = raw_copy


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_component_edit_tools(mcp: FastMCP) -> None:
    """Register all component editing tools with the MCP server."""

    @mcp.tool()
    async def add_symbol_to_schematic(
        schematic_path: str,
        library_name: str,
        symbol_name: str,
        x: float,
        y: float,
        rotation: int = 0,
        value: str | None = None,
        fields_autoplaced: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a symbol from a KiCad library to a schematic.

        Looks up the symbol in the index database, extracts its definition
        from the library file, injects it into the schematic's lib_symbols
        block, and inserts a placed instance for every unit of the symbol.
        Coordinates are mm in KiCad screen convention (**+Y is down**) and
        are auto-snapped to the 1.27 mm (50-mil) grid so pins land on KiCad's
        standard schematic grid and wires can connect to them. A backup
        (.kicad_sch.bak) is written before saving.

        For multi-unit symbols, additional units are auto-stacked below the
        anchor at +10 mm Y intervals.

        Tip: prefer ``place_symbol_relative`` when the new symbol should sit
        next to an existing component, or call ``find_free_area`` (with
        ``for_library``/``for_symbol``) to get a collision-free anchor before
        calling this tool.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            library_name: Library name as returned by ``search_symbols`` in
                the ``library_name`` field.  For KiCad 10 symdir-style
                libraries this is ``"TableName/FileBaseName"``
                (e.g. ``"Device/R_Small"``), not just the table name
                (e.g. not ``"Device"``).
            symbol_name: Symbol name within the library (e.g. "R").
            x: X placement coordinate in mm (will be aligned to 1.27 mm / 50-mil grid).
            y: Y placement coordinate in mm (will be aligned to 1.27 mm / 50-mil grid).
            rotation: Rotation in degrees; must be 0, 90, 180, or 270.
            value: Override for the Value property. Defaults to symbol_name.
            fields_autoplaced: When True (default) the placed symbol is marked
                ``(fields_autoplaced yes)`` so KiCad will automatically
                re-flow the Reference/Value field positions when the
                schematic is opened.  Set to False to suppress the flag,
                keeping the field positions fixed at the coordinates
                computed by this tool.

        Returns:
            dict with keys: success (bool), reference_assigned, lib_id,
            units_added, position (with 50-mil grid-aligned coords),
            body_bbox (world-space union of all placed unit bboxes — ``None``
            for graphics-less symbols), warnings.
        """
        return _do_add_symbol(
            schematic_path=schematic_path,
            library_name=library_name,
            symbol_name=symbol_name,
            x=x,
            y=y,
            rotation=rotation,
            value=value,
            fields_autoplaced=fields_autoplaced,
        )

    @mcp.tool()
    async def place_symbol_relative(
        schematic_path: str,
        library_name: str,
        symbol_name: str,
        anchor_reference: str,
        side: str,
        gap: float = 2.54,
        rotation: int = 0,
        value: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Place a new symbol next to an existing component without computing
        coordinates yourself.

        Looks up ``anchor_reference`` in the schematic, reads its world-space
        body bbox, picks an insertion point so the new symbol's body sits on
        the requested ``side`` with ``gap`` mm clearance (centre-aligned on
        the perpendicular axis), and inserts via the same path as
        ``add_symbol_to_schematic``. Coordinates are then snapped to the
        1.27 mm grid.

        Use this in preference to absolute ``add_symbol_to_schematic`` calls
        whenever you can describe the placement relative to an existing
        reference (e.g. "decoupling cap to the right of U1").

        Args:
            schematic_path: Path to the .kicad_sch file.
            library_name: Library name as accepted by
                ``add_symbol_to_schematic``.
            symbol_name: Symbol name within the library.
            anchor_reference: Reference designator of the existing component
                to anchor against (e.g. "U1").
            side: One of ``"right"``, ``"left"``, ``"above"``, ``"below"``
                (KiCad screen Y-down: "above" = smaller Y).
            gap: Clearance in mm between the bboxes. Defaults to 2.54 mm.
            rotation: Rotation for the new symbol. 0/90/180/270.
            value: Optional Value override.

        Returns:
            Same shape as ``add_symbol_to_schematic`` plus ``anchor_bbox``
            and ``side``. Returns ``{"error": ...}`` if the anchor can't be
            found or has no bbox.
        """
        if side not in ("right", "left", "above", "below"):
            return {"error": f"side must be right/left/above/below (got {side!r})"}
        if gap < 0 or not math.isfinite(gap):
            return {"error": f"gap must be a non-negative finite number (got {gap})"}

        # Look up anchor's world bbox via netlist extraction.
        try:
            from kcaa.utils.netlist_parser import extract_netlist

            netlist = extract_netlist(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to read schematic netlist: {exc}"}
        comps = netlist.get("components", {}) or {}
        anchor = comps.get(anchor_reference)
        if anchor is None:
            return {"error": f"Anchor reference {anchor_reference!r} not found"}
        bb_d = anchor.get("body_bbox")
        if not bb_d:
            return {
                "error": (
                    f"Anchor {anchor_reference!r} has no computable "
                    "bbox (lib symbol missing or graphics-less)"
                )
            }

        # We need the new symbol's lib bbox to centre it correctly.
        try:
            mgr = _get_index_manager()
            lib_rec = mgr.get_library_by_name(library_name)
            if lib_rec is None:
                return {"error": f"Library '{library_name}' not found in index"}
            sym_rec = mgr.get_symbol(library_name, symbol_name)
            if sym_rec is None:
                return {"error": (f"Symbol '{symbol_name}' not found in library '{library_name}'")}
            new_lib_raw = extract_lib_symbol_raw(
                lib_rec.file_path,
                sym_rec.file_index,
                symbol_name,
                lib_rec.mtime,
                lib_rec.file_size,
            )
            new_unit_bbs = compute_unit_bboxes(new_lib_raw)
        except Exception as exc:
            return {"error": f"Failed to inspect new symbol: {exc}"}

        if not new_unit_bbs:
            return {"error": "New symbol has no graphics; cannot compute relative placement"}

        # Predict the world bbox for ALL units placed at sym=(0,0) using the
        # same per-unit Y offset _do_add_symbol applies (unit_y = y + (N-1)*10).
        # This must mirror that loop or multi-unit symbols will overlap the
        # anchor or land off-centre.
        per_unit_world: list[BBox] = []
        for unit, lib_bb in sorted(new_unit_bbs.items()):
            unit_y_off = (unit - 1) * 10.0
            per_unit_world.append(lib_bbox_to_world(lib_bb, 0.0, unit_y_off, int(rotation), None))
        ref_at_origin = union_bboxes(per_unit_world)
        if ref_at_origin is None:
            return {"error": "New symbol has no graphics; cannot compute relative placement"}
        new_w = ref_at_origin.max_x - ref_at_origin.min_x
        new_h = ref_at_origin.max_y - ref_at_origin.min_y
        # Offset from sym placement (x,y) to bbox top-left corner.
        off_dx = ref_at_origin.min_x  # i.e. bbox.min_x - sym_x   (sym_x=0)
        off_dy = ref_at_origin.min_y

        anchor_min_x = float(bb_d["min_x"])
        anchor_min_y = float(bb_d["min_y"])
        anchor_max_x = float(bb_d["max_x"])
        anchor_max_y = float(bb_d["max_y"])
        anchor_cx = (anchor_min_x + anchor_max_x) / 2.0
        anchor_cy = (anchor_min_y + anchor_max_y) / 2.0

        # Compute the desired top-left of the new bbox.
        if side == "right":
            new_min_x = anchor_max_x + gap
            new_min_y = anchor_cy - new_h / 2.0
        elif side == "left":
            new_min_x = anchor_min_x - gap - new_w
            new_min_y = anchor_cy - new_h / 2.0
        elif side == "below":  # +Y in KiCad screen coords
            new_min_x = anchor_cx - new_w / 2.0
            new_min_y = anchor_max_y + gap
        else:  # "above"
            new_min_x = anchor_cx - new_w / 2.0
            new_min_y = anchor_min_y - gap - new_h

        # Translate bbox top-left back to the sym placement (x, y).
        sym_x = new_min_x - off_dx
        sym_y = new_min_y - off_dy

        result = _do_add_symbol(
            schematic_path=schematic_path,
            library_name=library_name,
            symbol_name=symbol_name,
            x=sym_x,
            y=sym_y,
            rotation=rotation,
            value=value,
            fields_autoplaced=True,
        )
        if result.get("success"):
            result["anchor_reference"] = anchor_reference
            result["anchor_bbox"] = bb_d
            result["side"] = side
        return result

    @mcp.tool()
    async def remove_symbol_from_schematic(
        schematic_path: str,
        references: list[str],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove placed symbol units for one or more reference designators.

        Removes every ``(symbol ...)`` entry whose Reference property matches
        any entry in *references* (case-sensitive).  Also removes the
        corresponding ``(lib_symbols ...)`` entry when no other placed symbol
        still uses that lib_id.  A backup (.kicad_sch.bak) is written before
        saving.

        Pass a single-element list to remove just one component, or multiple
        elements to remove several in one operation.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            references: List of reference designators to remove (e.g. ["C1",
                "R3"]).  Must contain at least one entry.

        Returns:
            dict with keys: success (bool), total_removed_units (int),
            results (dict mapping each reference to its outcome), warnings.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not references:
            return {"error": "references list must not be empty"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            ref_set = set(references)

            # Collect units to remove grouped by reference.
            per_ref: dict[str, list] = {r: [] for r in references}
            removed_lib_ids: set[str] = set()
            try:
                for sym in sch.symbol:
                    try:
                        ref_val = sym.property.Reference.value
                    except AttributeError:
                        continue
                    if ref_val in ref_set:
                        per_ref[ref_val].append(sym)
                        with contextlib.suppress(AttributeError):
                            removed_lib_ids.add(sym.lib_id.value)
            except AttributeError:
                pass  # empty schematic

            # Delete collected units.
            results: dict[str, Any] = {}
            total_removed = 0
            for ref, units in per_ref.items():
                if not units:
                    results[ref] = {"error": f"No symbol with reference {ref!r} found"}
                else:
                    for sym in units:
                        sym.delete()
                    results[ref] = {"removed_units": len(units)}
                    total_removed += len(units)

            if total_removed == 0:
                return {
                    "error": "None of the specified references were found",
                    "results": results,
                    "success": False,
                }

            # Remove orphaned lib_symbols entries.
            warnings: list[str] = []
            remaining_lib_ids: set[str] = set()
            try:
                for sym in sch.symbol:
                    with contextlib.suppress(AttributeError):
                        remaining_lib_ids.add(sym.lib_id.value)
            except AttributeError:
                pass

            for lib_id in removed_lib_ids:
                if lib_id not in remaining_lib_ids:
                    try:
                        del sch.lib_symbols[lib_id]
                    except Exception as exc:
                        warnings.append(f"Could not remove lib_symbol {lib_id!r}: {exc}")

            try:
                save_schematic(schematic_path, sch)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "total_removed_units": total_removed,
                "results": results,
                "warnings": warnings,
                "file_modified": schematic_path,
                "backup_path": schematic_path + ".bak",
            }

        except Exception as exc:
            log.exception("Unexpected error in remove_symbol_from_schematic")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def set_component_property(
        schematic_path: str,
        reference: str,
        property_name: str,
        property_value: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set or add a property on a placed schematic component.

        If the property already exists on the component it is updated
        in-place.  If it does not exist a new property is created by
        cloning the existing ``Value`` property entry and renaming it.
        The operation is applied to every unit that shares the given
        reference designator.  A backup (.kicad_sch.bak) is written
        before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            reference: Reference designator of the component to modify
                (e.g. "R1", "U3").
            property_name: Name of the property to set or create
                (e.g. "Value", "Footprint", "MPN", "Manufacturer").
            property_value: The new value string for the property.
                An empty string is a valid value and is permitted.

        Returns:
            dict with keys: success (bool), reference, property_name,
            property_value, units_updated (int), units_where_added (int),
            units_where_updated (int), action ("updated", "added", or
            "mixed" when some units already had the property and others
            did not).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not reference:
            return {"error": "reference must not be empty"}
        if not property_name:
            return {"error": "property_name must not be empty"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Collect all units with the given reference.
            units: list[Any] = []
            try:
                for sym in sch.symbol:
                    try:
                        if sym.property.Reference.value == reference:
                            units.append(sym)
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            if not units:
                return {"error": f"No symbol with reference {reference!r} found"}

            updated_count = 0
            added_count = 0
            for sym in units:
                existing = _find_property_by_name(sym, property_name)
                if existing is not None:
                    existing.value = property_value
                    updated_count += 1
                else:
                    # Clone the Value property to create a new entry with the
                    # correct structure (at, effects), then rename and set it.
                    try:
                        new_prop = sym.property.Value.clone()
                        new_prop.name = property_name
                        new_prop.value = property_value
                        # Non-standard properties are hidden by default in
                        # KiCad (only Reference and Value are visible on the
                        # canvas).  Inject (hide yes) into the effects node of
                        # the cloned property when needed.
                        if property_name not in _STANDARD_VISIBLE_PROPERTIES:
                            raw_tree = new_prop._pv._tree
                            for child in raw_tree:
                                if (
                                    isinstance(child, list)
                                    and len(child) >= 1
                                    and isinstance(child[0], sexpdata.Symbol)
                                    and child[0].value() == "effects"
                                ):
                                    child.append([sexpdata.Symbol("hide"), sexpdata.Symbol("yes")])
                                    break
                        added_count += 1
                    except Exception as exc:
                        return {
                            "error": f"Failed to add property {property_name!r} on unit {sym.unit.value if hasattr(sym, 'unit') else '?'}: {exc}"
                        }

            if added_count > 0 and updated_count > 0:
                action = "mixed"
            elif added_count > 0:
                action = "added"
            else:
                action = "updated"

            try:
                save_schematic(schematic_path, sch)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "reference": reference,
                "property_name": property_name,
                "property_value": property_value,
                "units_updated": len(units),
                "units_where_updated": updated_count,
                "units_where_added": added_count,
                "action": action,
                "file_modified": schematic_path,
                "backup_path": schematic_path + ".bak",
            }

        except Exception as exc:
            log.exception("Unexpected error in set_component_property")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def rename_symbol(
        schematic_path: str,
        symbol_uuid: str,
        target_reference: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Rename a symbol's reference designator in a schematic.

        Identifies the symbol by *symbol_uuid* (the ``uuid`` field from the
        ``.kicad_sch`` s-expression, returned by ``extract_schematic_netlist``
        in the ``uuid`` field of each component entry).  Once the anchor unit
        is located its current ``Reference`` property is read, and then every
        unit sharing that reference designator is updated together.

        A backup (.kicad_sch.bak) is written before saving.

        If *target_reference* is omitted, the next available reference for the
        same prefix is auto-assigned (scanning all project ``*.kicad_sch``
        files to avoid cross-sheet conflicts).

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            symbol_uuid: UUID of any placed unit of the symbol to rename
                (e.g. ``"a27313ed-36db-4154-9e69-a66c07529185"``).  Obtain
                this from ``extract_schematic_netlist`` → component ``uuid``.
            target_reference: New reference designator (e.g. ``"R10"``).
                When ``None`` (default), the next free reference for the same
                prefix is assigned automatically.

        Returns:
            dict with keys: success (bool), current_reference (the reference
            before the rename), target_reference (the final reference used),
            units_updated (int), file_modified, backup_path, and
            auto_assigned (bool, ``True`` when *target_reference* was
            auto-generated).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not symbol_uuid:
            return {"error": "symbol_uuid must not be empty"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Find the anchor unit by UUID to determine current_reference.
            anchor: Any | None = None
            try:
                for sym in sch.symbol:
                    try:
                        if sym.uuid.value == symbol_uuid:
                            anchor = sym
                            break
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            if anchor is None:
                return {"error": f"No symbol with UUID {symbol_uuid!r} found"}

            try:
                current_reference = anchor.property.Reference.value
            except AttributeError:
                return {"error": f"Symbol with UUID {symbol_uuid!r} has no Reference property"}

            if target_reference is not None and current_reference == target_reference:
                return {"error": "current_reference and target_reference are the same"}

            # Collect all units sharing current_reference (the full component).
            units: list[Any] = []
            try:
                for sym in sch.symbol:
                    try:
                        if sym.property.Reference.value == current_reference:
                            units.append(sym)
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            # Auto-assign target_reference if not provided.
            auto_assigned = False
            if target_reference is None:
                prefix_match = re.match(r"^([A-Za-z]+)", current_reference)
                prefix = prefix_match.group(1) if prefix_match else "U"
                target_reference = _next_reference(sch, prefix, schematic_path=schematic_path)
                auto_assigned = True

            # Check that target_reference does not already exist (skip the
            # symbol being renamed itself).
            for sym in sch.symbol:
                try:
                    if sym not in units and sym.property.Reference.value == target_reference:
                        return {
                            "error": f"Target reference {target_reference!r} already exists in this schematic"
                        }
                except AttributeError:
                    continue

            # Update the Reference property on every unit.
            for sym in units:
                ref_prop = _find_property_by_name(sym, "Reference")
                if ref_prop is not None:
                    ref_prop.value = target_reference
                else:
                    return {"error": f"Symbol {current_reference!r} has no Reference property"}
                # Also update instances.path.reference — KiCad uses this as the
                # authoritative display reference.
                _update_instance_reference(sym, target_reference)

            try:
                save_schematic(schematic_path, sch)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "current_reference": current_reference,
                "target_reference": target_reference,
                "auto_assigned": auto_assigned,
                "units_updated": len(units),
                "file_modified": schematic_path,
                "backup_path": schematic_path + ".bak",
            }

        except Exception as exc:
            log.exception("Unexpected error in rename_symbol")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def list_component_properties(
        schematic_path: str,
        reference: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all properties of a placed schematic component.

        Returns every ``(property ...)`` entry found on the first unit of the
        component identified by *reference*.  All units of a multi-unit
        symbol share the same property list, so reading from the first unit
        is sufficient.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            reference: Reference designator of the component to inspect
                (e.g. "R1", "U3").

        Returns:
            dict with keys: success (bool), reference,
            properties (list of {name (str), value (str)}).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not reference:
            return {"error": "reference must not be empty"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Find the first unit with the given reference.
            first_unit: Any | None = None
            try:
                for sym in sch.symbol:
                    try:
                        if sym.property.Reference.value == reference:
                            first_unit = sym
                            break
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            if first_unit is None:
                return {"error": f"No symbol with reference {reference!r} found"}

            properties: list[dict[str, str]] = []
            try:
                for prop in first_unit.property:
                    try:
                        name = prop.children[0]
                        value = prop.value
                        properties.append({"name": str(name), "value": str(value)})
                    except (AttributeError, IndexError):
                        continue
            except AttributeError:
                pass

            return {
                "success": True,
                "reference": reference,
                "properties": properties,
            }

        except Exception as exc:
            log.exception("Unexpected error in list_component_properties")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def delete_component_property(
        schematic_path: str,
        reference: str,
        property_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete a property from a placed schematic component.

        Removes the named property from every unit that shares the given
        reference designator.  ``Reference`` and ``Value`` are required KiCad
        fields and cannot be deleted; attempting to do so returns an error
        without modifying the file.  A backup (.kicad_sch.bak) is written
        before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            reference: Reference designator of the component to modify
                (e.g. "R1", "U3").
            property_name: Name of the property to delete
                (e.g. "MPN", "Manufacturer").  ``Reference`` and ``Value``
                are protected and cannot be deleted.

        Returns:
            dict with keys: success (bool), reference, property_name,
            units_updated (int).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not reference:
            return {"error": "reference must not be empty"}
        if not property_name:
            return {"error": "property_name must not be empty"}
        if property_name in ("Reference", "Value"):
            return {
                "error": (f"Property {property_name!r} is required by KiCad and cannot be deleted")
            }

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Collect all units with the given reference.
            units: list[Any] = []
            try:
                for sym in sch.symbol:
                    try:
                        if sym.property.Reference.value == reference:
                            units.append(sym)
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            if not units:
                return {"error": f"No symbol with reference {reference!r} found"}

            updated_count = 0
            for sym in units:
                prop = _find_property_by_name(sym, property_name)
                if prop is not None:
                    prop._pv.delete()
                    updated_count += 1

            if updated_count == 0:
                return {
                    "error": (f"Property {property_name!r} not found on component {reference!r}")
                }

            try:
                save_schematic(schematic_path, sch)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "reference": reference,
                "property_name": property_name,
                "units_updated": updated_count,
                "file_modified": schematic_path,
                "backup_path": schematic_path + ".bak",
            }

        except Exception as exc:
            log.exception("Unexpected error in delete_component_property")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def move_component(
        schematic_path: str,
        reference: str,
        x: float | None = None,
        y: float | None = None,
        rotation: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move and/or rotate a placed schematic component.

        At least one of ``x``, ``y``, ``rotation`` must be provided; omitted
        values are left unchanged. Coordinates are mm in KiCad screen
        convention (**+Y is down**) and are auto-snapped to the 1.27 mm
        (50-mil) grid so pins remain on KiCad's standard schematic grid and
        existing wires stay connected. Rotation is absolute (0/90/180/270°).
        All units sharing the reference are moved together, and Reference /
        Value field positions shift by the same delta. A backup
        (.kicad_sch.bak) is written before saving.

        Tip: to move a component to sit relative to another, prefer
        ``place_symbol_relative`` (handles bbox + clearance for you). To find
        free space, call ``find_free_area`` first.

        Args:
            schematic_path: Path to the .kicad_sch file.
            reference: Reference designator (e.g. "R1") or a sheet name.
            x: New X coordinate in mm (auto grid-snapped).
            y: New Y coordinate in mm (auto grid-snapped).
            rotation: New absolute rotation in degrees (0, 90, 180, or 270).

        Returns:
            dict with keys: success, reference, position ({x, y} after grid
            snap), rotation, units_updated, body_bbox (world-space union of
            unit bboxes after the move; ``None`` for graphics-less symbols).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not reference:
            return {"error": "reference must not be empty"}
        if x is None and y is None and rotation is None:
            return {"error": "At least one of x, y, or rotation must be provided"}
        if x is not None and not math.isfinite(x):
            return {"error": f"x must be a finite number (got {x!r})"}
        if y is not None and not math.isfinite(y):
            return {"error": f"y must be a finite number (got {y!r})"}
        if rotation is not None and rotation not in (0, 90, 180, 270):
            return {"error": f"rotation must be 0, 90, 180, or 270 (got {rotation!r})"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            units: list[Any] = []
            try:
                for sym in sch.symbol:
                    try:
                        if sym.property.Reference.value == reference:
                            units.append(sym)
                    except AttributeError:
                        continue
            except AttributeError:
                pass

            if not units:
                from kcaa.tools.sheet_tools import (
                    _do_update_sheet_symbol,
                    _list_sheet_symbols_impl,
                )

                sheet_info = next(
                    (
                        sheet
                        for sheet in _list_sheet_symbols_impl(schematic_path).get("sheets", [])
                        if sheet.get("sheet_name") == reference or sheet.get("uuid") == reference
                    ),
                    None,
                )
                if sheet_info is not None:
                    if rotation is not None:
                        return {"error": "rotation is not supported for sheet symbols"}
                    sheet_result = _do_update_sheet_symbol(
                        schematic_path=schematic_path,
                        sheet_identifier=reference,
                        sheet_name=None,
                        sheet_file=None,
                        x=x,
                        y=y,
                        width=None,
                        height=None,
                    )
                    if "error" in sheet_result:
                        return sheet_result
                    return {
                        "success": True,
                        "sheet_name": sheet_result.get("sheet_name")
                        or sheet_info.get("sheet_name"),
                        "position": sheet_result.get("position")
                        or {
                            "x": sheet_info.get("position", {}).get("x"),
                            "y": sheet_info.get("position", {}).get("y"),
                        },
                        "type": "sheet",
                    }
                return {"error": f"No symbol or sheet named {reference!r} found"}

            # Compute the target position (grid-snapped), then nudge it to
            # avoid overlap with other symbols/sheets if needed.
            first_at = units[0].at.value
            raw_new_x = _align_to_grid(x) if x is not None else float(first_at[0])
            raw_new_y = _align_to_grid(y) if y is not None else float(first_at[1])
            final_new_x = raw_new_x
            final_new_y = raw_new_y
            position_adjusted = False
            if x is not None or y is not None:
                try:
                    from kcaa.tools.placement_helpers import _find_free_area_impl
                    from kcaa.tools.sheet_tools import _has_position_conflict
                    from kcaa.utils.netlist_parser import extract_netlist

                    netlist = extract_netlist(schematic_path)
                    comp_info = (netlist.get("components") or {}).get(reference)
                    bb_d = comp_info.get("body_bbox") if comp_info else None
                    if bb_d:
                        bbox_w = float(bb_d["max_x"]) - float(bb_d["min_x"])
                        bbox_h = float(bb_d["max_y"]) - float(bb_d["min_y"])
                        # Offset from body_bbox origin to symbol origin (at position).
                        off_x = float(bb_d["min_x"]) - float(first_at[0])
                        off_y = float(bb_d["min_y"]) - float(first_at[1])
                        # Determine the UUID of the symbol to exclude it from
                        # the obstacle list while it is being moved.
                        try:
                            sym_uuid = units[0]._pv._tree[
                                next(
                                    i
                                    for i, c in enumerate(units[0]._pv._tree)
                                    if isinstance(c, list)
                                    and len(c) >= 1
                                    and isinstance(c[0], sexpdata.Symbol)
                                    and c[0].value() == "uuid"
                                )
                            ][1]
                        except Exception:
                            sym_uuid = None
                        # Try target position first; only search for free area
                        # if there is an actual conflict.
                        target_bbox_x = raw_new_x + off_x
                        target_bbox_y = raw_new_y + off_y
                        has_conflict = _has_position_conflict(
                            schematic_path,
                            target_bbox_x,
                            target_bbox_y,
                            bbox_w,
                            bbox_h,
                            exclude_uuid=sym_uuid,
                        )
                        if has_conflict:
                            free = _find_free_area_impl(
                                schematic_path=schematic_path,
                                width=bbox_w,
                                height=bbox_h,
                                prefer_near={"x": raw_new_x, "y": raw_new_y},
                                max_candidates=1,
                                exclude_uuid=sym_uuid,
                            )
                            cand = (free.get("candidates") or [{}])[0]
                            origin = cand.get("origin")
                            if origin is not None:
                                cand_x = float(origin["x"])
                                cand_y = float(origin["y"])
                                # Convert free-area bbox origin back to symbol origin.
                                adj_x = cand_x - off_x
                                adj_y = cand_y - off_y
                                if x is None:
                                    adj_x = raw_new_x
                                if y is None:
                                    adj_y = raw_new_y
                                if abs(adj_x - raw_new_x) > 1e-6 or abs(adj_y - raw_new_y) > 1e-6:
                                    final_new_x = _align_to_grid(adj_x)
                                    final_new_y = _align_to_grid(adj_y)
                                    position_adjusted = True
                except Exception:
                    pass  # Overlap avoidance is best-effort; proceed with requested coords.

            for sym in units:
                at = sym.at.value
                old_x = at[0]
                old_y = at[1]
                new_x = final_new_x if (x is not None) else old_x
                new_y = final_new_y if (y is not None) else old_y
                new_rot = rotation if rotation is not None else (at[2] if len(at) > 2 else 0)
                dx = new_x - old_x
                dy = new_y - old_y
                sym.at.value = [new_x, new_y, new_rot]
                # Shift every property's (at x y rot) by the same delta so
                # Reference/Value and other field labels move with the symbol.
                # Also ensure fields_autoplaced is set so KiCad's GUI knows
                # to reflow fields if the user manually opens the file.
                try:
                    raw_tree = sym._pv._tree
                    fa_found = False
                    for child in raw_tree:
                        if (
                            isinstance(child, list)
                            and len(child) >= 2
                            and isinstance(child[0], sexpdata.Symbol)
                        ):
                            tag = child[0].value()
                            if tag == "fields_autoplaced":
                                child[1] = sexpdata.Symbol("yes")
                                fa_found = True
                            elif tag == "property":
                                # Shift the (at px py rot) sub-node
                                for sub in child[2:]:
                                    if (
                                        isinstance(sub, list)
                                        and len(sub) >= 3
                                        and isinstance(sub[0], sexpdata.Symbol)
                                        and sub[0].value() == "at"
                                    ):
                                        sub[1] = round(float(sub[1]) + dx, 4)
                                        sub[2] = round(float(sub[2]) + dy, 4)
                                        break
                    if not fa_found:
                        fa_node = [sexpdata.Symbol("fields_autoplaced"), sexpdata.Symbol("yes")]
                        uuid_idx = next(
                            (
                                i
                                for i, child in enumerate(raw_tree)
                                if isinstance(child, list)
                                and len(child) >= 1
                                and isinstance(child[0], sexpdata.Symbol)
                                and child[0].value() == "uuid"
                            ),
                            len(raw_tree),
                        )
                        raw_tree.insert(uuid_idx, fa_node)
                except Exception:
                    log.debug(
                        "Failed to insert field_autoplace node, symbol position still applied"
                    )

            try:
                save_schematic(schematic_path, sch)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            final_at = units[0].at.value
            # Compute updated body_bbox (best-effort) so the LLM doesn't need
            # to re-extract the netlist after a move.
            body_bbox = None
            try:
                lib_id = units[0].lib_id.value
                wrapper = sch.lib_symbols._libsyms_by_id.get(lib_id)
                lib_raw = None
                if wrapper is not None:
                    if isinstance(wrapper, list):
                        lib_raw = wrapper
                    else:
                        pv = getattr(wrapper, "_pv", None)
                        lib_raw = getattr(pv, "_tree", None) if pv is not None else None
                if lib_raw is not None:
                    placements: list[tuple[int, float, float, int, str | None]] = []
                    for sym in units:
                        try:
                            unit_no = int(sym.unit.value)
                        except (AttributeError, ValueError, TypeError):
                            unit_no = 1
                        at = sym.at.value
                        rot = int(round(float(at[2]))) if len(at) > 2 else 0
                        try:
                            mv = sym.mirror.value
                            mirror_val = mv.value() if hasattr(mv, "value") else mv
                        except AttributeError:
                            mirror_val = None
                        placements.append((unit_no, float(at[0]), float(at[1]), rot, mirror_val))
                    body_bbox = _placed_world_bbox(lib_raw, placements)
            except Exception:
                body_bbox = None
            out: dict[str, Any] = {
                "success": True,
                "reference": reference,
                "position": {"x": final_at[0], "y": final_at[1]},
                "rotation": final_at[2] if len(final_at) > 2 else 0,
                "units_updated": len(units),
                "body_bbox": body_bbox,
                "position_adjusted": position_adjusted,
                "file_modified": schematic_path,
                "backup_path": schematic_path + ".bak",
            }
            if position_adjusted:
                out["requested_position"] = {"x": raw_new_x, "y": raw_new_y}
                out["note"] = "Position adjusted to nearest free area to avoid overlap."
            return out

        except Exception as exc:
            log.exception("Unexpected error in move_component")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def add_label_to_schematic(
        schematic_path: str,
        text: str,
        x: float,
        y: float,
        angle: int = 0,
        label_type: str = "local",
        shape: str = "input",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a local, global, or hierarchical net label to a KiCad schematic.

        Labels create named net connections at the given coordinate. The label
        must sit **exactly on a wire or pin endpoint** to actually attach —
        coordinates are mm in screen convention (**+Y is down**) and **must be
        aligned to the 1.27 mm (50-mil) grid**. This tool does NOT auto-snap;
        pass coordinates from ``extract_schematic_netlist`` (pin x/y) or wire
        endpoints (wires returned by ``include_wire_topology=True``).

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            text: Net label text (e.g. "VCC", "NET_A"). Must not be empty.
            x: X coordinate of the label connection point in mm.
            y: Y coordinate of the label connection point in mm.
            angle: Label text rotation in degrees; must be 0, 90, 180, or
                270. 0 = text reads left-to-right; 90 = bottom-to-top.
            label_type: Label kind: "local", "global", or "hierarchical".
            shape: Shape for global or hierarchical labels.

        Returns:
            dict with keys: success (bool), label (text, x, y, direction,
            label_type, shape). direction is one of "right", "down", "left",
            "up".
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not text:
            return {"error": "text must not be empty"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}
        if angle not in (0, 90, 180, 270):
            return {"error": f"angle must be 0, 90, 180, or 270 (got {angle})"}
        if label_type not in VALID_LABEL_TYPES:
            return {"error": f"label_type must be one of {VALID_LABEL_TYPES}"}
        if label_type in ("global", "hierarchical") and shape not in VALID_SHAPES:
            return {"error": f"shape must be one of {VALID_SHAPES}"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        shape_value: str | None = None
        try:
            if label_type == "local":
                lbl = sch.label.new()
                lbl.value = text
                lbl.at.value = [x, y, angle]
            elif label_type == "global":
                try:
                    lbl = sch.global_label.new()
                except AttributeError:
                    from skip.element_template import ElementTemplate

                    lbl = sch.new_from_list(list(ElementTemplate["global_label"]))
                lbl.value = text
                lbl.at.value = [x, y, angle]
                lbl.shape.value = shape
                shape_value = shape
            else:
                hier_tmpl = [
                    sexpdata.Symbol("hierarchical_label"),
                    text,
                    [sexpdata.Symbol("shape"), sexpdata.Symbol(shape)],
                    [sexpdata.Symbol("at"), x, y, angle],
                    [
                        sexpdata.Symbol("effects"),
                        [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
                        [sexpdata.Symbol("justify"), sexpdata.Symbol("left")],
                    ],
                    [sexpdata.Symbol("uuid"), sexpdata.Symbol(str(uuid.uuid4()))],
                ]
                lbl = sch.new_from_list(hier_tmpl)
                shape_value = shape

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to add label: {exc}"}

        return {
            "success": True,
            "label": {
                "text": text,
                "x": x,
                "y": y,
                "direction": _angle_to_direction(angle),
                "label_type": label_type,
                "shape": shape_value,
            },
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }

    @mcp.tool()
    async def list_labels_in_schematic(
        schematic_path: str,
        label_type: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List local, global, and hierarchical labels in a KiCad schematic.

        Returns every matching label's text, position, type, and shape. Use the
        returned coordinates with delete_label_from_schematic to remove a
        specific label.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            label_type: Optional filter: "local", "global", or "hierarchical".

        Returns:
            dict with keys: success (bool), labels (list of {text, x, y,
            direction, label_type, shape}), count (int). direction is one of
            "right", "down", "left", "up".
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if label_type is not None and label_type not in VALID_LABEL_TYPES:
            return {"error": f"label_type must be one of {VALID_LABEL_TYPES}"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        labels = []
        for current_type, attr_name in (
            ("local", "label"),
            ("global", "global_label"),
            ("hierarchical", "hierarchical_label"),
        ):
            if label_type is not None and current_type != label_type:
                continue
            for lbl in _iter_schematic_labels(sch, attr_name):
                try:
                    at_val = lbl.at.value
                    labels.append(
                        {
                            "text": str(lbl.value),
                            "x": float(at_val[0]),
                            "y": float(at_val[1]),
                            "direction": _angle_to_direction(at_val[2] if len(at_val) > 2 else 0),
                            "label_type": current_type,
                            "shape": (
                                str(lbl.shape.value)
                                if current_type in ("global", "hierarchical")
                                else None
                            ),
                        }
                    )
                except (AttributeError, IndexError, TypeError, ValueError):
                    continue

        return {"success": True, "labels": labels, "count": len(labels)}

    @mcp.tool()
    async def check_reference_conflicts(
        schematic_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Check for duplicate reference designators across a KiCad project.

        Follows the sheet hierarchy from the given schematic and reports any
        reference designators that appear in more than one schematic file.
        Only schematics referenced (directly or transitively) from
        *schematic_path* are scanned — backup or history folders are not
        included.

        Args:
            schematic_path: Absolute path to the root .kicad_sch file.

        Returns:
            dict with keys: success (bool), root_schematic (str or null),
            schematics_scanned (int), conflicts (list of
            ``{reference, instances: [{sheet, uuid}]}``).
            For multi-unit symbols only the anchor unit's UUID is reported.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}

        project_dir = _find_project_dir(schematic_path)
        if project_dir is None:
            return {
                "success": True,
                "root_schematic": None,
                "schematics_scanned": 0,
                "conflicts": [],
                "message": "No .kicad_pro found — cannot determine project scope",
            }

        root_sch = _find_root_schematic(schematic_path) or schematic_path
        refs_by_file = _collect_hierarchy_references(root_sch)

        # Invert: reference → list of {sheet, uuid} dicts.
        ref_to_instances: dict[str, list[dict[str, str]]] = {}
        for file_path, ref_dict in refs_by_file.items():
            for ref_str, ref_uuid in ref_dict.items():
                ref_to_instances.setdefault(ref_str, []).append(
                    {"sheet": file_path, "uuid": ref_uuid}
                )

        conflicts = [
            {
                "reference": ref_str,
                "instances": sorted(info_list, key=lambda x: x["sheet"]),
            }
            for ref_str, info_list in ref_to_instances.items()
            if len(info_list) > 1
        ]
        conflicts.sort(key=lambda c: c["reference"])

        return {
            "success": True,
            "root_schematic": root_sch,
            "schematics_scanned": len(refs_by_file),
            "conflicts": conflicts,
        }

    @mcp.tool()
    async def delete_label_from_schematic(
        schematic_path: str,
        x: float = 0.0,
        y: float = 0.0,
        text: str | None = None,
        tolerance: float = 0.01,
        positions: list[dict] | None = None,
        label_type: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete one or more local, global, or hierarchical labels.

        **Single mode** (default): removes all matching labels whose position
        matches (x, y) within *tolerance*. When *text* is provided, only labels
        with that exact text are removed. When *label_type* is provided, only
        that label type is considered.

        **Batch mode**: when *positions* is provided, the *x*/*y*/*text*
        parameters are ignored and each entry in *positions* is processed
        independently. Each entry is a dict with keys ``x`` (float), ``y``
        (float), and optionally ``text`` (str). *label_type* still applies.

        Use list_labels_in_schematic first to obtain exact coordinates.
        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            x: X coordinate of the label in mm (single mode).
            y: Y coordinate of the label in mm (single mode).
            text: Optional label text to match (single mode).
            tolerance: Maximum coordinate difference considered a match
                (default 0.01 mm). Applied in both modes.
            positions: Batch mode — list of dicts, each with ``x``, ``y``,
                and optional ``text`` keys.
            label_type: Optional filter: "local", "global", or "hierarchical".

        Returns:
            Single mode: dict with keys success (bool), deleted_count (int).
            Batch mode: dict with keys success (bool), total_deleted (int),
            results (list with per-entry outcome).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if label_type is not None and label_type not in VALID_LABEL_TYPES:
            return {"error": f"label_type must be one of {VALID_LABEL_TYPES}"}

        label_sources = (
            ("local", "label"),
            ("global", "global_label"),
            ("hierarchical", "hierarchical_label"),
        )

        # ---- batch mode ------------------------------------------------
        if positions is not None:
            if not positions:
                return {"error": "positions list must not be empty"}

            try:
                sch = safe_schematic(schematic_path)
            except Exception as exc:
                return {"error": f"Failed to open schematic: {exc}"}

            try:
                all_labels: list[tuple[str, Any]] = []
                for current_type, attr_name in label_sources:
                    if label_type is not None and current_type != label_type:
                        continue
                    all_labels.extend(
                        (current_type, lbl) for lbl in _iter_schematic_labels(sch, attr_name)
                    )

                results = []
                total_deleted = 0
                global_to_delete: list[Any] = []
                queued_ids: set[int] = set()

                for entry in positions:
                    ex = float(entry.get("x", 0.0))
                    ey = float(entry.get("y", 0.0))
                    etxt = entry.get("text")
                    if not math.isfinite(ex) or not math.isfinite(ey):
                        results.append({"x": ex, "y": ey, "error": "Non-finite coordinates"})
                        continue

                    matched = []
                    for _current_type, lbl in all_labels:
                        if id(lbl) in queued_ids:
                            continue
                        try:
                            at_val = lbl.at.value
                            lx, ly = float(at_val[0]), float(at_val[1])
                            if abs(lx - ex) <= tolerance and abs(ly - ey) <= tolerance:
                                if etxt is None or str(lbl.value) == etxt:
                                    matched.append(lbl)
                        except (AttributeError, IndexError, TypeError, ValueError):
                            continue

                    if not matched:
                        kind = f" {label_type}" if label_type is not None else ""
                        msg = f"No{kind} label found at ({ex}, {ey}) within tolerance {tolerance}"
                        if etxt is not None:
                            msg += f" with text {etxt!r}"
                        entry_result: dict[str, Any] = {"x": ex, "y": ey, "error": msg}
                        if etxt is not None:
                            entry_result["text"] = etxt
                        results.append(entry_result)
                    else:
                        for lbl in matched:
                            queued_ids.add(id(lbl))
                        global_to_delete.extend(matched)
                        total_deleted += len(matched)
                        entry_result = {"x": ex, "y": ey, "deleted_count": len(matched)}
                        if etxt is not None:
                            entry_result["text"] = etxt
                        results.append(entry_result)

                for lbl in global_to_delete:
                    lbl.delete()

                if total_deleted > 0:
                    try:
                        save_schematic(schematic_path, sch)
                    except Exception as exc:
                        return {"error": f"Failed to save schematic: {exc}"}

                result: dict[str, Any] = {
                    "success": total_deleted > 0,
                    "total_deleted": total_deleted,
                    "results": results,
                }
                if total_deleted > 0:
                    result["file_modified"] = schematic_path
                    result["backup_path"] = schematic_path + ".bak"
                return result

            except Exception as exc:
                log.exception("Unexpected error in delete_label_from_schematic (batch)")
                return {"error": str(exc), "success": False}

        # ---- single mode -----------------------------------------------
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            to_delete = []
            for current_type, attr_name in label_sources:
                if label_type is not None and current_type != label_type:
                    continue
                for lbl in _iter_schematic_labels(sch, attr_name):
                    try:
                        at_val = lbl.at.value
                        lx, ly = float(at_val[0]), float(at_val[1])
                        if abs(lx - x) <= tolerance and abs(ly - y) <= tolerance:
                            if text is None or str(lbl.value) == text:
                                to_delete.append(lbl)
                    except (AttributeError, IndexError, TypeError, ValueError):
                        continue

            if not to_delete:
                kind = f" {label_type}" if label_type is not None else ""
                msg = f"No{kind} label found at ({x}, {y}) within tolerance {tolerance}"
                if text is not None:
                    msg += f" with text {text!r}"
                return {"error": msg}

            for lbl in to_delete:
                lbl.delete()

            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to delete label: {exc}"}

        return {
            "success": True,
            "deleted_count": len(to_delete),
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }
