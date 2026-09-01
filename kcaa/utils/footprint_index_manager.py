"""
Orchestrator that keeps the FootprintDatabase in sync with the on-disk
footprint libraries.

Architecture overview
---------------------
FootprintIndexManager reads every fp-lib-table reachable from the user's
KiCad config (including ``type="Table"`` indirections), resolves the effective
library list (project-overrides-global, deduped by nickname), then for each
``.pretty`` directory computes a content fingerprint (SHA-256 of a sorted
"filename:mtime:size" manifest) and decides whether to:

  1. Skip  — fingerprint identical to stored value (content unchanged).
  2. Touch-only — fingerprint changed but dir_path changed too (AppImage
     remount); update dir_path in DB without reparsing the footprints.
  3. Full reparse — fingerprint differs; re-read all .kicad_mod files and
     persist the updated index.

Using the library nickname as the stable DB key means AppImage re-mounts
(which change the resolved dir_path) do not cause spurious cache invalidation.

Example
-------
    mgr = FootprintIndexManager()
    stats = mgr.sync()
    results = mgr.search_footprints('0402')
"""

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import threading
import time

from kcaa.utils.footprint_database import (
    DbStats,
    FootprintDatabase,
    FootprintRecord,
    FpLibraryRecord,
)
from kcaa.utils.pcb_library_utils import (
    build_effective_library_list,
    parse_kicad_mod,
    scan_footprint_library,
)

log = logging.getLogger(__name__)

from kcaa.utils.config import config

_DEFAULT_DB_PATH = Path(config.get_kcaa_data_dir()) / "kicad_footprints.db"


def normalize_project_id(input: str | None) -> str:
    """Canonical project identifier for a ``.kicad_pro``/``.kicad_pcb`` path.

    Always the realpath of the parent directory, regardless of which file
    kind was passed — sync (``.kicad_pro``) and find/add (``.kicad_pcb``)
    therefore produce identical strings and project-scoped lookups match.
    Empty string means "no project" (globals only).
    """
    if not input:
        return ""
    return os.path.realpath(os.path.dirname(input))


@dataclass
class SyncStats:
    added: int
    updated: int
    removed: int
    skipped: int
    failed: int
    total_footprints: int
    elapsed_seconds: float


class FootprintIndexManager:
    """Keeps a FootprintDatabase in sync with on-disk footprint libraries.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Defaults to the package-local
        ``footprint_db/kicad_footprints.db``.
    project_path:
        Optional path to a ``.kicad_pro`` or ``.kicad_pcb`` project file;
        used to locate the project-local fp-lib-table and to derive the
        project identifier (the realpath of the project directory) that
        scopes index reads and writes.
    project_id:
        Optional project identifier, already in canonical form (``"```` '
        '':`` globally scoped).  Pass either *project_path* or *project_id*,
        never both; *project_id* avoids re-deriving it from a path by callers
        that only need the scoped manager.
    """

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        project_path: str | None = None,
        *,
        project_id: str | None = None,
    ):
        if project_path is not None and project_id is not None:
            raise ValueError("Pass either project_path or project_id, not both")
        resolved = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db = FootprintDatabase(str(resolved))
        self._project_path = project_path
        self._project_id = (
            project_id if project_id is not None else normalize_project_id(project_path)
        )

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(
        self,
        force: bool = False,
        progress_callback=None,
    ) -> SyncStats:
        """Sync the database with the current library table.

        Change detection uses a content-fingerprint strategy:
        * Compute SHA-256 of a sorted "filename:mtime:size\\n" manifest for
          all .kicad_mod files in the .pretty directory.
        * Identical fingerprint → skip (no disk reads beyond stat()).
        * Different fingerprint but dir_path changed only → touch-only update
          (AppImage re-mount, same files).
        * Different fingerprint → full reparse.

        Pass ``force=True`` to bypass all change detection and reparse every
        library.
        Pass ``progress_callback(current, total, library_name)`` to receive
        per-library progress notifications.
        """
        t0 = time.time()
        stats = SyncStats(
            added=0,
            updated=0,
            removed=0,
            skipped=0,
            failed=0,
            total_footprints=0,
            elapsed_seconds=0.0,
        )

        # Capture the scope once into locals: sync runs in a background thread
        # while other tool calls can re-scope the shared singleton manager, and
        # this loop must not observe a mid-flight scope flip.
        project_id = self._project_id
        project_path = self._project_path
        entries = build_effective_library_list(project_path)
        # Only global + current-project libraries participate: other projects'
        # rows must never be touched by this sync.
        db_known = self._db.get_library_states(project_id)
        current_names: set[str] = set()
        total = len(entries)
        project_table = os.path.join(project_id, "fp-lib-table") if project_id else None

        for i, entry in enumerate(entries):
            lib_name = entry["nickname"]
            raw_uri = entry.get("raw_uri", entry["uri"])
            dir_path = entry["uri"]  # resolved path to .pretty directory
            description = entry.get("description", "")
            # Libraries listed in the project's own fp-lib-table belong to the
            # project; everything else (global user/system tables) is global.
            entry_project = project_id if entry.get("table_path") == project_table else ""

            if progress_callback is not None:
                try:
                    progress_callback(i, total, lib_name)
                except Exception as exc:
                    log.warning("progress_callback raised: %s", exc)

            if not os.path.isdir(dir_path):
                log.debug(f"[{i + 1}/{total}] Missing or not a dir: {lib_name} → {dir_path}")
                stats.failed += 1
                continue

            current_names.add(lib_name)

            try:
                new_checksum = self._compute_dir_checksum(dir_path)
            except OSError as exc:
                log.warning(f"[{i + 1}/{total}] Cannot fingerprint {dir_path}: {exc}")
                stats.failed += 1
                continue

            if lib_name in db_known:
                lib_id, db_checksum, db_dir_path = db_known[lib_name]

                if not force and new_checksum == db_checksum and db_checksum != "":
                    # Content unchanged — just refresh dir_path if it moved
                    # (e.g. AppImage re-mounted at a new temp path).
                    if db_dir_path != dir_path:
                        log.debug(
                            f"[{i + 1}/{total}] Path relocated, content unchanged: {lib_name}"
                        )
                        self._db.touch_library(lib_id, new_checksum, dir_path)
                    else:
                        log.debug(f"[{i + 1}/{total}] Unchanged: {lib_name}")
                    stats.skipped += 1
                    continue

                log.info(f"[{i + 1}/{total}] Updating: {lib_name}")
                n = self._index_library(
                    lib_name, raw_uri, dir_path, description, new_checksum, entry_project
                )
                if n >= 0:
                    stats.updated += 1
                    stats.total_footprints += n
                else:
                    stats.failed += 1
            else:
                log.info(f"[{i + 1}/{total}] Adding: {lib_name}")
                n = self._index_library(
                    lib_name, raw_uri, dir_path, description, new_checksum, entry_project
                )
                if n >= 0:
                    stats.added += 1
                    stats.total_footprints += n
                else:
                    stats.failed += 1

        if progress_callback is not None:
            try:
                progress_callback(total, total, "")
            except Exception as exc:
                log.warning("progress_callback raised on completion: %s", exc)

        # Remove libraries no longer in the effective table.
        for lib_name, (lib_id, _, _) in db_known.items():
            if lib_name not in current_names:
                log.info(f"Removing: {lib_name}")
                self._db.delete_library(lib_id)
                stats.removed += 1

        stats.elapsed_seconds = time.time() - t0
        log.info(
            f"Footprint sync complete in {stats.elapsed_seconds:.2f}s — "
            f"+{stats.added} added, ~{stats.updated} updated, "
            f"-{stats.removed} removed, ={stats.skipped} unchanged, "
            f"!{stats.failed} failed. "
            f"Footprints indexed this run: {stats.total_footprints}"
        )
        return stats

    def index_library(
        self,
        library_name: str,
        dir_path: str,
        description: str = "",
        raw_uri: str = "",
        project: str = "",
    ) -> int:
        """Index exactly one ``.pretty`` directory into the database.

        Narrow update for the PCB → library export tools: indexes a single
        library directory without traversing the effective library list.
        Change detection (checksum) is shared with ``sync``.

        :param project: Project identifier for the row ("" = global 3rdparty
            library).  Project-local fp-lib-table libraries are never pulled
            into the database by this path.
        :returns: Number of footprints stored, or -1 on failure.
        """
        if not os.path.isdir(dir_path):
            log.warning("index_library: not a directory: %s", dir_path)
            return -1
        try:
            new_checksum = self._compute_dir_checksum(dir_path)
        except OSError as exc:
            log.warning("index_library: cannot fingerprint %s: %s", dir_path, exc)
            return -1
        return self._index_library(
            library_name,
            raw_uri or dir_path,
            dir_path,
            description,
            new_checksum,
            project,
        )

    # ------------------------------------------------------------------
    # Search / lookup passthrough
    # ------------------------------------------------------------------

    def search_footprints(self, query: str, limit: int = 50) -> list[FootprintRecord]:
        """Full-text search across footprint names, descriptions, and tags.

        Scoped to the manager's project (global + project libraries).
        """
        return self._db.search(query, limit=limit, project=self._project_id)

    def search_by_name(
        self, name: str, exact: bool = False, limit: int = 50
    ) -> list[FootprintRecord]:
        """Search footprints by name substring or exact match, scoped to the
        manager's project (global + project libraries)."""
        return self._db.search_by_name(name, exact=exact, limit=limit, project=self._project_id)

    def get_footprint(self, library_name: str, footprint_name: str) -> FootprintRecord | None:
        """Look up a single footprint by library nickname and name, scoped to
        the manager's project (global + project libraries)."""
        return self._db.get_footprint(library_name, footprint_name, project=self._project_id)

    def get_library_footprints(self, library_name: str) -> list[FootprintRecord]:
        """Return all indexed footprints for a library, ordered by name,
        scoped to the manager's project (global + project libraries)."""
        return self._db.get_library_footprints(library_name, project=self._project_id)

    def get_all_libraries(self) -> list[FpLibraryRecord]:
        """Return all indexed library records, ordered by name, scoped to the
        manager's project (global + project libraries)."""
        return self._db.get_all_libraries(project=self._project_id)

    def library_name_exists(self, library_name: str) -> bool:
        """True if any indexed library (any project) has this nickname.

        Nicknames are globally unique — used to block same-name library
        creation regardless of project.
        """
        return self._db.library_name_exists(library_name)

    def get_all_footprint_names(self) -> set[str]:
        """Return every footprint name indexed in the manager's project scope
        (global + project libraries)."""
        return self._db.get_all_footprint_names(project=self._project_id)

    def get_stats(self) -> DbStats:
        """Return summary statistics about the footprint database, scoped to
        the manager's project (global + project libraries)."""
        return self._db.get_stats(project=self._project_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_dir_checksum(dir_path: str) -> str:
        """Compute a SHA-256 fingerprint for a .pretty directory.

        Hashes a sorted manifest of "filename:mtime:size\\n" lines for every
        .kicad_mod file in the directory.  This detects file additions,
        deletions, and in-place modifications with only stat() calls —
        no file content is read.
        """
        lines: list[str] = []
        for fname in os.listdir(dir_path):
            if not fname.endswith(".kicad_mod"):
                continue
            fpath = os.path.join(dir_path, fname)
            try:
                st = os.stat(fpath)
                lines.append(f"{fname}:{st.st_mtime}:{st.st_size}\n")
            except OSError:
                continue
        lines.sort()
        h = hashlib.sha256()
        for line in lines:
            h.update(line.encode())
        return h.hexdigest()

    def _index_library(
        self,
        library_name: str,
        raw_uri: str,
        dir_path: str,
        description: str,
        checksum: str,
        project: str = "",
    ) -> int:
        """Parse all .kicad_mod files in dir_path and persist to the database.
        Returns the number of footprints stored, or -1 on error.
        """
        try:
            footprints = self._parse_library(library_name, dir_path)
        except Exception as exc:
            log.error(f"Parse failed for {dir_path}: {exc}")
            # Clear any stale checksum so the next sync retries this library
            self._db.save_library(
                library_name=library_name,
                raw_uri=raw_uri,
                dir_path=dir_path,
                description=description,
                checksum="",
                footprints=[],
                project=project,
            )
            return -1

        n = self._db.save_library(
            library_name=library_name,
            raw_uri=raw_uri,
            dir_path=dir_path,
            description=description,
            checksum=checksum,
            footprints=footprints,
            project=project,
        )
        log.debug(f"  Indexed {n} footprints from {os.path.basename(dir_path)}")
        return n

    @staticmethod
    def _parse_library(library_name: str, dir_path: str) -> list[FootprintRecord]:
        """Read every .kicad_mod in dir_path and return FootprintRecord list."""
        records: list[FootprintRecord] = []
        for fp_name in scan_footprint_library(dir_path):
            mod_path = os.path.join(dir_path, fp_name + ".kicad_mod")
            try:
                info = parse_kicad_mod(mod_path)
                records.append(
                    FootprintRecord(
                        library_name=library_name,
                        footprint_name=fp_name,
                        library_id=0,  # filled in by FootprintDatabase.save_library
                        description=info.get("description", ""),
                        tags=info.get("tags", ""),
                        attr=info.get("attr", ""),
                        pad_count=len(info.get("pads", [])),
                        has_3d_model=bool(info.get("has_3d_model", False)),
                    )
                )
            except Exception as exc:
                log.warning(f"Skipped {mod_path}: {exc}")
        return records


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: FootprintIndexManager | None = None
_singleton_lock = threading.Lock()


def get_footprint_index_manager(
    project_path: str | None = None,
    *,
    project_id: str | None = None,
) -> FootprintIndexManager:
    """Return the module-level FootprintIndexManager singleton.

    On first call the manager is created with the default DB path.
    If ``project_path`` is provided it is forwarded to
    ``build_effective_library_list`` so project-local libraries are included.
    *project_id*, when given, scopes the manager to that canonical project
    identifier directly (no path involved); pass either it or
    ``project_path``, never both.

    Thread-safe (double-checked locking).
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = FootprintIndexManager(project_path=project_path, project_id=project_id)
    else:
        # Re-scoping mutates the shared singleton's project attributes; the
        # background sync thread reads them at launch and works for minutes.
        # Take the lock so a re-scope is never observed mid-flight and the
        # two attribute writes stay atomic as a pair.
        with _singleton_lock:
            if project_path is not None:
                _singleton._project_path = project_path
                _singleton._project_id = normalize_project_id(project_path)
            elif project_id is not None:
                _singleton._project_path = None
                _singleton._project_id = project_id
    return _singleton
