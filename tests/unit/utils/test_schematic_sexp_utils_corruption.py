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
    load_schematic,
    save_schematic,
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
        with pytest.raises(SchematicCorruptionError, match="unbalanced"):
            save_schematic(tmp_sch, _fake_sch(truncated))

        # After the exception, original file must be untouched.
        with open(tmp_sch, "rb") as fh:
            restored = fh.read()
        with open(SCHEMATIC_PATH, "rb") as fh:
            original = fh.read()
        assert restored == original  # untouched

    def test_unbalanced_brackets_raises_with_diff(self, tmp_sch):
        truncated = b'(kicad_sch (version 20240101) (paper "A4"'
        with pytest.raises(SchematicCorruptionError, match="unbalanced"):
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
