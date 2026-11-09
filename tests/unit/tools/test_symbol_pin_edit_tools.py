"""Tests for the pin-number / pin-name editing surface (issue #266).

Covers ``kcaa.utils.symbol_library_utils.set_symbol_pin`` plus the
``update_library_symbol_pin`` and ``update_schematic_symbol_pin`` MCP tools.
All writes happen on temporary copies of the fixtures so the originals stay
untouched.  No full netlist extraction is performed.
"""

import asyncio
from pathlib import Path
import shutil

import pytest

from kcaa.utils.symbol_library_utils import set_symbol_pin

FIXTURES = Path(__file__).parent / "fixtures"
SYMBOL_LIBRARY = FIXTURES / "pin_edit_symbols.kicad_sym"
SCHEMATIC = FIXTURES / "pin_edit.kicad_sch"

_TOOL_LIB = "pinlib"

BOTH_NONE = "both new_number and new_name are None; nothing to change"
BAD_NUMBER = (
    "new_number must be a non-empty token without whitespace, quotes, "
    "parentheses or backslashes (max 32 characters)"
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


# ---------------------------------------------------------------------------
# Library util
# ---------------------------------------------------------------------------


def test_rename_pin_number_keeps_pin_name_and_writes_backup(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    original_bytes = library.read_bytes()

    result = set_symbol_pin(library, "Plain", "P$1", new_number="1")

    assert result["symbol_name"] == "Plain"
    assert result["pin_number"] == "1"
    assert result["unit"] is None
    assert result["occurrences"] == 1
    assert result["changes"] == [
        {
            "unit": 1,
            "style": 1,
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "A",
        }
    ]
    assert result["changed"] is True
    assert result["backup_path"] == str(library) + ".bak"
    assert result["warnings"] == []

    text = library.read_text(encoding="utf-8")
    assert '(number "1"' in text
    assert '(number "P$1"' not in text
    assert '(name "A"' in text
    assert Path(result["backup_path"]).read_bytes() == original_bytes


def test_rename_number_and_name_in_one_call(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", "P$1", new_number="1", new_name="IN")

    assert result["occurrences"] == 1
    assert result["changes"] == [
        {
            "unit": 1,
            "style": 1,
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "IN",
        }
    ]
    text = library.read_text(encoding="utf-8")
    assert '(number "1"' in text
    assert '(name "IN"' in text
    plain_block = text[text.index('(symbol "Plain"') : text.index('(symbol "MultiUnit"')]
    assert '(name "A"' not in plain_block


def test_name_only_change_keeps_pin_number(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", "P$2", new_name="OUT")

    assert result["pin_number"] == "P$2"
    assert result["changes"] == [
        {
            "unit": 1,
            "style": 1,
            "old_number": "P$2",
            "old_name": "B",
            "new_number": "P$2",
            "new_name": "OUT",
        }
    ]
    text = library.read_text(encoding="utf-8")
    assert '(number "P$2"' in text
    assert '(name "OUT"' in text


def test_byte_preservation_only_the_number_literal_changes(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    original = library.read_text(encoding="utf-8")

    set_symbol_pin(library, "Plain", "P$1", new_number="1")

    edited = library.read_text(encoding="utf-8")
    original_lines = original.splitlines()
    edited_lines = edited.splitlines()
    assert len(original_lines) == len(edited_lines)
    differing = [
        (old, new) for old, new in zip(original_lines, edited_lines, strict=True) if old != new
    ]
    assert differing == [('        (number "P$1"', '        (number "1"')]

    other_start = original.index('  (symbol "Other"')
    assert original[other_start:] == edited[edited.index('  (symbol "Other"') :]
    assert '(number "9"' in edited
    assert "(generator kicad_symbol_editor)" in edited


def test_crlf_line_endings_are_preserved(tmp_path):
    library = tmp_path / "crlf.kicad_sym"
    original = SYMBOL_LIBRARY.read_bytes().replace(b"\n", b"\r\n")
    library.write_bytes(original)

    set_symbol_pin(library, "Plain", "P$1", new_number="1")

    edited = library.read_bytes()
    assert b"\r\n" in edited
    assert edited.replace(b"\r\n", b"\n").count(b"\n") == original.count(b"\r\n")
    assert edited == original.replace(b'(number "P$1"', b'(number "1"')


def test_multi_unit_scope_edits_all_units_or_one(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    both = set_symbol_pin(library, "MultiUnit", "1", new_name="RENAMED")
    assert both["occurrences"] == 2
    assert {change["unit"] for change in both["changes"]} == {1, 2}
    assert [change["new_name"] for change in both["changes"]] == ["RENAMED", "RENAMED"]

    one = set_symbol_pin(library, "MultiUnit", "1", new_name="UNIT_TWO", unit=2)
    assert one["occurrences"] == 1
    assert one["changes"] == [
        {
            "unit": 2,
            "style": 1,
            "old_number": "1",
            "old_name": "RENAMED",
            "new_number": "1",
            "new_name": "UNIT_TWO",
        }
    ]


def test_second_call_with_same_values_is_a_noop(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    set_symbol_pin(library, "Plain", "P$1", new_number="1")
    before_bytes = library.read_bytes()
    before_mtime = library.stat().st_mtime_ns
    backup_bytes = Path(str(library) + ".bak").read_bytes()

    result = set_symbol_pin(library, "Plain", "1", new_number="1")

    assert result["changed"] is False
    assert result["occurrences"] == 1
    assert result["backup_path"] is None
    assert library.read_bytes() == before_bytes
    assert library.stat().st_mtime_ns == before_mtime
    assert Path(str(library) + ".bak").read_bytes() == backup_bytes


def test_collision_error_leaves_file_untouched(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError, match="pin number 'P\\$2' is already used by pin 'B' in unit 1"):
        set_symbol_pin(library, "Plain", "P$1", new_number="P$2")

    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


def test_cross_unit_number_is_a_warning_not_an_error(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "MultiUnit", "1", new_number="2", unit=2)

    assert result["changed"] is True
    assert result["warnings"] == ["pin number '2' is also used in unit 1"]


def test_unknown_pin_error_lists_available_numbers(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", "X99", new_number="1")

    message = str(excinfo.value)
    assert message.startswith("pin 'X99' not found in symbol 'Plain'")
    assert "P$1" in message
    assert "P$2" in message
    assert library.read_bytes() == before


def test_extends_symbol_without_pins_is_refused(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Derived", "1", new_number="2")

    assert str(excinfo.value) == (
        "symbol 'Derived' extends 'Plain' and defines no pins; "
        "update the base symbol 'Plain' instead"
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"new_number": None, "new_name": None}, BOTH_NONE),
        ({"new_number": ""}, BAD_NUMBER),
        ({"new_number": "a b"}, BAD_NUMBER),
        ({"new_number": 'a"b'}, BAD_NUMBER),
        ({"new_number": "a(b"}, BAD_NUMBER),
        ({"new_number": "a\\b"}, BAD_NUMBER),
        ({"new_number": "x" * 33}, BAD_NUMBER),
        ({"new_name": 'a"b'}, BAD_NAME),
        ({"new_name": "a(b"}, BAD_NAME),
        ({"new_name": "a\nb"}, BAD_NAME),
        ({"new_name": "a\tb"}, BAD_NAME),
        ({"new_name": "a\\b"}, BAD_NAME),
        ({"new_name": "VCC\\"}, BAD_NAME),
    ],
)
def test_invalid_arguments_are_rejected_before_writing(tmp_path, kwargs, message):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", "P$1", **kwargs)

    assert str(excinfo.value) == message
    assert library.read_bytes() == before


def test_trailing_backslash_name_is_rejected_without_backup(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", "P$1", new_name="VCC\\")

    assert str(excinfo.value) == BAD_NAME
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


def test_backslash_sequence_name_is_rejected_not_sanitised(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    before = library.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", "P$1", new_name="A\\nB")

    assert str(excinfo.value) == BAD_NAME
    assert library.read_bytes() == before
    assert not Path(str(library) + ".bak").exists()


@pytest.mark.parametrize("pin_number", ["", None, 7])
def test_invalid_selector_is_rejected(tmp_path, pin_number):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        set_symbol_pin(library, "Plain", pin_number, new_number="1")

    assert str(excinfo.value) == BAD_SELECTOR


def test_empty_pin_name_and_spaces_are_written_verbatim(tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)

    result = set_symbol_pin(library, "Plain", "P$1", new_name="")

    assert result["changes"][0]["new_name"] == ""
    assert '(name ""' in library.read_text(encoding="utf-8")

    spaced = set_symbol_pin(library, "Plain", "P$2", new_name="PIN OUT")
    assert spaced["changes"][0]["new_name"] == "PIN OUT"
    assert '(name "PIN OUT"' in library.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Library tool
# ---------------------------------------------------------------------------


def test_tool_refreshes_index_after_a_real_edit(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._index_symbol_library",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
    )

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            library_name=_TOOL_LIB,
            symbol_name="Plain",
            pin_number="P$1",
            new_number="1",
        )
    )

    assert result["success"] is True
    assert result["library_name"] == _TOOL_LIB
    assert result["symbol_name"] == "Plain"
    assert result["library_path"] == str(library)
    assert result["occurrences"] == 1
    assert result["changed"] is True
    assert result["indexed"] is True
    assert calls == [((_TOOL_LIB, str(library)), {"project_dir": None, "project": ""})]

    noop = asyncio.run(
        _tools()["update_library_symbol_pin"](
            library_name=_TOOL_LIB,
            symbol_name="Plain",
            pin_number="1",
            new_number="1",
        )
    )
    assert noop["changed"] is False
    assert noop["indexed"] is False
    assert len(calls) == 1


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
            library_name=_TOOL_LIB,
            symbol_name="Plain",
            pin_number="P$1",
            new_number="1",
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


def test_tool_reports_invalid_pin_as_error(monkeypatch, tmp_path):
    library = _copy(SYMBOL_LIBRARY, tmp_path)
    _patch_library(monkeypatch, library)

    result = asyncio.run(
        _tools()["update_library_symbol_pin"](
            library_name=_TOOL_LIB,
            symbol_name="Plain",
            pin_number="nope",
            new_number="1",
        )
    )

    assert "error" in result
    assert "success" not in result
    assert result["error"].startswith("pin 'nope' not found in symbol 'Plain'")


# ---------------------------------------------------------------------------
# Schematic tool
# ---------------------------------------------------------------------------


def test_schematic_tool_edits_cache_only(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    original = schematic.read_text(encoding="utf-8")
    original_bytes = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pin_number="P$1",
            new_number="1",
        )
    )

    assert result["success"] is True
    assert result["lib_id"] == "PinEdit:Plain"
    assert result["symbol_name"] == "Plain"
    assert result["pin_number"] == "1"
    assert result["occurrences"] == 1
    assert result["changes"] == [
        {
            "unit": 1,
            "style": 1,
            "old_number": "P$1",
            "old_name": "A",
            "new_number": "1",
            "new_name": "A",
        }
    ]
    assert result["changed"] is True
    assert result["backup_path"] == str(schematic) + ".bak"
    assert result["affected_references"] == ["R1", "R2"]
    assert result["affected_count"] == 2
    assert result["warnings"] == []

    edited = schematic.read_text(encoding="utf-8")
    assert '(number "P$1"' not in edited
    assert '(number "1"' in edited
    # The unrelated cached symbol is byte-identical.
    c_start = original.index('(symbol "Device:C"')
    c_end = original.index("(wire ", c_start)
    assert original[c_start:c_end] in edited
    assert edited.count('(symbol "Device:C"') == 1
    # Wires, placements, pin uuids and instances are untouched.
    tail_start = original.index("(wire ")
    assert original[tail_start:] == edited[edited.index("(wire ") :]
    # The first edit wrote a backup of the original file.
    assert Path(str(schematic) + ".bak").read_bytes() == original_bytes


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
            pin_number="P$1",
            new_number="1",
        )
    )

    assert result["affected_references"] == ["R1"]
    assert result["affected_count"] == 1


def test_schematic_tool_unknown_lib_id_leaves_file_untouched(tmp_path):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="Missing:Symbol",
            pin_number="1",
            new_number="2",
        )
    )

    assert result == {
        "error": "lib_id 'Missing:Symbol' not found in the schematic's lib_symbols cache"
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
            pin_number="P$1",
            new_number="1",
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
            pin_number="1",
            new_number="1",
        )
    )

    assert result["changed"] is False
    assert result["occurrences"] == 1
    assert result["backup_path"] is None
    assert result["affected_references"] == ["R1", "R2"]
    assert schematic.read_bytes() == before
    assert schematic.stat().st_mtime_ns == before_mtime
    assert backup.read_bytes() == backup_before


@pytest.mark.parametrize("bad_name", ["VCC\\", "A\\nB", "a\\b"])
def test_schematic_tool_rejects_backslash_name_without_writing(tmp_path, bad_name):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pin_number="P$1",
            new_name=bad_name,
        )
    )

    assert result == {"error": BAD_NAME}
    assert schematic.read_bytes() == before
    assert not Path(str(schematic) + ".bak").exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"new_number": None, "new_name": None}, BOTH_NONE),
        ({"new_number": "a b"}, BAD_NUMBER),
        ({"new_name": 'a"b'}, BAD_NAME),
        ({"new_name": "VCC\\"}, BAD_NAME),
    ],
)
def test_schematic_tool_validates_before_writing(tmp_path, kwargs, message):
    schematic = _copy(SCHEMATIC, tmp_path)
    before = schematic.read_bytes()

    result = asyncio.run(
        _tools()["update_schematic_symbol_pin"](
            schematic_path=str(schematic),
            lib_id="PinEdit:Plain",
            pin_number="P$1",
            **kwargs,
        )
    )

    assert result == {"error": message}
    assert schematic.read_bytes() == before
