"""Atomic UTF-8 file writes and strict UTF-8 reads.

Shared low-level I/O for every KiCad text file KCAA rewrites.  All writes
go to a ``.tmp`` sibling, are flushed and fsynced, validated as non-empty,
and only then renamed over the target.  A failure removes the temporary
file and leaves the original bytes untouched.

KiCad files are treated as strictly UTF-8: reads use ``utf-8-sig`` (so a
BOM is tolerated) and raise a ``ValueError`` with recovery guidance on
decode errors.  There is deliberately no GBK fallback.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import shutil
import stat
from typing import Literal, overload

# A writer receives the temporary path and writes the complete file there.
Writer = Callable[[str], None]


def discard(path: str) -> None:
    """Remove *path* if present; cleanup must never mask the real error.

    On Windows a just-closed file can briefly reject ``unlink`` with
    ``PermissionError``, and a ``.tmp`` that inherited read-only permission
    bits is likewise undeletable.  Clear the write protection and retry;
    an equally failed cleanup must not replace the original exception that
    is already propagating.
    """
    try:
        os.unlink(path)
        return
    except OSError:
        pass
    try:
        os.chmod(path, 0o600)
        os.unlink(path)
    except OSError:
        pass


def preserve_mode(source: str, target: str) -> None:
    """Give *target* the permission bits of *source*, if source exists.

    Best effort: SMB/FUSE mounts and restricted ACLs can reject ``chmod``
    even on a file we just created, and that must not fail an otherwise
    complete save.
    """
    try:
        mode = stat.S_IMODE(os.stat(source).st_mode)
    except FileNotFoundError:
        return
    try:
        os.chmod(target, mode)
    except OSError:
        pass


def read_text_utf8(path: str) -> str:
    """Read a KiCad text file as strict UTF-8, tolerating a leading BOM.

    :param path: File to read.
    :returns: Decoded file contents.
    :raises ValueError: If the bytes are not valid UTF-8.  The message names
        the path and explains how to recover the file.
    :raises OSError: If the file cannot be opened.
    """
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path!r} is not valid UTF-8 (decode failed at byte {exc.start}: "
            f"{exc.reason}). The file may have been written as GBK by an "
            f"older KCAA version on Chinese Windows. Restore the previous "
            f"good copy from {path + '.bak'!r}, or convert the file "
            f"explicitly (for example: iconv -f GBK -t UTF-8), then retry."
        ) from exc


@overload
def atomic_write_text(path: str, text: str, *, create_backup: Literal[False] = False) -> None: ...


@overload
def atomic_write_text(path: str, text: str, *, create_backup: Literal[True]) -> str: ...


def atomic_write_text(path: str, text: str, *, create_backup: bool = False) -> str | None:
    """Write *text* to *path* as UTF-8, atomically.

    :param path: Destination file path.
    :param text: Complete file contents.
    :param create_backup: Copy the current file to ``<path>.bak`` first.
    :returns: The backup path when *create_backup* is true, else ``None``.
    :raises OSError: If writing, syncing, validating, or renaming fails.
    """

    def _write(tmp_path: str) -> None:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    if create_backup:
        return atomic_write_with(path, _write, create_backup=True)
    atomic_write_with(path, _write)
    return None


@overload
def atomic_write_with(
    path: str, writer: Writer, *, create_backup: Literal[False] = False
) -> None: ...


@overload
def atomic_write_with(path: str, writer: Writer, *, create_backup: Literal[True]) -> str: ...


def atomic_write_with(path: str, writer: Writer, *, create_backup: bool = False) -> str | None:
    """Atomically replace *path* with output produced by *writer*.

    The writer receives the temporary path; everything after it (fsync,
    emptiness check, permission preservation, rename, cleanup) is handled
    here.  Use this for libraries such as ``skip`` that open the output
    file themselves.

    ``OSError`` from the writer is re-raised unchanged so callers keep the
    errno and filename; other exceptions are wrapped in ``OSError`` with
    the target and backup paths for context.

    :param path: Destination file path.
    :param writer: Callable that writes the complete file to its argument.
    :param create_backup: Copy the current file to ``<path>.bak`` first.
        The backup is taken before the writer runs and its failure aborts
        the save without touching the target.
    :returns: The backup path when *create_backup* is true, else ``None``.
    :raises OSError: If backup, writing, syncing, validation, or renaming
        fails.
    """
    bak_path = path + ".bak"
    tmp_path = path + ".tmp"
    context = f" (backup: {bak_path!r})" if create_backup else ""

    # A leftover .tmp from an earlier interrupted run must never be mistaken
    # for the output of this save.
    discard(tmp_path)

    if create_backup:
        try:
            shutil.copy2(path, bak_path)
        except OSError as exc:
            raise OSError(
                f"Failed to back up {path!r} to {bak_path!r}; "
                f"the original file was not modified: {exc}"
            ) from exc

    try:
        writer(tmp_path)

        if not os.path.isfile(tmp_path):
            raise OSError(
                f"Failed to write {path!r}: serializer produced no output "
                f"(skip's will_write() may have refused the path); the "
                f"original file was left unchanged{context}"
            )
        if os.path.getsize(tmp_path) == 0:
            raise OSError(
                f"Failed to write {path!r}: serializer produced an empty "
                f"file; the original file was left unchanged{context}"
            )

        # A read-write handle keeps os.fsync() portable (Windows rejects it
        # on a read-only descriptor); no bytes are modified here.
        with open(tmp_path, "rb+") as fh:
            os.fsync(fh.fileno())

        # os.replace() keeps the temp file's mode, which is the process
        # default; carry the target's permissions over instead.
        preserve_mode(path, tmp_path)
        os.replace(tmp_path, path)
    except OSError:
        discard(tmp_path)
        raise
    except Exception as exc:
        discard(tmp_path)
        raise OSError(
            f"Failed to write {path!r}; the original file was left unchanged{context}: {exc}"
        ) from exc

    return bak_path if create_backup else None
