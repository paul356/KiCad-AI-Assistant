"""Tests for multi-unit symbol handling in SchematicParser.

skip/schematic yields one Symbol per unit under the same reference; each
unit is nested under that reference in ``units[unit]`` with its own anchor
and pins, so unit membership never has to be guessed from coordinates
(issue #89).
"""

import os

import pytest

from kcaa.utils.netlist_parser import SchematicParser

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "multi_unit.kicad_sch")


@pytest.fixture(scope="module")
def parsed():
    return SchematicParser(FIXTURE).parse()


class TestMultiUnitMerge:
    def test_units_nested_under_reference(self, parsed):
        comp = parsed["components"].get("U1")
        assert comp is not None, "U1 not found in parsed components"
        units = comp["units"]
        assert set(units) == {"1", "2"}, f"expected units 1 and 2, got {set(units)}"

    def test_no_flat_pins_or_position(self, parsed):
        """The ambiguous top-level position/pins are gone (issue #89)."""
        comp = parsed["components"]["U1"]
        assert "pins" not in comp
        assert "position" not in comp
        # No merged top-level bbox either — each unit reports its own.
        assert "body_bbox" not in comp

    def test_body_bbox_per_unit(self, parsed):
        """Each unit has its own world bbox spanning its pins; no top-level union."""
        comp = parsed["components"]["U1"]
        assert "body_bbox" not in comp
        u1 = comp["units"]["1"]["body_bbox"]
        # Unit 1 placed at 90°: pins at x 97.46..102.54, both on y=100.
        assert u1["min_x"] == pytest.approx(97.46)
        assert u1["max_x"] == pytest.approx(102.54)
        assert u1["min_y"] <= 100.0 <= u1["max_y"]
        u2 = comp["units"]["2"]["body_bbox"]
        # Unit 2 at 0°: pins stacked vertically at x=120.
        assert u2["min_x"] <= 120.0 <= u2["max_x"]
        assert u2["min_y"] == pytest.approx(97.46)
        assert u2["max_y"] == pytest.approx(102.54)

    def test_single_unit_bbox_nested(self, parsed):
        """R1's world bbox lives under units["1"] (single-unit component)."""
        comp = parsed["components"]["R1"]
        assert "body_bbox" not in comp
        u1 = comp["units"]["1"]["body_bbox"]
        assert u1["min_x"] < u1["max_x"]
        assert u1["min_y"] < u1["max_y"]

    def test_unit1_positions_rotated_ccw(self, parsed):
        """Unit 1 @ (100, 100, 90°): lib (0, +2.54) rotates to (-2.54, 0)."""
        u1 = parsed["components"]["U1"]["units"]["1"]
        assert (
            u1["position"]["x"],
            u1["position"]["y"],
            u1["position"]["rotation"],
        ) == pytest.approx((100.0, 100.0, 90.0))
        by_num = {p["num"]: p for p in u1["pins"]}
        assert set(by_num) == {"1", "2"}
        x, y = float(by_num["1"]["x"]), float(by_num["1"]["y"])
        assert (x, y) == pytest.approx((97.46, 100.0))
        x4, y4 = float(by_num["2"]["x"]), float(by_num["2"]["y"])
        assert (x4, y4) == pytest.approx((102.54, 100.0))

    def test_unit2_positions_unrotated(self, parsed):
        """Unit 2 @ (120, 100, 0°): lib (0, -2.54) stays put (Y-flipped)."""
        u2 = parsed["components"]["U1"]["units"]["2"]
        assert (
            u2["position"]["x"],
            u2["position"]["y"],
            u2["position"]["rotation"],
        ) == pytest.approx((120.0, 100.0, 0.0))
        by_num = {p["num"]: p for p in u2["pins"]}
        assert set(by_num) == {"3", "4"}
        x, y = float(by_num["4"]["x"]), float(by_num["4"]["y"])
        assert (x, y) == pytest.approx((120.0, 102.54))

    def test_directions_across_units(self, parsed):
        """Pin 1 (unit 1, placed 90°) exits left; pin 4 (unit 2, 0°) exits down."""
        comp = parsed["components"]["U1"]
        u1 = {p["num"]: p for p in comp["units"]["1"]["pins"]}
        u2 = {p["num"]: p for p in comp["units"]["2"]["pins"]}
        assert u1["1"]["direction"] == "left"
        assert u1["2"]["direction"] == "right"
        assert u2["3"]["direction"] == "up"
        assert u2["4"]["direction"] == "down"

    def test_single_unit_component_nested(self, parsed):
        """Single-unit components nest under units["1"] too."""
        comp = parsed["components"]["R1"]
        units = comp["units"]
        assert set(units) == {"1"}
        u1 = units["1"]
        assert (u1["position"]["x"], u1["position"]["y"]) == pytest.approx((161.29, 105.41))
        assert {p["num"] for p in u1["pins"]} == {"1", "2"}
