"""Tests for PCB S-expression load/save atomicity and encoding."""

from __future__ import annotations

from pathlib import Path

import pytest

from kcaa.utils import atomic_io, pcb_sexp_utils
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

PCB_TEXT = '(kicad_pcb (version 20240108) (generator "kcaa") (layers))\n'


def _tmp_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.tmp"))


def _stage_pcb(tmp_path: Path) -> tuple[Path, list]:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(PCB_TEXT, encoding="utf-8")
    return path, load_pcb(str(path))


def test_save_pcb_backs_up_previous_version(tmp_path: Path) -> None:
    path, data = _stage_pcb(tmp_path)
    original = path.read_bytes()

    bak = save_pcb(str(path), data)

    assert bak == str(path) + ".bak"
    assert Path(bak).read_bytes() == original
    assert path.read_text(encoding="utf-8").startswith("(kicad_pcb")
    assert _tmp_files(tmp_path) == []


def test_serialize_failure_leaves_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, data = _stage_pcb(tmp_path)
    original = path.read_bytes()

    def _explode(tree: object) -> str:
        raise ValueError("bad tree")

    monkeypatch.setattr(pcb_sexp_utils, "_serialize", _explode)

    with pytest.raises(ValueError, match="bad tree"):
        save_pcb(str(path), data)

    assert path.read_bytes() == original
    assert not (tmp_path / "board.kicad_pcb.bak").exists()
    assert _tmp_files(tmp_path) == []


def test_write_failure_keeps_target_and_removes_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, data = _stage_pcb(tmp_path)
    original = path.read_bytes()

    def _fail_replace(source: str, target: str) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(atomic_io.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="rename refused"):
        save_pcb(str(path), data)

    assert path.read_bytes() == original
    assert Path(str(path) + ".bak").read_bytes() == original
    assert _tmp_files(tmp_path) == []


def test_load_pcb_rejects_gbk_with_guidance(tmp_path: Path) -> None:
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes("(kicad_pcb 中文)".encode("gbk"))

    with pytest.raises(ValueError) as excinfo:
        load_pcb(str(path))

    message = str(excinfo.value)
    assert "not valid UTF-8" in message
    assert ".bak" in message
