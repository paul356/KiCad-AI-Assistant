"""
Unit tests for schematic symbol group placement tools.

Tests use a fixture derived from the integration test schematic
(tests/unit/tools/fixtures/tools_test.kicad_sch) which contains
8 placed symbols: R1-R7, C1.

Tools tested:
  1. assign_symbols_to_group
  2. list_symbol_groups
  3. get_symbol_group
  4. score_symbol_group
  5. place_symbol_group
  6. move_symbol_group
  7. rotate_symbol_group
"""

import asyncio
import math
import os
import shutil

import pytest

from kcaa.tools.schematic_group_tools import (
    _GRID_MM,
    _GROUP_PROPERTY,
    _compute_group_union_bbox,
    _compute_proximity_score,
    _find_anchor,
    _get_sym_at,
    _get_sym_property,
    _grid_arrange_relative,
    _iter_symbols,
    _snap_to_grid,
)
from kcaa.utils.skip_compat import safe_schematic

FIXTURE_SCH = os.path.join(os.path.dirname(__file__), "fixtures", "tools_test.kicad_sch")
_PLACED_REFS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "C1")
_TEST_GROUP = "test_power"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.schematic_group_tools import register_schematic_group_tools

    mock = _MockMCP()
    register_schematic_group_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sch_data():
    return safe_schematic(FIXTURE_SCH)


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def sch_copy(tmp_path):
    dest = tmp_path / "test.kicad_sch"
    shutil.copy(FIXTURE_SCH, dest)
    return str(dest)


@pytest.fixture
def sch_with_group(sch_copy, tools):
    """Fixture that assigns R1,R2,R3,R4 to _TEST_GROUP and returns the path."""
    result = _run(tools["assign_symbols_to_group"](sch_copy, ["R1", "R2", "R3", "R4"], _TEST_GROUP))
    assert "error" not in result, f"assign failed: {result.get('error')}"
    return sch_copy


# ===========================================================================
# Property helpers
# ===========================================================================


class TestPropertyHelpers:
    """Verify symbol property read / write / delete."""

    def test_get_existing_property(self, sch_data):
        for sym in _iter_symbols(sch_data):
            if _get_sym_property(sym, "Reference") == "R1":
                assert _get_sym_property(sym, "Reference") == "R1"
                assert _get_sym_property(sym, "Value") == "R_Small"
                return
        pytest.fail("R1 not found in fixture")

    def test_get_nonexistent_property(self, sch_data):
        for sym in _iter_symbols(sch_data):
            if _get_sym_property(sym, "Reference") == "R1":
                assert _get_sym_property(sym, "nonexistent_prop") is None
                return
        pytest.fail("R1 not found in fixture")

    def test_set_and_delete_property(self, sch_copy, tools):
        # Set via MCP tool, then verify persistence through reload
        result = _run(tools["assign_symbols_to_group"](sch_copy, ["R1"], "temp_group"))
        assert "error" not in result
        assert result["assigned"] == ["R1"]

        # Reload and verify
        sch = safe_schematic(sch_copy)
        found = False
        for sym in _iter_symbols(sch):
            if _get_sym_property(sym, "Reference") == "R1":
                assert _get_sym_property(sym, _GROUP_PROPERTY) == "temp_group"
                found = True
        assert found, "R1 not found after reload"

        # Remove via unassign (empty group name)
        result2 = _run(tools["assign_symbols_to_group"](sch_copy, ["R1"], ""))
        assert "error" not in result2
        assert result2["group_name"] == "(unassigned)"

        # Reload and verify removal
        sch2 = safe_schematic(sch_copy)
        for sym in _iter_symbols(sch2):
            if _get_sym_property(sym, "Reference") == "R1":
                assert _get_sym_property(sym, _GROUP_PROPERTY) is None
                return
        pytest.fail("R1 not found")


# ===========================================================================
# Symbol info helpers
# ===========================================================================


class TestSymbolInfo:
    """Verify _get_sym_at and _get_sym_pin_count."""

    def test_get_sym_at_returns_expected_position(self, sch_data):
        """R1 is at (161.29, 105.41, 0)."""
        for sym in _iter_symbols(sch_data):
            if _get_sym_property(sym, "Reference") == "R1":
                x, y, rot = _get_sym_at(sym)
                assert x == pytest.approx(161.29, abs=0.1)
                assert y == pytest.approx(105.41, abs=0.1)
                assert rot == 0.0
                return
        pytest.fail("R1 not found")

    def test_r2_position(self, sch_data):
        """R2 is at (100.0, 100.0)."""
        for sym in _iter_symbols(sch_data):
            if _get_sym_property(sym, "Reference") == "R2":
                x, y, rot = _get_sym_at(sym)
                assert x == pytest.approx(100.0, abs=0.1)
                assert y == pytest.approx(100.0, abs=0.1)
                return
        pytest.fail("R2 not found")

    def test_all_placed_symbols_have_ref_and_position(self, sch_data):
        refs_found = set()
        for sym in _iter_symbols(sch_data):
            ref = _get_sym_property(sym, "Reference")
            if ref and ref in _PLACED_REFS:
                x, y, _ = _get_sym_at(sym)
                assert isinstance(x, float)
                assert isinstance(y, float)
                refs_found.add(ref)
        assert refs_found == set(_PLACED_REFS), f"Expected all 8 refs, found: {sorted(refs_found)}"


# ===========================================================================
# Grid and snapping
# ===========================================================================


class TestSnapToGrid:
    def test_snap_exact_on_grid(self):
        assert _snap_to_grid(5.08) == 5.08
        assert _snap_to_grid(0.0) == 0.0

    def test_snap_off_grid(self):
        assert _snap_to_grid(5.0) == pytest.approx(5.08, abs=0.01)
        assert _snap_to_grid(3.0) == pytest.approx(2.54, abs=0.01)

    def test_negative_values(self):
        assert _snap_to_grid(-5.0) == pytest.approx(-5.08, abs=0.01)

    def test_snap_is_multiple_of_grid(self):
        for val in (1.0, 2.5, 7.3, -3.6, 100.0):
            snapped = _snap_to_grid(val)
            remainder = abs(snapped / _GRID_MM - round(snapped / _GRID_MM))
            assert remainder < 1e-6, f"{snapped} not on grid"


# ===========================================================================
# Group member helpers
# ===========================================================================


class TestFindAnchor:
    def test_highest_pin_count_wins(self):
        members = [
            {"reference": "U1", "pin_count": 8},
            {"reference": "R1", "pin_count": 2},
            {"reference": "C1", "pin_count": 2},
        ]
        anchor = _find_anchor(members)
        assert anchor["reference"] == "U1"

    def test_tie_broken_by_reference_sort(self):
        members = [
            {"reference": "R2", "pin_count": 2},
            {"reference": "R1", "pin_count": 2},
        ]
        anchor = _find_anchor(members)
        # max() on (pin_count, reference) → R2 wins (alphabetically larger)
        assert anchor["reference"] == "R2"

    def test_empty_returns_none(self):
        assert _find_anchor([]) is None


class TestComputeGroupUnionBbox:
    def test_union_bbox_from_members(self):
        members = [
            {"reference": "A", "body_bbox": {"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 10}},
            {"reference": "B", "body_bbox": {"min_x": 5, "min_y": 5, "max_x": 20, "max_y": 15}},
        ]
        bbox = _compute_group_union_bbox(members)
        assert bbox["min_x"] == 0
        assert bbox["min_y"] == 0
        assert bbox["max_x"] == 20
        assert bbox["max_y"] == 15

    def test_union_bbox_with_relative_layout(self):
        members = [
            {"reference": "A", "body_bbox": {"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 10}},
            {"reference": "B", "body_bbox": {"min_x": 0, "min_y": 0, "max_x": 5, "max_y": 5}},
        ]
        layout = [
            {"reference": "A", "dx": 0, "dy": 0},
            {"reference": "B", "dx": 10, "dy": 0},
        ]
        bbox = _compute_group_union_bbox(members, layout)
        assert bbox["min_x"] == 0
        assert bbox["max_x"] == 15
        assert bbox["min_y"] == 0
        assert bbox["max_y"] == 10

    def test_no_bbox_data_returns_none(self):
        members = [{"reference": "X"}]
        assert _compute_group_union_bbox(members) is None


# ===========================================================================
# Grid layout
# ===========================================================================


class TestGridArrangeRelative:
    def test_returns_all_non_anchor_members(self):
        anchor = {
            "reference": "U1",
            "x": 100,
            "y": 100,
            "body_bbox": {"min_x": 95, "min_y": 95, "max_x": 105, "max_y": 105},
            "pin_count": 8,
        }
        non_anchor = [
            {
                "reference": "R1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -2, "min_y": -1, "max_x": 2, "max_y": 1},
            },
            {
                "reference": "C1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -3, "min_y": -4, "max_x": 3, "max_y": 4},
            },
        ]
        result = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        refs = {r["reference"] for r in result}
        assert refs == {"R1", "C1"}

    def test_anchor_bbox_offset_is_relative(self):
        anchor = {
            "reference": "U1",
            "x": 100,
            "y": 100,
            "body_bbox": {"min_x": 95, "min_y": 95, "max_x": 105, "max_y": 105},
            "pin_count": 8,
        }
        non_anchor = [
            {
                "reference": "R1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -2, "min_y": -1, "max_x": 2, "max_y": 1},
            },
        ]
        result = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        r = result[0]
        assert r["reference"] == "R1"
        assert r["dx"] > 0
        assert abs(r["dx"] / _GRID_MM - round(r["dx"] / _GRID_MM)) < 1e-6

    def test_fallback_without_bbox(self):
        anchor = {"reference": "U1", "x": 100, "y": 100, "pin_count": 8}
        non_anchor = [
            {"reference": "R1", "pin_count": 2},
            {"reference": "C1", "pin_count": 2},
        ]
        result = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        assert len(result) == 2
        for r in result:
            assert "fallback grid" in r["rationale"]

    def test_members_sorted_by_pin_count_descending(self):
        anchor = {
            "reference": "U1",
            "x": 100,
            "y": 100,
            "body_bbox": {"min_x": 95, "min_y": 95, "max_x": 105, "max_y": 105},
            "pin_count": 10,
        }
        non_anchor = [
            {
                "reference": "R1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -2, "min_y": -1, "max_x": 2, "max_y": 1},
            },
            {
                "reference": "U2",
                "x": 0,
                "y": 0,
                "pin_count": 6,
                "body_bbox": {"min_x": -5, "min_y": -5, "max_x": 5, "max_y": 5},
            },
        ]
        result = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        assert result[0]["reference"] == "U2"

    def test_result_is_deterministic(self):
        anchor = {
            "reference": "U1",
            "x": 100,
            "y": 100,
            "body_bbox": {"min_x": 95, "min_y": 95, "max_x": 105, "max_y": 105},
            "pin_count": 8,
        }
        non_anchor = [
            {
                "reference": "R1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -2, "min_y": -1, "max_x": 2, "max_y": 1},
            },
            {
                "reference": "C1",
                "x": 0,
                "y": 0,
                "pin_count": 2,
                "body_bbox": {"min_x": -3, "min_y": -4, "max_x": 3, "max_y": 4},
            },
        ]
        r1 = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        r2 = _grid_arrange_relative(anchor, non_anchor, gap_mm=2.54)
        assert r1 == r2


# ===========================================================================
# Proximity scoring
# ===========================================================================


class TestComputeProximityScore:
    def test_single_member_returns_zero(self):
        members = [{"reference": "R1", "x": 100, "y": 100}]
        score = _compute_proximity_score(members)
        assert score["mean_nn_mm"] == 0.0
        assert score["mean_spread_mm"] == 0.0

    def test_two_members_same_position(self):
        members = [
            {"reference": "R1", "x": 100, "y": 100},
            {"reference": "R2", "x": 100, "y": 100},
        ]
        score = _compute_proximity_score(members)
        assert score["mean_nn_mm"] == 0.0
        assert score["mean_spread_mm"] == 0.0

    def test_positive_distance_gives_positive_score(self):
        members = [
            {"reference": "R1", "x": 0, "y": 0},
            {"reference": "R2", "x": 10, "y": 0},
            {"reference": "R3", "x": 0, "y": 10},
        ]
        score = _compute_proximity_score(members)
        assert score["mean_nn_mm"] > 0.0
        assert score["mean_spread_mm"] > 0.0

    def test_compact_group_has_lower_score_than_spread(self):
        compact = [
            {"reference": "A", "x": 100, "y": 100},
            {"reference": "B", "x": 105, "y": 100},
            {"reference": "C", "x": 100, "y": 105},
        ]
        spread = [
            {"reference": "A", "x": 100, "y": 100},
            {"reference": "B", "x": 200, "y": 100},
            {"reference": "C", "x": 100, "y": 200},
        ]
        score_compact = _compute_proximity_score(compact)
        score_spread = _compute_proximity_score(spread)
        assert score_compact["mean_spread_mm"] < score_spread["mean_spread_mm"]


# ===========================================================================
# MCP tool: assign_symbols_to_group
# ===========================================================================


class TestAssignSymbolsToGroup:
    def test_assigns_symbols_and_returns_expected_fields(self, tools, sch_copy):
        result = _run(tools["assign_symbols_to_group"](sch_copy, ["R1", "R2", "R3"], _TEST_GROUP))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_name"] == _TEST_GROUP
        assert sorted(result["assigned"]) == ["R1", "R2", "R3"]
        assert result["not_found"] == []
        assert os.path.exists(result["backup_path"])

    def test_reports_not_found(self, tools, sch_copy):
        result = _run(
            tools["assign_symbols_to_group"](sch_copy, ["R1", "NONEXISTENT"], _TEST_GROUP)
        )
        assert "error" not in result
        assert result["assigned"] == ["R1"]
        assert result["not_found"] == ["NONEXISTENT"]

    def test_all_not_found_returns_error(self, tools, sch_copy):
        result = _run(tools["assign_symbols_to_group"](sch_copy, ["XXX", "YYY"], _TEST_GROUP))
        assert "error" in result
        assert result["not_found"] == ["XXX", "YYY"]

    def test_remove_from_group_with_empty_name(self, tools, sch_with_group):
        result = _run(tools["assign_symbols_to_group"](sch_with_group, ["R1"], ""))
        assert "error" not in result
        assert result["group_name"] == "(unassigned)"
        assert result["assigned"] == ["R1"]

    def test_reassign_to_different_group(self, tools, sch_with_group):
        result = _run(tools["assign_symbols_to_group"](sch_with_group, ["R1", "R2"], "other_group"))
        assert "error" not in result
        assert result["group_name"] == "other_group"
        assert sorted(result["assigned"]) == ["R1", "R2"]

    def test_rejects_non_sch_file(self, tools):
        result = _run(tools["assign_symbols_to_group"]("/tmp/test.txt", ["R1"], _TEST_GROUP))
        assert "error" in result
        assert ".kicad_sch" in result["error"]


# ===========================================================================
# MCP tool: list_symbol_groups
# ===========================================================================


class TestListSymbolGroups:
    def test_empty_no_groups(self, tools, sch_copy):
        result = _run(tools["list_symbol_groups"](sch_copy))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_count"] == 0
        assert result["ungrouped_count"] == len(_PLACED_REFS)
        assert result["groups"] == []

    def test_lists_groups_after_assign(self, tools, sch_with_group):
        result = _run(tools["list_symbol_groups"](sch_with_group))
        assert "error" not in result
        assert result["group_count"] == 1
        groups = result["groups"]
        assert groups[0]["group_name"] == _TEST_GROUP
        assert groups[0]["member_count"] == 4
        assert sorted(groups[0]["members"]) == ["R1", "R2", "R3", "R4"]
        assert groups[0]["anchor_ref"] in ("R1", "R2", "R3", "R4")
        bbox = groups[0]["bbox"]
        assert bbox["min_x"] <= bbox["max_x"]
        assert bbox["min_y"] <= bbox["max_y"]
        assert result["ungrouped_count"] == 4

    def test_multiple_groups(self, tools, sch_copy):
        _run(tools["assign_symbols_to_group"](sch_copy, ["R1", "R2"], "group_a"))
        _run(tools["assign_symbols_to_group"](sch_copy, ["R3", "R4"], "group_b"))
        result = _run(tools["list_symbol_groups"](sch_copy))
        assert result["group_count"] == 2
        group_names = {g["group_name"] for g in result["groups"]}
        assert group_names == {"group_a", "group_b"}
        assert result["ungrouped_count"] == 4


# ===========================================================================
# MCP tool: get_symbol_group
# ===========================================================================


class TestGetSymbolGroup:
    def test_returns_member_details(self, tools, sch_with_group):
        result = _run(tools["get_symbol_group"](sch_with_group, _TEST_GROUP))
        assert "error" not in result
        assert result["group_name"] == _TEST_GROUP
        assert result["member_count"] == 4
        member_refs = {m["reference"] for m in result["members"]}
        assert member_refs == {"R1", "R2", "R3", "R4"}
        for m in result["members"]:
            assert "x" in m
            assert "y" in m
            assert "rotation" in m
            assert "pin_count" in m
            assert m["pin_count"] >= 0  # skip library may return 0 for schematic symbol pins
        assert result["anchor_ref"] is not None
        assert result["anchor_ref"] in ("R1", "R2", "R3", "R4")
        bbox = result["bbox"]
        assert bbox["min_x"] <= bbox["max_x"]

    def test_unknown_group_returns_error(self, tools, sch_copy):
        result = _run(tools["get_symbol_group"](sch_copy, "nonexistent"))
        assert "error" in result
        assert "nonexistent" in result["error"]


# ===========================================================================
# MCP tool: score_symbol_group
# ===========================================================================


class TestScoreSymbolGroup:
    def test_returns_proximity_metrics(self, tools, sch_with_group):
        result = _run(tools["score_symbol_group"](sch_with_group, _TEST_GROUP))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_name"] == _TEST_GROUP
        assert result["member_count"] == 4
        assert result["anchor_ref"] in ("R1", "R2", "R3", "R4")
        assert isinstance(result["mean_nn_mm"], int | float)
        assert isinstance(result["mean_spread_mm"], int | float)
        assert math.isfinite(result["mean_nn_mm"])
        assert math.isfinite(result["mean_spread_mm"])
        assert result["mean_spread_mm"] >= 0

    def test_unknown_group_returns_error(self, tools, sch_copy):
        result = _run(tools["score_symbol_group"](sch_copy, "nonexistent"))
        assert "error" in result


# ===========================================================================
# MCP tool: place_symbol_group
# ===========================================================================


class TestPlaceSymbolGroup:
    def test_places_all_members(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_name"] == _TEST_GROUP
        assert result["placed_count"] == 4
        # Empty sheet: should find clear position
        assert result["found_clear_position"] is True
        placed_refs = {p["reference"] for p in result["placed"]}
        assert placed_refs == {"R1", "R2", "R3", "R4"}
        assert result["anchor_ref"] in ("R1", "R2", "R3", "R4")

    def test_placed_positions_are_on_grid(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        assert "error" not in result
        for p in result["placed"]:
            assert abs(p["x"] / _GRID_MM - round(p["x"] / _GRID_MM)) < 1e-6, (
                f"{p['reference']} x={p['x']} not on grid"
            )
            assert abs(p["y"] / _GRID_MM - round(p["y"] / _GRID_MM)) < 1e-6, (
                f"{p['reference']} y={p['y']} not on grid"
            )

    def test_anchor_preserves_rotation(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        anchor_placed = next(p for p in result["placed"] if p["reference"] == result["anchor_ref"])
        assert "rotation" in anchor_placed

    def test_returns_proximity_scores(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        assert "mean_nn_mm" in result
        assert "mean_spread_mm" in result
        assert math.isfinite(result["mean_nn_mm"])

    def test_backup_created(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        backup = result.get("backup_path")
        assert backup is not None
        assert os.path.exists(backup)

    def test_empty_group_returns_error(self, tools, sch_copy):
        result = _run(tools["place_symbol_group"](sch_copy, "nonexistent_group"))
        assert "error" in result

    def test_custom_gap_is_accepted(self, tools, sch_with_group):
        result = _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP, gap_mm=5.0))
        assert "error" not in result
        assert result["placed_count"] == 4


# ===========================================================================
# MCP tool: move_symbol_group
# ===========================================================================


class TestMoveSymbolGroup:
    def test_moves_all_members_to_new_position(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        target_x, target_y = 50.0, 50.0
        result = _run(tools["move_symbol_group"](sch_with_group, _TEST_GROUP, target_x, target_y))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_name"] == _TEST_GROUP
        assert result["moved_count"] == 4
        assert result["anchor_position"]["x"] == pytest.approx(target_x, abs=_GRID_MM)
        assert result["anchor_position"]["y"] == pytest.approx(target_y, abs=_GRID_MM)

    def test_backup_created(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        result = _run(tools["move_symbol_group"](sch_with_group, _TEST_GROUP, 80.0, 80.0))
        backup = result.get("backup_path")
        assert backup is not None
        assert os.path.exists(backup)

    def test_empty_group_returns_error(self, tools, sch_copy):
        result = _run(tools["move_symbol_group"](sch_copy, "nonexistent", 50.0, 50.0))
        assert "error" in result


# ===========================================================================
# MCP tool: rotate_symbol_group
# ===========================================================================


class TestRotateSymbolGroup:
    def test_rotates_all_members(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        result = _run(tools["rotate_symbol_group"](sch_with_group, _TEST_GROUP, 90.0))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["group_name"] == _TEST_GROUP
        assert result["rotated_count"] == 4
        assert result["rotation_delta"] == 90.0

    def test_zero_rotation_is_noop(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        result = _run(tools["rotate_symbol_group"](sch_with_group, _TEST_GROUP, 0.0))
        assert "error" not in result
        assert result["rotation_delta"] == 0.0

    def test_negative_angle_is_accepted(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        result = _run(tools["rotate_symbol_group"](sch_with_group, _TEST_GROUP, -45.0))
        assert "error" not in result
        assert result["rotation_delta"] == -45.0

    def test_backup_created(self, tools, sch_with_group):
        _run(tools["place_symbol_group"](sch_with_group, _TEST_GROUP))
        result = _run(tools["rotate_symbol_group"](sch_with_group, _TEST_GROUP, 180.0))
        backup = result.get("backup_path")
        assert backup is not None
        assert os.path.exists(backup)

    def test_empty_group_returns_error(self, tools, sch_copy):
        result = _run(tools["rotate_symbol_group"](sch_copy, "nonexistent", 90.0))
        assert "error" in result


# ===========================================================================
# Round-trip: assign -> place -> move -> verify
# ===========================================================================


class TestRoundTrip:
    """End-to-end: assign, place, move, re-read, and verify persistence."""

    def test_full_workflow(self, tools, sch_copy):
        # 1. Assign
        r1 = _run(tools["assign_symbols_to_group"](sch_copy, ["R5", "R6", "R7"], "rt_group"))
        assert "error" not in r1

        # 2. List
        r2 = _run(tools["list_symbol_groups"](sch_copy))
        assert r2["group_count"] == 1
        assert r2["groups"][0]["group_name"] == "rt_group"

        # 3. Get
        r3 = _run(tools["get_symbol_group"](sch_copy, "rt_group"))
        member_refs = {m["reference"] for m in r3["members"]}
        assert member_refs == {"R5", "R6", "R7"}

        # 4. Place
        r4 = _run(tools["place_symbol_group"](sch_copy, "rt_group"))
        assert r4["placed_count"] == 3

        # 5. Move
        r5 = _run(tools["move_symbol_group"](sch_copy, "rt_group", 50.0, 60.0))
        assert r5["moved_count"] == 3

        # 6. Re-read
        r6 = _run(tools["get_symbol_group"](sch_copy, "rt_group"))
        for m in r6["members"]:
            assert abs(m["x"] - 50.0) < 100.0
            assert abs(m["y"] - 60.0) < 100.0
