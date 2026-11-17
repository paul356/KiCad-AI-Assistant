"""Version management for KiCad projects.

Project snapshots bundle the whole project file set — the ``.kicad_pro``,
every schematic and every PCB, including nested sub-sheets — into a single
``.tar.gz`` archive under ``.versions/project/`` (see
:func:`save_project_version`), so the whole file set shares one version id
and can be restored together as one consistent unit.
"""

import contextlib
from datetime import datetime
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
from typing import Any

log = logging.getLogger(__name__)

_VERSIONS_DIR = ".versions"
MAX_PROJECT_VERSIONS = 10
_PROJECT_VERSIONS_DIR = "project"


def _file_hash(path: str) -> str:
    """Return the SHA-256 hex digest of the file at *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def project_relative_path(project_file: str, path: str) -> str:
    """Return *path* relative to the project directory, using ``/`` separators.

    Both paths are resolved with :func:`os.path.realpath` before the
    containment check, so ``..`` traversal and symlinks that point outside
    the project are rejected.  The entry must be an existing file.

    :raises ValueError: If *path* is not a file or resolves outside the
        project directory; the message names the offending path.
    """
    project_root = os.path.realpath(os.path.dirname(os.path.abspath(project_file)))
    target = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(target):
        raise ValueError(f"include_files entry is not an existing file: {path}")
    rel = os.path.relpath(target, project_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise ValueError(f"include_files entry is outside the project directory: {path}")
    return rel.replace(os.sep, "/")


def project_file_set(project_file: str, extra_files: list[str] | None = None) -> dict[str, str]:
    """Return the project file set as ``{relative_path: absolute_path}``.

    The default set follows the standard KiCad convention: the
    ``.kicad_pro``, the same-stem root ``.kicad_sch`` and the same-stem
    ``.kicad_pcb`` live side by side.  Only files that currently exist are
    included, so a schematic-only project archives just the sch + pro pair.

    *extra_files* adds explicit files (for example every sheet and board
    listed by the project state), archived under their project-relative
    paths in the given order.  Entries that are missing, not files, or
    resolve outside the project directory are skipped with a debug log
    rather than failing the whole snapshot; entries already in the default
    set are skipped silently.  The returned mapping is de-duplicated by
    relative path and keeps insertion order, so default files come first
    and follow the input order after that.
    """
    pro = os.path.abspath(project_file)
    d = os.path.dirname(pro)
    stem = os.path.splitext(os.path.basename(pro))[0]
    files: dict[str, str] = {}
    for rel in (os.path.basename(pro), f"{stem}.kicad_sch", f"{stem}.kicad_pcb"):
        path = os.path.join(d, rel)
        if os.path.isfile(path):
            files[rel] = path
    for path in extra_files or ():
        try:
            rel = project_relative_path(pro, path)
        except ValueError as exc:
            log.debug("skipping project snapshot entry %r: %s", path, exc)
            continue
        if rel not in files:
            files[rel] = os.path.realpath(os.path.abspath(path))
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
    except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError):
        return None


def _write_project_archive(project_file: str, timestamp: str, files: dict[str, str]) -> str:
    """Pack *files* into a ``.tar.gz`` archive and return its path."""
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


def save_project_version(
    project_file: str,
    keep: int | None = None,
    include_files: list[str] | None = None,
    exclude_versions: set[str] | None = None,
) -> str:
    """Save a version archive bundling the whole project file set.

    The bundle is a single ``.tar.gz`` under ``<project_dir>/.versions/
    project/`` named ``<stem>.project.<timestamp>.tar.gz``, so every file in
    the set shares one version id and can be restored together (see
    :func:`restore_project_version`).  If the current file set is identical
    to the latest archive, no new archive is created and the existing one is
    returned unchanged.  At most *keep* archives are retained (default
    MAX_PROJECT_VERSIONS); oldest are pruned first.

    :param project_file: Absolute path to the project file (.kicad_pro).
    :param keep: Number of archives to retain, or None for the default.
    :param include_files: Extra files to archive, such as every board and
        sheet in the project.  Entries that are missing or resolve outside
        the project directory are skipped and logged at debug level.
    :param exclude_versions: Version ids that must never be pruned, such as
        the archive a restore is currently reading from.
    :returns: Path of the (new or existing) archive.
    :raises FileNotFoundError: If *project_file* or the whole file set is missing.
    :raises OSError: If the archive directory cannot be created or the write fails.
    """
    project_file = os.path.abspath(project_file)
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"File not found: {project_file}")
    files = project_file_set(project_file, extra_files=include_files)
    if not files:
        raise FileNotFoundError(f"No project files found for: {project_file}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    current_hashes = {rel: _file_hash(path) for rel, path in sorted(files.items())}

    existing = _list_project_archive_paths(project_file)
    if existing:
        latest_manifest = _read_project_manifest(existing[-1])
        if latest_manifest and latest_manifest.get("files") == current_hashes:
            return existing[-1]

    archive_path = _write_project_archive(project_file, timestamp, files)

    limit = MAX_PROJECT_VERSIONS if keep is None else keep
    if limit > 0:
        prefix = os.path.join(_project_versions_dir(project_file), _archive_prefix(project_file))
        excluded = {
            os.path.abspath(f"{prefix}.{version_id}.tar.gz")
            for version_id in (exclude_versions or ())
        }
        remaining = _list_project_archive_paths(project_file)
        while len(remaining) > limit:
            victim = next(
                (path for path in remaining if os.path.abspath(path) not in excluded),
                None,
            )
            if victim is None:
                break
            with contextlib.suppress(OSError):
                os.remove(victim)
            remaining.remove(victim)

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


def _safe_restore_target(project_dir: str, member_name: str) -> str | None:
    """Return the extraction target for *member_name*, or ``None`` if unsafe.

    A member is refused when its name is absolute, contains ``..``, resolves
    outside the project root, or would be written through a symlinked parent
    (or over a symlink).  This mirrors the containment rule used on save and
    prevents an archive from escaping the project tree during restore.
    """
    if os.path.isabs(member_name):
        return None
    if ".." in member_name.replace("\\", "/").split("/"):
        return None
    normalized = os.path.normpath(member_name)
    parts = normalized.split(os.sep)
    if normalized in ("", os.curdir, os.pardir) or os.pardir in parts:
        return None
    if parts and parts[0] == os.curdir:
        parts = parts[1:]
    if not parts:
        return None

    root = os.path.realpath(project_dir)
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None

    target = os.path.join(root, *parts)
    if os.path.islink(target):
        return None
    real_target = os.path.realpath(target)
    try:
        rel = os.path.relpath(real_target, root)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return target


def _restorable_members(project_dir: str, archive_path: str) -> tuple[list[tuple[str, str]], int]:
    """Return safe ``(member_name, target)`` pairs and the unsafe count.

    The manifest and non-file members are ignored.  Members whose path would
    escape the project root (including through a symlinked parent) are
    counted and refused.  Legacy archives without ``.manifest.json`` are
    handled from their member list alone.
    """
    members: list[tuple[str, str]] = []
    unsafe = 0
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == ".manifest.json" or not member.isfile():
                continue
            target = _safe_restore_target(project_dir, member.name)
            if target is None:
                unsafe += 1
                continue
            members.append((member.name, target))
    return members, unsafe


def restore_project_version(project_file: str, version_id: str) -> dict[str, Any]:
    """Restore the project files to a previously saved project version.

    The archive's file set (schematic, PCB, project file and any included
    nested files) is extracted over the current files, so a restore returns
    the whole project to one consistent state.  Before overwriting, every
    file in that archive that still exists on disk is itself archived (so
    the restore is undoable via the returned backup id).

    :param project_file: Absolute path to the project file (.kicad_pro).
    :param version_id: The ``id`` value returned by :func:`list_project_versions`.
    :returns: Dict with keys ``restored_from`` (version_id),
              ``backup_of_current`` (archive path taken before restore),
              ``backup_version_id``, ``backup_skipped`` (number of unsafe
              archive members excluded from the backup and restore) and
              ``files`` (relative paths restored).
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

    restored: list[str] = []
    project_dir = os.path.dirname(project_file)
    members, backup_skipped = _restorable_members(project_dir, archive_path)

    # Snapshot the current state first so the restore is undoable.  Include
    # every safe archived file that still exists, so the backup covers nested
    # sheets the restore is about to overwrite; files that now resolve
    # outside the project cannot be backed up or restored and are counted.
    backup_files = [target for _, target in members if os.path.isfile(target)]
    backup_path = save_project_version(
        project_file,
        include_files=backup_files,
        exclude_versions={version_id},
    )
    backup_version_id = project_version_id(backup_path, project_file)

    with tarfile.open(archive_path, "r:gz") as tar:
        for name, target in members:
            member = tar.getmember(name)
            fh = tar.extractfile(member)
            if fh is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(fh, out)
            restored.append(name)

    return {
        "restored_from": version_id,
        "backup_of_current": backup_path,
        "backup_version_id": backup_version_id,
        "backup_skipped": backup_skipped,
        "files": sorted(restored),
    }
