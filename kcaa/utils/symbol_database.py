"""
SQLite-backed symbol index database for KiCad symbol libraries.

Uses SQLAlchemy 2.x for all database operations.

Tables
------
libraries  -- one row per .kicad_sym file (id, path, mtime, size, ...)
symbols    -- one row per symbol (library_name, symbol_name, ...)
symbols_fts -- FTS5 virtual table mirroring symbols for full-text search

Note on FTS5
------------
SQLAlchemy has no native support for SQLite FTS5 virtual tables or their
associated triggers. The FTS5 DDL and MATCH queries use ``text()`` executed
directly against the connection, while all standard CRUD goes through the
ORM session.
"""

from dataclasses import dataclass
import logging
import os
import re
import time

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    create_engine,
    event,
    func,
    insert,
    or_,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses  (the external API — never expose ORM rows directly)
# ---------------------------------------------------------------------------


@dataclass
class LibraryRecord:
    id: int
    library_name: str
    file_path: str
    file_size: int
    mtime: float
    checksum: str
    symbol_count: int
    last_indexed: float
    kicad_version: str
    project: str = ""  # "" = global library; otherwise project identifier


@dataclass
class SymbolRecord:
    library_name: str
    symbol_name: str
    library_id: int
    description: str
    keywords: str
    pin_count: int
    file_index: int


@dataclass
class DbStats:
    library_count: int
    symbol_count: int
    last_sync: float  # Unix timestamp; 0.0 if never synced
    db_path: str


# ---------------------------------------------------------------------------
# ORM models  (internal — prefixed with _ to signal non-public)
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class _LibraryRow(_Base):
    __tablename__ = "libraries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False, unique=True)
    file_size = Column(Integer, nullable=False, default=0)
    mtime = Column(Float, nullable=False, default=0.0)
    checksum = Column(String, nullable=False, default="")
    symbol_count = Column(Integer, nullable=False, default=0)
    last_indexed = Column(Float, nullable=False, default=0.0)
    kicad_version = Column(String, nullable=False, default="")
    project = Column(String, nullable=False, default="", server_default="''")


class _SymbolRow(_Base):
    __tablename__ = "symbols"

    library_name = Column(String, nullable=False)
    symbol_name = Column(String, nullable=False)
    library_id = Column(Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False)
    description = Column(String, nullable=False, default="")
    keywords = Column(String, nullable=False, default="")
    pin_count = Column(Integer, nullable=False, default=0)
    file_index = Column(Integer, nullable=False, default=0)

    # Uniqueness is scoped to the PARENT library row, not the bare nickname:
    # same-nickname libraries in different projects own separate library
    # rows (libraries.project), so (library_id, symbol_name) lets two
    # projects hold overlapping symbol names without colliding.  The former
    # PK (library_name, symbol_name) made the second project's sync() fail
    # with an IntegrityError mid-run.
    __table_args__ = (
        PrimaryKeyConstraint("library_id", "symbol_name"),
        Index("idx_sym_library_id", "library_id"),
        Index("idx_sym_name", "symbol_name"),
        Index("idx_sym_library_name", "library_name"),
    )


# FTS5 virtual table + trigger DDL — executed once at schema setup time.
_DDL_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    library_name,
    symbol_name,
    description,
    keywords,
    content='symbols',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, library_name, symbol_name, description, keywords)
    VALUES (new.rowid, new.library_name, new.symbol_name, new.description, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, library_name, symbol_name, description, keywords)
    VALUES ('delete', old.rowid, old.library_name, old.symbol_name, old.description, old.keywords);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, library_name, symbol_name, description, keywords)
    VALUES ('delete', old.rowid, old.library_name, old.symbol_name, old.description, old.keywords);
    INSERT INTO symbols_fts(rowid, library_name, symbol_name, description, keywords)
    VALUES (new.rowid, new.library_name, new.symbol_name, new.description, new.keywords);
END;

"""


# ---------------------------------------------------------------------------
# SymbolDatabase
# ---------------------------------------------------------------------------


class SymbolDatabase:
    """SQLAlchemy-backed store for KiCad symbol library index data."""

    def __init__(self, db_path: str):
        """
        Open (or create) the database at db_path.
        The parent directory is created automatically if needed.
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path

        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        # Enable WAL mode, foreign keys, and a busy timeout on every new
        # connection.  The busy timeout lets narrow reindex writes (symbol
        # export tools) wait out a concurrent background sync instead of
        # failing immediately with SQLITE_BUSY.
        @event.listens_for(self._engine, "connect")
        def _set_pragmas(conn, _record):
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")

        self._Session = sessionmaker(bind=self._engine)
        self._apply_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        """Create ORM tables and FTS5 virtual table / triggers."""
        _Base.metadata.create_all(self._engine)
        with self._engine.connect() as conn:
            # Schema v2: libraries gains a `project` column.  Old databases
            # are upgraded in place (ALTER) — data is preserved.  v1 databases
            # have no project column; anything newer already matches the ORM.
            columns = conn.execute(text("PRAGMA table_info(libraries)")).all()
            col_names = {row[1] for row in columns}
            if "project" not in col_names and columns:
                log.info("symbol DB schema v1 → v2: adding libraries.project column")
                conn.execute(
                    text("ALTER TABLE libraries ADD COLUMN project VARCHAR NOT NULL DEFAULT ''")
                )
                conn.commit()

            # Schema v3: symbols PK (library_name, symbol_name) →
            # (library_id, symbol_name).  Same-nickname libraries across
            # projects each own a libraries row, so symbol uniqueness must
            # be per parent row; the old PK made the second project's sync()
            # hit a UNIQUE constraint.  SQLite cannot alter a PK — rebuild
            # the table.  Rowids are preserved so the FTS content table
            # stays valid, then the FTS index is rebuilt for good measure.
            # (Fresh databases created by create_all above already use v3.)
            sym_columns = conn.execute(text("PRAGMA table_info(symbols)")).all()
            sym_pk = [row[1] for row in sym_columns if row[5] > 0]
            if sym_pk == ["library_name", "symbol_name"]:
                log.info(
                    "symbol DB schema v2 → v3: symbols PK "
                    "(library_name, symbol_name) → (library_id, symbol_name)"
                )
                conn.execute(
                    text(
                        "CREATE TABLE symbols_new ("
                        "  library_name VARCHAR NOT NULL,"
                        "  symbol_name VARCHAR NOT NULL,"
                        "  library_id INTEGER NOT NULL "
                        "REFERENCES libraries (id) ON DELETE CASCADE,"
                        "  description VARCHAR NOT NULL,"
                        "  keywords VARCHAR NOT NULL,"
                        "  pin_count INTEGER NOT NULL,"
                        "  file_index INTEGER NOT NULL,"
                        "  PRIMARY KEY (library_id, symbol_name)"
                        ")"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO symbols_new "
                        "(rowid, library_name, symbol_name, library_id, "
                        " description, keywords, pin_count, file_index) "
                        "SELECT rowid, library_name, symbol_name, library_id, "
                        "       description, keywords, pin_count, file_index "
                        "FROM symbols"
                    )
                )
                conn.execute(text("DROP TABLE symbols"))
                conn.execute(text("ALTER TABLE symbols_new RENAME TO symbols"))
                conn.execute(text("CREATE INDEX idx_sym_library_id ON symbols (library_id)"))
                conn.execute(text("CREATE INDEX idx_sym_name ON symbols (symbol_name)"))
                conn.execute(text("CREATE INDEX idx_sym_library_name ON symbols (library_name)"))
                try:
                    conn.execute(text("INSERT INTO symbols_fts(symbols_fts) VALUES ('rebuild')"))
                except Exception as exc:
                    log.warning(f"symbols_fts rebuild failed after PK migration: {exc}")
                conn.commit()

            try:
                for statement in _DDL_FTS.split(";\n\n"):
                    stmt = statement.strip()
                    if stmt:
                        conn.execute(text(stmt))
                conn.commit()
            except Exception as exc:
                log.warning(
                    f"FTS5 not available in this SQLite build — full-text search disabled. ({exc})"
                )

    # ------------------------------------------------------------------
    # Public API — state query (used by SymbolIndexManager for sync)
    # ------------------------------------------------------------------

    def get_library_states(
        self, project: str | None = None,
    ) -> dict[str, tuple[int, float, int, str, str]]:
        """
        Return a snapshot of indexed libraries visible in *project* scope as
        ``{file_path: (id, mtime, file_size, checksum, project)}`` — the
        last element is the row's OWNING scope (``''`` = global), which the
        sync removal loop needs to avoid deleting rows it does not own.

        Scope = global libraries (``project=''``) plus the current project's
        libraries when *project* is given; ``None`` returns everything.
        """
        q = select(
            _LibraryRow.id,
            _LibraryRow.file_path,
            _LibraryRow.mtime,
            _LibraryRow.file_size,
            _LibraryRow.checksum,
            _LibraryRow.project,
        )
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(q).all()
        return {
            row.file_path: (row.id, row.mtime, row.file_size, row.checksum, row.project)
            for row in rows
        }

    # ------------------------------------------------------------------
    # Public API — write (used by SymbolIndexManager)
    # ------------------------------------------------------------------

    def save_library(
        self,
        library_name: str,
        file_path: str,
        mtime: float,
        file_size: int,
        kicad_version: str,
        symbols: list[SymbolRecord],
        checksum: str = "",
        project: str = "",
    ) -> int:
        """
        Insert or fully replace a library and its symbols in one transaction.
        Returns the number of symbols stored.
        """
        now = time.time()
        with self._Session() as session:
            # Always delete any existing row for this path (symbols are removed
            # via ON DELETE CASCADE) and insert a fresh record.  This avoids
            # any risk of stale data from a partial or in-place update.
            session.execute(
                _LibraryRow.__table__.delete().where(_LibraryRow.file_path == file_path)
            )

            lib_row = _LibraryRow(
                library_name=library_name,
                file_path=file_path,
                file_size=file_size,
                mtime=mtime,
                checksum=checksum,
                symbol_count=len(symbols),
                last_indexed=now,
                kicad_version=kicad_version,
                project=project,
            )
            session.add(lib_row)
            session.flush()  # assigns lib_row.id
            lib_id: int = lib_row.id

            if symbols:
                session.execute(
                    insert(_SymbolRow),
                    [
                        {
                            "library_name": library_name,
                            "symbol_name": sym.symbol_name,
                            "library_id": lib_id,
                            "description": sym.description,
                            "keywords": sym.keywords,
                            "pin_count": sym.pin_count,
                            "file_index": sym.file_index,
                        }
                        for sym in symbols
                    ],
                )
            session.commit()

        return len(symbols)

    def touch_library(
        self,
        lib_id: int,
        mtime: float,
        file_size: int,
        checksum: str,
    ) -> None:
        """
        Update only the file metadata (mtime, size, checksum) for a library
        whose content has not changed — avoids a full reparse.
        """
        with self._Session() as session:
            session.execute(
                _LibraryRow.__table__.update()
                .where(_LibraryRow.id == lib_id)
                .values(mtime=mtime, file_size=file_size, checksum=checksum)
            )
            session.commit()

    def delete_library(self, lib_id: int) -> None:
        """Delete a library row (symbols removed via ON DELETE CASCADE)."""
        with self._Session() as session:
            session.execute(_LibraryRow.__table__.delete().where(_LibraryRow.id == lib_id))
            session.commit()

    # ------------------------------------------------------------------
    # Public API — search
    # ------------------------------------------------------------------

    def search(
        self, query: str, limit: int = 50, project: str | None = None
    ) -> list[SymbolRecord]:
        """
        Full-text search across symbol_name, description, and keywords.
        Returns results ordered by FTS5 rank (best match first).
        Falls back to LIKE search if FTS5 is unavailable.

        *project* scope = global libraries (``project=''``) plus that
        project's libraries; ``None`` searches everything.
        """
        safe_query = self._fts_escape(query)
        params: dict[str, object] = {"q": safe_query, "lim": limit}
        if project == "":
            # Project scope: global libraries only.
            sql = text(
                """
                SELECT s.library_name, s.symbol_name, s.library_id,
                       s.description, s.keywords, s.pin_count, s.file_index
                FROM symbols_fts f
                JOIN symbols s ON s.rowid = f.rowid
                JOIN libraries lib ON s.library_id = lib.id
                WHERE symbols_fts MATCH :q AND lib.project = ''
                ORDER BY rank
                LIMIT :lim
                """
            )
        elif project:
            # Project scope: global plus the given project's libraries.
            params["proj"] = project
            sql = text(
                """
                SELECT s.library_name, s.symbol_name, s.library_id,
                       s.description, s.keywords, s.pin_count, s.file_index
                FROM symbols_fts f
                JOIN symbols s ON s.rowid = f.rowid
                JOIN libraries lib ON s.library_id = lib.id
                WHERE symbols_fts MATCH :q
                  AND (lib.project = '' OR lib.project = :proj)
                ORDER BY rank
                LIMIT :lim
                """
            )
        else:
            # No scope: search everything.
            sql = text(
                """
                SELECT s.library_name, s.symbol_name, s.library_id,
                       s.description, s.keywords, s.pin_count, s.file_index
                FROM symbols_fts f
                JOIN symbols s ON s.rowid = f.rowid
                JOIN libraries lib ON s.library_id = lib.id
                WHERE symbols_fts MATCH :q
                ORDER BY rank
                LIMIT :lim
                """
            )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, params).all()
            return [self._row_to_symbol(r) for r in rows]
        except Exception:
            log.debug("FTS5 unavailable, falling back to LIKE search")
            return self.search_by_name(query, limit=limit, project=project)

    def search_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
        project: str | None = None,
    ) -> list[SymbolRecord]:
        """
        Search symbols by name.
        exact=True  — case-insensitive exact match.
        exact=False — case-insensitive substring match.

        *project* scope = global libraries (``project=''``) plus that
        project's libraries; ``None`` searches everything.
        """
        with self._Session() as session:
            q = select(_SymbolRow)
            if exact:
                q = q.where(func.lower(_SymbolRow.symbol_name) == name.lower())
            else:
                q = q.where(
                    _SymbolRow.symbol_name.ilike(f"%{self._like_escape(name)}%", escape="\\")
                )
            clause = self._project_scope_clause(project)
            if clause is not None:
                q = q.join(_LibraryRow, _SymbolRow.library_id == _LibraryRow.id)
                q = q.where(clause)
            rows = session.execute(q.limit(limit)).scalars().all()
        return [self._orm_to_symbol(r) for r in rows]

    # ------------------------------------------------------------------
    # Public API — lookup
    # ------------------------------------------------------------------

    def get_symbol(
        self, library_name: str, symbol_name: str, project: str | None = None
    ) -> SymbolRecord | None:
        """Look up a single symbol by (library_name, symbol_name), scoped to
        *project* when given — a project-owned library row shadows the
        same-nickname global row (see ``_effective_library_ids``)."""
        ids = self._effective_library_ids(library_name, project)
        if not ids:
            return None
        with self._Session() as session:
            row = session.execute(
                select(_SymbolRow).where(
                    _SymbolRow.library_id.in_(ids),
                    _SymbolRow.symbol_name == symbol_name,
                )
            ).scalar_one_or_none()
        return self._orm_to_symbol(row) if row else None

    def get_library_symbols(
        self, library_name: str, project: str | None = None
    ) -> list[SymbolRecord]:
        """Return all symbols in a library, ordered by their position in the
        file, scoped to *project* when given — a project-owned row shadows
        the same-nickname global row (see ``_effective_library_ids``)."""
        ids = self._effective_library_ids(library_name, project)
        if not ids:
            return []
        with self._Session() as session:
            rows = session.execute(
                select(_SymbolRow)
                .where(
                    _SymbolRow.library_name == library_name,
                    _SymbolRow.library_id.in_(ids),
                )
                .order_by(_SymbolRow.file_index)
            ).scalars().all()
        return [self._orm_to_symbol(r) for r in rows]

    def get_all_symbols(self, project: str | None = None) -> list[SymbolRecord]:
        """Return every indexed symbol, ordered by library then position,
        scoped to *project* (global plus project libraries) when given."""
        q = select(_SymbolRow)
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.join(_LibraryRow, _SymbolRow.library_id == _LibraryRow.id)
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(
                q.order_by(_SymbolRow.library_name, _SymbolRow.file_index)
            ).scalars().all()
        return [self._orm_to_symbol(r) for r in rows]

    def get_all_libraries(self, project: str | None = None) -> list[LibraryRecord]:
        """Return indexed library records scoped to *project* (global plus
        project libraries when given; everything when ``None``), ordered
        alphabetically."""
        q = select(_LibraryRow)
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(q.order_by(_LibraryRow.library_name)).scalars().all()
        return [self._orm_to_library(r) for r in rows]

    def _effective_library_ids(self, library_name: str, project: str | None) -> list[int]:
        """Ids of the *library_name* rows visible in *project* scope.

        Shadow semantics (mirror of the index reader): within a project
        scope the project's own row of a nickname shadows the global row —
        KiCad lets a project sym-lib-table override a global library of the
        same name, and the reader drops the shadowed global entry.  So a
        project scope resolves to the project row(s) when any exist, else
        the global row(s).  Global scope (``''``) → global rows only;
        ``None`` → every matching row.
        """
        with self._Session() as session:
            q = select(_LibraryRow.id, _LibraryRow.project).where(
                _LibraryRow.library_name == library_name
            )
            if project == "":
                q = q.where(_LibraryRow.project == "")
            elif project is not None:
                q = q.where(
                    or_(_LibraryRow.project == "", _LibraryRow.project == project)
                )
            pairs = session.execute(q).all()
        if project is None:
            return [p[0] for p in pairs]
        if project:
            proj_ids = [p[0] for p in pairs if p[1] == project]
            if proj_ids:
                return proj_ids
        return [p[0] for p in pairs if p[1] == ""]

    def get_library_by_name(
        self, name: str, project: str | None = None
    ) -> LibraryRecord | None:
        """Look up a single library record by library_name, scoped to
        *project* when given.  In a project scope a project-owned row
        shadows a same-nickname global row (see ``_effective_library_ids``);
        ``None`` returns the first matching row deterministically."""
        with self._Session() as session:
            q = select(_LibraryRow).where(_LibraryRow.library_name == name)
            if project is None:
                # Un-scoped legacy lookup: same-nickname rows can exist across
                # scopes — pick the first deterministically instead of raising.
                q = q.order_by(_LibraryRow.id).limit(1)
            elif project == "":
                q = q.where(_LibraryRow.project == "")
            else:
                # Shadow semantics: the project's own row wins over the global
                # row of the same nickname.
                q = q.where(
                    or_(_LibraryRow.project == "", _LibraryRow.project == project)
                )
                rows = session.execute(q).scalars().all()
                if not rows:
                    return None
                proj_row = next((r for r in rows if r.project == project), None)
                row = proj_row or rows[0]
                return self._orm_to_library(row)
            row = session.execute(q).scalar_one_or_none()
        return self._orm_to_library(row) if row else None

    def get_library_by_name_exact(self, name: str, project: str) -> LibraryRecord | None:
        """Look up a library row by nickname **and** exact project ownership.

        Unlike ``get_library_by_name`` (scope-union: global + project), this
        matches ``project`` exactly: ``""`` finds only the global row, a
        project id only that project's row.  A same-nickname row owned by a
        different project is never returned — used for ownership-scoped
        deletes.
        """
        with self._Session() as session:
            row = session.execute(
                select(_LibraryRow).where(
                    _LibraryRow.library_name == name,
                    _LibraryRow.project == project,
                )
            ).scalar_one_or_none()
        return self._orm_to_library(row) if row else None

    def get_symbol_file_index(
        self, library_name: str, symbol_name: str, project: str | None = None
    ) -> int | None:
        """Return the 0-based file_index of a symbol, or None if not found,
        scoped to *project* when given — a project-owned library row shadows
        the same-nickname global row (see ``_effective_library_ids``)."""
        ids = self._effective_library_ids(library_name, project)
        if not ids:
            return None
        with self._Session() as session:
            row = session.execute(
                select(_SymbolRow.file_index).where(
                    _SymbolRow.library_id.in_(ids),
                    _SymbolRow.symbol_name == symbol_name,
                )
            ).scalar_one_or_none()
        return int(row) if row is not None else None

    def get_stats(self, project: str | None = None) -> DbStats:
        """Return summary statistics about the database, scoped to *project*
        (global plus project libraries when given; everything when ``None``)."""
        lib_q = select(func.count()).select_from(_LibraryRow)
        sym_q = select(func.count()).select_from(_SymbolRow)
        clause = self._project_scope_clause(project)
        if clause is not None:
            lib_q = lib_q.where(clause)
            sym_q = sym_q.join(_LibraryRow, _SymbolRow.library_id == _LibraryRow.id).where(clause)
        with self._Session() as session:
            lib_count: int = session.execute(lib_q).scalar_one()
            sym_count: int = session.execute(sym_q).scalar_one()
            last_sync = session.execute(select(func.max(_LibraryRow.last_indexed))).scalar_one()
        return DbStats(
            library_count=lib_count,
            symbol_count=sym_count,
            last_sync=float(last_sync) if last_sync else 0.0,
            db_path=self._db_path,
        )

    def close(self) -> None:
        """Dispose the engine (closes all pooled connections)."""
        self._engine.dispose()

    # ------------------------------------------------------------------
    # Internal helpers — query sanitization
    # ------------------------------------------------------------------

    @staticmethod
    def _fts_escape(query: str) -> str:
        """
        Convert a plain user query string into a safe FTS5 MATCH expression.
        Each whitespace-separated token is double-quoted to prevent FTS5
        syntax errors on inputs like 'C++' or '24V'.
        """
        tokens = re.split(r"\s+", query.strip())
        escaped = " ".join(f'"{t}"' for t in tokens if t)
        return escaped if escaped else '""'

    @staticmethod
    def _like_escape(s: str) -> str:
        """Escape LIKE special characters in a user-supplied substring."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _project_scope_clause(project: str | None = None):
        """A WHERE clause fragment limiting rows to *project* scope.

        Scope = global libraries (``project=''``) plus the given project's
        libraries.  An empty string means global-only scope (filters out all
        project-local rows); ``None`` (no project context) returns ``None``,
        meaning the caller should not filter at all.
        """
        if project is None:
            return None
        if project == "":
            return _LibraryRow.project == ""
        return or_(_LibraryRow.project == "", _LibraryRow.project == project)

    # ------------------------------------------------------------------
    # Internal helpers — ORM row → public dataclass
    # ------------------------------------------------------------------

    @staticmethod
    def _orm_to_symbol(row: _SymbolRow) -> SymbolRecord:
        return SymbolRecord(
            library_name=row.library_name,
            symbol_name=row.symbol_name,
            library_id=row.library_id,
            description=row.description,
            keywords=row.keywords,
            pin_count=row.pin_count,
            file_index=row.file_index,
        )

    @staticmethod
    def _orm_to_library(row: _LibraryRow) -> LibraryRecord:
        return LibraryRecord(
            id=row.id,
            library_name=row.library_name,
            file_path=row.file_path,
            file_size=row.file_size,
            mtime=row.mtime,
            checksum=row.checksum,
            symbol_count=row.symbol_count,
            last_indexed=row.last_indexed,
            kicad_version=row.kicad_version,
            project=row.project,
        )

    # FTS search returns raw DB rows (not ORM objects) — handle separately.
    @staticmethod
    def _row_to_symbol(row) -> SymbolRecord:
        return SymbolRecord(
            library_name=row.library_name,
            symbol_name=row.symbol_name,
            library_id=row.library_id,
            description=row.description,
            keywords=row.keywords,
            pin_count=row.pin_count,
            file_index=row.file_index,
        )
