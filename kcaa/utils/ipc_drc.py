"""
Design Rule Check (DRC) implementation using KiCad IPC (kipy) + pcbnew.

Triggers DRC via kipy's run_action, then reads violation markers using
the pcbnew board API (KiCad's native Python module, always available inside
the KiCad process).  No kicad-cli involved.
"""

import time
from typing import Any

from fastmcp import Context


async def run_drc_via_ipc(pcb_file: str, ctx: Context | None = None) -> dict[str, Any]:
    """Run DRC using KiCad IPC API + pcbnew marker reading.

    Workflow:
      1. Connect to KiCad via kipy.
      2. Clear existing markers (run_action).
      3. Trigger DRC (run_action).
      4. Read markers via pcbnew board API.
      5. Parse markers into violations dict.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb) — used for logging.
        ctx: MCP context for progress reporting.

    Returns:
        dict with keys: success, total_violations, violations,
        violation_categories, pcb_file.
    """
    results: dict[str, Any] = {
        "success": False,
        "pcb_file": pcb_file,
        "total_violations": 0,
        "violations": [],
        "violation_categories": {},
    }

    try:
        # --- Step 1: Connect to KiCad via kipy ---
        from kcaa.tools.kipy_tools import _connect

        kicad = _connect(timeout_ms=10000)
        board = kicad.get_board()
        if board is None:
            results["error"] = "No board is currently open in KiCad."
            return results

        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info("Clearing previous DRC markers...")

        # --- Step 2: Clear existing markers ---
        kicad.run_action("pcbnew.InspectionTool.clearMarkers")

        if ctx:
            await ctx.report_progress(20, 100)
            ctx.info("Running DRC via KiCad IPC...")

        # --- Step 3: Run DRC ---
        kicad.run_action("pcbnew.InspectionTool.runDRC")

        # DRC is synchronous inside KiCad, but give a short grace period
        # for markers to populate.
        time.sleep(0.5)

        if ctx:
            await ctx.report_progress(60, 100)
            ctx.info("Reading DRC markers...")

        # --- Step 4: Read markers via pcbnew ---
        import pcbnew  # noqa: PLC0415 — only available inside KiCad process

        ki_board = pcbnew.GetBoard()
        if ki_board is None:
            results["error"] = "Cannot access board via pcbnew. Is KiCad running?"
            return results

        markers = ki_board.GetMarkers()

        # --- Step 5: Parse markers ---
        violations = []
        error_types: dict[str, int] = {}

        for marker in markers:
            desc = marker.GetDescription()
            pos = marker.GetPosition()

            # Determine severity
            severity_raw = marker.GetSeverity()
            severity = _severity_to_string(severity_raw)

            violation = {
                "message": desc,
                "severity": severity,
                "location": {
                    "x": pcbnew.ToMM(pos.x),
                    "y": pcbnew.ToMM(pos.y),
                },
            }
            violations.append(violation)

            # Categorize
            category = desc if desc else "Unknown"
            error_types[category] = error_types.get(category, 0) + 1

        results["success"] = True
        results["total_violations"] = len(violations)
        results["violations"] = violations
        results["violation_categories"] = error_types

        if ctx:
            await ctx.report_progress(100, 100)
            ctx.info(
                f"DRC completed: {len(violations)} violation(s) in {len(error_types)} categories."
            )

        return results

    except ImportError as exc:
        results["error"] = (
            f"Required module not available: {exc}. "
            "DRC via IPC requires kipy (running KiCad) and pcbnew."
        )
        return results
    except RuntimeError as exc:
        results["error"] = f"KiCad connection failed: {exc}"
        return results
    except Exception as exc:
        results["error"] = f"Unexpected error in DRC via IPC: {exc}"
        return results


def _severity_to_string(severity: int) -> str:
    """Convert pcbnew SEVERITY enum value to a human-readable string."""
    severity_map = {
        # KiCad SEVERITY enum values (from drc_rule.h / pcbnew)
        0: "error",
        1: "warning",
        2: "exclusion",
        3: "ignore",
        4: "info",
    }
    return severity_map.get(severity, f"unknown({severity})")
