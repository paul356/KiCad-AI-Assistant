"""Low-level I/O helpers for KiCad schematic files (.kicad_sch).

Only concerns: save and backup.  No domain knowledge about symbols,
wires, or nets lives here.

The actual write mechanics live in :mod:`kcaa.utils.atomic_io`; schematic
serialisation goes through ``skip``, which opens its output file itself,
so it uses :func:`atomic_write_with` plus the UTF-8 open context.
"""

from __future__ import annotations

from typing import Any

from kcaa.utils.atomic_io import atomic_write_text, atomic_write_with
from kcaa.utils.skip_compat import utf8_open_context


def write_schematic_text(path: str, text: str) -> None:
    """Atomically write *text* to *path* as UTF-8.

    Intended for schematic-adjacent text files created from scratch (for
    example a generated child sheet).

    :param path: Destination file path.
    :param text: Complete file contents.
    :raises OSError: If writing, syncing, validating, or renaming fails.
    """
    atomic_write_text(path, text)


def save_schematic(path: str, sch: Any) -> str:
    """Write *sch* back to *path*, creating a .bak backup first.

    The serialisation runs inside :func:`utf8_open_context` because skip's
    ``SourceFile.write()`` uses a bare ``open(fpath, "w")``.  The staged
    output is atomically renamed over the target; on any failure the
    temporary file is removed and the original file keeps its exact bytes.

    :param path: Absolute path to the .kicad_sch file to overwrite.
    :param sch: The (possibly mutated) skip.Schematic object.
    :returns: Path to the backup file that was created.
    :raises OSError: If the backup, write, sync, or atomic rename fails.
        The original file is left untouched in every case.
    """

    def _serialize(tmp_path: str) -> None:
        # skip's SourceFile.write() serialises through a bare
        # open(fpath, "w"), which would otherwise use the locale encoding
        # (gbk on Chinese Windows).
        with utf8_open_context():
            sch.write(tmp_path)

    return atomic_write_with(path, _serialize, create_backup=True)
