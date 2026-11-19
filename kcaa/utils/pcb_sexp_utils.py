"""
Low-level S-expression I/O for KiCad PCB files (.kicad_pcb).

Only concerns: load, save, and backup.  No domain knowledge about
footprints, nets, or layers lives here.
"""

import re
from typing import Any

import sexpdata

from kcaa.utils.atomic_io import atomic_write_text, read_text_utf8

# Top-level PCB elements that should start on their own line for readability.
_NEWLINE_ELEMENTS = [
    "footprint",
    "net",
    "segment",
    "via",
    "zone",
    "gr_line",
    "gr_arc",
    "gr_text",
    "gr_rect",
    "gr_circle",
    "gr_curve",
    "arc",
    "generated",
    "embedded_fonts",
    "layers",
    "setup",
    "general",
]


def load_pcb(path: str) -> list[Any]:
    """Parse a .kicad_pcb file and return its S-expression tree.

    :param path: Absolute path to the .kicad_pcb file.
    :returns: A list representing the parsed S-expression tree.
    :raises FileNotFoundError: If path does not exist.
    :raises ValueError: If the file is not valid UTF-8 or cannot be parsed.
    """
    raw = read_text_utf8(path)
    try:
        return sexpdata.loads(raw)
    except Exception as exc:
        raise ValueError(f"Failed to parse PCB file '{path}': {exc}") from exc


def save_pcb(path: str, data: list[Any]) -> str | None:
    """Write *data* back to *path*, creating a .bak backup first.

    Uses an atomic write: serializes to a .tmp sibling file then
    renames it over the target so a crash or disk-full error never
    leaves the PCB file partially written or empty.

    :param path: Absolute path to the .kicad_pcb file to overwrite.
    :param data: The (possibly mutated) S-expression tree.
    :returns: Path to the backup file, or ``None`` if the target
        did not exist when it was saved.
    :raises OSError: If the backup, write, sync, or atomic rename fails.
        The original file is left untouched in every case.
    """
    text = _serialize(data)
    return atomic_write_text(path, text, create_backup=True)


def _serialize(data: list[Any]) -> str:
    """Convert an S-expression tree to a KiCad-compatible string.

    Uses sexpdata.dumps for the base serialisation, then inserts newlines
    before top-level PCB elements so the file remains readable (same
    technique used by skip's writeTree).
    """
    text = sexpdata.dumps(data)
    for elname in _NEWLINE_ELEMENTS:
        text = re.sub(r"\(\s*" + elname + r"\b", f"\n({elname}", text)
    return text
