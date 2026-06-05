"""
Design rules parsing from KiCad PCB S-expression files (.kicad_pcb).

Reads and writes the ``(setup (design_rules ...))`` and
``(setup (custom_rules ...))`` sections via the sexpdata library.
No KiCad process or kicad-cli is required — all operations are file-based.
"""

import logging
import os
from typing import Any

import sexpdata

from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known design-rule field names (matched against the sexp tag inside
# ``(setup (design_rules ...))``).  Values are millimeters.
# ---------------------------------------------------------------------------
_DESIGN_RULE_FIELDS = frozenset(
    {
        "min_clearance",
        "min_groove_width",
        "min_connection_width",
        "min_track_width",
        "min_via_annular_width",
        "min_via_size",
        "min_through_drill",
        "min_microvia_size",
        "min_microvia_drill",
        "copper_edge_clearance",
        "hole_clearance",
        "hole_to_hole_min",
        "silk_clearance",
        "min_resolved_spokes",
        "min_silk_text_height",
        "min_silk_text_thickness",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_section(tree: list[Any], tag: str) -> list[Any] | None:
    """Return the first child list whose first element is *tag* (a Symbol).

    Returns None if no matching section is found.
    """
    tag_sym = sexpdata.Symbol(tag)
    for item in tree:
        if isinstance(item, list) and len(item) > 0 and item[0] == tag_sym:
            return item
    return None


def _str_symbol(val: Any) -> str:
    """Convert a sexpdata Symbol to a plain string."""
    if isinstance(val, sexpdata.Symbol):
        return val.value()
    return str(val)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_design_rules_from_file(pcb_file: str) -> dict[str, Any]:
    """Read board-level design rules from a ``.kicad_pcb`` file.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.

    Returns:
        ``{"success": True, "rules": {...}, ...}`` on success, or an
        error dict with ``"success": False``.
    """
    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    setup = _find_section(tree, "setup")
    if setup is None:
        return {
            "success": True,
            "rules": {},
            "message": "No (setup ...) section found in PCB file.",
        }

    dr_section = _find_section(setup, "design_rules")
    if dr_section is None:
        return {"success": True, "rules": {}, "message": "No (design_rules ...) subsection found."}

    rules: dict[str, float] = {}
    for item in dr_section[1:]:
        if isinstance(item, list) and len(item) >= 2:
            field_name = _str_symbol(item[0])
            if field_name in _DESIGN_RULE_FIELDS:
                try:
                    rules[field_name] = float(item[1])
                except (ValueError, TypeError):
                    pass

    return {"success": True, "rules": rules}


def update_design_rules_in_file(pcb_file: str, updates: dict[str, float]) -> dict[str, Any]:
    """Update specific design-rule values in a ``.kicad_pcb`` file.

    Only fields present in *updates* are modified; all other fields
    and the rest of the file are left untouched.  A ``.bak`` backup
    is created automatically.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.
        updates: Mapping of field names (e.g. ``"min_clearance"``) to
                 new values in millimeters.

    Returns:
        ``{"success": True, "updated": [...], ...}`` or an error dict.
    """
    # Validate field names
    invalid = [k for k in updates if k not in _DESIGN_RULE_FIELDS]
    if invalid:
        return {"success": False, "error": f"Unknown design rule fields: {', '.join(invalid)}"}

    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    setup = _find_section(tree, "setup")
    if setup is None:
        return {"success": False, "error": "No (setup ...) section found in PCB file."}

    dr_section = _find_section(setup, "design_rules")
    if dr_section is None:
        return {"success": False, "error": "No (design_rules ...) subsection found in PCB file."}

    changed: list[str] = []
    for i, item in enumerate(dr_section):
        if not isinstance(item, list) or len(item) < 2:
            continue
        field_name = _str_symbol(item[0])
        if field_name in updates:
            old_val = item[1]
            new_val = float(updates[field_name])
            dr_section[i] = [sexpdata.Symbol(field_name), new_val]
            changed.append(f"{field_name}: {old_val}mm → {new_val}mm")

    if not changed:
        return {"success": True, "updated": [], "message": "No matching fields found to update."}

    try:
        bak_path = save_pcb(pcb_file, tree)
    except OSError as exc:
        return {"success": False, "error": f"Failed to save PCB file: {exc}"}

    return {"success": True, "updated": changed, "backup_path": bak_path}


def get_custom_rules_from_file(pcb_file: str) -> dict[str, Any]:
    """Read custom design rules from a ``.kicad_pcb`` file.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.

    Returns:
        ``{"success": True, "rules": [...]}`` with a list of custom
        rule dicts, or an error dict.
    """
    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    setup = _find_section(tree, "setup")
    if setup is None:
        return {
            "success": True,
            "rules": [],
            "message": "No (setup ...) section found in PCB file.",
        }

    cr_section = _find_section(setup, "custom_rules")
    if cr_section is None:
        return {"success": True, "rules": [], "message": "No (custom_rules ...) subsection found."}

    rules: list[dict[str, Any]] = []
    for item in cr_section[1:]:
        if isinstance(item, list) and len(item) >= 2 and _str_symbol(item[0]) == "rule":
            rule = _parse_custom_rule(item)
            if rule:
                rules.append(rule)

    return {"success": True, "rules": rules}


def add_custom_rule_to_file(
    pcb_file: str,
    name: str,
    condition: str,
    constraint_type: str,
    value: float,
    severity: str = "error",
) -> dict[str, Any]:
    """Append a custom design rule to the ``(custom_rules ...)`` section.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.
        name: Human-readable rule name.
        condition: Lisp-style condition expression (e.g.
                   ``"A.NetClass == 'HV'"``).
        constraint_type: One of ``clearance``, ``track_width``, ``hole_size``,
                         ``annular_width``, ``courtyard_clearance``, etc.
        value: Constraint value in millimeters.
        severity: ``"error"``, ``"warning"``, or ``"ignore"``.

    Returns:
        ``{"success": True, "rule": {...}, ...}`` or an error dict.
    """
    valid_severities = {"error", "warning", "ignore", "exclusion"}
    if severity not in valid_severities:
        return {
            "success": False,
            "error": f"Invalid severity '{severity}'. Must be one of {sorted(valid_severities)}.",
        }

    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    # Find or create (setup ...)
    setup = _find_section(tree, "setup")
    if setup is None:
        setup = [sexpdata.Symbol("setup")]
        tree.append(setup)

    # Find or create (custom_rules ...)
    cr_section = _find_section(setup, "custom_rules")
    if cr_section is None:
        cr_section = [sexpdata.Symbol("custom_rules")]
        setup.append(cr_section)

    # Build the new rule sexp
    rule_sexp: list[Any] = [
        sexpdata.Symbol("rule"),
        name,
        [sexpdata.Symbol("condition"), condition],
        [
            sexpdata.Symbol("constraint"),
            sexpdata.Symbol(constraint_type),
            sexpdata.Symbol("min"),
            value,
        ],
        [sexpdata.Symbol("severity"), sexpdata.Symbol(severity)],
    ]
    cr_section.append(rule_sexp)

    try:
        bak_path = save_pcb(pcb_file, tree)
    except OSError as exc:
        return {"success": False, "error": f"Failed to save PCB file: {exc}"}

    rule_dict = {
        "name": name,
        "condition": condition,
        "constraint": {"type": constraint_type, "min": value},
        "severity": severity,
    }
    return {"success": True, "rule": rule_dict, "backup_path": bak_path}


def restore_design_rules_from_backup(backup_path: str) -> dict[str, Any]:
    """Restore a ``.kicad_pcb`` file from its ``.bak`` backup.

    The backup is created automatically by ``update_design_rules_in_file``
    and ``add_custom_rule_to_file``.  This tool copies the backup over the
    current PCB file, creating a new safety backup of the current state first
    (so the restoration itself can be undone).

    Args:
        backup_path: Absolute path to the ``.kicad_pcb.bak`` backup file.

    Returns:
        ``{"success": True, "restored_to": ..., "safety_backup": ...}``
        or an error dict.
    """
    import shutil
    import time

    if not os.path.isfile(backup_path):
        return {"success": False, "error": f"Backup file not found: {backup_path}"}

    if not backup_path.endswith(".bak"):
        return {"success": False, "error": "Backup path must end with .bak"}

    original_path = backup_path[:-4]  # strip ".bak"
    if not os.path.isfile(original_path):
        return {"success": False, "error": f"Original file not found: {original_path}"}

    # Verify backup is a valid PCB file
    try:
        load_pcb(backup_path)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": f"Failed to parse backup file: {exc}"}

    # Create safety backup of current state before restoring
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safety_bak = original_path + f".pre-restore-{stamp}.bak"
    try:
        shutil.copy2(original_path, safety_bak)
    except OSError as exc:
        return {"success": False, "error": f"Failed to create safety backup: {exc}"}

    # Restore: copy backup over original
    try:
        shutil.copy2(backup_path, original_path)
    except OSError as exc:
        return {"success": False, "error": f"Failed to restore from backup: {exc}"}

    return {
        "success": True,
        "message": f"Design rules restored from {os.path.basename(backup_path)}",
        "restored_to": original_path,
        "safety_backup": safety_bak,
    }


def _parse_custom_rule(rule_sexp: list[Any]) -> dict[str, Any] | None:
    """Parse a single ``(rule ...)`` sexp into a dict."""
    if len(rule_sexp) < 2:
        return None
    rule: dict[str, Any] = {"name": _str_symbol(rule_sexp[1])}

    for item in rule_sexp[2:]:
        if not isinstance(item, list) or len(item) < 2:
            continue
        tag = _str_symbol(item[0])
        if tag == "condition" and len(item) >= 2:
            rule["condition"] = item[1]
        elif tag == "constraint":
            # (constraint <type> min|max|opt <value>)
            if len(item) >= 4:
                rule["constraint"] = {
                    "type": _str_symbol(item[1]),
                    _str_symbol(item[2]): item[3],
                }
            elif len(item) >= 3:
                rule["constraint"] = {
                    "type": _str_symbol(item[1]),
                    "value": item[2],
                }
        elif tag == "severity" and len(item) >= 2:
            rule["severity"] = _str_symbol(item[1])

    return rule
