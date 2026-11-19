"""Low-level I/O helpers for KiCad schematic files (.kicad_sch).

Only concerns: save and backup.  No domain knowledge about symbols,
wires, or nets lives here.

Every write happens as UTF-8 into a ``.tmp`` sibling and is only moved over
the target once it is complete and synced, so a failed serialisation can
never truncate or corrupt the schematic.  ``skip`` opens the output file
without an encoding, hence the explicit :func:`utf8_open_context`.
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Any

from kcaa.utils.skip_compat import utf8_open_context


def _discard(path: str) -> None:
    """Remove *path* if present; cleanup must never mask the real error.

    On Windows a just-closed file can briefly reject ``unlink`` with
    ``PermissionError``; an equally failed cleanup must not replace the
    original exception that is already propagating.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def _preserve_mode(source: str, target: str) -> None:
    """Give *target* the permission bits of *source*, if source exists."""
    try:
        mode = stat.S_IMODE(os.stat(source).st_mode)
    except FileNotFoundError:
        return
    os.chmod(target, mode)


def write_schematic_text(path: str, text: str) -> None:
    """Atomically write *text* to *path* as UTF-8.

    Intended for schematic-adjacent text files created from scratch (for
    example a generated child sheet).  The text is flushed and fsynced into
    a ``.tmp`` sibling before the atomic rename, so a failure leaves no
    partial target behind.

    :param path: Destination file path.
    :param text: Complete file contents.
    :raises OSError: If writing, syncing, or renaming fails.
    """
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _preserve_mode(path, tmp_path)
        os.replace(tmp_path, path)
    except OSError:
        _discard(tmp_path)
        raise
    except Exception as exc:
        _discard(tmp_path)
        raise OSError(f"Failed to write schematic file {path!r}: {exc}") from exc


def save_schematic(path: str, sch: Any) -> str:
    """Write *sch* back to *path*, creating a .bak backup first.

    Uses an atomic write: writes UTF-8 to a .tmp sibling file, fsyncs it,
    then renames it over the target so a crash or disk-full error never
    leaves the schematic file partially written or empty.  If serialisation,
    the write, or the sync fails, the temporary file is removed and the
    original file keeps its exact bytes.

    :param path: Absolute path to the .kicad_sch file to overwrite.
    :param sch: The (possibly mutated) skip.Schematic object.
    :returns: Path to the backup file that was created.
    :raises OSError: If the backup copy, the write, the sync, or the atomic
        rename fails.  The original file is left untouched in every case.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"

    # A leftover .tmp from an earlier interrupted run must never be mistaken
    # for the output of this save.
    _discard(tmp_path)

    try:
        shutil.copy(path, bak_path)
    except OSError as exc:
        raise OSError(
            f"Failed to back up schematic {path!r} to {bak_path!r}; "
            f"the original file was not modified: {exc}"
        ) from exc

    try:
        # skip's SourceFile.write() serialises through a bare
        # open(fpath, "w"), which would otherwise use the locale encoding
        # (gbk on Chinese Windows).
        with utf8_open_context():
            sch.write(tmp_path)

        if not os.path.isfile(tmp_path):
            raise OSError(
                "serializer produced no output (skip's will_write() may have refused the path)"
            )
        if os.path.getsize(tmp_path) == 0:
            raise OSError("serializer produced an empty file")

        # A read-write handle keeps os.fsync() portable (Windows rejects it
        # on a read-only descriptor); no bytes are modified here.
        with open(tmp_path, "rb+") as fh:
            os.fsync(fh.fileno())

        # os.replace() keeps the temp file's mode, which is the process
        # default; carry the target's permissions over instead.
        _preserve_mode(path, tmp_path)
        os.replace(tmp_path, path)
    except OSError:
        _discard(tmp_path)
        raise
    except Exception as exc:
        _discard(tmp_path)
        raise OSError(
            f"Failed to write schematic {path!r}; the original file was left "
            f"unchanged (backup: {bak_path!r}): {exc}"
        ) from exc

    return bak_path
