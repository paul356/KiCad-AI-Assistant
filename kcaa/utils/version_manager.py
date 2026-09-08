"""Version snapshot management for KiCad schematic and PCB files.

Per-file snapshots are stored in a ``.versions/`` subdirectory adjacent to
the file; at most MAX_VERSIONS snapshots are kept per file, oldest pruned
first.

Project-level snapshots bundle the project's schematic + PCB + ``.kicad_pro``
into a single ``.tar.gz`` archive under ``.versions/project/`` (see
:func:`save_project_version`), so the whole file set can be restored
together as one consistent unit.
"""

import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
import shutil
import tarfile
from typing import Any

MAX_VERSIONS = 10
_VERSIONS_DIR = ".versions"
MAX_PROJECT_VERSIONS = 10
_PROJECT_VERSIONS_DIR = "project"


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

    :param file_path: Absolute path to the file to restore.
    :param version_id: The ``id`` value returned by :func:`list_versions`.
    :returns: Dict with keys ``restored_from`` (version_id) and
              ``backup_of_current`` (path of the snapshot taken before restore).
    :raises FileNotFoundError: If *file_path* or the requested snapshot does not exist.
    :raises OSError: If the copy fails.
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

    # Snapshot current state first so the restore is undoable
    backup_path = save_version_snapshot(file_path)

    shutil.copy2(snapshot_path, file_path)
    return {"restored_from": version_id, "backup_of_current": backup_path}


def project_file_set(project_file: str) -> dict[str, str]:
    """Return the default project file set as ``{relative_path: absolute_path}``.

    Follows the standard KiCad convention: the ``.kicad_pro``, the same-stem
    root ``.kicad_sch`` and the same-stem ``.kicad_pcb`` live side by side.
    Only files that currently exist are included, so a schematic-only
    project archives just the sch + pro pair.
    """
    pro = os.path.abspath(project_file)
    d = os.path.dirname(pro)
    stem = os.path.splitext(os.path.basename(pro))[0]
    files: dict[str, str] = {}
    for rel in (os.path.basename(pro), f"{stem}.kicad_sch", f"{stem}.kicad_pcb"):
        path = os.path.join(d, rel)
        if os.path.isfile(path):
            files[rel] = path
    return files


def _project_versions_dir(project_file: str) -> str:
    """Return the project-archive directory for *project_file*."""
    return os.path.join(
        os.path.dirname(os.path.abspath(project_file)), _VERSIONS_DIR, _PROJECT_VERSIONS_DIR
    )


def _archive_prefix(project_file: str) -> str:
    """Return the archive filename prefix: ``<stem>.project``."""
    stem = os.path.splitext(os.path.basename(project_file))[0]
    return f"{stem}.project"


def _list_project_archive_paths(project_file: str) -> list[str]:
    """Return all project archive paths for *project_file*, sorted oldest-first."""
    vdir = _project_versions_dir(project_file)
    if not os.path.isdir(vdir):
        return []
    prefix = _archive_prefix(project_file) + "."
    entries = [
        os.path.join(vdir, name)
        for name in os.listdir(vdir)
        if name.startswith(prefix) and name.endswith(".tar.gz")
    ]
    return sorted(entries)


def _read_project_manifest(archive_path: str) -> dict[str, Any] | None:
    """Return the manifest dict embedded in a project archive, or None."""
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            member = tar.extractfile(".manifest.json")
            if member is None:
                return None
            return json.loads(member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, json.JSONDecodeError):
        return None


def _write_project_archive(project_file: str, timestamp: str) -> str:
    """Pack the project file set into a ``.tar.gz`` archive and return its path."""
    files = project_file_set(project_file)
    manifest = {
        "version": 1,
        "created_at": timestamp,
        "files": {rel: _file_hash(path) for rel, path in sorted(files.items())},
    }
    vdir = _project_versions_dir(project_file)
    os.makedirs(vdir, exist_ok=True)
    archive_path = os.path.join(vdir, f"{_archive_prefix(project_file)}.{timestamp}.tar.gz")

    manifest_data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    manifest_info = tarfile.TarInfo(name=".manifest.json")
    manifest_info.size = len(manifest_data)

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.addfile(manifest_info, io.BytesIO(manifest_data))
        for rel, path in sorted(files.items()):
            tar.add(path, arcname=rel)

    return archive_path


def project_version_id(archive_path: str, project_file: str) -> str:
    """Return the version id (timestamp) of a project archive path."""
    prefix = _archive_prefix(project_file) + "."
    name = os.path.basename(archive_path)
    return name[len(prefix) : -len(".tar.gz")]


def save_project_version(project_file: str, keep: int | None = None) -> str:
    """Save a version archive bundling the project's schematic + PCB + .kicad_pro.

    The bundle is a single ``.tar.gz`` under ``<project_dir>/.versions/
    project/`` named ``<stem>.project.<timestamp>.tar.gz``, so every file in
    the set shares one version id and can be restored together (see
    :func:`restore_project_version`).  If the current file set is identical
    to the latest archive, no new archive is created and the existing one is
    returned unchanged.  At most *keep* archives are retained (default
    MAX_PROJECT_VERSIONS); oldest are pruned first.

    :param project_file: Absolute path to the project file (.kicad_pro).
    :param keep: Number of archives to retain, or None for the default.
    :returns: Path of the (new or existing) archive.
    :raises FileNotFoundError: If *project_file* or the whole file set is missing.
    :raises OSError: If the archive directory cannot be created or the write fails.
    """
    project_file = os.path.abspath(project_file)
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"File not found: {project_file}")
    if not project_file_set(project_file):
        raise FileNotFoundError(f"No project files found for: {project_file}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    current_hashes = {
        rel: _file_hash(path) for rel, path in sorted(project_file_set(project_file).items())
    }

    existing = _list_project_archive_paths(project_file)
    if existing:
        latest_manifest = _read_project_manifest(existing[-1])
        if latest_manifest and latest_manifest.get("files") == current_hashes:
            return existing[-1]

    archive_path = _write_project_archive(project_file, timestamp)

    limit = MAX_PROJECT_VERSIONS if keep is None else keep
    if limit > 0:
        all_archives = _list_project_archive_paths(project_file)
        for old in all_archives[:-limit]:
            with contextlib.suppress(OSError):
                os.remove(old)

    return archive_path


def list_project_versions(project_file: str) -> list[dict[str, Any]]:
    """Return version metadata for a project, sorted newest-first.

    :param project_file: Absolute path to the project file (.kicad_pro).
    :returns: List of dicts with keys ``id``, ``timestamp``, ``size_bytes``
              and ``files`` (relative paths archived in that version).
    """
    project_file = os.path.abspath(project_file)
    result = []
    for path in reversed(_list_project_archive_paths(project_file)):
        version_id = project_version_id(path, project_file)
        try:
            dt = datetime.strptime(version_id, "%Y%m%d_%H%M%S_%f")
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts_str = version_id
        manifest = _read_project_manifest(path)
        files = sorted(manifest.get("files", {})) if manifest else []
        result.append(
            {
                "id": version_id,
                "timestamp": ts_str,
                "size_bytes": os.path.getsize(path),
                "files": files,
            }
        )
    return result


def restore_project_version(project_file: str, version_id: str) -> dict[str, Any]:
    """Restore the project files to a previously saved project version.

    The archive's file set (schematic, PCB, project file) is extracted over
    the current files, so a restore returns the whole project to one
    consistent state.  Before overwriting, the current file set is itself
    archived (so the restore is undoable via the returned backup id).

    :param project_file: Absolute path to the project file (.kicad_pro).
    :param version_id: The ``id`` value returned by :func:`list_project_versions`.
    :returns: Dict with keys ``restored_from`` (version_id),
              ``backup_of_current`` (archive path taken before restore),
              ``backup_version_id`` and ``files`` (relative paths restored).
    :raises FileNotFoundError: If *project_file* or the requested archive does not exist.
    :raises OSError: If the extraction fails.
    """
    project_file = os.path.abspath(project_file)
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"File not found: {project_file}")

    vdir = _project_versions_dir(project_file)
    archive_path = os.path.join(vdir, f"{_archive_prefix(project_file)}.{version_id}.tar.gz")
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(
            f"Version '{version_id}' not found for project {os.path.basename(project_file)!r}. "
            f"Use list_project_versions() to see available versions."
        )

    # Snapshot the current state first so the restore is undoable
    backup_path = save_project_version(project_file)
    backup_version_id = project_version_id(backup_path, project_file)

    restored: list[str] = []
    project_dir = os.path.dirname(project_file)
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == ".manifest.json" or not member.isfile():
                continue
            if os.path.isabs(member.name) or ".." in os.path.normpath(member.name).split(os.sep):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            target = os.path.join(project_dir, member.name)
            with open(target, "wb") as out:
                shutil.copyfileobj(fh, out)
            restored.append(member.name)

    return {
        "restored_from": version_id,
        "backup_of_current": backup_path,
        "backup_version_id": backup_version_id,
        "files": sorted(restored),
    }
