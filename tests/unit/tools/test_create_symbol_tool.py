"""Tests for the create_symbol MCP tool (kcaa.tools.symbol_edit_tools).

The tool is a pure library writer: it defines a brand-new lib symbol and
appends it to an existing ``.kicad_sym`` library, never touching a
schematic.  Disk-writing tests run in an isolated environment: the KiCad
config dir, 3rd-party symbols dir, sym-lib-table, and symbol index DB are
all redirected into a per-test tmp_path via monkeypatch, so the real user
environment is never touched.
"""

import asyncio
import math
import os

import pytest
import sexpdata

from kcaa.tools.symbol_edit_tools import (
    _DEFAULT_PIN_LENGTH_MM,
    _build_lib_symbol_raw,
    _do_create_symbol_library,
    _extract_lib_pin_positions,
    _lib_pins_world,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# 2 inputs on the left + 1 output on the right (taskbook example).
PINS_2IN_1OUT = [
    {"number": "1", "name": "IN", "type": "input", "direction": "left"},
    {"number": "2", "name": "IN2", "type": "input", "direction": "left"},
    {"number": "3", "name": "OUT", "type": "output", "direction": "right"},
]

LIB = "TestLib"
LIB_ID = "TestLib:MYOP"


class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.symbol_edit_tools import register_symbol_edit_tools

    mock = _MockMCP()
    register_symbol_edit_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated symbol-library environment with one pre-created lib ``TestLib``.

    Redirects: KiCad config dir -> tmp_path (sym-lib-table lives here),
    ``${KICAD{ver}_3RD_PARTY}`` -> tmp_path/3rdparty, symbol index DB ->
    tmp_path/symbol_test.db.  The library is created via the real
    ``_do_create_symbol_library`` so table registration and indexing are
    exercised end-to-end.
    """
    from kcaa.utils import pcb_library_utils
    from kcaa.utils.config import config

    third_party = tmp_path / "3rdparty"
    third_party.mkdir()
    (third_party / "symbols").mkdir()

    monkeypatch.setattr(pcb_library_utils, "_default_kicad_config_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(config, "_kicad_3rd_party", str(third_party))
    # ${KICAD10_3RD_PARTY} URIs are expanded via os.environ (ServerConfig
    # builds a fresh instance), so set the env var — not just the singleton.
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._3rd_party_symbols_dir",
        lambda: str(third_party / "symbols"),
    )
    # Isolate the symbol index: tools use the module-level singleton, so
    # swap the factory for a temp-DB manager (never the real user DB).
    from kcaa.utils.config import ServerConfig
    from kcaa.utils.symbol_index_manager import SymbolIndexManager
    from kcaa.utils.symbol_index_reader import SymbolIndexReader

    index_mgr = SymbolIndexManager(
        SymbolIndexReader(ServerConfig()), db_path=str(tmp_path / "symbol_test.db")
    )
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager", lambda project_path=None: index_mgr
    )

    created = _do_create_symbol_library(LIB)
    assert "error" not in created, created
    return {
        "tmp_path": str(tmp_path),
        "lib": LIB,
        "lib_path": created["path"],
        "table_path": created["table_path"],
        "index_mgr": index_mgr,
    }


# ---------------------------------------------------------------------------
# Helpers (operate on raw sexpdata from the .kicad_sym file)
# ---------------------------------------------------------------------------


def _lib_symbol_raw(lib_path: str, name: str):
    """Return the raw ``(symbol NAME ...)`` node from a .kicad_sym file."""
    with open(lib_path, encoding="utf-8") as fh:
        data = sexpdata.loads(fh.read())
    for entry in data:
        if (
            isinstance(entry, list)
            and len(entry) >= 2
            and isinstance(entry[0], sexpdata.Symbol)
            and entry[0].value() == "symbol"
            and entry[1] == name
        ):
            return entry
    return None


def _find_unit_node(lib_raw, name: str):
    for child in lib_raw[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "symbol"
            and child[1] == name
        ):
            return child
    return None


def _pin_nodes(lib_raw):
    unit = _find_unit_node(lib_raw, "MYOP_1_1")
    if unit is None:
        unit = _find_unit_node(lib_raw, "MYOP_0_1")
    return [
        child
        for child in unit[2:]
        if (
            isinstance(child, list)
            and len(child) >= 1
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "pin"
        )
    ]


def _pin_at(lib_raw, number: str):
    for node in _pin_nodes(lib_raw):
        for child in node[1:]:
            if (
                isinstance(child, list)
                and len(child) >= 2
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "number"
                and child[1] == number
            ):
                return node
    return None


def _rect_node(lib_raw):
    unit0 = _find_unit_node(lib_raw, "MYOP_0_1")
    assert unit0 is not None
    for child in unit0[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 1
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "rectangle"
        ):
            return child
    return None


def _rect_bounds(lib_raw) -> tuple[float, float, float, float]:
    rect = _rect_node(lib_raw)
    start = end = None
    for child in rect[1:]:
        if (
            isinstance(child, list)
            and len(child) >= 3
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "start"
        ):
            start = (float(child[1]), float(child[2]))
        elif (
            isinstance(child, list)
            and len(child) >= 3
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "end"
        ):
            end = (float(child[1]), float(child[2]))
    assert start is not None and end is not None
    return (start[0], start[1], end[0], end[1])  # min_x, max_y, max_x, min_y


def _properties(lib_raw):
    """Return {name: {text, hide}} for the top-level property nodes.

    Node shape: ``(property "Name" "value" (at ...) ... (hide yes)?)``
    """
    out = {}
    for child in lib_raw[2:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol)
            and child[0].value() == "property"
        ):
            text = child[2] if len(child) >= 3 and isinstance(child[2], str) else ""
            hide = any(
                isinstance(sub, list)
                and sub
                and isinstance(sub[0], sexpdata.Symbol)
                and sub[0].value() == "hide"
                for sub in child[2:]
            )
            out[child[1]] = {"text": text, "hide": hide}
    return out


def _call_create(tools, env, **kwargs):
    if "library" not in kwargs:
        kwargs["library"] = env["lib"]
    return asyncio.run(tools["create_symbol"](**kwargs))


# ---------------------------------------------------------------------------
# Library definition (pure writer — no schematic involvement)
# ---------------------------------------------------------------------------


class TestCreateSymbolDefinition:
    def test_writes_definition_and_reports_metadata(self, tools, env):
        result = _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert result.get("success") is True, result
        assert result["lib_id"] == LIB_ID
        assert result["library"] == LIB
        assert result["library_path"] == env["lib_path"]
        assert result["pin_count"] == 3
        assert result["units_added"] == 1
        assert result["warnings"] == []
        # Pure library writer: no schematic keys at all.
        for key in ("position", "file_modified", "backup_path"):
            assert key not in result, key

    def test_definition_survives_sexpdata_round_trip(self, tools, env):
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        raw = _lib_symbol_raw(env["lib_path"], "MYOP")
        assert raw is not None
        assert len(_pin_nodes(raw)) == 3

    def test_no_placed_units_in_library_entry(self, tools, env):
        """The library entry is a plain definition — no (symbol ...) instances."""
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        raw = _lib_symbol_raw(env["lib_path"], "MYOP")
        unit_names = [
            child[1]
            for child in raw[2:]
            if (
                isinstance(child, list)
                and len(child) >= 2
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "symbol"
            )
        ]
        assert unit_names == ["MYOP_0_1", "MYOP_1_1"]

    def test_properties_present_and_visibility(self, tools, env):
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        props = _properties(_lib_symbol_raw(env["lib_path"], "MYOP"))
        assert list(props)[:4] == ["Reference", "Value", "Footprint", "Datasheet"]
        assert props["Reference"]["text"] == "U"
        assert props["Value"]["text"] == "MYOP"
        assert props["Footprint"]["hide"] is True
        assert props["Datasheet"]["hide"] is True

    def test_value_override_used(self, tools, env):
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT, value="AMPLIFIER")
        props = _properties(_lib_symbol_raw(env["lib_path"], "MYOP"))
        assert props["Value"]["text"] == "AMPLIFIER"

    def test_library_file_backup_created(self, tools, env):
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        # append_symbol_to_library snapshots the library before editing.
        assert os.path.exists(env["lib_path"] + ".bak")

    def test_schematic_untouched(self, tools, env, tmp_path):
        """A plain definition call must not create or modify any schematic."""
        probe = tmp_path / "probe.kicad_sch"
        probe.write_text('(kicad_sch (version 20231120) (generator "probe"))\n')
        before = probe.read_bytes()
        _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert probe.read_bytes() == before
        assert not (tmp_path / "probe.kicad_sch.bak").exists()

    def test_multiple_symbols_append_to_same_library(self, tools, env):
        first = _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert first.get("success") is True
        second = _call_create(
            tools,
            env,
            symbol_name="MYOP2",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "left"}],
        )
        assert second.get("success") is True, second
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert sorted(list_library_symbols(env["lib_path"])) == ["MYOP", "MYOP2"]


# ---------------------------------------------------------------------------
# Lib symbol layout (direct checks on _build_lib_symbol_raw, Y-up lib coords)
# ---------------------------------------------------------------------------


class TestLibSymbolLayout:
    def test_pin_layout_geometry(self):
        raw, warnings = _build_lib_symbol_raw("MYOP", PINS_2IN_1OUT, "U", "MYOP")
        assert warnings == []
        positions = _extract_lib_pin_positions(raw)  # electrical (connection) ends
        lefts = [p for p in positions if p[0] < 0]
        rights = [p for p in positions if p[0] > 0]
        assert len(lefts) == 2
        assert len(rights) == 1
        # Left pins spaced 2.54 mm apart, first pin (IN) above second (IN2).
        lefts_sorted = sorted(lefts, key=lambda p: p[1], reverse=True)
        assert lefts_sorted[0] == (-5.715, 1.27)
        assert lefts_sorted[1] == (-5.715, -1.27)
        assert rights[0] == (5.715, 0.0)
        # Along-side pin offsets stay on the 1.27 mm (50-mil) grid — the strict
        # contract of _side_offsets (2.54 mm pitch, centred).  The body-normal
        # coordinate ±(half_w + pin_length) is necessarily a half-grid multiple
        # for the 6.35 mm body (half_w = 2.5 x 1.27); it is not part of the
        # offset contract, so it must NOT be excused with a 0.635 mm backdoor.
        for px, py in positions:
            if abs(px) > abs(py):  # left/right pin: y is the offset axis
                v = py
            else:  # up/down pin: x is the offset axis
                v = px
            assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, (px, py)

    def test_pin_order_counter_clockwise(self):
        # CCW order: left top->down, down left->right, right bottom->up,
        # up right->left.  Pin numbers in `pins` order must map accordingly.
        pins = [
            {"number": "1", "name": "L1", "type": "input", "direction": "left"},
            {"number": "2", "name": "L2", "type": "input", "direction": "left"},
            {"number": "3", "name": "D1", "type": "input", "direction": "down"},
            {"number": "4", "name": "D2", "type": "input", "direction": "down"},
            {"number": "5", "name": "R1", "type": "output", "direction": "right"},
            {"number": "6", "name": "R2", "type": "output", "direction": "right"},
            {"number": "7", "name": "U1", "type": "output", "direction": "up"},
            {"number": "8", "name": "U2", "type": "output", "direction": "up"},
        ]
        raw, warnings = _build_lib_symbol_raw("MYOP", pins, "U", "MYOP")
        assert warnings == []
        nodes = _pin_nodes(raw)
        node_at = {}
        for node in nodes:
            num = None
            at = None
            for child in node:
                if isinstance(child, list) and child and isinstance(child[0], sexpdata.Symbol):
                    if child[0].value() == "number":
                        num = child[1]
                    elif child[0].value() == "at":
                        at = (float(child[1]), float(child[2]))
            node_at[num] = at
        l1, l2 = node_at["1"], node_at["2"]  # left: topmost first
        assert l1[1] > l2[1]
        d1, d2 = node_at["3"], node_at["4"]  # down: leftmost first
        assert d1[0] < d2[0]
        r1, r2 = node_at["5"], node_at["6"]  # right: bottommost first
        assert r1[1] < r2[1]
        u1, u2 = node_at["7"], node_at["8"]  # up: rightmost first
        assert u1[0] > u2[0]

    def test_inner_ends_land_on_body_edge(self):
        raw, _ = _build_lib_symbol_raw("MYOP", PINS_2IN_1OUT, "U", "MYOP")
        min_x, max_y, max_x, min_y = _rect_bounds(raw)
        # Body defaults: width 6.35, height auto = pin span + 2.54 padding.
        assert (min_x, max_x) == (-3.175, 3.175)
        assert (max_y, min_y) == (2.54, -2.54)
        for node in _pin_nodes(raw):
            for child in node[1:]:
                if (
                    isinstance(child, list)
                    and len(child) >= 2
                    and isinstance(child[0], sexpdata.Symbol)
                    and child[0].value() == "at"
                ):
                    px, py = float(child[1]), float(child[2])
                    angle = int(child[3]) if len(child) >= 4 else 0
                    # Stub inner end = connection point + length along the pin
                    # angle (KiCad convention: angle points tip→body, lib
                    # coords Y-up).  It must land exactly on a body edge —
                    # this is what makes the "inner end exactly on the body
                    # edge" claim in _build_lib_symbol_raw true.
                    rad = math.radians(angle)
                    inner_x = round(px + _DEFAULT_PIN_LENGTH_MM * math.cos(rad), 4)
                    inner_y = round(py + _DEFAULT_PIN_LENGTH_MM * math.sin(rad), 4)
                    on_edge = (inner_x in (min_x, max_x) and min_y <= inner_y <= max_y) or (
                        inner_y in (min_y, max_y) and min_x <= inner_x <= max_x
                    )
                    assert on_edge, (node, inner_x, inner_y)

    def test_body_override_used_and_too_small_enlarged(self):
        raw, warnings = _build_lib_symbol_raw(
            "MYOP", PINS_2IN_1OUT, "U", "MYOP", body_width=10.16, body_height=6.35
        )
        assert warnings == []
        min_x, max_y, max_x, min_y = _rect_bounds(raw)
        assert (min_x, max_x) == (-5.08, 5.08)
        assert (max_y, min_y) == (3.175, -3.175)
        # Height below the pin span forces enlargement + warning.
        raw2, warnings2 = _build_lib_symbol_raw(
            "MYOP", PINS_2IN_1OUT, "U", "MYOP", body_height=1.27
        )
        assert warnings2, "expected an enlarge warning"
        _, max_y2, _, min_y2 = _rect_bounds(raw2)
        assert (max_y2, min_y2) == (1.27, -1.27)  # enlarged to pin span

    def test_all_four_directions(self):
        pins = [
            {"number": "1", "name": "L", "type": "input", "direction": "left"},
            {"number": "2", "name": "R", "type": "output", "direction": "right"},
            {"number": "3", "name": "U", "type": "power_in", "direction": "up"},
            {"number": "4", "name": "D", "type": "power_out", "direction": "down"},
        ]
        raw, warnings = _build_lib_symbol_raw("MYOP", pins, "U", "MYOP")
        assert warnings == []
        assert len(_pin_nodes(raw)) == 4
        for number in ("1", "2", "3", "4"):
            at = [
                c
                for c in _pin_at(raw, number)[1:]
                if isinstance(c, list) and c and c[0].value() == "at"
            ][0]
            x, y, angle = float(at[1]), float(at[2]), int(at[3])
            if number == "1":
                assert x < 0 and y == 0.0 and angle == 0
            elif number == "2":
                assert x > 0 and y == 0.0 and angle == 180
            elif number == "3":
                assert y > 0 and x == 0.0 and angle == 270
            else:
                assert y < 0 and x == 0.0 and angle == 90

    def test_lib_pins_world_positions_on_expected_sides(self):
        raw, _ = _build_lib_symbol_raw("MYOP", PINS_2IN_1OUT, "U", "MYOP")
        world = _lib_pins_world(raw, 100.0, 100.0, 0)
        assert len(world) == 3
        lefts = [p for p in world if p[0] < 100.0]
        rights = [p for p in world if p[0] > 100.0]
        assert len(lefts) == 2
        assert len(rights) == 1


# ---------------------------------------------------------------------------
# Validation failures — always {"error": ...} without "success"
# ---------------------------------------------------------------------------


class TestCreateSymbolValidation:
    def test_missing_library_returns_error(self, tools, env):
        result = _call_create(
            tools, env, library="NoSuchLib", symbol_name="MYOP", pins=PINS_2IN_1OUT
        )
        assert "error" in result
        assert "success" not in result
        assert "create_symbol_library" in result["error"]
        assert "NoSuchLib" in result["error"]

    def test_duplicate_symbol_name_returns_error(self, tools, env):
        first = _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert first.get("success") is True
        # A second call with the same name must not silently overwrite.
        second = _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert "error" in second
        assert "already exists" in second["error"]
        assert "success" not in second

    def test_empty_pins_returns_error(self, tools, env):
        result = _call_create(tools, env, symbol_name="MYOP", pins=[])
        assert "error" in result
        assert "success" not in result

    def test_invalid_direction_returns_error(self, tools, env):
        result = _call_create(
            tools,
            env,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "diagonal"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_invalid_type_returns_error(self, tools, env):
        result = _call_create(
            tools,
            env,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "analog", "direction": "left"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_duplicate_pin_number_returns_error(self, tools, env):
        pins = [
            {"number": "1", "name": "A", "type": "input", "direction": "left"},
            {"number": "1", "name": "B", "type": "output", "direction": "right"},
        ]
        result = _call_create(tools, env, symbol_name="MYOP", pins=pins)
        assert "error" in result
        assert "success" not in result

    def test_invalid_symbol_name_returns_error(self, tools, env):
        for bad in ("1MYOP", "MY-OP", "MY OP", "MY.OP"):
            result = _call_create(tools, env, symbol_name=bad, pins=PINS_2IN_1OUT)
            assert "error" in result, bad
            assert "success" not in result, bad
        # A valid all-alphanumeric name passes.
        ok = _call_create(tools, env, symbol_name="MYOP2", pins=PINS_2IN_1OUT)
        assert ok.get("success") is True

    def test_non_finite_body_size_returns_error(self, tools, env):
        result = _call_create(
            tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT, body_width=math.nan
        )
        assert "error" in result
        assert "success" not in result

    def test_empty_number_returns_error(self, tools, env):
        result = _call_create(
            tools,
            env,
            symbol_name="MYOP",
            pins=[{"number": "", "name": "A", "type": "input", "direction": "left"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_non_dict_pin_returns_error(self, tools, env):
        result = _call_create(tools, env, symbol_name="MYOP", pins=["1", "2"])
        assert "error" in result
        assert "success" not in result

    def test_non_string_value_returns_error(self, tools, env):
        result = _call_create(tools, env, symbol_name="MYOP", pins=PINS_2IN_1OUT, value=123)
        assert "error" in result
        assert "success" not in result

    def test_invalid_reference_prefix_returns_error(self, tools, env):
        for bad in ("1U", "U-1", "U 1", "U.1", ""):
            result = _call_create(
                tools,
                env,
                symbol_name="MYOP",
                pins=PINS_2IN_1OUT,
                reference_prefix=bad,
            )
            assert "error" in result, bad
            assert "success" not in result, bad
        ok = _call_create(
            tools,
            env,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            reference_prefix="U2_1",
        )
        assert ok.get("success") is True

    def test_none_pin_name_renders_as_empty(self, tools, env):
        """An explicit name=None must serialize as "" (never as nil)."""
        result = _call_create(
            tools,
            env,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": None, "type": "input", "direction": "left"}],
        )
        assert result.get("success") is True, result
        raw = _lib_symbol_raw(env["lib_path"], "MYOP")
        assert raw is not None
        node = _pin_at(raw, "1")
        name_node = [
            c
            for c in node[1:]
            if (
                isinstance(c, list)
                and len(c) >= 2
                and isinstance(c[0], sexpdata.Symbol)
                and c[0].value() == "name"
            )
        ][0]
        assert name_node[1] == ""


# ---------------------------------------------------------------------------
# create_symbol_library
# ---------------------------------------------------------------------------


class TestCreateSymbolLibrary:
    def test_create_library_and_register(self, tools, env):
        result = asyncio.run(tools["create_symbol_library"](name="SecondLib"))
        assert "error" not in result, result
        assert result["library"] == "SecondLib"
        assert result["registered"] is True
        path = result["path"]
        assert path.endswith("SecondLib.kicad_sym")
        assert os.path.isfile(path)
        assert os.path.isfile(result["table_path"])
        # Empty library parses as a valid kicad_symbol_lib root.
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert list_library_symbols(path) == []

    def test_duplicate_library_returns_error(self, tools, env):
        result = asyncio.run(tools["create_symbol_library"](name=LIB))
        assert "error" in result
        assert "already exists" in result["error"]
        assert "success" not in result

    def test_invalid_name_returns_error(self, tools, env):
        # Only names that sanitize to nothing are rejected; names like
        # "1Lib" or "My Lib" are sanitized to legal nicknames (matches
        # footprint-library semantics).
        for bad in ("", "   ", "///", "*#!@", "___"):
            result = asyncio.run(tools["create_symbol_library"](name=bad))
            assert "error" in result, repr(bad)
            assert "success" not in result, repr(bad)
        # Sanitizable names normalize and succeed.
        for ok in ("1Lib", "My Lib"):
            result = asyncio.run(tools["create_symbol_library"](name=ok))
            assert "error" not in result, (ok, result)

    def test_missing_project_dir_returns_error(self, tools, env):
        result = asyncio.run(
            tools["create_symbol_library"](name="ProjLib", project_dir="/no/such/dir")
        )
        assert "error" in result
        assert "success" not in result

    def test_created_library_is_writeable_by_create_symbol(self, tools, env):
        created = asyncio.run(tools["create_symbol_library"](name="SecondLib"))
        assert "error" not in created, created
        result = _call_create(
            tools, env, library="SecondLib", symbol_name="MYOP", pins=PINS_2IN_1OUT
        )
        assert result.get("success") is True, result
        assert result["lib_id"] == "SecondLib:MYOP"
