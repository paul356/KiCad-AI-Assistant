"""Tests for the symbol-library MCP tools:
``add_symbol_to_library`` and ``find_symbols_not_in_libraries``
(kcaa.tools.symbol_edit_tools).

Both tools are read-only with respect to the schematic: they never modify
the source .kicad_sch.  Disk writes go to the isolated per-test symbol
library environment (config dir / 3rd-party dir / index DB in tmp_path).
"""

import asyncio
import os
from pathlib import Path
import shutil

import pytest

from kcaa.tools.symbol_edit_tools import (
    _do_add_symbol_to_library,
    _do_create_symbol_library,
)

FIXTURE_SCH = str(
    Path(__file__).parent / "fixtures/tools_test.kicad_sch"
)  # has Device:R_Small and Device:C cached


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.symbol_edit_tools import register_symbol_edit_tools

    mock = _MockMCP()
    register_symbol_edit_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated symbol-library environment (config/3rd-party/index in tmp)."""
    from kcaa.utils import pcb_library_utils
    from kcaa.utils.config import config

    third_party = tmp_path / "3rdparty"
    third_party.mkdir()
    (third_party / "symbols").mkdir()

    monkeypatch.setattr(pcb_library_utils, "_default_kicad_config_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(config, "_kicad_3rd_party", str(third_party))
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._3rd_party_symbols_dir",
        lambda: str(third_party / "symbols"),
    )
    from kcaa.utils.config import ServerConfig
    from kcaa.utils.symbol_index_manager import SymbolIndexManager
    from kcaa.utils.symbol_index_reader import SymbolIndexReader

    index_mgr = SymbolIndexManager(
        SymbolIndexReader(ServerConfig()), db_path=str(tmp_path / "symbol_test.db")
    )
    monkeypatch.setattr("kcaa.tools.symbol_edit_tools._get_index_manager", lambda: index_mgr)

    created = _do_create_symbol_library("TestLib")
    assert "error" not in created, created
    return {
        "tmp_path": str(tmp_path),
        "lib": "TestLib",
        "lib_path": created["path"],
        "table_path": created["table_path"],
        "index_mgr": index_mgr,
    }


@pytest.fixture()
def tmp_sch(tmp_path):
    """A throwaway copy of the fixture schematic inside tmp_path.

    Sitting in tmp_path means project-sym-lib-table lookup (which checks the
    schematic's directory) resolves to the isolated table from ``env``.
    """
    dst = tmp_path / "tools_test.kicad_sch"
    shutil.copy(FIXTURE_SCH, dst)
    yield str(dst)
    # Never leave .bak behind across tests.
    if (tmp_path / "tools_test.kicad_sch.bak").exists():
        (tmp_path / "tools_test.kicad_sch.bak").unlink()


def _run(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


# ---------------------------------------------------------------------------
# add_symbol_to_library
# ---------------------------------------------------------------------------


class TestAddSymbolToLibrary:
    def test_exports_cached_symbol(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library=env["lib"],
        )
        assert "error" not in result, result
        assert result["exported"] == ["TestLib:R_Small"]
        assert result["exported_count"] == 1
        assert result["failed"] == []
        assert result["skipped"] == []
        assert result["indexed"] == 1

        # The definition really landed in the .kicad_sym file.
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert "R_Small" in list_library_symbols(env["lib_path"])

    def test_any_cached_symbol_not_just_custom(self, tools, env, tmp_sch):
        """add_symbol_to_library handles *any* cached symbol (user said so),
        not only ones created via create_symbol (自定义:...)."""
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:C"],
            library=env["lib"],
        )
        assert "error" not in result, result
        assert result["exported"] == ["TestLib:C"]
        assert result["exported_count"] == 1

    def test_plain_name_matches_local_part(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["R_Small"],
            library=env["lib"],
        )
        assert "error" not in result, result
        assert result["exported"] == ["TestLib:R_Small"]

    def test_duplicate_export_is_skipped_not_overwritten(self, tools, env, tmp_sch):
        first = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library=env["lib"],
        )
        assert first["exported_count"] == 1
        second = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library=env["lib"],
        )
        assert second["exported"] == []
        assert second["skipped"] == [{"symbol": "Device:R_Small", "reason": "already_in_library"}]
        assert second["skipped_count"] == 1
        assert second["exported_count"] == 0

    def test_missing_symbol_reported_in_failed(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Ghost"],
            library=env["lib"],
        )
        assert result["exported"] == []
        assert result["failed"] == [{"symbol": "Ghost", "reason": "not_in_schematic"}]
        assert result["failed_count"] == 1

    def test_unknown_library_returns_error(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library="NoSuchLib",
        )
        assert "error" in result
        assert "success" not in result

    def test_empty_symbols_returns_error(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=[],
            library=env["lib"],
        )
        assert "error" in result

    def test_schematic_untouched(self, tools, env):
        """The source schematic is never modified, backed up, or moved."""
        tmp_dir = Path(env["tmp_path"]) / "proj"
        tmp_dir.mkdir()
        sch_path = tmp_dir / "copy.kicad_sch"
        shutil.copy(FIXTURE_SCH, sch_path)
        before = sch_path.read_bytes()

        _run(
            tools["add_symbol_to_library"],
            schematic_path=str(sch_path),
            symbols=["Device:R_Small", "Device:C"],
            library=env["lib"],
        )
        assert sch_path.read_bytes() == before
        assert not Path(str(sch_path) + ".bak").exists()

    def test_two_symbols_batch_export(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbol_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small", "Device:C"],
            library=env["lib"],
        )
        assert result["exported_count"] == 2
        assert set(result["exported"]) == {"TestLib:R_Small", "TestLib:C"}


# ---------------------------------------------------------------------------
# find_symbols_not_in_libraries
# ---------------------------------------------------------------------------


class TestFindSymbolsNotInLibraries:
    def test_reports_cached_symbols_missing_from_libraries(self, tools, env, tmp_sch):
        """Device:R_Small / Device:C exist only as schematic cache — no
        ``Device`` library is registered, so both are reported."""
        result = _run(tools["find_symbols_not_in_libraries"], schematic_path=tmp_sch)
        assert "error" not in result, result
        names = {(m["library"], m["name"]) for m in result["missing"]}
        assert ("Device", "R_Small") in names
        assert ("Device", "C") in names
        assert result["missing_count"] == len(result["missing"]) >= 2

    def test_after_export_symbol_is_no_longer_missing(self, tools, env, tmp_sch):
        """After exporting a cached symbol into a library named like its
        lib_id prefix, find stops reporting it."""
        created = _do_create_symbol_library("Device")
        assert "error" not in created, created
        added = _do_add_symbol_to_library(tmp_sch, ["Device:R_Small"], "Device")
        assert added["exported_count"] == 1, added

        result = _run(tools["find_symbols_not_in_libraries"], schematic_path=tmp_sch)
        assert "error" not in result, result
        names = {(m["library"], m["name"]) for m in result["missing"]}
        assert ("Device", "R_Small") not in names
        assert ("Device", "C") in names  # C was not exported

    def test_read_only(self, tools, env, tmp_sch):
        before = os.path.getmtime(tmp_sch)
        _run(tools["find_symbols_not_in_libraries"], schematic_path=tmp_sch)
        assert os.path.getmtime(tmp_sch) == before

    def test_missing_file_returns_error(self, tools, env):
        result = _run(
            tools["find_symbols_not_in_libraries"],
            schematic_path="/no/such/file.kicad_sch",
        )
        assert "error" in result
        assert "success" not in result
