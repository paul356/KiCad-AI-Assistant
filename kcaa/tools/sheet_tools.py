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


def _sexp_sheet_pin(
    name: str,
    edge: str,
    distance_mm: float,
    indent: int = 2,
) -> str:
    """Build a ``(pin ...)`` S-expression for a sheet pin.

    *edge* is one of ``"right"``, ``"left"``, ``"bottom"``, ``"top"``.
    *distance_mm* is the offset along that edge from its origin corner.
    """
    edge_to_rotation = {"right": 0, "left": 180, "bottom": 270, "top": 90}
    rot = edge_to_rotation.get(edge, 0)
    # Pin anchor is placed at the edge midpoint at the right distance
    return (
        f'{" " * indent}(pin "{name}"'
        f" (at {_align_to_grid(distance_mm):.2f} {_align_to_grid(0):.2f} {rot})"
        f' (uuid "{uuid.uuid4()}")'
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
    """Return *value* as a plain list regardless of whether it is a single
    ``ParsedValue``, an ``ElementCollection``, a ``list``, or ``None``."""
    if value is None:
        return []
    # ElementCollection has __getitem__ but not __iter__; for-loop works via
    # __getitem__, but isinstance/iter tests fail.  Try len() first.
    try:
        length = len(value)
    except TypeError:
        return [value]  # single SheetWrapper / ParsedValue
    # Iterate by index (ElementCollection doesn't have __iter__)
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
        try:
            sheet_name = props.Sheet_name.value if hasattr(props, "Sheet_name") else None
        except (AttributeError, KeyError):
            pass
        try:
            sheet_file = props.Sheet_file.value if hasattr(props, "Sheet_file") else None
        except (AttributeError, KeyError):
            pass
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
    raise NotImplementedError("Sheet creation will be implemented in Phase 2")


def _do_remove_sheet_symbol(
    schematic_path: str,
    sheet_identifier: str,
) -> dict[str, Any]:
    """Implementation of remove_sheet_symbol."""
    raise NotImplementedError("Sheet removal will be implemented in Phase 3")


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
    raise NotImplementedError("Sheet update will be implemented in Phase 3")


def _do_add_sheet_pin(
    schematic_path: str,
    sheet_identifier: str,
    pin_name: str,
    edge: str,
    distance_mm: float,
) -> dict[str, Any]:
    """Implementation of add_sheet_pin."""
    raise NotImplementedError("Sheet pin tools will be implemented in Phase 4")


def _do_remove_sheet_pin(
    schematic_path: str,
    sheet_identifier: str,
    pin_name: str,
) -> dict[str, Any]:
    """Implementation of remove_sheet_pin."""
    raise NotImplementedError("Sheet pin tools will be implemented in Phase 4")


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

    # ---- Create tools ----

    @mcp.tool()
    async def create_child_sheet(
        parent_path: str,
        child_filename: str,
        paper: str = "A4",
        title: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a minimal valid child ``.kicad_sch`` file for a hierarchical sheet.

        Does NOT modify the parent schematic — use ``add_sheet_symbol`` to
        add the corresponding sheet symbol after creating the child file.

        **File mutation** — writes a new ``.kicad_sch`` file to disk.

        Args:
            parent_path: Absolute path to the parent ``.kicad_sch`` file.
                Used to resolve the directory for *child_filename*.
            child_filename: Filename for the new schematic (e.g.
                ``"power-supply.kicad_sch"``).  ``.kicad_sch`` is appended
                if missing.  May be an absolute path.
            paper: Paper size name (default ``"A4"``).  Valid: A4, A3, A2,
                A5, A, B, C, D, E, USLetter, USLegal, USLedger.
            title: Optional title for the child sheet's title block.

        Returns:
            dict with keys:
                - ``success`` (bool)
                - ``child_path`` (str): absolute path to the created file
                - ``child_uuid`` (str): the root UUID of the new file
        """
        if not parent_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {parent_path!r}"}
        if not os.path.isfile(parent_path):
            return {"error": f"Schematic file not found: {parent_path!r}"}
        if paper not in _PAPER_SIZES:
            return {
                "error": f"Unknown paper size {paper!r}. Valid: {', '.join(sorted(_PAPER_SIZES))}"
            }
        try:
            child_path = _generate_child_schematic(
                parent_path, child_filename, paper=paper, title=title
            )
            # Read back the UUID for the response
            child_sch = safe_schematic(child_path)
            child_uuid = child_sch.uuid.value
            return {
                "success": True,
                "child_path": child_path,
                "child_uuid": child_uuid,
            }
        except FileExistsError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("Error creating child schematic")
            return {"error": f"Failed to create child schematic: {e}"}

    # ---- CUD tools (stubs — implemented in later phases) ----

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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical sheet symbol to a schematic. **(not yet implemented)**

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

        Returns:
            dict with keys: success, sheet_uuid, position, size, pins_created,
            child_path (if create_child was True).
        """
        raise NotImplementedError(
            "Sheet creation will be implemented in Phase 2 (sheet-create-tool). "
            "Use create_child_sheet to create child files for now."
        )

    @mcp.tool()
    async def remove_sheet_symbol(
        schematic_path: str,
        sheet_identifier: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove a sheet symbol from a schematic. **(not yet implemented)**

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the sheet symbol to remove.

        Returns:
            dict with keys: success, removed_uuid, removed_name, orphaned_files.
        """
        raise NotImplementedError(
            "Sheet removal will be implemented in Phase 3 (sheet-remove-tool)."
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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a sheet symbol's properties. **(not yet implemented)**

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the sheet symbol to update.
            sheet_name: New display name (optional).
            sheet_file: New child file reference (optional).
            x: New X position in mm (optional).
            y: New Y position in mm (optional).
            width: New width in mm (optional).
            height: New height in mm (optional).

        Returns:
            dict with keys: success, uuid, updated_fields.
        """
        raise NotImplementedError(
            "Sheet update will be implemented in Phase 3 (sheet-update-tool)."
        )

    @mcp.tool()
    async def add_sheet_pin(
        schematic_path: str,
        sheet_identifier: str,
        pin_name: str,
        edge: str,
        distance_mm: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical pin to a sheet symbol. **(not yet implemented)**

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the target sheet symbol.
            pin_name: Name for the new pin (e.g. "VCC", "GND").
            edge: One of ``"right"``, ``"left"``, ``"bottom"``, ``"top"``.
            distance_mm: Distance along the edge from the origin corner.

        Returns:
            dict with keys: success, pin_uuid, pin_name, edge, distance_mm.
        """
        raise NotImplementedError(
            "Sheet pin tools will be implemented in Phase 4 (sheet-pin-tools)."
        )

    @mcp.tool()
    async def remove_sheet_pin(
        schematic_path: str,
        sheet_identifier: str,
        pin_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove a hierarchical pin from a sheet symbol. **(not yet implemented)**

        Args:
            schematic_path: Absolute path to the target ``.kicad_sch`` file.
            sheet_identifier: UUID or sheet name of the target sheet symbol.
            pin_name: Name of the pin to remove.

        Returns:
            dict with keys: success, removed_pin_name.
        """
        raise NotImplementedError(
            "Sheet pin tools will be implemented in Phase 4 (sheet-pin-tools)."
        )
