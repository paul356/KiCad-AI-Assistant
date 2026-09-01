"""
Unit tests for:
  - kcaa/utils/pcb_library_utils.py  (Table-type fix + attr/has_3d_model)
  - kcaa/utils/footprint_database.py
  - kcaa/utils/footprint_index_manager.py
"""

import os

import pytest

from kcaa.utils.footprint_database import (
    FootprintDatabase,
    FootprintRecord,
)
from kcaa.utils.footprint_index_manager import FootprintIndexManager
from kcaa.utils.pcb_library_utils import (
    _build_env_map,
    _parse_fp_lib_table_recursive,
    build_effective_library_list,
    parse_fp_lib_table,
    parse_kicad_mod,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FP_LIB_TABLE = os.path.join(FIXTURE_DIR, "fp-lib-table")
TEST_MOD = os.path.join(FIXTURE_DIR, "TestRes.kicad_mod")


def _compute_dir_checksum(path: str) -> str:
    return FootprintIndexManager._compute_dir_checksum(path)


# ---------------------------------------------------------------------------
# Helpers — build temporary .pretty and fp-lib-table fixtures
# ---------------------------------------------------------------------------


def _make_pretty(tmp_path, name: str, mods: list[str]) -> str:
    """Create a .pretty directory with given .kicad_mod filenames (empty)."""
    lib_dir = tmp_path / f"{name}.pretty"
    lib_dir.mkdir()
    for mod in mods:
        (lib_dir / f"{mod}.kicad_mod").write_text(
            f'(footprint "{mod}" (layer "F.Cu") (descr "{mod} desc") (tags "tag") (attr smd))'
        )
    return str(lib_dir)


def _make_fp_lib_table(path: str, entries: list[tuple[str, str]]) -> str:
    """Write a minimal fp-lib-table with given (nickname, uri) entries."""
    lines = ["(fp_lib_table\n  (version 7)"]
    for nick, uri in entries:
        lines.append(
            f'  (lib (name "{nick}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'
        )
    lines.append(")")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def _make_table_entry_fp_lib_table(outer_path: str, inner_path: str) -> None:
    """Write an fp-lib-table where one entry has type="Table"."""
    content = (
        "(fp_lib_table\n  (version 7)\n"
        f'  (lib (name "System") (type "Table") (uri "{inner_path}") (options "") (descr ""))\n'
        ")"
    )
    with open(outer_path, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# parse_kicad_mod — attr and has_3d_model
# ---------------------------------------------------------------------------


class TestParseKicadModAttr:
    def test_attr_smd(self):
        info = parse_kicad_mod(TEST_MOD)
        assert info["attr"] == "smd"

    def test_has_3d_model_false(self):
        info = parse_kicad_mod(TEST_MOD)
        assert info["has_3d_model"] is False

    def test_has_3d_model_true(self, tmp_path):
        mod = tmp_path / "W3D.kicad_mod"
        mod.write_text(
            '(footprint "W3D" (layer "F.Cu") (descr "with 3d") (attr smd)'
            '  (model "foo.step" (offset (xyz 0 0 0)))'
            ")"
        )
        info = parse_kicad_mod(str(mod))
        assert info["has_3d_model"] is True
        assert info["attr"] == "smd"

    def test_no_attr_defaults_to_empty(self, tmp_path):
        mod = tmp_path / "NoAttr.kicad_mod"
        mod.write_text('(footprint "NoAttr" (layer "F.Cu") (descr "no attr"))')
        info = parse_kicad_mod(str(mod))
        assert info["attr"] == ""

    def test_through_hole_attr(self, tmp_path):
        mod = tmp_path / "TH.kicad_mod"
        mod.write_text('(footprint "TH" (layer "F.Cu") (descr "through hole") (attr through_hole))')
        info = parse_kicad_mod(str(mod))
        assert info["attr"] == "through_hole"


# ---------------------------------------------------------------------------
# parse_kicad_mod — edge_cuts
# ---------------------------------------------------------------------------


class TestParseKicadModEdgeCuts:
    def test_empty_by_default(self):
        info = parse_kicad_mod(TEST_MOD)
        assert info["edge_cuts"] == []

    def test_parses_edge_cuts_lines(self, tmp_path):
        mod = tmp_path / "EC.kicad_mod"
        mod.write_text(
            '(footprint "EC" (layer "F.Cu")'
            "  (fp_line (start -1 0.5) (end 1.5 -0.5)"
            '    (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
            ")"
        )
        info = parse_kicad_mod(str(mod))
        assert info["edge_cuts"] == [
            {
                "type": "fp_line",
                "layer": "Edge.Cuts",
                "x1": -1.0,
                "y1": 0.5,
                "x2": 1.5,
                "y2": -0.5,
                "width": 0.1,
            }
        ]

    def test_ignores_non_edge_cuts_layers(self, tmp_path):
        mod = tmp_path / "Mixed.kicad_mod"
        mod.write_text(
            '(footprint "Mixed" (layer "F.Cu")'
            '  (fp_line (start 0 0) (end 1 1) (stroke (width 0.1)) (layer "F.CrtYd"))'
            '  (fp_line (start 0 0) (end 2 2) (stroke (width 0.1)) (layer "Edge.Cuts"))'
            ")"
        )
        info = parse_kicad_mod(str(mod))
        assert len(info["edge_cuts"]) == 1
        assert info["edge_cuts"][0]["x2"] == 2.0
        assert info["edge_cuts"][0]["x1"] == 0.0


# ---------------------------------------------------------------------------
# parse_fp_lib_table — Table-type indirection
# ---------------------------------------------------------------------------


class TestParseFpLibTableTableType:
    def test_direct_kicad_entries(self, tmp_path):
        lib_dir = _make_pretty(tmp_path, "Foo", ["R_0402"])
        table = tmp_path / "fp-lib-table"
        _make_fp_lib_table(str(table), [("Foo", str(lib_dir))])
        entries = parse_fp_lib_table(str(table))
        assert len(entries) == 1
        assert entries[0]["nickname"] == "Foo"
        assert entries[0]["type"] == "KiCad"

    def test_table_type_indirection(self, tmp_path):
        lib_dir = _make_pretty(tmp_path, "Inner", ["C_0402"])
        inner_table = tmp_path / "inner-fp-lib-table"
        _make_fp_lib_table(str(inner_table), [("Inner", str(lib_dir))])

        outer_table = tmp_path / "outer-fp-lib-table"
        _make_table_entry_fp_lib_table(str(outer_table), str(inner_table))

        entries = parse_fp_lib_table(str(outer_table))
        names = [e["nickname"] for e in entries]
        assert "Inner" in names
        # System entry itself should NOT appear (it was the Table pointer)
        assert "System" not in names

    def test_cycle_protection(self, tmp_path):
        # Outer table points to itself
        outer = tmp_path / "self-ref.fp-lib-table"
        _make_table_entry_fp_lib_table(str(outer), str(outer))
        # Should not infinite-loop; returns empty or partial result
        entries = parse_fp_lib_table(str(outer))
        assert isinstance(entries, list)

    def test_raw_uri_preserved(self, tmp_path):
        lib_dir = _make_pretty(tmp_path, "Bar", ["L_0603"])
        table = tmp_path / "fp-lib-table"
        _make_fp_lib_table(str(table), [("Bar", str(lib_dir))])
        entries = parse_fp_lib_table(str(table))
        assert entries[0]["raw_uri"] == str(lib_dir)


class TestBuildEffectiveLibraryList:
    def test_deduplication_project_wins(self, tmp_path):
        proj_dir = _make_pretty(tmp_path, "Proj", ["R_Proj"])
        sys_dir = _make_pretty(tmp_path, "Sys", ["C_Sys"])

        global_table = tmp_path / "global-fp-lib-table"
        global_table.write_text(
            "(fp_lib_table\n  (version 7)\n"
            f'  (lib (name "Shared") (type "KiCad") (uri "{sys_dir}") (options "") (descr "global"))\n'
            ")"
        )

        project_table = tmp_path / "project-fp-lib-table"
        project_table.write_text(
            "(fp_lib_table\n  (version 7)\n"
            f'  (lib (name "Shared") (type "KiCad") (uri "{proj_dir}") (options "") (descr "project"))\n'
            ")"
        )

        env = _build_env_map()
        visited: set = set()
        seen: set = set()
        result = []
        for tpath in [str(project_table), str(global_table)]:
            for lib in _parse_fp_lib_table_recursive(tpath, env, visited):
                if lib["nickname"] not in seen:
                    seen.add(lib["nickname"])
                    result.append(lib)

        assert len(result) == 1
        assert result[0]["description"] == "project"

    def test_returns_list(self):
        entries = build_effective_library_list()
        assert isinstance(entries, list)


# ---------------------------------------------------------------------------
# FootprintDatabase
# ---------------------------------------------------------------------------


def _make_db(tmp_path) -> FootprintDatabase:
    return FootprintDatabase(str(tmp_path / "fp.db"))


def _make_record(lib_name: str, fp_name: str, **kwargs) -> FootprintRecord:
    defaults = {
        "library_id": 0,
        "description": f"{fp_name} desc",
        "tags": "resistor smd",
        "attr": "smd",
        "pad_count": 2,
        "has_3d_model": False,
    }
    defaults.update(kwargs)
    return FootprintRecord(library_name=lib_name, footprint_name=fp_name, **defaults)


class TestFootprintDatabase:
    def test_save_and_retrieve(self, tmp_path):
        db = _make_db(tmp_path)
        recs = [_make_record("Res_SMD", "R_0402"), _make_record("Res_SMD", "R_0603")]
        n = db.save_library(
            "Res_SMD", "${FP}/Res.pretty", "/tmp/Res.pretty", "SMD Res", "abc123", recs
        )
        assert n == 2
        fps = db.get_library_footprints("Res_SMD")
        assert len(fps) == 2
        names = {r.footprint_name for r in fps}
        assert names == {"R_0402", "R_0603"}

    def test_get_footprint(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library(
            "Res_SMD",
            "${FP}/Res.pretty",
            "/tmp/Res.pretty",
            "SMD Res",
            "abc",
            [_make_record("Res_SMD", "R_0402", description="0402 res", tags="resistor")],
        )
        fp = db.get_footprint("Res_SMD", "R_0402")
        assert fp is not None
        assert fp.description == "0402 res"
        assert fp.tags == "resistor"

    def test_get_footprint_not_found(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_footprint("NoLib", "NoFP") is None

    def test_save_replaces_existing(self, tmp_path):
        db = _make_db(tmp_path)
        recs1 = [_make_record("Lib", "FP1")]
        db.save_library("Lib", "uri", "/path", "desc", "csum1", recs1)
        recs2 = [_make_record("Lib", "FP2")]
        db.save_library("Lib", "uri", "/path", "desc", "csum2", recs2)
        fps = db.get_library_footprints("Lib")
        assert len(fps) == 1
        assert fps[0].footprint_name == "FP2"

    def test_delete_library(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("L", "u", "/p", "d", "c", [_make_record("L", "F1")])
        states = db.get_library_states()
        lib_id = states["L"][0]
        db.delete_library(lib_id)
        assert db.get_library_footprints("L") == []
        assert "L" not in db.get_library_states()

    def test_touch_library(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("L", "u", "/old_path", "d", "csum1", [_make_record("L", "F1")])
        lib_id = db.get_library_states()["L"][0]
        db.touch_library(lib_id, "csum2", "/new_path")
        states = db.get_library_states()
        assert states["L"][1] == "csum2"
        assert states["L"][2] == "/new_path"

    def test_search_by_name(self, tmp_path):
        db = _make_db(tmp_path)
        recs = [
            _make_record("Res_SMD", "R_0402"),
            _make_record("Res_SMD", "R_0603"),
            _make_record("Cap_SMD", "C_0402"),
        ]
        db.save_library("Res_SMD", "u", "/p", "d", "c1", recs[:2])
        db.save_library("Cap_SMD", "u2", "/p2", "d2", "c2", [recs[2]])
        results = db.search_by_name("0402")
        names = {r.footprint_name for r in results}
        assert "R_0402" in names
        assert "C_0402" in names
        assert "R_0603" not in names

    def test_get_library_states(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("LibA", "rawA", "/dirA", "d", "csumA", [_make_record("LibA", "F")])
        states = db.get_library_states()
        assert "LibA" in states
        lib_id, checksum, dir_path = states["LibA"]
        assert isinstance(lib_id, int)
        assert checksum == "csumA"
        assert dir_path == "/dirA"

    def test_get_all_libraries(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("Zzz", "u", "/p", "d", "c", [])
        db.save_library("Aaa", "u2", "/p2", "d2", "c2", [])
        libs = db.get_all_libraries()
        assert len(libs) == 2
        assert libs[0].library_name == "Aaa"

    def test_stats(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library(
            "L", "u", "/p", "d", "c", [_make_record("L", "F1"), _make_record("L", "F2")]
        )
        stats = db.get_stats()
        assert stats.library_count == 1
        assert stats.footprint_count == 2
        assert stats.last_sync > 0

    def test_has_3d_model_round_trip(self, tmp_path):
        db = _make_db(tmp_path)
        rec = _make_record("Lib", "FP3D", has_3d_model=True)
        db.save_library("Lib", "u", "/p", "d", "c", [rec])
        fp = db.get_footprint("Lib", "FP3D")
        assert fp.has_3d_model is True


# Schema v2: project column (B scheme)
# ---------------------------------------------------------------------------


class TestSchemaV2ProjectColumn:
    def test_v1_db_auto_migrates_on_open(self, tmp_path):
        """A v1 database (no project column) gains it via ALTER, data kept."""
        import sqlite3

        db_path = tmp_path / "v1.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE fp_libraries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "library_name VARCHAR NOT NULL UNIQUE, "
            "raw_uri VARCHAR NOT NULL DEFAULT '', "
            "dir_path VARCHAR NOT NULL DEFAULT '', "
            "description VARCHAR NOT NULL DEFAULT '', "
            "checksum VARCHAR NOT NULL DEFAULT '', "
            "footprint_count INTEGER NOT NULL DEFAULT 0, "
            "last_indexed FLOAT NOT NULL DEFAULT 0.0)"
        )
        conn.execute(
            "CREATE TABLE footprints ("
            "library_name VARCHAR NOT NULL, "
            "footprint_name VARCHAR NOT NULL, "
            "library_id INTEGER NOT NULL, "
            "description VARCHAR NOT NULL DEFAULT '', "
            "tags VARCHAR NOT NULL DEFAULT '', "
            "attr VARCHAR NOT NULL DEFAULT '', "
            "pad_count INTEGER NOT NULL DEFAULT 0, "
            "has_3d_model INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (library_name, footprint_name))"
        )
        conn.execute(
            "INSERT INTO fp_libraries (library_name, raw_uri, dir_path) "
            "VALUES ('OldLib', 'u', '/p')"
        )
        conn.commit()
        conn.close()

        # Opening through the ORM must add the column, keep the row, and
        # default existing rows to the global scope (project='').
        db = FootprintDatabase(str(db_path))
        libs = db.get_all_libraries()
        assert [lib.library_name for lib in libs] == ["OldLib"]
        assert libs[0].project == ""

        # v2 rows can be stored and project-scoped queries work.
        db.save_library(
            "ProjLib",
            "u2",
            "/p2",
            "d2",
            "c2",
            [_make_record("ProjLib", "F1")],
            project="/proj",
        )
        assert db.get_all_libraries(project="/proj")[0].library_name == "OldLib"
        assert db.library_name_exists("ProjLib") is True

    def test_project_scope_filters_queries(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("Global", "u", "/g", "d", "c", [_make_record("Global", "GF1")])
        db.save_library("ProjA", "u", "/a", "d", "c", [_make_record("ProjA", "AF1")], project="/a")
        db.save_library("ProjB", "u", "/b", "d", "c", [_make_record("ProjB", "BF1")], project="/b")

        # No scope → everything.
        assert {l.library_name for l in db.get_all_libraries(None)} == {
            "Global",
            "ProjA",
            "ProjB",
        }
        # Project scope → global + that project only.
        assert {l.library_name for l in db.get_all_libraries("/a")} == {"Global", "ProjA"}
        assert {l.library_name for l in db.get_all_libraries("/b")} == {"Global", "ProjB"}
        # Empty scope → global only.
        assert [l.library_name for l in db.get_all_libraries("")] == ["Global"]

        # Footnote queries follow the same scope.
        assert {f.footprint_name for f in db.get_library_footprints("ProjA", "/a")} == {"AF1"}
        assert db.get_library_footprints("ProjB", "/a") == []
        assert db.get_footprint("ProjB", "BF1", "/a") is None
        assert db.get_footprint("Global", "GF1", "/a") is not None

    def test_project_scope_stats(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("Global", "u", "/g", "d", "c", [_make_record("Global", "GF1")])
        db.save_library("ProjA", "u", "/a", "d", "c", [_make_record("ProjA", "AF1")], project="/a")
        stats_a = db.get_stats("/a")
        assert stats_a.library_count == 2  # Global + ProjA
        assert stats_a.footprint_count == 2
        stats_g = db.get_stats("")
        assert stats_g.library_count == 1  # global only
        assert stats_g.footprint_count == 1

    def test_project_scope_search_by_name(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library(
            "ProjA", "u", "/a", "d", "c", [_make_record("ProjA", "Common_FP")], project="/a"
        )
        db.save_library(
            "ProjB", "u", "/b", "d", "c", [_make_record("ProjB", "Common_FP")], project="/b"
        )
        names_a = {r.footprint_name for r in db.search_by_name("Common", project="/a")}
        assert names_a == {"Common_FP"}
        names_b = {r.footprint_name for r in db.search_by_name("Common", project="/b")}
        assert names_b == {"Common_FP"}

    def test_library_name_exists_ignores_project(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("ProjLib", "u", "/p", "d", "c", [], project="/some/project")
        assert db.library_name_exists("ProjLib") is True
        assert db.library_name_exists("NeverSeen") is False

    def test_get_all_footprint_names_scoped(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_library("Global", "u", "/g", "d", "c", [_make_record("Global", "GF1")])
        db.save_library("ProjA", "u", "/a", "d", "c", [_make_record("ProjA", "AF1")], project="/a")
        assert db.get_all_footprint_names("/a") == {"GF1", "AF1"}
        assert db.get_all_footprint_names("/b") == {"GF1"}
        assert db.get_all_footprint_names("") == {"GF1"}


# ---------------------------------------------------------------------------
# FootprintIndexManager._compute_dir_checksum
# ---------------------------------------------------------------------------


class TestComputeDirChecksum:
    def test_empty_dir(self, tmp_path):
        d = tmp_path / "Empty.pretty"
        d.mkdir()
        csum = _compute_dir_checksum(str(d))
        assert isinstance(csum, str) and len(csum) == 64

    def test_same_content_same_checksum(self, tmp_path):
        d = tmp_path / "Lib.pretty"
        d.mkdir()
        (d / "R.kicad_mod").write_text("content")
        c1 = _compute_dir_checksum(str(d))
        c2 = _compute_dir_checksum(str(d))
        assert c1 == c2

    def test_different_checksum_after_add(self, tmp_path):
        d = tmp_path / "Lib.pretty"
        d.mkdir()
        (d / "R.kicad_mod").write_text("content")
        c1 = _compute_dir_checksum(str(d))
        (d / "C.kicad_mod").write_text("content2")
        c2 = _compute_dir_checksum(str(d))
        assert c1 != c2

    def test_different_checksum_after_modify(self, tmp_path):
        import time as _time

        d = tmp_path / "Lib.pretty"
        d.mkdir()
        f = d / "R.kicad_mod"
        f.write_text("original")
        c1 = _compute_dir_checksum(str(d))
        _time.sleep(0.01)
        f.write_text("modified")
        c2 = _compute_dir_checksum(str(d))
        assert c1 != c2

    def test_ignores_non_kicad_mod_files(self, tmp_path):
        d = tmp_path / "Lib.pretty"
        d.mkdir()
        (d / "R.kicad_mod").write_text("mod")
        c1 = _compute_dir_checksum(str(d))
        (d / "README.txt").write_text("ignore me")
        c2 = _compute_dir_checksum(str(d))
        assert c1 == c2


# ---------------------------------------------------------------------------
# FootprintIndexManager.sync
# ---------------------------------------------------------------------------


class TestFootprintIndexManagerSync:
    def _mgr(self, tmp_path, project_path=None) -> FootprintIndexManager:
        db_path = tmp_path / "test_fp.db"
        return FootprintIndexManager(db_path=str(db_path), project_path=project_path)

    def _make_lib_table_for(self, tmp_path, *pretty_dirs) -> str:
        table = tmp_path / "fp-lib-table"
        entries = [(os.path.basename(d).replace(".pretty", ""), d) for d in pretty_dirs]
        _make_fp_lib_table(str(table), entries)
        return str(table)

    def test_sync_adds_new_libraries(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402", "R_0603"])
        self._make_lib_table_for(tmp_path, lib_dir)

        mgr = self._mgr(tmp_path)
        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [
                {"nickname": "Res", "uri": lib_dir, "raw_uri": lib_dir, "description": ""},
            ],
        )
        stats = mgr.sync()
        assert stats.added == 1
        assert stats.total_footprints == 2
        assert mgr.get_stats().footprint_count == 2

    def test_sync_skips_unchanged(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        entry = {"nickname": "Res", "uri": lib_dir, "raw_uri": lib_dir, "description": ""}

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [entry],
        )
        mgr = self._mgr(tmp_path)
        s1 = mgr.sync()
        assert s1.added == 1
        s2 = mgr.sync()
        assert s2.skipped == 1
        assert s2.updated == 0
        assert s2.added == 0

    def test_sync_updates_on_file_change(self, tmp_path, monkeypatch):
        import time as _time

        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        entry = {"nickname": "Res", "uri": lib_dir, "raw_uri": lib_dir, "description": ""}

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [entry],
        )
        mgr = self._mgr(tmp_path)
        mgr.sync()

        _time.sleep(0.01)
        (os.path.join(lib_dir, "R_0402.kicad_mod")).__class__  # noop
        # Add a new file to change the checksum
        with open(os.path.join(lib_dir, "R_0805.kicad_mod"), "w") as f:
            f.write('(footprint "R_0805" (layer "F.Cu") (descr "new fp"))')

        s2 = mgr.sync()
        assert s2.updated == 1

    def test_sync_removes_deleted_library(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        entry = {"nickname": "Res", "uri": lib_dir, "raw_uri": lib_dir, "description": ""}

        call_count = 0

        def _effective(project_path=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [entry]
            return []  # library removed from table

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            _effective,
        )
        mgr = self._mgr(tmp_path)
        mgr.sync()
        s2 = mgr.sync()
        assert s2.removed == 1
        assert mgr.get_stats().library_count == 0

    def test_sync_force_reparses(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        entry = {"nickname": "Res", "uri": lib_dir, "raw_uri": lib_dir, "description": ""}

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [entry],
        )
        mgr = self._mgr(tmp_path)
        mgr.sync()
        s2 = mgr.sync(force=True)
        assert s2.updated == 1  # force=True causes reparse even if unchanged

    def test_sync_handles_missing_dir(self, tmp_path, monkeypatch):
        entry = {
            "nickname": "Ghost",
            "uri": "/nonexistent/.pretty",
            "raw_uri": "u",
            "description": "",
        }
        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [entry],
        )
        mgr = self._mgr(tmp_path)
        stats = mgr.sync()
        assert stats.failed == 1
        assert stats.added == 0

    def test_search_footprints_returns_results(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res_SMD", ["R_0402_1005Metric"])
        entry = {"nickname": "Res_SMD", "uri": lib_dir, "raw_uri": lib_dir, "description": ""}
        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [entry],
        )
        mgr = self._mgr(tmp_path)
        mgr.sync()
        results = mgr.search_footprints("0402")
        assert any("0402" in r.footprint_name for r in results)

    def test_touch_updates_dir_path_on_path_change(self, tmp_path, monkeypatch):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        entry_old = {
            "nickname": "Res",
            "uri": lib_dir,
            "raw_uri": "${FP}/Res.pretty",
            "description": "",
        }

        call_count = 0

        def _effective(project_path=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [entry_old]
            # Simulate AppImage re-mount: same content, different path
            new_dir = str(tmp_path / "new_mount" / "Res.pretty")
            os.makedirs(new_dir, exist_ok=True)
            import shutil

            for f in os.listdir(lib_dir):
                shutil.copy2(os.path.join(lib_dir, f), os.path.join(new_dir, f))
            return [
                {
                    "nickname": "Res",
                    "uri": new_dir,
                    "raw_uri": "${FP}/Res.pretty",
                    "description": "",
                }
            ]

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            _effective,
        )
        mgr = self._mgr(tmp_path)
        mgr.sync()

    def test_index_library_single_library(self, tmp_path):
        """index_library indexes exactly one dir — no table traversal."""
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402", "R_0603"])
        mgr = self._mgr(tmp_path)
        n = mgr.index_library("Res", lib_dir)
        assert n == 2
        assert mgr.get_stats().footprint_count == 2
        assert [r.footprint_name for r in mgr.get_library_footprints("Res")] == [
            "R_0402",
            "R_0603",
        ]

    def test_sync_tags_project_table_entries(self, tmp_path, monkeypatch):
        """Entries whose table_path is the project fp-lib-table are stored
        with the project id; others are stored as global (project="")."""
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        proj_table = str(proj_dir / "fp-lib-table")
        proj_lib = _make_pretty(proj_dir, "ProjLib", ["P_FP"])
        _make_fp_lib_table(proj_table, [("ProjLib", proj_lib)])
        global_table = str(tmp_path / "global-fp-lib-table")
        global_lib = _make_pretty(tmp_path, "GlobalLib", ["G_FP"])
        _make_fp_lib_table(global_table, [("GlobalLib", global_lib), ("GlobalLib", global_lib)])

        def _effective(project_path=None):
            return [
                {
                    "nickname": "ProjLib",
                    "uri": proj_lib,
                    "raw_uri": proj_lib,
                    "description": "",
                    "table_path": proj_table,
                },
                # A genuinely different table (global) — even inside the same
                # synced list it stays global.
                {
                    "nickname": "GlobalLib",
                    "uri": global_lib,
                    "raw_uri": global_lib,
                    "description": "",
                    "table_path": global_table,
                },
            ]

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            _effective,
        )
        mgr = self._mgr(tmp_path, project_path=str(proj_dir / "proj.kicad_pro"))
        stats = mgr.sync()
        assert stats.added == 2

        libs = mgr.get_all_libraries()
        by_name = {lib.library_name: lib.project for lib in libs}
        assert by_name["ProjLib"] == os.path.realpath(str(proj_dir))
        assert by_name["GlobalLib"] == ""

    def test_sync_does_not_delete_other_project_rows(self, tmp_path, monkeypatch):
        """Syncing project B must never remove project A's indexed rows."""
        # Real on-disk libraries so sync treats them as healthy.
        proj_a_lib = _make_pretty(tmp_path, "ProjALib", ["A_FP"])
        global_lib = _make_pretty(tmp_path, "GlobalLib", ["G_FP"])

        # Pre-seed the DB directly to simulate a previously-synced project A
        # plus a global library.
        mgr = self._mgr(tmp_path, project_path=None)
        mgr._db.save_library(
            "ProjALib",
            "u",
            proj_a_lib,
            "d",
            "c",
            [_make_record("ProjALib", "A_FP")],
            project="/projA",
        )
        mgr._db.save_library(
            "GlobalLib", "u", global_lib, "d", "c", [_make_record("GlobalLib", "G_FP")]
        )

        # Sync project B: effective list contains only B + global.
        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            lambda project_path=None: [
                {
                    "nickname": "GlobalLib",
                    "uri": global_lib,
                    "raw_uri": global_lib,
                    "description": "",
                }
            ],
        )
        mgr_b = self._mgr(tmp_path, project_path="/projB/proj.kicad_pro")
        stats = mgr_b.sync()
        # Global kept (still in table); ProjALib untouched by B's sync.
        assert stats.removed == 0
        names = {lib.library_name for lib in mgr_b._db.get_all_libraries(None)}
        assert "ProjALib" in names
        assert "GlobalLib" in names

    def test_index_library_update_after_file_add(self, tmp_path):
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])
        mgr = self._mgr(tmp_path)
        assert mgr.index_library("Res", lib_dir) == 1
        with open(os.path.join(lib_dir, "R_0805.kicad_mod"), "w") as f:
            f.write('(footprint "R_0805" (layer "F.Cu") (descr "new fp"))')
        assert mgr.index_library("Res", lib_dir) == 2
        assert mgr.get_stats().footprint_count == 2


# ---------------------------------------------------------------------------
# Tests for async sync_footprint_index + get_footprint_sync_status tools
# ---------------------------------------------------------------------------

import kcaa.tools.pcb_library_tools as _tool_module


class TestFpSyncTools:
    """Tests for the background sync tool pair."""

    def setup_method(self):
        """Reset module-level sync state before each test."""
        with _tool_module._fp_sync_lock:
            _tool_module._fp_sync_state.running = False
            _tool_module._fp_sync_state.current = 0
            _tool_module._fp_sync_state.total = 0
            _tool_module._fp_sync_state.current_library = ""
            _tool_module._fp_sync_state.last_result = None
            _tool_module._fp_sync_state.error = None

    def test_background_sync_completes(self, tmp_path, monkeypatch):
        """_run_fp_sync_in_background updates state to done with last_result."""
        lib_dir = _make_pretty(tmp_path, "Res", ["R_0402"])

        def _effective(project_path=None):
            return [
                {
                    "nickname": "Res",
                    "uri": lib_dir,
                    "raw_uri": "${FP}/Res.pretty",
                    "description": "",
                }
            ]

        from kcaa.utils.footprint_index_manager import FootprintIndexManager

        db_path = str(tmp_path / "fp.db")
        mgr = FootprintIndexManager(db_path=db_path)

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            _effective,
        )
        monkeypatch.setattr(
            "kcaa.tools.pcb_library_tools.get_footprint_index_manager",
            lambda project_path=None, project_id=None: mgr,
        )

        with _tool_module._fp_sync_lock:
            _tool_module._fp_sync_state.running = True

        _tool_module._run_fp_sync_in_background(False, None)

        with _tool_module._fp_sync_lock:
            assert _tool_module._fp_sync_state.running is False
            assert _tool_module._fp_sync_state.last_result is not None
            assert _tool_module._fp_sync_state.last_result["success"] is True
            assert _tool_module._fp_sync_state.error is None

    def test_state_reflects_progress(self, tmp_path, monkeypatch):
        """_run_fp_sync_in_background updates _fp_sync_state correctly."""
        lib_dir = _make_pretty(tmp_path, "Cap", ["C_0402"])

        def _effective(project_path=None):
            return [
                {
                    "nickname": "Cap",
                    "uri": lib_dir,
                    "raw_uri": "${FP}/Cap.pretty",
                    "description": "",
                }
            ]

        from kcaa.utils.footprint_index_manager import FootprintIndexManager

        db_path = str(tmp_path / "fp2.db")
        mgr = FootprintIndexManager(db_path=db_path)

        monkeypatch.setattr(
            "kcaa.utils.footprint_index_manager.build_effective_library_list",
            _effective,
        )
        monkeypatch.setattr(
            "kcaa.tools.pcb_library_tools.get_footprint_index_manager",
            lambda project_path=None, project_id=None: mgr,
        )

        with _tool_module._fp_sync_lock:
            _tool_module._fp_sync_state.running = True

        _tool_module._run_fp_sync_in_background(False, None)

        # After thread function completes: running=False, last_result populated
        with _tool_module._fp_sync_lock:
            assert _tool_module._fp_sync_state.running is False
            assert _tool_module._fp_sync_state.last_result is not None
            assert _tool_module._fp_sync_state.last_result["success"] is True
            assert _tool_module._fp_sync_state.error is None

    def test_already_running_guard(self):
        """sync_footprint_index returns already_running when sync is in progress."""
        import asyncio

        async def _call():
            with _tool_module._fp_sync_lock:
                _tool_module._fp_sync_state.running = True
                _tool_module._fp_sync_state.current = 5
                _tool_module._fp_sync_state.total = 20
                _tool_module._fp_sync_state.current_library = "SomeLib"

            from fastmcp import FastMCP

            mcp = FastMCP("test")
            _tool_module.register_pcb_library_tools(mcp)
            tool = await mcp.get_tool("sync_footprint_index")
            assert tool is not None
            result = await tool.fn(project_path="/tmp/proj.kicad_pro")
            return result

        result = asyncio.run(_call())
        assert result["status"] == "already_running"
        assert result["current"] == 5
        assert result["total"] == 20

    def test_status_percent_complete(self):
        """get_footprint_sync_status computes percent_complete correctly."""
        import asyncio

        async def _call():
            with _tool_module._fp_sync_lock:
                _tool_module._fp_sync_state.running = True
                _tool_module._fp_sync_state.current = 50
                _tool_module._fp_sync_state.total = 100
                _tool_module._fp_sync_state.current_library = "Lib50"

            from fastmcp import FastMCP

            mcp = FastMCP("test")
            _tool_module.register_pcb_library_tools(mcp)
            tool = await mcp.get_tool("get_footprint_sync_status")
            assert tool is not None
            return await tool.fn()

        result = asyncio.run(_call())
        assert result["percent_complete"] == 50
        assert result["running"] is True
        assert result["current_library"] == "Lib50"


class TestGetFootprintIndexManager:
    """The factory accepts a canonical ``project_id`` without re-deriving it."""

    def _factory_module(self):
        return __import__(
            "kcaa.utils.footprint_index_manager", fromlist=["get_footprint_index_manager"]
        )

    def test_project_id_used_verbatim(self, tmp_path, monkeypatch):
        """A canonical project_id must scope directly: no dirname re-derivation."""
        mod = self._factory_module()
        monkeypatch.setattr(mod, "_singleton", None)
        mgr = mod.get_footprint_index_manager(project_id="/tmp/SomeProject")
        assert mgr._project_id == "/tmp/SomeProject"
        assert mgr._project_path is None

    def test_rejects_both_project_path_and_id(self, tmp_path, monkeypatch):
        mod = self._factory_module()
        monkeypatch.setattr(mod, "_singleton", None)
        with pytest.raises(ValueError):
            mod.get_footprint_index_manager(
                project_path="/tmp/SomeProject/proj.kicad_pro",
                project_id="/tmp/SomeProject",
            )
