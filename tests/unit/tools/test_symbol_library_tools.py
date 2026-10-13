"""Tests for the symbol-library MCP tools:
``add_symbols_to_library`` and ``find_symbols_not_in_libraries``
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
    _find_project_dir,
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
    monkeypatch.setattr(
        "kcaa.tools.symbol_edit_tools._get_index_manager", lambda project_path=None: index_mgr
    )

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
# add_symbols_to_library
# ---------------------------------------------------------------------------


class TestAddSymbolToLibrary:
    def test_exports_cached_symbol(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbols_to_library"],
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
        """add_symbols_to_library handles *any* cached symbol (user said so),
        not only ones created via create_symbol (自定义:...)."""
        result = _run(
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:C"],
            library=env["lib"],
        )
        assert "error" not in result, result
        assert result["exported"] == ["TestLib:C"]
        assert result["exported_count"] == 1

    def test_plain_name_rejected(self, tools, env, tmp_sch):
        """Strict lib_id matching: plain names are rejected, not fuzzy-matched."""
        result = _run(
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["R_Small"],
            library=env["lib"],
        )
        assert result["exported"] == []
        assert result["failed"] == [
            {
                "symbol": "R_Small",
                "reason": "must_be_lib_id (plain names are not matched; use 'Library:Name')",
            }
        ]
        assert result["failed_count"] == 1

    def test_duplicate_export_is_skipped_not_overwritten(self, tools, env, tmp_sch):
        first = _run(
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library=env["lib"],
        )
        assert first["exported_count"] == 1
        second = _run(
            tools["add_symbols_to_library"],
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
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["Ghost:Missing"],
            library=env["lib"],
        )
        assert result["exported"] == []
        assert result["failed"] == [{"symbol": "Ghost:Missing", "reason": "not_in_schematic"}]
        assert result["failed_count"] == 1

    def test_unknown_library_returns_error(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library="NoSuchLib",
        )
        assert "error" in result
        assert "success" not in result

    def test_empty_symbols_returns_error(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbols_to_library"],
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
            tools["add_symbols_to_library"],
            schematic_path=str(sch_path),
            symbols=["Device:R_Small", "Device:C"],
            library=env["lib"],
        )
        assert sch_path.read_bytes() == before
        assert not Path(str(sch_path) + ".bak").exists()

    def test_two_symbols_batch_export(self, tools, env, tmp_sch):
        result = _run(
            tools["add_symbols_to_library"],
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

    def test_directory_type_library_no_false_missing(self, tools, env, tmp_sch):
        """KiCad 10 symdir layout: nickname -> directory of .kicad_sym files.

        Regression for the P1 where directory-type libraries were keyed as
        ``nickname/stem`` while lookup used the bare nickname, so every
        symbol in such a library was reported missing.
        """
        from kcaa.utils.sym_lib_table_utils import register_library_in_table
        from kcaa.utils.symbol_library_utils import list_library_symbols

        # Build a directory-type library "Device" with one .kicad_sym per symbol.
        device_dir = os.path.join(env["tmp_path"], "DeviceLib")
        os.makedirs(device_dir, exist_ok=True)
        names = ["R_Small", "C"]
        for name in names:
            path = os.path.join(device_dir, f"{name}.kicad_sym")
            Path(path).write_text(
                "(kicad_symbol_lib\n"
                "  (version 20220914)\n"
                f'  (symbol "{name}"\n'
                "    (in_bom yes)\n"
                "    (on_board yes)\n"
                "  )\n"
                ")\n"
            )
            assert name in list_library_symbols(path)

        reg = register_library_in_table(
            os.path.join(env["tmp_path"], "sym-lib-table"),
            "Device",
            device_dir,
            "directory-type test lib",
        )
        assert reg["registered"] is True, reg

        result = _run(tools["find_symbols_not_in_libraries"], schematic_path=tmp_sch)
        assert "error" not in result, result
        names = {(m["library"], m["name"]) for m in result["missing"]}
        # Device:R_Small / Device:C are present in the directory-type Device
        # library — they must NOT be reported missing.
        assert ("Device", "R_Small") not in names
        assert ("Device", "C") not in names

    def test_uses_index_after_completed_sync_even_if_dir_gone(
        self, tools, env, tmp_sch, monkeypatch
    ):
        """After a completed sync for this project, the index DB is
        authoritative: an indexed symbol is NOT missing even when its library
        directory is no longer live-scannable.  A partial/stale sync must not
        be trusted (the live fallback covers that — see the other tests).

        Mirrors the footprint-side
        ``test_uses_index_after_completed_sync_even_if_dir_gone``.
        """
        import kcaa.tools.symbol_tools as sym_tools
        from kcaa.utils.sym_lib_table_utils import register_library_in_table
        from kcaa.utils.symbol_index_manager import SymbolIndexManager, _project_dir_of
        from kcaa.utils.symbol_index_reader import SymbolIndexReader

        # Route the background sync at the same isolated index DB the tool
        # queries.  The sync manager needs a reader that sees the *project*
        # table (the env fixture's manager reads only the global table), so
        # build one pinned to the project dir; both managers share the same
        # temp DB file.
        project_dir = _project_dir_of(tmp_sch)
        sync_mgr = SymbolIndexManager(
            SymbolIndexReader(project_dir=project_dir),
            db_path=os.path.join(env["tmp_path"], "symbol_test.db"),
        )
        monkeypatch.setattr(
            "kcaa.tools.symbol_tools._get_index_manager", lambda project_path=None: sync_mgr
        )

        # Registered directory-type library "Device" containing only R_Small
        # (the fixture schematic references Device:R_Small and Device:C).
        device_dir = os.path.join(env["tmp_path"], "DeviceLib")
        os.makedirs(device_dir, exist_ok=True)
        Path(os.path.join(device_dir, "R_Small.kicad_sym")).write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            '  (symbol "R_Small"\n'
            "    (in_bom yes)\n"
            "    (on_board yes)\n"
            "  )\n"
            ")\n"
        )
        reg = register_library_in_table(
            os.path.join(env["tmp_path"], "sym-lib-table"),
            "Device",
            device_dir,
            "directory-type test lib",
        )
        assert reg["registered"] is True, reg

        # Run a real background sync scoped to this schematic, and wait for it.
        with sym_tools._sync_lock:
            sym_tools._sync_state.last_result = None
            sym_tools._sync_state.error = None
            sym_tools._sync_state.last_project_path = None
        sym_tools._run_sync_in_background(True, tmp_sch)
        with sym_tools._sync_lock:
            assert sym_tools._sync_state.last_result is not None
            assert sym_tools._sync_state.last_result["success"] is True
            assert sym_tools._sync_state.last_project_path == _project_dir_of(tmp_sch)

        # Library directory gone — the live path can no longer see R_Small;
        # only the (now-trusted) index can.
        shutil.rmtree(device_dir)
        assert not os.path.isdir(device_dir)

        result = _run(tools["find_symbols_not_in_libraries"], schematic_path=tmp_sch)
        assert "error" not in result, result
        names = {(m["library"], m["name"]) for m in result["missing"]}
        assert ("Device", "R_Small") not in names  # indexed -> exists
        assert ("Device", "C") in names  # never indexed -> missing
        assert result["missing_count"] == 1


# ---------------------------------------------------------------------------
# remove_symbols_from_library
# ---------------------------------------------------------------------------


def _seed_library(env, tools, tmp_sch, symbols=("Device:R_Small", "Device:C")):
    """Export fixture cached symbols into env's library; return their plain names."""
    result = _run(
        tools["add_symbols_to_library"],
        schematic_path=tmp_sch,
        symbols=list(symbols),
        library=env["lib"],
    )
    assert "error" not in result, result
    return [s.split(":")[-1] for s in result["exported"]]


class TestRemoveSymbolFromLibrary:
    def test_removes_one_symbol(self, tools, env, tmp_sch):
        names = _seed_library(env, tools, tmp_sch)
        assert "R_Small" in names

        result = _run(
            tools["remove_symbols_from_library"],
            library=env["lib"],
            symbols=["R_Small"],
        )
        assert "error" not in result, result
        assert result["removed"] == ["TestLib:R_Small"]
        assert result["removed_count"] == 1
        assert result["failed"] == []
        assert result["indexed"] == 1

        from kcaa.utils.symbol_library_utils import list_library_symbols

        lib_symbols = list_library_symbols(env["lib_path"])
        assert "R_Small" not in lib_symbols
        assert "C" in lib_symbols  # sibling untouched

    def test_batch_removes_multiple(self, tools, env, tmp_sch):
        _seed_library(env, tools, tmp_sch)
        result = _run(
            tools["remove_symbols_from_library"],
            library=env["lib"],
            symbols=["R_Small", "C"],
        )
        assert result["removed_count"] == 2
        assert set(result["removed"]) == {"TestLib:R_Small", "TestLib:C"}
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert list_library_symbols(env["lib_path"]) == []

    def test_missing_symbol_reported_in_failed(self, tools, env, tmp_sch):
        _seed_library(env, tools, tmp_sch)
        result = _run(
            tools["remove_symbols_from_library"],
            library=env["lib"],
            symbols=["R_Small", "Ghost"],
        )
        assert result["removed"] == ["TestLib:R_Small"]
        assert result["failed"] == [
            {"symbol": "Ghost", "reason": "not_in_library ('Ghost' not a top-level symbol)"}
        ]
        # valid name still removed — no partial-commit ambiguity

    def test_unsafe_name_rejected(self, tools, env):
        result = _run(
            tools["remove_symbols_from_library"],
            library=env["lib"],
            symbols=["../evil"],
        )
        assert result["removed"] == []
        assert result["failed"] == [{"symbol": "../evil", "reason": "unsafe_symbol_name"}]

    def test_unknown_library_returns_error(self, tools, env):
        result = _run(
            tools["remove_symbols_from_library"],
            library="NoSuchLib",
            symbols=["R_Small"],
        )
        assert "error" in result
        assert "success" not in result

    def test_empty_symbols_returns_error(self, tools, env):
        result = _run(tools["remove_symbols_from_library"], library=env["lib"], symbols=[])
        assert "error" in result

    def test_remove_last_symbol_leaves_valid_empty_library(self, tools, env, tmp_sch):
        _seed_library(env, tools, tmp_sch)
        _run(tools["remove_symbols_from_library"], library=env["lib"], symbols=["R_Small", "C"])
        text = Path(env["lib_path"]).read_text()
        assert text.strip().endswith(")")
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert list_library_symbols(env["lib_path"]) == []


# ---------------------------------------------------------------------------
# delete_symbol_library
# ---------------------------------------------------------------------------


class TestDeleteSymbolLibrary:
    def test_deletes_empty_library(self, tools, env):
        # env created TestLib empty.
        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" not in result, result
        assert result["deleted"] is True
        assert result["unregistered"] is True
        assert result["index_removed"] is True
        assert not os.path.exists(env["lib_path"])
        # Table entry dropped (a .bak copy of the table exists).
        table_text = Path(env["table_path"]).read_text()
        assert 'name "TestLib"' not in table_text
        assert os.path.isfile(env["table_path"] + ".bak")

    def test_refuses_non_empty_library(self, tools, env, tmp_sch):
        _seed_library(env, tools, tmp_sch)
        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" in result, result
        assert "not empty" in result["error"]
        assert "success" not in result
        # Library file and symbols survive untouched.
        assert os.path.isfile(env["lib_path"])
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert len(list_library_symbols(env["lib_path"])) == 2

    def test_empty_then_delete_flow(self, tools, env, tmp_sch):
        _seed_library(env, tools, tmp_sch)
        removed = _run(
            tools["remove_symbols_from_library"],
            library=env["lib"],
            symbols=["R_Small", "C"],
        )
        assert removed["removed_count"] == 2
        deleted = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" not in deleted, deleted
        assert deleted["deleted"] is True
        assert not os.path.exists(env["lib_path"])

    def test_unknown_library_returns_error(self, tools, env):
        result = _run(tools["delete_symbol_library"], library="NoSuchLib")
        assert "error" in result
        assert "success" not in result

    def test_deleted_library_absent_from_index(self, tools, env):
        _run(tools["delete_symbol_library"], library=env["lib"])
        assert env["index_mgr"].get_library_by_name("TestLib") is None

    def test_refuses_library_with_bare_atom_symbol_name(self, tools, env, tmp_sch):
        """Regression: a symbol whose name node is an unquoted atom
        (``(symbol FOO ...)`` instead of ``(symbol "FOO" ...)``) must still
        block deletion — the non-empty guard parses both forms."""
        # Reuse the seeded library, then rewrite its file with a bare-atom
        # top-level symbol (as a hand-written/third-party file may contain).
        _seed_library(env, tools, tmp_sch)
        bare = (
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol BARE\n"
            "    (in_bom yes)\n"
            "    (on_board yes)\n"
            "  )\n"
            ")\n"
        )
        Path(env["lib_path"]).write_text(bare)
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert "BARE" in list_library_symbols(env["lib_path"])

        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" in result, result
        assert "not empty" in result["error"]
        assert "success" not in result
        assert os.path.isfile(env["lib_path"])  # file survives

    def test_bare_atom_symbol_counted_by_list_library_symbols(self, tmp_path):
        """list_library_symbols recognizes both quoted (str) and unquoted
        (sexpdata.Symbol) top-level symbol name forms."""
        from kcaa.utils.symbol_library_utils import list_library_symbols

        lib = tmp_path / "bare.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol BARE\n"
            "    (in_bom yes)\n"
            "  )\n"
            '  (symbol "QUOTED"\n'
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
        )
        names = list_library_symbols(str(lib))
        assert "BARE" in names
        assert "QUOTED" in names


# ---------------------------------------------------------------------------
# System-library protection (never modify KiCad's own libraries)
# ---------------------------------------------------------------------------


class TestSystemLibraryGuard:
    """delete/remove symbol library tools must refuse libraries that
    resolve inside the KiCad installation (config.kicad_symbol_dir / ...).

    User libraries live in 3rd-party or project dirs; the tools' contract
    is "user libraries only" — system symdir/file libraries under
    /usr/share/kicad (or the platform app dir) are off limits.
    """

    def _register_system_lib(self, env, sys_dir):
        """Register and create a .kicad_sym that resolves inside sys_dir,
        as if a system library were registered in sym-lib-table."""
        from kcaa.utils.sym_lib_table_utils import register_library_in_table

        sys_dir.mkdir(parents=True, exist_ok=True)
        sys_file = sys_dir / "SysLib.kicad_sym"
        sys_file.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol SYS1\n"
            "    (in_bom yes)\n"
            "    (on_board yes)\n"
            "  )\n"
            ")\n"
        )
        # Register in the global user table used by the env fixture.
        table_path = env["table_path"]
        register_library_in_table(
            table_path,
            "SysLib",
            f"{sys_file}",
            description="system test lib",
        )
        return str(sys_file)

    def test_delete_refuses_system_library(self, tools, env, monkeypatch, tmp_path):
        """delete_symbol_library on a library whose file lives inside the
        KiCad system symbol dir must refuse with an error and not delete."""
        from kcaa.utils.config import config

        sys_dir = tmp_path / "system" / "symbols"
        sys_file = self._register_system_lib(env, sys_dir)
        monkeypatch.setattr(config, "_kicad_symbol_dir", str(sys_dir))

        # delete_symbol_library: must refuse pre-flight (empty library too).
        result = _run(tools["delete_symbol_library"], library="SysLib")
        assert "error" in result, result
        assert "system library" in result["error"]
        assert os.path.isfile(sys_file)  # file survives

    def test_remove_refuses_system_library(self, tools, env, monkeypatch, tmp_path):
        """remove_symbols_from_library on a system library must refuse."""
        from kcaa.utils.config import config

        sys_dir = tmp_path / "system" / "symbols"
        sys_file = self._register_system_lib(env, sys_dir)
        monkeypatch.setattr(config, "_kicad_symbol_dir", str(sys_dir))

        result = _run(tools["remove_symbols_from_library"], library="SysLib", symbols=["SYS1"])
        assert "error" in result, result
        assert "system library" in result["error"]
        assert os.path.isfile(sys_file)

    def test_user_library_inside_3rd_party_is_not_system(self, env):
        """A library created by create_symbol_library (3rd-party dir) must
        NOT be classified as a system library."""
        from kcaa.utils.config import config

        assert not config.is_system_library_path(env["lib_path"])

    def test_delete_refuses_library_outside_user_locations(self, tools, env, tmp_path):
        """delete_symbol_library must refuse a library whose file does not
        live in a user library location (3rd-party symbols dir or project
        dir) — even a valid, non-system user file."""
        from kcaa.utils.sym_lib_table_utils import register_library_in_table

        # Hand-made user library file outside MCP write locations.
        manual_dir = tmp_path / "manual_libs"
        manual_dir.mkdir()
        manual_file = manual_dir / "ManualLib.kicad_sym"
        manual_file.write_text(
            "(kicad_symbol_lib\n  (version 20220914)\n  (symbol M\n    (in_bom yes)\n  )\n)\n"
        )
        register_library_in_table(
            env["table_path"],
            "ManualLib",
            f"{manual_file}",
            description="Hand-managed, not MCP created",
        )

        result = _run(tools["delete_symbol_library"], library="ManualLib")
        assert "error" in result, result
        assert "user library location" in result["error"]
        assert os.path.isfile(manual_file)  # must survive

    def test_delete_accepts_mcp_created_library(self, tools, env):
        """delete_symbol_library succeeds on a library created by
        create_symbol_library (lives in the 3rd-party symbols dir)."""
        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" not in result, result
        assert result["deleted"] is True
        assert not os.path.isfile(env["lib_path"])

    def test_create_refuses_system_library(self, tools, env, monkeypatch, tmp_path):
        """create_symbol must refuse to write into a system library."""
        from kcaa.utils.config import config
        from kcaa.utils.symbol_library_utils import list_library_symbols

        sys_dir = tmp_path / "system" / "symbols"
        sys_file = self._register_system_lib(env, sys_dir)
        monkeypatch.setattr(config, "_kicad_symbol_dir", str(sys_dir))

        result = _run(
            tools["create_symbol"],
            library="SysLib",
            symbol_name="NEW1",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "left"}],
        )
        assert "error" in result, result
        assert "system library" in result["error"]
        assert "SYS1" in list_library_symbols(sys_file)  # file unchanged

    def test_add_symbols_refuses_system_library(self, tools, env, tmp_sch, monkeypatch, tmp_path):
        """add_symbols_to_library must refuse to write into a system library."""
        from kcaa.utils.config import config
        from kcaa.utils.symbol_library_utils import list_library_symbols

        sys_dir = tmp_path / "system" / "symbols"
        sys_file = self._register_system_lib(env, sys_dir)
        monkeypatch.setattr(config, "_kicad_symbol_dir", str(sys_dir))

        result = _run(
            tools["add_symbols_to_library"],
            schematic_path=tmp_sch,
            symbols=["Device:R_Small"],
            library="SysLib",
        )
        assert "error" in result, result
        assert "system library" in result["error"]
        assert "SYS1" in list_library_symbols(sys_file)  # file unchanged


# ---------------------------------------------------------------------------
# Ownership-scoped reindex + delete (Fix 1 / Fix 3)
# ---------------------------------------------------------------------------


class TestOwnershipScopedIndexWrites:
    """Editing a library must keep its OWN index ownership: a GLOBAL library
    edited from a project schematic stays global; deleting matches by
    ownership so another project's same-nickname row is never touched."""

    def _project_env(self, tmp_path, monkeypatch, env):
        """A project dir (with same-stem .kicad_pro + schematic copy) whose
        manager shares the env index DB with the global manager."""
        import os

        from kcaa.utils.config import ServerConfig
        from kcaa.utils.symbol_index_manager import SymbolIndexManager
        from kcaa.utils.symbol_index_reader import SymbolIndexReader

        proj = tmp_path / "proj"
        proj.mkdir()
        proj_real = os.path.realpath(str(proj))
        (proj / "tools_test.kicad_pro").write_text("")
        sch = proj / "tools_test.kicad_sch"
        shutil.copy(FIXTURE_SCH, sch)

        index_mgr_global = env["index_mgr"]  # db: tmp_path/symbol_test.db
        index_mgr_proj = SymbolIndexManager(
            SymbolIndexReader(ServerConfig(), project_dir=proj_real),
            db_path=str(tmp_path / "symbol_test.db"),
            project_path=proj_real,
        )

        def _dispatch(project_path=None):
            if project_path is None:
                return index_mgr_global
            if os.path.realpath(str(project_path)) == proj_real:
                return index_mgr_proj
            raise AssertionError(f"unexpected project path: {project_path}")

        monkeypatch.setattr("kcaa.tools.symbol_edit_tools._get_index_manager", _dispatch)
        return proj, proj_real, index_mgr_global, index_mgr_proj

    def test_edit_global_library_from_project_keeps_row_global(
        self, tools, env, tmp_path, monkeypatch
    ):
        """Fix 1 regression: exporting cached symbols from a project into a
        GLOBAL library must NOT re-attribute its index row to the project —
        the row's project stays '' and no other scope owns it."""
        _, proj_real, index_mgr_global, _ = self._project_env(tmp_path, monkeypatch, env)
        sch = str(tmp_path / "proj" / "tools_test.kicad_sch")
        assert (_find_project_dir(sch)) is not None, "project marker missing"

        result = _run(
            tools["add_symbols_to_library"],
            schematic_path=sch,
            symbols=["Device:R_Small"],
            library="TestLib",
        )
        assert "error" not in result, result

        row = index_mgr_global.get_library_by_name("TestLib")
        assert row is not None and row.project == ""
        # The project does not own it either — only the global scope does.
        assert index_mgr_global._db.get_library_by_name_exact("TestLib", proj_real) is None
        assert index_mgr_global._db.get_library_by_name_exact("TestLib", "") is not None

    def test_delete_project_library_without_project_dir_keeps_row(
        self, tools, env, tmp_path, monkeypatch
    ):
        """Fix 3(c): deleting a project library through the global manager
        (project_dir=None) cannot resolve it, so the index row survives."""

        proj, proj_real, _, index_mgr_proj = self._project_env(tmp_path, monkeypatch, env)
        created = _do_create_symbol_library("ProjOwned", project_dir=str(proj))
        assert "error" not in created, created
        assert index_mgr_proj.get_library_by_name("ProjOwned").project == proj_real

        # No project context: the project table is out of scope → resolution
        # error, and the row (visible only in the project scope) stays.
        result = _run(tools["delete_symbol_library"], library="ProjOwned")
        assert "error" in result, result
        assert index_mgr_proj.get_library_by_name("ProjOwned") is not None

    def test_delete_project_library_with_project_dir_removes_own_row(
        self, tools, env, tmp_path, monkeypatch
    ):
        """Fix 3: delete_symbol_library with the project context removes the
        project-owned row (and only it)."""
        proj, proj_real, _, index_mgr_proj = self._project_env(tmp_path, monkeypatch, env)
        created = _do_create_symbol_library("ProjOwned", project_dir=str(proj))
        assert "error" not in created, created

        result = _run(tools["delete_symbol_library"], library="ProjOwned", project_dir=str(proj))
        assert "error" not in result, result
        assert result["deleted"] is True
        assert index_mgr_proj.get_library_by_name("ProjOwned") is None


# ---------------------------------------------------------------------------
# Project-scope index wiring — create_symbol_library(project_dir=...) writes
# a project-scoped index row that global syncs never drop.
# ---------------------------------------------------------------------------


class TestCreateSymbolLibraryProjectScope:
    def test_project_create_scopes_index_row_and_survives_global_sync(self, tmp_path, monkeypatch):
        """Creating a project library indexes it under the project scope;
        the row is invisible to the global-scope manager and a subsequent
        global sync does not remove it."""
        # Point the KiCad config dir at tmp BEFORE constructing ServerConfig:
        # the global-scope sync reads the config sym-lib-table, and on clean
        # machines/CI the real ~/.config/kicad/<version> does not exist.
        # A valid empty table makes the reader treat the global scope as
        # "no global libraries" instead of raising FileNotFoundError.
        kicad_config = tmp_path / "kicad-config"
        kicad_config.mkdir()
        (kicad_config / "sym-lib-table").write_text(
            "(sym_lib_table\n  (version 1)\n)\n", encoding="utf-8"
        )
        monkeypatch.setenv("KICAD_CONFIG_DIR", str(kicad_config))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)

        from kcaa.utils.config import ServerConfig
        from kcaa.utils.symbol_index_manager import SymbolIndexManager
        from kcaa.utils.symbol_index_reader import SymbolIndexReader

        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        proj_real = os.path.realpath(str(proj_dir))

        index_mgr_global = SymbolIndexManager(SymbolIndexReader(ServerConfig()), db_path=":memory:")
        index_mgr_proj = SymbolIndexManager(
            SymbolIndexReader(ServerConfig(), project_dir=proj_real),
            db_path=":memory:",
            project_path=proj_real,
        )

        def _dispatch(project_path=None):
            if project_path is None:
                return index_mgr_global
            assert os.path.realpath(str(project_path)) == proj_real
            return index_mgr_proj

        monkeypatch.setattr("kcaa.tools.symbol_edit_tools._get_index_manager", _dispatch)
        # Isolate the exists-check from the real user tables.
        monkeypatch.setattr(
            "kcaa.tools.symbol_edit_tools.build_effective_symbol_library_list",
            lambda *a, **k: [],
        )

        result = _do_create_symbol_library("ProjLib", project_dir=proj_real)
        assert "error" not in result, result

        # Library file and project sym-lib-table were created in the project dir.
        assert os.path.isfile(os.path.join(proj_real, "ProjLib.kicad_sym"))
        table = Path(proj_real) / "sym-lib-table"
        assert "${KIPRJMOD}/ProjLib.kicad_sym" in table.read_text(encoding="utf-8")

        # Index row is project-scoped and invisible to the global manager.
        proj_row = index_mgr_proj.get_library_by_name("ProjLib")
        assert proj_row is not None
        assert proj_row.project == proj_real
        assert index_mgr_global.get_library_by_name("ProjLib") is None

        # A global sync must not drop the project row.
        stats = index_mgr_global.sync()
        assert stats.removed == 0
        assert index_mgr_proj.get_library_by_name("ProjLib") is not None


# ---------------------------------------------------------------------------
# P3-1: delete_symbol_library must not silently succeed (or hide the table
# entry) when unregister fails after the file was already removed — it
# reports file_removed/table_unregistered and still cleans the index.
# ---------------------------------------------------------------------------


class TestDeleteDanglingState:
    def test_unregister_failure_reports_partial_state(self, tools, env, monkeypatch):
        """unregister_library_in_table raising after file removal must yield
        an error stating exactly what was removed vs left, with index
        cleanup still attempted."""
        import kcaa.tools.symbol_edit_tools as mod

        def _boom(table_path, library, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "unregister_library_in_table", _boom)

        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" in result, result
        assert "sym-lib-table entry could not be unregistered" in result["error"]
        assert result["file_removed"] is True
        assert result["table_unregistered"] is False
        assert result["deleted"] is False
        # The file was removed before the unregister attempt…
        assert not os.path.isfile(env["lib_path"])
        # …and the index row was still cleaned up (table entry remains).
        assert result["index_removed"] is True
        assert env["index_mgr"].get_library_by_name("TestLib") is None
        assert env["table_path"] and os.path.isfile(env["table_path"])

    def test_unregister_returns_false_is_just_reported(self, tools, env, monkeypatch):
        """unregister_library_in_table returning unregistered=false after
        file removal is a reported, non-success state too."""
        import kcaa.tools.symbol_edit_tools as mod

        def _noop(table_path, library, **kwargs):
            return {"unregistered": False}

        monkeypatch.setattr(mod, "unregister_library_in_table", _noop)

        result = _run(tools["delete_symbol_library"], library=env["lib"])
        assert "error" not in result, result
        assert result["deleted"] is True  # file gone + index cleaned
        assert result["file_removed"] is True
        assert result["unregistered"] is False  # table entry left, reported


# ---------------------------------------------------------------------------
# P3-2: create_symbol with project_dir targets a project-local library.
# ---------------------------------------------------------------------------


class TestCreateSymbolProjectContext:
    def test_create_symbol_into_project_library(self, tools, env, tmp_path, monkeypatch):
        """create_symbol(project_dir=...) appends into a project-local
        library and reindexes it under the project scope."""
        helper = TestOwnershipScopedIndexWrites()
        proj, proj_real, index_mgr_global, index_mgr_proj = helper._project_env(
            tmp_path, monkeypatch, env
        )

        created = _do_create_symbol_library("ProjSym", project_dir=str(proj))
        assert "error" not in created, created

        result = _run(
            tools["create_symbol"],
            library="ProjSym",
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "left"}],
            project_dir=str(proj),
        )
        assert "error" not in result, result
        assert result.get("success") is True
        assert result["library"] == "ProjSym"

        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert "MYOP" in list_library_symbols(created["path"])

        # Reindexed under the PROJECT scope — visible there, not globally.
        proj_syms = {s.symbol_name for s in index_mgr_proj.get_library_symbols("ProjSym")}
        assert "MYOP" in proj_syms
        global_row = index_mgr_global.get_library_by_name("ProjSym")
        assert global_row is None

    def test_create_symbol_without_project_dir_keeps_global_default(
        self, tools, env, tmp_path, monkeypatch
    ):
        """Backward compat: project_dir omitted still resolves global-only
        (a project-local lib is not visible, so create refuses)."""
        helper = TestOwnershipScopedIndexWrites()
        proj, proj_real, _, index_mgr_proj = helper._project_env(tmp_path, monkeypatch, env)
        created = _do_create_symbol_library("ProjSym", project_dir=str(proj))
        assert "error" not in created, created

        result = _run(
            tools["create_symbol"],
            library="ProjSym",
            symbol_name="MYOP",
            pins=[{"number": "1", "name": "A", "type": "input", "direction": "left"}],
        )
        assert "error" in result, result  # global scope cannot resolve it
        assert index_mgr_proj.get_library_symbols("ProjSym") == []


# ---------------------------------------------------------------------------
# export_symbols_to_3rdparty
# ---------------------------------------------------------------------------


def _export_instance(lib_id: str, ref: str, x: float, uuid: str) -> str:
    """Render a minimal skip-parseable schematic symbol instance."""
    value = lib_id.partition(":")[2]
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x} 100.0 0) (unit 1) (body_style 1)\n'
        "    (exclude_from_sim no) (in_bom yes) (on_board yes)"
        " (in_pos_files yes) (dnp no) (fields_autoplaced yes)\n"
        f'    (uuid "{uuid}")\n'
        f'    (property "Reference" "{ref}" (at {x + 3} 99.0 0)'
        " (show_name no) (do_not_autoplace no)"
        " (effects (font (size 1.27 1.27))))\n"
        f'    (property "Value" "{value}" (at {x + 3} 101.0 0)'
        " (show_name no) (do_not_autoplace no)"
        " (effects (font (size 1.27 1.27))))\n"
        '    (pin "1" (number "1") (name "~"))\n'
        '    (pin "2" (number "2") (name "~"))\n'
        f'    (instances (project "demo" (path "/x" (reference "{ref}") (unit 1))))\n'
        "  )\n"
    )


def _export_mixed_schematic_text(bodyless: tuple[str, ...] = ()) -> str:
    """Render the export fixture: cached Device:R_Small / Device:C (from the
    base fixture, resolved via the isolated global Device library in tests)
    plus a cached custom inline symbol ``Custom:MYOP``, and one extra
    instance per *bodyless* lib_id (referenced but WITHOUT a cache entry)."""
    text = Path(FIXTURE_SCH).read_text(encoding="utf-8")
    unit = (
        '(symbol "MYOP_0_1" '
        "(pin passive line (at 0 2.54 270) (length 0.762) "
        '(name "" (effects (font (size 1.27 1.27)))) '
        '(number "1" (effects (font (size 1.27 1.27))))) '
        "(pin passive line (at 0 -2.54 90) (length 0.762) "
        '(name "" (effects (font (size 1.27 1.27)))) '
        '(number "2" (effects (font (size 1.27 1.27))))))'
    )
    custom_cache = (
        ' (symbol "Custom:MYOP" (pin_numbers (hide yes))\n'
        '  (property "Reference" "U" (at 0 0 0) (show_name no) (do_not_autoplace no)'
        " (effects (font (size 1.27 1.27))))\n"
        '  (property "Value" "MYOP" (at 0 0 0) (show_name no) (do_not_autoplace no)'
        " (effects (font (size 1.27 1.27))))\n"
        f"  {unit}\n"
        "  (embedded_fonts no))"
    )
    marker = "(embedded_fonts no)))"
    assert text.count(marker) == 1, "lib_symbols close marker moved"
    text = text.replace(marker, "(embedded_fonts no))" + custom_cache + ")", 1)

    instances = _export_instance("Custom:MYOP", "U1", 250.0, "11111111-2222-3333-4444-555555555555")
    for i, lib_id in enumerate(bodyless):
        instances += _export_instance(
            lib_id,
            f"U{2 + i}",
            260.0 + i,
            f"66666666-2222-3333-4444-{i:012d}",
        )
    return text.replace("(sheet_instances", instances + " (sheet_instances", 1)


def _register_global_symbol_library(env: dict, nickname: str, names: tuple[str, ...]) -> str:
    """Register a GLOBAL, file-style symbol library in the isolated env."""
    from kcaa.utils.sym_lib_table_utils import register_library_in_table

    lib_dir = Path(env["tmp_path"]) / f"global_{nickname.lower()}"
    lib_dir.mkdir(exist_ok=True)
    lib_file = lib_dir / f"{nickname}.kicad_sym"
    symbols = "\n".join(
        f'  (symbol "{n}" (in_bom yes) (on_board yes)'
        ' (property "Reference" "X" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        f' (property "Value" "{n}" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        " (embedded_fonts no))"
        for n in names
    )
    lib_file.write_text(
        "(kicad_symbol_lib\n  (version 20220914)\n  (generator kicad_symbol_editor)\n"
        f"{symbols}\n)\n",
        encoding="utf-8",
    )
    reg = register_library_in_table(
        os.path.join(env["tmp_path"], "sym-lib-table"),
        nickname,
        str(lib_file),
        "global test library",
    )
    assert reg.get("registered"), reg
    return str(lib_file)


class TestExportSymbolsTo3rdparty:
    """export_symbols_to_3rdparty materializes used cached symbols into
    project-owned 3rdparty libraries, registers them in the project
    sym-lib-table, and keeps existing lib_ids resolvable."""

    @staticmethod
    def _project(tmp_path, bodyless: tuple[str, ...] = ()) -> tuple[Path, str]:
        """Project dir (same-stem .kicad_pro) holding the export fixture."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tools_test.kicad_pro").write_text("{}", encoding="utf-8")
        sch = proj / "tools_test.kicad_sch"
        sch.write_text(_export_mixed_schematic_text(bodyless), encoding="utf-8")
        return proj, str(sch)

    def test_exports_used_cached_symbols_and_pins_prefixes(self, tools, env, tmp_path):
        """Symbols from a global library (Device:R_Small/Device:C) and a
        custom inline symbol (Custom:MYOP) are both materialized; the
        project table is re-pointed at the copies so the lib_ids keep
        resolving (pin the version)."""
        from kcaa.utils.config import ServerConfig
        from kcaa.utils.symbol_index_reader import SymbolIndexReader

        _register_global_symbol_library(env, "Device", ("R_Small", "C", "NE555"))
        proj, sch = self._project(tmp_path)
        proj_real = os.path.realpath(str(proj))
        before = Path(sch).read_bytes()

        result = _run(tools["export_symbols_to_3rdparty"], schematic_path=sch)
        assert "error" not in result, result
        assert result["project_dir"] == proj_real
        assert result["library_count"] == 2
        assert result["exported_count"] == 3
        assert set(result["exported"]) == {"Device:R_Small", "Device:C", "Custom:MYOP"}
        assert result["skipped"] == []
        assert result["failed"] == []

        device = result["libraries"]["Device"]
        assert device["exported_count"] == 2
        assert device["registered"] is True
        assert device["register_reason"] == "registered"
        assert device["written"] is True
        assert device["library_path"] == os.path.join(proj_real, "3rdparty", "Device.kicad_sym")
        assert device["table_path"] == os.path.join(proj_real, "sym-lib-table")
        assert device["indexed"] == 2

        custom = result["libraries"]["Custom"]
        assert custom["exported"] == ["Custom:MYOP"]
        assert custom["registered"] is True
        assert custom["register_reason"] == "registered"
        assert custom["indexed"] == 1

        # The definitions really landed in the 3rdparty .kicad_sym files.
        from kcaa.utils.symbol_library_utils import list_library_symbols

        assert set(list_library_symbols(device["library_path"])) == {"R_Small", "C"}
        assert set(list_library_symbols(custom["library_path"])) == {"MYOP"}

        # Project sym-lib-table gained ${KIPRJMOD}/3rdparty URIs and the
        # reader resolves the exported lib_ids through the project copy.
        libs = SymbolIndexReader(ServerConfig(), project_dir=proj_real).get_libraries()
        by_name = {e.name: e for e in libs}
        assert {"Device", "Custom"} <= set(by_name)
        assert by_name["Device"].uri == os.path.join(proj_real, "3rdparty", "Device.kicad_sym")
        assert by_name["Device"].table_path == os.path.join(proj_real, "sym-lib-table")
        assert by_name["Custom"].uri == os.path.join(proj_real, "3rdparty", "Custom.kicad_sym")

        # The source schematic is never modified.
        assert Path(sch).read_bytes() == before

    def test_explicit_project_dir_and_validation(self, tools, env, tmp_path):
        """project_dir may be passed explicitly; a missing dir errors."""
        proj, sch = self._project(tmp_path)
        result = _run(
            tools["export_symbols_to_3rdparty"],
            schematic_path=sch,
            project_dir=str(proj),
        )
        assert result["project_dir"] == os.path.realpath(str(proj))

        bad = _run(
            tools["export_symbols_to_3rdparty"],
            schematic_path=sch,
            project_dir=str(tmp_path / "nope"),
        )
        assert "error" in bad
        assert "Project directory not found" in bad["error"]

    def test_bodyless_resolvable_symbol_skipped_prefix_not_pinned(self, tools, env, tmp_path):
        """A used lib_id WITHOUT an inline body that already resolves via a
        registered library is skipped (not copied from the library), and the
        prefix is not re-pointed — its resolution is preserved."""
        from kcaa.utils.symbol_library_utils import list_library_symbols

        _register_global_symbol_library(env, "Device", ("R_Small", "C", "NE555"))
        proj, sch = self._project(tmp_path, bodyless=("Device:NE555",))

        result = _run(tools["export_symbols_to_3rdparty"], schematic_path=sch)
        assert "error" not in result, result
        assert any(
            s["symbol"] == "Device:NE555" and s["reason"].startswith("already_resolvable")
            for s in result["skipped"]
        )

        device = result["libraries"]["Device"]
        assert device["registered"] is False
        assert device["register_reason"].startswith("not_pinned")
        assert "NE555" not in list_library_symbols(device["library_path"])
        assert {"R_Small", "C"} <= set(list_library_symbols(device["library_path"]))

        # The project table has NO Device entry, so Device:NE555 keeps
        # resolving through the global library it already resolves via.
        table_text = (proj / "sym-lib-table").read_text(encoding="utf-8")
        assert 'name "Device"' not in table_text
        assert 'name "Custom"' in table_text

    def test_bodyless_unresolvable_symbol_failed(self, tools, env, tmp_path):
        """A used lib_id without an inline body and without any registered
        library is reported failed and never written."""
        from kcaa.utils.symbol_library_utils import list_library_symbols

        proj, sch = self._project(tmp_path, bodyless=("Device:NE555",))
        result = _run(tools["export_symbols_to_3rdparty"], schematic_path=sch)
        assert "error" not in result, result
        assert any(
            s["symbol"] == "Device:NE555" and s["reason"] == "no_inline_body"
            for s in result["failed"]
        )
        device = result["libraries"]["Device"]
        assert "NE555" not in list_library_symbols(device["library_path"])
        assert device["registered"] is False
        assert device["register_reason"].startswith("not_pinned")

    def test_idempotent_rerun(self, tools, env, tmp_path):
        """Re-running must not duplicate symbols: everything already present
        is skipped, the table keeps a single entry per prefix, and the
        result is stable."""
        from kcaa.utils.symbol_library_utils import list_library_symbols

        _register_global_symbol_library(env, "Device", ("R_Small", "C", "NE555"))
        proj, sch = self._project(tmp_path)

        first = _run(tools["export_symbols_to_3rdparty"], schematic_path=sch)
        assert "error" not in first, first
        second = _run(tools["export_symbols_to_3rdparty"], schematic_path=sch)
        assert "error" not in second, second

        assert second["exported"] == []
        assert second["exported_count"] == 0
        assert {s["symbol"] for s in second["skipped"]} == {
            "Device:R_Small",
            "Device:C",
            "Custom:MYOP",
        }
        assert all(s["reason"] == "already_in_library" for s in second["skipped"])
        assert second["skipped_count"] == first["exported_count"]
        assert set(second["libraries"]) == {"Device", "Custom"}
        assert second["libraries"]["Device"]["register_reason"] == "already_registered"
        assert second["libraries"]["Device"]["registered"] is True

        # No duplicated symbol definitions, no duplicated table entries.
        device_path = second["libraries"]["Device"]["library_path"]
        custom_path = second["libraries"]["Custom"]["library_path"]
        assert set(list_library_symbols(device_path)) == {"R_Small", "C"}
        assert set(list_library_symbols(custom_path)) == {"MYOP"}
        table_text = (proj / "sym-lib-table").read_text(encoding="utf-8")
        assert table_text.count('name "Device"') == 1
        assert table_text.count('name "Custom"') == 1

    def test_missing_schematic_returns_clean_error(self, tools, env, tmp_path):
        """A missing schematic yields a clean error, not a crash."""
        proj, _ = self._project(tmp_path)
        result = _run(
            tools["export_symbols_to_3rdparty"],
            schematic_path=str(proj / "missing.kicad_sch"),
        )
        assert "error" in result
        assert "Failed to open schematic" in result["error"]

    def test_empty_schematic_returns_clean_error(self, tools, env, tmp_path):
        """A schematic without symbol instances yields a clean error."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "tools_test.kicad_pro").write_text("{}", encoding="utf-8")
        sch = proj / "tools_test.kicad_sch"
        sch.write_text('(kicad_sch (version 20240108) (generator "eeschema"))', encoding="utf-8")

        result = _run(tools["export_symbols_to_3rdparty"], schematic_path=str(sch))
        assert "error" in result
        assert "no symbol instances" in result["error"]
