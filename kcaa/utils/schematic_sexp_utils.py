"""Low-level I/O helpers for KiCad schematic files (.kicad_sch).

Only concerns: load, save, and backup.  No domain knowledge about symbols,
wires, or nets lives here.
"""

import os
import shutil
from typing import Any

import sexpdata

from kcaa.utils.skip_compat import utf8_open_context

# Maximum number of .bak generations retained alongside each schematic.
# On every save: .bak -> .bak.1, .bak.1 -> .bak.2, ..., the oldest is
# discarded.  The most recent known-good copy is always at ``.bak``;
# older copies at ``.bak.1`` .. ``.bak.N`` let us recover from
# corruption that snuck past validation (e.g. KiCad autosave racing
# with our os.replace).
_MAX_BAK_GENERATIONS = 3


class SchematicCorruptionError(Exception):
    """Raised when a schematic file fails structural validation.

    The original file at *path* is left untouched; only the ``.tmp``
    staging file is removed.  Callers can recover by reading any of the
    ``.bak`` siblings (``.bak`` is the most recent known-good copy).
    """


def load_schematic(path: str) -> list[Any]:
    """Parse a .kicad_sch file and return its S-expression tree.

    Mirrors :func:`kcaa.utils.pcb_sexp_utils.load_pcb` so both schematic
    and PCB readers share the same parsing convention.

    :param path: Absolute path to the .kicad_sch file.
    :returns: A list representing the parsed S-expression tree.
    :raises FileNotFoundError: If path does not exist.
    :raises ValueError: If the file cannot be parsed.
    """
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        return sexpdata.loads(raw)
    except Exception as exc:
        raise ValueError(f"Failed to parse schematic file '{path}': {exc}") from exc


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _scan_paren_depth(text: str) -> tuple[int, str | None]:
    """Walk *text* tracking paren depth; return ``(final_depth, error_msg)``.

    Properly skips string literals and Lisp-style comments so depth isn't
    affected by ``(`` or ``)`` appearing inside them.  This catches
    asymmetric truncation that a naive ``count("(") == count(")")``
    misses: dropping a single ``(`` without its matching ``)`` produces
    unbalanced counts *and* a non-zero depth at EOF.

    The simple count check still passes when the dropped content was
    itself a balanced sub-tree (symmetric truncation); that's caught
    separately by the structural shape check in
    :func:`_validate_schematic_file`.
    """
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Skip the string literal (handle backslash escapes).
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            continue
        if c == ";":
            # Lisp comment — skip until newline.
            i += 1
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return depth, f"unexpected ')' at offset {i}"
        i += 1
    return depth, None


def _validate_schematic_text(text: str, source: str) -> None:
    """Validate the schematic S-expression in *text*.

    Four checks, in order:

    1. Non-empty.
    2. Depth-tracking scan — depth must be 0 at EOF (no unclosed
       parens), never negative (no stray closing parens).
    3. Parses cleanly as a sexpdata tree, with the root being
       ``(kicad_sch ...)`` — catches any file whose S-expression
       structure is broken even when the bracket count happens to match.
    4. The file ends with ``)`` — catches truncation that leaves the
       file ending mid-element like ``(wi---`` from a half-written
       ``(wire ...)`` entry.

    *source* is a label for error messages — usually a file path or
    ``"<bytes>"`` for in-memory validation.
    """
    if not text.strip():
        raise SchematicCorruptionError(f"{source}: empty")

    final_depth, scan_err = _scan_paren_depth(text)
    if scan_err:
        raise SchematicCorruptionError(
            f"{source}: malformed nesting: {scan_err}"
        )
    if final_depth != 0:
        raise SchematicCorruptionError(
            f"{source}: unclosed parens at end of file (depth = {final_depth})"
        )

    try:
        tree = sexpdata.loads(text)
    except Exception as exc:
        raise SchematicCorruptionError(
            f"{source}: failed to parse as an S-expression: {exc}"
        ) from exc

    if not isinstance(tree, list) or len(tree) == 0:
        raise SchematicCorruptionError(
            f"{source}: top-level should be a non-empty list "
            f"(the contents of the outer (kicad_sch …)), got "
            f"{type(tree).__name__ if not isinstance(tree, list) else 'empty list'}"
        )
    # sexpdata.loads("(kicad_sch …)") returns a flat list whose first
    # element is the Symbol ``kicad_sch`` and whose remaining elements
    # are the file's child entries.  So the check is on tree[0]
    # directly, not on tree[0][0].
    head = tree[0]
    if not (
        isinstance(head, (sexpdata.Symbol, str))
        and str(head) == "kicad_sch"
    ):
        head_repr = str(head) if isinstance(head, (sexpdata.Symbol, str)) else type(head).__name__
        raise SchematicCorruptionError(
            f"{source}: top-level should be (kicad_sch …), got "
            f"({head_repr} …)"
        )

    stripped = text.rstrip()
    if not stripped.endswith(")"):
        tail = stripped[-40:] if len(stripped) >= 40 else stripped
        raise SchematicCorruptionError(
            f"{source}: does not end with ')' "
            f"(likely truncated mid-element). Tail: {tail!r}"
        )


def _validate_schematic_file(path: str) -> None:
    """Validate the schematic file at *path* (post-write check)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SchematicCorruptionError(
            f"Cannot read written file {path!r}: {exc}"
        ) from exc
    _validate_schematic_text(text, source=str(path))


def check_schematic_integrity(path: str) -> None:
    """Raise :class:`SchematicCorruptionError` if *path* is not a valid schematic.

    Read-side equivalent of the post-write validator.  Use this before
    handing a file to skip's lenient parser — skip happily parses
    truncated files by ignoring trailing content, which used to let
    downstream tools silently "succeed" against a half-corrupt
    schematic.  Catching the imbalance here surfaces a clear error.

    :param path: Path to the ``.kicad_sch`` file to validate.
    :raises FileNotFoundError: If *path* does not exist.
    :raises SchematicCorruptionError: If any check fails.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Schematic file not found: {path!r}")
    _validate_schematic_file(path)


def validate_schematic_bytes(data: bytes, source: str = "<bytes>") -> None:
    """Validate schematic bytes already in memory (for ``restore_version`` etc.).

    Same checks as :func:`_validate_schematic_file` but operates on raw
    bytes — useful when the caller already has the snapshot contents
    and doesn't want to write-then-re-read from disk.

    :param data: Raw bytes of the schematic file.
    :param source: Label included in error messages (defaults to
        ``"<bytes>"``; pass the snapshot path for diagnostics).
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchematicCorruptionError(
            f"{source}: cannot decode as UTF-8: {exc}"
        ) from exc
    _validate_schematic_text(text, source=source)


# ---------------------------------------------------------------------------
# Save with multi-generation .bak rotation
# ---------------------------------------------------------------------------


def _rotate_bak_files(path: str) -> None:
    """Rotate ``path``'s ``.bak`` chain so the next save creates a fresh ``.bak``.

    ``.bak`` -> ``.bak.1`` -> ``.bak.2`` -> ``.bak.3`` (drop oldest).
    Done *before* the new save so each ``.bak.N`` represents a state
    from a prior save, not a snapshot of the file being overwritten.
    """
    # Drop the oldest generation.
    oldest = f"{path}.bak.{_MAX_BAK_GENERATIONS}"
    if os.path.exists(oldest):
        os.unlink(oldest)
    # Shift each .bak.N to .bak.(N+1), in reverse so we don't overwrite.
    for i in range(_MAX_BAK_GENERATIONS - 1, 0, -1):
        src = f"{path}.bak.{i}"
        dst = f"{path}.bak.{i + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    # .bak -> .bak.1
    bak = path + ".bak"
    if os.path.exists(bak):
        os.replace(bak, bak + ".1")


def save_schematic(path: str, sch) -> str:
    """Write *sch* back to *path*, creating a fresh ``.bak`` first.

    Layers of defense against the historical corruption bug:

    1. **Multi-generation .bak rotation** — the last
       ``_MAX_BAK_GENERATIONS`` previous states are kept as
       ``.bak``, ``.bak.1`` .. ``.bak.N``.  Corruption that sneaks
       past every check (e.g. KiCad autosave racing with our
       ``os.replace``) can still be undone manually.
    2. **UTF-8 write context** — ``skip``'s internal ``writeTree``
       calls ``open(path, 'w')`` with no encoding, so on non-UTF-8
       Windows locales (cp1252 / gbk) writes silently corrupt.  We
       wrap ``sch.write`` in :func:`utf8_open_context` to force UTF-8.
    3. **Structural validation of the ``.tmp``** before replacing the
       original — non-empty, depth-tracking nested-balance, parses as
       exactly one ``(kicad_sch ...)`` root, ends with ``)``.  On
       failure the original file stays untouched and
       :class:`SchematicCorruptionError` is raised.

    :param path: Absolute path to the .kicad_sch file to overwrite.
    :param sch: The (possibly mutated) skip.Schematic object.
    :returns: Path to the fresh ``.bak`` just written.
    :raises SchematicCorruptionError: If the written file fails validation.
    :raises OSError: If the backup copy or the atomic rename fails.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"

    # Rotate any existing .bak chain so the new .bak becomes the most
    # recent prior state.  Do this BEFORE copying the current state
    # so the rotation reflects "what existed before this save".
    _rotate_bak_files(path)

    # Create a fresh .bak from the current state.
    shutil.copy(path, bak_path)

    # Force UTF-8 around the write (skip's writeTree has no encoding).
    try:
        with utf8_open_context():
            sch.write(tmp_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    # Validate the .tmp BEFORE replacing the original.
    try:
        _validate_schematic_file(tmp_path)
    except SchematicCorruptionError:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    os.replace(tmp_path, path)
    return bak_path
