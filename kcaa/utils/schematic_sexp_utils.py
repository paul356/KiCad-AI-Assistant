"""Low-level I/O helpers for KiCad schematic files (.kicad_sch).

Only concerns: load, save, and backup.  No domain knowledge about symbols,
wires, or nets lives here.
"""

import os
import shutil
from typing import Any

import sexpdata

from kcaa.utils.skip_compat import utf8_open_context


class SchematicCorruptionError(Exception):
    """Raised when ``save_schematic`` detects a malformed written file.

    The original file at *path* is left untouched; only the ``.tmp``
    staging file is removed.  Callers can recover by reading the
    ``.bak`` sibling.
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


def save_schematic(path: str, sch) -> str:
    """Write *sch* back to *path*, creating a .bak backup first.

    Uses an atomic write: writes to a .tmp sibling file, validates the
    result, then renames over the target only if validation passes.
    A crash, disk-full error, or a corrupt serialization never leaves
    the schematic file partially written or empty.

    Post-write validation catches the silent-overwrite class of bugs
    where ``sch.write`` finishes without raising but produces a file
    that is missing trailing closing brackets or otherwise unparseable
    — historically this hid downstream failures (e.g. wire-addition
    tools failing mid-operation) because lenient readers accepted the
    truncated output.  When validation fails, the original *path* is
    left intact and a :class:`SchematicCorruptionError` is raised.
    Callers can recover by reading the ``.bak`` sibling.

    :param path: Absolute path to the .kicad_sch file to overwrite.
    :param sch: The (possibly mutated) skip.Schematic object.
    :returns: Path to the backup file that was created.
    :raises SchematicCorruptionError: If the written file fails validation.
    :raises OSError: If the backup copy or the atomic rename fails.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"
    shutil.copy(path, bak_path)

    # skip's writeTree opens the destination with ``open(path, 'w')`` —
    # no encoding argument.  On non-UTF-8 Windows locales (gbk/cp1252)
    # that silently corrupts or truncates the file.  Force UTF-8 around
    # the write so the read and write paths use the same encoding.
    try:
        with utf8_open_context():
            sch.write(tmp_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    try:
        _validate_schematic_file(tmp_path)
    except SchematicCorruptionError:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    os.replace(tmp_path, path)
    return bak_path


def _validate_schematic_file(path: str) -> None:
    """Validate a written schematic file.

    Three checks, in increasing strictness:

    1. Non-empty (skip's ``write`` has historically produced an empty
       file in some failure modes).
    2. Bracket balance — open-paren count must equal close-paren count.
       Catches trailing-bracket truncation directly.
    3. Re-parseable as an S-expression via sexpdata — catches nested
       structural corruption that a bracket count alone would miss.

    :param path: Path to the written file (typically the ``.tmp`` sibling).
    :raises SchematicCorruptionError: If any check fails.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SchematicCorruptionError(
            f"Cannot read written file {path!r}: {exc}"
        ) from exc

    if not text.strip():
        raise SchematicCorruptionError(
            f"Written schematic {path!r} is empty"
        )

    opens = text.count("(")
    closes = text.count(")")
    if opens != closes:
        raise SchematicCorruptionError(
            f"Written schematic {path!r} has unbalanced brackets: "
            f"{opens} open vs {closes} close "
            f"(diff = {opens - closes})"
        )

    try:
        sexpdata.loads(text)
    except Exception as exc:
        raise SchematicCorruptionError(
            f"Written schematic {path!r} failed to parse as an "
            f"S-expression: {exc}"
        ) from exc


def check_schematic_integrity(path: str) -> None:
    """Raise :class:`SchematicCorruptionError` if *path* is not a valid schematic.

    Same three checks as the post-write validator (non-empty, balanced
    brackets, parseable as S-expression) but exposed publicly so callers
    that only need a read-side integrity check — e.g. ``extract_netlist``
    pre-flight before handing the file to skip — can reuse it without
    dragging in the write-path scaffolding.

    Without this check, ``skip`` happily parses truncated files by
    ignoring trailing content, which used to let downstream tools
    silently "succeed" against a half-corrupt schematic.  Catching the
    imbalance here surfaces a clear error to the LLM user.

    :param path: Path to the ``.kicad_sch`` file to validate.
    :raises FileNotFoundError: If *path* does not exist.
    :raises SchematicCorruptionError: If any check fails.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Schematic file not found: {path!r}")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SchematicCorruptionError(
            f"Cannot read schematic {path!r}: {exc}"
        ) from exc

    if not text.strip():
        raise SchematicCorruptionError(
            f"Schematic {path!r} is empty"
        )

    opens = text.count("(")
    closes = text.count(")")
    if opens != closes:
        raise SchematicCorruptionError(
            f"Schematic {path!r} has unbalanced brackets: "
            f"{opens} open vs {closes} close "
            f"(diff = {opens - closes}). "
            f"The file may be truncated or otherwise corrupted; "
            f"restore from {path}.bak if available."
        )

    try:
        sexpdata.loads(text)
    except Exception as exc:
        raise SchematicCorruptionError(
            f"Schematic {path!r} failed to parse as an S-expression: {exc}"
        ) from exc
