"""Explicit tool policy registry for plugin-side MCP orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ToolKind = Literal["query", "file_mutation", "versioning", "ui_refresh", "ipc_action", "indexing"]

# Signature for post-process hooks: (result_dict) -> result_dict
PostProcessHook = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolPolicy:
    """Framework policy for one plugin-exposed MCP tool."""

    kind: ToolKind
    path_arg: str | None = None
    auto_snapshot: bool = False
    track_snapshot: bool = False
    mark_dirty: bool = False
    clear_dirty_paths_arg: str | None = None
    # Optional hook run *after* the MCP tool returns, on the plugin side.
    # Receives the MCP result dict; returns the (possibly modified) result.
    post_process: PostProcessHook | None = None


TOOL_POLICIES: dict[str, ToolPolicy] = {
    # Netlist tools
    "extract_project_netlist": ToolPolicy(kind="query"),
    "extract_schematic_netlist": ToolPolicy(kind="query"),
    "find_component_connections": ToolPolicy(kind="query"),
    # Project tools
    "get_project_structure": ToolPolicy(kind="query", path_arg="project_path"),
    # Symbol tools
    "sync_symbol_index": ToolPolicy(kind="indexing"),
    "get_symbol_sync_status": ToolPolicy(kind="query"),
    "search_symbols": ToolPolicy(kind="query"),
    "get_symbol": ToolPolicy(kind="query"),
    "list_symbol_libraries": ToolPolicy(kind="query"),
    "get_library_symbols": ToolPolicy(kind="query"),
    "get_symbol_index_stats": ToolPolicy(kind="query"),
    "get_symbol_pins": ToolPolicy(kind="query"),
    # Schematic edit tools
    "add_symbol_to_schematic": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "place_symbol_relative": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "remove_symbol_from_schematic": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "set_symbol_property": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "rename_symbol": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_symbol_properties": ToolPolicy(kind="query"),
    "delete_symbol_property": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "move_component": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "add_label_to_schematic": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_labels_in_schematic": ToolPolicy(kind="query"),
    "delete_label_from_schematic": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "check_reference_conflicts": ToolPolicy(kind="query"),
    # Sheet tools
    "list_sheet_symbols": ToolPolicy(kind="query"),
    "get_sheet_hierarchy": ToolPolicy(kind="query"),
    "add_sheet_symbol": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "remove_sheet_symbol": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "update_sheet_symbol": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "add_sheet_pin": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "remove_sheet_pin": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # Wire edit tools
    "connect_points_with_wire": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "connect_pins_with_wire": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "delete_wire_from_schematic": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # PCB library/index tools
    "sync_footprint_index": ToolPolicy(kind="indexing"),
    "get_footprint_sync_status": ToolPolicy(kind="query"),
    "list_footprint_libraries": ToolPolicy(kind="query"),
    "search_footprints": ToolPolicy(kind="query"),
    "get_footprint_details": ToolPolicy(kind="query"),
    # PCB → library export tools
    "find_footprints_not_in_libraries": ToolPolicy(kind="query"),
    "create_footprint_library": ToolPolicy(kind="file_mutation"),
    # Writes .kicad_mod files into the target library; the PCB itself is
    # never modified, so no PCB snapshot/dirty tracking is attached.
    "add_footprints_to_library": ToolPolicy(kind="file_mutation"),
    # PCB query tools
    "get_board_info": ToolPolicy(kind="query"),
    "list_footprints": ToolPolicy(kind="query"),
    "get_footprint": ToolPolicy(kind="query"),
    "list_nets": ToolPolicy(kind="query"),
    "get_ratsnest": ToolPolicy(kind="query"),
    "score_placement": ToolPolicy(kind="query"),
    "suggest_placement_order": ToolPolicy(kind="query"),
    # PCB placement tools
    "set_footprint_position": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "flip_footprint": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "set_footprint_property": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # PCB edit tools
    "get_board_outline": ToolPolicy(kind="query"),
    "clear_board_outline": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "add_board_outline_segment": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "add_board_outline_arc": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "set_board_outline_rect": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "get_footprint_bbox": ToolPolicy(kind="query"),
    "get_board_bounding_box": ToolPolicy(kind="query"),
    "align_footprints": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "distribute_footprints": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "move_footprints_by_delta": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # Placement helpers
    "find_free_pcb_area": ToolPolicy(kind="query"),
    "get_schematic_sheet_info": ToolPolicy(kind="query"),
    "find_free_area": ToolPolicy(kind="query"),
    # PCB group tools
    "assign_footprints_to_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_footprint_groups": ToolPolicy(kind="query"),
    "get_footprint_group": ToolPolicy(kind="query"),
    "score_footprint_group": ToolPolicy(kind="query"),
    "place_footprint_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "move_footprint_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "rotate_footprint_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # Schematic group tools
    "assign_symbols_to_group": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_symbol_groups": ToolPolicy(kind="query"),
    "get_symbol_group": ToolPolicy(kind="query"),
    "score_symbol_group": ToolPolicy(kind="query"),
    "place_symbol_group": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "move_symbol_group": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "rotate_symbol_group": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # PCB zone tools
    "list_zones": ToolPolicy(kind="query"),
    "add_zone": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "delete_zone": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # PCB routing tools
    "pcb_route_pad_to_pad": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "pcb_add_vias": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "pcb_delete_tracks": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "pcb_delete_vias": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_tracks": ToolPolicy(kind="query"),
    "list_vias": ToolPolicy(kind="query"),
    # DRC tools
    "run_drc_check": ToolPolicy(
        kind="ipc_action",
        path_arg="project_path",
    ),
    "get_effective_design_rules": ToolPolicy(
        kind="query",
        path_arg="project_path",
    ),
    "set_design_rules": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "set_net_class_rules": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "assign_nets_to_class": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "remove_nets_from_class": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "delete_net_class": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "add_custom_rule": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "del_custom_rule": ToolPolicy(
        kind="file_mutation",
        path_arg="project_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    # Skill tools
    "list_skills": ToolPolicy(kind="query"),
    "get_skill": ToolPolicy(kind="query"),
    "add_skill": ToolPolicy(kind="query"),
    "append_to_skill": ToolPolicy(kind="query"),
    "delete_skill": ToolPolicy(kind="query"),
    # KiCad IPC / UI tools
    "check_kicad_ipc_connection": ToolPolicy(kind="query"),
    "save_document": ToolPolicy(kind="ipc_action", path_arg="file_path"),
    "refill_zones": ToolPolicy(kind="ipc_action"),
    "update_pcb_from_schematic": ToolPolicy(kind="ipc_action"),
    "reload_kicad": ToolPolicy(kind="ui_refresh", clear_dirty_paths_arg="paths"),
    # Version tools
    "save_file_version": ToolPolicy(
        kind="versioning",
        path_arg="file_path",
        track_snapshot=True,
    ),
    "list_file_versions": ToolPolicy(kind="versioning"),
    "restore_file_version": ToolPolicy(
        kind="versioning",
        path_arg="file_path",
        track_snapshot=True,
        mark_dirty=True,
    ),
}


def get_tool_policy(tool_name: str) -> ToolPolicy:
    """Return the explicit policy for *tool_name*."""

    return TOOL_POLICIES[tool_name]


def get_missing_tool_policies(tool_names: list[str]) -> list[str]:
    """Return sorted tool names that are not covered by the registry."""

    return sorted(name for name in set(tool_names) if name not in TOOL_POLICIES)
