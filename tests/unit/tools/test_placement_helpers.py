"""Tests for placement_helpers (get_schematic_sheet_info, find_free_area)
and the new placement-related additions to component_edit_tools
(body_bbox in add/move returns, place_symbol_relative).
"""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")
TEST_SYM_PATH = str(Path(__file__).parent / "fixtures/test_symbols.kicad_sym")

_LIB_NAME = "Device"
_SYM_NAME = "R_Small"


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _placement_tools() -> dict:
    from kcaa.tools.placement_helpers import register_placement_helpers

    mock = _MockMCP()
    register_placement_helpers(mock)
    return mock.tools


def _component_tools() -> dict:
    from kcaa.tools.component_edit_tools import register_component_edit_tools

    mock = _MockMCP()
    register_component_edit_tools(mock)
    return mock.tools


def _make_temp_copy() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False)
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


def _make_mock_manager() -> MagicMock:
    mgr = MagicMock()
    lib_rec = MagicMock()
    lib_rec.file_path = TEST_SYM_PATH
    lib_rec.mtime = 0.0
    lib_rec.file_size = 0
    sym_rec = MagicMock()
    sym_rec.file_index = 0
    mgr.get_library_by_name.return_value = lib_rec
    mgr.get_symbol.return_value = sym_rec
    return mgr


@pytest.fixture()
def tmp_sch():
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


# ---------------------------------------------------------------------------
# get_schematic_sheet_info
# ---------------------------------------------------------------------------


class TestSheetInfo:
    def test_returns_paper_and_drawing_area(self, tmp_sch):
        tools = _placement_tools()
        info = tools["get_schematic_sheet_info"](tmp_sch)
        assert "paper" in info
        assert info["paper"]["width_mm"] > 0
        assert info["paper"]["height_mm"] > 0
        assert info["grid_mm"] == 1.27
        # drawing area starts at (0,0).
        assert info["drawing_area"]["min_x"] == 0.0
        assert info["drawing_area"]["min_y"] == 0.0
        # title block is in bottom-right, and recommended area excludes it.
        tb = info["title_block_default"]
        rec = info["recommended_area"]
        assert tb["max_x"] == info["paper"]["width_mm"]
        assert rec["max_y"] <= tb["min_y"]

    def test_missing_file_returns_error(self):
        tools = _placement_tools()
        out = tools["get_schematic_sheet_info"]("/nonexistent.kicad_sch")
        assert "error" in out


# ---------------------------------------------------------------------------
# find_free_area
# ---------------------------------------------------------------------------


class TestFindFreeArea:
    def test_returns_grid_aligned_candidates(self, tmp_sch):
        tools = _placement_tools()
        out = tools["find_free_area"](tmp_sch, width=10.0, height=10.0, max_candidates=3)
        assert "candidates" in out
        assert len(out["candidates"]) > 0
        for cand in out["candidates"]:
            x = cand["origin"]["x"]
            y = cand["origin"]["y"]
            # Snapped to 1.27 mm grid.
            assert abs(round(x / 1.27) * 1.27 - x) < 1e-6
            assert abs(round(y / 1.27) * 1.27 - y) < 1e-6
            bb = cand["bbox"]
            assert bb["max_x"] - bb["min_x"] == pytest.approx(10.0)
            assert bb["max_y"] - bb["min_y"] == pytest.approx(10.0)

    def test_avoids_existing_components(self, tmp_sch):
        """After adding a symbol, find_free_area must not return overlapping anchors."""
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            res = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=80.0,
                    y=80.0,
                )
            )
        assert res["success"], res
        bb = res["body_bbox"]
        assert bb is not None

        ptools = _placement_tools()
        out = ptools["find_free_area"](
            tmp_sch, width=5.0, height=5.0, max_candidates=20, margin=2.54
        )
        for cand in out["candidates"]:
            cb = cand["bbox"]
            # No overlap with the placed symbol's body bbox.
            sep = (
                cb["max_x"] <= bb["min_x"]
                or cb["min_x"] >= bb["max_x"]
                or cb["max_y"] <= bb["min_y"]
                or cb["min_y"] >= bb["max_y"]
            )
            assert sep, f"candidate {cb} overlaps placed bbox {bb}"

    def test_avoids_sheet_symbols(self, tmp_sch):
        """find_free_area must not return candidates overlapping a sheet symbol."""
        sheet_x, sheet_y, sheet_w, sheet_h = 50.0, 50.0, 60.0, 40.0
        mock_sheets = {
            "sheets": [
                {
                    "uuid": "test-uuid",
                    "sheet_name": "TestSheet",
                    "sheet_file": "child.kicad_sch",
                    "position": {"x": sheet_x, "y": sheet_y},
                    "size": {"width": sheet_w, "height": sheet_h},
                    "pins": [],
                }
            ]
        }
        tools = _placement_tools()
        with patch(
            "kcaa.tools.sheet_tools._list_sheet_symbols_impl",
            return_value=mock_sheets,
        ):
            out = tools["find_free_area"](
                tmp_sch, width=5.0, height=5.0, max_candidates=20, margin=2.54
            )
        assert "candidates" in out
        sheet_bb = {
            "min_x": sheet_x,
            "min_y": sheet_y,
            "max_x": sheet_x + sheet_w,
            "max_y": sheet_y + sheet_h,
        }
        for cand in out["candidates"]:
            cb = cand["bbox"]
            sep = (
                cb["max_x"] <= sheet_bb["min_x"]
                or cb["min_x"] >= sheet_bb["max_x"]
                or cb["max_y"] <= sheet_bb["min_y"]
                or cb["min_y"] >= sheet_bb["max_y"]
            )
            assert sep, f"candidate {cb} overlaps sheet bbox {sheet_bb}"

    def test_invalid_dimensions_error(self, tmp_sch):
        tools = _placement_tools()
        out = tools["find_free_area"](tmp_sch, width=0.0, height=10.0)
        assert "error" in out

    def test_oversized_request_returns_no_candidates(self, tmp_sch):
        tools = _placement_tools()
        out = tools["find_free_area"](tmp_sch, width=10000.0, height=10000.0)
        assert out["candidates"] == []

    def test_prefer_near_right_side_returns_closest(self, tmp_sch):
        """When prefer_near targets the right side of the sheet, the result
        must be the *true* nearest free grid cell — not some position from
        the far-left column.

        This guards against grid-scan implementations that fill their
        candidate buffer with left-edge positions and exit before reaching
        the bias area (e.g. row-major scan with a low max_collect cap).

        The bias area is deliberately crowded with an asymmetric cluster so
        the algorithm must reject occupied positions before settling on an
        unambiguous nearest free cell.  The test then independently verifies
        that no closer free grid cell exists.
        """
        from kcaa.tools.placement_helpers import GRID_MM as _GRID_MM
        from kcaa.tools.placement_helpers import PlacementHelpers
        from kcaa.tools.sheet_tools import _align_to_grid

        comps = _component_tools()
        mgr = _make_mock_manager()

        bias_x, bias_y = 170.0, 50.0
        cluster_positions = [
            (bias_x - 3.81, bias_y - 3.81),  # lower-left
            (bias_x, bias_y),  # center
            (bias_x + 3.81, bias_y + 3.81),  # upper-right
            (bias_x + 3.81, bias_y),  # right
            (bias_x, bias_y + 3.81),  # top
        ]
        # Collect body bboxes from every placed cluster component so we can
        # independently verify occupancy.
        cluster_bboxes: list[dict] = []
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            for i, (cx, cy) in enumerate(cluster_positions):
                res = asyncio.run(
                    comps["add_symbol_to_schematic"](
                        schematic_path=tmp_sch,
                        library_name=_LIB_NAME,
                        symbol_name=_SYM_NAME,
                        x=cx,
                        y=cy,
                    )
                )
                assert res["success"], f"Cluster[{i}] at ({cx},{cy}): {res}"
                cluster_bboxes.append(res["body_bbox"])

        width = 5.0
        height = 5.0
        margin = 3.81  # default used by the algorithm

        result = PlacementHelpers.find_free_area(
            schematic_path=tmp_sch,
            width=_align_to_grid(width),
            height=_align_to_grid(height),
            prefer_near={"x": bias_x, "y": bias_y},
            max_candidates=1,
        )
        cand = (result.get("candidates") or [None])[0]
        assert cand is not None, "Expected at least one candidate"
        ox = float(cand["origin"]["x"])
        oy = float(cand["origin"]["y"])

        # --- Helpers (same logic as the algorithm) ---

        def _inflate(bb: dict) -> dict:
            return {
                "min_x": bb["min_x"] - margin,
                "min_y": bb["min_y"] - margin,
                "max_x": bb["max_x"] + margin,
                "max_y": bb["max_y"] + margin,
            }

        def _overlaps(a: dict, b: dict) -> bool:
            return (
                a["min_x"] < b["max_x"]
                and a["max_x"] > b["min_x"]
                and a["min_y"] < b["max_y"]
                and a["max_y"] > b["min_y"]
            )

        # --- 1. Sanity: the result bbox must NOT overlap any cluster ---
        result_bb = {"min_x": ox, "min_y": oy, "max_x": ox + width, "max_y": oy + height}
        infl_bboxes = [_inflate(cbb) for cbb in cluster_bboxes]
        for i, ibb in enumerate(infl_bboxes):
            assert not _overlaps(result_bb, ibb), (
                f"result {result_bb} overlaps cluster[{i}] inflated {ibb}"
            )

        # --- 2. Prove the result is the TRUE nearest free grid cell ---
        # Walk ALL grid positions in distance order.  Every position CLOSER
        # to the bias than the result MUST overlap a cluster component.
        # If a closer free cell exists, the algorithm is broken.

        result_cx = ox + width / 2.0
        result_cy = oy + height / 2.0
        result_dist = ((result_cx - bias_x) ** 2 + (result_cy - bias_y) ** 2) ** 0.5

        # Drawing area bounds (A4 = 297×210, edge margin = 10 mm).
        sheet_w, sheet_h = 297.0, 210.0
        edge = 10.0

        def _snap_up(v: float) -> float:
            n = int(v / _GRID_MM)
            while n * _GRID_MM < v - 1e-9:
                n += 1
            return n * _GRID_MM

        x0 = _snap_up(edge)
        y0 = _snap_up(edge)
        x_hi = sheet_w - edge - width
        y_hi = sheet_h - edge - height

        closer_free = None
        x = x0
        while x <= x_hi + 1e-9:
            y = y0
            while y <= y_hi + 1e-9:
                pt_cx = x + width / 2.0
                pt_cy = y + height / 2.0
                pt_dist = ((pt_cx - bias_x) ** 2 + (pt_cy - bias_y) ** 2) ** 0.5
                if pt_dist >= result_dist - 1e-9:
                    y += _GRID_MM
                    continue
                pt_bb = {"min_x": x, "min_y": y, "max_x": x + width, "max_y": y + height}
                if not any(_overlaps(pt_bb, ibb) for ibb in infl_bboxes):
                    closer_free = (x, y, pt_dist)
                    break
                y += _GRID_MM
            if closer_free:
                break
            x += _GRID_MM

        assert closer_free is None, (
            f"Grid point ({closer_free[0]:.2f},{closer_free[1]:.2f}) "
            f"dist={closer_free[2]:.2f} is free and closer to bias "
            f"({bias_x},{bias_y}) than algorithm's pick ({ox:.2f},{oy:.2f}) "
            f"dist={result_dist:.2f} — the algorithm did NOT return the "
            f"true nearest free position!"
        )

    def test_for_symbol_returns_placement_anchor(self, tmp_sch):
        """When for_library/for_symbol are passed, each candidate must
        include a ``placement`` whose use as add_symbol_to_schematic(x, y)
        yields a body_bbox that matches the candidate bbox (i.e. origin vs
        symbol-anchor offset is handled by the tool, not the LLM).
        """
        tools = _placement_tools()
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            out = tools["find_free_area"](
                tmp_sch,
                for_library=_LIB_NAME,
                for_symbol=_SYM_NAME,
                max_candidates=1,
            )
            assert out["candidates"], out
            cand = out["candidates"][0]
            assert "placement" in cand
            res = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=cand["placement"]["x"],
                    y=cand["placement"]["y"],
                )
            )
        assert res["success"], res
        bb = res["body_bbox"]
        cb = cand["bbox"]
        # Body bbox of the placed symbol should match the free-area bbox
        # (within grid snap of 1.27 mm).
        for k in ("min_x", "min_y", "max_x", "max_y"):
            assert abs(bb[k] - cb[k]) <= 1.27, (k, bb[k], cb[k])


# ---------------------------------------------------------------------------
# place_symbol_relative + bbox in returns
# ---------------------------------------------------------------------------


class TestPlaceSymbolRelative:
    def test_add_returns_body_bbox(self, tmp_sch):
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            res = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=100.0,
                    y=100.0,
                )
            )
        assert res["success"], res
        bb = res["body_bbox"]
        assert bb is not None
        assert bb["max_x"] > bb["min_x"]
        assert bb["max_y"] > bb["min_y"]

    def test_relative_right_lands_to_the_right(self, tmp_sch):
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            anchor = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=120.0,
                    y=80.0,
                )
            )
            assert anchor["success"], anchor
            anchor_ref = anchor["reference_assigned"]
            anchor_bb = anchor["body_bbox"]

            placed = asyncio.run(
                comps["place_symbol_relative"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    anchor_reference=anchor_ref,
                    side="right",
                    gap=2.54,
                )
            )
        assert placed.get("success"), placed
        new_bb = placed["body_bbox"]
        assert new_bb is not None
        # New symbol must be entirely to the right of the anchor.
        assert new_bb["min_x"] >= anchor_bb["max_x"]

    def test_relative_invalid_side_errors(self, tmp_sch):
        comps = _component_tools()
        out = asyncio.run(
            comps["place_symbol_relative"](
                schematic_path=tmp_sch,
                library_name=_LIB_NAME,
                symbol_name=_SYM_NAME,
                anchor_reference="R99",
                side="diagonal",
            )
        )
        assert "error" in out

    def test_multi_unit_prediction_unions_every_unit(self):
        """Regression: place_symbol_relative must predict the union of EVERY
        placed unit's world bbox (each at unit_y = (N-1)*10), not just unit
        1, otherwise multi-unit symbols overlap the anchor."""
        from sexpdata import Symbol as S

        from kcaa.utils.symbol_geometry import (
            compute_unit_bboxes,
            lib_bbox_to_world,
            union_bboxes,
        )

        lib = [
            S("symbol"),
            "DUAL",
            [S("symbol"), "DUAL_1_1", [S("rectangle"), [S("start"), -1, -2], [S("end"), 1, 2]]],
            [S("symbol"), "DUAL_2_1", [S("rectangle"), [S("start"), -1, -2], [S("end"), 1, 2]]],
        ]
        unit_bbs = compute_unit_bboxes(lib)
        per_unit = [
            lib_bbox_to_world(bb, 0.0, (u - 1) * 10.0, 0, None)
            for u, bb in sorted(unit_bbs.items())
        ]
        merged = union_bboxes(per_unit)
        # Unit 1 placed at y=0, unit 2 at y=10. Y-flip turns lib (-2..+2) into
        # world (0-2..0+2)=(-2..2) for unit 1 and (10-2..10+2)=(8..12) for u2.
        assert merged is not None
        assert merged.min_y == pytest.approx(-2.0)
        assert merged.max_y == pytest.approx(12.0)
        assert (merged.max_y - merged.min_y) > 10.0  # spans both units

    def test_move_component_returns_bbox(self, tmp_sch):
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            added = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=60.0,
                    y=60.0,
                )
            )
            assert added["success"], added
            ref = added["reference_assigned"]

            moved = asyncio.run(
                comps["move_component"](
                    schematic_path=tmp_sch,
                    reference=ref,
                    x=90.0,
                    y=90.0,
                )
            )
        assert moved["success"], moved
        bb = moved["body_bbox"]
        assert bb is not None
        # The bbox should be near (90, 90).
        cx = (bb["min_x"] + bb["max_x"]) / 2.0
        cy = (bb["min_y"] + bb["max_y"]) / 2.0
        assert abs(cx - 90.0) < 5.0
        assert abs(cy - 90.0) < 5.0

    def test_move_component_snaps_to_grid(self, tmp_sch):
        """Regression: move_component must snap x/y to the 1.27 mm grid so
        pins remain on-grid (the system prompt advertises this behaviour)."""
        comps = _component_tools()
        mgr = _make_mock_manager()
        with patch(
            "kcaa.tools.component_edit_tools._get_index_manager",
            return_value=mgr,
        ):
            added = asyncio.run(
                comps["add_symbol_to_schematic"](
                    schematic_path=tmp_sch,
                    library_name=_LIB_NAME,
                    symbol_name=_SYM_NAME,
                    x=60.0,
                    y=60.0,
                )
            )
            assert added["success"], added
            ref = added["reference_assigned"]
            moved = asyncio.run(
                comps["move_component"](
                    schematic_path=tmp_sch,
                    reference=ref,
                    x=90.10,
                    y=90.10,
                )
            )
        assert moved["success"], moved
        # Read back the .kicad_sch and verify the placed symbol's `at` is
        # at the nearest grid point to (90.10, 90.10), i.e. 71*1.27=90.17.
        from kcaa.utils.netlist_parser import extract_netlist

        netlist = extract_netlist(tmp_sch)
        comp = netlist["components"][ref]
        x_pos, y_pos = comp["position"]["x"], comp["position"]["y"]
        # Each coordinate must be an exact multiple of 1.27.
        assert abs(round(x_pos / 1.27) * 1.27 - x_pos) < 1e-6
        assert abs(round(y_pos / 1.27) * 1.27 - y_pos) < 1e-6


def test_system_prompt_mentions_placement_workflow():
    from kicad_plugin.llm_client import build_system_prompt

    rendered = build_system_prompt("CTX")
    for needle in (
        "find_free_area",
        "place_symbol_relative",
        "1.27",
        "Y DOWN",
        "get_schematic_sheet_info",
        # Wiring guidance + correct tool names.
        "connect_pins_with_wire",
        "connect_points_with_wire",
        "closest",
        "Manhattan",
    ):
        assert needle in rendered, f"prompt missing {needle!r}"
    # Must NOT reference the old non-existent tool names.
    for forbidden in ("add_wire(", "create_junction"):
        assert forbidden not in rendered, f"prompt still references {forbidden!r}"
    assert "CTX" in rendered
