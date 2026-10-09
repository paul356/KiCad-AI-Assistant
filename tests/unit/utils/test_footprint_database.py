"""Tests for FootprintDatabase — SQLAlchemy/SQLite storage layer for the
indexed footprint libraries (full-text index sync in particular)."""

import pytest

from kcaa.utils.footprint_database import FootprintDatabase, FootprintRecord


def _make_record(lib_name: str, fp_name: str, **kwargs) -> FootprintRecord:
    defaults = {
        "library_name": lib_name,
        "footprint_name": fp_name,
        "library_id": 0,
        "description": "sensor",
        "tags": "passive",
        "attr": "smd",
        "pad_count": 2,
        "has_3d_model": False,
    }
    defaults.update(kwargs)
    return FootprintRecord(**defaults)


@pytest.fixture
def db():
    d = FootprintDatabase(":memory:")
    yield d
    d.close()


class TestFTSUpdateTrigger:
    """A raw UPDATE on the footprints table (no save_library round trip) must
    keep footprints_fts in sync: stale tokens disappear, new tokens appear."""

    @pytest.fixture(autouse=True)
    def _populate(self, db):
        self.db = db
        db.save_library(
            "Lib",
            "${KICAD10_FOOTPRINT_DIR}/Lib.pretty",
            "/tmp/Lib.pretty",
            "desc",
            "checksum",
            [
                _make_record("Lib", "R_0402", description="resistor smd"),
                _make_record("Lib", "C_0402", description="capacitor smd", tags="capacitor"),
            ],
        )

    def test_update_refreshes_fts_description(self):
        from sqlalchemy import text as sql_text

        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text(
                    "UPDATE footprints SET description = 'new token XYZ' "
                    "WHERE footprint_name = 'R_0402'"
                )
            )
            conn.commit()

        # The new description token is searchable with the full new row.
        found = self.db.search("XYZ")
        assert any(f.footprint_name == "R_0402" and f.description == "new token XYZ" for f in found)

        # A second in-place update removes the previous description token.
        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text(
                    "UPDATE footprints SET description = 'scrubbed clean' "
                    "WHERE footprint_name = 'R_0402'"
                )
            )
            conn.commit()
        assert self.db.search("XYZ") == []
        assert any(f.footprint_name == "R_0402" for f in self.db.search("scrubbed"))

    def test_update_refreshes_fts_tags(self):
        from sqlalchemy import text as sql_text

        # 'KWX' appears in no other column — only the tags column is indexed
        # with it, so it isolates the tags-update path.
        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text(
                    "UPDATE footprints SET tags = 'special KWX' WHERE footprint_name = 'C_0402'"
                )
            )
            conn.commit()

        assert any(f.footprint_name == "C_0402" for f in self.db.search("KWX"))

        with self.db._engine.connect() as conn:
            conn.execute(
                sql_text("UPDATE footprints SET tags = 'gone' WHERE footprint_name = 'C_0402'")
            )
            conn.commit()
        assert self.db.search("KWX") == []
