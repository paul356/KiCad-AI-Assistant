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
POWER_TOUCH_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "power_touch.kicad_sch")
MID_WIRE_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mid_wire.kicad_sch")


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


class TestPowerSymbolConnectivity:
    """Power symbols (ref #PWR?) declare nets by name at their pin tips.

    KiCad power symbols carry the net name in their Value property and a
    power_in/power_out pin at the library origin; any component pin whose
    tip sits at that coordinate joins the named net even with no wire.
    The fixture reuses the pin_touch geometry (R1 pin 2 touching R1 pin 1
    of R2 at (100, 102.54)) and adds two power:PGND symbols: one on the
    touch point and one on what used to be the isolated R1 pin 1.
    """

    @pytest.fixture(scope="class")
    def power_parsed(self):
        return SchematicParser(POWER_TOUCH_FIXTURE).parse()

    @staticmethod
    def _net_of(parsed):
        return {
            (p["component"], str(p["pin"])): net
            for net, pins in parsed["nets"].items()
            for p in pins
        }

    def test_power_symbols_are_collected(self, power_parsed):
        """Both #PWR? placements survive as separate power symbols, each
        carrying its Value ("PGND") and pin-tip position."""
        ps = power_parsed["power_symbols"]
        assert [p["type"] for p in ps] == ["PGND", "PGND"]
        positions = {(p["position"]["x"], p["position"]["y"]) for p in ps}
        assert positions == {(100.0, 102.54), (100.0, 97.46)}

    def test_component_pins_join_the_named_power_net(self, power_parsed):
        """The touching pair AND the previously isolated R1 pin 1 all land
        on the PGND net — the power symbol's pin tip is a connection point
        at the same world coordinate."""
        net_of = self._net_of(power_parsed)
        assert net_of[("R1", "1")] == "PGND"
        assert net_of[("R1", "2")] == "PGND"
        assert net_of[("R2", "1")] == "PGND"

    def test_isolated_pin_no_longer_reported_as_pin_net(self, power_parsed):
        """The old single-pin auto net Net-(R1-Pin1) must be gone, and the
        PGND net carries all three joining pins."""
        assert "Net-(R1-Pin1)" not in power_parsed["nets"]
        assert len(power_parsed["nets"]["PGND"]) == 3

    def test_unconnected_pin_stays_separate(self, power_parsed):
        """R2 pin 2 has no power symbol and no wire: it remains its own
        single-pin net."""
        assert power_parsed["nets"].get("Net-(R2-Pin2)") == [{"component": "R2", "pin": "2"}]


class TestMidWireAnchorConnectivity:
    """Point items anchored mid-wire connect to that wire's net.

    KiCad connects a label (or pin tip) whose anchor lands ANYWHERE on a
    wire segment — junction dots exist only for wire-to-wire crossings.
    Fixture geometry:

    - wire (100, 102.54) -> (100, 112.46) joining R1 pin 2 and R2 pin 1,
      with local label "MID" anchored at (100, 107.5) — its middle;
    - wire (150, 97.46) -> (150, 105.0) with R3 pin 2 at (150, 102.54)
      and R4 pin 1 at (150, 101.0) — both pin tips on the wire body.
    """

    @pytest.fixture(scope="class")
    def mid_parsed(self):
        return SchematicParser(MID_WIRE_FIXTURE).parse()

    @staticmethod
    def _net_of(parsed):
        return {
            (p["component"], str(p["pin"])): net
            for net, pins in parsed["nets"].items()
            for p in pins
        }

    def test_fixture_label_is_mid_wire(self, mid_parsed):
        """Sanity: the MID label sits strictly between the wire's ends, so
        a coordinate-coincidence-only matcher could never catch it."""
        label = next(l for l in mid_parsed["labels"] if l["text"] == "MID")
        y = label["position"]["y"]
        assert 102.54 < y < 112.46

    def test_mid_wire_label_names_the_whole_wire_net(self, mid_parsed):
        """Both pins of the wire tree land on the label's net, and the
        auto-generated name no longer claims them."""
        net_of = self._net_of(mid_parsed)
        assert net_of[("R1", "2")] == "MID"
        assert net_of[("R2", "1")] == "MID"
        mid_pins = sorted((p["component"], p["pin"]) for p in mid_parsed["nets"]["MID"])
        assert mid_pins == [("R1", "2"), ("R2", "1")]
        assert not any(
            n.startswith("Net")
            and len(ps) > 1
            and ("R1", "2") in [(p["component"], p["pin"]) for p in ps]
            for n, ps in mid_parsed["nets"].items()
        )

    def test_pin_tips_on_wire_body_connect(self, mid_parsed):
        """R3 pin 2 and R4 pin 1 tips sit on the middle of the vertical
        wire; both must join that wire net."""
        net_of = self._net_of(mid_parsed)
        assert net_of[("R3", "1")] == net_of[("R3", "2")] == net_of[("R4", "1")]

    def test_pin_off_wire_stays_separate(self, mid_parsed):
        """R4 pin 2 sits below the wire end and connects to nothing."""
        assert mid_parsed["nets"].get("Net-(R4-Pin2)") == [{"component": "R4", "pin": "2"}]


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


# ---------------------------------------------------------------------------
# Pre-flight integrity check in extract_netlist
# ---------------------------------------------------------------------------


class TestExtractNetlistIntegrityCheck:
    """``extract_netlist`` must surface file corruption loudly, not silently.

    Historically ``skip``'s parser accepted truncated files by ignoring
    trailing content, which let downstream tools "succeed" against a
    half-corrupt schematic. The pre-flight check in
    :func:`kcaa.utils.netlist_parser.extract_netlist` now raises
    :class:`SchematicCorruptionError` before handing the file to skip,
    so the LLM sees a clear error instead of bogus analysis.
    """

    def test_truncated_file_raises_corruption_error(self, tmp_path):
        from kcaa.utils.netlist_parser import extract_netlist
        from kcaa.utils.schematic_sexp_utils import SchematicCorruptionError

        bad = tmp_path / "truncated.kicad_sch"
        # Same truncation shape that delete_wire_from_schematic historically
        # produced: file ends mid-expression with trailing close brackets missing.
        bad.write_bytes(b'(kicad_sch (version 20240101) (paper "A4"')

        with pytest.raises(SchematicCorruptionError, match="unclosed parens"):
            extract_netlist(str(bad))

    def test_missing_file_raises_filenotfound(self, tmp_path):
        from kcaa.utils.netlist_parser import extract_netlist

        missing = tmp_path / "does_not_exist.kicad_sch"
        with pytest.raises(FileNotFoundError):
            extract_netlist(str(missing))

    def test_empty_file_raises_corruption_error(self, tmp_path):
        from kcaa.utils.netlist_parser import extract_netlist
        from kcaa.utils.schematic_sexp_utils import SchematicCorruptionError

        empty = tmp_path / "empty.kicad_sch"
        empty.write_bytes(b"")

        with pytest.raises(SchematicCorruptionError, match="empty"):
            extract_netlist(str(empty))

    def test_valid_file_succeeds(self):
        from kcaa.utils.netlist_parser import extract_netlist

        result = extract_netlist(FIXTURE)
        assert "error" not in result
        assert result["component_count"] > 0
