"""
Unit tests for PCB zone tools: list_zones, add_zone, and delete_zone.

Uses tests/unit/tools/fixtures/test_board_with_zones.kicad_pcb which
contains:
  - zone0001…001  copper_pour on F.Cu  (net GND)
  - zone0002…002  keepout    on F.Cu   (no net)
  - zone0003…003  copper_pour on B.Cu  (net VCC)
"""

import asyncio
import os
import re
import shutil

import pytest
import sexpdata

from kcaa.tools.pcb_zone_tools import _build_zone_node, _find_net_by_name, _parse_zone
from kcaa.utils.pcb_sexp_utils import load_pcb

FIXTURE_PCB = os.path.join(os.path.dirname(__file__), "fixtures", "test_board_with_zones.kicad_pcb")
PRE_V10_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "test_board_pre_v10.kicad_pcb"
)

UUID_COPPER_GND = "zone0001-0000-0000-0000-000000000001"
UUID_KEEPOUT = "zone0002-0000-0000-0000-000000000002"
UUID_COPPER_VCC = "zone0003-0000-0000-0000-000000000003"


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
def tmp_pcb_no_net_table(tmp_path):
    """Fixture rewritten to KiCad's name-only net format without top-level net table."""
    dst = tmp_path / "test_board_with_zones_name_only.kicad_pcb"
    shutil.copy2(FIXTURE_PCB, dst)
    text = dst.read_text(encoding="utf-8")
    text = re.sub(r'^\t\(net\s+\d+\s+"[^"]*"\)\n', "", text, flags=re.MULTILINE)
    text = re.sub(r'\(net\s+\d+\s+"([^"]*)"\)', r'(net "\1")', text)
    dst.write_text(text, encoding="utf-8")
    return str(dst)


@pytest.fixture()
def tmp_pre_v10_pcb(tmp_path):
    """Copy the KiCad 9 (version 20241229) fixture so writes don't affect it."""
    dst = tmp_path / "test_board_pre_v10.kicad_pcb"
    shutil.copy2(PRE_V10_FIXTURE, dst)
    return str(dst)


@pytest.fixture()
def tmp_pre_v10_pcb_pad_net(tmp_path):
    """Pre-10 board whose net table lacks VCC; only a pad names that net."""
    dst = tmp_path / "test_board_pre_v10_pad_net.kicad_pcb"
    shutil.copy2(PRE_V10_FIXTURE, dst)
    text = dst.read_text(encoding="utf-8")
    footprint = (
        '\t(footprint "Test:Point"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "aaaaaaaa-0000-0000-0000-0000000000aa")\n'
        "\t\t(at 10.0 10.0)\n"
        '\t\t(pad "1" smd rect\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 0.5 0.5)\n"
        '\t\t\t(layers "F.Cu")\n'
        '\t\t\t(net 2 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    text = text.replace('\t(net 1 "GND")\n)', '\t(net 1 "GND")\n' + footprint + ")")
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
        assert zone["layer"] == "F.Cu"
        assert zone["keepout_rules"]["tracks"] == "not_allowed"
        assert zone["keepout_rules"]["vias"] == "not_allowed"
        assert zone["keepout_rules"]["copperpour"] == "not_allowed"
        assert zone["keepout_rules"]["footprints"] == "allowed"

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

        after = _run(self.list_zones(tmp_pcb, None))
        assert after["keepout_count"] == 2
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["zone_type"] == "keepout"
        assert added["layer"] == "B.Cu"
        assert added["keepout_rules"]["tracks"] == "allowed"

    def test_add_copper_zone_without_top_level_net_table(self, tmp_pcb_no_net_table):
        result = _run(
            self.add_zone(
                pcb_path=tmp_pcb_no_net_table,
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

        after = _run(self.list_zones(tmp_pcb_no_net_table, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
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
# add_zone: net reference form follows the board format version
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

    def test_pre_v10_board_writes_netcode_and_name(self, tmp_pre_v10_pcb):
        result = self._add_zone(tmp_pre_v10_pcb)
        node = _find_zone_node(load_pcb(tmp_pre_v10_pcb), result["zone_uuid"])
        dumped = sexpdata.dumps(node)
        assert "(net 1)" in dumped
        assert '(net_name "GND")' in dumped
        assert '(net "GND")' not in dumped

        after = _run(self.tools["list_zones"](tmp_pre_v10_pcb, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["net"] == 1
        assert added["net_name"] == "GND"

    def test_pre_v10_board_without_a_net_table_entry_still_writes_net_name(
        self, tmp_pre_v10_pcb_pad_net
    ):
        """Name resolved from a pad, so no netcode: pre-10 keeps the legacy pair."""
        result = self._add_zone(tmp_pre_v10_pcb_pad_net, net_name="VCC")
        assert result["net"] is None
        node = _find_zone_node(load_pcb(tmp_pre_v10_pcb_pad_net), result["zone_uuid"])
        dumped = sexpdata.dumps(node)
        assert '(net "VCC")' in dumped
        assert '(net_name "VCC")' in dumped

        after = _run(self.tools["list_zones"](tmp_pre_v10_pcb_pad_net, None))
        added = next(z for z in after["zones"] if z["uuid"] == result["zone_uuid"])
        assert added["net"] is None
        assert added["net_name"] == "VCC"

    def test_v10_keepout_keeps_legacy_net_pair(self, tmp_pcb):
        """Rule areas are untouched by #303 — they still carry (net 0)."""
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
        assert "(net 0)" in dumped
        assert '(net_name "")' in dumped


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
            net_id=net_id,
            net_name=name,
            board_version=20241229,
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
            keepout_text="allowed",
        )
        dumped = sexpdata.dumps(node)
        assert '(net_name "GND")' in dumped
        assert "(net_name GND)" not in dumped

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
