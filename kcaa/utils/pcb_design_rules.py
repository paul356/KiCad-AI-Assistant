"""
Design rules parsing from KiCad project files (.kicad_pro).

KiCad 10.0 moved board-level design rules from ``.kicad_pcb`` s-expressions
into ``.kicad_pro`` JSON under ``board.design_settings.rules``.
This module reads and writes that section directly — no KiCad process
or kicad-cli is required.
"""

import json as _json
import logging
import os
import shutil
from typing import Any

import sexpdata

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# User-facing field names (MCP API) — these are the names the LLM sees.
# They map to the KiCad 10.0 ``board.design_settings.rules`` keys
# in the ``.kicad_pro`` JSON file.
_USER_FACING_DESIGN_RULE_FIELDS = frozenset(
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

# Map user-facing name → KiCad 10.0 ``board.design_settings.rules`` JSON key.
_DESIGN_RULE_FIELD_MAP: dict[str, str] = {
    "min_clearance": "min_clearance",
    "min_groove_width": "min_groove_width",
    "min_connection_width": "min_connection",
    "min_track_width": "min_track_width",
    "min_via_annular_width": "min_via_annular_width",
    "min_via_size": "min_via_diameter",
    "min_through_drill": "min_through_hole_diameter",
    "min_microvia_size": "min_microvia_diameter",
    "min_microvia_drill": "min_microvia_drill",
    "copper_edge_clearance": "min_copper_edge_clearance",
    "hole_clearance": "min_hole_clearance",
    "hole_to_hole_min": "min_hole_to_hole",
    "silk_clearance": "min_silk_clearance",
    "min_resolved_spokes": "min_resolved_spokes",
    "min_silk_text_height": "min_text_height",
    "min_silk_text_thickness": "min_text_thickness",
}

# Reverse map: KiCad 10.0 key → user-facing name (for read-back).
_PRO_KEY_TO_USER: dict[str, str] = {v: k for k, v in _DESIGN_RULE_FIELD_MAP.items()}


def _find_section(tree: list[Any], tag: str) -> list[Any] | None:
    """Return the first child list whose first element is *tag* (a Symbol).

    Returns None if no matching section is found.
    """
    tag_sym = sexpdata.Symbol(tag)
    for item in tree:
        if isinstance(item, list) and len(item) > 0 and item[0] == tag_sym:
            return item
    return None


def _dru_path(pcb_file: str) -> str:
    """Derive the .kicad_dru path from a .kicad_pcb path."""
    return pcb_file.replace(".kicad_pcb", ".kicad_dru")


def _load_dru(dru_file: str) -> list[Any]:
    """Parse a .kicad_dru file and return its S-expression tree.

    Uses ``sexpdata.parse`` because dru files contain multiple top-level
    forms (``(version 1)`` + ``(rule ...)`` etc.), unlike .kicad_pcb
    which has a single ``(kicad_pcb ...)`` wrapper.
    """
    with open(dru_file, encoding="utf-8") as f:
        return sexpdata.parse(f.read())


def _save_dru(dru_file: str, data: list[Any]) -> str | None:
    """Write *data* to *dru_file*, creating a .bak backup first if file exists.

    Each top-level form in *data* is serialised on its own line so that
    the file can be round-tripped with ``sexpdata.parse``.

    Returns the backup path or None if the file didn't exist yet.
    """
    bak_path = None
    if os.path.exists(dru_file):
        bak_path = dru_file + ".bak"
        shutil.copy2(dru_file, bak_path)

    lines = [sexpdata.dumps(item) for item in data]
    text = "\n".join(lines) + "\n"
    tmp_path = dru_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, dru_file)
    return bak_path


def _parse_dru_value(val: Any) -> float:
    """Parse a dru constraint value, stripping unit suffix (e.g. '0.4mm' → 0.4)."""
    s = _str_symbol(val)
    for unit in ("mm", "mil", "um", "nm", "inch", "in"):
        if s.endswith(unit):
            return float(s[: -len(unit)])
    return float(s)


def _format_dru_value(value: float) -> sexpdata.Symbol:
    """Format a constraint value for dru output, adding mm suffix."""
    return sexpdata.Symbol(f"{value}mm")


def _str_symbol(val: Any) -> str:
    """Convert a sexpdata Symbol to a plain string."""
    if isinstance(val, sexpdata.Symbol):
        return val.value()
    return str(val)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_effective_design_rules_from_file(pcb_file: str) -> dict[str, Any]:
    """Read all design rules and net classes for a PCB from its project file.

    Returns a unified view with three sections:

    * ``design_rules`` — global minimums from the project's
      ``board.design_settings.rules`` in ``.kicad_pro`` (KiCad 10.0+).
    * ``net_classes`` — per-netclass working values from the project's
      ``net_settings.classes``. Each entry has a ``nets`` list of
      assigned net names.
    * ``custom_rules`` — additional conditional constraints from the
      PCB file's ``(custom_rules ...)`` sexp section.

    All three layers are checked independently during DRC — violating
    any one of them triggers an error.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.

    Returns:
        ``{"success": True, "design_rules": {...},
          "net_classes": [...], "custom_rules": [...]}`` on success.
    """
    result: dict[str, Any] = {"success": True}
    notes: list[str] = []

    pro_file = pcb_file.replace(".kicad_pcb", ".kicad_pro")

    # 1. Board constraints from .kicad_pro JSON
    try:
        with open(pro_file, encoding="utf-8") as f:
            pro_data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError) as exc:
        return {"success": False, "error": f"Cannot read project file: {exc}"}

    rules_raw = pro_data.get("board", {}).get("design_settings", {}).get("rules")
    if isinstance(rules_raw, dict):
        bc: dict[str, float] = {}
        for pro_key, value in rules_raw.items():
            if isinstance(value, int | float | bool):
                user_key = _PRO_KEY_TO_USER.get(pro_key)
                if user_key is not None:
                    bc[user_key] = float(value)
        if bc:
            result["design_rules"] = bc
        else:
            result["design_rules"] = {}
    else:
        result["design_rules"] = {}
        notes.append("No board.design_settings.rules found in project file.")

    # 2. Net classes from .kicad_pro
    try:
        from kcaa.utils.net_settings import get_net_classes_from_pro

        nc_result = get_net_classes_from_pro(pro_file)
        if nc_result.get("success"):
            result["net_classes"] = nc_result.get("classes", [])
        else:
            result["net_classes"] = []
            notes.append(nc_result.get("error", "Cannot read net classes"))
    except Exception as exc:
        result["net_classes"] = []
        notes.append(f"Cannot read net classes: {exc}")

    # 3. Custom rules from .kicad_pcb sexp
    cr_result = get_custom_rules_from_file(pcb_file)
    result["custom_rules"] = cr_result.get("rules", [])

    # Add rule-layer semantics note (static, not per-file)
    notes.insert(
        0,
        (
            "Three layers checked independently: "
            "(1) design_rules — global hard minimums, apply to all objects; "
            "(2) net_classes — per-net working values, checked on top of board minimums "
            "(net class values can be stricter but not looser than board constraints); "
            "(3) custom_rules — conditional DRC rules that can override or augment "
            "the above. Violating any layer triggers a DRC error."
        ),
    )

    if notes:
        result["note"] = "; ".join(notes)

    return result


def update_design_rules_in_file(pro_file: str, updates: dict[str, float]) -> dict[str, Any]:
    """Update board-level design rule values in the ``.kicad_pro`` project file.

    Only fields present in *updates* are modified; all other fields
    and the rest of the file are left untouched.  A ``.bak`` backup
    is created automatically.

    Design rules are stored in ``board.design_settings.rules`` in the
    project file (KiCad 10.0+ format).

    Args:
        pro_file: Absolute path to the ``.kicad_pro`` file.
        updates: Mapping of user-facing field names (e.g.
                 ``"min_through_drill"``, ``"min_track_width"``)
                 to new values in millimeters.

    Returns:
        ``{"success": True, "updated": [...], ...}`` or an error dict.
    """
    # Validate field names
    invalid = [k for k in updates if k not in _USER_FACING_DESIGN_RULE_FIELDS]
    if invalid:
        return {"success": False, "error": f"Unknown design rule fields: {', '.join(invalid)}"}

    try:
        with open(pro_file, encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError) as exc:
        return {"success": False, "error": f"Cannot read project file: {exc}"}

    board = data.get("board")
    if not isinstance(board, dict):
        return {"success": False, "error": "No 'board' section in project file."}

    ds = board.get("design_settings")
    if not isinstance(ds, dict):
        return {"success": False, "error": "No 'board.design_settings' section in project file."}

    rules = ds.get("rules")
    if not isinstance(rules, dict):
        return {
            "success": False,
            "error": "No 'board.design_settings.rules' section in project file. "
            "Open Board Setup in KiCad, adjust any value, and click OK to generate this section.",
        }

    changed: list[str] = []
    for user_key, value in updates.items():
        pro_key = _DESIGN_RULE_FIELD_MAP[user_key]
        old_val = rules.get(pro_key)
        rules[pro_key] = float(value)
        changed.append(f"{user_key}: {old_val}mm → {value}mm")

    if not changed:
        return {"success": True, "updated": [], "message": "No matching fields found to update."}

    import shutil

    bak_path = pro_file + ".bak"
    try:
        shutil.copy2(pro_file, bak_path)
    except OSError as exc:
        return {"success": False, "error": f"Failed to create backup: {exc}"}

    try:
        with open(pro_file, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
    except OSError as exc:
        return {"success": False, "error": f"Failed to write project file: {exc}"}

    log.info("Updated design rules in %s: %s", pro_file, changed)
    return {
        "success": True,
        "updated": changed,
        "backup_path": bak_path,
        "warning": f"Changes saved to {pro_file}. Reopen the project in KiCad for design rules to take effect.",
    }


def get_custom_rules_from_file(pcb_file: str) -> dict[str, Any]:
    """Read custom design rules from the ``.kicad_dru`` file.

    KiCad 10.0 stores custom DRC rules in the .kicad_dru file, not in
    .kicad_pcb.  This function derives the dru path from the pcb path.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file (used to
                  derive the ``.kicad_dru`` path).

    Returns:
        ``{"success": True, "rules": [...]}`` with a list of custom
        rule dicts, or an error dict.
    """
    dru_file = _dru_path(pcb_file)

    if not os.path.exists(dru_file):
        return {"success": True, "rules": [], "message": "No .kicad_dru file found."}

    try:
        tree = _load_dru(dru_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    rules: list[dict[str, Any]] = []
    for item in tree:
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
    """Append a custom design rule to the ``.kicad_dru`` file.

    KiCad 10.0 stores custom DRC rules in .kicad_dru.  If the file does
    not exist yet, it is created with a ``(version 1)`` header.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file (used to
                  derive the ``.kicad_dru`` path).
        name: Human-readable rule name (a Symbol, no quotes).
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

    dru_file = _dru_path(pcb_file)

    # Load existing dru or create new tree
    if os.path.exists(dru_file):
        try:
            tree = _load_dru(dru_file)
        except (FileNotFoundError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
    else:
        tree = [sexpdata.Symbol("version"), 1]

    # Build the new rule sexp in dru format:
    # (rule Name (condition "...") (constraint type (min 0.4mm)) (severity error))
    rule_sexp: list[Any] = [
        sexpdata.Symbol("rule"),
        sexpdata.Symbol(name),
        [sexpdata.Symbol("condition"), condition],
        [
            sexpdata.Symbol("constraint"),
            sexpdata.Symbol(constraint_type),
            [sexpdata.Symbol("min"), _format_dru_value(value)],
        ],
        [sexpdata.Symbol("severity"), sexpdata.Symbol(severity)],
    ]
    tree.append(rule_sexp)

    try:
        bak_path = _save_dru(dru_file, tree)
    except OSError as exc:
        return {"success": False, "error": f"Failed to save dru file: {exc}"}

    rule_dict = {
        "name": name,
        "condition": condition,
        "constraint": {"type": constraint_type, "min": value},
        "severity": severity,
    }
    return {"success": True, "rule": rule_dict, "backup_path": bak_path}


def remove_custom_rule_from_file(pcb_file: str, rule_name: str) -> dict[str, Any]:
    """Remove a custom design rule by name from the ``.kicad_dru`` file.

    A ``.bak`` backup is created automatically.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file (used to
                  derive the ``.kicad_dru`` path).
        rule_name: Name of the custom rule to remove (matches the ``name`` argument
                   from ``add_custom_rule``).

    Returns:
        ``{"success": True, "removed": rule_name, "backup_path": ...}`` or error.
    """
    dru_file = _dru_path(pcb_file)

    if not os.path.exists(dru_file):
        return {"success": False, "error": "No .kicad_dru file found."}

    try:
        tree = _load_dru(dru_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    removed = None
    new_tree: list[Any] = []
    for item in tree:
        if isinstance(item, list) and len(item) >= 2 and _str_symbol(item[0]) == "rule":
            if _str_symbol(item[1]) == rule_name:
                removed = item
                continue
        new_tree.append(item)

    if removed is None:
        return {"success": False, "error": f"Custom rule '{rule_name}' not found."}

    # Replace tree in-place
    tree.clear()
    tree.extend(new_tree)

    try:
        bak_path = _save_dru(dru_file, tree)
    except OSError as exc:
        return {"success": False, "error": f"Failed to save dru file: {exc}"}

    log.info("Removed custom rule '%s' from %s", rule_name, dru_file)
    return {"success": True, "removed": rule_name, "backup_path": bak_path}


def _parse_custom_rule(rule_sexp: list[Any]) -> dict[str, Any] | None:
    """Parse a single ``(rule ...)`` sexp into a dict.

    Handles both the dru format ``(constraint type (min 0.4mm))`` and
    the legacy PCB format ``(constraint type min 0.4)``.
    """
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
            # Dru format:   (constraint <type> (min 0.4mm))  → 3 top-level items
            # Legacy format: (constraint <type> min <value>)  → 4 top-level items
            if len(item) >= 4:
                rule["constraint"] = {
                    "type": _str_symbol(item[1]),
                    _str_symbol(item[2]): _parse_dru_value(item[3]),
                }
            elif len(item) >= 3 and isinstance(item[2], list) and len(item[2]) >= 2:
                # Dru format: third element is (min|max|opt <val_mm>)
                sub = item[2]
                rule["constraint"] = {
                    "type": _str_symbol(item[1]),
                    _str_symbol(sub[0]): _parse_dru_value(sub[1]),
                }
            elif len(item) >= 3:
                rule["constraint"] = {
                    "type": _str_symbol(item[1]),
                    "value": item[2],
                }
        elif tag == "severity" and len(item) >= 2:
            rule["severity"] = _str_symbol(item[1])

    return rule
