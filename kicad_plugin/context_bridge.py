"""Render the published project state as a system-prompt block.

Collection lives in ``kicad_plugin/project_state.py``: the panel scans the
open project and writes ``project_state.json``; this module only formats the
validated record.  It never probes KiCad, never reads window titles and keeps
no cache, so an invalid (or missing) record renders nothing at all.
"""

from __future__ import annotations

import os


def context_to_system_prompt_block(state: dict | None) -> str:
    """Render *state* as a plain-text block for the LLM system prompt.

    Returns an empty string for an invalid/missing record.  The block lists
    the project root, the project file and both file manifests; it never
    singles out a "current" file, because the state does not know which
    editor has focus.
    """
    if not state:
        return ""

    root = state["project_root"]
    project_file = os.path.join(root, state["profile_file"])
    lines = [
        "## Active KiCad Context",
        f"Project root: {root}",
        f"Project file: {project_file}",
        "Schematic files:",
    ]
    lines.extend(f"- {name}" for name in state.get("schematic_files", ()))
    lines.append("PCB files:")
    lines.extend(f"- {name}" for name in state.get("pcb_files", ()))
    return "\n".join(lines)
