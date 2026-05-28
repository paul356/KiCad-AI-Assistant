"""
Unit tests for kcaa/utils/pcb_sexp_utils.py and
kcaa/utils/pcb_footprint_utils.py.

These modules are pure helpers (no FastMCP dependency) so they can be
imported and tested without the mcp.tool() decoration pattern.
"""

import os
import shutil

import pytest

from kcaa.utils.pcb_footprint_utils import (
    LAYER_FLIP_MAP,
    find_footprint,
    flip_fp_layers,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    set_fp_at,
    set_fp_property,
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
