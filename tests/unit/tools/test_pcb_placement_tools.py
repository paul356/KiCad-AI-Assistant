"""
Unit tests for kcaa/tools/pcb_placement_tools.py
"""
import asyncio
import os
import shutil

import pytest

from kcaa.utils.pcb_sexp_utils import load_pcb
from kcaa.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")
BOARD_WITH_OUTLINE_FIXTURE = os.path.join(FIXTURE_DIR, "test_board_with_outline.kicad_pcb")


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


def _run(coro):
    return asyncio.run(coro)


class TestSetFootprintPosition:
    def test_moves_xy(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=99.0, y=88.0, rotation=None, ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, _ = get_fp_at(fp)
        assert x == pytest.approx(99.0)
        assert y == pytest.approx(88.0)

    def test_updates_rotation(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=None, y=None, rotation=45.0, ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        _, _, rot = get_fp_at(fp)
        assert rot == pytest.approx(45.0)

    def test_partial_update_preserves_unchanged(self, tools, board_copy):
        original_data = load_pcb(board_copy)
        original_fp = find_footprint(original_data, "R1")
        _, orig_y, orig_rot = get_fp_at(original_fp)

        _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=5.0, y=None, rotation=None, ctx=None
        ))
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        x, y, rot = get_fp_at(fp)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(orig_y)
        assert rot == pytest.approx(orig_rot)

    def test_creates_backup(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=1.0, y=2.0, rotation=None, ctx=None
        ))
        assert "error" not in result
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="U99", x=1.0, y=None, rotation=None, ctx=None
        ))
        assert "error" in result
        assert "U99" in result["error"]

    def test_error_when_no_coords_given(self, tools, board_copy):
        result = _run(tools["set_footprint_position"](
            pcb_path=board_copy, reference="R1", x=None, y=None, rotation=None, ctx=None
        ))
        assert "error" in result


class TestFlipFootprint:
    def test_flips_f_to_b(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="R1", ctx=None
        ))
        assert "error" not in result
        data = load_pcb(board_copy)
        fp = find_footprint(data, "R1")
        assert get_fp_layer(fp) == "B.Cu"

    def test_flips_b_to_f(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="J1", ctx=None
        ))
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
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="R1", ctx=None
        ))
        assert os.path.isfile(result["backup_path"])

    def test_error_on_missing_reference(self, tools, board_copy):
        result = _run(tools["flip_footprint"](
            pcb_path=board_copy, reference="U99", ctx=None
        ))
        assert "error" in result


class TestAlignFootprints:
    def test_align_y_to_mean(self, tools, board_with_outline_copy):
        from kcaa.utils.pcb_sexp_utils import save_pcb
        from kcaa.utils.pcb_footprint_utils import set_fp_at

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
        from kcaa.utils.pcb_sexp_utils import save_pcb
        from kcaa.utils.pcb_footprint_utils import set_fp_at

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
        from kcaa.utils.pcb_sexp_utils import save_pcb
        from kcaa.utils.pcb_footprint_utils import set_fp_at

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
