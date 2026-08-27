"""Tests for context_bridge: context collection and system prompt rendering."""

import os
from unittest.mock import MagicMock, patch

from kicad_plugin.context_bridge import (
    collect_context,
    context_to_system_prompt_block,
    project_path_from_title,
)


class TestCollectContextNoPcbnew:
    """When pcbnew is not importable (outside KiCad), context is empty."""

    def test_returns_all_none_fields(self):
        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=None):
            ctx = collect_context()
        assert ctx["active_project"] is None
        assert ctx["active_schematic"] is None
        assert ctx["active_pcb"] is None
        assert ctx["active_sheet"] is None

    def test_returns_dict(self):
        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=None):
            ctx = collect_context()
        assert isinstance(ctx, dict)


class TestCollectContextWithPcbnew:
    """Tests for the pcbnew-available code path."""

    def _make_mock_pcbnew(self, pcb_path=None):
        mock_pcbnew = MagicMock()
        mock_board = MagicMock()
        mock_board.GetFileName.return_value = pcb_path or ""
        mock_pcbnew.GetBoard.return_value = mock_board
        mock_pcbnew.GetCurrentFrame.return_value = None
        return mock_pcbnew

    def test_active_pcb_set_when_board_has_filename(self, tmp_path):
        pcb = str(tmp_path / "test.kicad_pcb")
        open(pcb, "w").close()  # create so abspath resolves
        mock_pcbnew = self._make_mock_pcbnew(pcb_path=pcb)

        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=mock_pcbnew):
            ctx = collect_context()

        assert ctx["active_pcb"] == os.path.abspath(pcb)

    def test_active_schematic_derived_from_pcb(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        sch = str(tmp_path / "board.kicad_sch")
        open(pcb, "w").close()
        open(sch, "w").close()
        mock_pcbnew = self._make_mock_pcbnew(pcb_path=pcb)

        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=mock_pcbnew):
            ctx = collect_context()

        assert ctx["active_schematic"] == os.path.abspath(sch)

    def test_active_pcb_not_set_when_filename_empty(self):
        mock_pcbnew = self._make_mock_pcbnew(pcb_path="")

        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=mock_pcbnew):
            ctx = collect_context()

        assert ctx["active_pcb"] is None

    def test_active_pcb_not_set_when_board_is_none(self):
        mock_pcbnew = MagicMock()
        mock_pcbnew.GetBoard.return_value = None
        mock_pcbnew.GetCurrentFrame.return_value = None

        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=mock_pcbnew):
            ctx = collect_context()

        assert ctx["active_pcb"] is None

    def test_get_board_raises_returns_empty_context(self):
        mock_pcbnew = MagicMock()
        mock_pcbnew.GetBoard.side_effect = RuntimeError("pcbnew internal error")
        mock_pcbnew.GetCurrentFrame.return_value = None

        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=mock_pcbnew):
            ctx = collect_context()  # must not raise

        assert isinstance(ctx, dict)


class TestContextToSystemPromptBlock:
    def test_includes_active_project(self):
        ctx = {
            "active_project": "/proj/test.kicad_pro",
            "active_schematic": "/proj/test.kicad_sch",
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "/proj/test.kicad_pro" in block

    def test_includes_active_schematic(self):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/test.kicad_sch",
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "/proj/test.kicad_sch" in block

    def test_no_project_shows_none(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "(none)" in block

    def test_schematic_path_in_instruction(self):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/board.kicad_sch",
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "schematic_path" in block
        assert "/proj/board.kicad_sch" in block

    def test_no_schematic_instruction_when_none(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "schematic_path" not in block

    def test_returns_string(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_sheet": None,
        }
        assert isinstance(context_to_system_prompt_block(ctx), str)

    def test_active_pcb_rendered_when_pcb_editor(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": pcb,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert pcb in block
        assert "Active PCB" in block

    def test_active_pcb_not_rendered_when_none(self, tmp_path):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/board.kicad_sch",
            "active_pcb": None,
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "Active PCB: /" not in block  # Should show "(none)" not a path


class TestProjectPathFromTitle:
    def test_pcb_editor_title_derives_existing_project(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pro = pcb.with_suffix(".kicad_pro")
        pcb.write_text("(kicad_pcb)")
        pro.write_text("(kicad_project)")
        assert project_path_from_title(f"PCB Editor: {pcb}") == os.path.abspath(str(pro))

    def test_schematic_editor_title_derives_project(self, tmp_path):
        sch = tmp_path / "board.kicad_sch"
        sch.with_suffix(".kicad_pro").write_text("(kicad_project)")
        sch.write_text("(kicad_sch)")
        assert project_path_from_title(f"Eeschema: {sch}") == os.path.abspath(
            str(sch.with_suffix(".kicad_pro"))
        )

    def test_project_title_direct(self, tmp_path):
        pro = tmp_path / "proj.kicad_pro"
        pro.write_text("(kicad_project)")
        assert project_path_from_title(f"KiCad - {pro}") == os.path.abspath(str(pro))

    def test_derived_project_missing_returns_none(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        # No sibling .kicad_pro — derivation fails and returns None.
        assert project_path_from_title(f"PCB Editor: {pcb}") is None

    def test_unrelated_title_returns_none(self):
        assert project_path_from_title("Settings Dialog") is None
        assert project_path_from_title("") is None
