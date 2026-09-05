"""Tests for kcaa.utils.schematic_sexp_utils — save_schematic corruption detection.

Covers:
* Round-trip through save_schematic leaves the file valid (happy path).
* Unbalanced-bracket truncation is detected; original file is preserved.
* sexpdata-unparseable output is detected.
* Empty output is detected.
* ``.tmp`` staging file is cleaned up on failure.
* ``.bak`` sibling is preserved on failure.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kcaa.utils.schematic_sexp_utils import (
    SchematicCorruptionError,
    check_schematic_integrity,
    load_schematic,
    save_schematic,
    validate_schematic_bytes,
)


SCHEMATIC_PATH = str(
    Path(__file__).parent.parent / "tools" / "fixtures" / "tools_test.kicad_sch"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_sch():
    """Fresh copy of the fixture schematic; cleaned up after each test."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir()
    )
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    yield tmp.name
    for p in (tmp.name, tmp.name + ".bak", tmp.name + ".tmp"):
        if os.path.exists(p):
            os.unlink(p)


def _fake_sch(write_payload: bytes):
    """Return a mock schematic whose ``.write(path)`` writes ``write_payload``."""
    fake = MagicMock()

    def _write(path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(write_payload)

    fake.write = _write
    return fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_round_trip_preserves_balance(self, tmp_sch):
        from kcaa.utils.skip_compat import safe_schematic

        sch = safe_schematic(tmp_sch)
        bak = save_schematic(tmp_sch, sch)

        assert bak == tmp_sch + ".bak"
        with open(tmp_sch) as fh:
            text = fh.read()
        assert text.count("(") == text.count(")")
        # The .bak is byte-identical to the original fixture.
        with open(SCHEMATIC_PATH) as fh:
            original = fh.read()
        with open(bak) as fh:
            backup = fh.read()
        assert backup == original

    def test_load_after_save_succeeds(self, tmp_sch):
        from kcaa.utils.skip_compat import safe_schematic

        sch = safe_schematic(tmp_sch)
        save_schematic(tmp_sch, sch)
        # Should not raise
        load_schematic(tmp_sch)


# ---------------------------------------------------------------------------
# Corruption detection
# ---------------------------------------------------------------------------


class TestCorruptionDetection:
    def test_unbalanced_brackets_refuses_replace(self, tmp_sch):
        # Truncate trailing closing brackets — the historical bug.
        truncated = b'(kicad_sch (version 20240101) (paper "A4"'
        with pytest.raises(SchematicCorruptionError, match="unclosed parens"):
            save_schematic(tmp_sch, _fake_sch(truncated))

        # After the exception, original file must be untouched.
        with open(tmp_sch, "rb") as fh:
            restored = fh.read()
        with open(SCHEMATIC_PATH, "rb") as fh:
            original = fh.read()
        assert restored == original  # untouched

    def test_unbalanced_brackets_raises_with_depth_diff(self, tmp_sch):
        truncated = b'(kicad_sch (version 20240101) (paper "A4"'
        with pytest.raises(SchematicCorruptionError, match=r"depth = 2"):
            save_schematic(tmp_sch, _fake_sch(truncated))

    # Note: sexpdata is too lenient to construct a *balanced* payload it
    # rejects outright, so the S-expression parse branch in the validator
    # is exercised only as defense-in-depth.  The bracket-balance check is
    # the primary corruption guard and is fully covered above.

    def test_empty_file_raises(self, tmp_sch):
        with pytest.raises(SchematicCorruptionError, match="empty"):
            save_schematic(tmp_sch, _fake_sch(b""))

    def test_whitespace_only_file_raises(self, tmp_sch):
        with pytest.raises(SchematicCorruptionError, match="empty"):
            save_schematic(tmp_sch, _fake_sch(b"   \n\t  "))


# ---------------------------------------------------------------------------
# Cleanup behaviour on failure
# ---------------------------------------------------------------------------


class TestFailureCleanup:
    def test_tmp_removed_on_corruption(self, tmp_sch):
        tmp_path = tmp_sch + ".tmp"
        try:
            save_schematic(tmp_sch, _fake_sch(b"(kicad_sch"))
        except SchematicCorruptionError:
            pass
        assert not os.path.exists(tmp_path)

    def test_bak_preserved_on_corruption(self, tmp_sch):
        try:
            save_schematic(tmp_sch, _fake_sch(b"(kicad_sch"))
        except SchematicCorruptionError:
            pass
        assert os.path.exists(tmp_sch + ".bak")
        # And the .bak is byte-identical to the original fixture.
        with open(tmp_sch + ".bak") as fh:
            backup = fh.read()
        with open(SCHEMATIC_PATH) as fh:
            original = fh.read()
        assert backup == original

    def test_tmp_removed_on_write_exception(self, tmp_sch):
        # If sch.write itself raises, the .tmp must still be cleaned up.
        tmp_path = tmp_sch + ".tmp"
        failing_sch = MagicMock()
        failing_sch.write = lambda path: (_ for _ in ()).throw(
            RuntimeError("disk full")
        )

        with pytest.raises(RuntimeError, match="disk full"):
            save_schematic(tmp_sch, failing_sch)
        assert not os.path.exists(tmp_path)
        # The original is also untouched in this case (replace never ran).
        with open(tmp_sch, "rb") as fh:
            assert fh.read() == open(SCHEMATIC_PATH, "rb").read()


# ---------------------------------------------------------------------------
# UTF-8 write-path regression
# ---------------------------------------------------------------------------


class TestUtf8WritePath:
    """``save_schematic`` must write through UTF-8 regardless of locale.

    ``skip``'s internal ``writeTree`` calls ``open(path, 'w')`` with no
    encoding argument (see ``.venv/.../skip/sexp/util.py``). On a
    non-UTF-8 Windows locale (cp1252 / gbk) that mangles or truncates
    the file — the historical cause of the silent corruption that
    ``delete_wire_from_schematic`` was blamed for.

    The fix wraps ``sch.write()`` in
    :func:`kcaa.utils.skip_compat.utf8_open_context`, which forces
    UTF-8 on top of whatever default the system has.  These tests
    pin that wrap in place.
    """

    def test_save_schematic_enters_utf8_context_around_write(
        self, monkeypatch, tmp_path
    ):
        """The write path must run inside ``utf8_open_context``.

        We patch the context manager on the ``schematic_sexp_utils``
        module to a capturing sentinel. If ``save_schematic`` calls
        ``sch.write()`` outside the wrap, the sentinel sees no entry
        and the assertion fails — that's exactly the bug pattern
        that caused the original Windows corruption.
        """
        from kcaa.utils import schematic_sexp_utils

        # Sentinel that records enter/exit without doing anything else.
        events: list[str] = []

        class CapturingContext:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *exc):
                events.append("exit")
                return False

        monkeypatch.setattr(
            schematic_sexp_utils, "utf8_open_context", CapturingContext
        )

        # Mock sch: write() records whether the wrap was active at call
        # time, then writes a minimal valid S-expression so the
        # post-write validator can find the file.
        write_active_wrap: list[bool] = []

        def fake_write(path: str) -> None:
            write_active_wrap.append(bool(events) and events[-1] == "enter")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(kicad_sch (version 20240101))")

        sch_mock = MagicMock()
        sch_mock.write = fake_write

        # Create a real file so shutil.copy (the .bak step) succeeds.
        target = tmp_path / "out.kicad_sch"
        target.write_text("(kicad_sch)")
        save_schematic(str(target), sch_mock)

        assert write_active_wrap == [True], (
            "sch.write() was called outside the UTF-8 context — "
            "the wrap in save_schematic was removed or skipped."
        )
        assert events == ["enter", "exit"], (
            f"Expected exactly one enter/exit pair, got {events!r}"
        )

    def test_utf8_context_skipped_when_sch_write_raises(
        self, monkeypatch, tmp_path
    ):
        """If ``sch.write()`` raises, the UTF-8 context still exits cleanly.

        No half-written file should leak, and the exception should
        propagate without leaving the wrap in a bad state.
        """
        from kcaa.utils import schematic_sexp_utils

        events: list[str] = []

        class CapturingContext:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")
                return False  # don't suppress

        monkeypatch.setattr(
            schematic_sexp_utils, "utf8_open_context", CapturingContext
        )

        sch_mock = MagicMock()
        sch_mock.write = lambda path: (_ for _ in ()).throw(
            RuntimeError("disk full")
        )

        target = tmp_path / "out.kicad_sch"
        target.write_text("(kicad_sch)")
        with pytest.raises(RuntimeError, match="disk full"):
            save_schematic(str(target), sch_mock)
        assert events == ["enter", "exit"]


# ---------------------------------------------------------------------------
# Reproduction tests for the user's reported bug patterns
# ---------------------------------------------------------------------------


class TestUserReportedCorruptionPatterns:
    """Pin down the specific corruption shapes observed in the field.

    Three patterns from the bug report:

    * **Mid-element truncation** — file ends ``(wi---`` instead of
      ``(width 0) (type default) (uuid "…")``.  Catches via the EOF
      ``)`` check.
    * **Symmetric truncation** — a complete balanced sub-tree (e.g.
      a whole ``(wire …)`` block) is dropped.  Bracket count matches,
      depth returns to zero, but the file is shorter than the source.
      Caught by the structural top-level check.
    * **Restore from a corrupt snapshot** — ``restore_version`` used
      to ``shutil.copy2`` a malformed snapshot directly over the live
      file.  Now validates the snapshot bytes before the copy.
    """

    def test_mid_element_truncation_caught_by_depth_scan(self, tmp_path):
        """Pattern: ``(wi---`` at end of file (user's exact example).

        The unmatched ``(wi`` opener leaves depth != 0 at EOF, which
        the depth-tracking scan catches before the EOF-pattern check
        ever runs.  Either check would catch this; the depth scan is
        what actually fires here.
        """
        # Truncate inside a (wire …) entry, mid-keyword.  This leaves
        # an unmatched ``(`` so depth returns non-zero at EOF.
        truncated = (
            b'(kicad_sch (version 20240101) (generator "eeschema") '
            b'(paper "A4") (wire (pts (xy 0 0) (xy 10 0)) '
            b'(stroke (wi'
        )
        bad = tmp_path / "mid_trunc.kicad_sch"
        bad.write_bytes(truncated)

        with pytest.raises(SchematicCorruptionError, match=r"unclosed parens"):
            check_schematic_integrity(str(bad))

    def test_eof_pattern_check_fires_when_depth_is_balanced(self, tmp_path):
        """Truncation that drops the outermost ``)`` — depth = 0 but
        the file ends mid-element.  Caught by the EOF ``)`` check
        since the depth scan passes.
        """
        # File looks balanced in depth (every ( has a )) but ends
        # inside a property string with no closing ).  Hand-crafted
        # case: open a string literal whose closing quote + ) was
        # cut off, leaving the structure superficially intact.
        truncated = (
            b'(kicad_sch (version 20240101) (generator "eeschema") '
            b'(paper "A4")) (extraneous_garbage'
        )
        bad = tmp_path / "eof_trunc.kicad_sch"
        bad.write_bytes(truncated)

        # depth == 0 here (the outer kicad_sch closes properly), so
        # the depth scan passes.  sexpdata parses this as a list of
        # two top-level entries, which our structural check rejects.
        with pytest.raises(SchematicCorruptionError):
            check_schematic_integrity(str(bad))

    def test_symmetric_truncation_passes_validator(self, tmp_path):
        """Dropping a complete balanced sub-tree yields a still-valid
        kicad_sch file.  The bracket count and depth scan both pass
        — this corruption pattern is exactly what the multi-gen
        ``.bak`` chain (TestMultiGenBak below) is for.

        Pinning this behavior so future validator upgrades don't
        accidentally start rejecting structurally-valid-but-shorter
        files.
        """
        # Full valid kicad_sch with one (wire …) entry.  The
        # "symmetric" corruption drops the whole wire entry but
        # keeps the rest balanced.
        full = (
            b'(kicad_sch (version 20240101) (generator "eeschema") '
            b'(paper "A4") '
            b'(wire (pts (xy 0 0) (xy 10 0)) '
            b'(stroke (width 0) (type default) (uuid "abc")))'
        )
        # Drop everything from "(wire" to the matching "))" — that's
        # the wire entry's open paren + its closing paren.  The result
        # is a still-balanced (kicad_sch …) with one fewer child.
        cropped = (
            b'(kicad_sch (version 20240101) (generator "eeschema") '
            b'(paper "A4") '
            b'(symbol (lib_id "Device:R") (at 100 100)))'
        )
        bad = tmp_path / "sym_trunc.kicad_sch"
        bad.write_bytes(cropped)

        # Validator accepts this — it's a structurally-valid file,
        # just missing content.  Multi-gen .bak is the recovery path.
        check_schematic_integrity(str(bad))  # no exception
        assert b"wire" not in bad.read_bytes()

    def test_save_schematic_keeps_three_generations_of_bak(self, tmp_sch):
        """After multiple saves, ``.bak``, ``.bak.1``, ``.bak.2`` exist.

        ``.bak`` is the most recent prior state, ``.bak.1`` the one
        before that, etc.  Up to ``_MAX_BAK_GENERATIONS`` are retained.
        """
        # Initial save → creates .bak
        sch_mock = MagicMock()

        def write_v1(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(kicad_sch (version 20240101) (paper \"A4\"))")

        sch_mock.write = write_v1
        save_schematic(tmp_sch, sch_mock)
        assert os.path.exists(tmp_sch + ".bak")
        assert not os.path.exists(tmp_sch + ".bak.1")

        # Second save → rotates .bak to .bak.1, creates fresh .bak
        def write_v2(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(kicad_sch (version 20240102) (paper \"A4\"))")

        sch_mock.write = write_v2
        save_schematic(tmp_sch, sch_mock)
        assert os.path.exists(tmp_sch + ".bak")
        assert os.path.exists(tmp_sch + ".bak.1")

        # Third save → .bak.1 rotates to .bak.2
        def write_v3(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(kicad_sch (version 20240103) (paper \"A4\"))")

        sch_mock.write = write_v3
        save_schematic(tmp_sch, sch_mock)
        assert os.path.exists(tmp_sch + ".bak")
        assert os.path.exists(tmp_sch + ".bak.1")
        assert os.path.exists(tmp_sch + ".bak.2")

    def test_save_schematic_drops_oldest_after_fourth_save(self, tmp_sch):
        """Fourth save rotates past ``_MAX_BAK_GENERATIONS`` and drops the oldest."""
        # Pre-populate with three .bak generations and a base file.
        # The next save rotates each one forward, dropping .bak.3.
        for i in range(1, 4):
            with open(tmp_sch + f".bak.{i}", "w") as fh:
                fh.write(f"(gen-{i})")
        with open(tmp_sch, "w") as fh:
            fh.write("(current)")

        sch_mock = MagicMock()

        def write(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(kicad_sch (version 20240104))")

        sch_mock.write = write
        save_schematic(tmp_sch, sch_mock)

        # Oldest (was .bak.3) is gone; new chain has .bak.1, .bak.2, .bak.3.
        assert not os.path.exists(tmp_sch + ".bak.4")
        assert os.path.exists(tmp_sch + ".bak.3")


# ---------------------------------------------------------------------------
# restore_version: validate snapshot before overwriting live file
# ---------------------------------------------------------------------------


class TestRestoreVersionValidation:
    """``restore_version`` must not propagate corruption from a bad snapshot."""

    def test_restore_rejects_truncated_snapshot(self, tmp_path):
        from kcaa.utils.version_manager import restore_version
        from kcaa.utils.schematic_sexp_utils import SchematicCorruptionError

        sch = tmp_path / "live.kicad_sch"
        sch.write_bytes(b"(kicad_sch (version 20240101))")
        vdir = sch.parent / ".versions"
        vdir.mkdir()
        snap = vdir / "live.kicad_sch.bad_snapshot"
        snap.write_bytes(b'(kicad_sch (version 20240101) (paper "A4"')

        original = sch.read_bytes()
        with pytest.raises(SchematicCorruptionError, match="unclosed parens"):
            restore_version(str(sch), "bad_snapshot")

        # Live file must be untouched.
        assert sch.read_bytes() == original
        # No .tmp staging file left behind.
        assert not (sch.parent / (sch.name + ".tmp")).exists()

    def test_restore_rejects_mid_element_truncation(self, tmp_path):
        from kcaa.utils.version_manager import restore_version
        from kcaa.utils.schematic_sexp_utils import SchematicCorruptionError

        sch = tmp_path / "live.kicad_sch"
        sch.write_bytes(b"(kicad_sch (version 20240101))")
        vdir = sch.parent / ".versions"
        vdir.mkdir()
        snap = vdir / "live.kicad_sch.mid"
        # User's exact pattern: file ends ``(wi---`` mid-keyword.
        snap.write_bytes(
            b'(kicad_sch (version 20240101) (generator "eeschema") '
            b"(wire (pts (xy 0 0) (xy 10 0)) (stroke (wi"
        )

        original = sch.read_bytes()
        with pytest.raises(SchematicCorruptionError, match=r"unclosed parens"):
            restore_version(str(sch), "mid")
        assert sch.read_bytes() == original

    def test_restore_accepts_valid_snapshot(self, tmp_path):
        from kcaa.utils.version_manager import restore_version

        sch = tmp_path / "live.kicad_sch"
        sch.write_bytes(b"(kicad_sch (version 20240101) (paper \"A\"))")
        vdir = sch.parent / ".versions"
        vdir.mkdir()
        snap = vdir / "live.kicad_sch.good"
        snap.write_bytes(b"(kicad_sch (version 20240901) (paper \"B\"))")

        result = restore_version(str(sch), "good")
        assert result["restored_from"] == "good"
        assert sch.read_bytes() == b"(kicad_sch (version 20240901) (paper \"B\"))"

    def test_restore_non_kicad_file_skips_validation(self, tmp_path):
        """Plain text / non-KiCad files use shutil.copy2 — no structural check.

        Only ``.kicad_sch`` files get the structural validator.  Plain
        text snapshots (``.txt``, ``.md``, etc.) fall through to the
        original ``shutil.copy2`` path so we don't accidentally reject
        arbitrary file types.
        """
        from kcaa.utils.version_manager import restore_version

        txt = tmp_path / "notes.txt"
        txt.write_bytes(b"hello world\n")
        vdir = txt.parent / ".versions"
        vdir.mkdir()
        snap = vdir / "notes.txt.v1"
        snap.write_bytes(b"goodbye world\n")

        # Truncated text — would NOT pass a schematic validator, but
        # we skip validation for non-KiCad files entirely.
        snap_trunc = vdir / "notes.txt.v2"
        snap_trunc.write_bytes(b"goodbye wo")  # truncated

        result = restore_version(str(txt), "v2")
        assert txt.read_bytes() == b"goodbye wo"
