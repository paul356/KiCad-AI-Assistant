"""Tests for the create_symbol MCP tool (kcaa.tools.symbol_edit_tools).

Like the rest of the symbol_edit tests, disk-writing tests operate on a
temporary copy of tests/unit/tools/tools_test.kicad_sch so the fixture is
never modified.  The tool builds a brand-new lib symbol definition from
``pins`` and injects it under lib_id "自定义:NAME"; no index or library
fixture is involved.
"""

import asyncio
import math
import os
from pathlib import Path
import shutil
import tempfile

import pytest
import sexpdata
import skip

from kcaa.tools.symbol_edit_tools import (
    _DEFAULT_PIN_LENGTH_MM,
    _build_lib_symbol_raw,
    _extract_lib_pin_positions,
    _lib_pins_world,
)

# ---------------------------------------------------------------------------
# Paths / fixtures
# ---------------------------------------------------------------------------

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")

# 2 inputs on the left + 1 output on the right (taskbook example).
PINS_2IN_1OUT = [
    {"number": "1", "name": "IN", "type": "input", "direction": "left"},
    {"number": "2", "name": "IN2", "type": "input", "direction": "left"},
    {"number": "3", "name": "OUT", "type": "output", "direction": "right"},
]

LIB_ID = "自定义:MYOP"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_copy() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir())
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


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
def tmp_sch():
    """Yield a temp copy of the schematic, then clean up."""
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


def _lib_symbol_raw(sch, lib_id: str):
    """Return the raw (symbol ...) lib entry for *lib_id* from a reloaded sch."""
    for entry in sch.lib_symbols._pv._tree:
        if isinstance(entry, list) and len(entry) >= 2 and entry[1] == lib_id:
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


def _placed_symbols(sch, lib_id: str = LIB_ID):
    out = []
    for sym in sch.symbol:
        try:
            if sym.lib_id.value == lib_id:
                out.append(sym)
        except AttributeError:
            continue
    return out


def _call_create(tools, tmp_sch, **kwargs):
    return asyncio.run(tools["create_symbol"](schematic_path=tmp_sch, **kwargs))


# ---------------------------------------------------------------------------
# Definition building
# ---------------------------------------------------------------------------


class TestCreateSymbolDefinition:
    def test_injects_definition_and_reports_metadata(self, tools, tmp_sch):
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert result.get("success") is True, result
        assert result["lib_id"] == LIB_ID
        assert result["pin_count"] == 3
        assert result["units_added"] == 1
        assert result["position"] is None
        assert result["file_modified"] == tmp_sch
        assert result["backup_path"] == tmp_sch + ".bak"
        assert result["warnings"] == []

    def test_file_round_trips_through_skip(self, tools, tmp_sch):
        """After writing, skip re-parses the file and sees the new definition."""
        _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        sch = skip.Schematic(tmp_sch)
        assert LIB_ID in sch.lib_symbols
        raw = _lib_symbol_raw(sch, LIB_ID)
        assert raw is not None
        assert len(_pin_nodes(raw)) == 3
        # Properties Reference/Value/Footprint/Datasheet are present.
        prop_names = [
            child[1]
            for child in raw[2:]
            if (
                isinstance(child, list)
                and len(child) >= 2
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "property"
            )
        ]
        assert prop_names[:4] == ["Reference", "Value", "Footprint", "Datasheet"]
        # Footprint/Datasheet carry (hide yes); Reference/Value stay visible.
        prop_hide = {
            child[1]: any(
                isinstance(c, list)
                and len(c) >= 2
                and isinstance(c[0], sexpdata.Symbol)
                and c[0].value() == "hide"
                for c in child[2:]
            )
            for child in raw[2:]
            if (
                isinstance(child, list)
                and len(child) >= 2
                and isinstance(child[0], sexpdata.Symbol)
                and child[0].value() == "property"
            )
        }
        assert prop_hide == {
            "Reference": False,
            "Value": False,
            "Footprint": True,
            "Datasheet": True,
        }

    def test_define_only_creates_no_placed_instance(self, tools, tmp_sch):
        """Without x/y there must be no placed symbol using the new lib_id."""
        before = len(list(skip.Schematic(tmp_sch).symbol))
        _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        sch = skip.Schematic(tmp_sch)
        assert len(list(sch.symbol)) == before
        assert _placed_symbols(sch) == []

    def test_creates_backup(self, tools, tmp_sch):
        _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert os.path.exists(tmp_sch + ".bak")


class TestLibSymbolLayout:
    """Direct checks on _build_lib_symbol_raw geometry (lib coords, Y-up)."""

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
                    # edge" claim in _build_lib_symbol_raw true for the
                    # generated geometry.
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

    def test_placed_pins_world_positions_on_expected_sides(self):
        raw, _ = _build_lib_symbol_raw("MYOP", PINS_2IN_1OUT, "U", "MYOP")
        world = _lib_pins_world(raw, 100.0, 100.0, 0)
        assert len(world) == 3
        lefts = [p for p in world if p[0] < 100.0]
        rights = [p for p in world if p[0] > 100.0]
        assert len(lefts) == 2
        assert len(rights) == 1


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


class TestCreateSymbolPlacement:
    def test_place_assigns_next_reference(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            x=100.0,
            y=100.0,
        )
        assert result.get("success") is True, result
        pos = result["position"]
        assert pos is not None
        # Placement is auto-snapped to the 1.27mm (50-mil) grid.
        assert abs(pos["x"] / 1.27 - round(pos["x"] / 1.27)) < 1e-6
        assert abs(pos["y"] / 1.27 - round(pos["y"] / 1.27)) < 1e-6
        sch = skip.Schematic(tmp_sch)
        placed = _placed_symbols(sch)
        assert len(placed) == 1
        sym = placed[0]
        assert sym.property.Reference.value == "U1"
        assert sym.property.Value.value == "MYOP"
        # Pin world coords land left/right of the instance, as designed.
        raw = _lib_symbol_raw(sch, LIB_ID)
        world = _lib_pins_world(raw, pos["x"], pos["y"], 0)
        assert sum(1 for p in world if p[0] < pos["x"]) == 2
        assert sum(1 for p in world if p[0] > pos["x"]) == 1

    def test_place_offsets_grid(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "left"}],
            x=100.1,
            y=99.9,
        )
        assert result.get("success") is True, result
        px, py = result["position"]["x"], result["position"]["y"]
        assert abs(px / 1.27 - round(px / 1.27)) < 1e-6
        assert abs(py / 1.27 - round(py / 1.27)) < 1e-6

    def test_value_override_used(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            x=200.0,
            y=120.0,
            value="AMPLIFIER",
        )
        assert result.get("success") is True, result
        sch = skip.Schematic(tmp_sch)
        assert _placed_symbols(sch)[0].property.Value.value == "AMPLIFIER"


# ---------------------------------------------------------------------------
# Validation failures — always {"error": ...} without "success"
# ---------------------------------------------------------------------------


class TestCreateSymbolValidation:
    def test_empty_pins_returns_error(self, tools, tmp_sch):
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=[])
        assert "error" in result
        assert "success" not in result

    def test_invalid_direction_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "diagonal"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_invalid_type_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "analog", "direction": "left"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_duplicate_pin_number_returns_error(self, tools, tmp_sch):
        pins = [
            {"number": "1", "name": "A", "type": "input", "direction": "left"},
            {"number": "1", "name": "B", "type": "output", "direction": "right"},
        ]
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=pins)
        assert "error" in result
        assert "success" not in result

    def test_invalid_symbol_name_returns_error(self, tools, tmp_sch):
        for bad in ("1MYOP", "MY-OP", "MY OP", "MY.OP"):
            result = _call_create(tools, tmp_sch, symbol_name=bad, pins=PINS_2IN_1OUT)
            assert "error" in result, bad
            assert "success" not in result, bad
        # A valid all-alphanumeric name passes.
        ok = _call_create(tools, tmp_sch, symbol_name="MYOP2", pins=PINS_2IN_1OUT)
        assert ok.get("success") is True

    def test_x_without_y_returns_error(self, tools, tmp_sch):
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT, x=100.0)
        assert "error" in result
        assert "success" not in result

    def test_y_without_x_returns_error(self, tools, tmp_sch):
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT, y=100.0)
        assert "error" in result
        assert "success" not in result

    def test_invalid_rotation_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            x=100.0,
            y=100.0,
            rotation=45,
        )
        assert "error" in result
        assert "success" not in result

    def test_non_finite_coordinate_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            x=math.inf,
            y=100.0,
        )
        assert "error" in result
        assert "success" not in result

    def test_non_finite_body_size_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            body_width=math.nan,
        )
        assert "error" in result
        assert "success" not in result

    def test_empty_number_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=[{"number": "", "name": "A", "type": "input", "direction": "left"}],
        )
        assert "error" in result
        assert "success" not in result

    def test_non_dict_pin_returns_error(self, tools, tmp_sch):
        result = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=["1", "2"])
        assert "error" in result
        assert "success" not in result

    def test_invalid_extension_returns_error(self, tools):
        result = _call_create(tools, "/tmp/nope.txt", symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert "error" in result
        assert "success" not in result

    def test_missing_file_returns_error(self, tools, tmp_sch):
        missing = tmp_sch + ".missing.kicad_sch"
        result = _call_create(tools, missing, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert "error" in result
        assert "success" not in result

    def test_duplicate_symbol_name_returns_error(self, tools, tmp_sch):
        first = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert first.get("success") is True
        # A second call with the same name must not silently re-define.
        second = _call_create(tools, tmp_sch, symbol_name="MYOP", pins=PINS_2IN_1OUT)
        assert "error" in second
        assert "already exists in schematic lib_symbols" in second["error"]
        assert "success" not in second

    def test_non_string_value_returns_error(self, tools, tmp_sch):
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            value=123,
        )
        assert "error" in result
        assert "success" not in result

    def test_invalid_reference_prefix_returns_error(self, tools, tmp_sch):
        for bad in ("1U", "U-1", "U 1", "U.1", ""):
            result = _call_create(
                tools,
                tmp_sch,
                symbol_name="MYOP",
                pins=PINS_2IN_1OUT,
                reference_prefix=bad,
            )
            assert "error" in result, bad
            assert "success" not in result, bad
        ok = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=PINS_2IN_1OUT,
            reference_prefix="U2_1",
        )
        assert ok.get("success") is True

    def test_none_pin_name_renders_as_empty(self, tools, tmp_sch):
        """An explicit name=None must serialize as "" (never as nil)."""
        result = _call_create(
            tools,
            tmp_sch,
            symbol_name="MYOP",
            pins=[{"number": "1", "name": None, "type": "input", "direction": "left"}],
        )
        assert result.get("success") is True, result
        sch = skip.Schematic(tmp_sch)  # round-trip proves the file parsed
        raw = _lib_symbol_raw(sch, LIB_ID)
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
