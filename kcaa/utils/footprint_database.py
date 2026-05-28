"""
SQLite-backed footprint index database for KiCad footprint libraries.

Uses SQLAlchemy 2.x for all database operations.

Tables
------
fp_libraries  -- one row per .pretty directory (id, library_name, ...)
footprints    -- one row per .kicad_mod file (library_name, footprint_name, ...)
footprints_fts -- FTS5 virtual table mirroring footprints for full-text search

Cache invalidation
------------------
Each fp_libraries row stores a SHA-256 checksum of the directory's content
fingerprint: SHA-256 of the sorted "filename:mtime:size\\n" manifest for all
.kicad_mod files in the .pretty directory.  This detects additions, deletions,
and in-place modifications without reading any file content.

Note on FTS5
------------
SQLAlchemy has no native support for SQLite FTS5 virtual tables or their
associated triggers.  The FTS5 DDL and MATCH queries use ``text()`` executed
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
class FpLibraryRecord:
    id: int
    library_name: str  # nickname (stable key, API identifier)
    raw_uri: str  # unexpanded URI e.g. "${KICAD10_FOOTPRINT_DIR}/Foo.pretty"
    dir_path: str  # resolved runtime path to .pretty directory
    description: str
    checksum: str  # SHA-256 of sorted "filename:mtime:size\n" manifest
    footprint_count: int
    last_indexed: float


@dataclass
class FootprintRecord:
    library_name: str
    footprint_name: str
    library_id: int
    description: str
    tags: str
    attr: str  # "smd", "through_hole", or ""
    pad_count: int
    has_3d_model: bool


@dataclass
class DbStats:
    library_count: int
    footprint_count: int
    last_sync: float  # Unix timestamp; 0.0 if never synced
    db_path: str


# ---------------------------------------------------------------------------
# ORM models  (internal — prefixed with _ to signal non-public)
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class _FpLibraryRow(_Base):
    __tablename__ = "fp_libraries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_name = Column(String, nullable=False, unique=True)
    raw_uri = Column(String, nullable=False, default="")
    dir_path = Column(String, nullable=False, default="")
    description = Column(String, nullable=False, default="")
    checksum = Column(String, nullable=False, default="")
    footprint_count = Column(Integer, nullable=False, default=0)
    last_indexed = Column(Float, nullable=False, default=0.0)


class _FootprintRow(_Base):
    __tablename__ = "footprints"

    library_name = Column(String, nullable=False, primary_key=True)
    footprint_name = Column(String, nullable=False, primary_key=True)
    library_id = Column(Integer, ForeignKey("fp_libraries.id", ondelete="CASCADE"), nullable=False)
    description = Column(String, nullable=False, default="")
    tags = Column(String, nullable=False, default="")
    attr = Column(String, nullable=False, default="")
    pad_count = Column(Integer, nullable=False, default=0)
    has_3d_model = Column(Integer, nullable=False, default=0)  # 0/1 boolean

    __table_args__ = (
        Index("idx_fp_library_id", "library_id"),
        Index("idx_fp_name", "footprint_name"),
    )


# FTS5 virtual table + trigger DDL — executed once at schema setup time.
_DDL_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS footprints_fts USING fts5(
    library_name,
    footprint_name,
    description,
    tags,
    content='footprints',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS footprints_ai AFTER INSERT ON footprints BEGIN
    INSERT INTO footprints_fts(rowid, library_name, footprint_name, description, tags)
    VALUES (new.rowid, new.library_name, new.footprint_name, new.description, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS footprints_ad AFTER DELETE ON footprints BEGIN
    INSERT INTO footprints_fts(footprints_fts, rowid, library_name, footprint_name, description, tags)
    VALUES ('delete', old.rowid, old.library_name, old.footprint_name, old.description, old.tags);
END;

"""


# ---------------------------------------------------------------------------
# FootprintDatabase
# ---------------------------------------------------------------------------


class FootprintDatabase:
    """SQLAlchemy-backed store for KiCad footprint library index data."""

    def __init__(self, db_path: str):
        """Open (or create) the database at db_path."""
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path

        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self._engine, "connect")
        def _set_pragmas(conn, _record):
            cursor = conn.execute("PRAGMA journal_mode = WAL")
            result = cursor.fetchone()
            if result and result[0].lower() not in ("wal", "memory"):
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "PRAGMA journal_mode=WAL not applied; got %r", result[0]
                )
            conn.execute("PRAGMA foreign_keys = ON")
            fk_result = conn.execute("PRAGMA foreign_keys").fetchone()
            if not fk_result or fk_result[0] != 1:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "PRAGMA foreign_keys=ON not applied; got %r", fk_result
                )

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
    # Public API — state query (used by FootprintIndexManager for sync)
    # ------------------------------------------------------------------

    def get_library_states(self) -> dict[str, tuple[int, str, str]]:
        """Return a snapshot of all indexed libraries as
        ``{library_name: (id, checksum, dir_path)}``.
        """
        with self._Session() as session:
            rows = session.execute(
                select(
                    _FpLibraryRow.id,
                    _FpLibraryRow.library_name,
                    _FpLibraryRow.checksum,
                    _FpLibraryRow.dir_path,
                )
            ).all()
        return {row.library_name: (row.id, row.checksum, row.dir_path) for row in rows}

    # ------------------------------------------------------------------
    # Public API — write (used by FootprintIndexManager)
    # ------------------------------------------------------------------

    def save_library(
        self,
        library_name: str,
        raw_uri: str,
        dir_path: str,
        description: str,
        checksum: str,
        footprints: list["FootprintRecord"],
    ) -> int:
        """Insert or fully replace a library and its footprints.
        Returns the number of footprints stored.
        """
        now = time.time()
        with self._Session() as session:
            session.execute(
                _FpLibraryRow.__table__.delete().where(_FpLibraryRow.library_name == library_name)
            )

            lib_row = _FpLibraryRow(
                library_name=library_name,
                raw_uri=raw_uri,
                dir_path=dir_path,
                description=description,
                checksum=checksum,
                footprint_count=len(footprints),
                last_indexed=now,
            )
            session.add(lib_row)
            session.flush()
            lib_id: int = lib_row.id

            if footprints:
                session.execute(
                    insert(_FootprintRow),
                    [
                        {
                            "library_name": library_name,
                            "footprint_name": fp.footprint_name,
                            "library_id": lib_id,
                            "description": fp.description,
                            "tags": fp.tags,
                            "attr": fp.attr,
                            "pad_count": fp.pad_count,
                            "has_3d_model": int(fp.has_3d_model),
                        }
                        for fp in footprints
                    ],
                )
            session.commit()

        return len(footprints)

    def touch_library(
        self,
        lib_id: int,
        checksum: str,
        dir_path: str,
    ) -> None:
        """Update only the checksum and dir_path without reparsing footprints."""
        with self._Session() as session:
            session.execute(
                _FpLibraryRow.__table__.update()
                .where(_FpLibraryRow.id == lib_id)
                .values(checksum=checksum, dir_path=dir_path)
            )
            session.commit()

    def delete_library(self, lib_id: int) -> None:
        """Delete a library row (footprints removed via ON DELETE CASCADE)."""
        with self._Session() as session:
            session.execute(_FpLibraryRow.__table__.delete().where(_FpLibraryRow.id == lib_id))
            session.commit()

    # ------------------------------------------------------------------
    # Public API — search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 50) -> list["FootprintRecord"]:
        """Full-text search across footprint_name, description, and tags.
        Falls back to LIKE search if FTS5 is unavailable.
        """
        safe_query = self._fts_escape(query)
        sql = text(
            """
            SELECT f.library_name, f.footprint_name, f.library_id,
                   f.description, f.tags, f.attr, f.pad_count, f.has_3d_model
            FROM footprints_fts fts
            JOIN footprints f ON f.rowid = fts.rowid
            WHERE footprints_fts MATCH :q
            ORDER BY rank
            LIMIT :lim
            """
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, {"q": safe_query, "lim": limit}).all()
            return [self._row_to_footprint(r) for r in rows]
        except Exception:
            log.debug("FTS5 unavailable, falling back to LIKE search")
            return self.search_by_name(query, limit=limit)

    def search_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
    ) -> list["FootprintRecord"]:
        """Search footprints by name substring or exact match."""
        with self._Session() as session:
            q = select(_FootprintRow)
            if exact:
                q = q.where(func.lower(_FootprintRow.footprint_name) == name.lower())
            else:
                q = q.where(
                    _FootprintRow.footprint_name.ilike(f"%{self._like_escape(name)}%", escape="\\")
                )
            rows = session.execute(q.limit(limit)).scalars().all()
        return [self._orm_to_footprint(r) for r in rows]

    # ------------------------------------------------------------------
    # Public API — lookup
    # ------------------------------------------------------------------

    def get_footprint(self, library_name: str, footprint_name: str) -> "FootprintRecord | None":
        """Look up a single footprint by (library_name, footprint_name)."""
        with self._Session() as session:
            row = session.execute(
                select(_FootprintRow).where(
                    _FootprintRow.library_name == library_name,
                    _FootprintRow.footprint_name == footprint_name,
                )
            ).scalar_one_or_none()
        return self._orm_to_footprint(row) if row else None

    def get_library_footprints(self, library_name: str) -> list["FootprintRecord"]:
        """Return all footprints in a library, ordered by name."""
        with self._Session() as session:
            rows = (
                session.execute(
                    select(_FootprintRow)
                    .where(_FootprintRow.library_name == library_name)
                    .order_by(_FootprintRow.footprint_name)
                )
                .scalars()
                .all()
            )
        return [self._orm_to_footprint(r) for r in rows]

    def get_all_libraries(self) -> list["FpLibraryRecord"]:
        """Return all indexed library records, ordered alphabetically."""
        with self._Session() as session:
            rows = (
                session.execute(select(_FpLibraryRow).order_by(_FpLibraryRow.library_name))
                .scalars()
                .all()
            )
        return [self._orm_to_library(r) for r in rows]

    def get_stats(self) -> "DbStats":
        """Return summary statistics about the database."""
        with self._Session() as session:
            lib_count: int = session.execute(
                select(func.count()).select_from(_FpLibraryRow)
            ).scalar_one()
            fp_count: int = session.execute(
                select(func.count()).select_from(_FootprintRow)
            ).scalar_one()
            last_sync = session.execute(select(func.max(_FpLibraryRow.last_indexed))).scalar_one()
        return DbStats(
            library_count=lib_count,
            footprint_count=fp_count,
            last_sync=float(last_sync) if last_sync else 0.0,
            db_path=self._db_path,
        )

    def close(self) -> None:
        """Dispose the engine (closes all pooled connections)."""
        self._engine.dispose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fts_escape(query: str) -> str:
        """Convert a plain user query into a safe FTS5 MATCH expression."""
        tokens = re.split(r"\s+", query.strip())
        # Strip double-quotes from each token to avoid malformed FTS5 MATCH syntax
        cleaned = [t.replace('"', "") for t in tokens if t]
        escaped = " ".join(f'"{t}"' for t in cleaned if t)
        return escaped if escaped else '""'

    @staticmethod
    def _like_escape(s: str) -> str:
        """Escape LIKE special characters in a user-supplied substring."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _orm_to_footprint(row: _FootprintRow) -> "FootprintRecord":
        return FootprintRecord(
            library_name=row.library_name,
            footprint_name=row.footprint_name,
            library_id=row.library_id,
            description=row.description,
            tags=row.tags,
            attr=row.attr,
            pad_count=row.pad_count,
            has_3d_model=bool(row.has_3d_model),
        )

    @staticmethod
    def _orm_to_library(row: _FpLibraryRow) -> "FpLibraryRecord":
        return FpLibraryRecord(
            id=row.id,
            library_name=row.library_name,
            raw_uri=row.raw_uri,
            dir_path=row.dir_path,
            description=row.description,
            checksum=row.checksum,
            footprint_count=row.footprint_count,
            last_indexed=row.last_indexed,
        )

    @staticmethod
    def _row_to_footprint(row) -> "FootprintRecord":
        """Convert a raw DB row (from FTS query) to FootprintRecord."""
        return FootprintRecord(
            library_name=row.library_name,
            footprint_name=row.footprint_name,
            library_id=row.library_id,
            description=row.description,
            tags=row.tags,
            attr=row.attr,
            pad_count=row.pad_count,
            has_3d_model=bool(row.has_3d_model),
        )
