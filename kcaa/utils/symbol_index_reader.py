"""
KiCad symbol library table reader.

Reads the sym-lib-table file and expands environment variables in URIs.
"""

from dataclasses import dataclass
import logging
import os
import re

from kcaa.utils.config import LibraryPathConfig
from kcaa.utils.skip_compat import safe_source_file

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class LibraryTableEntry:
    """One entry from the sym-lib-table file."""

    name: str
    lib_type: str
    uri: str  # fully expanded (no ${VAR} placeholders)
    options: str
    descr: str


# ---------------------------------------------------------------------------
# SymbolIndexReader
# ---------------------------------------------------------------------------


class SymbolIndexReader:
    """Reads the KiCad sym-lib-table file and returns its library entries."""

    def __init__(self, config: LibraryPathConfig | None = None):
        self._config = config or LibraryPathConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_libraries(self) -> list[LibraryTableEntry]:
        """Parse the sym-lib-table file and return all library entries."""
        return self._parse_table()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_table(self) -> list[LibraryTableEntry]:
        path = self._config.symbol_table_file
        if not os.path.exists(path):
            raise FileNotFoundError(f"sym-lib-table not found: {path}")
        return self._parse_table_file(path, visited=set())

    def _parse_table_file(
        self,
        path: str,
        visited: set[str],
    ) -> list[LibraryTableEntry]:
        """
        Parse a single sym-lib-table file and return its entries.

        Entries whose type is ``"Table"`` are treated as indirection: their URI
        is expanded, the referenced file is parsed recursively, and the nested
        entries are spliced in-place of the indirection entry.  A ``visited``
        set guards against circular references.
        """
        real_path = os.path.realpath(path)
        if real_path in visited:
            log.warning("Circular sym-lib-table reference detected, skipping: %s", path)
            return []
        visited.add(real_path)

        log.info("Parsing sym-lib-table: %s", path)
        table = safe_source_file(path)
        entries: list[LibraryTableEntry] = []

        if not hasattr(table, "lib"):
            log.info("No library entries found in: %s", path)
            return entries

        # skip returns a bare ParsedValue node when there is only one lib entry,
        # and an ElementCollection when there are multiple. Normalise to iterable.
        import skip.sexp.parser as _sp

        raw = table.lib
        libs = [raw] if isinstance(raw, _sp.ParsedValue) else raw
        for lib in libs:
            name = lib.name.value if hasattr(lib, "name") and hasattr(lib.name, "value") else ""
            lib_type = lib.type.value if hasattr(lib, "type") and hasattr(lib.type, "value") else ""
            uri = lib.uri.value if hasattr(lib, "uri") and hasattr(lib.uri, "value") else ""
            options = (
                lib.options.value
                if hasattr(lib, "options") and hasattr(lib.options, "value")
                else ""
            )
            descr = lib.descr.value if hasattr(lib, "descr") and hasattr(lib.descr, "value") else ""

            if uri:
                uri = self._expand_env_vars(uri)

            # KiCad 10+: a "Table" entry redirects to another sym-lib-table file.
            if lib_type.lower() == "table":
                if os.path.isfile(uri):
                    log.info("Following sym-lib-table indirection: %s -> %s", path, uri)
                    entries.extend(self._parse_table_file(uri, visited))
                else:
                    log.warning("sym-lib-table indirection target not found: %s", uri)
                continue

            if lib_type.lower() != "kicad":
                log.info("Skipping non-KiCad library entry '%s' (type=%s)", name, lib_type)
                continue

            log.info("Found library '%s': %s", name, uri)
            entries.append(
                LibraryTableEntry(
                    name=name,
                    lib_type=lib_type,
                    uri=uri,
                    options=options,
                    descr=descr,
                )
            )

        log.info(
            "Loaded %d librar%s from: %s", len(entries), "y" if len(entries) == 1 else "ies", path
        )
        return entries

    def _expand_env_vars(self, path: str) -> str:
        """Replace ${VAR_NAME} placeholders using the configured env vars."""
        for var, value in self._config.get_env_vars().items():
            path = path.replace("${" + var + "}", value)
        # Normalise mixed \ and / separators on Windows.
        path = os.path.normpath(path)
        unresolved = sorted(set(re.findall(r"\$\{([^}]+)\}", path)))
        if unresolved:
            log.warning(
                "Unresolved sym-lib-table variable(s) %s in URI: %s",
                unresolved,
                path,
            )
        return path
