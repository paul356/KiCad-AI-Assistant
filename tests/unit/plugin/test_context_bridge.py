"""Tests for the pure context formatter.

Context collection lives in ``kicad_plugin/project_state.py``; this module
only turns a validated record into the system-prompt block.
"""

from kicad_plugin.context_bridge import context_to_system_prompt_block


def _state(root="/proj", profile="board.kicad_pro"):
    return {
        "version": 2,
        "written_at": "2026-11-17T09:12:33Z",
        "project_root": root,
        "profile_file": profile,
        "pcb_files": ["hardware/main.kicad_pcb"],
        "schematic_files": ["board.kicad_sch", "hardware/main_bus.kicad_sch"],
    }


def test_renders_root_project_file_and_absolute_manifests():
    block = context_to_system_prompt_block(_state())

    assert block.startswith("## Active KiCad Context")
    assert "Project root: /proj" in block
    assert "Project file: /proj/board.kicad_pro" in block
    assert "- /proj/board.kicad_sch" in block
    assert "- /proj/hardware/main_bus.kicad_sch" in block
    assert "- /proj/hardware/main.kicad_pcb" in block


def test_invalid_state_renders_nothing_at_all():
    assert context_to_system_prompt_block(None) == ""
    assert context_to_system_prompt_block({}) == ""


def test_block_never_names_an_active_or_current_file():
    block = context_to_system_prompt_block(_state())

    assert "active" not in block
    assert "current" not in block


def test_empty_manifests_still_render_the_headers():
    state = _state()
    state["pcb_files"] = []
    state["schematic_files"] = []

    block = context_to_system_prompt_block(state)

    assert "Schematic files:" in block
    assert "PCB files:" in block
