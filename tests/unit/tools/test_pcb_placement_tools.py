"""
Unit tests for kcaa/tools/pcb_placement_tools.py
"""

import asyncio
import os
import shutil

import pytest

from kcaa.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")
BOARD_WITH_OUTLINE_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_outline.kicad_pcb")
GROUP_PLACEMENT_FIXTURE = os.path.join(FIXTURE_DIR, "test_group_placement.kicad_pcb")
SIDES_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_sides.kicad_pcb")


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.pcb_placement_tools import register_pcb_placement_tools

    mock = _MockMCP()
    register_pcb_placement_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def board_copy(tmp_path):
    """Return path to a temporary copy of the test board."""
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    return str(dest)


@pytest.fixture
def board_with_outline_copy(tmp_path):
    dest = tmp_path / "board_with_outline.kicad_pcb"
    shutil.copy(BOARD_WITH_OUTLINE_FIXTURE, dest)
    return str(dest)


@pytest.fixture
def group_board_copy(tmp_path):
    """Board with courtyard geometry (0603/SOD-123/USB-C), for collision tests."""
    dest = tmp_path / "group_placement.kicad_pcb"
    shutil.copy(GROUP_PLACEMENT_FIXTURE, dest)
    return str(dest)


def _get_helper_tools() -> dict:
    from kcaa.tools.pcb_placement_helpers import register_pcb_placement_helper_tools

    mock = _MockMCP()
    register_pcb_placement_helper_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def helper_tools():
    return _get_helper_tools()


@pytest.fixture
def sides_board_copy(tmp_path):
    """Board with mixed F.Cu/B.Cu parts and one footprint without a layer."""
    dest = tmp_path / "board_sides.kicad_pcb"
    shutil.copy(SIDES_FIXTURE, dest)
    return str(dest)


def _run(coro):
    return asyncio.run(coro)


class TestSetFootprintPosition:
    def test_moves_xy(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": 99.0, "y": 88.0, "rotation": None}],
                ctx=None,
            )
        )
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, _ = get_fp_at(fp)
        assert x == pytest.approx(99.0)
        assert y == pytest.approx(88.0)

    def test_updates_rotation(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": None, "y": None, "rotation": 45.0}],
                ctx=None,
            )
        )
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        _, _, rot = get_fp_at(fp)
        assert rot == pytest.approx(45.0)

    def test_partial_update_preserves_unchanged(self, tools, board_copy):
        original_data = load_pcb(board_copy)
        original_fp = find_footprint(original_data, "R1")
        _, orig_y, orig_rot = get_fp_at(original_fp)

        _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": 5.0, "y": None, "rotation": None}],
                ctx=None,
            )
        )
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, rot = get_fp_at(fp)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(orig_y)
        assert rot == pytest.approx(orig_rot)

    def test_creates_backup(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": 1.0, "y": 2.0, "rotation": None}],
                ctx=None,
            )
        )
        assert "error" not in result
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "U99", "x": 1.0, "y": None, "rotation": None}],
                ctx=None,
            )
        )
        assert result.get("success") is False, result
        assert result["results"][0]["reference"] == "U99"
        assert "error" in result["results"][0]

    def test_error_when_no_coords_given(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": None, "y": None, "rotation": None}],
                ctx=None,
            )
        )
        assert "error" in result

    def test_unknown_item_field_rejected(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": 1.0, "bogus": 1}],
                ctx=None,
            )
        )
        assert "error" in result
        assert "bogus" in result["error"]

    def test_non_numeric_coordinate_rejected_upfront(self, tools, board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=board_copy,
                items=[{"reference": "R1", "x": "10"}],
                ctx=None,
            )
        )
        assert "error" in result
        assert "non-numeric" in result["error"]

    def test_force_false_adjusts_to_free_spot(self, tools, group_board_copy):
        """Moving R1 onto R2's anchor collides; force=False auto-adjusts to
        the nearest collision-free position."""
        result = _run(
            tools["set_footprint_position"](
                pcb_path=group_board_copy,
                items=[{"reference": "R1", "x": 96.52, "y": 90.17, "rotation": None}],
                ctx=None,
                force=False,
            )
        )
        assert result.get("success") is True, result
        entry = result["results"][0]
        assert entry["status"] == "placed_at_adjusted_position"
        assert entry["requested_position"]["x"] == pytest.approx(96.52)
        assert entry["requested_position"]["y"] == pytest.approx(90.17)
        assert entry["placed_at"]["x"] != pytest.approx(96.52) or entry["placed_at"][
            "y"
        ] != pytest.approx(90.17)

    def test_force_true_places_with_warning(self, tools, group_board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=group_board_copy,
                items=[{"reference": "R1", "x": 96.52, "y": 90.17, "rotation": None}],
                ctx=None,
                force=True,
            )
        )
        assert result.get("success") is True, result
        entry = result["results"][0]
        assert entry["status"] == "placed"
        assert "warnings" in entry
        assert entry["warnings"]["courtyard_overlaps"]

    def test_force_false_no_free_spot_returns_error(self, tools, group_board_copy, monkeypatch):
        """No free spot within the search radius -> footprint is NOT moved."""
        import kcaa.tools.pcb_placement_tools as placement_tools

        monkeypatch.setattr(placement_tools, "find_nearest_free_position", lambda *a, **k: None)
        result = _run(
            tools["set_footprint_position"](
                pcb_path=group_board_copy,
                items=[{"reference": "R1", "x": 96.52, "y": 90.17, "rotation": None}],
                ctx=None,
                force=False,
            )
        )
        assert result["failure_count"] == 1
        assert "error" in result["results"][0]
        data = load_pcb(group_board_copy)
        x, y, _ = get_fp_at(find_footprint(data, "R1"))
        assert x == pytest.approx(106.68)
        assert y == pytest.approx(80.01)


class TestFlipFootprint:
    def test_flips_f_to_b(self, tools, board_copy):
        result = _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        assert get_fp_layer(fp) == "B.Cu"

    def test_flips_b_to_f(self, tools, board_copy):
        result = _run(tools["flip_footprint"](pcb_path=board_copy, reference="J1", ctx=None))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "J1")
        assert get_fp_layer(fp) == "F.Cu"

    def test_double_flip_restores_layer(self, tools, board_copy):
        original_layer = get_fp_layer(find_footprint(load_pcb(board_copy), "R1"))
        _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        final_layer = get_fp_layer(find_footprint(load_pcb(board_copy), "R1"))
        assert final_layer == original_layer

    def test_creates_backup(self, tools, board_copy):
        result = _run(tools["flip_footprint"](pcb_path=board_copy, reference="R1", ctx=None))
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["flip_footprint"](pcb_path=board_copy, reference="U99", ctx=None))
        assert "error" in result


class TestAlignFootprints:
    def test_align_y_to_mean(self, tools, board_with_outline_copy):
        from kcaa.utils.pcb_footprint_utils import set_fp_at
        from kcaa.utils.pcb_sexp_utils import save_pcb

        data = load_pcb(board_with_outline_copy)
        fp = find_footprint(data, "R3")
        ox, oy, rot = get_fp_at(fp)
        set_fp_at(fp, ox, 30.0, rot)
        save_pcb(board_with_outline_copy, data)

        result = _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R2", "R3"],
                axis="y",
                coordinate=None,
                ctx=None,
            )
        )
        assert "error" not in result
        expected_mean = (20.0 + 20.0 + 30.0) / 3.0
        assert result["target_coordinate"] == pytest.approx(expected_mean, abs=1e-4)
        data2 = load_pcb(board_with_outline_copy)
        for ref in ("R1", "R2", "R3"):
            _, py, _ = get_fp_at(find_footprint(data2, ref))
            assert py == pytest.approx(expected_mean, abs=1e-4)

    def test_align_y_to_explicit_coordinate(self, tools, board_with_outline_copy):
        result = _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R2"],
                axis="y",
                coordinate=25.0,
                ctx=None,
            )
        )
        assert "error" not in result
        for entry in result["aligned"]:
            assert entry["new_y"] == pytest.approx(25.0)

    def test_align_x_to_explicit(self, tools, board_with_outline_copy):
        from kcaa.utils.pcb_footprint_utils import set_fp_at
        from kcaa.utils.pcb_sexp_utils import save_pcb

        data = load_pcb(board_with_outline_copy)
        fp = find_footprint(data, "R3")
        ox, _, rot = get_fp_at(fp)
        set_fp_at(fp, ox, 30.0, rot)
        save_pcb(board_with_outline_copy, data)

        result = _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R3"],
                axis="x",
                coordinate=15.0,
                ctx=None,
            )
        )
        assert "error" not in result
        for entry in result["aligned"]:
            assert entry["new_x"] == pytest.approx(15.0)

    def test_not_found_listed(self, tools, board_with_outline_copy):
        result = _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "NOTHERE"],
                axis="y",
                coordinate=20.0,
                ctx=None,
            )
        )
        assert "NOTHERE" in result["not_found"]

    def test_invalid_axis_rejected(self, tools, board_with_outline_copy):
        result = _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1"],
                axis="z",
                coordinate=None,
                ctx=None,
            )
        )
        assert "error" in result

    def test_backup_created(self, tools, board_with_outline_copy):
        _run(
            tools["align_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R2"],
                axis="y",
                coordinate=20.0,
                ctx=None,
            )
        )
        assert os.path.exists(board_with_outline_copy + ".bak")


class TestDistributeFootprints:
    def test_three_footprints_evenly_spaced(self, tools, board_with_outline_copy):
        from kcaa.utils.pcb_footprint_utils import set_fp_at
        from kcaa.utils.pcb_sexp_utils import save_pcb

        data = load_pcb(board_with_outline_copy)
        for ref, new_x in (("R2", 12.0), ("R3", 30.0)):
            fp = find_footprint(data, ref)
            ox, oy, rot = get_fp_at(fp)
            set_fp_at(fp, new_x, oy, rot)
        save_pcb(board_with_outline_copy, data)

        result = _run(
            tools["distribute_footprints"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R2", "R3"],
                axis="x",
                ctx=None,
            )
        )
        assert "error" not in result
        assert result["spacing_mm"] == pytest.approx(10.0)
        new_xs = {e["reference"]: e["new_x"] for e in result["distributed"]}
        assert new_xs["R1"] == pytest.approx(10.0)
        assert new_xs["R2"] == pytest.approx(20.0)
        assert new_xs["R3"] == pytest.approx(30.0)
        data2 = load_pcb(board_with_outline_copy)
        px, _, _ = get_fp_at(find_footprint(data2, "R2"))
        assert px == pytest.approx(20.0)

    def test_two_refs_unchanged(self, tools, board_with_outline_copy):
        result = _run(
            tools["distribute_footprints"](
                pcb_path=board_with_outline_copy, references=["R1", "R2"], axis="x", ctx=None
            )
        )
        assert "error" not in result
        assert result["spacing_mm"] == pytest.approx(20.0)

    def test_invalid_axis_rejected(self, tools, board_with_outline_copy):
        result = _run(
            tools["distribute_footprints"](
                pcb_path=board_with_outline_copy, references=["R1", "R2"], axis="z", ctx=None
            )
        )
        assert "error" in result

    def test_too_few_refs_rejected(self, tools, board_with_outline_copy):
        result = _run(
            tools["distribute_footprints"](
                pcb_path=board_with_outline_copy, references=["R1"], axis="x", ctx=None
            )
        )
        assert "error" in result


class TestMoveFootprintsByDelta:
    def test_moves_by_delta(self, tools, board_with_outline_copy):
        result = _run(
            tools["move_footprints_by_delta"](
                pcb_path=board_with_outline_copy,
                references=["R1", "R2"],
                dx=5.0,
                dy=-3.0,
                ctx=None,
            )
        )
        assert "error" not in result
        moves = {e["reference"]: e for e in result["moved"]}
        assert moves["R1"]["new_x"] == pytest.approx(15.0)
        assert moves["R1"]["new_y"] == pytest.approx(17.0)
        assert moves["R2"]["new_x"] == pytest.approx(35.0)

    def test_zero_delta_rejected(self, tools, board_with_outline_copy):
        result = _run(
            tools["move_footprints_by_delta"](
                pcb_path=board_with_outline_copy, references=["R1"], dx=0.0, dy=0.0, ctx=None
            )
        )
        assert "error" in result

    def test_empty_refs_rejected(self, tools, board_with_outline_copy):
        result = _run(
            tools["move_footprints_by_delta"](
                pcb_path=board_with_outline_copy, references=[], dx=1.0, dy=0.0, ctx=None
            )
        )
        assert "error" in result

    def test_not_found_listed(self, tools, board_with_outline_copy):
        result = _run(
            tools["move_footprints_by_delta"](
                pcb_path=board_with_outline_copy,
                references=["R1", "MISSING"],
                dx=1.0,
                dy=0.0,
                ctx=None,
            )
        )
        assert "MISSING" in result["not_found"]
        assert len(result["moved"]) == 1

    def test_backup_created(self, tools, board_with_outline_copy):
        _run(
            tools["move_footprints_by_delta"](
                pcb_path=board_with_outline_copy, references=["R1"], dx=0.0, dy=1.0, ctx=None
            )
        )
        assert os.path.exists(board_with_outline_copy + ".bak")


class TestSideAwareCollisions:
    """Courtyard collisions only apply to footprints on the same board side.

    Fixture layout (``test_board_sides.kicad_pcb``), every courtyard is
    ``(-1.0, -0.5) .. (1.0, 0.5)``:

      R1 — F.Cu at (10, 10)
      R2 — B.Cu at (10, 10)   (overlaps R1 in XY, opposite side)
      R3 — F.Cu at (30, 10)   (same side as R1)
      R4 — B.Cu at (12.7, 10) (B-side-only obstacle next to R1)
      R5 — no layer node at (20, 10)  (unknown side)
    """

    def test_move_across_sides_allowed(self, tools, sides_board_copy):
        result = _run(
            tools["set_footprint_position"](
                pcb_path=sides_board_copy,
                items=[{"reference": "R1", "x": 10.5, "y": 10.0, "rotation": None}],
                ctx=None,
            )
        )
        assert "error" not in result, result
        entry = result["results"][0]
        assert entry["success"] is True
        assert entry["status"] == "placed"
        assert entry["placed_at"]["x"] == pytest.approx(10.5)
        data = load_pcb(sides_board_copy)
        x, y, _ = get_fp_at(find_footprint(data, "R1"))
        assert x == pytest.approx(10.5)
        assert y == pytest.approx(10.0)

    def test_move_same_side_rejected(self, tools, sides_board_copy, monkeypatch):
        import kcaa.tools.pcb_placement_tools as placement_tools

        monkeypatch.setattr(placement_tools, "find_nearest_free_position", lambda *a, **k: None)
        result = _run(
            tools["set_footprint_position"](
                pcb_path=sides_board_copy,
                items=[{"reference": "R1", "x": 30.0, "y": 10.0, "rotation": None}],
                ctx=None,
            )
        )
        assert result["failure_count"] == 1
        entry = result["results"][0]
        assert "error" in entry
        assert entry["proposed_position_overlaps"] == ["R3"]
        data = load_pcb(sides_board_copy)
        x, y, _ = get_fp_at(find_footprint(data, "R1"))
        assert x == pytest.approx(10.0)
        assert y == pytest.approx(10.0)

    def test_flip_to_occupied_side_rejected(self, tools, sides_board_copy):
        result = _run(tools["flip_footprint"](pcb_path=sides_board_copy, reference="R1", ctx=None))
        assert "error" in result
        assert result["overlapping_with"] == ["R2"]
        data = load_pcb(sides_board_copy)
        assert get_fp_layer(find_footprint(data, "R1")) == "F.Cu"

    def test_flip_to_clear_side_succeeds(self, tools, sides_board_copy):
        result = _run(tools["flip_footprint"](pcb_path=sides_board_copy, reference="R3", ctx=None))
        assert "error" not in result, result
        data = load_pcb(sides_board_copy)
        assert get_fp_layer(find_footprint(data, "R3")) == "B.Cu"

    def test_flip_back_to_front_rejected(self, tools, sides_board_copy):
        result = _run(tools["flip_footprint"](pcb_path=sides_board_copy, reference="R2", ctx=None))
        assert "error" in result
        assert result["overlapping_with"] == ["R1"]
        data = load_pcb(sides_board_copy)
        assert get_fp_layer(find_footprint(data, "R2")) == "B.Cu"

    def test_align_cross_side_overlap_not_flagged(self, tools, sides_board_copy):
        result = _run(
            tools["align_footprints"](
                pcb_path=sides_board_copy,
                references=["R2", "R3"],
                axis="x",
                coordinate=30.0,
                ctx=None,
            )
        )
        assert "error" not in result, result
        new_xs = {entry["reference"]: entry["new_x"] for entry in result["aligned"]}
        assert new_xs["R2"] == pytest.approx(30.0)
        data = load_pcb(sides_board_copy)
        assert get_fp_at(find_footprint(data, "R2"))[0] == pytest.approx(30.0)

    def test_align_same_side_overlap_flagged(self, tools, sides_board_copy):
        result = _run(
            tools["align_footprints"](
                pcb_path=sides_board_copy,
                references=["R1", "R3"],
                axis="x",
                coordinate=30.0,
                ctx=None,
            )
        )
        assert "error" in result
        flagged = {c["ref"]: c["overlapping_with"] for c in result["collisions"]}
        assert flagged["R1"] == ["R3"]
        assert flagged["R3"] == ["R1"]
        data = load_pcb(sides_board_copy)
        assert get_fp_at(find_footprint(data, "R1"))[0] == pytest.approx(10.0)

    def test_nearest_free_position_ignores_opposite_side(self, sides_board_copy):
        from kcaa.tools.pcb_placement_helpers import find_nearest_free_position

        data = load_pcb(sides_board_copy)
        # R2 is on B.Cu; R1 (F.Cu) sits exactly on the target but must not
        # push the search away.
        assert find_nearest_free_position(data, "R2", 10.0, 10.0, 0.0) == (
            pytest.approx(10.16),
            pytest.approx(10.16),
        )

    def test_nearest_free_position_still_blocked_by_unknown_side(self, sides_board_copy):
        from kcaa.tools.pcb_placement_helpers import find_nearest_free_position

        data = load_pcb(sides_board_copy)
        # R5 has no layer node; the conservative rule keeps it blocking R1.
        free = find_nearest_free_position(data, "R1", 20.0, 10.0, 0.0)
        assert free is not None
        x, y = free
        assert not (x - 1.0 < 21.0 and x + 1.0 > 19.0 and y - 0.5 < 10.5 and y + 0.5 > 9.5)

    def test_unknown_layer_still_collides(self, tools, sides_board_copy, monkeypatch):
        import kcaa.tools.pcb_placement_tools as placement_tools

        monkeypatch.setattr(placement_tools, "find_nearest_free_position", lambda *a, **k: None)
        result = _run(
            tools["set_footprint_position"](
                pcb_path=sides_board_copy,
                items=[{"reference": "R1", "x": 20.0, "y": 10.0, "rotation": None}],
                ctx=None,
            )
        )
        assert result["failure_count"] == 1
        entry = result["results"][0]
        assert "error" in entry
        assert entry["proposed_position_overlaps"] == ["R5"]

    def test_find_free_pcb_area_ignores_opposite_side(self, helper_tools, sides_board_copy):
        result = _run(
            helper_tools["find_free_pcb_area"](
                pcb_path=sides_board_copy,
                footprint_ref="R2",
                prefer_near_ref="R1",
                margin=0.0,
                max_candidates=5,
                ctx=None,
            )
        )
        assert "candidates" in result, result
        first = result["candidates"][0]
        assert first["x"] == pytest.approx(10.16)
        assert first["y"] == pytest.approx(10.16)
        # The candidate really does sit on top of R1 (opposite side).
        assert first["x"] - 1.0 < 11.0 and first["x"] + 1.0 > 9.0
        assert first["y"] - 0.5 < 10.5 and first["y"] + 0.5 > 9.5

    def test_find_free_pcb_area_without_ref_blocks_both_sides(self, helper_tools, sides_board_copy):
        result = _run(
            helper_tools["find_free_pcb_area"](
                pcb_path=sides_board_copy,
                width=2.0,
                height=1.0,
                prefer_near_ref="R1",
                margin=0.0,
                max_candidates=200,
                ctx=None,
            )
        )
        assert result["candidates"], result
        for cand in result["candidates"]:
            # R1 is F.Cu, R4 is B.Cu.  Without footprint_ref there is no side
            # to compare, so both must still block: a candidate at (12.7,
            # 10.16) would overlap R4 alone and only appear if the no-ref path
            # wrongly assumed the F side.
            overlaps_r1 = (
                cand["x"] - 1.0 < 11.0
                and cand["x"] + 1.0 > 9.0
                and cand["y"] - 0.5 < 10.5
                and cand["y"] + 0.5 > 9.5
            )
            assert not overlaps_r1, cand
            overlaps_r4 = (
                cand["x"] - 1.0 < 13.7
                and cand["x"] + 1.0 > 11.7
                and cand["y"] - 0.5 < 10.5
                and cand["y"] + 0.5 > 9.5
            )
            assert not overlaps_r4, cand

    def test_unknown_proposal_side_collides_with_both(self, tools, sides_board_copy, monkeypatch):
        """A layer-less proposal must not default to either real side."""
        import kcaa.tools.pcb_placement_tools as placement_tools

        monkeypatch.setattr(placement_tools, "find_nearest_free_position", lambda *a, **k: None)
        result = _run(
            tools["set_footprint_position"](
                pcb_path=sides_board_copy,
                items=[{"reference": "R5", "x": 10.0, "y": 10.0, "rotation": None}],
                ctx=None,
            )
        )
        assert result["failure_count"] == 1
        entry = result["results"][0]
        assert "error" in entry
        assert entry["proposed_position_overlaps"] == ["R1", "R2"]

    def test_group_position_ignores_opposite_side_static(self, sides_board_copy):
        """A B.Cu member over the F.Cu R1 is clear and is not relocated."""
        from kcaa.tools.pcb_placement_helpers import _find_group_board_position

        data = load_pcb(sides_board_copy)
        x, y, found_clear, push_list = _find_group_board_position(
            data,
            {"R2"},
            [{"ref": "R2", "dx": 0.0, "dy": 0.0, "rotation": 0.0}],
            anchor_current_pos=(10.0, 10.0),
        )
        assert found_clear is True
        assert x == pytest.approx(10.16)
        assert y == pytest.approx(10.16)
        assert push_list == []

    def test_group_position_still_blocked_by_same_side_static(self, sides_board_copy):
        """An F.Cu member over the F.Cu R3 cannot keep its position."""
        from kcaa.tools.pcb_placement_helpers import _find_group_board_position

        data = load_pcb(sides_board_copy)
        x, y, found_clear, _ = _find_group_board_position(
            data,
            {"R1"},
            [{"ref": "R1", "dx": 0.0, "dy": 0.0, "rotation": 0.0}],
            anchor_current_pos=(30.0, 10.0),
        )
        started_clear = found_clear and x == pytest.approx(30.48) and y == pytest.approx(10.16)
        assert not started_clear

    def test_push_shove_ignores_opposite_side_conflict(self):
        """Pushing an F.Cu part onto a B.Cu courtyard is not a new conflict."""
        from kcaa.tools.pcb_placement_helpers import _resolve_push_shove

        member = {"min_x": -0.5, "min_y": -0.5, "max_x": 1.5, "max_y": 0.5}
        pushed = {
            "ref": "A",
            "layer": "F.Cu",
            "min_x": -1.0,
            "min_y": -0.5,
            "max_x": 1.0,
            "max_y": 0.5,
        }
        opposite = {
            "ref": "B",
            "layer": "B.Cu",
            "min_x": -1.0,
            "min_y": 0.9,
            "max_x": 1.0,
            "max_y": 1.9,
        }
        result = _resolve_push_shove({"A": [member]}, [pushed, opposite], 2.0)
        assert result is not None
        assert result[0]["ref"] == "A"
        assert result[0]["push_dy"] == pytest.approx(1.27)

    def test_push_shove_blocked_by_same_side_conflict(self):
        """The same push is rejected when the obstruction is on the same side."""
        from kcaa.tools.pcb_placement_helpers import _resolve_push_shove

        member = {"min_x": -0.5, "min_y": -0.5, "max_x": 1.5, "max_y": 0.5}
        pushed = {
            "ref": "A",
            "layer": "F.Cu",
            "min_x": -1.0,
            "min_y": -0.5,
            "max_x": 1.0,
            "max_y": 0.5,
        }
        same_side = {
            "ref": "B",
            "layer": "F.Cu",
            "min_x": -1.0,
            "min_y": 0.9,
            "max_x": 1.0,
            "max_y": 1.9,
        }
        assert _resolve_push_shove({"A": [member]}, [pushed, same_side], 2.0) is None

    def test_push_shove_cross_check_blocks_same_side_pushed_pair(self):
        """Two pushed F.Cu parts that overlap each other after the push are rejected.

        A is pushed down by 1.27 and B up by 1.27; they start 2 mm apart, so
        their original courtyards do not touch, and the per-push new-conflict
        loop skips both refs because both are in ``conflicting``.  Only the
        final pushed-vs-pushed cross-check can reject this push set.
        """

        def box(ref: str, layer: str, cy: float) -> dict:
            return {
                "ref": ref,
                "layer": layer,
                "min_x": -0.5,
                "min_y": cy - 0.5,
                "max_x": 0.5,
                "max_y": cy + 0.5,
            }

        from kcaa.tools.pcb_placement_helpers import _resolve_push_shove

        member_a = {"min_x": -0.5, "min_y": -1.5, "max_x": 0.5, "max_y": -0.2}
        member_b = {"min_x": -0.5, "min_y": 3.2, "max_x": 0.5, "max_y": 4.5}
        conflicting = {"A": [member_a], "B": [member_b]}
        statics = [box("A", "F.Cu", 0.0), box("B", "F.Cu", 3.0)]
        assert _resolve_push_shove(conflicting, statics, 2.0) is None

    def test_push_shove_cross_check_allows_opposite_side_pushed_pair(self):
        """The same overlapping push pair is accepted when the sides differ."""

        def box(ref: str, layer: str, cy: float) -> dict:
            return {
                "ref": ref,
                "layer": layer,
                "min_x": -0.5,
                "min_y": cy - 0.5,
                "max_x": 0.5,
                "max_y": cy + 0.5,
            }

        from kcaa.tools.pcb_placement_helpers import _resolve_push_shove

        member_a = {"min_x": -0.5, "min_y": -1.5, "max_x": 0.5, "max_y": -0.2}
        member_b = {"min_x": -0.5, "min_y": 3.2, "max_x": 0.5, "max_y": 4.5}
        conflicting = {"A": [member_a], "B": [member_b]}
        statics = [box("A", "F.Cu", 0.0), box("B", "B.Cu", 3.0)]
        pushes = _resolve_push_shove(conflicting, statics, 2.0)
        assert pushes == [
            {"ref": "A", "push_dx": 0.0, "push_dy": pytest.approx(1.27)},
            {"ref": "B", "push_dx": 0.0, "push_dy": pytest.approx(-1.27)},
        ]
