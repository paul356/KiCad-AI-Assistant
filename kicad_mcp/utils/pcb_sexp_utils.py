"""
Low-level S-expression I/O for KiCad PCB files (.kicad_pcb).

Only concerns: load, save, and backup.  No domain knowledge about
footprints, nets, or layers lives here.
"""
import os
import re
import shutil
from typing import Any, List

import sexpdata

# Top-level PCB elements that should start on their own line for readability.
_NEWLINE_ELEMENTS = [
    "footprint", "net", "segment", "via", "zone", "gr_line", "gr_arc",
    "gr_text", "gr_rect", "gr_circle", "gr_curve", "arc", "generated",
    "embedded_fonts", "layers", "setup", "general",
]


def load_pcb(path: str) -> List[Any]:
    """Parse a .kicad_pcb file and return its S-expression tree.

    :param path: Absolute path to the .kicad_pcb file.
    :returns: A list representing the parsed S-expression tree.
    :raises FileNotFoundError: If path does not exist.
    :raises ValueError: If the file cannot be parsed.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        return sexpdata.loads(raw)
    except Exception as exc:
        raise ValueError(f"Failed to parse PCB file '{path}': {exc}") from exc


def save_pcb(path: str, data: List[Any]) -> str:
    """Write *data* back to *path*, creating a .bak backup first.

    Uses an atomic write: serializes to a .tmp sibling file then
    renames it over the target so a crash or disk-full error never
    leaves the PCB file partially written or empty.

    :param path: Absolute path to the .kicad_pcb file to overwrite.
    :param data: The (possibly mutated) S-expression tree.
    :returns: Path to the backup file that was created.
    :raises OSError: If the backup copy or the atomic rename fails.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"
    shutil.copy2(path, bak_path)

    text = _serialize(data)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp_path, path)

    return bak_path


def _serialize(data: List[Any]) -> str:
    """Convert an S-expression tree to a KiCad-compatible string.

    Uses sexpdata.dumps for the base serialisation, then inserts newlines
    before top-level PCB elements so the file remains readable (same
    technique used by skip's writeTree).
    """
    text = sexpdata.dumps(data)
    for elname in _NEWLINE_ELEMENTS:
        text = re.sub(r"\(\s*" + elname + r"\b", f"\n({elname}", text)
    return text
