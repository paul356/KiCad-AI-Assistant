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
        lambda project_path=None, project_id=None: index_mgr,
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
        result = _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
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
        result = _run(tools["create_3rdparty_footprint_library"](name="TestSys", ctx=None))
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
        result = _run(tools["create_3rdparty_footprint_library"](name="OtherProjLib", ctx=None))
        assert "error" in result
        assert "already exists" in result["error"]

    def test_refuses_existing_directory(self, tools, tmp_path, project):
        """A pre-existing <name>.pretty directory blocks creation."""
        target = os.path.join(project["third_party"], "footprints", "Taken.pretty")
        os.makedirs(target)
        result = _run(tools["create_3rdparty_footprint_library"](name="Taken", ctx=None))
        assert "error" in result
        assert "Directory already exists" in result["error"]
        assert "Taken" in result["error"]
        assert not os.path.isfile(os.path.join(project["tmp_path"], "fp-lib-table.bak"))

    def test_creates_backup_of_table(self, tools, tmp_path, project):
        _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
        assert os.path.isfile(os.path.join(project["tmp_path"], "fp-lib-table.bak"))


class TestAddFootprints:
    def test_exports_missing_to_3rdparty(self, tools, tmp_path, project):
        # Create the target library first, then export into it.
        created = _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
        assert "error" not in created, created
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 2
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert result["library_path"] == target
        assert os.path.isfile(os.path.join(target, "Sensor_Board_XYZ.kicad_mod"))
        assert os.path.isfile(os.path.join(target, "Connector_Odd.kicad_mod"))
        assert not os.path.exists(os.path.join(target, "R_0402_1005Metric.kicad_mod"))
        # Index updated for exactly this library.
        assert result["indexed"] == 2
        libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in libs] == ["MyVendor"]

    def test_board_untouched(self, tools, tmp_path, project):
        _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        before = open(board, encoding="utf-8").read()
        _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="MyVendor", ctx=None
            )
        )
        assert open(board, encoding="utf-8").read() == before

    def test_no_overwrite_on_second_run(self, tools, tmp_path, project):
        _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
        # Real-world premise: the index is built (sync) before exporting.
        project["index_mgr"].index_library("TestSys", project["system_lib"])
        board = _make_board(tmp_path)
        first = _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="MyVendor", ctx=None
            )
        )
        assert first["exported_count"] == 2
        second = _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="MyVendor", ctx=None
            )
        )
        assert second["exported_count"] == 0
        assert second["failed_count"] == 2  # Sensor_Board_XYZ, Connector_Odd
        assert second["skipped_count"] == 1  # R_0402 lives in TestSys
        assert all(s["reason"].startswith("target file already exists") for s in second["failed"])

    def test_unknown_library_errors(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="NoSuchLib", ctx=None
            )
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_export_into_existing_user_library(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_3rdparty_library"](pcb_path=board, library="TestSys", ctx=None)
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
        libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in libs] == ["TestSys"]


class TestExportFileContent:
    def test_exported_mod_is_valid_and_clean(self, tools, tmp_path, project):
        _run(tools["create_3rdparty_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_3rdparty_library"](
                pcb_path=board, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        mod_path = os.path.join(
            project["third_party"], "footprints", "MyVendor.pretty", "Sensor_Board_XYZ.kicad_mod"
        )
        text = open(mod_path, encoding="utf-8").read()
        assert '(footprint "Sensor_Board_XYZ"' in text
        assert "(version 20260206)" in text
        assert "\t(at " not in text  # no placement
        assert "uuid" not in text
        assert "(net " not in text  # no pad nets
        assert '"Reference"' not in text
        # rotation: pad 90-45=45, text 90+45=135
        assert "(at -0.5 0.0 45.0)" in text
        assert "(at 0 2.0 135.0)" in text
