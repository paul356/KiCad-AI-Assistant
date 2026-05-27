"""Explicit tool policy registry for plugin-side MCP orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolKind = Literal["query", "file_mutation", "versioning", "ui_refresh", "ipc_action", "indexing"]


@dataclass(frozen=True)
class ToolPolicy:
    """Framework policy for one plugin-exposed MCP tool."""

    kind: ToolKind
    path_arg: str | None = None
    auto_snapshot: bool = False
    track_snapshot: bool = False
    mark_dirty: bool = False
    clear_dirty_paths_arg: str | None = None


TOOL_POLICIES: dict[str, ToolPolicy] = {
    # Netlist tools
    "extract_project_netlist": ToolPolicy(kind="query"),
    "extract_schematic_netlist": ToolPolicy(kind="query"),
    "find_component_connections": ToolPolicy(kind="query"),
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
    "set_component_property": ToolPolicy(
        kind="file_mutation",
        path_arg="schematic_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_component_properties": ToolPolicy(kind="query"),
    "delete_component_property": ToolPolicy(
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
    "assign_to_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "list_groups": ToolPolicy(kind="query"),
    "get_group": ToolPolicy(kind="query"),
    "score_group": ToolPolicy(kind="query"),
    "place_component_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "move_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
        auto_snapshot=True,
        mark_dirty=True,
    ),
    "rotate_group": ToolPolicy(
        kind="file_mutation",
        path_arg="pcb_path",
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
