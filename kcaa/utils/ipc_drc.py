"""
Design Rule Check (DRC) — opens the DRC dialog in KiCad via kipy IPC.

The action ``pcbnew.DRCTool.runDRC`` opens the Design Rules Checker.
The user clicks "Run DRC" to run the check and views results in the dialog.
"""

from typing import Any

from fastmcp import Context


async def run_drc_via_ipc(pcb_file: str, ctx: Context | None = None) -> dict[str, Any]:
    """Open the DRC dialog via kipy IPC.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb).
        ctx: MCP context for progress reporting.

    Returns:
        ``{"success": True}`` or an error dict.
    """
    try:
        from kcaa.tools.kipy_tools import _connect

        kicad = _connect(timeout_ms=10000)
        board = kicad.get_board()
        if board is None:
            return {
                "success": False,
                "error": "No board is currently open in KiCad.",
                "pcb_file": pcb_file,
            }

        kicad.run_action("pcbnew.DRCTool.runDRC")

        return {"success": True, "pcb_file": pcb_file}
    except RuntimeError as exc:
        return {
            "success": False,
            "error": f"KiCad connection failed: {exc}",
            "pcb_file": pcb_file,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Unexpected error: {exc}",
            "pcb_file": pcb_file,
        }
