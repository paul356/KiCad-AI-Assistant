"""Tests for the Auto Route pre-routing project snapshot."""

from kicad_plugin import project_state as pstate
from kicad_plugin.ui.panel import _take_autoroute_snapshot


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
