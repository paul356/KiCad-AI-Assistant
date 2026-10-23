"""
Unit tests for kcaa/utils/pcb_sexp_utils.py and
kcaa/utils/pcb_footprint_utils.py.

These modules are pure helpers (no FastMCP dependency) so they can be
imported and tested without the mcp.tool() decoration pattern.
"""

import os
import shutil

import pytest
import sexpdata

from kcaa.utils.pcb_footprint_utils import (
    LAYER_FLIP_MAP,
    _fp_property_is_hidden,
    find_footprint,
    flip_fp_layers,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    set_fp_at,
    set_fp_property,
    upsert_fp_property,
)
from kcaa.utils.pcb_sexp_utils import _serialize, load_pcb, save_pcb

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_board.kicad_pcb")


# ---------------------------------------------------------------------------
# pcb_sexp_utils
# ---------------------------------------------------------------------------


class TestLoadPcb:
    def test_loads_fixture(self):
        data = load_pcb(BOARD_FIXTURE)
        assert isinstance(data, list)
        assert str(data[0]) == "kicad_pcb"

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_pcb("/nonexistent/path.kicad_pcb")

    def test_raises_on_invalid_sexp(self, tmp_path):
        bad = tmp_path / "bad.kicad_pcb"
        bad.write_text("((unclosed")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_pcb(str(bad))


class TestSavePcb:
    def test_creates_backup_and_writes(self, tmp_path):
        src = tmp_path / "board.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, src)
        data = load_pcb(str(src))
        bak = save_pcb(str(src), data)
        assert bak == str(src) + ".bak"
        assert os.path.isfile(bak)
        # Written file must parse back correctly
        data2 = load_pcb(str(src))
        assert str(data2[0]) == "kicad_pcb"

    def test_backup_preserves_original_content(self, tmp_path):
        src = tmp_path / "board.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, src)
        original = src.read_text()
        data = load_pcb(str(src))
        save_pcb(str(src), data)
        bak_content = (tmp_path / "board.kicad_pcb.bak").read_text()
        assert bak_content == original

    def test_round_trip_preserves_footprint_count(self, tmp_path):
        src = tmp_path / "board.kicad_pcb"
        shutil.copy(BOARD_FIXTURE, src)
        data = load_pcb(str(src))
        save_pcb(str(src), data)
        data2 = load_pcb(str(src))
        fps1 = [i for i in data if isinstance(i, list) and str(i[0]) == "footprint"]
        fps2 = [i for i in data2 if isinstance(i, list) and str(i[0]) == "footprint"]
        assert len(fps1) == len(fps2) == 3


class TestSerialize:
    def test_footprint_on_own_line(self):
        data = load_pcb(BOARD_FIXTURE)
        text = _serialize(data)
        assert "\n(footprint" in text

    def test_net_on_own_line(self):
        data = load_pcb(BOARD_FIXTURE)
        text = _serialize(data)
        assert "\n(net" in text


# ---------------------------------------------------------------------------
# pcb_footprint_utils
# ---------------------------------------------------------------------------


class TestFindFootprint:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_finds_r1(self):
        fp = find_footprint(self.data, "R1")
        assert fp is not None
        assert str(fp[0]) == "footprint"

    def test_finds_c1(self):
        fp = find_footprint(self.data, "C1")
        assert fp is not None

    def test_raises_on_missing(self):
        with pytest.raises(KeyError, match="U99"):
            find_footprint(self.data, "U99")


class TestGetFpAt:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_r1_position(self):
        fp = find_footprint(self.data, "R1")
        x, y, rot = get_fp_at(fp)
        assert x == pytest.approx(10.0)
        assert y == pytest.approx(20.0)
        assert rot == pytest.approx(0.0)

    def test_c1_rotation(self):
        fp = find_footprint(self.data, "C1")
        _, _, rot = get_fp_at(fp)
        assert rot == pytest.approx(90.0)


class TestSetFpAt:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_updates_position(self):
        fp = find_footprint(self.data, "R1")
        set_fp_at(fp, 55.5, 66.6, 45.0)
        x, y, rot = get_fp_at(fp)
        assert x == pytest.approx(55.5)
        assert y == pytest.approx(66.6)
        assert rot == pytest.approx(45.0)


class TestGetFpProperty:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_get_reference(self):
        fp = find_footprint(self.data, "R1")
        assert get_fp_property(fp, "Reference") == "R1"

    def test_get_value(self):
        fp = find_footprint(self.data, "R1")
        assert get_fp_property(fp, "Value") == "10k"

    def test_missing_property_returns_none(self):
        fp = find_footprint(self.data, "R1")
        assert get_fp_property(fp, "NonExistent") is None


class TestSetFpProperty:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_updates_value(self):
        fp = find_footprint(self.data, "R1")
        result = set_fp_property(fp, "Value", "22k")
        assert result is True
        assert get_fp_property(fp, "Value") == "22k"

    def test_returns_false_for_missing(self):
        fp = find_footprint(self.data, "R1")
        assert set_fp_property(fp, "NoSuchProp", "x") is False


def _property_node(fp: list, name: str) -> list | None:
    """Return the ``property`` child node named *name* inside *fp*, or None."""
    for sub in fp:
        if isinstance(sub, list) and len(sub) >= 3 and str(sub[0]) == "property":
            prop_name = sub[1] if isinstance(sub[1], str) else str(sub[1])
            if prop_name == name:
                return sub
    return None


class TestUpsertFpProperty:
    """Hidden-marker behavior of upsert_fp_property.

    The ``hidden`` flag is the internal-metadata channel: only agent-internal
    properties (placement_group) may use it; the shared callers with the
    default ``hidden=False`` (Reference designator) must keep the exact
    visibility they had before.
    """

    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)
        self.fp = find_footprint(self.data, "R1")

    def test_creates_hidden_node_with_hide_marker(self):
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        node = _property_node(self.fp, "placement_group")
        assert node is not None
        assert _fp_property_is_hidden(node) is True
        # KiCad 10 canonical hidden shape: node-level (hide yes), a direct
        # child of the property node.
        assert _serialize([self.fp]).find('(property "placement_group" "usb_c" (hide yes))') != -1

    def test_creates_visible_node_without_hide_marker(self):
        upsert_fp_property(self.fp, "placement_group", "usb_c")
        node = _property_node(self.fp, "placement_group")
        assert node is not None
        assert get_fp_property(self.fp, "placement_group") == "usb_c"
        assert _fp_property_is_hidden(node) is False
        assert all(not (isinstance(s, list) and s and str(s[0]) == "effects") for s in node[3:])

    def test_update_preserves_hide_marker(self):
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        upsert_fp_property(self.fp, "placement_group", "power", hidden=True)
        node = _property_node(self.fp, "placement_group")
        assert get_fp_property(self.fp, "placement_group") == "power"
        assert _fp_property_is_hidden(node) is True
        markers = [s for s in node[3:] if isinstance(s, list) and s and str(s[0]) == "hide"]
        assert len(markers) == 1

    def test_heal_adds_node_level_marker_after_at_and_layer(self):
        """Healing an existing node places (hide yes) after at/layer, before
        the trailing effects block — the order KiCad 10 writes."""
        upsert_fp_property(self.fp, "placement_group", "usb_c")
        node = _property_node(self.fp, "placement_group")
        node.append([sexpdata.Symbol("at"), -1.5, 2.0])
        node.append([sexpdata.Symbol("layer"), "F.Fab"])
        node.append(
            [
                sexpdata.Symbol("effects"),
                [sexpdata.Symbol("font"), [sexpdata.Symbol("size"), 1.27, 1.27]],
            ]
        )
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        assert _fp_property_is_hidden(node) is True
        keys = [str(s[0]) for s in node[3:] if isinstance(s, list) and s]
        assert keys[:3] == ["at", "layer", "hide"]
        assert "effects" in keys[3:]

    def test_heal_places_hide_yes_after_stray_hide_no(self):
        """A stale hand-written `(hide no)` must not shadow the healed marker.

        KiCad applies hide tokens sequentially — last one wins — so the new
        `(hide yes)` must be ordered after an existing `(hide no)`.  Without
        the order hardening this produced `(hide yes) (hide no)` (visible in
        KiCad while reported hidden) when at/layer preceded the stray token.
        """
        upsert_fp_property(self.fp, "placement_group", "usb_c")
        node = _property_node(self.fp, "placement_group")
        node.append([sexpdata.Symbol("at"), 0.0, 0.0])
        node.append([sexpdata.Symbol("layer"), "F.Fab"])
        node.append([sexpdata.Symbol("hide"), sexpdata.Symbol("no")])
        node.append([sexpdata.Symbol("uuid"), "7a3db89d-8dcf-4bd0-808e-2abe95a3086d"])
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        assert _fp_property_is_hidden(node) is True
        assert (
            _serialize([self.fp]).find(
                '(property "placement_group" "usb_c" (at 0.0 0.0) (layer "F.Fab") '
                '(hide no) (hide yes) (uuid "7a3db89d-8dcf-4bd0-808e-2abe95a3086d"))'
            )
            != -1
        )

    @pytest.mark.parametrize(
        "extra_children",
        [
            # (hide no) inside the canonical segment.
            (
                [sexpdata.Symbol("at"), 0.0, 0.0],
                [sexpdata.Symbol("layer"), "F.Fab"],
                [sexpdata.Symbol("hide"), sexpdata.Symbol("no")],
                [sexpdata.Symbol("uuid"), "7a3db89d-8dcf-4bd0-808e-2abe95a3086d"],
                [sexpdata.Symbol("effects"), [sexpdata.Symbol("font"), 1.0]],
            ),
            # (hide no) between uuid and effects — past the walk break point.
            (
                [sexpdata.Symbol("at"), 0.0, 0.0],
                [sexpdata.Symbol("layer"), "F.Fab"],
                [sexpdata.Symbol("uuid"), "7a3db89d-8dcf-4bd0-808e-2abe95a3086d"],
                [sexpdata.Symbol("hide"), sexpdata.Symbol("no")],
                [sexpdata.Symbol("effects"), [sexpdata.Symbol("font"), 1.0]],
            ),
            # (hide no) after effects entirely.
            (
                [sexpdata.Symbol("at"), 0.0, 0.0],
                [sexpdata.Symbol("layer"), "F.Fab"],
                [sexpdata.Symbol("uuid"), "7a3db89d-8dcf-4bd0-808e-2abe95a3086d"],
                [sexpdata.Symbol("effects"), [sexpdata.Symbol("font"), 1.0]],
                [sexpdata.Symbol("hide"), sexpdata.Symbol("no")],
            ),
        ],
        ids=["before_uuid", "after_uuid", "after_effects"],
    )
    def test_heal_hide_yes_is_always_trailing_hide(self, extra_children):
        """Healing must leave (hide yes) as the LAST hide token in the node,
        wherever a stale (hide no) sits — KiCad's last-token-wins parse."""
        upsert_fp_property(self.fp, "placement_group", "usb_c")
        node = _property_node(self.fp, "placement_group")
        node.extend(extra_children)
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        assert _fp_property_is_hidden(node) is True
        serialized = sexpdata.dumps(node)
        hide_no_at = serialized.find("(hide no)")
        hide_yes_at = serialized.find("(hide yes)")
        # The new marker is ordered after the stale one and is the trailing
        # hide token in the node.
        assert 0 <= hide_no_at < hide_yes_at
        assert "(hide" not in serialized[hide_yes_at + len("(hide yes)") :]

    def test_hidden_upsert_heals_existing_visible_node(self):
        upsert_fp_property(self.fp, "placement_group", "usb_c")
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        node = _property_node(self.fp, "placement_group")
        assert _fp_property_is_hidden(node) is True
        assert _serialize([self.fp]).find('(property "placement_group" "usb_c" (hide yes))') != -1

    def test_default_upsert_leaves_existing_hidden_untouched(self):
        upsert_fp_property(self.fp, "placement_group", "usb_c", hidden=True)
        upsert_fp_property(self.fp, "placement_group", "power")
        node = _property_node(self.fp, "placement_group")
        assert _fp_property_is_hidden(node) is True
        assert get_fp_property(self.fp, "placement_group") == "power"

    def test_reference_default_stays_visible(self):
        """The Reference designator path must never gain a hide marker."""
        upsert_fp_property(self.fp, "Reference", "R1A")
        node = _property_node(self.fp, "Reference")
        assert get_fp_property(self.fp, "Reference") == "R1A"
        assert _fp_property_is_hidden(node) is False
        assert all(not (isinstance(s, list) and s and str(s[0]) == "effects") for s in node[3:])


class TestFpPropertyHiddenReadCompat:
    """_fp_property_is_hidden must read every hide form KiCad has written.

    Writer output: node-level ``(hide yes)``.  Real boards from older KiCad
    or the previous server may carry ``(effects (hide))`` or bare ``(hide)``
    — all of these are recognized as hidden on input.
    """

    def _prop(self, *children: list) -> list:
        return [sexpdata.Symbol("property"), "placement_group", "g", *children]

    def test_node_level_hide_yes(self):
        assert (
            _fp_property_is_hidden(self._prop([sexpdata.Symbol("hide"), sexpdata.Symbol("yes")]))
            is True
        )

    def test_legacy_hide_inside_effects(self):
        assert (
            _fp_property_is_hidden(
                self._prop([sexpdata.Symbol("effects"), [sexpdata.Symbol("hide")]])
            )
            is True
        )

    def test_legacy_hide_yes_inside_effects(self):
        assert (
            _fp_property_is_hidden(
                self._prop(
                    [
                        sexpdata.Symbol("effects"),
                        [sexpdata.Symbol("hide"), sexpdata.Symbol("yes")],
                    ]
                )
            )
            is True
        )

    def test_legacy_bare_hide_on_node(self):
        assert _fp_property_is_hidden(self._prop([sexpdata.Symbol("hide")])) is True

    def test_no_marker_is_visible(self):
        assert _fp_property_is_hidden(self._prop()) is False

    def test_hide_no_is_visible(self):
        assert (
            _fp_property_is_hidden(self._prop([sexpdata.Symbol("hide"), sexpdata.Symbol("no")]))
            is False
        )


class TestFlipFpLayers:
    def setup_method(self):
        self.data = load_pcb(BOARD_FIXTURE)

    def test_flip_f_to_b(self):
        fp = find_footprint(self.data, "R1")
        assert get_fp_layer(fp) == "F.Cu"
        flip_fp_layers(fp)
        assert get_fp_layer(fp) == "B.Cu"

    def test_flip_b_to_f(self):
        fp = find_footprint(self.data, "J1")
        assert get_fp_layer(fp) == "B.Cu"
        flip_fp_layers(fp)
        assert get_fp_layer(fp) == "F.Cu"

    def test_double_flip_restores_original(self):
        fp = find_footprint(self.data, "R1")
        original = get_fp_layer(fp)
        flip_fp_layers(fp)
        flip_fp_layers(fp)
        assert get_fp_layer(fp) == original


class TestLayerFlipMap:
    def test_all_entries_are_symmetric(self):
        for src, dst in LAYER_FLIP_MAP.items():
            assert LAYER_FLIP_MAP.get(dst) == src, f"{src} -> {dst} has no reverse mapping"
