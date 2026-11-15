"""
Unit tests for PCB zone tools: list_zones, add_zone, and delete_zone.

Uses tests/unit/tools/fixtures/test_board_with_zones.kicad_pcb, a board
written by KiCad 10's ``SaveBoard()``, which contains:
  - 1e08b401…4fd5  copper_pour on F.Cu  (net GND)
  - c601cc88…0f1a  keepout    on F.Cu   (no net)
  - c4f6bbf4…54c0  copper_pour on B.Cu  (net VCC)

and the footprint R1 (Resistor_SMD:R_0402_1005Metric) at (10, 20) with pad 1
on VCC and pad 2 on GND.
"""

import asyncio
import os
import shutil

import pytest
import sexpdata

from kcaa.tools.pcb_zone_tools import _build_zone_node, _find_net_by_name, _parse_zone
from kcaa.utils.pcb_sexp_utils import load_pcb
from kcaa.utils.sexp_utils import sym_str

FIXTURE_PCB = os.path.join(os.path.dirname(__file__), "fixtures", "test_board_with_zones.kicad_pcb")

UUID_COPPER_GND = "1e08b401-2611-467f-9f96-aea85e324fd5"
UUID_KEEPOUT = "c601cc88-e4d3-43a1-87e8-1daf351a0f1a"
UUID_COPPER_VCC = "c4f6bbf4-adaf-420b-8018-83250cf554c0"

# File-level layer ids as KiCad 10 writes them in the ``(layers ...)`` block:
# PCB_LAYER_ID values, odd for front / even for back (see
# pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.cpp, formatBoardLayers()).
FILE_LAYER_IDS = {
    "F.Cu": 0,
    "B.Cu": 2,
    "F.Mask": 1,
    "B.Mask": 3,
    "F.SilkS": 5,
    "B.SilkS": 7,
    "F.Adhes": 9,
    "B.Adhes": 11,
    "F.Paste": 13,
    "B.Paste": 15,
    "Dwgs.User": 17,
    "Cmts.User": 19,
    "Eco1.User": 21,
    "Eco2.User": 23,
    "Edge.Cuts": 25,
    "Margin": 27,
    "B.CrtYd": 29,
    "F.CrtYd": 31,
    "B.Fab": 33,
    "F.Fab": 35,
    "User.1": 39,
    "User.2": 41,
    "User.3": 43,
    "User.4": 45,
}

# Keepout rules KiCad 10 writes; it has no ``text`` rule.
KEEPOUT_KEYWORDS = {"tracks", "vias", "pads", "copperpour", "footprints"}


# ---------------------------------------------------------------------------
# Helpers
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
    from kcaa.tools.pcb_zone_tools import register_pcb_zone_tools

    mock = _MockMCP()
    register_pcb_zone_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tmp_pcb(tmp_path):
    """Copy the fixture PCB to a temp directory so writes don't affect it."""
    dst = tmp_path / "test_board_with_zones.kicad_pcb"
    shutil.copy2(FIXTURE_PCB, dst)
    return str(dst)


@pytest.fixture()
def tmp_pcb_legacy_net_table(tmp_path):
    """Fixture rewritten with a numbered net table in the KiCad 9 shape.

    Position and shape match KiCad 9; KiCad 10 still loads the table. It is not
    a byte-exact KiCad 9 save of this board.
    """
    dst = tmp_path / "test_board_with_zones_legacy_net_table.kicad_pcb"
    shutil.copy2(FIXTURE_PCB, dst)
    text = dst.read_text(encoding="utf-8")
    table = '\t(net 0 "")\n\t(net 1 "GND")\n\t(net 2 "VCC")\n'
    # KiCad 9 put the net table between (setup ...) and the footprints. A missed
    # anchor fails the test below (net stays None) instead of passing.
    text = text.replace("\t(footprint ", table + "\t(footprint ", 1)
    dst.write_text(text, encoding="utf-8")
    return str(dst)


def _find_zone_node(data, zone_uuid):
    """Return the top-level ``(zone ...)`` node with *zone_uuid*."""
    for item in data:
        if not (isinstance(item, list) and item and item[0] == sexpdata.Symbol("zone")):
            continue
        for sub in item:
            if isinstance(sub, list) and len(sub) >= 2 and sub[0] == sexpdata.Symbol("uuid"):
                if sub[1] == zone_uuid:
                    return item
    raise AssertionError(f"zone {zone_uuid} not found in parsed tree")


# ---------------------------------------------------------------------------
# list_zones
# ---------------------------------------------------------------------------


class TestListZones:
    def setup_method(self):
        self.tools = _get_tools()
        self.list_zones = self.tools["list_zones"]

    def test_count(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        assert result["count"] == 3

    def test_copper_pour_count(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        assert result["copper_pour_count"] == 2

    def test_keepout_count(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        assert result["keepout_count"] == 1

    def test_copper_pour_fields(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        zone = next(z for z in result["zones"] if z["uuid"] == UUID_COPPER_GND)
        assert zone["zone_type"] == "copper_pour"
        assert zone["net_name"] == "GND"
        assert zone["layer"] == "F.Cu"
        assert zone["fill"] is True
        assert len(zone["polygon_pts"]) == 4

    def test_hatch_style_reads_back_as_plain_string(self):
        """Reproduces #292: the bare token came back as a Symbol, not a str."""
        result = _run(self.list_zones(FIXTURE_PCB, None))
        zone = next(z for z in result["zones"] if z["uuid"] == UUID_COPPER_GND)
        assert zone["hatch_style"] == "edge"

    def test_keepout_fields(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        zone = next(z for z in result["zones"] if z["uuid"] == UUID_KEEPOUT)
        assert zone["zone_type"] == "keepout"
        assert zone["net"] == 0
        assert zone["net_name"] == ""
        assert zone["layer"] == "F.Cu"
        assert zone["keepout_rules"] == {
            "tracks": "not_allowed",
            "vias": "not_allowed",
            "pads": "allowed",
            "copperpour": "not_allowed",
            "footprints": "allowed",
        }

    def test_polygon_pts_coordinates(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        zone = next(z for z in result["zones"] if z["uuid"] == UUID_KEEPOUT)
        pts = zone["polygon_pts"]
        assert len(pts) == 4
        assert pts[0] == {"x": 30.0, "y": 30.0}
        assert pts[2] == {"x": 50.0, "y": 50.0}

    def test_b_cu_copper_pour(self):
        result = _run(self.list_zones(FIXTURE_PCB, None))
        zone = next(z for z in result["zones"] if z["uuid"] == UUID_COPPER_VCC)
        assert zone["zone_type"] == "copper_pour"
        assert zone["net_name"] == "VCC"
        assert zone["layer"] == "B.Cu"


# ---------------------------------------------------------------------------
# add_zone
# ---------------------------------------------------------------------------


class TestAddZone:
    def setup_method(self):
        self.tools = _get_tools()
        self.add_zone = self.tools["add_zone"]
        self.list_zones = self.tools["list_zones"]

    def test_add_copper_pour_zone(self, tmp_pcb):
        points = [
            {"x": 70.0, "y": 10.0},
            {"x": 90.0, "y": 10.0},
            {"x": 90.0, "y": 30.0},
            {"x": 70.0, "y": 30.0},
        ]
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=points,
                net_name="GND",
                zone_type="copper_pour",
                ctx=None,
            )
        )
        assert result["added"] is True
        assert result["zone_type"] == "copper_pour"
        assert result["net_name"] == "GND"
        assert result["layer"] == "F.Cu"
        assert result["fill"] is True
        assert result["polygon_pts"] == points

        after = _run(self.list_zones(tmp_pcb, None))
        assert after["count"] == 4
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["zone_type"] == "copper_pour"
        assert added["net_name"] == "GND"
        assert added["layer"] == "F.Cu"
        assert added["polygon_pts"] == points

    def test_fill_false_round_trips(self, tmp_pcb):
        """Reproduces #292: (fill no) was read back as fill=True."""
        before = _run(self.list_zones(tmp_pcb, None))["count"]
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                    {"x": 70.0, "y": 30.0},
                ],
                net_name="GND",
                fill=False,
                ctx=None,
            )
        )
        after = _run(self.list_zones(tmp_pcb, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert after["count"] == before + 1
        assert added["fill"] is False

    def test_add_keepout_zone(self, tmp_pcb):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="B.Cu",
                zone_type="keepout",
                polygon_pts=[
                    {"x": 70.0, "y": 35.0},
                    {"x": 90.0, "y": 35.0},
                    {"x": 90.0, "y": 55.0},
                    {"x": 70.0, "y": 55.0},
                ],
                keepout_tracks="allowed",
                ctx=None,
            )
        )
        assert result["added"] is True
        assert result["zone_type"] == "keepout"
        assert result["net"] == 0
        assert result["net_name"] == ""

        node = _find_zone_node(load_pcb(tmp_pcb), result["zone_uuid"])
        keepout = next(
            sub
            for sub in node[1:]
            if isinstance(sub, list) and sub and sym_str(sub[0]) == "keepout"
        )
        # KiCad 10's rule-area block: five clauses, in this order, `pads` always
        # allowed, and no `text` clause (10.x rejects it on load).
        assert sexpdata.dumps(keepout) == (
            "(keepout (tracks allowed) (vias not_allowed) (pads allowed) "
            "(copperpour not_allowed) (footprints allowed))"
        )
        assert [sym_str(rule[0]) for rule in keepout[1:]] == [
            "tracks",
            "vias",
            "pads",
            "copperpour",
            "footprints",
        ]
        assert "text" not in [sym_str(rule[0]) for rule in keepout[1:]]

        # A 10.x rule area carries no net node at all.
        zone_keys = [sym_str(sub[0]) for sub in node[1:] if isinstance(sub, list) and sub]
        assert "net" not in zone_keys
        assert "net_name" not in zone_keys

        # ... and the read-back is unchanged: no net node still means net 0.
        after = _run(self.list_zones(tmp_pcb, None))
        assert after["keepout_count"] == 2
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["zone_type"] == "keepout"
        assert added["net"] == 0
        assert added["net_name"] == ""
        assert added["layer"] == "B.Cu"
        assert added["keepout_rules"] == {
            "tracks": "allowed",
            "vias": "not_allowed",
            "pads": "allowed",
            "copperpour": "not_allowed",
            "footprints": "allowed",
        }

    def test_keepout_text_parameter_is_gone(self, tmp_pcb):
        """The concept does not exist in the KiCad 10 format (#308)."""
        with pytest.raises(TypeError, match="keepout_text"):
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="B.Cu",
                zone_type="keepout",
                polygon_pts=[
                    {"x": 70.0, "y": 35.0},
                    {"x": 90.0, "y": 35.0},
                    {"x": 90.0, "y": 55.0},
                ],
                keepout_text="allowed",
                ctx=None,
            )

    def test_add_copper_zone_without_top_level_net_table(self, tmp_pcb):
        """The fixture itself is a KiCad 10 board with no net table: nets live in pads."""
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                    {"x": 70.0, "y": 30.0},
                ],
                net_name="GND",
                ctx=None,
            )
        )
        assert result["added"] is True
        assert result["net"] is None
        assert result["net_name"] == "GND"

        after = _run(self.list_zones(tmp_pcb, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["net"] is None
        assert added["net_name"] == "GND"

    def test_add_copper_zone_with_legacy_net_table(self, tmp_pcb_legacy_net_table):
        """A numbered net table in the KiCad 9 shape supplies the netcode.

        The board format itself is still KiCad 10, so the zone is written in the
        v10 name-only form even though the netcode was resolved.
        """
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb_legacy_net_table,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                    {"x": 70.0, "y": 30.0},
                ],
                net_name="GND",
                ctx=None,
            )
        )
        assert result["added"] is True
        assert result["net"] == 1
        assert result["net_name"] == "GND"

        after = _run(self.list_zones(tmp_pcb_legacy_net_table, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["net"] is None
        assert added["net_name"] == "GND"

    def test_add_zone_creates_backup(self, tmp_pcb):
        _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                    {"x": 70.0, "y": 30.0},
                ],
                net_name="GND",
                ctx=None,
            )
        )
        assert os.path.exists(tmp_pcb + ".bak")

    def test_copper_zone_requires_net_name(self, tmp_pcb):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                ],
                net_name=None,
                ctx=None,
            )
        )
        assert "error" in result

    def test_unknown_net_rejected(self, tmp_pcb):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                ],
                net_name="NO_SUCH_NET",
                ctx=None,
            )
        )
        assert "error" in result

    def test_invalid_layer_rejected(self, tmp_pcb):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="Inner42.Cu",
                polygon_pts=[
                    {"x": 70.0, "y": 10.0},
                    {"x": 90.0, "y": 10.0},
                    {"x": 90.0, "y": 30.0},
                ],
                net_name="GND",
                ctx=None,
            )
        )
        assert "error" in result

    def test_too_few_points_rejected(self, tmp_pcb):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb,
                layer="F.Cu",
                polygon_pts=[{"x": 70.0, "y": 10.0}, {"x": 90.0, "y": 10.0}],
                net_name="GND",
                ctx=None,
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# add_zone: the zone net reference form
# ---------------------------------------------------------------------------

_ADD_POINTS = [
    {"x": 70.0, "y": 10.0},
    {"x": 90.0, "y": 10.0},
    {"x": 90.0, "y": 30.0},
    {"x": 70.0, "y": 30.0},
]


class TestZoneNetForm:
    def setup_method(self):
        self.tools = _get_tools()

    def _add_zone(self, pcb_path, net_name="GND"):
        result = _run(
            self.tools["add_zone"](
                pcb_path=pcb_path,
                layer="F.Cu",
                polygon_pts=_ADD_POINTS,
                net_name=net_name,
                ctx=None,
            )
        )
        assert result["added"] is True, result
        return result

    def test_v10_board_writes_name_only_net(self, tmp_pcb):
        result = self._add_zone(tmp_pcb)
        node = _find_zone_node(load_pcb(tmp_pcb), result["zone_uuid"])
        dumped = sexpdata.dumps(node)
        assert '(net "GND")' in dumped
        assert "net_name" not in dumped

        after = _run(self.tools["list_zones"](tmp_pcb, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["net"] is None
        assert added["net_name"] == "GND"

    def test_v10_keepout_writes_no_net_node(self, tmp_pcb):
        """A KiCad 10 rule area carries no (net ...) and no net_name node."""
        result = _run(
            self.tools["add_zone"](
                pcb_path=tmp_pcb,
                layer="B.Cu",
                zone_type="keepout",
                polygon_pts=_ADD_POINTS,
                ctx=None,
            )
        )
        assert result["added"] is True, result
        node = _find_zone_node(load_pcb(tmp_pcb), result["zone_uuid"])
        dumped = sexpdata.dumps(node)
        assert "(net " not in dumped
        assert "net_name" not in dumped


# ---------------------------------------------------------------------------
# delete_zone
# ---------------------------------------------------------------------------


class TestDeleteZone:
    def setup_method(self):
        self.tools = _get_tools()
        self.list_zones = self.tools["list_zones"]
        self.delete_zone = self.tools["delete_zone"]

    def test_delete_copper_pour(self, tmp_pcb):
        result = _run(self.delete_zone(tmp_pcb, UUID_COPPER_GND, None))
        assert result["deleted"] is True
        assert result["zone_uuid"] == UUID_COPPER_GND
        assert "backup_path" in result

        after = _run(self.list_zones(tmp_pcb, None))
        assert after["count"] == 2
        assert not any(z["uuid"] == UUID_COPPER_GND for z in after["zones"])

    def test_delete_keepout(self, tmp_pcb):
        result = _run(self.delete_zone(tmp_pcb, UUID_KEEPOUT, None))
        assert result["deleted"] is True

        after = _run(self.list_zones(tmp_pcb, None))
        assert after["keepout_count"] == 0
        assert after["copper_pour_count"] == 2

    def test_delete_nonexistent_uuid(self, tmp_pcb):
        result = _run(self.delete_zone(tmp_pcb, "does-not-exist", None))
        assert result["deleted"] is False
        assert "error" in result

    def test_backup_created(self, tmp_pcb):
        _run(self.delete_zone(tmp_pcb, UUID_COPPER_VCC, None))
        assert os.path.exists(tmp_pcb + ".bak")

    def test_delete_all_zones_sequentially(self, tmp_pcb):
        for uuid in (UUID_COPPER_GND, UUID_KEEPOUT, UUID_COPPER_VCC):
            _run(self.delete_zone(tmp_pcb, uuid, None))
        after = _run(self.list_zones(tmp_pcb, None))
        assert after["count"] == 0


class TestBareSymbolAtoms:
    def test_parse_zone_net_name_is_plain_str(self):
        """Reproduces #295: a bare net-name atom must be returned as a plain str."""
        node = [
            sexpdata.Symbol("zone"),
            [sexpdata.Symbol("net_name"), sexpdata.Symbol("GND")],
            [sexpdata.Symbol("layer"), sexpdata.Symbol("F.Cu")],
        ]
        result = _parse_zone(node)
        assert result["net_name"] == "GND"
        assert type(result["net_name"]) is str

    def test_find_net_and_build_zone_write_plain_string(self):
        """Reproduces #295: a bare net-table name must be written back quoted."""
        data = [[sexpdata.Symbol("net"), 1, sexpdata.Symbol("GND")]]
        found = _find_net_by_name(data, "GND")
        assert found is not None
        net_id, name = found
        assert net_id == 1
        assert name == "GND"
        assert type(name) is str

        node = _build_zone_node(
            zone_uuid="zone-test",
            zone_type="copper_pour",
            layer="F.Cu",
            polygon_pts=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
            net_name=name,
            hatch_style="edge",
            hatch_pitch=0.508,
            clearance=0.5,
            min_thickness=0.25,
            fill=True,
            thermal_gap=0.5,
            thermal_bridge_width=0.5,
            keepout_tracks="not_allowed",
            keepout_vias="not_allowed",
            keepout_copperpour="not_allowed",
            keepout_footprints="allowed",
        )
        # A copper pour writes its net by name, and the name must stay a quoted
        # string (a bare symbol would serialize as ``GND``).
        assert node[1] == [sexpdata.Symbol("net"), "GND"]
        dumped = sexpdata.dumps(node)
        assert '(net "GND")' in dumped
        assert "(net GND)" not in dumped
        assert "net_name" not in dumped

    @pytest.mark.parametrize(
        "net_node",
        [
            [sexpdata.Symbol("net"), 1, sexpdata.Symbol("GND")],
            [sexpdata.Symbol("net"), sexpdata.Symbol("GND")],
        ],
        ids=["with-id", "id-less"],
    )
    def test_find_net_by_name_pad_fallback_is_plain_str(self, net_node):
        """Reproduces #295: KiCad 10 boards may omit the net table.

        The net then lives in the pad and must still come back as a plain str.
        """
        tree = [
            [
                sexpdata.Symbol("footprint"),
                "Lib:R1",
                [
                    sexpdata.Symbol("pad"),
                    "1",
                    sexpdata.Symbol("smd"),
                    sexpdata.Symbol("rect"),
                    net_node,
                ],
            ]
        ]
        found = _find_net_by_name(tree, "GND")
        assert found is not None
        net_id, name = found
        assert net_id is None
        assert name == "GND"
        assert type(name) is str


# ---------------------------------------------------------------------------
# Fixture format guard
# ---------------------------------------------------------------------------


class TestFixtureIsKiCad10Shape:
    """Guards the fixture's byte shape without needing KiCad installed (#306).

    The fixture used to claim ``(version 20260206)`` while carrying layer names
    KiCad never writes (``B.Courtyard``/``F.Courtyard``), a name-only top-level
    net table and a ``(text ...)`` keepout rule, so KiCad 10 refused to load it.
    Its layer *numbers* were also wrong; KiCad tolerates those, renumbering from
    each layer's name on save, so the guard pins them but they did not block the
    load.
    """

    def _tree(self):
        with open(FIXTURE_PCB, encoding="utf-8") as handle:
            return sexpdata.loads(handle.read())

    def test_top_level_net_entries_carry_an_integer_code(self):
        """A top-level net entry must never come back in name-only form.

        A KiCad 10 board has no top-level net table, so this loop runs zero times
        on the current fixture: the test does not assert that the fixture lacks a
        table, only that any entry present carries its netcode.
        """
        for node in self._tree():
            if not (isinstance(node, list) and node and sym_str(node[0]) == "net"):
                continue
            assert isinstance(node[1], int), (
                f"top-level net entry has no netcode: {node!r}. KiCad parses the net "
                "table with parseInt('net number'); only net references may be name-only."
            )
            assert len(node) == 3, f"unexpected net entry shape: {node!r}"

    def test_layer_numbers_are_file_level_ids(self):
        for node in self._tree():
            if not (isinstance(node, list) and node and sym_str(node[0]) == "layers"):
                continue
            entries = node[1:]
            assert entries, "the board declares no layers"
            for entry in entries:
                number, name = entry[0], sym_str(entry[1])
                assert name in FILE_LAYER_IDS, f"unknown layer name {name!r}"
                assert number == FILE_LAYER_IDS[name], (
                    f"layer {name!r} is declared as {number!r}, "
                    f"KiCad 10 writes {FILE_LAYER_IDS[name]}"
                )

    def test_keepout_rules_use_kicad10_keywords(self):
        keepouts = 0
        for node in self._tree():
            if not (isinstance(node, list) and node and sym_str(node[0]) == "zone"):
                continue
            for child in node[1:]:
                if not (isinstance(child, list) and child and sym_str(child[0]) == "keepout"):
                    continue
                keepouts += 1
                for rule in child[1:]:
                    keyword = sym_str(rule[0])
                    assert keyword in KEEPOUT_KEYWORDS, (
                        f"keepout rule {keyword!r} is not one KiCad 10 writes "
                        f"({sorted(KEEPOUT_KEYWORDS)})"
                    )
        assert keepouts == 1, "the fixture must keep exactly one keepout zone"
