"""
KiCad symbol library table reader.

Reads the sym-lib-table file and expands environment variables in URIs.
"""

from dataclasses import dataclass
import logging
import os
import re

from kcaa.utils.config import ServerConfig
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
    table_path: str = ""  # sym-lib-table file the entry came from


# ---------------------------------------------------------------------------
# SymbolIndexReader
# ---------------------------------------------------------------------------


class SymbolIndexReader:
    """Reads the KiCad sym-lib-table file(s) and returns library entries."""

    def __init__(
        self,
        config: ServerConfig | None = None,
        project_dir: str | None = None,
    ):
        self._config = config or ServerConfig()
        self._project_dir = project_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_libraries(self) -> list[LibraryTableEntry]:
        """Parse sym-lib-table file(s) and return all library entries.

        With a project directory whose ``sym-lib-table`` exists, the project
        table is parsed first, then the global user table; duplicate
        nicknames keep the project entry (project wins over global).  Without
        a project table only the global table is read.
        """
        entries: list[LibraryTableEntry] = []
        seen: set[str] = set()
        if self._project_dir:
            project_table = os.path.join(self._project_dir, "sym-lib-table")
            if os.path.isfile(project_table):
                entries.extend(self._parse_table_file(project_table, visited=set()))
                seen = {e.name for e in entries}

        global_path = self._config.symbol_table_file
        if global_path and os.path.exists(global_path):
            for entry in self._parse_table_file(global_path, visited=set()):
                if entry.name not in seen:
                    entries.append(entry)
            return entries

        if entries:
            # Project table only — a global user table is absent.
            return entries
        raise FileNotFoundError(f"sym-lib-table not found: {global_path}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

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
                uri = self._expand_env_vars(uri, self._project_dir)

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
                    table_path=real_path,
                )
            )

        log.info(
            "Loaded %d librar%s from: %s", len(entries), "y" if len(entries) == 1 else "ies", path
        )
        return entries

    def _expand_env_vars(self, path: str, project_dir: str | None = None) -> str:
        """Replace ${VAR_NAME} placeholders using the configured env vars.

        ``${KIPRJMOD}`` resolves to *project_dir* when a project is in
        context (same single addition as ``pcb_library_utils._build_env_map``);
        all other variables keep the current env behavior.
        """
        env = dict(self._config.get_env_vars())
        if project_dir:
            env["KIPRJMOD"] = project_dir
        for var, value in env.items():
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
