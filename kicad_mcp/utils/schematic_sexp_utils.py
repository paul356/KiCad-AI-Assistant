"""Low-level I/O helpers for KiCad schematic files (.kicad_sch).

Only concerns: save and backup.  No domain knowledge about symbols,
wires, or nets lives here.
"""

import os
import shutil


def save_schematic(path: str, sch) -> str:
    """Write *sch* back to *path*, creating a .bak backup first.

    Uses an atomic write: writes to a .tmp sibling file then renames it
    over the target so a crash or disk-full error never leaves the
    schematic file partially written or empty.

    :param path: Absolute path to the .kicad_sch file to overwrite.
    :param sch: The (possibly mutated) skip.Schematic object.
    :returns: Path to the backup file that was created.
    :raises OSError: If the backup copy or the atomic rename fails.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"
    shutil.copy(path, bak_path)
    sch.write(tmp_path)
    os.replace(tmp_path, path)
    return bak_path
