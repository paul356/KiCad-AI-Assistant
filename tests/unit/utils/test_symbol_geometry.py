"""Unit tests for kcaa.utils.symbol_geometry."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import sexpdata

from kcaa.utils.symbol_geometry import (
    BBox,
    bboxes_overlap,
    compute_lib_bbox,
    compute_unit_bboxes,
    inflate_bbox,
    lib_bbox_to_world,
    union_bboxes,
)


FIXTURES = Path(__file__).parent.parent / "tools" / "fixtures"


def _load_lib_symbol(name: str) -> list:
    """Load one (symbol "<name>" ...) S-expression from test_symbols.kicad_sym."""
    text = (FIXTURES / "test_symbols.kicad_sym").read_text()
    parsed = sexpdata.loads(text)
    for entry in parsed[1:]:
        if (
            isinstance(entry, list)
            and len(entry) >= 2
            and isinstance(entry[0], sexpdata.Symbol)
            and entry[0].value() == "symbol"
            and isinstance(entry[1], str)
            and entry[1] == name
        ):
            return entry
    raise KeyError(f"symbol {name!r} not found in fixture")


# ---------------------------------------------------------------------------
# BBox dataclass
# ---------------------------------------------------------------------------

class TestBBoxBasics:
    def test_width_height_and_dict(self):
        b = BBox(1.0, 2.0, 4.0, 6.0)
        assert b.width == 3.0
        assert b.height == 4.0
        d = b.to_dict()
        assert d == {
            "min_x": 1.0, "min_y": 2.0, "max_x": 4.0, "max_y": 6.0,
            "width": 3.0, "height": 4.0,
        }

    def test_inflate(self):
        b = inflate_bbox(BBox(0, 0, 2, 4), margin=1.0)
        assert b == BBox(-1.0, -1.0, 3.0, 5.0)

    def test_union_empty(self):
        assert union_bboxes([]) is None

    def test_union_combines(self):
        a = BBox(0, 0, 2, 2)
        b = BBox(-1, 1, 3, 5)
        assert union_bboxes([a, b]) == BBox(-1, 0, 3, 5)

    def test_overlap(self):
        assert bboxes_overlap(BBox(0, 0, 5, 5), BBox(3, 3, 8, 8))
        assert not bboxes_overlap(BBox(0, 0, 5, 5), BBox(5, 0, 10, 5))
        assert not bboxes_overlap(BBox(0, 0, 5, 5), BBox(6, 0, 10, 5))


# ---------------------------------------------------------------------------
# compute_unit_bboxes
# ---------------------------------------------------------------------------

class TestComputeUnitBboxes:
    def test_r_small_fixture(self):
        """R_Small has a horizontal polyline (graphics) in unit 0 plus two
        pins at (0, ±2.54) in unit 1. Bbox must include both pin tips."""
        lib = _load_lib_symbol("R_Small")
        bboxes = compute_unit_bboxes(lib)
        assert set(bboxes.keys()) == {1}
        b = bboxes[1]
        # X spans the polyline ±0.762; Y spans pin tips ±2.54.
        assert b.min_x == pytest.approx(-0.762)
        assert b.max_x == pytest.approx(0.762)
        assert b.min_y == pytest.approx(-2.54)
        assert b.max_y == pytest.approx(2.54)
        assert b.width == pytest.approx(1.524)
        assert b.height == pytest.approx(5.08)

    def test_compute_lib_bbox_convenience(self):
        lib = _load_lib_symbol("R_Small")
        b = compute_lib_bbox(lib)
        assert b == compute_unit_bboxes(lib)[1]

    def test_synthetic_rectangle_only(self):
        """Synthetic single-unit symbol with one rectangle."""
        from sexpdata import Symbol as S
        lib = [S("symbol"), "BOX", [
            S("symbol"), "BOX_1_1",
            [S("rectangle"), [S("start"), -3.0, -2.0], [S("end"), 5.0, 4.0]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b == BBox(-3.0, -2.0, 5.0, 4.0)

    def test_circle(self):
        from sexpdata import Symbol as S
        lib = [S("symbol"), "C1", [
            S("symbol"), "C1_1_1",
            [S("circle"), [S("center"), 1.0, 2.0], [S("radius"), 3.0]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b == BBox(-2.0, -1.0, 4.0, 5.0)

    def test_arc_cardinal_extremum(self):
        """Quarter arc from (1,0) through (cos45,sin45) to (0,1) on the unit
        circle centered at origin: bbox must reach (1, 0) and (0, 1) and not
        exceed them. No cardinal extremum lies *inside* this sweep, so the
        bbox is exactly the convex hull of the three points."""
        from sexpdata import Symbol as S
        lib = [S("symbol"), "A1", [
            S("symbol"), "A1_1_1",
            [S("arc"),
             [S("start"), 1.0, 0.0],
             [S("mid"), math.cos(math.pi / 4), math.sin(math.pi / 4)],
             [S("end"), 0.0, 1.0]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b.min_x == pytest.approx(0.0)
        assert b.max_x == pytest.approx(1.0)
        assert b.min_y == pytest.approx(0.0)
        assert b.max_y == pytest.approx(1.0)

    def test_arc_includes_unswept_cardinal(self):
        """Half-circle arc from (1,0) through (0,1) to (-1,0): the topmost
        point (0, 1) is the mid; bbox y-range is [0, 1] (cardinals at
        theta = π/2 inside sweep, at -π/2 outside)."""
        from sexpdata import Symbol as S
        lib = [S("symbol"), "A2", [
            S("symbol"), "A2_1_1",
            [S("arc"),
             [S("start"), 1.0, 0.0],
             [S("mid"), 0.0, 1.0],
             [S("end"), -1.0, 0.0]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b.max_y == pytest.approx(1.0)
        assert b.min_y == pytest.approx(0.0)
        # The arc must NOT include the bottom of the circle (-1, 0)→(1, 0)
        # passing through y=-1.
        assert b.min_y > -0.5

    def test_arc_major_clockwise_sweep_includes_all_cardinals(self):
        """Major (≈345°) clockwise arc on the unit circle: start at the bottom
        (0,-1), end just above start on the right at (sin15°,-cos15°), with
        mid at (-sin15°,-cos15°) forcing the sweep to wrap around the long
        way. The bbox MUST reach (-1,-1)..(1,1) — not just hug the bottom.

        Regression: previous implementation's CW _in_sweep test was
        algebraically wrong and rejected every cardinal extremum, returning a
        sliver bbox. See code-review feedback for the deterministic case.
        """
        from sexpdata import Symbol as S
        a = math.radians(15)
        lib = [S("symbol"), "MAJORCW", [
            S("symbol"), "MAJORCW_1_1",
            [S("arc"),
             [S("start"), 0.0, -1.0],
             [S("mid"), -math.sin(a), -math.cos(a)],
             [S("end"), math.sin(a), -math.cos(a)]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b.min_x == pytest.approx(-1.0, abs=1e-6)
        assert b.max_x == pytest.approx(1.0, abs=1e-6)
        assert b.min_y == pytest.approx(-1.0, abs=1e-6)
        assert b.max_y == pytest.approx(1.0, abs=1e-6)

    def test_arc_includes_off_defining_cardinal(self):
        """Arc from (1, -0.1) through (0, 1) to (-1, -0.1): the topmost
        cardinal extremum at (0, 1) coincides with mid; bbox max_y == 1."""
        from sexpdata import Symbol as S
        lib = [S("symbol"), "A3", [
            S("symbol"), "A3_1_1",
            # near-half arc but defining points don't include the rightmost
            [S("arc"),
             [S("start"), 0.5, math.sqrt(0.75)],
             [S("mid"), 1.0, 0.0],
             [S("end"), 0.5, -math.sqrt(0.75)]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        # The rightmost point (1, 0) is the mid → in extents.
        assert b.max_x == pytest.approx(1.0)

    def test_unit0_common_unioned_with_unit1(self):
        from sexpdata import Symbol as S
        lib = [S("symbol"), "X", [
            S("symbol"), "X_0_1",
            [S("rectangle"), [S("start"), -1, -1], [S("end"), 1, 1]],
        ], [
            S("symbol"), "X_1_1",
            [S("pin"), S("passive"), S("line"),
             [S("at"), 0.0, 5.0, 270], [S("length"), 1.0]],
        ]]
        b = compute_unit_bboxes(lib)[1]
        assert b == BBox(-1.0, -1.0, 1.0, 5.0)

    def test_multi_unit_separate_bboxes(self):
        from sexpdata import Symbol as S
        lib = [S("symbol"), "DUAL",
            [S("symbol"), "DUAL_1_1",
                [S("rectangle"), [S("start"), 0, 0], [S("end"), 2, 2]]],
            [S("symbol"), "DUAL_2_1",
                [S("rectangle"), [S("start"), 0, 0], [S("end"), 4, 4]]],
        ]
        bboxes = compute_unit_bboxes(lib)
        assert set(bboxes.keys()) == {1, 2}
        assert bboxes[1] == BBox(0, 0, 2, 2)
        assert bboxes[2] == BBox(0, 0, 4, 4)

    def test_empty_symbol_returns_empty(self):
        from sexpdata import Symbol as S
        lib = [S("symbol"), "EMPTY"]
        assert compute_unit_bboxes(lib) == {}


# ---------------------------------------------------------------------------
# lib_bbox_to_world
# ---------------------------------------------------------------------------

class TestLibBboxToWorld:
    def test_no_rotation(self):
        lib = BBox(-1.0, -2.0, 3.0, 4.0)
        w = lib_bbox_to_world(lib, sym_x=10.0, sym_y=20.0, rotation=0)
        # Y-flip: lib y_max (=4) → world y_min = 20 - 4 = 16
        #         lib y_min (=-2) → world y_max = 20 - (-2) = 22
        assert w == BBox(9.0, 16.0, 13.0, 22.0)

    def test_rotation_90_swaps_dimensions(self):
        """A 4×6 lib bbox rotated 90° CCW becomes 6×4 in world space."""
        lib = BBox(0.0, 0.0, 4.0, 6.0)
        w = lib_bbox_to_world(lib, sym_x=0.0, sym_y=0.0, rotation=90)
        # CCW rotation in lib Y-up: (x, y) → (-y, x).
        # Corners (0,0)(4,0)(4,6)(0,6) → (0,0)(0,4)(-6,4)(-6,0)
        # Then +(0,0) and Y-flip: (x, -y) for each.
        # → (0,0)(0,-4)(-6,-4)(-6,0). AABB = (-6,-4,0,0).
        assert w == BBox(-6.0, -4.0, 0.0, 0.0)
        assert w.width == 6.0 and w.height == 4.0

    def test_rotation_180(self):
        lib = BBox(-1.0, -2.0, 3.0, 4.0)
        w = lib_bbox_to_world(lib, sym_x=0.0, sym_y=0.0, rotation=180)
        # CCW 180: corners (-1,-2)(3,-2)(3,4)(-1,4) → (1,2)(-3,2)(-3,-4)(1,-4)
        # Y-flip: → (1,-2)(-3,-2)(-3,4)(1,4). AABB = (-3,-2,1,4).
        assert w == BBox(-3.0, -2.0, 1.0, 4.0)

    def test_rotation_270(self):
        lib = BBox(0.0, 0.0, 4.0, 6.0)
        w = lib_bbox_to_world(lib, sym_x=0.0, sym_y=0.0, rotation=270)
        # CCW 270: (x, y) → (y, -x). Corners → (0,0)(0,-4)(6,-4)(6,0).
        # Y-flip → (0,0)(0,4)(6,4)(6,0). AABB = (0, 0, 6, 4).
        assert w == BBox(0.0, 0.0, 6.0, 4.0)
        assert w.width == 6.0 and w.height == 4.0

    def test_translation(self):
        lib = BBox(-1.0, -1.0, 1.0, 1.0)
        w = lib_bbox_to_world(lib, sym_x=100.0, sym_y=50.0, rotation=0)
        assert w == BBox(99.0, 49.0, 101.0, 51.0)

    def test_mirror_y_flips_x(self):
        """mirror=y flips lib x: bbox (1, -1, 3, 1) → (-3, -1, -1, 1) lib,
        then placed at origin with no rotation."""
        lib = BBox(1.0, -1.0, 3.0, 1.0)
        w = lib_bbox_to_world(lib, 0.0, 0.0, rotation=0, mirror="y")
        # After mirror_y: corners x negated → bbox lib (-3, -1, -1, 1).
        # Y-flip alone: (-3, -1, -1, 1).
        assert w == BBox(-3.0, -1.0, -1.0, 1.0)

    def test_mirror_x_flips_y(self):
        lib = BBox(-1.0, 1.0, 1.0, 3.0)
        w = lib_bbox_to_world(lib, 0.0, 0.0, rotation=0, mirror="x")
        # mirror_x: lib y negated → lib bbox (-1, -3, 1, -1).
        # Y-flip: world y = -lib_y → world bbox (-1, 1, 1, 3).
        assert w == BBox(-1.0, 1.0, 1.0, 3.0)

    def test_invalid_rotation_snaps(self):
        lib = BBox(0.0, 0.0, 1.0, 1.0)
        # 45° → snaps to nearest of {0, 90, 180, 270} = 0
        w = lib_bbox_to_world(lib, 0.0, 0.0, rotation=45)
        # Width/height preserved; placement at origin with Y-flip.
        assert w.width == pytest.approx(1.0)
        assert w.height == pytest.approx(1.0)
