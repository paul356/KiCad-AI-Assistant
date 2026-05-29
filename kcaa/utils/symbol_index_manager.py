"""
Symbol Index Manager

Bridges SymbolIndexReader (reads sym-lib-table) and SymbolDatabase
(SQLite index). Provides a single high-level API for syncing, searching,
and looking up KiCad symbols.

Parsing responsibility:
    This module owns all .kicad_sym file parsing via the skip library.
    SymbolDatabase is a pure storage layer; it receives already-parsed
    SymbolRecord lists and knows nothing about skip or KiCad file formats.

Database location:
    <this file's directory>/symbol_db/kicad_symbols.db
"""

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import time

import skip.collection

from kcaa.utils.skip_compat import safe_source_file

from kcaa.utils.symbol_database import (
    DbStats,
    LibraryRecord,
    SymbolDatabase,
    SymbolRecord,
)
from kcaa.utils.symbol_index_reader import SymbolIndexReader

log = logging.getLogger(__name__)

from kcaa.utils.config import config

_DEFAULT_DB_PATH = Path(config.get_kcaa_data_dir()) / "kicad_symbols.db"


# ---------------------------------------------------------------------------
# SyncStats
# ---------------------------------------------------------------------------


@dataclass
class SyncStats:
    added: int
    updated: int
    removed: int
    skipped: int
    failed: int
    total_symbols: int
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Optional diagnostic dependencies
# ---------------------------------------------------------------------------

try:
    import objgraph as _objgraph  # type: ignore
except ImportError:
    _objgraph = None

try:
    from pympler import asizeof as _asizeof  # type: ignore
except ImportError:
    _asizeof = None


def _rss_mb() -> float:
    """Return current process RSS in MB (Linux only)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def _parsed_value_count() -> int | None:
    if _objgraph is None:
        return None
    return _objgraph.count("ParsedValue")


# ---------------------------------------------------------------------------
# SymbolIndexManager
# ---------------------------------------------------------------------------


class SymbolIndexManager:
    """
    High-level API for indexing and searching KiCad symbols.

    On first use, call sync() to parse all libraries and populate the DB.
    Subsequent calls to sync() are incremental — only changed or new files
    are reparsed.

    Example
    -------
        mgr = SymbolIndexReader()
        idx = SymbolIndexManager(mgr)
        stats = idx.sync()
        results = idx.search_symbols('resistor')
    """

    def __init__(
        self,
        library_manager: SymbolIndexReader,
        db_path: str | Path | None = None,
    ):
        self._library_manager = library_manager
        resolved = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db = SymbolDatabase(str(resolved))

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(
        self,
        force: bool = False,
        diagnose: bool = False,
        progress_callback=None,
    ) -> SyncStats:
        """
        Sync the database with the current library table.

        Change detection uses a two-stage strategy:
        1. Fast path: if mtime and size match the DB → skip (no disk read).
        2. Checksum path: if mtime or size differ, compute the file's SHA-256.
           - Same checksum as stored → file content unchanged (e.g. file was
             touched or copied); update mtime/size in DB without reparsing.
           - Different checksum → content changed; fully reparse and reindex.

        Pass force=True to bypass all change detection and reparse every file.
        Pass diagnose=True to enable memory/object diagnostics during parsing.
        Pass progress_callback(current, total, library_name) to receive
        per-library progress notifications (called after each library is
        processed, whether skipped, indexed, or failed).
        """
        t0 = time.time()
        stats = SyncStats(
            added=0,
            updated=0,
            removed=0,
            skipped=0,
            failed=0,
            total_symbols=0,
            elapsed_seconds=0.0,
        )

        entries = self._library_manager.get_libraries()
        db_known = self._db.get_library_states()  # {path: (id, mtime, size, checksum)}

        current_paths: set[str] = set()

        # Expand entries into (library_name, file_path) pairs.
        # KiCad 10 sym-lib-table entries may point to a directory (.kicad_symdir)
        # containing multiple .kicad_sym files rather than a single .kicad_sym file.
        all_file_entries: list[tuple[str, str]] = []  # (library_name, file_path)
        for entry in entries:
            raw_path = entry.uri
            if not raw_path:
                continue
            if os.path.isdir(raw_path):
                try:
                    for fname in sorted(os.listdir(raw_path)):
                        if fname.endswith(".kicad_sym"):
                            stem = fname[: -len(".kicad_sym")]
                            lib_name = f"{entry.name}/{stem}"
                            all_file_entries.append((lib_name, os.path.join(raw_path, fname)))
                except OSError as exc:
                    log.warning(f"Cannot list directory {raw_path}: {exc}")
                    stats.failed += 1
            else:
                all_file_entries.append((entry.name, raw_path))

        total = len(all_file_entries)

        for i, (lib_name, path) in enumerate(all_file_entries):
            if progress_callback is not None:
                try:
                    progress_callback(i, total, lib_name)
                except Exception as exc:
                    log.warning("progress_callback raised: %s", exc)
            if not os.path.exists(path):
                log.warning(f"[{i + 1}/{total}] Skipping missing: {lib_name} -> {path}")
                stats.failed += 1
                continue

            current_paths.add(path)

            try:
                stat = os.stat(path)
                cur_mtime: float = stat.st_mtime
                cur_size: int = stat.st_size
            except OSError as exc:
                log.warning(f"[{i + 1}/{total}] Cannot stat {path}: {exc}")
                stats.failed += 1
                continue

            if path in db_known:
                lib_id, db_mtime, db_size, db_checksum = db_known[path]

                if not force and db_mtime == cur_mtime and db_size == cur_size:
                    # Fast path: metadata identical → assume unchanged.
                    log.debug(f"[{i + 1}/{total}] Unchanged: {lib_name}")
                    stats.skipped += 1
                    continue

                # Metadata changed — compare content via checksum before reparsing.
                new_checksum = self._compute_checksum(path)
                if not force and new_checksum == db_checksum and db_checksum != "":
                    # File was touched/copied but content is identical → just
                    # update the stored mtime/size so the fast path fires next time.
                    log.debug(f"[{i + 1}/{total}] Metadata changed, content unchanged: {lib_name}")
                    self._db.touch_library(lib_id, cur_mtime, cur_size, new_checksum)
                    stats.skipped += 1
                    continue

                log.info(f"[{i + 1}/{total}] Updating: {lib_name}")
                n = self._index_library(
                    lib_name,
                    path,
                    cur_mtime,
                    cur_size,
                    new_checksum,
                    diagnose=diagnose,
                )
                if n >= 0:
                    stats.updated += 1
                    stats.total_symbols += n
                else:
                    stats.failed += 1
            else:
                log.info(f"[{i + 1}/{total}] Adding: {lib_name}")
                new_checksum = self._compute_checksum(path)
                n = self._index_library(
                    lib_name,
                    path,
                    cur_mtime,
                    cur_size,
                    new_checksum,
                    diagnose=diagnose,
                )
                if n >= 0:
                    stats.added += 1
                    stats.total_symbols += n
                else:
                    stats.failed += 1

        if progress_callback is not None:
            try:
                progress_callback(total, total, "")
            except Exception as exc:
                log.warning("progress_callback raised on completion: %s", exc)

        # Remove libraries no longer in the table.
        for path, (lib_id, _, _, _) in db_known.items():
            if path not in current_paths:
                log.info(f"Removing: {path}")
                self._db.delete_library(lib_id)
                stats.removed += 1

        stats.elapsed_seconds = time.time() - t0
        log.info(
            f"Sync complete in {stats.elapsed_seconds:.2f}s — "
            f"+{stats.added} added, ~{stats.updated} updated, "
            f"-{stats.removed} removed, ={stats.skipped} unchanged, "
            f"!{stats.failed} failed. "
            f"Symbols indexed this run: {stats.total_symbols}"
        )
        return stats

    def _index_library(
        self,
        library_name: str,
        file_path: str,
        mtime: float,
        file_size: int,
        checksum: str,
        diagnose: bool = False,
    ) -> int:
        """
        Parse a .kicad_sym file and persist the extracted symbols.
        Returns the number of symbols stored, or -1 on error.
        """
        try:
            symbols, kicad_version = self._parse_library(file_path, diagnose=diagnose)
        except Exception as exc:
            log.error(f"Parse failed for {file_path}: {exc}")
            return -1

        n = self._db.save_library(
            library_name, file_path, mtime, file_size, kicad_version, symbols, checksum
        )
        log.info(f"  Indexed {n} symbols from {os.path.basename(file_path)}")
        return n

    # ------------------------------------------------------------------
    # Library parsing (skip-library logic lives here, not in SymbolDatabase)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_checksum(file_path: str) -> str:
        """
        Compute a SHA-256 digest of the file at file_path.
        Read in chunks to avoid loading large library files fully into memory.
        """
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _parse_library(
        self,
        file_path: str,
        diagnose: bool = False,
    ) -> tuple[list[SymbolRecord], str]:
        """
        Parse a .kicad_sym file and extract symbol metadata.

        The SourceFile object (and its entire S-expression tree) is deleted
        immediately after extraction so it can be garbage-collected.

        Pass diagnose=True to print RSS / ParsedValue / pympler snapshots at
        each major stage (same report as the former diagnose_single_library).

        Returns (list[SymbolRecord], kicad_version_string).
        """

        def snapshot(label: str) -> None:
            if not diagnose:
                return
            rss = _rss_mb()
            pv = _parsed_value_count()
            pv_str = f", ParsedValue live: {pv}" if pv is not None else ""
            print(f"  [{label}] RSS: {rss:.1f} MB{pv_str}")

        snapshot("before SourceFile()")
        library_file = safe_source_file(file_path)
        snapshot("after SourceFile()")

        if diagnose and _asizeof is not None:
            deep_mb = _asizeof.asizeof(library_file) / 1024 / 1024
            print(f"  [deep size] pympler asizeof: {deep_mb:.1f} MB")

        kicad_version = self._extract_version(library_file)
        symbol_elements = self._get_symbol_elements(library_file)

        if diagnose:
            print(f"  [info] symbol count: {len(symbol_elements)}")

        symbols: list[SymbolRecord] = []
        for idx, sym_el in enumerate(symbol_elements):
            try:
                symbol_name = sym_el.value
                if not symbol_name or not isinstance(symbol_name, str):
                    continue

                description, keywords = self._extract_properties(sym_el)
                pin_count = len(sym_el.getElementsByEntityType("pin"))

                symbols.append(
                    SymbolRecord(
                        library_name="",  # filled in by SymbolDatabase.save_library
                        symbol_name=symbol_name,
                        library_id=0,  # filled in by SymbolDatabase.save_library
                        description=description,
                        keywords=keywords,
                        pin_count=pin_count,
                        file_index=idx,
                    )
                )
            except Exception as exc:
                log.warning(f"Skipped symbol {idx} in {file_path}: {exc}")

        snapshot("after extracting symbols (elements still alive)")

        # Discard the S-expression tree so it can be garbage-collected.
        del symbol_elements
        snapshot("after del symbol_elements")
        del library_file
        snapshot("after del library_file (before any GC)")

        if diagnose:
            print("  [note] ParsedValue count may not drop until cyclic GC runs.")

        return symbols, kicad_version

    @staticmethod
    def _extract_version(library_file: object) -> str:
        """Extract the kicad_version string from the library file root."""
        if hasattr(library_file, "version"):
            try:
                return str(library_file.version.value)
            except Exception as exc:
                log.warning("Could not read kicad_version: %s", exc)
        return ""

    @staticmethod
    def _get_symbol_elements(library_file: object) -> list:
        """Return the list of top-level symbol ParsedValues."""
        if not hasattr(library_file, "symbol"):
            return []
        elements = library_file.symbol
        # Could be a single ParsedValue, a plain list, or an ElementCollection.
        if isinstance(elements, (list, skip.collection.ElementCollection)):
            return elements
        return [elements]

    @staticmethod
    def _extract_properties(sym_el: object) -> tuple[str, str]:
        """
        Walk the direct property children of a symbol element and extract
        the Description and ki_keywords values.

        KiCad property S-expression:
            (property "Description" "Resistor" ...)
        After parsing:
            prop.children[0] = "Description"   (str, property name)
            prop.children[1] = "Resistor"       (str, property value)
        """
        description = ""
        keywords = ""
        for prop in sym_el.getElementsByEntityType("property"):
            children = prop.children
            if len(children) < 2:
                continue
            prop_name = children[0] if isinstance(children[0], str) else ""
            prop_val = children[1] if isinstance(children[1], str) else ""
            if prop_name in ("Description", "ki_description"):
                description = prop_val
            elif prop_name == "ki_keywords":
                keywords = prop_val
        return description, keywords

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_symbols(self, query: str, limit: int = 50) -> list[SymbolRecord]:
        """
        Full-text search across symbol name, description, and keywords.
        Results are ordered by relevance (FTS5 rank).
        """
        return self._db.search(query, limit=limit)

    def search_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
    ) -> list[SymbolRecord]:
        """
        Search symbols by name.
        exact=True  — case-insensitive whole-name match.
        exact=False — case-insensitive substring match.
        """
        return self._db.search_by_name(name, exact=exact, limit=limit)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_symbol(self, library_name: str, symbol_name: str) -> SymbolRecord | None:
        """Look up a single symbol by library and symbol name."""
        return self._db.get_symbol(library_name, symbol_name)

    def get_library_symbols(self, library_name: str) -> list[SymbolRecord]:
        """Return all symbols in a library, ordered by position in file."""
        return self._db.get_library_symbols(library_name)

    def list_all_symbols(self) -> list[str]:
        """Return all symbol keys as 'library_name:symbol_name' strings."""
        return [f"{s.library_name}:{s.symbol_name}" for s in self._db.get_all_symbols()]

    def get_all_libraries(self) -> list[LibraryRecord]:
        """Return all indexed library records."""
        return self._db.get_all_libraries()

    def get_library_by_name(self, name: str) -> LibraryRecord | None:
        """Look up a library record by library name."""
        return self._db.get_library_by_name(name)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_statistics(self) -> DbStats:
        """Return library and symbol counts from the database."""
        return self._db.get_stats()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
