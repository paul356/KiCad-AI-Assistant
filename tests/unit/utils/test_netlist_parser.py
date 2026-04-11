"""
Tests for SchematicParser / netlist_parser pin-angle extraction.
"""

import os

import pytest

from kicad_mcp.utils.netlist_parser import SchematicParser

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


class TestNetlistPinAngle:
    """Tests that every pin dict produced by SchematicParser contains a valid 'angle' field."""

    def test_pin_angle_key_present(self, parsed):
        """Every pin dict must contain the 'angle' key."""
        pins = _all_pins(parsed)
        assert pins, "Fixture produced no pins – check fixture path"
        for pin in pins:
            assert "angle" in pin, f"Pin {pin} is missing the 'angle' key"

    def test_pin_angle_is_numeric_string(self, parsed):
        """The 'angle' value must be a string that converts to float without error."""
        for pin in _all_pins(parsed):
            try:
                float(pin["angle"])
            except (ValueError, TypeError) as exc:
                pytest.fail(f"pin['angle']={pin['angle']!r} is not a valid numeric string: {exc}")

    def test_pin_angle_value_range(self, parsed):
        """Parsed pin angles must satisfy 0.0 <= angle < 360.0."""
        for pin in _all_pins(parsed):
            angle = float(pin["angle"])
            assert 0.0 <= angle < 360.0, (
                f"Pin {pin.get('num')} angle {angle} is outside [0, 360)"
            )

    def test_r1_has_two_pins_with_angle(self, parsed):
        """R1 (R_Small) has exactly 2 pins; both must carry an 'angle' key."""
        r1 = parsed["components"].get("R1")
        assert r1 is not None, "R1 not found in parsed components"
        pins = r1.get("pins", [])
        assert len(pins) == 2, f"Expected 2 pins for R1, got {len(pins)}"
        for pin in pins:
            assert "angle" in pin

    def test_c1_has_two_pins_with_angle(self, parsed):
        """C1 (capacitor) has exactly 2 pins; both must carry an 'angle' key."""
        c1 = parsed["components"].get("C1")
        assert c1 is not None, "C1 not found in parsed components"
        pins = c1.get("pins", [])
        assert len(pins) == 2, f"Expected 2 pins for C1, got {len(pins)}"
        for pin in pins:
            assert "angle" in pin

    def test_r1_pin_angles_exact(self, parsed):
        """R1 (Device:R_Small, rotation=0) must have pin 1 at 270.0° and pin 2 at 90.0°."""
        r1 = parsed["components"].get("R1")
        assert r1 is not None, "R1 not found in parsed components"
        by_num = {pin["num"]: pin for pin in r1.get("pins", [])}
        assert by_num["1"]["angle"] == "270.0", (
            f"R1 pin 1 angle expected '270.0', got {by_num['1']['angle']!r}"
        )
        assert by_num["2"]["angle"] == "90.0", (
            f"R1 pin 2 angle expected '90.0', got {by_num['2']['angle']!r}"
        )

    def test_c1_pin_angles_exact(self, parsed):
        """C1 (Device:C, rotation=0) must have pin 1 at 270.0° and pin 2 at 90.0°."""
        c1 = parsed["components"].get("C1")
        assert c1 is not None, "C1 not found in parsed components"
        by_num = {pin["num"]: pin for pin in c1.get("pins", [])}
        assert by_num["1"]["angle"] == "270.0", (
            f"C1 pin 1 angle expected '270.0', got {by_num['1']['angle']!r}"
        )
        assert by_num["2"]["angle"] == "90.0", (
            f"C1 pin 2 angle expected '90.0', got {by_num['2']['angle']!r}"
        )

    def test_pins_have_diverse_angles(self, parsed):
        """The fixture must produce at least two distinct pin angles, none equal to 0.0.

        All components in the fixture are placed at rotation=0.  For R_Small and
        Device:C the physical pin directions are 90° and 270°, so no pin should
        ever produce angle 0.0.  If the parser incorrectly defaults every angle to
        0.0 this assertion will catch it even if the numeric-string check passes.
        """
        angles = {float(pin["angle"]) for pin in _all_pins(parsed)}
        assert len(angles) >= 2, (
            f"Expected at least 2 distinct pin angles in fixture, got {angles!r}"
        )
        assert 0.0 not in angles, (
            f"No pin in the fixture should have angle 0.0 (all components at rotation=0 "
            f"with 90°/270° pin directions), but got angles {angles!r}"
        )
