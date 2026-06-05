"""
DRC implementations for different KiCad API approaches.
"""

from kcaa.tools.drc_impl.ipc_drc import run_drc_via_ipc
from kcaa.tools.drc_impl.pcb_design_rules import (
    add_custom_rule_to_file,
    get_custom_rules_from_file,
    get_design_rules_from_file,
    restore_design_rules_from_backup,
    update_design_rules_in_file,
)

__all__ = [
    "run_drc_via_ipc",
    "get_design_rules_from_file",
    "update_design_rules_in_file",
    "get_custom_rules_from_file",
    "add_custom_rule_to_file",
    "restore_design_rules_from_backup",
]
