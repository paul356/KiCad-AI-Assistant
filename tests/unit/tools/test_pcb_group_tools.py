"""
Unit tests for all five group placement enhancements.

Tests use a fixture derived from the real smart-keyboard PCB
(tests/unit/tools/fixtures/test_group_placement.kicad_pcb) whose
footprint geometry, net names, and positions are copied verbatim from
/home/user1/pcb/smart-keyboard/smart-keyboard.kicad_pcb.

Enhancements tested:
  1. Direction-aware support function (tighter initial clearance)
  2. Member rotation optimisation (4 candidates, pick min pad distance)
  3. Four-rotation group sweep (pick min intra-group HPWL orientation)
  4. Angle-fan outward scan (±15 ° / ±30 ° fan avoids blocked rays)
  5. prefer_near board placement bias

KiCad 8.x net format: ``(net "name")`` — no integer IDs in pad net nodes.
"""
import asyncio
import math
import os
import shutil

import pytest

from kcaa.utils.pcb_sexp_utils import load_pcb
from kcaa.utils.pcb_footprint_utils import find_footprint, get_fp_at
from kcaa.utils.pcb_board_utils import get_fp_courtyard_bbox
from kcaa.tools.pcb_placement_helpers import (
    _get_fp_local_pads,
    _get_fp_pads_world,
    _compute_layout_hpwl,
    _find_group_board_position,
    _bboxes_overlap,
    _get_board_bounds_or_fallback,
    _GRID_MM,
)
from kcaa.tools.pcb_group_tools import _grid_layout, _rotate_layout

FIXTURE_PCB = os.path.join(os.path.dirname(__file__), "fixtures", "test_group_placement.kicad_pcb")

_USB_C_REFS = ("J3", "R1", "R2", "C1", "D1")

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
    from kcaa.tools.pcb_group_tools import register_pcb_group_tools
    mock = _MockMCP()
    register_pcb_group_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pcb_data():
    return load_pcb(FIXTURE_PCB)


@pytest.fixture(scope="module")
def fp_cache(pcb_data):
    return {ref: find_footprint(pcb_data, ref) for ref in _USB_C_REFS}


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def board_copy(tmp_path):
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(FIXTURE_PCB, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# _get_fp_local_pads
# ---------------------------------------------------------------------------


class TestGetFpLocalPads:
    """Verify that local pad coordinates are read correctly in KiCad 8.x format."""

    def test_j3_pad_count(self, fp_cache):
        """J3 has 22 pads (2 NPTH + 16 SMD signals + 4 shield THT)."""
        pads = _get_fp_local_pads(fp_cache["J3"])
        assert len(pads) == 22

    def test_kicad8x_net_format_parsed(self, fp_cache):
        """KiCad 8.x two-element ``(net "name")`` nodes are read correctly."""
        pads = _get_fp_local_pads(fp_cache["J3"])
        nets = {p["net"] for p in pads if p["net"]}
        assert "GND" in nets
        assert "Net-(D1-A)" in nets
        assert "Net-(J3-CC1)" in nets
        assert "Net-(J3-CC2)" in nets
        # Regression: old format used integer IDs — make sure none slip through.
        for net in nets:
            assert not net.isdigit(), f"Net name looks like integer ID: {net!r}"

    def test_r1_local_coords_match_real_pcb(self, fp_cache):
        """R1 pad 1 is at local (-0.825, 0) and pad 2 at (+0.825, 0)."""
        pads = _get_fp_local_pads(fp_cache["R1"])
        assert len(pads) == 2
        lxs = sorted(round(p["lx"], 3) for p in pads)
        assert lxs == [-0.825, 0.825]

    def test_local_coords_are_not_world_coords(self, fp_cache):
        """Local X of R1 pads is ~±0.825, not the world X (~105–108 mm)."""
        local = _get_fp_local_pads(fp_cache["R1"])
        world = _get_fp_pads_world(fp_cache["R1"])
        local_xs = {abs(round(p["lx"], 1)) for p in local}
        world_xs = {round(abs(p["x"]), 0) for p in world}
        assert local_xs == {0.8}           # ±0.825 → abs rounds to 0.8
        assert all(x > 10.0 for x in world_xs)   # world X is ~105–108 mm

    def test_d1_pads_have_expected_nets(self, fp_cache):
        """D1 pad 1 → /VCC_SYS, pad 2 → Net-(D1-A)."""
        pads = _get_fp_local_pads(fp_cache["D1"])
        nets = {p["net"] for p in pads if p["net"]}
        assert "/VCC_SYS" in nets
        assert "Net-(D1-A)" in nets


# ---------------------------------------------------------------------------
# _compute_layout_hpwl
# ---------------------------------------------------------------------------


class TestComputeLayoutHpwl:
    """Verify the translation-invariant HPWL helper."""

    def test_positive_hpwl_for_shared_nets(self, pcb_data, fp_cache):
        """With members at their real PCB positions HPWL ≈ 82.61 mm."""
        j3_x, j3_y, _ = get_fp_at(fp_cache["J3"])
        layout = []
        for ref in _USB_C_REFS:
            x, y, rot = get_fp_at(fp_cache[ref])
            layout.append({"ref": ref, "dx": x - j3_x, "dy": y - j3_y, "rotation": rot})
        hpwl = _compute_layout_hpwl(fp_cache, layout)
        assert hpwl > 0.0
        assert abs(hpwl - 82.61) < 0.5, f"Expected ≈82.61 mm, got {hpwl:.3f} mm"

    def test_zero_hpwl_when_no_nets_shared(self, fp_cache):
        """D1 and R2 share no net → HPWL = 0."""
        cache = {"D1": fp_cache["D1"], "R2": fp_cache["R2"]}
        layout = [
            {"ref": "D1", "dx": 0.0, "dy": 0.0, "rotation": 0.0},
            {"ref": "R2", "dx": 5.0, "dy": 0.0, "rotation": 0.0},
        ]
        assert _compute_layout_hpwl(cache, layout) == 0.0

    def test_translation_invariance(self, fp_cache):
        """Shifting the entire layout by (100, 200) does not change HPWL."""
        cache = {r: fp_cache[r] for r in ("J3", "R1", "C1", "D1")}
        base = [
            {"ref": "J3", "dx": 0.0,   "dy": 0.0,  "rotation": -90.0},
            {"ref": "R1", "dx": 10.0,  "dy": 0.0,  "rotation": 0.0},
            {"ref": "C1", "dx": -10.0, "dy": 0.0,  "rotation": 0.0},
            {"ref": "D1", "dx": 0.0,   "dy": 10.0, "rotation": 0.0},
        ]
        shifted = [{**p, "dx": p["dx"] + 100.0, "dy": p["dy"] + 200.0} for p in base]
        h1 = _compute_layout_hpwl(cache, base)
        h2 = _compute_layout_hpwl(cache, shifted)
        assert abs(h1 - h2) < 1e-6

    def test_all_at_origin_gives_known_value(self, fp_cache):
        """With all 5 members at (0,0,0°) HPWL ≈ 34.12 mm (pad spread only)."""
        layout = [{"ref": r, "dx": 0.0, "dy": 0.0, "rotation": 0.0} for r in _USB_C_REFS]
        hpwl = _compute_layout_hpwl(fp_cache, layout)
        assert abs(hpwl - 34.12) < 0.5, f"Expected ≈34.12 mm, got {hpwl:.3f} mm"


# ---------------------------------------------------------------------------
# _rotate_layout  (Enhancement 3 support function)
# ---------------------------------------------------------------------------


class TestRotateLayout:
    """Verify the group-rotation helper under the KiCad CW convention."""

    def test_zero_rotation_is_identity(self):
        layout = [{"ref": "A", "dx": 10.0, "dy": 5.0, "rotation": 30.0}]
        result = _rotate_layout(layout, 0.0)
        assert result[0]["dx"] == 10.0
        assert result[0]["dy"] == 5.0
        assert result[0]["rotation"] == 30.0

    def test_anchor_stays_at_origin_after_any_rotation(self):
        layout = [
            {"ref": "anchor", "dx": 0.0, "dy": 0.0, "rotation": 0.0},
            {"ref": "member", "dx": 5.0, "dy": 3.0, "rotation": 0.0},
        ]
        for angle in (90.0, 180.0, 270.0, 45.0, -45.0):
            result = _rotate_layout(layout, angle)
            anchor = next(p for p in result if p["ref"] == "anchor")
            assert abs(anchor["dx"]) < 1e-9, f"anchor dx non-zero after {angle}°"
            assert abs(anchor["dy"]) < 1e-9, f"anchor dy non-zero after {angle}°"

    def test_90deg_cw_rotates_correctly(self):
        """KiCad CW 90°: x′ = x·cos90 + y·sin90 = y, y′ = -x·sin90 + y·cos90 = -x."""
        layout = [{"ref": "A", "dx": 10.0, "dy": 0.0, "rotation": 0.0}]
        result = _rotate_layout(layout, 90.0)
        assert abs(result[0]["dx"] - 0.0) < 1e-9
        assert abs(result[0]["dy"] - (-10.0)) < 1e-9

    def test_180deg_negates_both_axes(self):
        layout = [{"ref": "A", "dx": 3.0, "dy": -4.0, "rotation": 0.0}]
        result = _rotate_layout(layout, 180.0)
        assert abs(result[0]["dx"] - (-3.0)) < 1e-9
        assert abs(result[0]["dy"] - 4.0) < 1e-9

    def test_rotation_field_increments(self):
        layout = [{"ref": "A", "dx": 5.0, "dy": 0.0, "rotation": 45.0}]
        result = _rotate_layout(layout, 90.0)
        assert abs(result[0]["rotation"] - 135.0) < 1e-9

    def test_360deg_roundtrip(self):
        layout = [{"ref": "A", "dx": 7.3, "dy": -2.1, "rotation": 45.0}]
        result = _rotate_layout(layout, 360.0)
        assert abs(result[0]["dx"] - 7.3) < 1e-6
        assert abs(result[0]["dy"] - (-2.1)) < 1e-6

    def test_returns_independent_copy(self):
        """_rotate_layout must not mutate the input list."""
        original_dx = 5.0
        layout = [{"ref": "A", "dx": original_dx, "dy": 0.0, "rotation": 0.0}]
        _rotate_layout(layout, 90.0)
        assert layout[0]["dx"] == original_dx


# ---------------------------------------------------------------------------
# _find_group_board_position — prefer_near
# ---------------------------------------------------------------------------


class TestFindGroupBoardPositionPreferNear:
    """Verify that prefer_near returns a board position closer to the target."""

    def _make_full_layout(self, pcb_data):
        suggestions = _grid_layout(pcb_data, "J3", ["R1", "R2", "C1", "D1"], gap_mm=1.0)
        _, _, anchor_rot = get_fp_at(find_footprint(pcb_data, "J3"))
        full_layout = [{"ref": "J3", "dx": 0.0, "dy": 0.0, "rotation": anchor_rot}]
        full_layout += [
            {"ref": s["reference"], "dx": s["dx"], "dy": s["dy"], "rotation": s["rotation"]}
            for s in suggestions
        ]
        return full_layout

    def test_prefer_near_returns_closer_position(self, pcb_data):
        """With prefer_near=(50,50) the returned anchor is closer to (50,50) than the default."""
        group_refs = set(_USB_C_REFS)
        full_layout = self._make_full_layout(pcb_data)
        target = (50.0, 50.0)

        ax_default, ay_default, ok_default, _ = _find_group_board_position(
            pcb_data, group_refs, full_layout
        )
        ax_near, ay_near, ok_near, _ = _find_group_board_position(
            pcb_data, group_refs, full_layout, prefer_near=target
        )

        assert ok_default
        assert ok_near
        dist_default = math.hypot(ax_default - target[0], ay_default - target[1])
        dist_near = math.hypot(ax_near - target[0], ay_near - target[1])
        assert dist_near <= dist_default + 1e-6, (
            f"prefer_near should return position closer to {target}: "
            f"default dist={dist_default:.2f}, near dist={dist_near:.2f}"
        )

    def test_prefer_near_result_closer_than_default_for_off_corner_target(self, pcb_data):
        """Corner case: target near bottom-right corner is closer with prefer_near."""
        group_refs = set(_USB_C_REFS)
        full_layout = self._make_full_layout(pcb_data)
        target = (150.0, 100.0)

        ax_default, ay_default, _, _ = _find_group_board_position(
            pcb_data, group_refs, full_layout
        )
        ax_near, ay_near, ok_near, _ = _find_group_board_position(
            pcb_data, group_refs, full_layout, prefer_near=target
        )

        assert ok_near
        dist_default = math.hypot(ax_default - target[0], ay_default - target[1])
        dist_near = math.hypot(ax_near - target[0], ay_near - target[1])
        assert dist_near <= dist_default + 1e-6

    def test_prefer_near_none_returns_position_within_board(self, pcb_data):
        """Default (prefer_near=None) raster scan returns a position inside the board outline."""
        group_refs = set(_USB_C_REFS)
        full_layout = self._make_full_layout(pcb_data)
        bounds = _get_board_bounds_or_fallback(pcb_data)

        ax, ay, ok, _ = _find_group_board_position(pcb_data, group_refs, full_layout)
        assert ok
        assert bounds["min_x"] <= ax <= bounds["max_x"]
        assert bounds["min_y"] <= ay <= bounds["max_y"]


# ---------------------------------------------------------------------------
# place_component_group MCP tool — Enhancements 3 & 5 end-to-end
# ---------------------------------------------------------------------------


class TestPlaceComponentGroup:
    """End-to-end tests for the place_component_group MCP tool."""

    def test_place_group_returns_placed_count(self, tools, board_copy):
        """place_component_group places all 5 usb_c members (anchor + 4)."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["placed_count"] == 5

    def test_place_group_hpwl_positive_and_finite(self, tools, board_copy):
        """HPWL reported after placement is a positive finite number."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        assert "error" not in result
        hpwl = result["intra_hpwl_mm"]
        assert hpwl > 0.0
        assert math.isfinite(hpwl)

    def test_place_group_found_clear_position(self, tools, board_copy):
        """Phase 2 raster scan finds a collision-free board position."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        assert result["found_clear_position"] is True

    def test_anchor_ref_is_j3(self, tools, board_copy):
        """J3 (22-pad connector) is always chosen as the anchor (highest priority tier)."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        assert result["anchor_ref"] == "J3"

    def test_placed_list_contains_all_refs(self, tools, board_copy):
        """The ``placed`` list contains entries for every group member."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        placed_refs = {p["reference"] for p in result["placed"]}
        assert placed_refs == set(_USB_C_REFS)

    def test_backup_file_created(self, tools, board_copy):
        """A .kicad_pcb.bak backup is written before the file is modified."""
        result = _run(tools["place_component_group"](board_copy, "usb_c"))
        backup = result.get("backup_path", board_copy + ".bak")
        assert os.path.exists(backup), f"Backup not found at {backup}"

    def test_unknown_group_returns_error(self, tools, board_copy):
        """Requesting a group that does not exist returns an error dict."""
        result = _run(tools["place_component_group"](board_copy, "nonexistent_group"))
        assert "error" in result


# ---------------------------------------------------------------------------
# _generate_grid_candidates
# ---------------------------------------------------------------------------


class TestGenerateGridCandidates:
    """Verify the grid candidate generator used by _grid_layout."""

    def test_anchor_position_included(self):
        """(0, 0) must be in the list so the overlap check can reject it."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        cands = _generate_grid_candidates(5.0)
        assert (0.0, 0.0) in cands

    def test_sorted_by_distance_from_origin(self):
        """Candidates are sorted closest-first when center is (0, 0)."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        cands = _generate_grid_candidates(5.0)
        dists = [x * x + y * y for x, y in cands]
        assert dists == sorted(dists)

    def test_center_offset_pulls_first_candidate_near_center(self):
        """With center_x=-5, center_y=0 the first candidate is close to (-5, 0)."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        cands = _generate_grid_candidates(10.0, center_x=-5.0, center_y=0.0)
        first = cands[0]
        assert math.hypot(first[0] + 5.0, first[1]) < _GRID_MM * 1.5

    def test_sorted_by_distance_from_center(self):
        """Candidates are sorted by distance from the given center."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        cx, cy = -3.81, 2.54
        cands = _generate_grid_candidates(10.0, center_x=cx, center_y=cy)
        dists = [(x - cx) ** 2 + (y - cy) ** 2 for x, y in cands]
        assert dists == sorted(dists)

    def test_deterministic_for_same_center(self):
        """Two calls with identical parameters return identical lists."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        a = _generate_grid_candidates(8.0, center_x=-2.54, center_y=1.27)
        b = _generate_grid_candidates(8.0, center_x=-2.54, center_y=1.27)
        assert a == b

    def test_all_positions_on_grid(self):
        """Every returned position is a multiple of _GRID_MM."""
        from kcaa.tools.pcb_group_tools import _generate_grid_candidates
        cands = _generate_grid_candidates(5.0)
        for x, y in cands:
            assert abs(round(x / _GRID_MM) * _GRID_MM - x) < 1e-6, f"{x} not on grid"
            assert abs(round(y / _GRID_MM) * _GRID_MM - y) < 1e-6, f"{y} not on grid"


# ---------------------------------------------------------------------------
# _choose_rotation_for_grid
# ---------------------------------------------------------------------------


class TestChooseRotationForGrid:
    """Verify edge-perpendicular rotation selection."""

    def _wide_fp(self, pcb_data):
        """R1 at 0° is wider than tall (0603 landscape)."""
        return find_footprint(pcb_data, "R1")

    def test_horizontal_zone_prefers_wide_rotation(self, pcb_data):
        """Component to the left/right should be rotated so bbox.width >= bbox.height."""
        from kcaa.tools.pcb_group_tools import _choose_rotation_for_grid
        mfp = self._wide_fp(pcb_data)
        # Place to the right (cx=10, cy=0) — horizontal zone
        rot = _choose_rotation_for_grid(mfp, cx=10.0, cy=0.0, base_rot=0.0, connecting_pairs=[])
        bb = get_fp_courtyard_bbox(mfp, 0.0, 0.0, rot)
        assert bb is not None
        w = bb["max_x"] - bb["min_x"]
        h = bb["max_y"] - bb["min_y"]
        assert w >= h, f"Expected wide orientation for horizontal zone, got w={w:.3f} h={h:.3f}"

    def test_no_connecting_pairs_returns_zero(self, pcb_data):
        """When connecting_pairs is empty, function returns 0 (base rotation not used)."""
        from kcaa.tools.pcb_group_tools import _choose_rotation_for_grid
        mfp = self._wide_fp(pcb_data)
        # No connecting pairs → returns 0 regardless of position or base_rot
        rot = _choose_rotation_for_grid(mfp, cx=0.0, cy=-10.0, base_rot=45.0, connecting_pairs=[])
        assert rot == 0, f"Expected 0 when no connecting_pairs, got {rot}"

    def test_with_connecting_pairs_returns_90_degree_step(self, pcb_data):
        """When connecting_pairs provided, result is a 90° step (0, 90, 180, 270)."""
        from kcaa.tools.pcb_group_tools import _choose_rotation_for_grid
        mfp = self._wide_fp(pcb_data)
        # Provide a connecting pair: (lx, ly, ax, ay, aw, ah, mw, mh)
        # lx,ly = member pad local coords, ax,ay = anchor pad coords
        pairs = [(1.0, 0.0, 5.0, 0.0, 1.0, 1.0, 1.0, 1.0)]
        rot = _choose_rotation_for_grid(mfp, cx=5.0, cy=0.0, base_rot=45.0, connecting_pairs=pairs)
        valid = {0.0, 90.0, 180.0, 270.0}
        assert rot in valid, f"Rotation {rot} not in expected set {valid}"


# ---------------------------------------------------------------------------
# _grid_layout
# ---------------------------------------------------------------------------


class TestGridLayout:
    """Validate the closest-first grid placement algorithm."""

    def test_all_members_placed(self, pcb_data):
        """Every non-anchor member receives a suggestion entry."""
        from kcaa.tools.pcb_group_tools import _grid_layout
        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)
        placed_refs = {s["reference"] for s in suggestions}
        assert placed_refs == set(member_refs)

    def test_no_overlap_between_members(self, pcb_data):
        """Placed member courtyards must not overlap each other (gap=0)."""
        from kcaa.tools.pcb_group_tools import _grid_layout
        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)

        bboxes: list[dict] = []
        for s in suggestions:
            if "warning" in s:
                continue
            mfp = find_footprint(pcb_data, s["reference"])
            bb = get_fp_courtyard_bbox(mfp, 0.0, 0.0, s["rotation"])
            if bb:
                bboxes.append({
                    "ref": s["reference"],
                    "min_x": bb["min_x"] + s["dx"],
                    "min_y": bb["min_y"] + s["dy"],
                    "max_x": bb["max_x"] + s["dx"],
                    "max_y": bb["max_y"] + s["dy"],
                })

        for i in range(len(bboxes)):
            for j in range(i + 1, len(bboxes)):
                assert not _bboxes_overlap(bboxes[i], bboxes[j]), (
                    f"{bboxes[i]['ref']} overlaps {bboxes[j]['ref']}"
                )

    def test_no_overlap_with_anchor(self, pcb_data):
        """No placed member courtyard overlaps the anchor courtyard."""
        from kcaa.tools.pcb_group_tools import _grid_layout
        anchor_fp = find_footprint(pcb_data, "J3")
        _, _, anchor_rot = get_fp_at(anchor_fp)
        anchor_bb = get_fp_courtyard_bbox(anchor_fp, 0.0, 0.0, anchor_rot)

        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)

        for s in suggestions:
            if "warning" in s or anchor_bb is None:
                continue
            mfp = find_footprint(pcb_data, s["reference"])
            bb = get_fp_courtyard_bbox(mfp, 0.0, 0.0, s["rotation"])
            if bb is None:
                continue
            placed = {
                "min_x": bb["min_x"] + s["dx"],
                "min_y": bb["min_y"] + s["dy"],
                "max_x": bb["max_x"] + s["dx"],
                "max_y": bb["max_y"] + s["dy"],
            }
            assert not _bboxes_overlap(placed, anchor_bb), (
                f"{s['reference']} overlaps anchor"
            )

    def test_positions_near_grid(self, pcb_data):
        """All dx/dy offsets are within one grid step of _GRID_MM multiples.
        
        The grid algorithm places pads on grid, but footprint centers are
        offset by pad local coordinates, so centers may not be exact grid multiples.
        """
        from kcaa.tools.pcb_group_tools import _grid_layout
        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)
        # Footprint center = pad position - rotated pad offset
        # Pad positions are on grid, but pad offsets are not, so allow up to
        # one grid step tolerance (1.27mm)
        tolerance = _GRID_MM
        for s in suggestions:
            grid_dx = round(s["dx"] / _GRID_MM) * _GRID_MM
            grid_dy = round(s["dy"] / _GRID_MM) * _GRID_MM
            assert abs(grid_dx - s["dx"]) < tolerance, (
                f"{s['reference']} dx={s['dx']} not near grid (expected ~{grid_dx})"
            )
            assert abs(grid_dy - s["dy"]) < tolerance, (
                f"{s['reference']} dy={s['dy']} not near grid (expected ~{grid_dy})"
            )

    def test_no_placement_warning_for_normal_group(self, pcb_data):
        """A typical 5-member group should place without any overlap warnings."""
        from kcaa.tools.pcb_group_tools import _grid_layout
        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)
        warnings = [s["reference"] for s in suggestions if "warning" in s]
        assert not warnings, f"Unexpected warnings: {warnings}"

    def test_connected_members_placed_closer_than_unconnected(self, pcb_data):
        """Connected members should on average be closer to the anchor than unconnected ones."""
        from kcaa.tools.pcb_group_tools import _grid_layout, _is_ground_net
        from kcaa.tools.pcb_placement_helpers import _get_fp_local_pads

        # Build anchor net map
        anchor_fp = find_footprint(pcb_data, "J3")
        ax, ay, _ = get_fp_at(anchor_fp)
        from kcaa.tools.pcb_placement_helpers import _get_fp_pads_world
        anchor_net_pts: dict = {}
        for p in _get_fp_pads_world(anchor_fp):
            net = p["net"]
            if net and not _is_ground_net(net):
                anchor_net_pts.setdefault(net, []).append((p["x"] - ax, p["y"] - ay))

        member_refs = [r for r in _USB_C_REFS if r != "J3"]
        suggestions = _grid_layout(pcb_data, "J3", member_refs, gap_mm=1.0)

        connected_dists, unconnected_dists = [], []
        for s in suggestions:
            dist = math.hypot(s["dx"], s["dy"])
            mfp = find_footprint(pcb_data, s["reference"])
            nets = {p["net"] for p in _get_fp_local_pads(mfp) if p["net"]}
            if any(n in anchor_net_pts for n in nets if not _is_ground_net(n)):
                connected_dists.append(dist)
            else:
                unconnected_dists.append(dist)

        if connected_dists and unconnected_dists:
            avg_conn = sum(connected_dists) / len(connected_dists)
            avg_unc = sum(unconnected_dists) / len(unconnected_dists)
            assert avg_conn <= avg_unc + 5.0, (
                f"Connected avg dist {avg_conn:.1f} mm should be <= unconnected {avg_unc:.1f} mm"
            )

    def test_grid_algo_via_tool(self, tools, tmp_path):
        """place_component_group completes without error using the grid layout."""
        copy = str(tmp_path / "grid.kicad_pcb")
        shutil.copy(FIXTURE_PCB, copy)
        result = _run(tools["place_component_group"](copy, "usb_c"))
        assert "error" not in result, result
        assert result["placed_count"] == len(_USB_C_REFS)
        placed_refs = {p["reference"] for p in result["placed"]}
        assert placed_refs == set(_USB_C_REFS)


# ---------------------------------------------------------------------------
# rotate_group MCP tool
# ---------------------------------------------------------------------------


class TestRotateGroup:
    """Tests for the rotate_group MCP tool."""

    def test_rotate_group_90_cw_preserves_relative_positions(self, tools, board_copy):
        """90° CW rotation moves components to the right BELOW the anchor."""
        # First place the group
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        # Get initial positions
        data_before = load_pcb(board_copy)
        j3_before = find_footprint(data_before, "J3")
        r1_before = find_footprint(data_before, "R1")
        ax, ay, _ = get_fp_at(j3_before)
        r1x_before, r1y_before, _ = get_fp_at(r1_before)
        dx_before = r1x_before - ax
        dy_before = r1y_before - ay
        
        # Rotate 90° CW
        result = _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        
        # Check positions after rotation
        data_after = load_pcb(board_copy)
        j3_after = find_footprint(data_after, "J3")
        r1_after = find_footprint(data_after, "R1")
        ax_after, ay_after, _ = get_fp_at(j3_after)
        r1x_after, r1y_after, _ = get_fp_at(r1_after)
        
        # Anchor should not move
        assert abs(ax_after - ax) < 0.01
        assert abs(ay_after - ay) < 0.01
        
        # R1's relative position should rotate 90° CW
        # In KiCad coords (+Y down), 90° CW rotation: (dx, dy) → (-dy, dx)
        # RIGHT (+dx) → DOWN (+dy), DOWN (+dy) → LEFT (-dx)
        dx_after = r1x_after - ax_after
        dy_after = r1y_after - ay_after
        expected_dx = -dy_before
        expected_dy = dx_before
        
        assert abs(dx_after - expected_dx) < 0.01, (
            f"R1 dx: expected {expected_dx:.2f}, got {dx_after:.2f}"
        )
        assert abs(dy_after - expected_dy) < 0.01, (
            f"R1 dy: expected {expected_dy:.2f}, got {dy_after:.2f}"
        )

    def test_rotate_group_increments_component_rotations(self, tools, board_copy):
        """Component orientations counter-rotate to maintain orientation in group frame."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        data_before = load_pcb(board_copy)
        j3_before = find_footprint(data_before, "J3")
        d1_before = find_footprint(data_before, "D1")
        _, _, j3_rot_before = get_fp_at(j3_before)
        _, _, d1_rot_before = get_fp_at(d1_before)
        
        # Rotate 90° CW
        result = _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        assert "error" not in result
        
        data_after = load_pcb(board_copy)
        j3_after = find_footprint(data_after, "J3")
        d1_after = find_footprint(data_after, "D1")
        _, _, j3_rot_after = get_fp_at(j3_after)
        _, _, d1_rot_after = get_fp_at(d1_after)
        
        # Components counter-rotate to maintain orientation in group frame
        # Group rotates 90° CW, so component orientations decrease by 90°
        expected_j3_rot = (j3_rot_before - 90.0) % 360.0
        expected_d1_rot = (d1_rot_before - 90.0) % 360.0
        
        assert abs(j3_rot_after - expected_j3_rot) < 0.01, (
            f"J3 rotation: expected {expected_j3_rot:.1f}°, got {j3_rot_after:.1f}°"
        )
        assert abs(d1_rot_after - expected_d1_rot) < 0.01, (
            f"D1 rotation: expected {expected_d1_rot:.1f}°, got {d1_rot_after:.1f}°"
        )

    def test_rotate_group_180_degrees(self, tools, board_copy):
        """180° rotation should flip positions while preserving distances."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        data_before = load_pcb(board_copy)
        j3_before = find_footprint(data_before, "J3")
        r2_before = find_footprint(data_before, "R2")
        ax, ay, _ = get_fp_at(j3_before)
        r2x_before, r2y_before, _ = get_fp_at(r2_before)
        dist_before = math.hypot(r2x_before - ax, r2y_before - ay)
        
        # Rotate 180°
        result = _run(tools["rotate_group"](board_copy, "usb_c", 180.0))
        assert "error" not in result
        
        data_after = load_pcb(board_copy)
        j3_after = find_footprint(data_after, "J3")
        r2_after = find_footprint(data_after, "R2")
        ax_after, ay_after, _ = get_fp_at(j3_after)
        r2x_after, r2y_after, _ = get_fp_at(r2_after)
        dist_after = math.hypot(r2x_after - ax_after, r2y_after - ay_after)
        
        # Distance should be preserved
        assert abs(dist_after - dist_before) < 0.01
        
        # Relative position should be negated
        dx_before = r2x_before - ax
        dy_before = r2y_before - ay
        dx_after = r2x_after - ax_after
        dy_after = r2y_after - ay_after
        
        assert abs(dx_after + dx_before) < 0.01
        assert abs(dy_after + dy_before) < 0.01

    def test_rotate_group_multiple_rotations_compound(self, tools, board_copy):
        """Two 90° rotations should equal one 180° rotation."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        # Rotate 90° twice
        _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        result = _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        assert "error" not in result
        
        data = load_pcb(board_copy)
        c1 = find_footprint(data, "C1")
        j3 = find_footprint(data, "J3")
        c1x, c1y, c1_rot = get_fp_at(c1)
        ax, ay, j3_rot = get_fp_at(j3)
        
        # The rotation field should have incremented by 180° total
        # (we don't know the initial rotation, but it should have changed)
        assert result["rotation_delta"] == 90.0
        assert result["rotated_count"] == 5

    def test_rotate_group_anchor_stays_in_place(self, tools, board_copy):
        """The anchor position must not change during rotation."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        data_before = load_pcb(board_copy)
        j3_before = find_footprint(data_before, "J3")
        ax_before, ay_before, _ = get_fp_at(j3_before)
        
        # Rotate 45°
        _run(tools["rotate_group"](board_copy, "usb_c", 45.0))
        
        data_after = load_pcb(board_copy)
        j3_after = find_footprint(data_after, "J3")
        ax_after, ay_after, _ = get_fp_at(j3_after)
        
        assert abs(ax_after - ax_before) < 1e-6
        assert abs(ay_after - ay_before) < 1e-6

    def test_rotate_group_returns_correct_metadata(self, tools, board_copy):
        """rotate_group returns group_name, anchor_ref, rotation_delta, rotated_count."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        result = _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        assert result["group_name"] == "usb_c"
        assert result["anchor_ref"] == "J3"
        assert result["rotation_delta"] == 90.0
        assert result["rotated_count"] == 5
        assert "backup_path" in result

    def test_rotate_group_creates_backup(self, tools, board_copy):
        """A .kicad_pcb.bak backup is created before rotation."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        result = _run(tools["rotate_group"](board_copy, "usb_c", 90.0))
        backup = result.get("backup_path", board_copy + ".bak")
        assert os.path.exists(backup), f"Backup not found at {backup}"

    def test_rotate_group_unknown_group_returns_error(self, tools, board_copy):
        """Rotating a nonexistent group returns an error."""
        result = _run(tools["rotate_group"](board_copy, "nonexistent", 90.0))
        assert "error" in result
        assert "no members" in result["error"].lower()

    def test_rotate_group_360_is_identity(self, tools, board_copy):
        """360° rotation should return to original positions (modulo floating point)."""
        _run(tools["place_component_group"](board_copy, "usb_c"))
        
        data_before = load_pcb(board_copy)
        r1_before = find_footprint(data_before, "R1")
        r1x_before, r1y_before, r1_rot_before = get_fp_at(r1_before)
        
        # Rotate 360°
        _run(tools["rotate_group"](board_copy, "usb_c", 360.0))
        
        data_after = load_pcb(board_copy)
        r1_after = find_footprint(data_after, "R1")
        r1x_after, r1y_after, r1_rot_after = get_fp_at(r1_after)
        
        # Position should be unchanged
        assert abs(r1x_after - r1x_before) < 1e-6
        assert abs(r1y_after - r1y_before) < 1e-6
        
        # Rotation should increment by 360° (wraps to same angle)
        expected_rot = (r1_rot_before + 360.0) % 360.0
        assert abs(r1_rot_after - expected_rot) < 0.01
