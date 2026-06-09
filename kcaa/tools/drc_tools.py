"""
Design Rule Check (DRC) tools for KiCad PCB files.
"""

import os

# import logging # <-- Remove if no other logging exists
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.file_utils import get_project_files

# Import implementations
from kcaa.utils.ipc_drc import run_drc_via_ipc
from kcaa.utils.net_settings import (
    assign_nets_to_class_in_pro,
    remove_nets_from_class_in_pro,
    set_net_class_in_pro,
)
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
        """Open the Design Rules Checker dialog in KiCad.

        Opens the DRC dialog via kipy IPC.  Click **Run DRC** in the
        dialog to check for violations.  Results are displayed in the
        dialog's list view.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            ``{"success": True}`` when the dialog was opened.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            return {"success": False, "error": "PCB file not found in project"}

        return await run_drc_via_ipc(files["pcb"], ctx)

    @mcp.tool()
    def get_effective_design_rules(project_path: str) -> dict[str, Any]:
        """Get all design constraints for a KiCad project.

        Returns a unified view with three sections:

        * ``design_rules`` — global minimums (clearance, track width,
          via sizes, etc.) from the PCB file's design rules. These are
          checked against **all** objects during DRC.
        * ``net_classes`` — per-netclass working values (clearance, track
          width, via sizes, diff-pair dimensions). Each entry includes a
          ``nets`` list of assigned net names.
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
        DRC.  To change per-netclass working values, use ``set_net_class_rules``.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            rules: Dict mapping field names to new values in millimeters.

        Returns:
            Dictionary with ``updated`` list of changes and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        return update_design_rules_in_file(project_path, rules)

    @mcp.tool()
    def set_net_class_rules(
        project_path: str,
        class_name: str,
        updates: dict[str, float],
    ) -> dict[str, Any]:
        """Update a net class's design parameters in the project file.

        Net classes define working values (clearance, track width, via sizes,
        diff-pair dimensions) for nets in that class.  These are checked
        **in addition to** the board-level minimums — violating either
        triggers a DRC error.

        If the net class does not exist, it is **automatically created**
        using the Default net class values as a baseline, then the provided
        *updates* are applied on top.  Specify only the fields you want to
        override — all others inherit from Default.

        Use ``get_design_rules`` to see current net class values before
        modifying.

        Valid fields: ``clearance``, ``track_width``, ``via_diameter``,
        ``via_drill``, ``microvia_diameter``, ``microvia_drill``,
        ``diff_pair_width``, ``diff_pair_gap``, ``diff_pair_via_gap``.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            class_name: Net class name (e.g. ``"Default"``, ``"HV"``)
            updates: Dict mapping field names to new values in millimeters.

        Returns:
            Dictionary with ``updated`` list of changes and ``backup_path``.
            Includes ``"created": true`` when the net class was auto-created.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        return set_net_class_in_pro(project_path, class_name, updates)

    @mcp.tool()
    def assign_nets_to_class(
        project_path: str,
        class_name: str,
        nets: list[str],
    ) -> dict[str, Any]:
        """Assign nets to a net class in the project file.

        Adds exact-match entries to ``net_settings.netclass_patterns`` so each
        listed net appears in the **Members** tab of Board Setup → Net Classes
        and receives the class's design constraints (clearance, track width,
        via size, etc.).

        If a net was previously assigned to a different class, the old pattern
        is **removed** (net is moved to the new class).

        Nets already in the target class are silently skipped (returned in
        ``existing``).  Use this after ``set_net_class_rules`` to make the
        class constraints apply to specific nets.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            class_name: Net class name to assign nets to (e.g. ``"VBUS"``)
            nets: List of net names (e.g. ``["/tp4056/VBUS", "VCC_SYS"]``)

        Returns:
            Dictionary with ``assigned`` (newly assigned nets),
            ``existing`` (already assigned), and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        return assign_nets_to_class_in_pro(project_path, class_name, nets)

    @mcp.tool()
    def remove_nets_from_class(
        project_path: str,
        class_name: str,
        nets: list[str],
    ) -> dict[str, Any]:
        """Remove nets from a net class, reverting them to Default.

        Deletes exact-match entries from ``net_settings.netclass_patterns``
        so the listed nets no longer receive the class's design constraints.
        After removal, nets fall back to the Default net class.

        Nets not currently in the specified class are silently skipped
        (returned in ``not_found``).

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            class_name: Net class name to remove nets from (e.g. ``"VBUS"``)
            nets: List of net names (e.g. ``["/tp4056/VBUS"]``)

        Returns:
            Dictionary with ``removed`` (nets taken out of the class),
            ``not_found`` (nets not in the class), and ``backup_path``.
        """
        if not os.path.exists(project_path):
            return {"success": False, "error": f"Project not found: {project_path}"}

        return remove_nets_from_class_in_pro(project_path, class_name, nets)

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
