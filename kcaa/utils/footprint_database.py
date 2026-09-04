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
class FpLibraryRecord:
    id: int
    library_name: str  # nickname (stable key, API identifier)
    raw_uri: str  # unexpanded URI e.g. "${KICAD10_FOOTPRINT_DIR}/Foo.pretty"
    dir_path: str  # resolved runtime path to .pretty directory
    description: str
    checksum: str  # SHA-256 of sorted "filename:mtime:size\n" manifest
    footprint_count: int
    last_indexed: float
    project: str = ""  # "" = global library; otherwise project identifier


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
    project = Column(String, nullable=False, default="", server_default="''")
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
            # Schema v2: fp_libraries gains a `project` column.  Old databases
            # are upgraded in place (ALTER) — data is preserved.  v1 databases
            # have no project column; anything newer already matches the ORM.
            columns = conn.execute(text("PRAGMA table_info(fp_libraries)")).all()
            col_names = {row[1] for row in columns}
            if "project" not in col_names and columns:
                log.info("footprint DB schema v1 → v2: adding fp_libraries.project column")
                conn.execute(
                    text("ALTER TABLE fp_libraries ADD COLUMN project VARCHAR NOT NULL DEFAULT ''")
                )
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
    # Public API — state query (used by FootprintIndexManager for sync)
    # ------------------------------------------------------------------

    def get_library_states(self, project: str | None = None) -> dict[str, tuple[int, str, str]]:
        """Return a snapshot of indexed libraries visible in *project* scope as
        ``{library_name: (id, checksum, dir_path)}``.

        Scope = global libraries (``project=''``) plus the current project's
        libraries when *project* is given; ``None`` returns everything.
        """
        q = select(
            _FpLibraryRow.id,
            _FpLibraryRow.library_name,
            _FpLibraryRow.checksum,
            _FpLibraryRow.dir_path,
        )
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(q).all()
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
        project: str = "",
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
                project=project,
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

    def search(
        self, query: str, limit: int = 50, project: str | None = None
    ) -> list["FootprintRecord"]:
        """Full-text search across footprint_name, description, and tags.
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
                SELECT f.library_name, f.footprint_name, f.library_id,
                       f.description, f.tags, f.attr, f.pad_count, f.has_3d_model
                FROM footprints_fts fts
                JOIN footprints f ON f.rowid = fts.rowid
                JOIN fp_libraries lib ON f.library_id = lib.id
                WHERE footprints_fts MATCH :q AND lib.project = ''
                ORDER BY rank
                LIMIT :lim
                """
            )
        elif project:
            # Project scope: global plus the given project's libraries.
            params["proj"] = project
            sql = text(
                """
                SELECT f.library_name, f.footprint_name, f.library_id,
                       f.description, f.tags, f.attr, f.pad_count, f.has_3d_model
                FROM footprints_fts fts
                JOIN footprints f ON f.rowid = fts.rowid
                JOIN fp_libraries lib ON f.library_id = lib.id
                WHERE footprints_fts MATCH :q
                  AND (lib.project = '' OR lib.project = :proj)
                ORDER BY rank
                LIMIT :lim
                """
            )
        else:
            # No scope: search everything.
            sql = text(
                """
                SELECT f.library_name, f.footprint_name, f.library_id,
                       f.description, f.tags, f.attr, f.pad_count, f.has_3d_model
                FROM footprints_fts fts
                JOIN footprints f ON f.rowid = fts.rowid
                JOIN fp_libraries lib ON f.library_id = lib.id
                WHERE footprints_fts MATCH :q
                ORDER BY rank
                LIMIT :lim
                """
            )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, params).all()
            return [self._row_to_footprint(r) for r in rows]
        except Exception:
            log.debug("FTS5 unavailable, falling back to LIKE search")
            return self.search_by_name(query, limit=limit, project=project)

    def search_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
        project: str | None = None,
    ) -> list["FootprintRecord"]:
        """Search footprints by name substring or exact match.

        *project* scope = global libraries (``project=''``) plus that
        project's libraries; ``None`` searches everything.
        """
        with self._Session() as session:
            q = select(_FootprintRow)
            if exact:
                q = q.where(func.lower(_FootprintRow.footprint_name) == name.lower())
            else:
                q = q.where(
                    _FootprintRow.footprint_name.ilike(f"%{self._like_escape(name)}%", escape="\\")
                )
            clause = self._project_scope_clause(project)
            if clause is not None:
                q = q.join(_FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id)
                q = q.where(clause)
            rows = session.execute(q.limit(limit)).scalars().all()
        return [self._orm_to_footprint(r) for r in rows]

    # ------------------------------------------------------------------
    # Public API — lookup
    # ------------------------------------------------------------------

    def get_footprint(
        self,
        library_name: str,
        footprint_name: str,
        project: str | None = None,
    ) -> "FootprintRecord | None":
        """Look up a single footprint by (library_name, footprint_name),
        scoped to *project* (global plus project libraries) when given."""
        with self._Session() as session:
            q = select(_FootprintRow).where(
                _FootprintRow.library_name == library_name,
                _FootprintRow.footprint_name == footprint_name,
            )
            clause = self._project_scope_clause(project)
            if clause is not None:
                q = q.join(_FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id)
                q = q.where(clause)
            row = session.execute(q).scalar_one_or_none()
        return self._orm_to_footprint(row) if row else None

    def get_library_footprints(
        self, library_name: str, project: str | None = None
    ) -> list["FootprintRecord"]:
        """Return all footprints in a library, ordered by name, scoped to
        *project* (global plus project libraries) when given."""
        with self._Session() as session:
            q = select(_FootprintRow).where(_FootprintRow.library_name == library_name)
            clause = self._project_scope_clause(project)
            if clause is not None:
                q = q.join(_FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id)
                q = q.where(clause)
            rows = session.execute(q.order_by(_FootprintRow.footprint_name)).scalars().all()
        return [self._orm_to_footprint(r) for r in rows]

    def get_all_libraries(self, project: str | None = None) -> list["FpLibraryRecord"]:
        """Return indexed library records scoped to *project* (global plus
        project libraries when given; everything when ``None``), ordered
        alphabetically."""
        q = select(_FpLibraryRow)
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(q.order_by(_FpLibraryRow.library_name)).scalars().all()
        return [self._orm_to_library(r) for r in rows]

    def library_name_exists(self, library_name: str) -> bool:
        """True if any library row (any project) has this nickname.

        Library nicknames are globally unique (a same-named library would be
        shadowed in KiCad), so this deliberately does NOT scope by project.
        """
        with self._Session() as session:
            row = session.execute(
                select(_FpLibraryRow.id).where(_FpLibraryRow.library_name == library_name)
            ).first()
        return row is not None

    def get_all_footprint_names(self, project: str | None = None) -> set[str]:
        """Return every footprint name indexed in *project* scope (global plus
        project libraries when given; everything when ``None``)."""
        q = select(_FootprintRow.footprint_name).join(
            _FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id
        )
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            rows = session.execute(q).scalars().all()
        return set(rows)

    def get_all_library_footprints(self, project: str | None = None) -> set[tuple[str, str]]:
        """Return every ``(library_name, footprint_name)`` pair indexed in
        *project* scope (global plus project libraries when given; everything
        when ``None``)."""
        q = select(_FootprintRow.library_name, _FootprintRow.footprint_name).join(
            _FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id
        )
        clause = self._project_scope_clause(project)
        if clause is not None:
            q = q.where(clause)
        with self._Session() as session:
            return set(session.execute(q).all())

    def get_stats(self, project: str | None = None) -> "DbStats":
        """Return summary statistics about the database, scoped to *project*
        (global plus project libraries when given; everything when ``None``)."""
        lib_q = select(func.count()).select_from(_FpLibraryRow)
        fp_q = select(func.count()).select_from(_FootprintRow)
        clause = self._project_scope_clause(project)
        if clause is not None:
            lib_q = lib_q.where(clause)
            fp_q = fp_q.join(_FpLibraryRow, _FootprintRow.library_id == _FpLibraryRow.id).where(
                clause
            )
        with self._Session() as session:
            lib_count: int = session.execute(lib_q).scalar_one()
            fp_count: int = session.execute(fp_q).scalar_one()
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
            return _FpLibraryRow.project == ""
        return or_(_FpLibraryRow.project == "", _FpLibraryRow.project == project)

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
            project=row.project,
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
