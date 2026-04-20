"""Tests for context_bridge: context collection and system prompt rendering."""
from unittest.mock import patch, MagicMock

import pytest

from kicad_plugin.context_bridge import collect_context, context_to_system_prompt_block


class TestCollectContextNoPcbnew:
    """When pcbnew is not importable (outside KiCad), context is empty."""

    def test_returns_all_none_fields(self):
        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=None):
            ctx = collect_context()
        assert ctx["active_project"] is None
        assert ctx["active_schematic"] is None
        assert ctx["active_pcb"] is None
        assert ctx["active_editor"] == "unknown"
        assert ctx["selected_refs"] == []
        assert ctx["active_sheet"] is None

    def test_returns_dict(self):
        with patch("kicad_plugin.context_bridge._try_import_pcbnew", return_value=None):
            ctx = collect_context()
        assert isinstance(ctx, dict)


class TestContextToSystemPromptBlock:
    def test_includes_active_project(self):
        ctx = {
            "active_project": "/proj/test.kicad_pro",
            "active_schematic": "/proj/test.kicad_sch",
            "active_pcb": None,
            "active_editor": "schematic",
            "selected_refs": ["R1", "C3"],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "/proj/test.kicad_pro" in block

    def test_includes_active_schematic(self):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/test.kicad_sch",
            "active_pcb": None,
            "active_editor": "schematic",
            "selected_refs": [],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "/proj/test.kicad_sch" in block

    def test_includes_selected_refs(self):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/test.kicad_sch",
            "active_pcb": None,
            "active_editor": "schematic",
            "selected_refs": ["U1", "U2"],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "U1" in block
        assert "U2" in block

    def test_no_project_shows_none(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_editor": "unknown",
            "selected_refs": [],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        assert "(none)" in block

    def test_schematic_path_in_instruction(self):
        ctx = {
            "active_project": None,
            "active_schematic": "/proj/board.kicad_sch",
            "active_pcb": None,
            "active_editor": "schematic",
            "selected_refs": [],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        # Should tell the LLM to use this path
        assert "schematic_path" in block
        assert "/proj/board.kicad_sch" in block

    def test_no_schematic_instruction_when_none(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_editor": "unknown",
            "selected_refs": [],
            "active_sheet": None,
        }
        block = context_to_system_prompt_block(ctx)
        # No schematic path → no schematic_path instruction
        assert "schematic_path" not in block

    def test_returns_string(self):
        ctx = {
            "active_project": None,
            "active_schematic": None,
            "active_pcb": None,
            "active_editor": "unknown",
            "selected_refs": [],
            "active_sheet": None,
        }
        assert isinstance(context_to_system_prompt_block(ctx), str)
