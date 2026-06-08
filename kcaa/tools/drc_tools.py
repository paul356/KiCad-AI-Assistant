"""
Design Rule Check (DRC) tools for KiCad PCB files.
"""

import os

# import logging # <-- Remove if no other logging exists
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.drc_history import compare_with_previous, save_drc_result
from kcaa.utils.file_utils import get_project_files

# Import implementations
from kcaa.utils.ipc_drc import run_drc_via_ipc
from kcaa.utils.net_settings import set_net_class_in_pro
from kcaa.utils.pcb_design_rules import (
    add_custom_rule_to_file,
    get_effective_design_rules_from_file,
    remove_custom_rule_from_file,
    update_design_rules_in_file,
)


def register_drc_tools(mcp: FastMCP) -> None:
    """Register DRC tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

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
    def get_effective_design_rules(project_path: str) -> dict[str, Any]:
        """Get all design constraints for a KiCad project.

        Returns a unified view with three sections:

        * ``design_rules`` — global minimums (clearance, track width,
          via sizes, etc.) from the PCB file's design rules. These are
          checked against **all** objects during DRC.
        * ``net_classes`` — per-netclass working values (clearance, track
          width, via sizes, diff-pair dimensions) from the project file.
        * ``custom_rules`` — additional conditional DRC rules.

        **All layers are checked independently during DRC** — violating
        any one triggers an error.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Dictionary with ``design_rules``, ``net_classes``,
            and ``custom_rules`` keys.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return get_effective_design_rules_from_file(files["pcb"])

    @mcp.tool()
    def set_design_rules(project_path: str, rules: dict[str, float]) -> dict[str, Any]:
        """Update board-level design rule minimums (global hard floor).

        Only the fields provided in *rules* are modified.  A ``.bak``
        backup is created automatically.

        These are **global minimums** checked against all objects during
        DRC.  To change per-netclass working values, use ``set_net_class``.

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
    def set_net_class(
        project_path: str,
        class_name: str,
        updates: dict[str, float],
    ) -> dict[str, Any]:
        """Update a net class's design parameters in the project file.

        Net classes define working values (clearance, track width, via sizes,
        diff-pair dimensions) for nets in that class.  These are checked
        **in addition to** the board-level minimums — violating either
        triggers a DRC error.

        Use ``get_design_rules`` to see current net class values before
        modifying.

        Valid fields: ``clearance``, ``track_width``, ``via_diameter``,
        ``via_drill``, ``microvia_diameter``, ``microvia_drill``,
        ``diff_pair_width``, ``diff_pair_gap``, ``diff_pair_via_gap``.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            class_name: Net class name (e.g. ``"Default"``)
            updates: Dict mapping field names to new values in millimeters.

        Returns:
            Dictionary with ``updated`` list of changes and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        return set_net_class_in_pro(project_path, class_name, updates)

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
    def del_custom_rule(project_path: str, rule_name: str) -> dict[str, Any]:
        """Remove a custom design rule by name from the PCB file.

        A ``.bak`` backup is created automatically.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            rule_name: Name of the custom rule to remove (matches the name
                       argument from ``add_custom_rule``).

        Returns:
            Dictionary with ``removed`` and ``backup_path`` keys, or error.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return remove_custom_rule_from_file(files["pcb"], rule_name)
