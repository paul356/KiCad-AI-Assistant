"""Compatibility helpers for the skip library on Windows.

The skip library opens KiCad files without specifying an encoding parameter,
so Python falls back to the system default locale encoding.  On Windows the
default is typically ``gbk`` (CP936), which fails to decode KiCad's UTF-8
S-expression files.

This module provides a context manager that temporarily monkey-patches
``builtins.open`` so that text-mode opens without an explicit *encoding*
argument default to ``"utf-8"``.  The patch is deliberately limited:

* It only supplies ``encoding="utf-8"`` when the caller did NOT already pass
  an encoding — existing explicit encoding overrides are preserved.
* It does NOT touch ``sys.getfilesystemencoding()``, ``PYTHONUTF8``, or
  environment variables, so filesystem path handling remains unaffected.
* It restores the original ``builtins.open`` on exit, even if an exception
  occurs.

Usage::

    from kcaa.utils.skip_compat import safe_source_file, safe_schematic

    # Replace  skip.sexp.sourcefile.SourceFile(path)
    table = safe_source_file(path)

    # Replace  skip.Schematic(path)
    sch = safe_schematic(path)
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# Internal: temporary open() monkey-patch
# ---------------------------------------------------------------------------

@contextmanager
def _utf8_open_context():
    """Context manager that defaults text-mode opens to ``encoding="utf-8"``.

    Only affects calls that do **not** already pass an explicit ``encoding=``
    keyword; those are left unchanged.  Binary-mode opens (``"rb"``, ``"wb"``,
    etc.) are never touched.
    """
    original_open = builtins.open

    def _open(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener: Any = None,
    ):
        if encoding is None and "b" not in mode:
            encoding = "utf-8"
        return original_open(
            file, mode, buffering, encoding, errors, newline, closefd, opener
        )

    builtins.open = _open  # type: ignore[assignment]
    try:
        yield
    finally:
        builtins.open = original_open


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def safe_source_file(path: str):
    """Open a KiCad S-expression file with UTF-8 encoding via skip.

    Replacement for ``skip.sexp.sourcefile.SourceFile(path)`` that forces
    UTF-8 decoding on platforms where the default locale is not UTF-8
    (e.g. Windows with ``gbk`` / CP936).
    """
    import skip.sexp.sourcefile

    with _utf8_open_context():
        return skip.sexp.sourcefile.SourceFile(path)


def safe_schematic(path: str):
    """Open a KiCad schematic file with UTF-8 encoding via skip.

    Replacement for ``skip.Schematic(path)`` that forces UTF-8 decoding
    on platforms where the default locale is not UTF-8.
    """
    import skip

    with _utf8_open_context():
        return skip.Schematic(path)
