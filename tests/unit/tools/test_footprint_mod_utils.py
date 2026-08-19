"""Unit tests for footprint .kicad_mod creation/editing utilities."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kcaa.utils.footprint_mod_utils import (
    add_fp_arc,
    add_fp_circle,
    add_fp_line,
    add_fp_rect,
    add_fp_text,
    add_pad,
    create_footprint_mod,
    delete_element_from_footprint,
    get_footprint_mod_info,
    load_footprint_mod,
    save_footprint_mod,
    set_footprint_mod_attr,
)


@pytest.fixture
def tmp_mod(tmp_path: Path) -> Path:
    """Return a path to a temporary .kicad_mod file."""
    pretty = tmp_path / "MyLib.pretty"
    pretty.mkdir()
    return pretty / "R_0805.kicad_mod"


def test_create_footprint_mod(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805", layer="F.Cu", description="0805 resistor", tags="resistor", attr="smd")
    save_footprint_mod(str(tmp_mod), data)

    assert tmp_mod.exists()
    info = get_footprint_mod_info(load_footprint_mod(str(tmp_mod)))
    assert info["name"] == "R_0805"
    assert info["layer"] == "F.Cu"
    assert info["description"] == "0805 resistor"
    assert info["tags"] == "resistor"
    assert info["attr"] == "smd"


def test_load_footprint_mod(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805", layer="F.Cu")
    save_footprint_mod(str(tmp_mod), data)

    loaded = load_footprint_mod(str(tmp_mod))
    assert loaded[1] == "R_0805"


def test_save_creates_backup(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    save_footprint_mod(str(tmp_mod), data)
    backup = save_footprint_mod(str(tmp_mod), data)

    assert os.path.exists(backup)
    assert backup == str(tmp_mod) + ".bak"


def test_add_pad(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    add_pad(data, "1", "smd", "rect", (0, 0), (1.0, 1.2), ["F.Cu", "F.Paste", "F.Mask"])
    save_footprint_mod(str(tmp_mod), data)

    info = get_footprint_mod_info(load_footprint_mod(str(tmp_mod)))
    assert info["pad_count"] == 1


def test_add_pad_with_rotation_and_drill(tmp_mod: Path) -> None:
    data = create_footprint_mod("HDR")
    add_pad(data, "1", "thru_hole", "circle", (0, 0, 45), (1.5, 1.5), ["*.Cu", "*.Mask"], drill=0.8)
    save_footprint_mod(str(tmp_mod), data)

    loaded = load_footprint_mod(str(tmp_mod))
    info = get_footprint_mod_info(loaded)
    assert info["pad_count"] == 1
    raw = tmp_mod.read_text()
    assert "thru_hole" in raw
    assert "circle" in raw
    assert "(drill 0.8)" in raw


def test_add_graphics(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    add_fp_line(data, (-1, 0.5), (1, 0.5), "F.SilkS", 0.12)
    add_fp_arc(data, (-1, 0), (0, 1), (1, 0), "F.SilkS", 0.12)
    add_fp_circle(data, (0, 0), (0.5, 0), "F.Fab", 0.1)
    add_fp_rect(data, (-1, -1), (1, 1), "F.Courtyard", 0.05)
    save_footprint_mod(str(tmp_mod), data)

    info = get_footprint_mod_info(load_footprint_mod(str(tmp_mod)))
    assert info["line_count"] == 1
    assert info["arc_count"] == 1
    assert info["circle_count"] == 1
    assert info["rect_count"] == 1


def test_add_text(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    add_fp_text(data, "reference", "REF**", (0, -1.5), "F.SilkS")
    add_fp_text(data, "value", "R_0805", (0, 1.5), "F.Fab")
    save_footprint_mod(str(tmp_mod), data)

    info = get_footprint_mod_info(load_footprint_mod(str(tmp_mod)))
    assert info["text_count"] == 2


def test_set_and_delete_attribute(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    set_footprint_mod_attr(data, "descr", "New description")
    save_footprint_mod(str(tmp_mod), data)

    loaded = load_footprint_mod(str(tmp_mod))
    info = get_footprint_mod_info(loaded)
    assert info["description"] == "New description"


def test_delete_element(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    add_pad(data, "1", "smd", "rect", (0, 0), (1, 1), ["F.Cu"])
    add_pad(data, "2", "smd", "rect", (1, 0), (1, 1), ["F.Cu"])
    assert get_footprint_mod_info(data)["pad_count"] == 2

    deleted = delete_element_from_footprint(data, "pad", 0)
    assert deleted is True
    assert get_footprint_mod_info(data)["pad_count"] == 1


def test_delete_element_out_of_range(tmp_mod: Path) -> None:
    data = create_footprint_mod("R_0805")
    deleted = delete_element_from_footprint(data, "pad", 5)
    assert deleted is False
