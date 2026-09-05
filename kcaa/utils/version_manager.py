"""Version snapshot management for KiCad schematic and PCB files.

Snapshots are stored in a .versions/ subdirectory adjacent to the file.
At most MAX_VERSIONS snapshots are kept per file; oldest are pruned first.
"""

import contextlib
from datetime import datetime
import hashlib
import os
import shutil
from typing import Any

MAX_VERSIONS = 10
_VERSIONS_DIR = ".versions"


def _versions_dir(file_path: str) -> str:
    """Return the path to the .versions directory for *file_path*."""
    return os.path.join(os.path.dirname(os.path.abspath(file_path)), _VERSIONS_DIR)


def _file_hash(path: str) -> str:
    """Return the SHA-256 hex digest of the file at *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _list_snapshot_paths(file_path: str) -> list[str]:
    """Return all snapshot paths for *file_path*, sorted oldest-first."""
    vdir = _versions_dir(file_path)
    if not os.path.isdir(vdir):
        return []
    basename = os.path.basename(file_path)
    prefix = basename + "."
    entries = [os.path.join(vdir, name) for name in os.listdir(vdir) if name.startswith(prefix)]
    return sorted(entries)


def save_version_snapshot(file_path: str) -> str:
    """Save a snapshot of *file_path* unless it is identical to the latest one.

    Compares the current file content against the most recent snapshot using
    SHA-256.  If they match, no new snapshot is created and the existing
    snapshot path is returned unchanged.

    Snapshots are stored in ``<file_dir>/.versions/<basename>.<timestamp>``
    where timestamp is ``YYYYMMDD_HHMMSS_ffffff``.  At most MAX_VERSIONS
    snapshots are retained; the oldest are deleted when the limit is exceeded.

    :param file_path: Absolute path to the file to snapshot.
    :returns: Path of the (new or existing) snapshot.
    :raises FileNotFoundError: If *file_path* does not exist.
    :raises OSError: If the snapshot directory cannot be created or the copy fails.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    existing = _list_snapshot_paths(file_path)
    if existing:
        latest = existing[-1]
        if _file_hash(file_path) == _file_hash(latest):
            return latest

    vdir = _versions_dir(file_path)
    os.makedirs(vdir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    basename = os.path.basename(file_path)
    snapshot_path = os.path.join(vdir, f"{basename}.{timestamp}")
    shutil.copy2(file_path, snapshot_path)

    # Prune oldest snapshots beyond the limit
    all_snapshots = _list_snapshot_paths(file_path)
    for old in all_snapshots[:-MAX_VERSIONS]:
        with contextlib.suppress(OSError):
            os.remove(old)

    return snapshot_path


def list_versions(file_path: str) -> list[dict[str, Any]]:
    """Return version metadata for *file_path*, sorted newest-first.

    :param file_path: Absolute path to the file whose versions to list.
    :returns: List of dicts, each with keys ``id``, ``timestamp``, ``size_bytes``.
              ``id`` is the timestamp suffix used in the snapshot filename and
              can be passed directly to :func:`restore_version`.
    """
    file_path = os.path.abspath(file_path)
    snapshots = _list_snapshot_paths(file_path)
    result = []
    basename = os.path.basename(file_path)
    prefix = basename + "."
    for path in reversed(snapshots):
        version_id = os.path.basename(path)[len(prefix) :]
        # Parse timestamp: YYYYMMDD_HHMMSS_ffffff → human-readable
        try:
            dt = datetime.strptime(version_id, "%Y%m%d_%H%M%S_%f")
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts_str = version_id
        size = os.path.getsize(path)
        result.append({"id": version_id, "timestamp": ts_str, "size_bytes": size})
    return result


def restore_version(file_path: str, version_id: str) -> dict[str, Any]:
    """Restore *file_path* to the snapshot identified by *version_id*.

    Before overwriting, the current file is itself snapshotted (so the
    restore is undoable via another call to this function or via the
    snapshot just created).

    The snapshot is validated structurally before it overwrites the
    live file: an empty, truncated, or otherwise malformed snapshot
    raises :class:`kcaa.utils.schematic_sexp_utils.SchematicCorruptionError`
    and leaves the live file untouched.  This guards against restoring
    a snapshot that was taken from a corrupt source — historically the
    destination would silently inherit the corruption because
    :func:`shutil.copy2` does no integrity check.

    The restore itself is atomic: the snapshot bytes are written to a
    ``.tmp`` sibling, validated again, and only then ``os.replace``d
    over the live file.  If anything between the snapshot read and the
    final replace fails, the original is left intact.

    :param file_path: Absolute path to the file to restore.
    :param version_id: The ``id`` value returned by :func:`list_versions`.
    :returns: Dict with keys ``restored_from`` (version_id) and
              ``backup_of_current`` (path of the snapshot taken before restore).
    :raises FileNotFoundError: If *file_path* or the requested snapshot does not exist.
    :raises kcaa.utils.schematic_sexp_utils.SchematicCorruptionError: If the snapshot is malformed.
    :raises OSError: If the copy or rename fails.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    basename = os.path.basename(file_path)
    vdir = _versions_dir(file_path)
    snapshot_path = os.path.join(vdir, f"{basename}.{version_id}")
    if not os.path.isfile(snapshot_path):
        raise FileNotFoundError(
            f"Version '{version_id}' not found for {basename!r}. "
            f"Use list_versions() to see available versions."
        )

    # Snapshot current state first so the restore is undoable.
    backup_path = save_version_snapshot(file_path)

    # Read + validate the snapshot bytes BEFORE writing anything.
    # _schematic_extensions holds the KiCad file extensions we validate
    # structurally; everything else (text files, etc.) falls through
    # to a plain copy.
    from kcaa.utils.schematic_sexp_utils import validate_schematic_bytes

    if file_path.endswith(".kicad_sch"):
        with open(snapshot_path, "rb") as fh:
            snapshot_bytes = fh.read()
        validate_schematic_bytes(snapshot_bytes, source=snapshot_path)

        # Atomic write: .tmp + os.replace so a mid-write failure can't
        # leave the live file half-overwritten.
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(snapshot_bytes)
        # Re-validate the .tmp (defense-in-depth — bit-rot in the tmp fs).
        with open(tmp_path, "rb") as fh:
            tmp_bytes = fh.read()
        validate_schematic_bytes(tmp_bytes, source=tmp_path)
        os.replace(tmp_path, file_path)
    else:
        shutil.copy2(snapshot_path, file_path)

    return {"restored_from": version_id, "backup_of_current": backup_path}
