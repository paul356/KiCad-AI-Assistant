"""Tests that tools use the centralized CLI path instead of constructing one."""

import subprocess
from types import SimpleNamespace

import pytest

from kcaa.tools.bom_tools import export_bom_with_cli
from kcaa.tools.export_tools import generate_thumbnail_with_cli

FAKE_CLI = "/fake/bin/kicad-cli"


def _write_output(cmd: list[str], content: bytes = b"Reference,Value\nR1,10k\n"):
    output_index = cmd.index("--output") + 1
    with open(cmd[output_index], "wb") as handle:
        handle.write(content)


class TestBomTool:
    @pytest.mark.asyncio
    async def test_uses_detected_cli(self, tmp_path, monkeypatch):
        """The BOM command starts with the path returned by find_kicad_cli."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            _write_output(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("kcaa.tools.bom_tools.find_kicad_cli", lambda: FAKE_CLI)
        monkeypatch.setattr(subprocess, "run", fake_run)
        schematic = tmp_path / "board.kicad_sch"
        schematic.write_text("(kicad_sch)", encoding="utf-8")

        result = await export_bom_with_cli(str(schematic), str(tmp_path), "proj", None)

        assert result["success"] is True
        assert captured["cmd"][0] == FAKE_CLI

    @pytest.mark.asyncio
    async def test_missing_cli_returns_error(self, tmp_path, monkeypatch):
        """No detected CLI produces the existing error structure, no subprocess."""
        monkeypatch.setattr("kcaa.tools.bom_tools.find_kicad_cli", lambda: None)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("subprocess must not run without a CLI"),
        )
        schematic = tmp_path / "board.kicad_sch"
        schematic.write_text("(kicad_sch)", encoding="utf-8")

        result = await export_bom_with_cli(str(schematic), str(tmp_path), "proj", None)

        assert result["success"] is False
        assert "kicad-cli not found" in result["error"]
        assert result["schematic_file"] == str(schematic)


class TestExportTool:
    @pytest.mark.asyncio
    async def test_uses_detected_cli(self, tmp_path, monkeypatch):
        """The SVG export command starts with the detected CLI path."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            _write_output(list(cmd), b"<svg></svg>")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("kcaa.tools.export_tools.find_kicad_cli", lambda: FAKE_CLI)
        monkeypatch.setattr(subprocess, "run", fake_run)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)", encoding="utf-8")

        result = await generate_thumbnail_with_cli(str(pcb), None)

        assert result is not None
        assert captured["cmd"][0] == FAKE_CLI

    @pytest.mark.asyncio
    async def test_missing_cli_returns_none(self, tmp_path, monkeypatch, capsys):
        """No detected CLI logs a hint and returns None without invoking a process."""
        monkeypatch.setattr("kcaa.tools.export_tools.find_kicad_cli", lambda: None)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("subprocess must not run without a CLI"),
        )
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)", encoding="utf-8")

        result = await generate_thumbnail_with_cli(str(pcb), None)

        assert result is None
        assert "kicad-cli not found" in capsys.readouterr().out
