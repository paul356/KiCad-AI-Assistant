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
    String,
    create_engine,
    event,
    func,
    insert,
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


class _SymbolRow(_Base):
    __tablename__ = "symbols"

    library_name = Column(String, nullable=False, primary_key=True)
    symbol_name = Column(String, nullable=False, primary_key=True)
    library_id = Column(Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False)
    description = Column(String, nullable=False, default="")
    keywords = Column(String, nullable=False, default="")
    pin_count = Column(Integer, nullable=False, default=0)
    file_index = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_sym_library_id", "library_id"),
        Index("idx_sym_name", "symbol_name"),
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

        # Enable WAL mode and foreign keys on every new connection.
        @event.listens_for(self._engine, "connect")
        def _set_pragmas(conn, _record):
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")

        self._Session = sessionmaker(bind=self._engine)
        self._apply_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        """Create ORM tables and FTS5 virtual table / triggers."""
        _Base.metadata.create_all(self._engine)
        with self._engine.connect() as conn:
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

    def get_library_states(self) -> dict[str, tuple[int, float, int, str]]:
        """
        Return a snapshot of all indexed libraries as
        ``{file_path: (id, mtime, file_size, checksum)}``.
        """
        with self._Session() as session:
            rows = session.execute(
                select(
                    _LibraryRow.id,
                    _LibraryRow.file_path,
                    _LibraryRow.mtime,
                    _LibraryRow.file_size,
                    _LibraryRow.checksum,
                )
            ).all()
        return {row.file_path: (row.id, row.mtime, row.file_size, row.checksum) for row in rows}

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

    def search(self, query: str, limit: int = 50) -> list[SymbolRecord]:
        """
        Full-text search across symbol_name, description, and keywords.
        Returns results ordered by FTS5 rank (best match first).
        Falls back to LIKE search if FTS5 is unavailable.
        """
        safe_query = self._fts_escape(query)
        sql = text(
            """
            SELECT s.library_name, s.symbol_name, s.library_id,
                   s.description, s.keywords, s.pin_count, s.file_index
            FROM symbols_fts f
            JOIN symbols s ON s.rowid = f.rowid
            WHERE symbols_fts MATCH :q
            ORDER BY rank
            LIMIT :lim
            """
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, {"q": safe_query, "lim": limit}).all()
            return [self._row_to_symbol(r) for r in rows]
        except Exception:
            log.debug("FTS5 unavailable, falling back to LIKE search")
            return self.search_by_name(query, limit=limit)

    def search_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
    ) -> list[SymbolRecord]:
        """
        Search symbols by name.
        exact=True  — case-insensitive exact match.
        exact=False — case-insensitive substring match.
        """
        with self._Session() as session:
            q = select(_SymbolRow)
            if exact:
                q = q.where(func.lower(_SymbolRow.symbol_name) == name.lower())
            else:
                q = q.where(
                    _SymbolRow.symbol_name.ilike(f"%{self._like_escape(name)}%", escape="\\")
                )
            rows = session.execute(q.limit(limit)).scalars().all()
        return [self._orm_to_symbol(r) for r in rows]

    # ------------------------------------------------------------------
    # Public API — lookup
    # ------------------------------------------------------------------

    def get_symbol(self, library_name: str, symbol_name: str) -> SymbolRecord | None:
        """Look up a single symbol by (library_name, symbol_name)."""
        with self._Session() as session:
            row = session.execute(
                select(_SymbolRow).where(
                    _SymbolRow.library_name == library_name,
                    _SymbolRow.symbol_name == symbol_name,
                )
            ).scalar_one_or_none()
        return self._orm_to_symbol(row) if row else None

    def get_library_symbols(self, library_name: str) -> list[SymbolRecord]:
        """Return all symbols in a library, ordered by their position in the file."""
        with self._Session() as session:
            rows = (
                session.execute(
                    select(_SymbolRow)
                    .where(_SymbolRow.library_name == library_name)
                    .order_by(_SymbolRow.file_index)
                )
                .scalars()
                .all()
            )
        return [self._orm_to_symbol(r) for r in rows]

    def get_all_symbols(self) -> list[SymbolRecord]:
        """Return every indexed symbol, ordered by library then position."""
        with self._Session() as session:
            rows = (
                session.execute(
                    select(_SymbolRow).order_by(_SymbolRow.library_name, _SymbolRow.file_index)
                )
                .scalars()
                .all()
            )
        return [self._orm_to_symbol(r) for r in rows]

    def get_all_libraries(self) -> list[LibraryRecord]:
        """Return all indexed library records, ordered alphabetically."""
        with self._Session() as session:
            rows = (
                session.execute(select(_LibraryRow).order_by(_LibraryRow.library_name))
                .scalars()
                .all()
            )
        return [self._orm_to_library(r) for r in rows]

    def get_library_by_name(self, name: str) -> LibraryRecord | None:
        """Look up a single library record by library_name."""
        with self._Session() as session:
            row = session.execute(
                select(_LibraryRow).where(_LibraryRow.library_name == name)
            ).scalar_one_or_none()
        return self._orm_to_library(row) if row else None

    def get_symbol_file_index(self, library_name: str, symbol_name: str) -> int | None:
        """Return the 0-based file_index of a symbol, or None if not found."""
        with self._Session() as session:
            row = session.execute(
                select(_SymbolRow.file_index).where(
                    _SymbolRow.library_name == library_name,
                    _SymbolRow.symbol_name == symbol_name,
                )
            ).scalar_one_or_none()
        return int(row) if row is not None else None

    def get_stats(self) -> DbStats:
        """Return summary statistics about the database."""
        with self._Session() as session:
            lib_count: int = session.execute(
                select(func.count()).select_from(_LibraryRow)
            ).scalar_one()
            sym_count: int = session.execute(
                select(func.count()).select_from(_SymbolRow)
            ).scalar_one()
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
