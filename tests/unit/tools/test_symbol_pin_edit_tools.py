"""Tests for the pin-number / pin-name editing surface (issue #266).

Covers ``kcaa.utils.symbol_library_utils.set_symbol_pin`` (batch API) plus the
``update_library_symbol_pin`` and ``update_schematic_symbol_pin`` MCP tools.
All writes happen on temporary copies of the fixtures so the originals stay
untouched.  Library edits promise byte preservation; schematic edits are
checked semantically because the normal save path re-serialises the file.
"""

import asyncio
from pathlib import Path
import shutil
import stat

import pytest
import sexpdata

from kcaa.utils.netlist_parser import SchematicParser, analyze_netlist
from kcaa.utils.symbol_library_utils import set_symbol_pin

FIXTURES = Path(__file__).parent / "fixtures"
SYMBOL_LIBRARY = FIXTURES / "pin_edit_symbols.kicad_sym"
SCHEMATIC = FIXTURES / "pin_edit.kicad_sch"
MULTI_UNIT = Path(__file__).parent.parent / "utils/fixtures/multi_unit.kicad_sch"
SINGLE_PIN = Path(__file__).parent.parent / "utils/fixtures/single_pin_symbol.kicad_sch"

_TOOL_LIB = "pinlib"

BAD_LIST = "pins must be a non-empty list"
BAD_TOO_MANY = "pins must contain at most 256 entries"
BAD_ENTRY = (
    "each pins entry must be an object with pin_number and at least one of new_number/new_name"
)
BOTH_NONE = "both new_number and new_name are None; nothing to change"
BAD_NUMBER = (
    "new_number must be a non-empty token without whitespace, quotes, "
    "parentheses, backslashes or control characters (max 32 characters)"
)
BAD_NAME = "new_name must not contain quotes, parentheses, backslashes or control characters"
BAD_SELECTOR = "pin_number must be a non-empty string"


class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _tools() -> dict:
    from kcaa.tools.symbol_edit_tools import register_symbol_edit_tools

    mock = _MockMCP()
    register_symbol_edit_tools(mock)
    return mock.tools


def _copy(source: Path, tmp_path: Path, name: str | None = None) -> Path:
    target = tmp_path / (name or source.name)
    shutil.copy2(source, target)
    return target


def _edit(pin_number: str, new_number: str | None = None, new_name: str | None = None) -> dict:
    entry: dict = {"pin_number": pin_number}
    if new_number is not None:
        entry["new_number"] = new_number
    if new_name is not None:
        entry["new_name"] = new_name
    return entry


def _patch_library(monkeypatch, library_path: Path):
    """Patch library resolution + system-path detection for the library tool."""
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools.resolve_symbol_library",
        lambda *args, **kwargs: {
            "path": str(library_path),
            "table_path": str(library_path.parent / "sym-lib-table"),
        },
    )
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools.config.is_system_library_path",
        lambda path: False,
    )


_PROJECT_LOCAL_LIB = "pinlib_local"


def _make_project_local_library(tmp_path) -> tuple[Path, Path]:
    """Project dir whose sym-lib-table registers one ``${KIPRJMOD}`` library."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    library = project_dir / "pin_edit_local.kicad_sym"
    shutil.copy2(SYMBOL_LIBRARY, library)
    (project_dir / "sym-lib-table").write_text(
        "(sym_lib_table\n"
        f'  (lib (name "{_PROJECT_LOCAL_LIB}")(type "KiCad")'
        '(uri "${KIPRJMOD}/pin_edit_local.kicad_sym")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    return project_dir, library


def _add_common_pin(schematic: Path) -> None:
    """Give ``myLib:DUAL`` a common-unit pin present on both unit placements.

    The shared ``multi_unit.kicad_sch`` fixture has no common-unit sub-symbol,
    so the copy is extended in place: a ``DUAL_0_1`` pin ``C0`` plus the
    matching instance pin key on the unit-1 and unit-2 placements.
    """
    text = schematic.read_text(encoding="utf-8")
    text = text.replace(
        '(embedded_fonts no) (symbol "DUAL_1_1"',
        '(embedded_fonts no) (symbol "DUAL_0_1"'
        " (pin passive line (at 0 0 0) (length 0.762)"
        ' (name "COM" (effects (font (size 1.27 1.27))))'
        ' (number "C0" (effects (font (size 1.27 1.27)))))) (symbol "DUAL_1_1"',
        1,
    )
    text = text.replace(
        '(pin "2" (uuid "33333333-3333-3333-3333-333333333332"))',
        '(pin "C0" (uuid "66666666-6666-6666-6666-666666666660"))'
        ' (pin "2" (uuid "33333333-3333-3333-3333-333333333332"))',
        1,
    )
    text = text.replace(
        '(pin "4" (uuid "55555555-5555-5555-5555-555555555554"))',
        '(pin "C0" (uuid "66666666-6666-6666-6666-666666666661"))'
        ' (pin "4" (uuid "55555555-5555-5555-5555-555555555554"))',
        1,
    )
    schematic.write_text(text, encoding="utf-8")


def _make_bare_id_schematic(schematic: Path, qualified: str, bare: str) -> None:
    """Rewrite a cached id — and its placements — to the bare stored form."""
    text = schematic.read_text(encoding="utf-8")
    text = text.replace(f'(symbol "{qualified}"', f'(symbol "{bare}"')
    text = text.replace(f'(lib_id "{qualified}")', f'(lib_id "{bare}")')
    schematic.write_text(text, encoding="utf-8")


_CACHED_SYMBOL_TEMPLATE = (
    '(symbol "{lib_id}" (pin_numbers (hide yes)) (pin_names (offset 0.254))'
    " (exclude_from_sim no) (in_bom yes) (on_board yes)"
    " (duplicate_pin_numbers_are_jumpers no)"
    ' (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))'
    ' (property "Value" "X" (at 0 0 0) (effects (font (size 1.27 1.27))))'
    ' (symbol "Extra_1_1" (pin passive line (at 0 0 0) (length 2.54)'
    ' (name "A" (effects (font (size 1.27 1.27))))'
    ' (number "1" (effects (font (size 1.27 1.27)))))))'
)


def _add_cached_symbols(schematic: Path, lib_ids: list[str]) -> None:
    """Prepend minimal ``lib_symbols`` entries for *lib_ids*."""
    nodes = "".join(_CACHED_SYMBOL_TEMPLATE.format(lib_id=lib_id) for lib_id in lib_ids)
    text = schematic.read_text(encoding="utf-8")
    schematic.write_text(
        text.replace("(lib_symbols ", f"(lib_symbols {nodes}", 1), encoding="utf-8"
    )


def _add_stacked_pin(schematic: Path) -> None:
    """Give the cached ``PinEdit:Plain`` a second pin sharing number ``P$1``.

    KiCad stacked pins sit on top of each other (duplicate numbers are
    jumpers).  The instance pin key is added to both placements so the key
    rewrite for a shared number becomes observable.
    """
    text = schematic.read_text(encoding="utf-8")
    text = text.replace(
        '(number "P$2" (effects (font (size 1.27 1.27))))))',
        '(number "P$2" (effects (font (size 1.27 1.27)))))'
        " (pin passive line (at -5.08 0 0) (length 2.54)"
        ' (name "A2" (effects (font (size 1.27 1.27))))'
        ' (number "P$1" (effects (font (size 1.27 1.27))))))',
        1,
    )
    for uuid, stacked_uuid in (
        ("e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44", "7b1c2d3e-4f50-4617-8829-3a4b5c6d7e01"),
        ("1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5a66", "7b1c2d3e-4f50-4617-8829-3a4b5c6d7e02"),
    ):
        text = text.replace(
            f'(pin "P$1" (uuid "{uuid}"))',
            f'(pin "P$1" (uuid "{uuid}")) (pin "P$1" (uuid "{stacked_uuid}"))',
            1,
        )
    schematic.write_text(text, encoding="utf-8")


def _patch_real_resolution(monkeypatch):
    """Keep real resolution hermetic: no host global tables, no system paths."""
    monkeypatch.setattr(
        "kcaa.utils.symbol_library_utils._default_kicad_config_dirs",
        lambda: [],
    )
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools.config.is_system_library_path",
        lambda path: False,
    )


def _keyword(node) -> str:
    return str(node[0]) if isinstance(node, list) and node else ""


def _child_value(node, keyword: str):
    for child in node[1:]:
        if _keyword(child) == keyword and len(child) > 1:
            return str(child[1])
    return None


def _child_node(node, keyword: str):
    for child in node[1:]:
        if _keyword(child) == keyword:
            return child
    return None


def _lib_pin_pairs(sub_symbol) -> tuple[tuple[str, str], ...]:
    pairs = []
    for child in sub_symbol[1:]:
        if _keyword(child) != "pin":
            continue
        values = {}
        for part in child[2:]:
            key = _keyword(part)
            if key in ("name", "number") and len(part) > 1:
                values[key] = str(part[1])
        pairs.append((values.get("number", ""), values.get("name", "")))
    return tuple(pairs)


def _symbol_pin_numbers(path: Path, symbol_name: str) -> set[str]:
    """Return the pin numbers of one top-level symbol in a library file."""
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    for node in data[1:]:
        if _keyword(node) == "symbol" and len(node) > 1 and str(node[1]) == symbol_name:
            numbers: set[str] = set()
            for sub in node[2:]:
                if _keyword(sub) == "symbol":
                    numbers.update(number for number, _ in _lib_pin_pairs(sub))
            return numbers
    return set()


def _semantic_snapshot(path: Path) -> dict:
    """Parse a schematic into comparable semantic pieces.

    The schematic save path re-serialises the whole document, so tests compare
    these parsed structures instead of file bytes for anything written through
    ``save_schematic``.
    """
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    snapshot: dict = {
        "lib_symbols": {},
        "placements": [],
        "wires": [],
        "junctions": [],
        "labels": [],
        "sheet_instances": None,
    }
    for node in data[1:]:
        keyword = _keyword(node)
        if keyword == "lib_symbols":
            for lib in node[1:]:
                if _keyword(lib) != "symbol":
                    continue
                units = {
                    str(sub[1]): _lib_pin_pairs(sub) for sub in lib[2:] if _keyword(sub) == "symbol"
                }
                snapshot["lib_symbols"][str(lib[1])] = units
        elif keyword == "symbol":
            properties = {
                str(prop[1]): str(prop[2])
                for prop in node[2:]
                if _keyword(prop) == "property" and len(prop) > 2
            }
            snapshot["placements"].append(
                {
                    "lib_id": _child_value(node, "lib_id"),
                    "reference": properties.get("Reference"),
                    "unit": _child_value(node, "unit"),
                    "uuid": _child_value(node, "uuid"),
                    "pins": tuple(
                        str(child[1])
                        for child in node[2:]
                        if _keyword(child) == "pin" and len(child) > 1
                    ),
                    "properties": tuple(sorted(properties.items())),
                    "instances": sexpdata.dumps(_child_node(node, "instances")),
                }
            )
        elif keyword == "wire":
            snapshot["wires"].append(sexpdata.dumps(node))
        elif keyword == "junction":
            snapshot["junctions"].append(sexpdata.dumps(node))
        elif keyword.endswith("label"):
            snapshot["labels"].append(sexpdata.dumps(node))
        elif keyword == "sheet_instances":
            snapshot["sheet_instances"] = sexpdata.dumps(node)
    return snapshot


def _placement_pin_numbers(path: Path, lib_id: str) -> list[set[str]]:
    """Return each placement's resolved pin-number set for *lib_id*."""
    from kcaa.utils.skip_compat import safe_schematic

    sch = safe_schematic(str(path))
    return [
        {pin.number for pin in symbol.pin} for symbol in sch.symbol if symbol.lib_id.value == lib_id
    ]


def _placement_pin_uuids(path: Path) -> list[tuple[str, str]]:
    """Return ``(lib_id, uuid)`` for every instance pin key in *path*."""
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for node in data[1:]:
        if _keyword(node) != "symbol":
            continue
        lib_id = _child_value(node, "lib_id")
        for child in node[1:]:
            if _keyword(child) != "pin":
                continue
            pairs.append((lib_id, _child_value(child, "uuid")))
    return pairs


def _connection_count(path: Path) -> int:
    """Return the number of resolved pin connections via the netlist parser."""
    return analyze_netlist(SchematicParser(str(path)).parse())["total_pin_connections"]


# ---------------------------------------------------------------------------
# Library util
# ---------------------------------------------------------------------------


def test_rename_pin_number_keeps_pin_name_and_writes_backup(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    original_bytes = library.read_bytes()

    result = set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])

    assert result["symbol_name"] == "Plain"
    assert result["unit"] is None
    assert result["pins_requested"] == 1
    assert result["pins_changed"] == 1
    assert result["occurrences"] == 1
    assert result["changes"] == [
        {
            "pin_number": "P$1",
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "A",
            "changed": True,
        }
    ]
    assert result["changed"] is True
    assert result["backup_path"] == str(library) + ".bak"
    assert result["warnings"] == []

    text = library.read_text(encoding="utf-8")
    assert '(number "1"' in text
    plain_block = text[text.index('(symbol "Plain"') : text.index('(symbol "MultiUnit"')]
    assert '(number "P$1"' not in plain_block
    assert '(name "A"' in plain_block
    assert Path(result["backup_path"]).read_bytes() == original_bytes


def test_rename_number_and_name_in_one_call(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1", new_name="IN")])

    assert result["occurrences"] == 1
    assert result["changes"] == [
        {
            "pin_number": "P$1",
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "IN",
            "changed": True,
        }
    ]
    text = library.read_text(encoding="utf-8")
    assert '(number "1"' in text
    assert '(name "IN"' in text
    plain_block = text[text.index('(symbol "Plain"') : text.index('(symbol "MultiUnit"')]
    assert '(name "A"' not in plain_block


def test_name_only_change_keeps_pin_number(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", [_edit("P$2", new_name="OUT")])

    assert result["changes"] == [
        {
            "pin_number": "P$2",
            "old_number": "P$2",
            "old_name": "B",
            "new_number": "P$2",
            "new_name": "OUT",
            "changed": True,
        }
    ]
    text = library.read_text(encoding="utf-8")
    assert '(number "P$2"' in text
    assert '(name "OUT"' in text


def test_byte_preservation_only_the_number_literals_change(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    original = library.read_text(encoding="utf-8")

    set_symbol_pin(
        library,
        "Plain",
        [_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
    )

    edited = library.read_text(encoding="utf-8")
    original_lines = original.splitlines()
    edited_lines = edited.splitlines()
    assert len(original_lines) == len(edited_lines)
    differing = [
        (old, new) for old, new in zip(original_lines, edited_lines, strict=True) if old != new
    ]
    assert differing == [
        ('        (number "P$1"', '        (number "1"'),
        ('        (number "P$2"', '        (number "2"'),
    ]

    other_start = original.index('  (symbol "Other"')
    assert original[other_start:] == edited[edited.index('  (symbol "Other"') :]
    assert '(number "9"' in edited
    assert "(generator kicad_symbol_editor)" in edited


def test_crlf_line_endings_are_preserved(tmp_path):
    library = tmp_path / "crlf.kicad_sym"
    original = SYMBOL_LIBRARY.read_bytes().replace(b"\n", b"\r\n")
    library.write_bytes(original)

    set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])

    edited = library.read_bytes()
    assert b"\r\n" in edited
    assert edited.replace(b"\r\n", b"\n").count(b"\n") == original.count(b"\r\n")
    assert edited == original.replace(b'(number "P$1"', b'(number "1"', 1)


def test_multi_unit_scope_edits_all_units_or_one(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    both = set_symbol_pin(library, "MultiUnit", [_edit("1", new_name="RENAMED")])
    assert both["pins_requested"] == 1
    assert both["pins_changed"] == 2
    assert both["occurrences"] == 2
    assert [change["new_name"] for change in both["changes"]] == ["RENAMED", "RENAMED"]
    assert library.read_text(encoding="utf-8").count('(name "RENAMED"') == 2

    one = set_symbol_pin(library, "MultiUnit", [_edit("1", new_name="UNIT_TWO")], unit=2)
    assert one["occurrences"] == 1
    assert one["changes"] == [
        {
            "pin_number": "1",
            "old_number": "1",
            "old_name": "RENAMED",
            "new_number": "1",
            "new_name": "UNIT_TWO",
            "changed": True,
        }
    ]


def test_second_call_with_same_values_is_a_noop(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])
    before_bytes = library.read_bytes()
    before_mtime = library.stat().st_mtime_ns
    backup_bytes = Path(str(library) + ".bak").read_bytes()

    result = set_symbol_pin(library, "Plain", [_edit("1", new_number="1")])

    assert result["changed"] is False
    assert result["pins_changed"] == 0
    assert result["occurrences"] == 1
    assert result["backup_path"] is None
    assert library.read_bytes() == before_bytes
    assert library.stat().st_mtime_ns == before_mtime
    assert Path(str(library) + ".bak").read_bytes() == backup_bytes


def test_collision_error_leaves_file_untouched(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(
        ValueError, match="pin number 'P\\$2' is already used by another pin in the symbol"
    ):
        set_symbol_pin(library, "Plain", [_edit("P$1", new_number="P$2")])

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_cross_unit_number_is_a_warning_not_an_error(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "MultiUnit", [_edit("1", new_number="2")], unit=2)

    assert result["changed"] is True
    assert result["warnings"] == ["pin number '2' is also used by another pin of this symbol"]


def test_unknown_pin_error_lists_available_numbers(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", [_edit("X99", new_number="1")])

    message = str(excinfo.value)
    assert message.startswith("pin 'X99' not found in symbol 'Plain'")
    assert "(available pin numbers: " in message
    assert "P$1" in message
    assert "P$2" in message
    assert library.read_bytes() == before


def test_extends_symbol_without_pins_is_refused(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Derived", [_edit("1", new_number="2")])

    assert str(excinfo.value) == (
        "symbol 'Derived' extends 'Plain' and defines no pins; "
        "update the base symbol 'Plain' instead"
    )


def test_two_pins_renamed_in_one_write(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    original_bytes = library.read_bytes()

    result = set_symbol_pin(
        library,
        "Plain",
        [_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
    )

    assert result["pins_requested"] == 2
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert [change["pin_number"] for change in result["changes"]] == ["P$1", "P$2"]
    assert [change["new_number"] for change in result["changes"]] == ["1", "2"]
    assert Path(str(library) + ".bak").read_bytes() == original_bytes


def test_cycle_permutation_is_accepted(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(
        library,
        "Chain",
        [
            _edit("P$1", new_number="1"),
            _edit("1", new_number="2"),
            _edit("2", new_number="P$1"),
        ],
    )

    assert result["changed"] is True
    assert result["pins_requested"] == 3
    assert result["pins_changed"] == 3
    assert _symbol_pin_numbers(library, "Chain") == {"1", "2", "P$1", "P$2"}


def test_mixed_number_and_name_batch(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(
        library,
        "Plain",
        [_edit("P$1", new_number="1"), _edit("P$2", new_name="OUT")],
    )

    assert result["pins_changed"] == 2
    assert result["changes"] == [
        {
            "pin_number": "P$1",
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "A",
            "changed": True,
        },
        {
            "pin_number": "P$2",
            "old_number": "P$2",
            "old_name": "B",
            "new_number": "P$2",
            "new_name": "OUT",
            "changed": True,
        },
    ]


def test_batch_atomicity_unknown_pin(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError, match="pin 'MISSING' not found"):
        set_symbol_pin(
            library,
            "Plain",
            [_edit("P$1", new_number="1"), _edit("MISSING", new_number="2")],
        )

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_batch_atomicity_collision(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError, match="pin number 'P\\$2' is already used"):
        set_symbol_pin(
            library,
            "Plain",
            [_edit("P$1", new_number="P$2"), _edit("P$2", new_name="X")],
        )

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_batch_rejects_more_than_256_entries(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()
    pins = [_edit(f"X{i}", new_number="1") for i in range(257)]

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", pins)

    assert str(excinfo.value) == BAD_TOO_MANY
    assert library.read_bytes() == before


def test_duplicate_selector_is_rejected(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1"), _edit("P$1", new_name="X")])

    assert str(excinfo.value) == "duplicate pin_number 'P$1' in pins list"
    assert library.read_bytes() == before


def test_partial_noop_batch_still_writes(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(
        library,
        "Plain",
        [_edit("P$1", new_number="P$1"), _edit("P$2", new_number="2")],
    )

    assert result["changed"] is True
    assert result["pins_requested"] == 2
    assert result["pins_changed"] == 1
    assert result["occurrences"] == 2


def test_fully_noop_batch_does_not_write(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    result = set_symbol_pin(
        library,
        "Plain",
        [_edit("P$1", new_number="P$1"), _edit("P$2", new_name="B")],
    )

    assert result["changed"] is False
    assert result["pins_changed"] == 0
    assert result["backup_path"] is None
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


@pytest.mark.parametrize(
    ("pins", "message"),
    [
        ([], BAD_LIST),
        ("not-a-list", BAD_LIST),
        ([None], BAD_ENTRY),
        ([{"new_number": "1"}], BAD_SELECTOR),
        ([{"pin_number": ""}], BAD_SELECTOR),
        ([{"pin_number": "P$1"}], BOTH_NONE),
        ([{"pin_number": "P$1", "new_number": ""}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "a b"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": 'a"b'}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "a(b"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "a\\b"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "x" * 33}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "\x00"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "\x1b"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_number": "\x7f"}], BAD_NUMBER),
        ([{"pin_number": "P$1", "new_name": 'a"b'}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "a(b"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "a\nb"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "a\tb"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "a\\b"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "VCC\\"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "\x00"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "\x1b"}], BAD_NAME),
        ([{"pin_number": "P$1", "new_name": "\x7f"}], BAD_NAME),
        (
            [{"pin_number": "P$1", "new_number": "1", "extra": "x"}],
            "unknown pins entry field: 'extra'",
        ),
    ],
)
def test_invalid_batches_are_rejected_before_writing(tmp_path, pins, message):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", pins)

    assert str(excinfo.value) == message
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x7f"])
def test_control_characters_are_rejected_without_backup(tmp_path, control):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", [_edit("P$1", new_name=f"A{control}B")])

    assert str(excinfo.value) == BAD_NAME
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


def test_common_pin_edits_apply_to_every_unit(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "MultiUnit", [_edit("C0", new_number="C9")], unit=0)

    assert result["changed"] is True
    assert result["occurrences"] == 1
    assert [change["new_number"] for change in result["changes"]] == ["C9"]
    assert result["warnings"] == []
    assert _symbol_pin_numbers(library, "MultiUnit") == {"C9", "1", "2", "3"}
    text = library.read_text(encoding="utf-8")
    assert '(number "C9"' in text
    assert '(number "C0"' not in text


@pytest.mark.parametrize(
    ("pins", "unit"),
    [
        ([_edit("1", new_number="C0")], 1),
        ([_edit("C0", new_number="1")], 0),
    ],
)
def test_common_pin_number_collision_is_an_error(tmp_path, pins, unit):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError, match="pin number 'C0'|pin number '1'"):
        set_symbol_pin(library, "MultiUnit", pins, unit=unit)

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_both_styles_do_not_collide_and_noop_is_allowed(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    noop = set_symbol_pin(library, "BothStyles", [_edit("1", new_name="A")])
    assert noop["changed"] is False
    assert noop["occurrences"] == 2

    renamed = set_symbol_pin(library, "BothStyles", [_edit("1", new_number="10")], unit=1)
    assert renamed["changed"] is True
    assert renamed["pins_changed"] == 2
    assert renamed["occurrences"] == 2
    assert library.read_text(encoding="utf-8").count('(number "10"') == 2
    assert {change["new_number"] for change in renamed["changes"]} == {"10"}
    assert _symbol_pin_numbers(library, "BothStyles") == {"10", "2"}


def test_pre_existing_stacked_pins_do_not_block_edits(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    renamed = set_symbol_pin(library, "Stacked", [_edit("6", new_number="7")])
    assert renamed["changed"] is True
    assert _symbol_pin_numbers(library, "Stacked") == {"5", "7"}

    stack_name = set_symbol_pin(library, "Stacked", [_edit("5", new_name="STACK")])
    assert stack_name["changed"] is True
    assert stack_name["occurrences"] == 2
    assert stack_name["pins_changed"] == 2


def test_stacked_pins_are_renumbered_together(tmp_path):
    """KiCad jumpers: a shared number stays one connection after the rename."""
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Stacked", [_edit("5", new_number="7")])

    assert result["changed"] is True
    assert result["pins_requested"] == 1
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert result["warnings"] == []
    assert _symbol_pin_numbers(library, "Stacked") == {"7", "6"}


def test_stacked_rename_rejects_a_number_already_in_use(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    # Make 7 an existing number, then try to renumber the stacked 5s onto it.
    set_symbol_pin(library, "Stacked", [_edit("6", new_number="7")])
    before = library.read_bytes()

    with pytest.raises(ValueError, match="pin number '7' is already used by another pin"):
        set_symbol_pin(library, "Stacked", [_edit("5", new_number="7")])

    assert library.read_bytes() == before
    assert not Path(str(library) + ".tmp").exists()
    assert _symbol_pin_numbers(library, "Stacked") == {"5", "7"}


def test_batch_cannot_map_two_selectors_to_one_number(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError, match="requested for more than one pin"):
        set_symbol_pin(
            library,
            "Plain",
            [_edit("P$1", new_number="9"), _edit("P$2", new_number="9")],
        )

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


def test_shared_selector_does_not_warn_against_its_own_edit(tmp_path):
    """One selector matching several pins is a single edit, not a conflict."""
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "MultiUnit", [_edit("1", new_number="5")])

    assert result["pins_changed"] == 2
    assert result["warnings"] == []
    assert _symbol_pin_numbers(library, "MultiUnit") == {"C0", "5", "2", "3"}


def test_same_new_number_warns_once_per_batch(tmp_path):
    """Two selectors landing on one number from separate parts warn once."""
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(
        library,
        "MultiUnit",
        [_edit("2", new_number="5"), _edit("3", new_number="5")],
    )

    assert result["changed"] is True
    assert result["warnings"] == ["pin number '5' is also used by another pin of this symbol"]
    assert [change["new_number"] for change in result["changes"]] == ["5", "5"]


def test_duplicate_pin_names_are_allowed(tmp_path):
    """Names label pins; only numbers are constrained to stay unique."""
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", [_edit("P$1", new_name="B")])

    assert result["changed"] is True
    assert [change["new_name"] for change in result["changes"]] == ["B"]
    text = library.read_text(encoding="utf-8")
    plain = text[text.index('(symbol "Plain"') : text.index('(symbol "MultiUnit"')]
    assert plain.count('(name "B"') == 2


def test_read_only_library_is_refused(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()
    library.chmod(0o444)

    with pytest.raises(PermissionError, match="not writable"):
        set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()
    assert stat.S_IMODE(library.stat().st_mode) == 0o444


def test_library_mode_is_preserved(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    library.chmod(0o640)

    set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])

    assert stat.S_IMODE(library.stat().st_mode) == 0o640


def test_corrupt_library_reports_syntax_error(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    text = library.read_text(encoding="utf-8")
    library.write_text(text[: text.rindex(")")], encoding="utf-8")
    before = library.read_bytes()

    with pytest.raises(ValueError, match="not valid s-expression"):
        set_symbol_pin(library, "Plain", [_edit("P$1", new_number="1")])

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


def test_empty_pin_name_and_spaces_are_written_verbatim(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", [_edit("P$1", new_name="")])

    assert result["changes"][0]["new_name"] == ""
    assert '(name ""' in library.read_text(encoding="utf-8")

    spaced = set_symbol_pin(library, "Plain", [_edit("P$2", new_name="PIN OUT")])
    assert spaced["changes"][0]["new_name"] == "PIN OUT"
    assert '(name "PIN OUT"' in library.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Library tool
# ---------------------------------------------------------------------------


def test_tool_refreshes_index_once_for_batch(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._index_symbol_library",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
    )

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
        )
    )

    assert result["success"] is True
    assert result["library_name"] == _TOOL_LIB
    assert result["symbol_name"] == "Plain"
    assert result["library_path"] == str(library)
    assert result["pins_requested"] == 2
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert result["changed"] is True
    assert result["indexed"] is True
    assert calls == [
        ((_TOOL_LIB, str(library)), {"project_dir": str(tmp_path), "project": str(tmp_path)})
    ]

    noop = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("1", new_number="1")],
        )
    )
    assert noop["changed"] is False
    assert noop["indexed"] is False
    assert len(calls) == 1


def test_tool_reports_index_failure_as_not_indexed(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._index_symbol_library",
        lambda *args, **kwargs: -1,
    )

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["indexed"] is False
    assert '(number "1"' in library.read_text(encoding="utf-8")


def test_tool_refuses_system_library(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools.config.is_system_library_path",
        lambda path: True,
    )
    before = library.read_bytes()

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result == {
        "error": (
            f"Refusing to modify system library '{_TOOL_LIB}' "
            f"(resolves to {library}, inside the KiCad installation): "
            "symbol library tools operate on user libraries only."
        )
    }
    assert library.read_bytes() == before


def test_tool_refuses_read_only_library(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    before = library.read_bytes()
    library.chmod(0o444)

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result == {
        "error": (
            f"Library file is not writable: {library} "
            "(fix the file permissions and retry; nothing was changed)"
        )
    }
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_tool_reports_invalid_pin_as_error(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:Plain",
            pins=[_edit("nope", new_number="1")],
        )
    )

    assert "error" in result
    assert "success" not in result
    assert result["error"].startswith("pin 'nope' not found in symbol 'Plain'")


def test_tool_selector_in_two_units_edits_both(monkeypatch, tmp_path):
    """A shared number is one connection, so every unit's definition is edited."""
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._index_symbol_library",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
    )

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=f"{_TOOL_LIB}:MultiUnit",
            pins=[_edit("1", new_name="RENAMED")],
        )
    )

    assert "unit" not in result
    assert result["pins_requested"] == 1
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert {change["new_name"] for change in result["changes"]} == {"RENAMED"}
    assert [change["old_number"] for change in result["changes"]] == ["1", "1"]
    assert library.read_text(encoding="utf-8").count('(name "RENAMED"') == 2
    assert len(calls) == 1


def test_tool_rejects_lib_id_without_colon(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    before = library.read_bytes()

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id="MYOP",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result == {"error": "lib_id must be 'Library:Symbol' (got 'MYOP')"}
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


@pytest.mark.parametrize("lib_id", [":Plain", f"{_TOOL_LIB}:"])
def test_tool_rejects_lib_id_with_empty_half(monkeypatch, tmp_path, lib_id):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    before = library.read_bytes()

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(tmp_path),
            lib_id=lib_id,
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result == {"error": f"lib_id must be 'Library:Symbol' (got {lib_id!r})"}
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()
    assert not Path(str(library) + ".tmp").exists()


def test_tool_project_local_library_edits_with_project_dir(monkeypatch, tmp_path):
    project_dir, library = _make_project_local_library(tmp_path)
    _patch_real_resolution(monkeypatch)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._index_symbol_library",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
    )

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            project_dir=str(project_dir),
            lib_id=f"{_PROJECT_LOCAL_LIB}:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result["success"] is True
    assert result["library_name"] == _PROJECT_LOCAL_LIB
    assert result["symbol_name"] == "Plain"
    assert result["library_path"] == str(library)
    assert result["changed"] is True
    assert result["indexed"] is True
    assert calls == [
        (
            (_PROJECT_LOCAL_LIB, str(library)),
            {"project_dir": str(project_dir), "project": str(project_dir.resolve())},
        )
    ]
    assert '(number "1"' in library.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Schematic tool
# ---------------------------------------------------------------------------


def test_schematic_tool_edits_symbol_stored_without_nickname(tmp_path):
    """A bare cache key is editable as-is; the qualified spelling must not match."""
    schematic = _copy(SCHEMATIC, tmp_path)
    _make_bare_id_schematic(schematic, "PinEdit:Plain", "Plain")
    before = schematic.read_bytes()

    missing = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert missing["error"].startswith(
        "lib_id 'PinEdit:Plain' not found in the schematic's lib_symbols cache"
    )
    assert "'Plain'" in missing["error"]
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
        )
    )

    assert result["success"] is True
    assert result["lib_id"] == "Plain"
    assert result["changed"] is True
    assert result["placements_updated"] == 2
    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["Plain"]["Plain_1_1"] == (("1", "A"), ("2", "B"))
    assert [placement["pins"] for placement in after["placements"]] == [
        ("C0", "1", "2"),
        ("C0", "1", "2"),
    ]


def test_schematic_tool_bare_name_does_not_match_qualified_cache(tmp_path):
    """``R_Small`` must not resolve to the stored ``Device:R_Small``."""
    schematic = _copy(SINGLE_PIN, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="R_Small",
            pins=[_edit("1", new_number="2")],
        )
    )

    assert result["error"].startswith(
        "lib_id 'R_Small' not found in the schematic's lib_symbols cache"
    )
    assert "'Device:R_Small'" in result["error"]
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()
    assert not Path(str(schematic) + ".tmp").exists()


def test_schematic_tool_not_found_lists_at_most_five_ids(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    _add_cached_symbols(schematic, [f"Zz:Extra{n}" for n in range(1, 6)])

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="Nope:Nope",
            pins=[_edit("1", new_number="2")],
        )
    )

    assert result == {
        "error": (
            "lib_id 'Nope:Nope' not found in the schematic's lib_symbols cache"
            " (available: 'Zz:Extra1', 'Zz:Extra2', 'Zz:Extra3', 'Zz:Extra4', 'Zz:Extra5')"
        )
    }


def test_schematic_tool_renames_stacked_pins_in_placements(tmp_path):
    """A shared number rewrites every placement key; uuids and netlist survive."""
    schematic = _copy(SCHEMATIC, tmp_path)
    _add_stacked_pin(schematic)
    before_uuids = _placement_pin_uuids(schematic)
    connections_before = _connection_count(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result["changed"] is True
    assert result["pins_requested"] == 1
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert result["placements_updated"] == 2
    assert result["warnings"] == []

    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["PinEdit:Plain"]["Plain_1_1"] == (
        ("1", "A"),
        ("P$2", "B"),
        ("1", "A2"),
    )
    assert [placement["pins"] for placement in after["placements"]] == [
        ("C0", "1", "1", "P$2"),
        ("C0", "1", "1", "P$2"),
    ]
    assert _placement_pin_uuids(schematic) == before_uuids
    assert _placement_pin_numbers(schematic, "PinEdit:Plain") == [
        {"C0", "1", "P$2"},
        {"C0", "1", "P$2"},
    ]
    assert _connection_count(schematic) == connections_before


def test_schematic_tool_batch_updates_cache_and_placement_keys(tmp_path):
    """Renames both cached pins and both placements' instance pin keys.

    The file is re-serialised by the normal schematic save path, so assertions
    are semantic (parsed structures), never byte identity.
    """
    schematic = _copy(SCHEMATIC, tmp_path)
    original_bytes = schematic.read_bytes()
    before = _semantic_snapshot(schematic)
    connections_before = _connection_count(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
        )
    )

    assert result["success"] is True
    assert result["lib_id"] == "PinEdit:Plain"
    assert result["symbol_name"] == "Plain"
    assert result["pins_requested"] == 2
    assert result["pins_changed"] == 2
    assert result["occurrences"] == 2
    assert result["changes"] == [
        {
            "pin_number": "P$1",
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "A",
            "changed": True,
        },
        {
            "pin_number": "P$2",
            "old_number": "P$2",
            "old_name": "B",
            "new_number": "2",
            "new_name": "B",
            "changed": True,
        },
    ]
    assert result["changed"] is True
    assert result["backup_path"] == str(schematic) + ".bak"
    assert result["affected_references"] == ["R1", "R2"]
    assert result["affected_count"] == 2
    assert result["placements_updated"] == 2
    assert result["warnings"] == []

    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["PinEdit:Plain"]["Plain_1_1"] == (
        ("1", "A"),
        ("2", "B"),
    )
    assert after["lib_symbols"]["PinEdit:Plain"]["Plain_0_1"] == (("C0", "COMMON"),)
    assert after["lib_symbols"]["Device:C"] == before["lib_symbols"]["Device:C"]
    assert [placement["pins"] for placement in after["placements"]] == [
        ("C0", "1", "2"),
        ("C0", "1", "2"),
    ]
    for old_placement, new_placement in zip(before["placements"], after["placements"], strict=True):
        assert {key: value for key, value in old_placement.items() if key != "pins"} == {
            key: value for key, value in new_placement.items() if key != "pins"
        }
    assert after["wires"] == before["wires"]
    assert after["junctions"] == before["junctions"]
    assert after["labels"] == before["labels"]
    assert after["sheet_instances"] == before["sheet_instances"]

    # The reader now resolves the renamed pins on every placement.
    assert _connection_count(schematic) == connections_before == 6
    assert _placement_pin_numbers(schematic, "PinEdit:Plain") == [
        {"C0", "1", "2"},
        {"C0", "1", "2"},
    ]
    assert Path(str(schematic) + ".bak").read_bytes() == original_bytes


def test_schematic_tool_rewrites_through_normal_save_path(tmp_path):
    """A name-only edit is checked semantically: the save path re-serialises."""
    schematic = _copy(SINGLE_PIN, tmp_path)
    before = _semantic_snapshot(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="Mechanical:MountingHole_Pad",
            pins=[_edit("1", new_name="PAD")],
        )
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["placements_updated"] == 0
    assert result["affected_references"] == ["MGT1"]

    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["Mechanical:MountingHole_Pad"]["MountingHole_Pad_1_1"] == (
        ("1", "PAD"),
    )
    assert after["lib_symbols"]["Device:R_Small"] == before["lib_symbols"]["Device:R_Small"]
    assert after["placements"] == before["placements"]
    assert after["wires"] == before["wires"]
    assert after["junctions"] == before["junctions"]
    assert after["labels"] == before["labels"]
    assert after["sheet_instances"] == before["sheet_instances"]


def test_schematic_tool_common_pin_updates_placements_of_every_unit(tmp_path):
    """A common-unit pin edit rewrites the instance keys of every unit.

    No unit selector exists any more, so the batch covers the whole cached
    definition and all placements of the ``lib_id``.
    """
    schematic = _copy(MULTI_UNIT, tmp_path)
    _add_common_pin(schematic)
    connections_before = _connection_count(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="myLib:DUAL",
            pins=[_edit("C0", new_number="C9")],
        )
    )

    assert "unit" not in result
    assert result["occurrences"] == 1
    assert result["changed"] is True
    assert result["placements_updated"] == 2
    assert result["affected_references"] == ["U1"]
    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["myLib:DUAL"]["DUAL_0_1"] == (("C9", "COM"),)
    assert after["lib_symbols"]["myLib:DUAL"]["DUAL_1_1"] == (("1", ""), ("2", ""))
    assert after["lib_symbols"]["myLib:DUAL"]["DUAL_2_1"] == (("3", ""), ("4", ""))
    placements = [
        (placement["unit"], placement["pins"])
        for placement in after["placements"]
        if placement["lib_id"] == "myLib:DUAL"
    ]
    assert placements == [("1", ("1", "C9", "2")), ("2", ("3", "C9", "4"))]
    assert _connection_count(schematic) == connections_before


def test_schematic_tool_common_pin_updates_all_placements(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    connections_before = _connection_count(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("C0", new_number="C1")],
        )
    )

    assert result["changed"] is True
    assert result["placements_updated"] == 2
    assert result["affected_references"] == ["R1", "R2"]
    assert result["affected_count"] == 2
    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["PinEdit:Plain"]["Plain_0_1"] == (("C1", "COMMON"),)
    assert [placement["pins"] for placement in after["placements"]] == [
        ("C1", "P$1", "P$2"),
        ("C1", "P$1", "P$2"),
    ]
    assert _placement_pin_numbers(schematic, "PinEdit:Plain") == [
        {"C1", "P$1", "P$2"},
        {"C1", "P$1", "P$2"},
    ]
    assert _connection_count(schematic) == connections_before == 6


def test_schematic_tool_cross_unit_number_warning(tmp_path):
    schematic = _copy(MULTI_UNIT, tmp_path)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="myLib:DUAL",
            pins=[_edit("1", new_number="3")],
        )
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["warnings"] == ["pin number '3' is also used by another pin of this symbol"]
    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["myLib:DUAL"]["DUAL_1_1"] == (("3", ""), ("2", ""))


def test_schematic_tool_chain_rename_rewrites_keys_once(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$2", new_number="P$1")],
        )
    )

    assert result["changed"] is True
    assert result["placements_updated"] == 2
    after = _semantic_snapshot(schematic)
    assert after["lib_symbols"]["PinEdit:Plain"]["Plain_1_1"] == (
        ("1", "A"),
        ("P$1", "B"),
    )
    assert [placement["pins"] for placement in after["placements"]] == [
        ("C0", "1", "P$1"),
        ("C0", "1", "P$1"),
    ]
    assert _placement_pin_numbers(schematic, "PinEdit:Plain") == [
        {"C0", "1", "P$1"},
        {"C0", "1", "P$1"},
    ]


def test_schematic_tool_common_pin_collision_is_an_error(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="C0")],
        )
    )

    assert result["error"] == "pin number 'C0' is already used by another pin in the symbol"
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()
    assert not Path(str(schematic) + ".tmp").exists()


def test_schematic_tool_rewrite_keeps_instance_pin_uuids(tmp_path):
    """Keys are matched by number; each instance's ``(uuid …)`` child survives."""
    schematic = _copy(SCHEMATIC, tmp_path)
    before_uuids = _placement_pin_uuids(schematic)

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result["changed"] is True
    assert result["placements_updated"] == 2
    assert _placement_pin_uuids(schematic) == before_uuids
    assert [placement["pins"] for placement in _semantic_snapshot(schematic)["placements"]] == [
        ("C0", "1", "P$2"),
        ("C0", "1", "P$2"),
    ]


def test_schematic_tool_reports_single_instance(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    text = schematic.read_text(encoding="utf-8")
    text = text.replace(
        '(symbol (lib_id "PinEdit:Plain") (at 120 100 0)',
        '(symbol (lib_id "Device:C") (at 120 100 0)',
    )
    schematic.write_text(text, encoding="utf-8")

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1")],
        )
    )

    assert result["affected_references"] == ["R1"]
    assert result["affected_count"] == 1
    assert result["placements_updated"] == 1


def test_schematic_tool_unknown_lib_id_leaves_file_untouched(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="Missing:Symbol",
            pins=[_edit("1", new_number="2")],
        )
    )

    assert result == {
        "error": (
            "lib_id 'Missing:Symbol' not found in the schematic's lib_symbols cache"
            " (available: 'PinEdit:Plain', 'Device:C')"
        )
    }
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()


def test_schematic_tool_second_call_is_a_noop(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    tools = _tools()
    asyncio.run(
        tools["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$2", new_number="2")],
        )
    )
    before = schematic.read_bytes()
    before_mtime = schematic.stat().st_mtime_ns
    backup = Path(str(schematic) + ".bak")
    backup_before = backup.read_bytes()

    result = asyncio.run(
        tools["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("1", new_number="1"), _edit("2", new_number="2")],
        )
    )

    assert result["changed"] is False
    assert result["pins_changed"] == 0
    assert result["occurrences"] == 2
    assert result["backup_path"] is None
    assert result["affected_references"] == ["R1", "R2"]
    assert result["placements_updated"] == 0
    assert schematic.read_bytes() == before
    assert schematic.stat().st_mtime_ns == before_mtime
    assert backup.read_bytes() == backup_before


def test_schematic_tool_duplicate_selector_is_rejected(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_number="1"), _edit("P$1", new_name="X")],
        )
    )

    assert result == {"error": "duplicate pin_number 'P$1' in pins list"}
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()


@pytest.mark.parametrize("bad_name", ["VCC\\", "A\\nB", "a\\b"])
def test_schematic_tool_rejects_backslash_name_without_writing(tmp_path, bad_name):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=[_edit("P$1", new_name=bad_name)],
        )
    )

    assert result == {"error": BAD_NAME}
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()


@pytest.mark.parametrize(
    ("pins", "message"),
    [
        ([], BAD_LIST),
        ([None], BAD_ENTRY),
        ([_edit("P$1", new_number="a b")], BAD_NUMBER),
        ([_edit("P$1", new_name='a"b')], BAD_NAME),
        ([_edit("P$1", new_name="VCC\\")], BAD_NAME),
        ([_edit("P$1", new_name="\x00")], BAD_NAME),
        ([_edit("P$1", new_name="\x1b")], BAD_NAME),
        ([_edit("P$1", new_name="\x7f")], BAD_NAME),
        ([_edit("P$1", new_number="\x00")], BAD_NUMBER),
        ([_edit("P$1", new_number="\x1b")], BAD_NUMBER),
        ([_edit("P$1", new_number="\x7f")], BAD_NUMBER),
    ],
)
def test_schematic_tool_validates_before_writing(tmp_path, pins, message):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pins=pins,
        )
    )

    assert result == {"error": message}
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()


# ---------------------------------------------------------------------------
# Public tool schema
# ---------------------------------------------------------------------------


def test_tool_schemas_pin_the_public_surface():
    """The MCP schema must keep the round-8/9 signatures (no ``unit``).

    Calling the functions directly would still fail if someone re-added
    ``unit`` or gave ``project_dir`` a default; the advertised schema is what
    the plugin exposes to the model, so it is asserted here.
    """
    from fastmcp import FastMCP

    from kcaa.tools.symbol_edit_tools import register_symbol_edit_tools

    mcp = FastMCP("test")
    register_symbol_edit_tools(mcp)

    async def _parameters():
        return {
            name: (await mcp.get_tool(name)).parameters
            for name in ("update_library_symbol_pin", "update_schematic_symbol_pin")
        }

    schemas = asyncio.run(_parameters())

    library = schemas["update_library_symbol_pin"]
    assert sorted(library["required"]) == ["lib_id", "pins", "project_dir"]
    assert "unit" not in library["properties"]
    assert "ctx" not in library["properties"]
    assert library["properties"]["pins"]["type"] == "array"
    assert library["properties"]["pins"]["items"]["type"] == "object"

    schematic = schemas["update_schematic_symbol_pin"]
    assert sorted(schematic["required"]) == ["lib_id", "pins", "schematic_path"]
    assert "unit" not in schematic["properties"]
    assert "ctx" not in schematic["properties"]
    assert schematic["properties"]["pins"]["type"] == "array"
    assert schematic["properties"]["pins"]["items"]["type"] == "object"
