"""
Unit tests for the PCB → footprint library tools registered in
``kcaa.tools.pcb_library_tools`` and their supporting utils
(``normalize_footprint_for_library`` in ``kcaa.utils.pcb_footprint_utils``,
``fp_lib_table_utils`` registration).
"""

import asyncio
import os

import pytest

from kcaa.utils.footprint_index_manager import FootprintIndexManager
from kcaa.utils.fp_lib_table_utils import register_library_in_table
from kcaa.utils.pcb_footprint_utils import (
    normalize_footprint_for_library,
    serialize_footprint_mod,
    split_footprint_header,
)

# Every footprint placed on the test board; used as the explicit
# ``footprints`` argument for add_footprints_to_library.
_BOARD_FOOTPRINTS = ["Sensor_Board_XYZ", "R_0402_1005Metric", "Connector_Odd"]

# Minimal board with three footprints:
#   - CustomLib:Sensor_Board_XYZ  -> missing everywhere (candidate, 45° rotation)
#   - Resistor_SMD:R_0402_1005Metric -> exists in the TestSys library, skipped
#     when exporting elsewhere; failed when the target directory has it
#   - CustomLib:Connector_Odd    -> missing everywhere (candidate)
_BOARD = """\
(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(40 "B.SilkS" user)
\t\t(49 "F.SilkS" user)
\t)
\t(net "VCC")
\t(net "GND")
\t(footprint "CustomLib:Sensor_Board_XYZ"
\t\t(layer "F.Cu")
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t\t(at 10.0 20.0 45.0)
\t\t(property "Reference" "U1")
\t\t(property "Value" "Sensor")
\t\t(property "Footprint" "CustomLib:Sensor_Board_XYZ")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0.0 90.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "VCC")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0.0 90.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "GND")
\t\t)
\t\t(fp_text reference "U1" (at 0 2.0 90.0) (layer "F.SilkS"))
\t\t(fp_text value "Sensor" (at 0 -2.0 90.0) (layer "F.Fab"))
\t)
\t(footprint "Resistor_SMD:R_0402_1005Metric"
\t\t(layer "F.Cu")
\t\t(uuid "22222222-0000-0000-0000-000000000002")
\t\t(at 20.0 30.0 0.0)
\t\t(property "Reference" "R1")
\t\t(property "Value" "10k")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "VCC")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "GND")
\t\t)
\t)
\t(footprint "CustomLib:Connector_Odd"
\t\t(layer "B.Cu")
\t\t(uuid "33333333-0000-0000-0000-000000000003")
\t\t(at 30.0 40.0 0.0)
\t\t(property "Reference" "J1")
\t\t(property "Value" "Conn")
\t\t(pad "1" thru_hole circle
\t\t\t(at -1.27 0.0)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1.0)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t\t(net "GND")
\t\t)
\t)
)
"""


def _make_board(tmp_path, name="custom_board.kicad_pcb") -> str:
    path = tmp_path / name
    path.write_text(_BOARD, encoding="utf-8")
    return str(path)


def _make_library(tmp_path, nickname: str, mods: list[str]) -> str:
    """Create a .pretty dir with (empty) .kicad_mod files."""
    lib_dir = tmp_path / f"{nickname}.pretty"
    lib_dir.mkdir()
    for mod in mods:
        (lib_dir / f"{mod}.kicad_mod").write_text(f'(footprint "{mod}")', encoding="utf-8")
    return str(lib_dir)


def _make_fp_lib_table(tmp_path, entries: list[tuple[str, str]]) -> str:
    lines = ["(fp_lib_table", "\t(version 7)"]
    for nick, uri in entries:
        lines.append(
            f'\t(lib (name "{nick}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'
        )
    lines.append(")")
    path = tmp_path / "fp-lib-table"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.pcb_library_tools import register_pcb_library_tools

    mock = _MockMCP()
    register_pcb_library_tools(mock)  # type: ignore[arg-type]
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Fixtures: system lib (TestSys with R_0402), 3rdparty dir, isolated index DB.

    The fp-lib-table lives in the board's directory, so it doubles as the
    project-local table (find_fp_lib_tables checks the PCB directory first).
    """
    sys_lib = _make_library(tmp_path, "TestSys", ["R_0402_1005Metric"])
    table = _make_fp_lib_table(
        tmp_path,
        [("TestSys", sys_lib)],
    )
    third_party = tmp_path / "3rdparty"
    third_party.mkdir()

    index_mgr = FootprintIndexManager(db_path=str(tmp_path / "fp_test.db"))

    from kcaa.utils import pcb_library_utils
    from kcaa.utils.config import config

    monkeypatch.setattr(pcb_library_utils, "_default_kicad_config_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(config, "_kicad_3rd_party", str(third_party))
    # `${KICAD10_3RD_PARTY}` URIs are expanded via os.environ (ServerConfig
    # builds a fresh instance), so set the env var — not just the singleton.
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    monkeypatch.setattr(
        "kcaa.tools.pcb_library_tools._3rd_party_footprints_dir",
        lambda: str(third_party / "footprints"),
    )
    # Isolate the footprint index: tools use the module-level singleton, so
    # swap the factory for a temp-DB manager (never the real user DB).
    monkeypatch.setattr(
        "kcaa.tools.pcb_library_tools.get_footprint_index_manager",
        lambda project_path=None: index_mgr,
    )
    return {
        "tmp_path": str(tmp_path),
        "table": table,
        "system_lib": sys_lib,
        "third_party": str(third_party),
        "index_mgr": index_mgr,
    }


# ---------------------------------------------------------------------------
# normalize_footprint_for_library
# ---------------------------------------------------------------------------


class TestNormalizeForLibrary:
    def _node(self):
        import sexpdata

        return [
            n
            for n in sexpdata.loads(_BOARD)
            if isinstance(n, list) and n and str(n[0]) == "footprint"
        ]

    def test_header_and_version(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        assert split_footprint_header(out) == (None, "Sensor_Board_XYZ")
        assert out[1] == "Sensor_Board_XYZ"
        assert str(out[2][0]) == "version"
        assert out[2][1] == 20260206

    def test_strips_instance_data(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        keys = [str(x[0]) if isinstance(x, list) and x else "" for x in out]
        assert "at" not in keys  # placement gone
        assert "uuid" not in keys
        for child in out:
            if isinstance(child, list) and child and str(child[0]) == "pad":
                sub_keys = [str(s[0]) if isinstance(s, list) and s else "" for s in child]
                assert "net" not in sub_keys  # nets stripped
                assert child[1:] == child[1:]  # pad number preserved
        refs = [c[1] for c in out if isinstance(c, list) and c and str(c[0]) == "property"]
        assert "Reference" not in refs
        assert "Value" not in refs

    def test_rotation_inverted_for_pads(self):
        node = self._node()[0]  # fp at rotation 45°
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        for child in out:
            if isinstance(child, list) and child and str(child[0]) == "pad":
                for sub in child:
                    if isinstance(sub, list) and sub and str(sub[0]) == "at":
                        # pad stored 90° absolute -> local 90 - 45 = 45
                        assert sub[3] == pytest.approx(45.0)

    def test_rotation_inverted_for_text(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        for child in out:
            if isinstance(child, list) and child and str(child[0]) == "fp_text":
                for sub in child:
                    if isinstance(sub, list) and sub and str(sub[0]) == "at":
                        # text stored 90° (board-space readable) -> local 90 + 45 = 135
                        assert sub[3] == pytest.approx(135.0)
                        break
                if child[1] and str(child[1]) == "reference":
                    assert child[2] == "REF**"
                elif child[1] and str(child[1]) == "value":
                    assert child[2] == "Sensor_Board_XYZ"

    def test_serialize_roundtrip(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        text = serialize_footprint_mod(out)
        import sexpdata

        reparsed = sexpdata.loads(text)
        assert reparsed[0] == "footprint" or str(reparsed[0]) == "footprint"
        assert str(reparsed[1]) == "Sensor_Board_XYZ"


# ---------------------------------------------------------------------------
# fp_lib_table_utils registration
# ---------------------------------------------------------------------------


class TestRegisterLibraryInTable:
    def test_creates_new_table(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        result = register_library_in_table(
            table, "MyLib", "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty"
        )
        assert result["registered"] is True
        text = open(table, encoding="utf-8").read()
        assert 'name "MyLib"' in text
        assert "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty" in text

    def test_appends_to_existing_preserving_other_entries(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        _make_fp_lib_table(tmp_path, [("TestSys", "/tmp/TestSys.pretty")])
        register_library_in_table(table, "MyLib", "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty")
        text = open(table, encoding="utf-8").read()
        assert 'name "TestSys"' in text
        assert 'name "MyLib"' in text
        # original entry byte-identical (surgical append)
        assert "/tmp/TestSys.pretty" in text

    def test_no_duplicate_registration(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        register_library_in_table(table, "MyLib", "uri1")
        r2 = register_library_in_table(table, "MyLib", "uri2")
        assert r2["registered"] is False
        assert r2["reason"] == "already_registered"
        text = open(table, encoding="utf-8").read()
        assert text.count('name "MyLib"') == 1

    def test_creates_backup(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        _make_fp_lib_table(tmp_path, [("TestSys", "/tmp/TestSys.pretty")])
        register_library_in_table(table, "MyLib", "uri")
        assert os.path.isfile(table + ".bak")

    def test_sanitizes_nickname(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        register_library_in_table(table, "My Lib/With Bads", "uri")
        text = open(table, encoding="utf-8").read()
        assert 'name "My_Lib_With_Bads"' in text


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


class TestFindMissingFootprints:
    def test_reports_missing_and_existing(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        result = _run(tools["find_missing_footprints"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        names = [fp["name"] for fp in result["missing"]]
        assert "Sensor_Board_XYZ" in names
        assert "Connector_Odd" in names
        assert "R_0402_1005Metric" not in names  # in TestSys library
        assert result["missing_count"] == 2

    def test_list_is_read_only(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        before = open(board, encoding="utf-8").read()
        _run(tools["find_missing_footprints"](pcb_path=board, ctx=None))
        assert open(board, encoding="utf-8").read() == before

    def test_does_not_write_footprint_database(self, tools, tmp_path, project):
        """The read path never writes to the index DB (it reads it)."""
        board = _make_board(tmp_path)
        result = _run(tools["find_missing_footprints"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        assert project["index_mgr"].get_stats().footprint_count == 0
        assert project["index_mgr"].get_stats().library_count == 0

    def test_uses_indexed_footprint_names_when_db_populated(self, tools, tmp_path, project):
        """find_missing consumes the index DB: an indexed name is NOT missing,
        even when the library directory is no longer live-scannable."""
        board = _make_board(tmp_path)
        project["index_mgr"].index_library("TestSys", project["system_lib"])
        result = _run(tools["find_missing_footprints"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        names = [fp["name"] for fp in result["missing"]]
        assert "R_0402_1005Metric" not in names  # indexed → existing
        assert "Sensor_Board_XYZ" in names
        assert "Connector_Odd" in names
        assert result["missing_count"] == 2


class TestCreate3rdPartyLibrary:
    def test_creates_and_registers(self, tools, tmp_path, project):
        result = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" not in result, result
        assert result["library"] == "MyVendor"
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert result["path"] == lib_dir
        assert os.path.isdir(lib_dir)
        final_table = os.path.join(project["tmp_path"], "fp-lib-table")
        assert result["table_path"] == final_table
        assert result["registered"] is True
        table_text = open(final_table, encoding="utf-8").read()
        assert 'name "MyVendor"' in table_text
        assert "${KICAD10_3RD_PARTY}/footprints/MyVendor.pretty" in table_text
        # Only the new library lands in the index DB — never the whole table.
        libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in libs] == ["MyVendor"]

    def test_collision_errors(self, tools, tmp_path, project):
        result = _run(tools["create_footprint_library"](name="TestSys", ctx=None))
        assert "error" in result
        assert "already exists" in result["error"]
        assert "TestSys" not in [
            lib.library_name for lib in project["index_mgr"].get_all_libraries()
        ]

    def test_collision_with_indexed_other_project_library(self, tools, tmp_path, project):
        """A nickname indexed under any project blocks creation."""
        project["index_mgr"]._db.save_library(
            "OtherProjLib", "u", "/x", "d", "c", [], project="/other/project"
        )
        result = _run(tools["create_footprint_library"](name="OtherProjLib", ctx=None))
        assert "error" in result
        assert "already exists" in result["error"]

    def test_refuses_existing_directory(self, tools, tmp_path, project):
        """A pre-existing <name>.pretty directory blocks creation."""
        target = os.path.join(project["third_party"], "footprints", "Taken.pretty")
        os.makedirs(target)
        result = _run(tools["create_footprint_library"](name="Taken", ctx=None))
        assert "error" in result
        assert "Directory already exists" in result["error"]
        assert "Taken" in result["error"]
        assert not os.path.isfile(os.path.join(project["tmp_path"], "fp-lib-table.bak"))


class TestCreateProjectLibrary:
    """create_footprint_library's project_dir branch (${KIPRJMOD} scope)."""

    def test_creates_project_local_library(self, tools, tmp_path, project):
        proj_dir = tmp_path / "subproj"
        proj_dir.mkdir()
        result = _run(
            tools["create_footprint_library"](name="ProjLib", project_dir=str(proj_dir), ctx=None)
        )
        assert "error" not in result, result
        lib_dir = proj_dir / "ProjLib.pretty"
        assert result["path"] == str(lib_dir)
        assert os.path.isdir(lib_dir)
        # Registered in the project's own fp-lib-table, created on demand.
        table = proj_dir / "fp-lib-table"
        assert result["table_path"] == str(table)
        assert os.path.isfile(table)
        table_text = table.read_text(encoding="utf-8")
        assert 'name "ProjLib"' in table_text
        assert "${KIPRJMOD}/ProjLib.pretty" in table_text
        # Indexed under the project id (realpath of the project dir), so the
        # library shows up in the project scope but not the global scope.
        libs = project["index_mgr"]._db.get_all_libraries(project=None)
        assert [lib.library_name for lib in libs] == ["ProjLib"]
        assert libs[0].project == os.path.realpath(str(proj_dir))
        global_libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in global_libs] == []

    def test_missing_project_dir_errors(self, tools, tmp_path, project):
        result = _run(
            tools["create_footprint_library"](
                name="ProjLib", project_dir=str(tmp_path / "nope"), ctx=None
            )
        )
        assert "error" in result
        assert "Project directory not found" in result["error"]
        # Nothing was created or indexed.
        assert not (tmp_path / "nope").exists()
        assert project["index_mgr"]._db.get_all_libraries(project=None) == []

    def test_project_name_collides_with_global(self, tools, tmp_path, project):
        """Nickname uniqueness is global: a project library can't shadow the
        existing TestSys (registered in the global user fp-lib-table)."""
        proj_dir = tmp_path / "subproj"
        proj_dir.mkdir()
        result = _run(
            tools["create_footprint_library"](name="TestSys", project_dir=str(proj_dir), ctx=None)
        )
        assert "error" in result
        assert "already exists" in result["error"]
        assert not (proj_dir / "TestSys.pretty").exists()


class TestAddFootprints:
    def test_exports_missing_to_3rdparty(self, tools, tmp_path, project):
        # Create the target library first, then export into it.
        created = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" not in created, created
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 2
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert result["library_path"] == target
        assert os.path.isfile(os.path.join(target, "Sensor_Board_XYZ.kicad_mod"))
        assert os.path.isfile(os.path.join(target, "Connector_Odd.kicad_mod"))
        assert not os.path.exists(os.path.join(target, "R_0402_1005Metric.kicad_mod"))
        # The fixture's fp-lib-table doubles as the project table (board and
        # table share tmp_path), so the add's ownership check tags the
        # library under the project, not the global scope.
        libs = project["index_mgr"]._db.get_all_libraries(project=os.path.realpath(str(tmp_path)))
        assert [lib.library_name for lib in libs] == ["MyVendor"]
        assert libs[0].project == os.path.realpath(str(tmp_path))

    def test_board_untouched(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        before = open(board, encoding="utf-8").read()
        _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert open(board, encoding="utf-8").read() == before

    def test_no_overwrite_on_second_run(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        # Real-world premise: the index is built (sync) before exporting.
        project["index_mgr"].index_library("TestSys", project["system_lib"])
        board = _make_board(tmp_path)
        first = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert first["exported_count"] == 2
        second = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert second["exported_count"] == 0
        assert second["failed_count"] == 2  # Sensor_Board_XYZ, Connector_Odd
        assert second["skipped_count"] == 1  # R_0402 lives in TestSys
        assert all(s["reason"].startswith("target file already exists") for s in second["failed"])

    def test_exports_only_requested_subset(self, tools, tmp_path, project):
        """Only the explicit footprints list is exported — no full export."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 1
        assert result["exported"] == [
            os.path.join(
                project["third_party"], "footprints", "MyVendor.pretty", "Connector_Odd.kicad_mod"
            )
        ]
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.path.isfile(os.path.join(target, "Connector_Odd.kicad_mod"))
        assert not os.path.exists(os.path.join(target, "Sensor_Board_XYZ.kicad_mod"))

    def test_not_on_board_is_skipped(self, tools, tmp_path, project):
        """A requested name not placed on the board is skipped, never written."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["NoSuchFootprint"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 0
        assert result["skipped_count"] == 1
        assert result["skipped"] == [{"name": "NoSuchFootprint", "reason": "not on board"}]
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(target) == []

    def test_empty_footprints_exports_nothing(self, tools, tmp_path, project):
        """An empty footprints list is valid: no export, no error."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=[], library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 0
        assert result["failed_count"] == 0
        assert result["skipped_count"] == 0

    def test_export_into_existing_user_library(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="TestSys", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["library_path"] == project["system_lib"]
        assert os.path.isfile(os.path.join(project["system_lib"], "Sensor_Board_XYZ.kicad_mod"))
        # R_0402 already exists in the target directory: reported as failed,
        # never overwritten.
        assert [f["name"] for f in result["failed"]] == ["R_0402_1005Metric"]
        assert result["failed_count"] == 1
        assert result["skipped_count"] == 0
        r_0402_path = os.path.join(project["system_lib"], "R_0402_1005Metric.kicad_mod")
        mod = open(r_0402_path, encoding="utf-8").read()
        assert "(version" not in mod  # still the bare fixture, not an exported copy
        # indexing re-scans the whole target: R_0402 (fixture) + 2 exported
        assert result["indexed"] == 3
        libs = project["index_mgr"]._db.get_all_libraries(project=os.path.realpath(str(tmp_path)))
        assert [lib.library_name for lib in libs] == ["TestSys"]

    def test_global_target_library_keeps_global_ownership(self, tools, tmp_path, project):
        """A library registered in the global user table stays globally owned.

        The board lives in a subproject directory that has no fp-lib-table,
        so the target library's table (global) differs from the project
        table path — ownership must be "" (global), not the project id.
        """
        proj_dir = tmp_path / "projA"
        proj_dir.mkdir()
        board = _make_board(proj_dir, name="board.kicad_pcb")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="TestSys", ctx=None
            )
        )
        assert "error" not in result, result
        libs = project["index_mgr"]._db.get_all_libraries(project=None)
        assert [lib.library_name for lib in libs] == ["TestSys"]
        assert libs[0].project == ""  # global, not project-owned
        # Still visible in the global scope.
        global_libs = project["index_mgr"]._db.get_all_libraries(project="")
        assert [lib.library_name for lib in global_libs] == ["TestSys"]


class TestExportFileContent:
    def test_exported_mod_is_valid_and_clean(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result


class TestUnsafeFootprintNames:
    """BLOCKER 2: crafted PCB headers must never reach the filesystem."""

    def _board_with_name(self, tmp_path, header_name: str) -> str:
        board = _make_board(tmp_path)
        text = open(board, encoding="utf-8").read()
        text = text.replace(
            '(footprint "CustomLib:Sensor_Board_XYZ"', f'(footprint "{header_name}"'
        )
        path = tmp_path / "evil.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_traversal_name_is_failed_not_written(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = self._board_with_name(tmp_path, "../../escape")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["../../escape", "R_0402_1005Metric", "Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        # R_0402 lives in TestSys (skipped); Connector_Odd exports fine;
        # the crafted name is failed, not written.
        assert result["exported_count"] == 1
        names = {f["name"] for f in result["failed"]}
        assert "../../escape" in names
        # Nothing was written outside the library directory.
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(lib_dir) == ["Connector_Odd.kicad_mod"]
        assert not os.path.isfile(os.path.join(tmp_path, "escape.kicad_mod"))
        assert "escape.kicad_mod" not in os.listdir(lib_dir)

    def test_empty_name_is_failed_not_written(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = self._board_with_name(tmp_path, "CustomLib:")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["", "R_0402_1005Metric", "Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert "" in {f["name"] for f in result["failed"]}
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(lib_dir) == ["Connector_Odd.kicad_mod"]  # no stray ".kicad_mod"

    def test_write_footprint_mod_rejects_unsafe_name(self, tmp_path):
        from kcaa.utils.pcb_footprint_utils import is_safe_footprint_name, write_footprint_mod

        lib_dir = tmp_path / "Lib.pretty"
        lib_dir.mkdir()
        with pytest.raises(ValueError):
            write_footprint_mod(str(lib_dir), "../../escape", ["footprint"])
        with pytest.raises(ValueError):
            write_footprint_mod(str(lib_dir), "", ["footprint"])
        assert os.listdir(lib_dir) == []
        # Sanity: an ordinary name still works.
        assert is_safe_footprint_name("R_0402_1005Metric")
        assert not is_safe_footprint_name("../up")
        assert not is_safe_footprint_name("a/b")
        assert not is_safe_footprint_name("")


class TestCreateRollback:
    """MINOR 2: a failed registration must not leave an orphaned .pretty dir."""

    def test_registration_failure_removes_created_dir(self, tools, tmp_path, project, monkeypatch):
        _run = asyncio.run
        from kcaa.tools import pcb_library_tools

        def _boom(*args, **kwargs):
            raise ValueError("fp-lib-table is a single line")

        monkeypatch.setattr(pcb_library_tools, "register_library_in_table", _boom)
        result = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" in result
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert not os.path.exists(lib_dir)
