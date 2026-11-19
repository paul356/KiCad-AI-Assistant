"""Tests for schematic save: UTF-8 encoding and atomic write protection."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys

import pytest

from kcaa.tools.sheet_tools import _generate_child_schematic
from kcaa.utils import atomic_io, schematic_sexp_utils
from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic

NON_ASCII = "µΩ中文"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _tmp_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.tmp"))


# Minimal valid schematic.  The text element carries characters that cannot
# be encoded with ASCII/gbk, so a locale-dependent write fails on it.
_SCHEMATIC = f"""(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")
  (uuid "abcd0000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (text "{NON_ASCII}" (at 10 10 0) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "abcd0000-0000-0000-0000-000000000002"))
  (sheet_instances (path "/" (page "1")))
  (embedded_fonts no)
)
"""

# Executed in a subprocess whose preferred encoding is forced away from UTF-8.
_CHILD_SCRIPT = """
import locale
import sys

from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic

encoding_path, schematic_path = sys.argv[1], sys.argv[2]
with open(encoding_path, "w", encoding="ascii") as fh:
    fh.write(locale.getpreferredencoding(False))
save_schematic(schematic_path, safe_schematic(schematic_path))
"""


def _stage_schematic(tmp_path: Path) -> tuple[Path, bytes]:
    path = tmp_path / "board.kicad_sch"
    path.write_text(_SCHEMATIC, encoding="utf-8")
    return path, path.read_bytes()


def _text_values(path: Path) -> list[str]:
    return [element.value for element in safe_schematic(str(path)).text]


def test_save_under_non_utf8_locale(tmp_path: Path) -> None:
    """Saving must force UTF-8 even when the locale codec cannot encode µ."""
    path, _ = _stage_schematic(tmp_path)
    encoding_file = tmp_path / "encoding.txt"

    env = dict(os.environ)
    env.update({"PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "PYTHONUTF8": "0"})
    env.setdefault("KICAD_VERSION", "9.0")

    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(encoding_file), str(path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"save failed under non-UTF-8 locale:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    encoding = encoding_file.read_text(encoding="ascii")
    if encoding.lower().replace("-", "") in {"utf8", "utf_8"}:
        pytest.skip(f"could not force a non-UTF-8 locale (got {encoding!r})")

    raw = path.read_bytes()
    assert NON_ASCII in raw.decode("utf-8")
    assert _text_values(path) == [NON_ASCII]
    assert _tmp_files(tmp_path) == []


def test_serialization_failure_leaves_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-write failure must discard the temp file and keep the original."""
    path, original = _stage_schematic(tmp_path)
    sch = safe_schematic(str(path))

    def _explode_after_partial_write(fpath: str, tree: object) -> None:
        Path(fpath).write_text("(kicad_sch (partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr("skip.sexp.sourcefile.writeTree", _explode_after_partial_write)

    with pytest.raises(OSError) as excinfo:
        save_schematic(str(path), sch)

    assert "disk full" in str(excinfo.value)
    assert path.read_bytes() == original
    assert _tmp_files(tmp_path) == []
    # The pre-write backup was already taken and still reflects the original.
    assert (tmp_path / "board.kicad_sch.bak").read_bytes() == original


def test_oserror_type_and_attributes_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError subclasses are re-raised unchanged after temp cleanup."""
    path, original = _stage_schematic(tmp_path)
    sch = safe_schematic(str(path))
    target = str(path)

    def _fail_replace(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "permission denied", dst)

    monkeypatch.setattr(atomic_io.os, "replace", _fail_replace)

    with pytest.raises(PermissionError) as excinfo:
        save_schematic(target, sch)

    assert excinfo.value.errno == errno.EACCES
    assert excinfo.value.filename == target
    assert path.read_bytes() == original
    assert _tmp_files(tmp_path) == []


def test_non_oserror_is_wrapped_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Programming errors from the serializer keep their chain and context."""
    path, original = _stage_schematic(tmp_path)
    sch = safe_schematic(str(path))

    def _explode(fpath: str, tree: object) -> None:
        raise ValueError("bad tree")

    monkeypatch.setattr("skip.sexp.sourcefile.writeTree", _explode)

    with pytest.raises(OSError) as excinfo:
        save_schematic(str(path), sch)

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert str(path) in str(excinfo.value)
    assert str(path) + ".bak" in str(excinfo.value)
    assert path.read_bytes() == original
    assert _tmp_files(tmp_path) == []


def test_refused_write_does_not_replace_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skip silently skipping the write must not be mistaken for success."""
    path, original = _stage_schematic(tmp_path)

    class _RefusingWriter:
        def write(self, fpath: str) -> None:  # pragma: no cover - must not write
            return None

    with pytest.raises(OSError, match="produced no output") as excinfo:
        save_schematic(str(path), _RefusingWriter())  # type: ignore[arg-type]

    assert str(path) in str(excinfo.value)
    assert str(path) + ".bak" in str(excinfo.value)
    assert path.read_bytes() == original
    assert _tmp_files(tmp_path) == []


def test_backup_failure_does_not_touch_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed backup must abort before the main file is rewritten."""
    path, original = _stage_schematic(tmp_path)

    def _fail_copy(src: str, dst: str) -> None:
        raise OSError("backup refused")

    monkeypatch.setattr(atomic_io.shutil, "copy2", _fail_copy)

    with pytest.raises(OSError, match="backup refused"):
        save_schematic(str(path), safe_schematic(str(path)))

    assert path.read_bytes() == original
    assert _tmp_files(tmp_path) == []


def test_backup_matches_pre_write_bytes(tmp_path: Path) -> None:
    """The .bak file is the exact pre-write state of the schematic."""
    path, original = _stage_schematic(tmp_path)

    backup = save_schematic(str(path), safe_schematic(str(path)))

    assert backup == str(path) + ".bak"
    assert Path(backup).read_bytes() == original


def test_round_trip_is_utf8_and_stable(tmp_path: Path) -> None:
    """Non-ASCII content survives and re-saving does not drift the layout."""
    path, _ = _stage_schematic(tmp_path)

    save_schematic(str(path), safe_schematic(str(path)))
    first_pass = path.read_bytes()

    raw = first_pass.decode("utf-8")
    assert NON_ASCII in raw
    assert "\r\n" not in raw

    reloaded = safe_schematic(str(path))
    assert [element.value for element in reloaded.text] == [NON_ASCII]

    save_schematic(str(path), reloaded)
    assert path.read_bytes() == first_pass


def test_write_schematic_text_is_utf8(tmp_path: Path) -> None:
    path = tmp_path / "child.kicad_sch"
    payload = f'(kicad_sch (text "{NON_ASCII}"))\n'

    schematic_sexp_utils.write_schematic_text(str(path), payload)

    assert path.read_bytes().decode("utf-8") == payload
    assert _tmp_files(tmp_path) == []


def test_write_schematic_text_failure_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "child.kicad_sch"

    def _fail_replace(src: str, dst: str) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(atomic_io.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="rename refused"):
        schematic_sexp_utils.write_schematic_text(str(path), "(kicad_sch)\n")

    assert not path.exists()
    assert _tmp_files(tmp_path) == []


def test_generate_child_schematic_writes_utf8_atomically(tmp_path: Path) -> None:
    """A generated child sheet keeps a non-ASCII title and leaves no .tmp."""
    parent = tmp_path / "root.kicad_sch"
    parent.write_text("(kicad_sch)\n", encoding="utf-8")
    title = "分压器 µΩ 模块"

    child_path = Path(_generate_child_schematic(str(parent), "child", title=title))

    assert child_path == tmp_path / "child.kicad_sch"
    assert title in child_path.read_bytes().decode("utf-8")
    assert _tmp_files(tmp_path) == []


def test_chmod_failure_does_not_fail_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Permission parity is best effort: chmod rejection must not lose the save."""
    path, _ = _stage_schematic(tmp_path)
    chmod_calls: list[str] = []

    def _deny_chmod(target: str, mode: int, *, follow_symlinks: bool = True) -> None:
        if ".kicad_sch." in target and target.endswith(".tmp"):
            chmod_calls.append(target)
            raise PermissionError(errno.EPERM, "chmod refused", target)

    monkeypatch.setattr(atomic_io.os, "chmod", _deny_chmod)

    backup = save_schematic(str(path), safe_schematic(str(path)))

    assert chmod_calls
    assert Path(backup).exists()
    assert NON_ASCII in path.read_bytes().decode("utf-8")
    assert _text_values(path) == [NON_ASCII]
    assert _tmp_files(tmp_path) == []
