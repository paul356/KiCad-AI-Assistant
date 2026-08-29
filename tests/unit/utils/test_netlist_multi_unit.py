"""Tests for multi-unit symbol pin merging in SchematicParser.

skip/schematic yields one Symbol per unit under the same reference; without
merging, only the last unit's pins survive (the U2 case in
docs/skip_library_notes.md §6 lost its 9 power pins).
"""

import os

import pytest

from kcaa.utils.netlist_parser import SchematicParser

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "multi_unit.kicad_sch")


@pytest.fixture(scope="module")
def parsed():
    return SchematicParser(FIXTURE).parse()


class TestMultiUnitMerge:
    def test_all_units_merged_into_one_entry(self, parsed):
        comp = parsed["components"].get("U1")
        assert comp is not None, "U1 not found in parsed components"
        pins = comp.get("pins", [])
        assert len(pins) == 4, f"expected 4 pins across both units, got {len(pins)}"
        by_num = {p["num"]: p for p in pins}
        assert set(by_num) == {"1", "2", "3", "4"}

    def test_unit1_positions_rotated_ccw(self, parsed):
        """Unit 1 @ (100, 100, 90°): lib (0, +2.54) rotates to (-2.54, 0)."""
        by_num = {p["num"]: p for p in parsed["components"]["U1"]["pins"]}
        x, y = float(by_num["1"]["x"]), float(by_num["1"]["y"])
        assert (x, y) == pytest.approx((97.46, 100.0))

    def test_unit2_positions_unrotated(self, parsed):
        """Unit 2 @ (120, 100, 0°): lib (0, -2.54) stays put (Y-flipped)."""
        by_num = {p["num"]: p for p in parsed["components"]["U1"]["pins"]}
        x, y = float(by_num["4"]["x"]), float(by_num["4"]["y"])
        assert (x, y) == pytest.approx((120.0, 102.54))

    def test_directions_across_units(self, parsed):
        """Pin 1 (unit 1, placed 90°) exits left; pin 4 (unit 2, 0°) exits down."""
        by_num = {p["num"]: p for p in parsed["components"]["U1"]["pins"]}
        assert by_num["1"]["direction"] == "left"
        assert by_num["2"]["direction"] == "right"
        assert by_num["3"]["direction"] == "up"
        assert by_num["4"]["direction"] == "down"
