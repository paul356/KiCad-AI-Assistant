"""
Context bridge: collect the active KiCad editor context and return it
as a structured dict that the plugin injects into every LLM request.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def _try_import_pcbnew():
    try:
        import pcbnew  # noqa: F401

        return pcbnew
    except ImportError:
        return None


def collect_context() -> dict[str, Any]:
    """
    Return the current KiCad editor context.

    Fields:
        active_project     Absolute path to .kicad_pro, or None
        active_schematic   Absolute path to .kicad_sch (root), or None
        active_pcb         Absolute path to .kicad_pcb, or None
        selected_refs      List of component reference strings currently selected
        active_sheet       Sheet path in hierarchical schematics, or None
    """
    ctx: dict[str, Any] = {
        "active_project": None,
        "active_schematic": None,
        "active_pcb": None,
        "selected_refs": [],
        "active_sheet": None,
    }

    pcbnew = _try_import_pcbnew()
    if pcbnew is None:
        log.debug("pcbnew not available — returning empty context")
        return ctx

    # ------------------------------------------------------------------ #
    # PCB file path + selection (single GetBoard() call)
    # ------------------------------------------------------------------ #
    try:
        board = pcbnew.GetBoard()
        if board:
            pcb_path = board.GetFileName()
            if pcb_path:
                ctx["active_pcb"] = os.path.abspath(pcb_path)

                proj_dir = os.path.dirname(pcb_path)
                proj_name = os.path.splitext(os.path.basename(pcb_path))[0]
                pro_path = os.path.join(proj_dir, proj_name + ".kicad_pro")
                if os.path.exists(pro_path):
                    ctx["active_project"] = os.path.abspath(pro_path)

                sch_path = os.path.join(proj_dir, proj_name + ".kicad_sch")
                if os.path.exists(sch_path):
                    ctx["active_schematic"] = os.path.abspath(sch_path)

            # Collect selected footprint references
            selection = []
            for fp in board.GetFootprints():
                try:
                    if fp.IsSelected():
                        selection.append(fp.GetReference())
                except Exception:
                    pass
            ctx["selected_refs"] = selection
    except Exception as e:
        log.debug("Could not collect PCB context: %s", e)

    return ctx


def context_to_system_prompt_block(ctx: dict[str, Any]) -> str:
    """
    Render the context dict as a plain-text block for insertion into
    the LLM system prompt.
    """
    lines = ["## Active KiCad Context"]

    if ctx.get("active_project"):
        lines.append(f"Active project: {ctx['active_project']}")
    else:
        lines.append("Active project: (none)")

    if ctx.get("active_schematic"):
        lines.append(f"Active schematic: {ctx['active_schematic']}")
    else:
        lines.append("Active schematic: (none)")

    if ctx.get("active_pcb"):
        lines.append(f"Active PCB: {ctx['active_pcb']}")
    else:
        lines.append("Active PCB: (none)")

    refs = ctx.get("selected_refs", [])
    if refs:
        lines.append(f"Selected components: {', '.join(refs)}")
    else:
        lines.append("Selected components: (none)")

    if ctx.get("active_sheet"):
        lines.append(f"Active sheet path: {ctx['active_sheet']}")

    if ctx.get("active_schematic"):
        lines.append(
            f"\nWhen editing the schematic, use {ctx['active_schematic']!r} "
            "as the schematic_path argument for all editing tools unless the "
            "engineer specifies a different file."
        )

    if ctx.get("active_pcb"):
        lines.append(
            f"\nWhen editing the PCB, use {ctx['active_pcb']!r} "
            "as the pcb_path argument for all PCB tools unless the "
            "engineer specifies a different file."
        )

    return "\n".join(lines)
