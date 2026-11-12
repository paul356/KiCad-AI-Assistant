"""Tests for ``update_schematic_symbol_from_library`` (issue #276).

The tool refreshes the schematic's cached ``lib_symbols`` definition from the
indexed library.  All writes happen on temporary copies of the fixtures so the
committed files stay untouched.  The symbol index is replaced by a small fake,
so no KiCad installation or network access is required.
"""

import asyncio
import copy
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
import sexpdata
from sexpdata import Symbol

from kcaa.utils.skip_compat import safe_schematic

FIXTURES = Path(__file__).parent / "fixtures"
LIBRARY = FIXTURES / "refresh_symbols.kicad_sym"
SCHEMATIC = FIXTURES / "refresh.kicad_sch"

LIB_ID = "My:MYOP"
DERIVED_LIB_ID = "My:MYOP_DERIVED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class _Record:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _FakeIndex:
    """Symbol index fake with exact-name library/symbol lookup."""

    def __init__(
        self,
        library_path: Path,
        library_names: tuple[str, ...] = ("My",),
        symbols: tuple[str, ...] = ("MYOP", "MYOP_DERIVED"),
    ):
        self.library_path = library_path
        self.library_names = set(library_names)
        self.symbols = set(symbols)
        self.project_paths: list[str | None] = []

    def get_library_by_name(self, name: str):
        if name not in self.library_names:
            return None
        return _Record(
            file_path=str(self.library_path),
            file_size=0,
            mtime=0.0,
            library_name=name,
        )

    def get_symbol(self, library_name: str, symbol_name: str):
        if library_name not in self.library_names or symbol_name not in self.symbols:
            return None
        return _Record(file_index=0)


@pytest.fixture()
def refresh_env(tmp_path, monkeypatch):
    """Temporary project, library and schematic sharing one fake index."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    library = tmp_path / "refresh_symbols.kicad_sym"
    schematic = tmp_path / "refresh.kicad_sch"
    shutil.copy(LIBRARY, library)
    shutil.copy(SCHEMATIC, schematic)

    index = _FakeIndex(library)

    def _manager(project_path=None):
        index.project_paths.append(project_path)
        return index

    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager",
        _manager,
    )
    return SimpleNamespace(
        project_dir=project_dir,
        library=library,
        schematic=schematic,
        index=index,
        tools=_tools(),
    )


def _run(env, **overrides):
    kwargs = {
        "schematic_path": str(env.schematic),
        "project_dir": str(env.project_dir),
        "lib_id": LIB_ID,
        "dry_run": False,
    }
    kwargs.update(overrides)
    return asyncio.run(env.tools["update_schematic_symbol_from_library"](**kwargs))


def _keyword(node) -> str:
    return str(node[0]) if isinstance(node, list) and node else ""


def _child(node, keyword: str):
    for child in node[1:]:
        if _keyword(child) == keyword:
            return child
    return None


def _load_library(path: Path) -> list:
    return sexpdata.loads(path.read_text(encoding="utf-8"))


def _save_library(path: Path, tree: list) -> None:
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")


def _library_symbol(tree: list, name: str) -> list:
    for node in tree[1:]:
        if _keyword(node) == "symbol" and len(node) > 1 and node[1] == name:
            return node
    raise AssertionError(f"symbol {name!r} not found in library fixture")


def _sub_symbols(symbol: list) -> dict[tuple[int, int], list]:
    """Return ``{(unit, style): node}`` for a library symbol definition."""
    result = {}
    # The cache stores a qualified top-level name while unit sub-symbols keep
    # the plain ``<symbol>_<unit>_<style>`` form.
    plain_name = str(symbol[1]).split(":", 1)[-1]
    prefix = f"{plain_name}_"
    for child in symbol[2:]:
        if _keyword(child) != "symbol" or not isinstance(child[1], str):
            continue
        suffix = child[1][len(prefix) :]
        if not child[1].startswith(prefix):
            continue
        parts = suffix.split("_")
        if len(parts) != 2:
            continue
        result[(int(parts[0]), int(parts[1]))] = child
    return result


def _pins(symbol: list) -> list[list]:
    return [
        pin for sub in _sub_symbols(symbol).values() for pin in sub[2:] if _keyword(pin) == "pin"
    ]


def _pin_by_number(symbol: list, number: str) -> list:
    for pin in _pins(symbol):
        if str(_child(pin, "number")[1]) == number:
            return pin
    raise AssertionError(f"pin {number!r} not found")


def _rename_pin(path: Path, symbol_name: str, number: str, new_name: str) -> None:
    tree = _load_library(path)
    pin = _pin_by_number(_library_symbol(tree, symbol_name), number)
    for part in pin[2:]:
        if _keyword(part) == "name":
            part[1] = new_name
    _save_library(path, tree)


def _renumber_pin(path: Path, symbol_name: str, old: str, new: str) -> None:
    tree = _load_library(path)
    pin = _pin_by_number(_library_symbol(tree, symbol_name), old)
    _child(pin, "number")[1] = new
    _save_library(path, tree)


def _move_pin(path: Path, symbol_name: str, number: str, x: float, y: float, angle: int) -> None:
    tree = _load_library(path)
    pin = _pin_by_number(_library_symbol(tree, symbol_name), number)
    at = _child(pin, "at")
    at[1], at[2], at[3] = x, y, angle
    _save_library(path, tree)


def _remove_pin(path: Path, symbol_name: str, number: str) -> None:
    tree = _load_library(path)
    symbol = _library_symbol(tree, symbol_name)
    for sub in _sub_symbols(symbol).values():
        for index, pin in enumerate(sub[2:], start=2):
            if _keyword(pin) != "pin":
                continue
            if str(_child(pin, "number")[1]) == number:
                del sub[index]
                _save_library(path, tree)
                return
    raise AssertionError(f"pin {number!r} not found")


def _add_rectangle(path: Path, symbol_name: str, unit: int, style: int) -> None:
    tree = _load_library(path)
    sub = _sub_symbols(_library_symbol(tree, symbol_name))[(unit, style)]
    sub.append(
        [
            Symbol("rectangle"),
            [Symbol("start"), -1.27, 1.27],
            [Symbol("end"), 1.27, -1.27],
            [Symbol("stroke"), [Symbol("width"), 0.254], [Symbol("type"), Symbol("default")]],
            [Symbol("fill"), [Symbol("type"), Symbol("none")]],
        ]
    )
    _save_library(path, tree)


def _change_property(path: Path, symbol_name: str, name: str, value: str) -> None:
    tree = _load_library(path)
    symbol = _library_symbol(tree, symbol_name)
    for child in symbol[2:]:
        if _keyword(child) == "property" and child[1] == name:
            child[2] = value
            _save_library(path, tree)
            return
    raise AssertionError(f"property {name!r} not found")


def _ensure_sub_symbol(symbol: list, unit: int, style: int) -> list:
    """Return (creating if needed) a unit/style sub-symbol node."""
    plain_name = str(symbol[1]).split(":", 1)[-1]
    name = f"{plain_name}_{unit}_{style}"
    for child in symbol[2:]:
        if _keyword(child) == "symbol" and child[1] == name:
            return child
    node = [Symbol("symbol"), name]
    insert_at = len(symbol)
    for index, child in enumerate(symbol[2:], start=2):
        if _keyword(child) == "embedded_fonts":
            insert_at = index
            break
    symbol.insert(insert_at, node)
    return node


def _new_pin_node(
    number: str,
    name: str,
    x: float,
    y: float,
    angle: int,
    pin_type: str = "passive",
    shape: str = "line",
    length: float = 2.54,
    hidden: bool = False,
) -> list:
    effects = [Symbol("effects"), [Symbol("font"), [Symbol("size"), 1.27, 1.27]]]
    node = [
        Symbol("pin"),
        Symbol(pin_type),
        Symbol(shape),
        [Symbol("at"), x, y, angle],
        [Symbol("length"), length],
        [Symbol("name"), name, copy.deepcopy(effects)],
        [Symbol("number"), number, copy.deepcopy(effects)],
    ]
    if hidden:
        node.append([Symbol("hide"), Symbol("yes")])
    return node


def _add_library_pin(
    path: Path,
    symbol_name: str,
    unit: int,
    style: int,
    number: str,
    name: str,
    x: float,
    y: float,
    angle: int,
    **pin_fields,
) -> None:
    tree = _load_library(path)
    sub = _ensure_sub_symbol(_library_symbol(tree, symbol_name), unit, style)
    sub.append(_new_pin_node(number, name, x, y, angle, **pin_fields))
    _save_library(path, tree)


def _add_cached_pin(
    path: Path,
    lib_id: str,
    unit: int,
    style: int,
    number: str,
    name: str,
    x: float,
    y: float,
    angle: int,
    **pin_fields,
) -> None:
    tree = sexpdata.loads(path.read_text(encoding="utf-8"))
    lib_symbols = next(node for node in tree[1:] if _keyword(node) == "lib_symbols")
    cached = next(
        child for child in lib_symbols[1:] if _keyword(child) == "symbol" and child[1] == lib_id
    )
    sub = _ensure_sub_symbol(cached, unit, style)
    sub.append(_new_pin_node(number, name, x, y, angle, **pin_fields))
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")


def _pin_at_number(
    symbol: list,
    number: str,
    x: float,
    y: float,
) -> list:
    for pin in _pins(symbol):
        at = _child(pin, "at")
        if (
            str(_child(pin, "number")[1]) == number
            and float(at[1]) == float(x)
            and float(at[2]) == float(y)
        ):
            return pin
    raise AssertionError(f"pin {number!r} at ({x}, {y}) not found")


def _renumber_pin_at(
    path: Path,
    symbol_name: str,
    old: str,
    new: str,
    x: float,
    y: float,
) -> None:
    tree = _load_library(path)
    pin = _pin_at_number(_library_symbol(tree, symbol_name), old, x, y)
    _child(pin, "number")[1] = new
    _save_library(path, tree)


def _change_pin_fields(
    path: Path,
    symbol_name: str,
    number: str,
    *,
    pin_type: str | None = None,
    shape: str | None = None,
    length: float | None = None,
    hidden: bool | None = None,
) -> None:
    tree = _load_library(path)
    pin = _pin_by_number(_library_symbol(tree, symbol_name), number)
    if pin_type is not None:
        pin[1] = Symbol(pin_type)
    if shape is not None:
        pin[2] = Symbol(shape)
    if length is not None:
        _child(pin, "length")[1] = length
    if hidden is True and _child(pin, "hide") is None:
        pin.append([Symbol("hide"), Symbol("yes")])
    elif hidden is False and _child(pin, "hide") is not None:
        pin.remove(_child(pin, "hide"))
    _save_library(path, tree)


def _reorder_properties(path: Path, symbol_name: str, first: str, second: str) -> None:
    tree = _load_library(path)
    symbol = _library_symbol(tree, symbol_name)
    nodes = {str(child[1]): child for child in symbol[2:] if _keyword(child) == "property"}
    first_index = next(i for i, child in enumerate(symbol) if child is nodes[first])
    second_index = next(i for i, child in enumerate(symbol) if child is nodes[second])
    symbol[first_index], symbol[second_index] = symbol[second_index], symbol[first_index]
    _save_library(path, tree)


def _non_lib_symbols_snapshot(path: Path) -> list[str]:
    """Serialize every top-level node except ``lib_symbols``."""
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    return [sexpdata.dumps(node) for node in data[1:] if _keyword(node) != "lib_symbols"]


def _add_mirrored_unit2_placement(schematic: Path) -> None:
    """Add a unit-2 placement rotated 90° with a Y mirror."""
    tree = sexpdata.loads(schematic.read_text(encoding="utf-8"))
    source = None
    for node in tree[1:]:
        if _keyword(node) != "symbol" or "U2" not in {
            str(prop[2]) for prop in node[2:] if _keyword(prop) == "property"
        }:
            continue
        source = copy.deepcopy(node)
        break
    assert source is not None
    _child(source, "at")[1], _child(source, "at")[2] = 150, 120
    _child(source, "unit")[1] = 2
    _child(source, "uuid")[1] = "cc000000-0000-4000-8000-000000000001"
    source.insert(2, [Symbol("mirror"), Symbol("y")])
    for prop in source[2:]:
        if _keyword(prop) == "property" and prop[1] == "Reference":
            prop[2] = "U3"
    for child in list(source):
        if _keyword(child) == "pin":
            source.remove(child)
    source.insert(
        next(i for i, child in enumerate(source) if _keyword(child) == "instances"),
        [Symbol("pin"), "3", [Symbol("uuid"), "cc000000-0000-4000-8000-000000000002"]],
    )
    for instances in source[2:]:
        if _keyword(instances) != "instances":
            continue
        path_node = _child(instances, "project")
        if path_node is not None:
            path_node = _child(path_node, "path")
            if path_node is not None:
                _child(path_node, "reference")[1] = "U3"
                _child(path_node, "unit")[1] = 2
    sheet_index = next(i for i, node in enumerate(tree) if _keyword(node) == "sheet_instances")
    tree.insert(sheet_index, source)
    schematic.write_text(sexpdata.dumps(tree), encoding="utf-8")


def _renumber_all_at(
    path: Path,
    symbol_name: str,
    old: str,
    new: str,
    x: float,
    y: float,
) -> None:
    """Renumber every occurrence of *old* at ``(x, y)``."""
    tree = _load_library(path)
    changed = 0
    for pin in _pins(_library_symbol(tree, symbol_name)):
        at = _child(pin, "at")
        if (
            str(_child(pin, "number")[1]) == old
            and float(at[1]) == float(x)
            and float(at[2]) == float(y)
        ):
            _child(pin, "number")[1] = new
            changed += 1
    assert changed
    _save_library(path, tree)


def _add_placement_pin_key(
    schematic: Path,
    reference: str,
    number: str,
    uuid_value: str,
) -> None:
    tree = sexpdata.loads(schematic.read_text(encoding="utf-8"))
    for node in tree[1:]:
        if _keyword(node) != "symbol":
            continue
        properties = {
            str(prop[1]): str(prop[2]) for prop in node[2:] if _keyword(prop) == "property"
        }
        if properties.get("Reference") != reference:
            continue
        if any(_keyword(child) == "pin" and str(child[1]) == number for child in node[2:]):
            return
        insert_at = next(
            (i for i, child in enumerate(node) if _keyword(child) == "instances"),
            len(node),
        )
        node.insert(insert_at, [Symbol("pin"), number, [Symbol("uuid"), uuid_value]])
        schematic.write_text(sexpdata.dumps(tree), encoding="utf-8")
        return
    raise AssertionError(f"placement {reference!r} not found")


def _raw_cache(sch) -> dict[str, list]:
    from kcaa.tools.symbol_edit_tools import _extract_cached_raw_entries

    return dict(_extract_cached_raw_entries(sch))


def _placement_snapshot(path: Path) -> list[dict]:
    """Semantic snapshot of every placed symbol (touches no caches)."""
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    placements = []
    for node in data[1:]:
        if _keyword(node) != "symbol":
            continue
        properties = {
            str(prop[1]): str(prop[2]) for prop in node[2:] if _keyword(prop) == "property"
        }
        placements.append(
            {
                "lib_id": str(_child(node, "lib_id")[1]),
                "at": list(_child(node, "at")[1:]),
                "unit": str(_child(node, "unit")[1]),
                "body_style": str(_child(node, "body_style")[1]),
                "mirror": (
                    sexpdata.dumps(_child(node, "mirror"))
                    if _child(node, "mirror") is not None
                    else None
                ),
                "uuid": str(_child(node, "uuid")[1]),
                "properties": properties,
                "pins": [
                    (str(child[1]), str(_child(child, "uuid")[1]))
                    for child in node[2:]
                    if _keyword(child) == "pin"
                ],
                "instances": sexpdata.dumps(_child(node, "instances")),
            }
        )
    return placements


def _cache_entry(sch, lib_id: str) -> list:
    return _raw_cache(sch)[lib_id]


def _assert_unrelated_untouched(path: Path, before: list[dict]) -> None:
    after = _placement_snapshot(path)
    unrelated_before = [entry for entry in before if entry["lib_id"] == "Other:UNRELATED"]
    unrelated_after = [entry for entry in after if entry["lib_id"] == "Other:UNRELATED"]
    assert unrelated_after == unrelated_before


# ---------------------------------------------------------------------------
# Successful refresh and diff reporting
# ---------------------------------------------------------------------------


def test_pin_name_change_updates_cache_only(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["success"] is True, result
    assert result["changed"] is True
    assert result["pins_renamed"] == [
        {"number": "1", "old_name": "IN", "new_name": "ANODE", "unit": 1, "style": 1}
    ]
    assert result["placements_updated"] == 0
    assert result["warnings"] == []
    assert Path(result["backup_path"]).exists()

    sch = safe_schematic(str(refresh_env.schematic))
    pin = _pin_by_number(_cache_entry(sch, LIB_ID), "1")
    assert str(_child(pin, "name")[1]) == "ANODE"
    assert _placement_snapshot(refresh_env.schematic) == before


def test_pin_renumbering_rewrites_placement_keys_and_keeps_uuids(refresh_env):
    _renumber_pin(refresh_env.library, "MYOP", "1", "P$1")
    _renumber_pin(refresh_env.library, "MYOP", "2", "P$2")
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["affected_references"] == ["U1", "U2"]
    assert result["affected_count"] == 2
    assert result["placements_updated"] == 2
    assert result["warnings"] == []
    assert {(item["old_number"], item["new_number"]) for item in result["pins_renumbered"]} == {
        ("1", "P$1"),
        ("2", "P$2"),
    }

    after = _placement_snapshot(refresh_env.schematic)
    assert [entry["pins"] for entry in after] == [
        [
            ("P$1", "a0b1c2d3-e4f5-4607-8899-aabbccddee01"),
            ("P$2", "e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44"),
        ],
        [
            ("P$1", "1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5a66"),
            ("P$2", "2b3c4d5e-6f70-4819-0a1b-2c3d4e5f6b77"),
        ],
        [("1", "7b1c2d3e-4f50-4617-8829-3a4b5c6d7e01")],
    ]
    # Everything except the pin keys is untouched.
    for old, new in zip(before, after, strict=True):
        assert {key: value for key, value in old.items() if key != "pins"} == {
            key: value for key, value in new.items() if key != "pins"
        }


def test_cross_unit_renumber_does_not_touch_other_units(refresh_env):
    # The same number exists in unit 1 and unit 2; only unit 2 is renumbered.
    _add_library_pin(refresh_env.library, "MYOP", 2, 1, "1", "ALT", 0.0, -7.62, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 2, 1, "1", "ALT", 0.0, -7.62, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "9", 0.0, -7.62)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["pins_renumbered"] == [
        {"old_number": "1", "new_number": "9", "name": "ALT", "unit": 2, "style": 1}
    ]
    assert result["placements_updated"] == 0
    assert any(
        "pin renumbering for unit 2, style 1 was not applied: "
        "no placement uses that unit/style and no pin keys needed rewriting" in warning
        for warning in result["warnings"]
    )
    placements = _placement_snapshot(refresh_env.schematic)
    assert [entry["pins"] for entry in placements[:2]] == [
        [
            ("1", "a0b1c2d3-e4f5-4607-8899-aabbccddee01"),
            ("2", "e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44"),
        ],
        [
            ("1", "1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5a66"),
            ("2", "2b3c4d5e-6f70-4819-0a1b-2c3d4e5f6b77"),
        ],
    ]


def test_unit0_renumber_does_not_rewrite_other_scopes(refresh_env):
    # Pin "1" exists in unit 0 and unit 1; only the unit-0 copy is renumbered.
    _add_library_pin(refresh_env.library, "MYOP", 0, 1, "1", "COMMON", 0.0, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 0, 1, "1", "COMMON", 0.0, 0.0, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "9", 0.0, 0.0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert any("conflicting renumber targets" in w for w in result["warnings"])
    assert _placement_snapshot(refresh_env.schematic) == before


def test_unit0_renumber_applies_when_no_other_scope_shares_number(refresh_env):
    _add_library_pin(refresh_env.library, "MYOP", 0, 1, "5", "COMMON", 0.0, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 0, 1, "5", "COMMON", 0.0, 0.0, 0)
    _add_placement_pin_key(refresh_env.schematic, "U1", "5", "ee000000-0000-4000-8000-000000000001")
    _add_placement_pin_key(refresh_env.schematic, "U2", "5", "ee000000-0000-4000-8000-000000000002")
    _renumber_pin_at(refresh_env.library, "MYOP", "5", "9", 0.0, 0.0)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is False
    assert result["placements_updated"] == 2
    assert result["pins_renumbered"] == [
        {"old_number": "5", "new_number": "9", "name": "COMMON", "unit": 0, "style": 1}
    ]
    placements = _placement_snapshot(refresh_env.schematic)
    assert placements[0]["pins"] == [
        ("1", "a0b1c2d3-e4f5-4607-8899-aabbccddee01"),
        ("2", "e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44"),
        ("9", "ee000000-0000-4000-8000-000000000001"),
    ]
    assert placements[1]["pins"][-1] == ("9", "ee000000-0000-4000-8000-000000000002")


def test_stacked_rename_shadowed_by_number_key_is_reported(refresh_env):
    _add_library_pin(refresh_env.library, "MYOP", 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["backup_path"] is not None
    assert result["pins_added"] == []
    assert result["pins_removed"] == []
    assert result["pins_renamed"] == []
    assert result["pins_moved"] == []
    assert result["pins_modified"] == []
    assert len(result["pins_unclassified"]) == 2
    assert {entry["old"]["name"] for entry in result["pins_unclassified"] if entry["old"]} == {"IN"}
    assert {entry["new"]["name"] for entry in result["pins_unclassified"] if entry["new"]} == {
        "ANODE"
    }
    assert _placement_snapshot(refresh_env.schematic) == before
    sch = safe_schematic(str(refresh_env.schematic))
    names = [
        str(_child(pin, "name")[1])
        for pin in _pins(_cache_entry(sch, LIB_ID))
        if str(_child(pin, "number")[1]) == "1"
    ]
    assert names == ["ANODE", "STACK"]


def test_fully_renumbered_stacked_pins_rewrite_keys(refresh_env):
    _add_library_pin(refresh_env.library, "MYOP", 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _renumber_all_at(refresh_env.library, "MYOP", "1", "9", -7.62, 0.0)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is False
    assert result["placements_updated"] == 2
    assert result["pins_renumbered"] == [
        {"old_number": "1", "new_number": "9", "name": "IN", "unit": 1, "style": 1}
    ]
    placements = _placement_snapshot(refresh_env.schematic)
    assert placements[0]["pins"] == [
        ("9", "a0b1c2d3-e4f5-4607-8899-aabbccddee01"),
        ("2", "e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44"),
    ]
    assert placements[1]["pins"] == [
        ("9", "1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5a66"),
        ("2", "2b3c4d5e-6f70-4819-0a1b-2c3d4e5f6b77"),
    ]
    assert not any("was removed from the library" in w for w in result["warnings"])


def test_partially_renumbered_stacked_pins_are_refused(refresh_env):
    _add_library_pin(refresh_env.library, "MYOP", 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 1, 1, "1", "STACK", -7.62, 0.0, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "9", -7.62, 0.0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert result["pins_renumbered"] == []
    assert any("could not infer a unique pin renumbering" in w for w in result["warnings"])
    assert _placement_snapshot(refresh_env.schematic) == before


def test_renumber_target_colliding_with_retained_pin_is_rejected(refresh_env):
    # Old 1@-7.62 and 2@7.62; the library drops 2 and renumbers 1 to 2.
    _remove_pin(refresh_env.library, "MYOP", "2")
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "2", -7.62, 0.0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert result["pins_renumbered"] == []
    assert (
        "could not infer a unique pin renumbering mapping; "
        "placement pin keys were left unchanged" in result["warnings"]
    )
    assert any(
        "pin number '1'" in warning
        and "may have been renumbered and the mapping was rejected" in warning
        for warning in result["warnings"]
    )
    assert not any(
        "pin '1'" in warning and "was removed from the library" in warning
        for warning in result["warnings"]
    )
    assert not any("moved from" in warning for warning in result["warnings"])
    assert _placement_snapshot(refresh_env.schematic) == before


def test_common_and_own_unit_conflict_skips_the_placement(refresh_env):
    _add_library_pin(refresh_env.library, "MYOP", 0, 1, "1", "COMMON", 0.0, 0.0, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 0, 1, "1", "COMMON", 0.0, 0.0, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "9", 0.0, 0.0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "10", -7.62, 0.0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert any("conflicting renumber targets" in w for w in result["warnings"])
    assert _placement_snapshot(refresh_env.schematic) == before


def test_renumber_to_a_retained_identity_number_is_rejected(refresh_env):
    # Pin 2 -> 3 while another pin keeps number 3 in the same unit/style.
    _add_library_pin(refresh_env.library, "MYOP", 1, 1, "3", "KEEP", 0.0, -10.16, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 1, 1, "3", "KEEP", 0.0, -10.16, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "2", "3", 7.62, 0.0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert result["pins_renumbered"] == []
    assert _placement_snapshot(refresh_env.schematic) == before


def test_duplicate_number_renumber_is_rejected(refresh_env):
    # Two pins numbered "1" in unit 1; only the extra one is renumbered.
    _add_library_pin(refresh_env.library, "MYOP", 1, 1, "1", "STACK", 0.0, -10.16, 0)
    _add_cached_pin(refresh_env.schematic, LIB_ID, 1, 1, "1", "STACK", 0.0, -10.16, 0)
    _renumber_pin_at(refresh_env.library, "MYOP", "1", "9", 0.0, -10.16)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is True
    assert result["placements_updated"] == 0
    assert result["pins_renumbered"] == []
    assert result["warnings"] == [
        "could not infer a unique pin renumbering mapping; placement pin keys were left unchanged"
    ]
    assert _placement_snapshot(refresh_env.schematic) == before


def test_renumber_is_idempotent_and_creates_one_backup(refresh_env):
    _renumber_pin(refresh_env.library, "MYOP", "1", "P$1")

    first = _run(refresh_env)
    assert first["changed"] is True
    second = _run(refresh_env)

    assert second["changed"] is False
    assert second["placements_updated"] == 0
    assert len(list(refresh_env.schematic.parent.glob("*.bak"))) == 1
    assert not list(refresh_env.schematic.parent.glob("*.tmp"))


def test_pin_type_shape_length_and_hide_changes_are_reported(refresh_env):
    _change_pin_fields(
        refresh_env.library,
        "MYOP",
        "1",
        pin_type="output",
        shape="inverted",
        length=3.81,
        hidden=True,
    )

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["renumber_ambiguous"] is False
    assert result["pins_added"] == []
    assert result["pins_removed"] == []
    assert result["pins_renamed"] == []
    assert result["pins_moved"] == []
    assert result["graphics_changed"] is False
    assert {(item["field"], item["old"], item["new"]) for item in result["pins_modified"]} == {
        ("type", "input", "output"),
        ("shape", "line", "inverted"),
        ("length", 2.54, 3.81),
        ("hide", False, True),
    }
    assert {item["number"] for item in result["pins_modified"]} == {"1"}


def test_property_reordering_is_reported(refresh_env):
    _reorder_properties(refresh_env.library, "MYOP", "Reference", "Value")

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["properties_reordered"] is True
    assert result["properties_changed"] == []
    assert result["properties_added"] == []
    assert result["properties_removed"] == []


def test_apply_and_save_failures_are_reported_distinctly(refresh_env, monkeypatch):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")
    original = refresh_env.schematic.read_bytes()

    def _raise(exc: Exception):
        def _fail(*args, **kwargs):
            raise exc

        return _fail

    with monkeypatch.context() as patcher:
        patcher.setattr(
            "kcaa.tools.symbol_edit_tools._replace_cached_lib_symbol",
            _raise(ValueError("bad tree")),
        )
        apply_failure = _run(refresh_env)
    assert apply_failure["error"].startswith("Failed to apply symbol definition:")
    assert "bad tree" in apply_failure["error"]
    assert refresh_env.schematic.read_bytes() == original

    with monkeypatch.context() as patcher:
        patcher.setattr(
            "kcaa.tools.symbol_edit_tools.save_schematic",
            _raise(OSError("disk full")),
        )
        save_failure = _run(refresh_env)
    assert save_failure["error"].startswith("Failed to save schematic")
    assert "target file was not replaced" in save_failure["error"]
    assert "disk full" in save_failure["error"]


def test_graphics_only_change_updates_cache(refresh_env):
    _add_rectangle(refresh_env.library, "MYOP", unit=0, style=1)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["graphics_changed"] is True
    assert result["pins_added"] == []
    assert result["pins_removed"] == []
    sch = safe_schematic(str(refresh_env.schematic))
    cached = _cache_entry(sch, LIB_ID)
    assert "rectangle" in [str(child[0]) for child in _sub_symbols(cached)[(0, 1)][1:]]
    assert _placement_snapshot(refresh_env.schematic) == before


def test_property_change_updates_definition_not_placements(refresh_env):
    _change_property(refresh_env.library, "MYOP", "Value", "MYOP_V2")

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["properties_changed"] == ["Value"]
    assert result["graphics_changed"] is False
    sch = safe_schematic(str(refresh_env.schematic))
    cached_props = {
        str(child[1]): str(child[2])
        for child in _cache_entry(sch, LIB_ID)[2:]
        if _keyword(child) == "property"
    }
    assert cached_props["Value"] == "MYOP_V2"
    # Placement fields are a deliberate non-goal: U1 keeps its own Value.
    placements = _placement_snapshot(refresh_env.schematic)
    assert [entry["properties"]["Value"] for entry in placements] == [
        "MYOP",
        "MYOP",
        "UNRELATED",
    ]


def test_no_differences_writes_nothing(refresh_env):
    original = refresh_env.schematic.read_bytes()

    result = _run(refresh_env)

    assert result["success"] is True
    assert result["changed"] is False
    assert result["backup_path"] is None
    assert result["placements_updated"] == 0
    assert not Path(str(refresh_env.schematic) + ".bak").exists()
    assert not Path(str(refresh_env.schematic) + ".tmp").exists()
    assert refresh_env.schematic.read_bytes() == original


def test_dry_run_reports_diff_without_writing(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")
    original = refresh_env.schematic.read_bytes()

    result = _run(refresh_env, dry_run=True)

    assert result["changed"] is True
    assert result["dry_run"] is True
    assert result["backup_path"] is None
    assert result["pins_renamed"][0]["new_name"] == "ANODE"
    assert not Path(str(refresh_env.schematic) + ".bak").exists()
    assert not Path(str(refresh_env.schematic) + ".tmp").exists()
    assert refresh_env.schematic.read_bytes() == original


def test_dry_run_preview_matches_the_real_run(refresh_env):
    _renumber_pin(refresh_env.library, "MYOP", "1", "P$1")
    original = refresh_env.schematic.read_bytes()

    preview = _run(refresh_env, dry_run=True)
    applied = _run(refresh_env)

    assert refresh_env.schematic.read_bytes() != original
    for key in (
        "changed",
        "pins_added",
        "pins_removed",
        "pins_renamed",
        "pins_renumbered",
        "pins_moved",
        "pins_modified",
        "pins_unclassified",
        "properties_added",
        "properties_removed",
        "properties_changed",
        "properties_reordered",
        "graphics_changed",
        "renumber_ambiguous",
        "affected_references",
        "affected_count",
        "placements_updated",
    ):
        assert applied[key] == preview[key], key


def test_second_run_is_idempotent(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")

    first = _run(refresh_env)
    assert first["changed"] is True
    second = _run(refresh_env)

    assert second["success"] is True
    assert second["changed"] is False
    assert second["backup_path"] is None
    assert second["placements_updated"] == 0


def test_multi_unit_sub_symbols_survive_refresh(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "3", "VDD")

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["pins_renamed"] == [
        {"number": "3", "old_name": "VCC", "new_name": "VDD", "unit": 2, "style": 1}
    ]
    sch = safe_schematic(str(refresh_env.schematic))
    sub_symbols = _sub_symbols(_cache_entry(sch, LIB_ID))
    assert set(sub_symbols) == {(0, 1), (1, 1), (2, 1)}
    assert len(_pins(_cache_entry(sch, LIB_ID))) == 3


# ---------------------------------------------------------------------------
# Placement key edge cases
# ---------------------------------------------------------------------------


def test_removed_pin_keeps_placement_key_and_warns(refresh_env):
    _remove_pin(refresh_env.library, "MYOP", "2")

    result = _run(refresh_env)

    assert result["changed"] is True
    assert result["pins_removed"] == [{"number": "2", "name": "OUT", "unit": 1, "style": 1}]
    assert any("removed from the library" in warning for warning in result["warnings"])
    assert result["placements_updated"] == 0
    placements = _placement_snapshot(refresh_env.schematic)
    assert placements[0]["pins"] == [
        ("1", "a0b1c2d3-e4f5-4607-8899-aabbccddee01"),
        ("2", "e3c9a2b7-8d4f-4e3a-9b6c-2f0a1d5b8e44"),
    ]
    sch = safe_schematic(str(refresh_env.schematic))
    assert "2" not in {str(_child(pin, "number")[1]) for pin in _pins(_cache_entry(sch, LIB_ID))}


def test_moved_pin_warns_with_coordinates_and_leaves_placement(refresh_env):
    _move_pin(refresh_env.library, "MYOP", "1", -8.89, 0.0, 0)
    before = _placement_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result["pins_moved"] == [
        {
            "number": "1",
            "name": "IN",
            "old_at": [-7.62, 0.0, 0.0],
            "new_at": [-8.89, 0.0, 0.0],
            "unit": 1,
            "style": 1,
        }
    ]
    moved_warning = next(w for w in result["warnings"] if "moved from" in w)
    assert "-7.62" in moved_warning
    assert "-8.89" in moved_warning
    assert "wires were not moved" in moved_warning
    assert result["placements_updated"] == 0
    assert _placement_snapshot(refresh_env.schematic) == before


# ---------------------------------------------------------------------------
# Resolution errors
# ---------------------------------------------------------------------------


def test_unknown_cached_lib_id_lists_available_ids(refresh_env):
    result = _run(refresh_env, lib_id="Nope:Nope")

    assert "error" in result
    assert "success" not in result
    assert "Nope:Nope" in result["error"]
    assert "'My:MYOP'" in result["error"]
    assert "'Other:UNRELATED'" in result["error"]


def test_unknown_library_or_symbol_is_reported_with_scope(refresh_env, monkeypatch):
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager",
        lambda project_path=None: _FakeIndex(refresh_env.library, library_names=()),
    )
    unknown_library = _run(refresh_env)
    assert "error" in unknown_library
    assert "My" in unknown_library["error"]
    assert "MYOP" in unknown_library["error"]
    assert "searched:" in unknown_library["error"]
    assert str(refresh_env.project_dir) in unknown_library["error"]

    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager",
        lambda project_path=None: _FakeIndex(refresh_env.library, symbols=("OTHER",)),
    )
    unknown_symbol = _run(refresh_env)
    assert "error" in unknown_symbol
    assert "My" in unknown_symbol["error"]
    assert "MYOP" in unknown_symbol["error"]


def test_symdir_library_form_is_resolved(refresh_env, monkeypatch):
    library_names = (f"My/{LIB_ID.split(':', 1)[1]}",)
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager",
        lambda project_path=None: _FakeIndex(refresh_env.library, library_names=library_names),
    )
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")

    result = _run(refresh_env)

    assert result.get("success") is True, result
    assert result["changed"] is True
    assert result["warnings"] == []


def test_project_dir_scopes_every_index_query(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")

    result = _run(refresh_env)

    assert result.get("success") is True, result
    assert refresh_env.index.project_paths
    assert set(refresh_env.index.project_paths) == {str(refresh_env.project_dir)}


def test_bare_lib_id_without_colon_is_rejected(refresh_env):
    # Store the cached definition under a bare id, as some schematics do.
    tree = sexpdata.loads(refresh_env.schematic.read_text(encoding="utf-8"))
    lib_symbols = next(node for node in tree[1:] if _keyword(node) == "lib_symbols")
    for child in lib_symbols[1:]:
        if _keyword(child) == "symbol" and child[1] == LIB_ID:
            child[1] = "MYOP"
    refresh_env.schematic.write_text(sexpdata.dumps(tree), encoding="utf-8")

    result = _run(refresh_env, lib_id="MYOP")

    assert "error" in result
    assert "no library nickname" in result["error"]
    assert "Library:Symbol" in result["error"]


def test_wrong_extension_is_rejected(refresh_env, tmp_path):
    wrong = tmp_path / "refresh.txt"
    shutil.copy(SCHEMATIC, wrong)

    result = _run(refresh_env, schematic_path=str(wrong))

    assert "error" in result
    assert "Not a .kicad_sch file" in result["error"]


def test_missing_file_is_rejected(refresh_env, tmp_path):
    result = _run(refresh_env, schematic_path=str(tmp_path / "missing.kicad_sch"))

    assert "error" in result
    assert "Schematic file not found" in result["error"]


# ---------------------------------------------------------------------------
# Derived symbols and unrelated content
# ---------------------------------------------------------------------------


def _insert_stale_derived_cache(schematic: Path) -> None:
    tree = sexpdata.loads(schematic.read_text(encoding="utf-8"))
    lib_symbols = next(node for node in tree[1:] if _keyword(node) == "lib_symbols")
    lib_symbols.append(
        [
            Symbol("symbol"),
            DERIVED_LIB_ID,
            [Symbol("extends"), "MYOP"],
            [
                Symbol("property"),
                "Reference",
                "U",
                [Symbol("at"), 0, 0, 0],
                [Symbol("effects"), [Symbol("font"), [Symbol("size"), 1.27, 1.27]]],
            ],
            [
                Symbol("property"),
                "Value",
                "MYOP_DERIVED",
                [Symbol("at"), 0, 2.54, 0],
                [Symbol("effects"), [Symbol("font"), [Symbol("size"), 1.27, 1.27]]],
            ],
            [
                Symbol("symbol"),
                "MYOP_DERIVED_1_1",
                [
                    Symbol("pin"),
                    Symbol("input"),
                    Symbol("line"),
                    [Symbol("at"), -7.62, 0, 0],
                    [Symbol("length"), 2.54],
                    [
                        Symbol("name"),
                        "OLD",
                        [Symbol("effects"), [Symbol("font"), [Symbol("size"), 1.27, 1.27]]],
                    ],
                    [
                        Symbol("number"),
                        "1",
                        [Symbol("effects"), [Symbol("font"), [Symbol("size"), 1.27, 1.27]]],
                    ],
                ],
            ],
        ]
    )
    schematic.write_text(sexpdata.dumps(tree), encoding="utf-8")


def test_derived_symbol_is_flattened_into_cache(refresh_env):
    _insert_stale_derived_cache(refresh_env.schematic)

    result = _run(refresh_env, lib_id=DERIVED_LIB_ID)

    assert result.get("success") is True, result
    assert result["changed"] is True
    assert result["library_name"] == "My"
    assert result["affected_count"] == 0

    sch = safe_schematic(str(refresh_env.schematic))
    cached = _cache_entry(sch, DERIVED_LIB_ID)
    assert "extends" not in [str(child[0]) for child in cached[2:]]
    assert set(_sub_symbols(cached)) == {(0, 1), (1, 1), (2, 1)}
    cached_props = {
        str(child[1]): str(child[2]) for child in cached[2:] if _keyword(child) == "property"
    }
    assert cached_props["Value"] == "MYOP_DERIVED"
    assert cached_props["Description"] == "Derived operational amplifier"
    assert cached_props["Datasheet"] == "~"


def test_malformed_extends_property_returns_error(refresh_env):
    _insert_stale_derived_cache(refresh_env.schematic)
    tree = _load_library(refresh_env.library)
    _library_symbol(tree, "MYOP_DERIVED").append([Symbol("property"), Symbol("private")])
    _save_library(refresh_env.library, tree)
    original = refresh_env.schematic.read_bytes()

    result = _run(refresh_env, lib_id=DERIVED_LIB_ID)

    assert "error" in result
    assert result["error"].startswith("Failed to resolve extends chain:")
    assert refresh_env.schematic.read_bytes() == original


def test_rotated_mirrored_non_unit1_placement_is_preserved(refresh_env):
    _add_mirrored_unit2_placement(refresh_env.schematic)
    before = _placement_snapshot(refresh_env.schematic)
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")

    result = _run(refresh_env)

    assert result.get("success") is True, result
    after = _placement_snapshot(refresh_env.schematic)
    assert after == before
    extra = next(entry for entry in after if entry["lib_id"] == LIB_ID and entry["unit"] == "2")
    assert extra["at"] == [150, 120, 90]
    assert extra["mirror"] is not None
    assert extra["pins"] == [("3", "cc000000-0000-4000-8000-000000000002")]


def test_junction_label_and_non_lib_symbols_nodes_survive(refresh_env):
    _rename_pin(refresh_env.library, "MYOP", "1", "ANODE")
    before_sch = safe_schematic(str(refresh_env.schematic))
    unrelated_before = sexpdata.dumps(_cache_entry(before_sch, "Other:UNRELATED"))
    top_level_before = _non_lib_symbols_snapshot(refresh_env.schematic)

    result = _run(refresh_env)

    assert result.get("success") is True, result
    assert _non_lib_symbols_snapshot(refresh_env.schematic) == top_level_before
    after_sch = safe_schematic(str(refresh_env.schematic))
    assert sexpdata.dumps(_cache_entry(after_sch, "Other:UNRELATED")) == unrelated_before
    text = refresh_env.schematic.read_text(encoding="utf-8")
    assert "(junction" in text
    assert '(label "NET_IN"' in text


def test_unrelated_cached_symbol_and_placement_stay_untouched(refresh_env):
    before_sch = safe_schematic(str(refresh_env.schematic))
    unrelated_before = sexpdata.dumps(_cache_entry(before_sch, "Other:UNRELATED"))
    placements_before = _placement_snapshot(refresh_env.schematic)
    _renumber_pin(refresh_env.library, "MYOP", "1", "P$1")

    _run(refresh_env)

    after_sch = safe_schematic(str(refresh_env.schematic))
    unrelated_after = sexpdata.dumps(_cache_entry(after_sch, "Other:UNRELATED"))
    assert unrelated_after == unrelated_before
    _assert_unrelated_untouched(refresh_env.schematic, placements_before)
    text = refresh_env.schematic.read_text(encoding="utf-8")
    assert '(lib_id "Other:UNRELATED")' in text
    assert '(pin "1" (uuid "7b1c2d3e-4f50-4617-8829-3a4b5c6d7e01"))' in text
