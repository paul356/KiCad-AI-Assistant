"""
Tests for SchematicParser / netlist_parser pin-angle extraction.
"""

import os

import pytest

from kcaa.utils.netlist_parser import SchematicParser

# Fixture shared with the tools tests - contains R1…R7 and C1.
FIXTURE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "tools",
    "fixtures",
    "tools_test.kicad_sch",
)


def _all_pins(parsed: dict) -> list:
    """Return a flat list of every pin dict across all components."""
    pins = []
    for comp in parsed["components"].values():
        pins.extend(comp.get("pins", []))
    return pins


@pytest.fixture(scope="module")
def parsed():
    """Parse the fixture schematic once for the whole module."""
    return SchematicParser(FIXTURE).parse()


class TestNetlistPinDirection:
    """Tests that every pin dict produced by SchematicParser contains a valid 'direction' field."""

    def test_pin_direction_key_present(self, parsed):
        """Every pin dict must contain the 'direction' key."""
        pins = _all_pins(parsed)
        assert pins, "Fixture produced no pins – check fixture path"
        for pin in pins:
            assert "direction" in pin, f"Pin {pin} is missing the 'direction' key"

    def test_pin_direction_is_valid_string(self, parsed):
        """The 'direction' value must be one of 'right', 'down', 'left', 'up'."""
        valid = {"right", "down", "left", "up"}
        for pin in _all_pins(parsed):
            assert pin["direction"] in valid, (
                f"pin['direction']={pin['direction']!r} is not a valid direction string"
            )

    def test_r1_has_two_pins_with_direction(self, parsed):
        """R1 (R_Small) has exactly 2 pins; both must carry a 'direction' key."""
        r1 = parsed["components"].get("R1")
        assert r1 is not None, "R1 not found in parsed components"
        pins = r1.get("pins", [])
        assert len(pins) == 2, f"Expected 2 pins for R1, got {len(pins)}"
        for pin in pins:
            assert "direction" in pin

    def test_c1_has_two_pins_with_direction(self, parsed):
        """C1 (capacitor) has exactly 2 pins; both must carry a 'direction' key."""
        c1 = parsed["components"].get("C1")
        assert c1 is not None, "C1 not found in parsed components"
        pins = c1.get("pins", [])
        assert len(pins) == 2, f"Expected 2 pins for C1, got {len(pins)}"
        for pin in pins:
            assert "direction" in pin

    def test_r1_pin_directions_exact(self, parsed):
        """R1 (Device:R_Small, rotation=0) must have pin 1 pointing up and pin 2 pointing down.

        Stub angles (lib) 270°/90° → exit (stub+180): 90° → 'up', 270° → 'down'.
        """
        r1 = parsed["components"].get("R1")
        assert r1 is not None, "R1 not found in parsed components"
        by_num = {pin["num"]: pin for pin in r1.get("pins", [])}
        assert by_num["1"]["direction"] == "up", (
            f"R1 pin 1 direction expected 'up', got {by_num['1']['direction']!r}"
        )
        assert by_num["2"]["direction"] == "down", (
            f"R1 pin 2 direction expected 'down', got {by_num['2']['direction']!r}"
        )

    def test_c1_pin_directions_exact(self, parsed):
        """C1 (Device:C, rotation=0) must have pin 1 pointing up and pin 2 pointing down.

        Stub angles (lib) 270°/90° → exit (stub+180): 90° → 'up', 270° → 'down'.
        """
        c1 = parsed["components"].get("C1")
        assert c1 is not None, "C1 not found in parsed components"
        by_num = {pin["num"]: pin for pin in c1.get("pins", [])}
        assert by_num["1"]["direction"] == "up", (
            f"C1 pin 1 direction expected 'up', got {by_num['1']['direction']!r}"
        )
        assert by_num["2"]["direction"] == "down", (
            f"C1 pin 2 direction expected 'down', got {by_num['2']['direction']!r}"
        )

    def test_pins_have_diverse_directions(self, parsed):
        """The fixture must produce at least two distinct pin directions, none equal to 'right'.

        All components in the fixture are placed at rotation=0.  For R_Small and
        Device:C the physical pin directions are 'up' and 'down', so no pin should
        ever produce direction 'right'.
        """
        directions = {pin["direction"] for pin in _all_pins(parsed)}
        assert len(directions) >= 2, (
            f"Expected at least 2 distinct pin directions in fixture, got {directions!r}"
        )
        assert "right" not in directions, (
            f"No pin in the fixture should have direction 'right' (all components at rotation=0 "
            f"with up/down pin directions), but got directions {directions!r}"
        )
