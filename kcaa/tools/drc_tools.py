"""
Design Rule Check (DRC) tools for KiCad PCB files.
"""

import os

# import logging # <-- Remove if no other logging exists
from typing import Any

from fastmcp import Context, FastMCP

# Import implementations
from kcaa.tools.drc_impl.ipc_drc import run_drc_via_ipc
from kcaa.tools.drc_impl.pcb_design_rules import (
    add_custom_rule_to_file,
    get_custom_rules_from_file,
    get_design_rules_from_file,
    restore_design_rules_from_backup,
    update_design_rules_in_file,
)
from kcaa.utils.drc_history import compare_with_previous, get_drc_history, save_drc_result
from kcaa.utils.file_utils import get_project_files


def register_drc_tools(mcp: FastMCP) -> None:
    """Register DRC tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def get_drc_history_tool(project_path: str) -> dict[str, Any]:
        """Get the DRC check history for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Dictionary with DRC history entries
        """
        print(f"Getting DRC history for project: {project_path}")

        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}

        # Get history entries
        history_entries = get_drc_history(project_path)

        # Calculate trend information
        trend = None
        if len(history_entries) >= 2:
            first = history_entries[-1]  # Oldest entry
            last = history_entries[0]  # Newest entry

            first_violations = first.get("total_violations", 0)
            last_violations = last.get("total_violations", 0)

            if first_violations > last_violations:
                trend = "improving"
            elif first_violations < last_violations:
                trend = "degrading"
            else:
                trend = "stable"

        return {
            "success": True,
            "project_path": project_path,
            "history_entries": history_entries,
            "entry_count": len(history_entries),
            "trend": trend,
        }

    @mcp.tool()
    async def run_drc_check(project_path: str, ctx: Context | None) -> dict[str, Any]:
        """Run a Design Rule Check on a KiCad PCB file.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with DRC results and statistics
        """
        print(f"Running DRC check for project: {project_path}")

        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}

        # Get PCB file from project
        files = get_project_files(project_path)
        if "pcb" not in files:
            print("PCB file not found in project")
            return {"success": False, "error": "PCB file not found in project"}

        pcb_file = files["pcb"]
        print(f"Found PCB file: {pcb_file}")

        # Report progress to user
        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(f"Starting DRC check on {os.path.basename(pcb_file)}")

        # Run DRC using IPC (kipy + pcbnew)
        print("Using KiCad IPC for DRC")
        if ctx:
            ctx.info("Using KiCad IPC for DRC check...")
        drc_results = await run_drc_via_ipc(pcb_file, ctx)

        # Process and save results if successful
        if drc_results and drc_results.get("success", False):
            # logging.info(f"[DRC] DRC check successful for {pcb_file}. Saving results.") # <-- Remove log
            # Save results to history
            save_drc_result(project_path, drc_results)

            # Add comparison with previous run
            comparison = compare_with_previous(project_path, drc_results)
            if comparison:
                drc_results["comparison"] = comparison

                if ctx:
                    if comparison["change"] < 0:
                        ctx.info(
                            f"Great progress! You've fixed {abs(comparison['change'])} DRC violations since the last check."
                        )
                    elif comparison["change"] > 0:
                        ctx.info(
                            f"Found {comparison['change']} new DRC violations since the last check."
                        )
                    else:
                        ctx.info("No change in the number of DRC violations since the last check.")
        elif drc_results:
            # logging.warning(f"[DRC] DRC check reported failure for {pcb_file}: {drc_results.get('error')}") # <-- Remove log
            # Pass or print a warning if needed
            pass
        else:
            # logging.error(f"[DRC] DRC check returned None for {pcb_file}") # <-- Remove log
            # Pass or print an error if needed
            pass

        # Complete progress
        if ctx:
            await ctx.report_progress(100, 100)

        return drc_results or {"success": False, "error": "DRC check failed with an unknown error"}

    @mcp.tool()
    def get_design_rules(project_path: str) -> dict[str, Any]:
        """Get the board-level design rules for a KiCad project.

        Reads constraints such as minimum clearance, track width, via size,
        etc. from the PCB file's ``(setup (design_rules ...))`` section.
        No KiCad process is required.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Dictionary with ``rules`` key containing constraint name→value
            pairs in millimeters.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return get_design_rules_from_file(files["pcb"])

    @mcp.tool()
    def set_design_rules(project_path: str, rules: dict[str, float]) -> dict[str, Any]:
        """Update board-level design rule values in the PCB file.

        Only the fields provided in *rules* are modified.  A ``.bak``
        backup is created automatically.  After updating, reload the board
        in KiCad to see the changes take effect.

        Example fields: ``min_clearance``, ``min_track_width``,
        ``min_via_size``, ``min_through_drill``, ``copper_edge_clearance``,
        ``hole_clearance``, ``silk_clearance``.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            rules: Dict mapping field names to new values in millimeters.

        Returns:
            Dictionary with ``updated`` list of changes and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return update_design_rules_in_file(files["pcb"], rules)

    @mcp.tool()
    def list_custom_rules(project_path: str) -> dict[str, Any]:
        """List custom design rules defined in the PCB file.

        Custom rules are additional constraints written in KiCad's
        Lisp-like DSL that apply to specific net classes, layers, or
        object types.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Dictionary with ``rules`` list of custom rule objects.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return get_custom_rules_from_file(files["pcb"])

    @mcp.tool()
    def add_custom_rule(
        project_path: str,
        name: str,
        condition: str,
        constraint_type: str,
        value: float,
        severity: str = "error",
    ) -> dict[str, Any]:
        """Add a custom design rule to the PCB file.

        Custom rules use KiCad's constraint DSL to target specific objects
        (nets, layers, etc.).  Common constraint types include
        ``clearance``, ``track_width``, ``hole_size``, ``annular_width``,
        and ``courtyard_clearance``.

        Example condition: ``"A.NetClass == 'HV'"`` (apply to nets in
        the HV net class).

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            name: Human-readable name for the rule
            condition: Lisp-style condition expression
            constraint_type: Type of constraint to enforce
            value: Constraint value in millimeters
            severity: ``"error"``, ``"warning"``, ``"ignore"``, or
                      ``"exclusion"`` (default: ``"error"``)

        Returns:
            Dictionary with the created rule and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return add_custom_rule_to_file(
            files["pcb"], name, condition, constraint_type, value, severity
        )

    @mcp.tool()
    def restore_design_rules(backup_path: str) -> dict[str, Any]:
        """Restore PCB design rules from a ``.bak`` backup file.

        When design rules are modified via ``set_design_rules`` or
        ``add_custom_rule``, a ``.kicad_pcb.bak`` backup is automatically
        created.  Use this tool to restore the design rules to the state
        captured in that backup.  A safety backup of the current state is
        created before restoring, so the operation can be undone.

        Args:
            backup_path: Absolute path to the ``.kicad_pcb.bak`` file.

        Returns:
            Dictionary with ``restored_to`` and ``safety_backup`` paths
            on success, or an error dict on failure.
        """
        return restore_design_rules_from_backup(backup_path)
