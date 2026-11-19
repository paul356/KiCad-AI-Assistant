"""Tests for the shared atomic I/O helpers."""

from __future__ import annotations

from pathlib import Path
import stat

import pytest

from kcaa.utils.atomic_io import atomic_write_text, atomic_write_with, read_text_utf8

TEXT = "µΩ中文"


def test_atomic_write_text_writes_utf8(tmp_path: Path) -> None:
    path = tmp_path / "file.kicad_sch"

    atomic_write_text(str(path), TEXT + "\n")

    assert path.read_bytes() == (TEXT + "\n").encode("utf-8")
    assert not (tmp_path / "file.kicad_sch.tmp").exists()


def test_atomic_write_text_preserves_mode(tmp_path: Path) -> None:
    path = tmp_path / "file.kicad_sch"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o640)

    atomic_write_text(str(path), "new\n")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text(encoding="utf-8") == "new\n"


def test_backup_is_previous_bytes_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "file.kicad_sch"
    original = (TEXT + "\n").encode("utf-8")
    path.write_bytes(original)
    path.chmod(0o640)

    bak = atomic_write_text(str(path), "new\n", create_backup=True)

    assert bak == str(path) + ".bak"
    assert Path(bak).read_bytes() == original
    assert stat.S_IMODE(Path(bak).stat().st_mode) == 0o640
    assert path.read_text(encoding="utf-8") == "new\n"


def test_write_failure_keeps_original_and_removes_tmp(tmp_path: Path) -> None:
    path = tmp_path / "file.kicad_sch"
    original = (TEXT + "\n").encode("utf-8")
    path.write_bytes(original)

    def _explode(tmp: str) -> None:
        Path(tmp).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        atomic_write_with(str(path), _explode)

    assert path.read_bytes() == original
    assert not (tmp_path / "file.kicad_sch.tmp").exists()


def test_non_oserror_is_wrapped_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.kicad_sch"
    original = (TEXT + "\n").encode("utf-8")
    path.write_bytes(original)

    def _explode(tmp: str) -> None:
        raise ValueError("bad tree")

    with pytest.raises(OSError) as excinfo:
        atomic_write_with(str(path), _explode, create_backup=True)

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert str(path) in str(excinfo.value)
    assert str(path) + ".bak" in str(excinfo.value)
    assert path.read_bytes() == original
    assert not (tmp_path / "file.kicad_sch.tmp").exists()


def test_empty_output_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "file.kicad_sch"
    original = (TEXT + "\n").encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(OSError, match="empty"):
        atomic_write_text(str(path), "")

    assert path.read_bytes() == original
    assert not (tmp_path / "file.kicad_sch.tmp").exists()


def test_read_text_utf8_accepts_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf" + TEXT.encode("utf-8"))

    assert read_text_utf8(str(path)) == TEXT


def test_read_text_utf8_rejects_gbk_with_guidance(tmp_path: Path) -> None:
    path = tmp_path / "gbk.kicad_sch"
    path.write_bytes("中文原理图".encode("gbk"))

    with pytest.raises(ValueError) as excinfo:
        read_text_utf8(str(path))

    message = str(excinfo.value)
    assert str(path) in message
    assert "not valid UTF-8" in message
    assert "GBK" in message
    assert ".bak" in message
    assert "convert" in message
