"""Tests for the Auto Route pre-routing project snapshot."""

from kicad_plugin.ui.panel import _take_autoroute_snapshot


def test_snapshot_targets_real_project_for_nested_board(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    board = hardware / "main_bus.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    calls = []

    def call_tool(base_url, name, args):
        calls.append((base_url, name, args))
        return {"success": True, "archive_path": str(tmp_path / "archive.tar.gz")}

    backup_path, error = _take_autoroute_snapshot(str(board), "http://mcp", call_tool)

    assert error is None
    assert backup_path == str(tmp_path / "archive.tar.gz")
    assert calls == [
        (
            "http://mcp",
            "save_project_version",
            {"project_file": str(pro), "include_files": [str(board)]},
        )
    ]


def test_no_project_aborts_before_snapshot_or_routing(tmp_path, isolated_project_search):
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
    assert str(board) in error
    assert ".kicad_pro" in error
    assert "Save" in error
    assert calls == []  # no snapshot call, so the router is never launched
    assert not (tmp_path / ".versions").exists()
