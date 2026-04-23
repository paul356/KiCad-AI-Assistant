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

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kicad_mcp.utils.footprint_database import (
    FootprintDatabase,
    FootprintRecord,
    FpLibraryRecord,
    DbStats,
)
from kicad_mcp.utils.pcb_library_utils import (
    build_effective_library_list,
    parse_kicad_mod,
    scan_footprint_library,
)

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent / 'footprint_db' / 'kicad_footprints.db'


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
        Optional path to a ``.kicad_pro`` file; the project-local
        fp-lib-table is read in addition to the global one.
    """

    def __init__(
        self,
        db_path: 'str | Path | None' = None,
        project_path: Optional[str] = None,
    ):
        resolved = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db = FootprintDatabase(str(resolved))
        self._project_path = project_path

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
            added=0, updated=0, removed=0, skipped=0,
            failed=0, total_footprints=0, elapsed_seconds=0.0,
        )

        entries = build_effective_library_list(self._project_path)
        db_known = self._db.get_library_states()   # {library_name: (id, checksum, dir_path)}
        current_names: set[str] = set()
        total = len(entries)

        for i, entry in enumerate(entries):
            lib_name = entry["nickname"]
            raw_uri = entry.get("raw_uri", entry["uri"])
            dir_path = entry["uri"]   # resolved path to .pretty directory
            description = entry.get("description", "")

            if progress_callback is not None:
                try:
                    progress_callback(i, total, lib_name)
                except Exception as exc:
                    log.warning("progress_callback raised: %s", exc)

            if not os.path.isdir(dir_path):
                log.debug(f"[{i+1}/{total}] Missing or not a dir: {lib_name} → {dir_path}")
                stats.failed += 1
                continue

            current_names.add(lib_name)

            try:
                new_checksum = self._compute_dir_checksum(dir_path)
            except OSError as exc:
                log.warning(f"[{i+1}/{total}] Cannot fingerprint {dir_path}: {exc}")
                stats.failed += 1
                continue

            if lib_name in db_known:
                lib_id, db_checksum, db_dir_path = db_known[lib_name]

                if not force and new_checksum == db_checksum and db_checksum != '':
                    # Content unchanged — just refresh dir_path if it moved
                    # (e.g. AppImage re-mounted at a new temp path).
                    if db_dir_path != dir_path:
                        log.debug(
                            f"[{i+1}/{total}] Path relocated, content unchanged: {lib_name}"
                        )
                        self._db.touch_library(lib_id, new_checksum, dir_path)
                    else:
                        log.debug(f"[{i+1}/{total}] Unchanged: {lib_name}")
                    stats.skipped += 1
                    continue

                log.info(f"[{i+1}/{total}] Updating: {lib_name}")
                n = self._index_library(lib_name, raw_uri, dir_path, description, new_checksum)
                if n >= 0:
                    stats.updated += 1
                    stats.total_footprints += n
                else:
                    stats.failed += 1
            else:
                log.info(f"[{i+1}/{total}] Adding: {lib_name}")
                n = self._index_library(lib_name, raw_uri, dir_path, description, new_checksum)
                if n >= 0:
                    stats.added += 1
                    stats.total_footprints += n
                else:
                    stats.failed += 1

        if progress_callback is not None:
            try:
                progress_callback(total, total, '')
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

    # ------------------------------------------------------------------
    # Search / lookup passthrough
    # ------------------------------------------------------------------

    def search_footprints(
        self, query: str, limit: int = 50
    ) -> list[FootprintRecord]:
        """Full-text search across footprint names, descriptions, and tags."""
        return self._db.search(query, limit=limit)

    def search_by_name(
        self, name: str, exact: bool = False, limit: int = 50
    ) -> list[FootprintRecord]:
        """Search footprints by name substring or exact match."""
        return self._db.search_by_name(name, exact=exact, limit=limit)

    def get_footprint(
        self, library_name: str, footprint_name: str
    ) -> FootprintRecord | None:
        """Look up a single footprint by library nickname and name."""
        return self._db.get_footprint(library_name, footprint_name)

    def get_library_footprints(
        self, library_name: str
    ) -> list[FootprintRecord]:
        """Return all indexed footprints for a library, ordered by name."""
        return self._db.get_library_footprints(library_name)

    def get_all_libraries(self) -> list[FpLibraryRecord]:
        """Return all indexed library records, ordered by name."""
        return self._db.get_all_libraries()

    def get_stats(self) -> DbStats:
        """Return summary statistics about the footprint database."""
        return self._db.get_stats()

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
            if not fname.endswith('.kicad_mod'):
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
    ) -> int:
        """Parse all .kicad_mod files in dir_path and persist to the database.
        Returns the number of footprints stored, or -1 on error.
        """
        try:
            footprints = self._parse_library(library_name, dir_path)
        except Exception as exc:
            log.error(f"Parse failed for {dir_path}: {exc}")
            return -1

        n = self._db.save_library(
            library_name=library_name,
            raw_uri=raw_uri,
            dir_path=dir_path,
            description=description,
            checksum=checksum,
            footprints=footprints,
        )
        log.debug(f"  Indexed {n} footprints from {os.path.basename(dir_path)}")
        return n

    @staticmethod
    def _parse_library(
        library_name: str, dir_path: str
    ) -> list[FootprintRecord]:
        """Read every .kicad_mod in dir_path and return FootprintRecord list."""
        records: list[FootprintRecord] = []
        for fp_name in scan_footprint_library(dir_path):
            mod_path = os.path.join(dir_path, fp_name + '.kicad_mod')
            try:
                info = parse_kicad_mod(mod_path)
                records.append(FootprintRecord(
                    library_name=library_name,
                    footprint_name=fp_name,
                    library_id=0,   # filled in by FootprintDatabase.save_library
                    description=info.get('description', ''),
                    tags=info.get('tags', ''),
                    attr=info.get('attr', ''),
                    pad_count=len(info.get('pads', [])),
                    has_3d_model=bool(info.get('has_3d_model', False)),
                ))
            except Exception as exc:
                log.warning(f"Skipped {mod_path}: {exc}")
        return records


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: FootprintIndexManager | None = None


def get_footprint_index_manager(
    project_path: Optional[str] = None,
) -> FootprintIndexManager:
    """Return the module-level FootprintIndexManager singleton.

    On first call the manager is created with the default DB path.
    If ``project_path`` is provided it is forwarded to
    ``build_effective_library_list`` so project-local libraries are included.
    """
    global _singleton
    if _singleton is None:
        _singleton = FootprintIndexManager(project_path=project_path)
    elif project_path is not None:
        _singleton._project_path = project_path
    return _singleton
