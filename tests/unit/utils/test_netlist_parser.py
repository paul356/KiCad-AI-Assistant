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
        for unit in (comp.get("units") or {}).values():
            pins.extend(unit.get("pins", []))
    return pins


def _pins_of(comp: dict) -> list:
    """Pins of a component's unit 1 (fixture components are single-unit)."""
    return comp["units"]["1"].get("pins", [])


PIN_TOUCH_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pin_touch.kicad_sch")


@pytest.fixture(scope="module")
def parsed():
    """Parse the fixture schematic once for the whole module."""
    return SchematicParser(FIXTURE).parse()


class TestPinTouchConnectivity:
    """Pin tips touching directly (no wire) form a connection.

    KiCad's connection model places a connection point at every pin tip;
    two pins whose world positions coincide share that point and are on the
    same net without any wire. The fixture places R1 pin 2 at (100, 102.54)
    exactly on top of R2 pin 1, with no wire between them.
    """

    @pytest.fixture(scope="class")
    def touch_parsed(self):
        return SchematicParser(PIN_TOUCH_FIXTURE).parse()

    def test_touching_pins_share_a_world_position(self, touch_parsed):
        """Fixture sanity: the two pins really do occupy the same coordinate,
        otherwise the connectivity assertion below would be vacuous."""
        by_ref = {}
        for ref in ("R1", "R2"):
            for unit in touch_parsed["components"][ref]["units"].values():
                for pin in unit["pins"]:
                    by_ref[(ref, str(pin["num"]))] = (float(pin["x"]), float(pin["y"]))
        assert by_ref[("R1", "2")] == by_ref[("R2", "1")] == (100.0, 102.54)

    def test_touching_pins_land_in_the_same_net(self, touch_parsed):
        """R1 pin 2 and R2 pin 1 touch end-to-end with no wire; they must
        share one net (KiCad connection-point semantics), and that net must
        contain exactly those two pins."""
        net_of = {
            (p["component"], str(p["pin"])): net
            for net, pins in touch_parsed["nets"].items()
            for p in pins
        }
        assert net_of[("R1", "2")] == net_of[("R2", "1")]
        touch_net = net_of[("R1", "2")]
        assert touch_net not in ("Net-(R1-Pin1)", "Net-(R2-Pin2)")
        assert len(touch_parsed["nets"][touch_net]) == 2

    def test_non_touching_pins_stay_separate(self, touch_parsed):
        """R1 pin 1 and R2 pin 2 have no wire and no shared position, so they
        remain distinct single-pin nets."""
        net_of = {
            (p["component"], str(p["pin"])): net
            for net, pins in touch_parsed["nets"].items()
            for p in pins
        }
        assert net_of[("R1", "1")] != net_of[("R2", "2")]
        assert len(touch_parsed["nets"][net_of[("R1", "1")]]) == 1
        assert len(touch_parsed["nets"][net_of[("R2", "2")]]) == 1


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
        pins = _pins_of(r1)
        assert len(pins) == 2, f"Expected 2 pins for R1, got {len(pins)}"
        for pin in pins:
            assert "direction" in pin

    def test_c1_has_two_pins_with_direction(self, parsed):
        """C1 (capacitor) has exactly 2 pins; both must carry a 'direction' key."""
        c1 = parsed["components"].get("C1")
        assert c1 is not None, "C1 not found in parsed components"
        pins = _pins_of(c1)
        assert len(pins) == 2, f"Expected 2 pins for C1, got {len(pins)}"
        for pin in pins:
            assert "direction" in pin

    def test_r1_pin_directions_exact(self, parsed):
        """R1 (Device:R_Small, rotation=0) must have pin 1 pointing up and pin 2 pointing down.

        Stub angles (lib) 270°/90° → exit (stub+180): 90° → 'up', 270° → 'down'.
        """
        r1 = parsed["components"].get("R1")
        assert r1 is not None, "R1 not found in parsed components"
        by_num = {pin["num"]: pin for pin in _pins_of(r1)}
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
        by_num = {pin["num"]: pin for pin in _pins_of(c1)}
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
