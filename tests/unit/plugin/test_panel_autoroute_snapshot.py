"""Tests for the Auto Route pre-routing project snapshot."""

import sys
import types
from unittest.mock import patch

from kicad_plugin import project_state as pstate
from kicad_plugin.ui.panel import _take_autoroute_snapshot
from tests.unit.plugin.test_active_project_state import _panel_module


def _publish(state_file, pro):
    record = pstate.build_record(str(pro))
    assert record is not None
    assert pstate.write_state(str(state_file), record)
    return state_file


def test_snapshot_targets_real_project_for_nested_board(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    board = hardware / "main_bus.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    state_file = _publish(tmp_path / "project_state.json", pro)
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True, "archive_path": str(tmp_path / "archive.tar.gz")}

    backup_path, error = _take_autoroute_snapshot(
        str(board), "http://mcp", call_tool, str(state_file)
    )

    assert error is None
    assert backup_path == str(tmp_path / "archive.tar.gz")
    assert calls == [
        (
            "http://mcp",
            "save_project_version",
            {"project_file": str(pro), "include_files": [str(board)]},
        )
    ]


def test_no_state_aborts_before_snapshot_or_routing(tmp_path):
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    board = hardware / "main_bus.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True}

    backup_path, error = _take_autoroute_snapshot(str(board), "http://mcp", call_tool)

    assert backup_path == ""
    assert error is not None
    assert "retry" in error
    assert calls == []  # no snapshot call, so the router is never launched
    assert not (tmp_path / ".versions").exists()


def test_state_for_another_root_aborts_before_snapshot_or_routing(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    other_pro = other / "other.kicad_pro"
    other_pro.write_text("{}\n")
    state_file = _publish(tmp_path / "project_state.json", other_pro)
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    board = hardware / "main_bus.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True}

    backup_path, error = _take_autoroute_snapshot(
        str(board), "http://mcp", call_tool, str(state_file)
    )

    assert backup_path == ""
    assert error is not None
    assert "outside the active KiCad project" in error
    assert calls == []


def test_unsaved_board_aborts_before_snapshot_or_routing(tmp_path):
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True}

    for board_path in ("", str(tmp_path / "not_saved.kicad_pcb")):
        backup_path, error = _take_autoroute_snapshot(board_path, "http://mcp", call_tool)

        assert backup_path == ""
        assert error is not None
        assert "Save the .kicad_pcb" in error
        assert calls == []


def test_snapshot_includes_every_project_file_from_state(tmp_path):
    import json

    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    root_sch = tmp_path / "aria.kicad_sch"
    root_sch.write_text("(kicad_sch)\n")
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    board = hardware / "main_bus.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    nested_sch = hardware / "main_bus.kicad_sch"
    nested_sch.write_text("(kicad_sch)\n")
    state_file = tmp_path / "project_state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 2,
                "written_at": "2026-11-17T09:12:33Z",
                "project_root": str(tmp_path),
                "profile_file": pro.name,
                "pcb_files": ["hardware/main_bus.kicad_pcb"],
                "schematic_files": ["aria.kicad_sch", "hardware/main_bus.kicad_sch"],
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True, "archive_path": str(tmp_path / "archive.tar.gz")}

    backup_path, error = _take_autoroute_snapshot(
        str(board), "http://mcp", call_tool, str(state_file)
    )

    assert error is None
    assert backup_path == str(tmp_path / "archive.tar.gz")
    assert calls == [
        (
            "http://mcp",
            "save_project_version",
            {
                "project_file": str(pro),
                "include_files": [str(board), str(root_sch), str(nested_sch)],
            },
        )
    ]


def test_snapshot_tool_exception_returns_an_error(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    board = tmp_path / "aria.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    state_file = _publish(tmp_path / "project_state.json", pro)

    def call_tool(base_url, name, args):
        raise RuntimeError("mcp down")

    backup_path, error = _take_autoroute_snapshot(
        str(board), "http://mcp", call_tool, str(state_file)
    )

    assert backup_path == ""
    assert error is not None
    assert "could not save a project snapshot" in error
    assert "mcp down" in error


def test_snapshot_tool_error_returns_an_error(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    board = tmp_path / "aria.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    state_file = _publish(tmp_path / "project_state.json", pro)

    def call_tool(base_url, name, args):
        return {"error": "disk full"}

    backup_path, error = _take_autoroute_snapshot(
        str(board), "http://mcp", call_tool, str(state_file)
    )

    assert backup_path == ""
    assert error is not None
    assert "could not save a project snapshot" in error
    assert "disk full" in error


def _autoroute_panel(module, state_file):
    obj = object.__new__(module.AssistantPanel)
    obj._on_autoroute = types.MethodType(module.AssistantPanel._on_autoroute, obj)
    entries = []
    obj._busy = False
    obj._append_entry = entries.append
    obj._render_conversation = lambda *args, **kwargs: None
    obj._set_status = lambda *args, **kwargs: None
    obj.Layout = lambda: None
    obj._state_path = lambda: str(state_file)
    obj._server_mgr = types.SimpleNamespace(base_url="http://mcp")
    obj._C_WARN_HEX = "#ff0000"
    obj._C_WARN = 0
    obj._menu_autoroute_id = 1
    obj.GetMenuBar = lambda: types.SimpleNamespace(Enable=lambda *args: None)
    return obj, entries


def _run_autoroute_with_snapshot_failure(tmp_path, monkeypatch, call_tool):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    board_path = tmp_path / "aria.kicad_pcb"
    board_path.write_text("(kicad_pcb)\n")
    state_file = _publish(tmp_path / "project_state.json", pro)

    board = types.SimpleNamespace(
        GetFileName=lambda: str(board_path),
        GetNetCount=lambda: 2,
        FindNet=lambda code: types.SimpleNamespace(GetNetname=lambda: f"NET{code}"),
    )
    fake_pcbnew = types.ModuleType("pcbnew")
    fake_pcbnew.GetBoard = lambda: board
    fake_pcbnew.ExportSpecctraDSN = lambda *args: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)

    with _panel_module() as module:
        fake_wx = sys.modules["wx"]
        ok = object()
        fake_wx.ID_OK = ok
        fake_wx.OK = 0
        fake_wx.ICON_INFORMATION = 0

        class _Dialog:
            def __init__(self, *args, **kwargs):
                pass

            def ShowModal(self):
                return ok

            def SetSelections(self, selections):
                pass

            def GetSelections(self):
                return []

            def Destroy(self):
                pass

        fake_wx.MultiChoiceDialog = _Dialog
        obj, entries = _autoroute_panel(module, state_file)
        with (
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=call_tool),
            patch("kicad_plugin.autorouter.start_freerouting_thread") as start,
        ):
            obj._on_autoroute(types.SimpleNamespace())
    return entries, start


def test_autoroute_does_not_start_when_snapshot_raises(tmp_path, monkeypatch):
    def call_tool(base_url, name, args):
        raise RuntimeError("mcp down")

    entries, start = _run_autoroute_with_snapshot_failure(tmp_path, monkeypatch, call_tool)

    assert start.call_count == 0
    assert any(
        entry.get("type") == "status"
        and "could not save a project snapshot" in entry.get("text", "")
        for entry in entries
    )


def test_autoroute_does_not_start_when_snapshot_returns_an_error(tmp_path, monkeypatch):
    def call_tool(base_url, name, args):
        return {"error": "disk full"}

    entries, start = _run_autoroute_with_snapshot_failure(tmp_path, monkeypatch, call_tool)

    assert start.call_count == 0
    assert any(
        entry.get("type") == "status" and "disk full" in entry.get("text", "") for entry in entries
    )
