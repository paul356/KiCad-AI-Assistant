"""Schematic editing tools for KiCad MCP server.

Provides tools to add symbols to KiCad schematics by combining
the symbol index DB, the streaming extractor, and the skip library.
"""

import copy
import logging
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import sexpdata
import skip
from fastmcp import FastMCP
from fastmcp import Context

from kicad_mcp.config import LibraryPathConfig
from kicad_mcp.utils.symbol_extractor import extract_lib_symbol_raw
from kicad_mcp.utils.symbol_index_manager import SymbolIndexManager
from kicad_mcp.utils.symbol_index_reader import SymbolIndexReader

log = logging.getLogger(__name__)

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


def _next_reference(sch: Any, prefix: str) -> str:
    """
    Auto-assign the next available reference designator for a given prefix.

    Scans sch.symbol for references that start with ``prefix`` followed by
    digits, finds the maximum integer suffix, and returns prefix + (max+1).
    Returns prefix + "1" if no existing references match.
    """
    suffix_re = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    max_n = 0
    try:
        for sym in sch.symbol:
            try:
                ref_val = sym.property.Reference.value
                m = suffix_re.match(ref_val)
                if m:
                    max_n = max(max_n, int(m.group(1)))
            except AttributeError:
                continue
    except AttributeError:
        # sch.symbol doesn't exist on an empty schematic.
        pass
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
        rest = sub_name[len(prefix):]   # e.g. "1_1" or "0_1"
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
        rest = sub_name[len(prefix):]
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
        if pname in ("Reference", "Value",
                     "ki_keywords", "ki_fp_filters", "ki_description"):
            continue
        prop_x, prop_y, prop_rot = _get_property_at(prop)
        pdx, pdy = _rotate_offset(prop_x, prop_y, rotation)
        pval = prop[2] if len(prop) >= 3 else ""
        extra_props.append(
            _build_property(pname, pval, x + pdx, y + pdy, prop_rot,
                            hide=(pname == "Description"),
                            do_not_autoplace=not fields_autoplaced)
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
        *([[sexpdata.Symbol("fields_autoplaced"), sexpdata.Symbol("yes")]] if fields_autoplaced else []),
        [sexpdata.Symbol("uuid"), sym_uuid],
        _build_property("Reference", reference, x + ref_dx, y + ref_dy, 0,
                         justify="left", do_not_autoplace=not fields_autoplaced),
        _build_property("Value", value, x + val_dx, y + val_dy, 0,
                         justify="left", do_not_autoplace=not fields_autoplaced),
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
        The placement coordinates are automatically aligned to the 1.27 mm
        (50-mil) grid so that pins land on KiCad's standard schematic grid
        and wires can connect to them. A backup (.kicad_sch.bak) is written before 
        saving.

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
            units_added, position (with 50-mil grid-aligned coords), warnings.
        """
        # ---- Input validation ----
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}
        if rotation not in (0, 90, 180, 270):
            return {"error": f"rotation must be 0, 90, 180, or 270 (got {rotation})"}

        # Align coordinates to grid
        x = _align_to_grid(x)
        y = _align_to_grid(y)
        try:
            # For KiCad 10+ .kicad_symdir libraries the index stores
            # library_name as "TableName/SymFileName" (e.g. "Device/R_Small").
            # KiCad's lib_id format is "TableName:SymbolName" (e.g. "Device:R_Small"),
            # so we use only the part before the first "/" as the table name.
            table_name = library_name.split("/")[0]
            lib_id_str = f"{table_name}:{symbol_name}"
            effective_value = value or symbol_name

            # Index lookup
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
                        f"Symbol '{symbol_name}' not found in library '{library_name}'. "
                        "Verify the symbol name is correct."
                    )
                }

            # Extract lib symbol raw S-expression
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

            # Open schematic
            try:
                sch = skip.Schematic(schematic_path)
            except Exception as exc:
                return {"error": f"Failed to open schematic: {exc}"}

            # Schematic UUID
            sch_uuid_obj = getattr(sch, "uuid", None)
            if sch_uuid_obj is not None:
                sch_uuid = str(sch_uuid_obj.value).lstrip("/")
            else:
                sch_uuid = str(uuid.uuid4())

            # Inject lib symbol definition if absent.
            # Must pass table_name (e.g. "Device") not the full index key
            # (e.g. "Device/C") so that sub-symbol names are formed as
            # "Device:C_0_1" rather than "Device/C:C_0_1".
            try:
                if lib_id_str not in sch.lib_symbols:
                    _add_lib_symbol(sch.lib_symbols, lib_sym_raw, table_name)
            except Exception as exc:
                return {"error": f"Failed to inject lib symbol: {exc}"}

            # Unit count and reference prefix
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
            reference = _next_reference(sch, prefix)

            # Project name
            project_name = _find_project_name(schematic_path)

            # Build and insert all units
            for unit in range(1, unit_count + 1):
                unit_y = y + (unit - 1) * 10.0
                placed_raw = _build_placed_symbol(
                    lib_id_str, x, unit_y, rotation, unit,
                    reference, effective_value, sch_uuid,
                    project_name, lib_sym_raw,
                    fields_autoplaced=fields_autoplaced,
                )
                sch.new_from_list(placed_raw)
                # Note: new_from_list appends to the raw S-expression tree
                # but does not register the entry in sch.symbol
                # (SymbolCollection). The symbol will appear in sch.symbol
                # after write() + reload.

            # Backup and save
            try:
                shutil.copy(schematic_path, schematic_path + ".bak")
                sch.write(schematic_path)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "reference_assigned": reference,
                "lib_id": lib_id_str,
                "units_added": unit_count,
                "position": {"x": x, "y": y},
                "warnings": [],
            }

        except Exception as exc:
            log.exception("Unexpected error in add_symbol_to_schematic")
            return {"error": str(exc), "success": False}

    @mcp.tool()
    async def remove_symbol_from_schematic(
        schematic_path: str,
        reference: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove all placed symbol units with the given reference designator.

        Removes every ``(symbol ...)`` entry whose Reference property matches
        *reference* (case-sensitive).  Also removes the corresponding
        ``(lib_symbols ...)`` entry when no other placed symbol still uses
        that lib_id.  A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            reference: Reference designator to remove (e.g. "C1").

        Returns:
            dict with keys: success (bool), removed_units (int), warnings.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}

        try:
            sch = skip.Schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            # Collect units to remove and the lib_ids they use.
            to_remove = []
            removed_lib_ids: set[str] = set()
            try:
                for sym in sch.symbol:
                    try:
                        ref_val = sym.property.Reference.value
                    except AttributeError:
                        continue
                    if ref_val == reference:
                        to_remove.append(sym)
                        try:
                            removed_lib_ids.add(sym.lib_id.value)
                        except AttributeError:
                            pass
            except AttributeError:
                pass  # empty schematic

            if not to_remove:
                return {"error": f"No symbol with reference {reference!r} found"}

            for sym in to_remove:
                sym.delete()

            # Remove orphaned lib_symbols entries.
            warnings: list[str] = []
            remaining_lib_ids: set[str] = set()
            try:
                for sym in sch.symbol:
                    try:
                        remaining_lib_ids.add(sym.lib_id.value)
                    except AttributeError:
                        pass
            except AttributeError:
                pass

            for lib_id in removed_lib_ids:
                if lib_id not in remaining_lib_ids:
                    try:
                        del sch.lib_symbols[lib_id]
                    except Exception as exc:
                        warnings.append(f"Could not remove lib_symbol {lib_id!r}: {exc}")

            try:
                shutil.copy(schematic_path, schematic_path + ".bak")
                sch.write(schematic_path)
            except Exception as exc:
                return {"error": f"Failed to save schematic: {exc}"}

            return {
                "success": True,
                "removed_units": len(to_remove),
                "warnings": warnings,
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
            sch = skip.Schematic(schematic_path)
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
                                    child.append(
                                        [sexpdata.Symbol("hide"),
                                         sexpdata.Symbol("yes")]
                                    )
                                    break
                        added_count += 1
                    except Exception as exc:
                        return {"error": f"Failed to add property {property_name!r} on unit {sym.unit.value if hasattr(sym, 'unit') else '?'}: {exc}"}

            if added_count > 0 and updated_count > 0:
                action = "mixed"
            elif added_count > 0:
                action = "added"
            else:
                action = "updated"

            try:
                shutil.copy(schematic_path, schematic_path + ".bak")
                sch.write(schematic_path)
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
            }

        except Exception as exc:
            log.exception("Unexpected error in set_component_property")
            return {"error": str(exc), "success": False}
