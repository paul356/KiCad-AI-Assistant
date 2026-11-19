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

from kcaa.utils.atomic_io import read_text_utf8
from kcaa.utils.file_utils import load_project_json
from kcaa.utils.project_resolution import find_project_file, project_name
from kcaa.utils.schematic_sexp_utils import write_schematic_text
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


def _collect_occupied_bboxes(
    schematic_path: str,
    exclude_uuid: str | None = None,
    margin: float = 3.81,
    exclude_refs: set[str] | None = None,
) -> list:
    """Collect occupied bounding boxes for overlap detection.

    Gathers sheet symbols, symbol components, and the title block,
    inflating each by *margin* for clearance.  When *exclude_uuid*
    is provided, the sheet with that UUID is skipped.
    When *exclude_refs* is provided, components with those
    references are skipped.
    """
    from kcaa.tools.placement_helpers import (
        _default_title_block_bbox,
        _parse_paper_size,
        _sheet_symbol_bbox,
    )
    from kcaa.utils.netlist_parser import component_body_bbox, extract_netlist
    from kcaa.utils.symbol_geometry import BBox, inflate_bbox

    occupied: list = []
    _exclude_refs = exclude_refs or set()

    # Sheet symbols.
    sheet_info = _list_sheet_symbols_impl(schematic_path)
    for sheet in sheet_info.get("sheets", []):
        if exclude_uuid and sheet.get("uuid") == exclude_uuid:
            continue
        bb = _sheet_symbol_bbox(sheet)
        if bb is not None:
            occupied.append(inflate_bbox(bb, margin))

    # Symbol components (skip type="sheet" — already covered above).
    netlist = extract_netlist(schematic_path)
    for ref, comp in (netlist.get("components", {}) or {}).items():
        if ref in _exclude_refs:
            continue
        if comp.get("type") == "sheet":
            continue
        bb_d = component_body_bbox(comp)
        if not bb_d:
            continue
        try:
            bb = BBox(
                float(bb_d["min_x"]),
                float(bb_d["min_y"]),
                float(bb_d["max_x"]),
                float(bb_d["max_y"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        occupied.append(inflate_bbox(bb, margin))

    # Title block.
    _, sheet_w, sheet_h, _ = _parse_paper_size(schematic_path)
    tb = _default_title_block_bbox(sheet_w, sheet_h)
    occupied.append(tb)

    return occupied


def _has_position_conflict(
    schematic_path: str,
    x: float,
    y: float,
    w: float,
    h: float,
    exclude_uuid: str | None = None,
    margin: float = 3.81,
    exclude_refs: set[str] | None = None,
) -> bool:
    """Return True if a bbox at (x, y, w, h) overlaps any occupied area."""
    from kcaa.utils.symbol_geometry import BBox, bboxes_overlap

    cand = BBox(x, y, x + w, y + h)
    occupied = _collect_occupied_bboxes(schematic_path, exclude_uuid, margin, exclude_refs)
    conflicts = [occ for occ in occupied if bboxes_overlap(cand, occ)]
    if conflicts:
        log.info(
            "_has_position_conflict: bbox(%s, %s, %sx%s) conflicts with %d areas: %s",
            x,
            y,
            w,
            h,
            len(conflicts),
            [(occ.min_x, occ.min_y, occ.max_x, occ.max_y) for occ in conflicts],
        )
    return bool(conflicts)


# ---------------------------------------------------------------------------
# S-expression helpers for canonical sheet data
# ---------------------------------------------------------------------------

# Top-level sections KiCad writes after all schematic items.  Root schematics
# carry both; child schematics carry neither, so new items must be inserted
# before them rather than appended.
_ROOT_ONLY_SECTIONS = frozenset({"sheet_instances", "embedded_fonts"})


def _tag_of(node: Any) -> str | None:
    """Return the leading symbol of an S-expression node, if it has one."""
    import sexpdata

    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return node[0].value()
    return None


def _property_key(name: Any) -> str:
    """Normalise a sheet property name: ``Sheetname`` and ``Sheet name`` match."""
    if not isinstance(name, str):
        return ""
    return name.replace(" ", "").replace("_", "").lower()


def _load_schematic_tree(path: str) -> list:
    """Parse *path* into its raw ``sexpdata`` tree."""
    import sexpdata

    return sexpdata.loads(read_text_utf8(path))


def _try_load_schematic_tree(path: str) -> list | None:
    """Parse *path*, returning ``None`` when the file is unusable.

    Best-effort variant of :func:`_load_schematic_tree` for child sheets an
    unrelated tool may have left missing or truncated.  ``OSError`` covers
    missing/unreadable files, ``ValueError`` covers undecodable bytes
    (``UnicodeDecodeError``), and the ``sexpdata`` parser reports unbalanced
    or trailing input with its own exception types.
    """
    import sexpdata

    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        if not content.strip():
            return None
        return sexpdata.loads(content)
    except (
        OSError,
        ValueError,
        AssertionError,  # sexpdata.loads asserts on empty parses
        RecursionError,  # sexpdata recurses once per nesting level
        sexpdata.ExpectClosingBracket,
        sexpdata.ExpectNothing,
        sexpdata.ExpectSExp,
    ) as exc:
        log.debug("Skipping unreadable child schematic %s: %s", path, exc)
        return None


def _is_root_schematic(tree: list) -> bool:
    """Return True when *tree* belongs to a root schematic.

    Only the project root records ``(sheet_instances (path "/" ...))``.
    Children never carry that section in KiCad 10, but older kcaa releases
    wrote ``(sheet_instances (path "/<file uuid>" ...))`` into every child,
    so the root path itself must be checked rather than the section alone.
    """
    for node in tree:
        if _tag_of(node) != "sheet_instances":
            continue
        for entry in node[1:]:
            if _tag_of(entry) == "path" and len(entry) > 1 and entry[1] == "/":
                return True
    return False


def _top_level_string(tree: list, tag: str) -> str | None:
    """Return the string value of the first top-level ``(tag "...")`` node."""
    for node in tree:
        if _tag_of(node) == tag and len(node) > 1 and isinstance(node[1], str):
            return node[1]
    return None


def _first_instance_path(sheet_node: list, project_name: str) -> str | None:
    """Return the ``instances`` path recorded on a sheet node.

    Prefers the ``project`` clause named *project_name* (the schematic stem),
    since a file may carry clauses for several projects.  When no clause
    matches, the first path in any clause is returned: the file itself does
    not identify which clause belongs to it.
    """
    fallback: str | None = None
    for child in sheet_node:
        if _tag_of(child) != "instances":
            continue
        for project in child[1:]:
            if _tag_of(project) != "project":
                continue
            for entry in project[1:]:
                if _tag_of(entry) == "path" and len(entry) > 1 and isinstance(entry[1], str):
                    if len(project) > 1 and project[1] == project_name:
                        return entry[1]
                    if fallback is None:
                        fallback = entry[1]
    return fallback


def _sheet_file_from_node(sheet_node: list) -> str | None:
    """Return the ``Sheetfile`` value of a sheet node (either spelling)."""
    for child in sheet_node:
        if (
            _tag_of(child) == "property"
            and len(child) > 2
            and _property_key(child[1]) == "sheetfile"
        ):
            return child[2] if isinstance(child[2], str) else None
    return None


def _project_root_sheet_uuid(schematic_path: str) -> tuple[str | None, str | None]:
    """Return ``(root_sheet_uuid, error)`` for *schematic_path*.

    KiCad stores the project root sheet UUID in the project file
    (``schematic.top_level_sheets[].uuid``), not in the schematic.  The
    ``(uuid ...)`` at the top of a ``.kicad_sch`` is the screen/file UUID and
    may legitimately differ from the root sheet UUID (which is what every
    instance path in the hierarchy starts with).

    ``(None, None)`` means no owning project file exists: the schematic is a
    standalone document and the caller may fall back to its own UUID.  When
    a project file exists but does not confirm this exact file (invalid JSON,
    missing or empty ``top_level_sheets``, no matching entry, a matching
    entry whose UUID is not a non-blank string without whitespace, or
    several distinct matching UUIDs), the error names the file and the
    reason, and the caller must write nothing.  A matched UUID is rejected
    in three separate cases: it is blank after stripping, it differs from
    its stripped form, or it contains whitespace anywhere.  The value is
    never silently stripped.

    Entries are matched by real path (with ``normcase`` for Windows): each
    ``filename`` is resolved relative to the project directory, so files in
    other directories with the same basename never claim each other.
    """
    project_file = find_project_file(schematic_path)
    if not project_file:
        return None, None

    def fail(reason: str) -> tuple[None, str]:
        return None, (
            f"Cannot resolve the root sheet UUID for {os.path.basename(schematic_path)!r}: "
            f"{reason}.  Nothing was written."
        )

    data = load_project_json(project_file)
    if not isinstance(data, dict):
        return fail(
            f"the project file {os.path.basename(project_file)!r} could not be read "
            "or is not valid JSON"
        )

    schematic = data.get("schematic")
    if not isinstance(schematic, dict):
        return fail(
            f"the project file {os.path.basename(project_file)!r} has no 'schematic' object"
        )

    sheets = schematic.get("top_level_sheets")
    if not isinstance(sheets, list) or not sheets:
        return fail(
            f"the project file {os.path.basename(project_file)!r} has no "
            "'schematic.top_level_sheets' entries"
        )

    project_dir = os.path.dirname(project_file)
    # normcase is identity on POSIX; on Windows it folds case and separators,
    # so a project entry that differs only in letter case still matches.
    target = os.path.normcase(os.path.realpath(schematic_path))
    matches: list[str] = []
    for entry in sheets:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str):
            continue
        entry_path = os.path.normcase(os.path.realpath(os.path.join(project_dir, filename)))
        if entry_path != target:
            continue
        entry_uuid = entry.get("uuid")
        if (
            not isinstance(entry_uuid, str)
            or not entry_uuid.strip()
            or entry_uuid != entry_uuid.strip()
            or any(ch.isspace() for ch in entry_uuid)
        ):
            return fail(
                f"its {os.path.basename(project_file)!r} 'schematic.top_level_sheets' "
                "entry has no string 'uuid', or its 'uuid' is blank or contains whitespace"
            )
        matches.append(entry_uuid)

    if not matches:
        return fail(
            f"the project file {os.path.basename(project_file)!r} does not list this "
            "schematic in 'schematic.top_level_sheets'"
        )

    if len(set(matches)) > 1:
        return fail(
            f"the project file {os.path.basename(project_file)!r} lists more than one "
            "distinct root sheet UUID for this schematic"
        )

    return matches[0], None


def _resolve_sheet_parent_path(schematic_path: str, tree: list) -> tuple[str | None, str | None]:
    """Resolve the instance path of the sheet that owns *schematic_path*.

    For a root schematic this is ``"/" + <root sheet UUID>``.  The UUID is
    taken from the owning project's ``top_level_sheets`` entry for this exact
    file, because that is the value KiCad writes into every instance path.
    Only a standalone schematic (no project file at all) falls back to the
    file's own ``(uuid ...)``, then to an existing sibling sheet's
    ``instances`` path (preferring the clause for this schematic's stem).  A
    project file that exists but cannot confirm this schematic fails the
    call instead of guessing.

    KiCad always writes ``(uuid ...)`` into a saved schematic and assigns one
    on load when it is missing, so a root with neither its own UUID nor an
    existing sheet instance path is not a state KiCad produces.

    Returns ``(path, error)``; exactly one of the two is set.  A child
    schematic has no record of its enclosing sheet path, so it is rejected
    instead of emitting a guessed path.
    """
    if not _is_root_schematic(tree):
        return None, (
            f"Cannot add a sheet to {os.path.basename(schematic_path)!r}: it is not the "
            "project root (its (sheet_instances) section does not describe the '/' root "
            "path), so the enclosing sheet path is unknown.  Add the sheet to the "
            "project's root schematic instead."
        )

    project_uuid, project_error = _project_root_sheet_uuid(schematic_path)
    if project_error:
        return None, project_error
    # The tri-state, not truthiness, decides: only (None, None) — no project
    # file at all — reaches the standalone fallback.
    if project_uuid is not None:
        return "/" + project_uuid, None

    root_uuid = _top_level_string(tree, "uuid")
    if root_uuid:
        return "/" + root_uuid, None

    stem = os.path.splitext(os.path.basename(schematic_path))[0]
    for node in tree:
        if _tag_of(node) == "sheet":
            instance_path = _first_instance_path(node, stem)
            if instance_path:
                return instance_path, None

    return None, (
        f"Cannot resolve the root sheet UUID for {os.path.basename(schematic_path)!r}: "
        "the file has no (uuid ...) and no existing sheet instances, so the enclosing "
        "sheet path cannot be determined.  Nothing was written."
    )


def _collect_page_numbers(node: Any, pages: set[int]) -> None:
    """Add every integer ``(page "...")`` value under *node* to *pages*."""
    if not isinstance(node, list):
        return
    tag = _tag_of(node)
    if tag == "page":
        for value in node[1:]:
            try:
                pages.add(int(value))
            except (TypeError, ValueError):
                continue
        return
    for child in node[1:] if tag else node:
        _collect_page_numbers(child, pages)


def _collect_used_page_numbers(
    schematic_path: str,
    root_tree: list | None = None,
    max_depth: int = 64,
) -> set[int]:
    """Collect page numbers already used anywhere in the sheet hierarchy.

    Reads the root schematic plus every sheet schematic it references,
    recursively.  Unreadable children are ignored (best effort).
    """
    pages: set[int] = set()
    visited: set[str] = set()

    def walk(path: str, tree: list, depth: int) -> None:
        real = os.path.realpath(path)
        if real in visited:
            return
        visited.add(real)
        _collect_page_numbers(tree, pages)
        if depth >= max_depth:
            return
        base_dir = os.path.dirname(os.path.abspath(path))
        for node in tree:
            if _tag_of(node) != "sheet":
                continue
            child_file = _sheet_file_from_node(node)
            if not child_file:
                continue
            child_path = (
                child_file if os.path.isabs(child_file) else os.path.join(base_dir, child_file)
            )
            child_tree = _try_load_schematic_tree(child_path)
            if child_tree is None:
                continue
            walk(child_path, child_tree, depth + 1)

    if root_tree is None:
        try:
            root_tree = _load_schematic_tree(schematic_path)
        except Exception:  # caller reports parse errors elsewhere
            return pages
    walk(schematic_path, root_tree, 0)
    return pages


def _next_page_number(schematic_path: str, root_tree: list | None = None) -> int:
    """Return the lowest positive page number unused in the project."""
    used = _collect_used_page_numbers(schematic_path, root_tree)
    page = 1
    while page in used:
        page += 1
    return page


def _insert_schematic_item(tree: list, entry: list) -> None:
    """Insert *entry* with the other schematic items.

    KiCad writes ``sheet_instances`` and ``embedded_fonts`` last; appending
    after them is not canonical.
    """
    import copy

    index = len(tree)
    for i, node in enumerate(tree):
        if _tag_of(node) in _ROOT_ONLY_SECTIONS:
            index = i
            break
    tree.insert(index, copy.deepcopy(entry))


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
    *child_filename* is absolute).  It contains the boilerplate KiCad expects
    from a child schematic: ``(kicad_sch ...)`` header, ``(uuid ...)``,
    ``(paper ...)``, optional ``title_block`` and an empty ``(lib_symbols)``.

    ``sheet_instances`` and ``embedded_fonts`` are root-file-only sections and
    are deliberately omitted; KiCad drops them from child files on save.

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
        )

    A file written as a project root would instead end with
    ``(sheet_instances (path "/" (page "1")))`` and ``(embedded_fonts no)``;
    that form is not emitted here.
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

    file_uuid = str(uuid.uuid4())

    # Build S-expression lines.  Child schematics stop at (lib_symbols):
    # sheet_instances/embedded_fonts belong to the project's root file only.
    lines: list[str] = []
    lines.append('(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")')
    lines.append(f'  (uuid "{file_uuid}")')
    lines.append(f'  (paper "{paper}")')

    if title:
        lines.append("  (title_block")
        lines.append(f'    (title "{title}")')
        lines.append("  )")

    lines.append("  (lib_symbols)")
    lines.append(")")

    write_schematic_text(child_path, "\n".join(lines) + "\n")

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
            normalized_name = _property_key(prop_name)
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


def _build_sheet_tree(root_path: str, max_depth: int = 10) -> dict[str, Any]:
    """Walk the hierarchical sheet tree rooted at *root_path*.

    Shared by ``get_sheet_hierarchy`` and ``get_project_structure.sheets``
    so both tools parse schematic hierarchy through the same
    implementation.  Each node carries ``file``, optional ``sheet_name``,
    ``sheet_count`` (number of direct children), and ``children`` only
    when the sheet has sub-sheets.  Cycle, depth-cut, missing-file, and
    parse-failure nodes carry ``cycle_detected`` / ``max_depth_reached``
    / ``error`` markers.
    """
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

        node["sheet_count"] = len(children)
        if children:
            node["children"] = children
        return node

    root = _walk(root_path, 0, None)
    # Remove internal tracking
    _visited.clear()

    return root


def _get_sheet_hierarchy_impl(
    schematic_path: str,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Implementation of get_sheet_hierarchy — recursive tree walk."""
    if not schematic_path.endswith(".kicad_sch"):
        return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
    if not os.path.isfile(schematic_path):
        return {"error": f"Schematic file not found: {schematic_path!r}"}

    return {
        "root_schematic": schematic_path,
        "hierarchy": _build_sheet_tree(schematic_path, max_depth),
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

    # Resolve canonical instance data before touching the filesystem, so a
    # failure leaves neither a modified schematic nor an orphan child file.
    sch = safe_schematic(schematic_path)
    parent_path, path_error = _resolve_sheet_parent_path(schematic_path, sch.tree)
    if path_error:
        return {"error": path_error}
    page_number = _next_page_number(schematic_path, sch.tree)

    # Optionally create child file
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

    # Snap coordinates to grid.  The grid product is rounded again so the
    # emitted numbers carry no float noise (KiCad writes at most 6 decimals).
    x = round(_align_to_grid(x), 6)
    y = round(_align_to_grid(y), 6)
    width = round(_align_to_grid(width), 6)
    height = round(_align_to_grid(height), 6)

    # Generate UUID for the sheet symbol
    sheet_uuid = str(uuid.uuid4())

    # Build the (sheet ...) S-expression as a nested list, in the order and
    # dialect KiCad 10 writes it.
    sheet_entry: list = [
        sexpdata.Symbol("sheet"),
        [sexpdata.Symbol("at"), x, y],
        [sexpdata.Symbol("size"), width, height],
        [sexpdata.Symbol("exclude_from_sim"), sexpdata.Symbol("no")],
        [sexpdata.Symbol("in_bom"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("on_board"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("dnp"), sexpdata.Symbol("no")],
        [
            sexpdata.Symbol("stroke"),
            [sexpdata.Symbol("width"), 0],
            [sexpdata.Symbol("type"), sexpdata.Symbol("solid")],
        ],
        [
            sexpdata.Symbol("fill"),
            [sexpdata.Symbol("color"), 0, 0, 0, 0],
        ],
        [sexpdata.Symbol("uuid"), sheet_uuid],
        [
            sexpdata.Symbol("property"),
            "Sheetname",
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
            "Sheetfile",
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
            pin_x = round(pin_x, 6)
            pin_y = round(pin_y, 6)

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
    project_stem = (
        project_name(schematic_path) or os.path.splitext(os.path.basename(schematic_path))[0]
    )
    instances_entry = [
        sexpdata.Symbol("instances"),
        [
            sexpdata.Symbol("project"),
            project_stem,
            [
                sexpdata.Symbol("path"),
                parent_path,
                [sexpdata.Symbol("page"), str(page_number)],
            ],
        ],
    ]
    sheet_entry.append(instances_entry)

    # Insert with the other schematic items, before the root-only sections.
    _insert_schematic_item(sch.tree, sheet_entry)

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
        old_x = target_sheet.get("position", {}).get("x", 0)
        old_y = target_sheet.get("position", {}).get("y", 0)
        new_x = round(_align_to_grid(x), 6) if x is not None else old_x
        new_y = round(_align_to_grid(y), 6) if y is not None else old_y
        dx = new_x - old_x
        dy = new_y - old_y

        # Find and update the (at ...) entry for the sheet box
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "at":
                    child[1] = new_x
                    child[2] = new_y
                    final_position = {"x": new_x, "y": new_y}
                    updated_fields.append("position")
                    break

        # Also update all pin (at ...) coordinates by the same delta,
        # because pin positions are stored as absolute schematic coordinates.
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "pin":
                    for grandchild in child:
                        if isinstance(grandchild, list) and len(grandchild) >= 3:
                            if (
                                isinstance(grandchild[0], sexpdata.Symbol)
                                and grandchild[0].value() == "at"
                            ):
                                grandchild[1] = round(grandchild[1] + dx, 6)
                                grandchild[2] = round(grandchild[2] + dy, 6)
                                break

    # Update size if provided
    if width is not None or height is not None:
        new_width = (
            round(_align_to_grid(width), 6)
            if width is not None
            else target_sheet.get("size", {}).get("width", 0)
        )
        new_height = (
            round(_align_to_grid(height), 6)
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
                    if len(child) > 2 and _property_key(child[1]) == "sheetname":
                        child[2] = sheet_name
                        final_sheet_name = sheet_name
                        updated_fields.append("sheet_name")
                        break

    # Update sheet file if provided
    if sheet_file is not None:
        for child in sheet_entry:
            if isinstance(child, list) and len(child) > 0:
                if isinstance(child[0], sexpdata.Symbol) and child[0].value() == "property":
                    if len(child) > 2 and _property_key(child[1]) == "sheetfile":
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

    # KiCad writes at most 6 decimals; raw float sums would otherwise leak
    # noise such as 182.88000000000002 into the file.
    pin_x = round(pin_x, 6)
    pin_y = round(pin_y, 6)

    # Generate UUID for the pin
    pin_uuid = str(uuid.uuid4())

    # right/top edges: text reads inward → justify right
    # left/bottom edges: text reads inward → justify left
    pin_justify = "right" if edge in ("right", "top") else "left"

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
            [sexpdata.Symbol("justify"), sexpdata.Symbol(pin_justify)],
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
                - ``hierarchy`` (dict): tree node with ``file``, optional
                  ``sheet_name``, ``sheet_count`` (number of direct
                  children), and ``children`` — omitted when the sheet has
                  no sub-sheets.  Each child node also carries ``file``
                  and ``sheet_name`` when present.
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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical sheet symbol to a schematic.

        The sheet's node structure, property names, flags, fill and stroke
        match KiCad 10 (``Sheetname`` / ``Sheetfile`` properties,
        ``(fill (color 0 0 0 0))``, four
        ``exclude_from_sim``/``in_bom``/``on_board``/``dnp`` flags and a
        solid stroke), placed before any ``sheet_instances`` section.  Its
        ``instances`` path is the containing sheet's path (``/<root uuid>``
        for a sheet added to the project root) and its page is the lowest
        unused page number in the project.

        Only root schematics can be extended: a child schematic has no record
        of its enclosing sheet path, so the call fails rather than writing a
        guessed instance path.

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
            child_path (if create_child was True), and ``position_adjusted``
            when auto-placement changes the requested location.
        """
        requested_position = {"x": x, "y": y}
        snapped_x = _align_to_grid(x)
        snapped_y = _align_to_grid(y)
        eff_w = _align_to_grid(width)
        eff_h = _align_to_grid(height)
        place_x = snapped_x
        place_y = snapped_y
        position_adjusted = False

        if _has_position_conflict(schematic_path, snapped_x, snapped_y, eff_w, eff_h):
            log.info(
                "add_sheet_symbol: target (%s, %s) %sx%s conflicts, searching free area",
                snapped_x,
                snapped_y,
                eff_w,
                eff_h,
            )
            from kcaa.tools.placement_helpers import PlacementHelpers

            free_area = PlacementHelpers.find_free_area(
                schematic_path=schematic_path,
                width=eff_w,
                height=eff_h,
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
                    log.info(
                        "add_sheet_symbol: adjusted to nearest free (%s, %s) "
                        "from requested (%s, %s)",
                        place_x,
                        place_y,
                        snapped_x,
                        snapped_y,
                    )
                else:
                    log.info(
                        "add_sheet_symbol: nearest free is same as target (%s, %s)",
                        snapped_x,
                        snapped_y,
                    )

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

        Returns:
            dict with keys: success, uuid, updated_fields, and optionally
            ``position_adjusted`` / ``requested_position`` / ``note`` when
            auto-placement changes the requested location.
        """
        place_x = x
        place_y = y
        position_adjusted = False
        requested_position: dict[str, Any] | None = None

        if x is not None or y is not None:
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

                log.info(
                    "update_sheet_symbol: sheet=%s uuid=%s requested=(%s,%s) "
                    "current=(%s,%s) size=%sx%s",
                    sheet_identifier,
                    sheet_uuid,
                    req_x,
                    req_y,
                    cur_x,
                    cur_y,
                    eff_w,
                    eff_h,
                )

                if _has_position_conflict(
                    schematic_path, req_x, req_y, eff_w, eff_h, exclude_uuid=sheet_uuid
                ):
                    from kcaa.tools.placement_helpers import PlacementHelpers

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
                    log.info(
                        "update_sheet_symbol: find_free_area returned %d candidates, "
                        "picked origin=%s",
                        len(free_area.get("candidates") or []),
                        origin,
                    )
                    if origin is not None:
                        cand_x = float(origin["x"])
                        cand_y = float(origin["y"])
                        # Lock axes that were not explicitly specified by the caller.
                        if x is None:
                            cand_x = cur_x
                        if y is None:
                            cand_y = cur_y
                        requested_position = {"x": x, "y": y}
                        place_x = cand_x
                        place_y = cand_y
                        position_adjusted = True
                        log.info(
                            "update_sheet_symbol: adjusted to nearest free (%s, %s) "
                            "from requested (%s, %s)",
                            place_x,
                            place_y,
                            req_x,
                            req_y,
                        )
                    else:
                        log.info(
                            "update_sheet_symbol: nearest free is same as target (%s, %s)",
                            req_x,
                            req_y,
                        )

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
