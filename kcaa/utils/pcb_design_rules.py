"""
Design rules parsing from KiCad PCB S-expression files (.kicad_pcb).

Reads and writes the ``(setup (design_rules ...))`` and
``(setup (custom_rules ...))`` sections via the sexpdata library.
No KiCad process or kicad-cli is required — all operations are file-based.
"""

import json as _json
import logging
import os
import platform as _platform
from typing import Any

import sexpdata

from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)


def _get_design_defaults_path() -> str:
    """Return the path to the design defaults file in the kcaa data directory.

    Tries ``kcaa.utils.config.config.get_kcaa_data_dir()`` first (available
    when the MCP server is running).  Falls back to detecting the directory
    from the ``KICAD_VERSION`` environment variable and platform.
    """
    try:
        from kcaa.utils.config import config as _config

        return os.path.join(_config.get_kcaa_data_dir(), "design-defaults.json")
    except Exception:
        pass

    version = os.environ.get("KICAD_VERSION")
    if not version:
        # Try KICAD{N}_* variables (set inside KiCad)
        for key in os.environ:
            if key.startswith("KICAD") and "_" in key:
                major = key[5:].split("_")[0]
                if major.isdigit():
                    version = f"{major}.0"
                    break

    if version:
        system = _platform.system()
        if system == "Darwin":
            base = os.path.expanduser(f"~/Library/Preferences/kicad/{version}")
        elif system == "Windows":
            base = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")), "kicad", version
            )
        else:
            base = os.path.expanduser(f"~/.config/kicad/{version}")
        return os.path.join(base, "kcaa", "design-defaults.json")

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Known design-rule field names (sexp tags inside (design_rules ...)).
# Must match the keys exported by the plugin's _export_design_defaults().
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


def get_effective_design_rules_from_file(pcb_file: str) -> dict[str, Any]:
    """Read all design rules and net classes for a PCB from file.

    Returns a unified view with three sections:

    * ``design_rules`` — global minimums from the PCB file's
      ``(design_rules ...)`` section.  These apply to **all** objects.
    * ``net_classes`` — per-netclass working values from the project's
      ``.kicad_pro`` file.  Each net class has its own clearance,
      track width, via sizes, and diff-pair dimensions.
    * ``custom_rules`` — additional conditional constraints from the
      ``(custom_rules ...)`` section.

    All three layers are checked independently during DRC — violating
    any one of them triggers an error.

    When the PCB file has no ``(design_rules ...)`` section, defaults
    exported by the plugin are used for ``design_rules``.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.

    Returns:
        ``{"success": True, "design_rules": {...},
          "net_classes": [...], "custom_rules": [...]}`` on success.
    """
    result: dict[str, Any] = {"success": True}
    notes: list[str] = []

    # 1. Board constraints from .kicad_pcb sexp
    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    setup = _find_section(tree, "setup")
    if setup is not None:
        dr_section = _find_section(setup, "design_rules")
    else:
        dr_section = None

    if dr_section is not None:
        bc: dict[str, float] = {}
        for item in dr_section[1:]:
            if isinstance(item, list) and len(item) >= 2:
                field_name = _str_symbol(item[0])
                if field_name not in _DESIGN_RULE_FIELDS:
                    continue
                try:
                    bc[field_name] = float(item[1])
                except (ValueError, TypeError):
                    pass
        if bc:
            result["design_rules"] = bc
        else:
            result["design_rules"] = {}
            notes.append("No design rules found; using plugin defaults if available.")
    else:
        fallback = _load_exported_defaults("No (design_rules ...) section found in PCB file.")
        if fallback.get("defaults_used"):
            result["design_rules"] = fallback["rules"]
            result["design_rules"]["defaults_used"] = True
            notes.append(fallback["message"])
        else:
            result["design_rules"] = {}
            notes.append(fallback["message"])

    # 2. Net classes from .kicad_pro
    pro_file = pcb_file.replace(".kicad_pcb", ".kicad_pro")
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


def _load_exported_defaults(message: str) -> dict[str, Any]:
    """Try to load KiCad defaults from the file the plugin exports at startup.

    When the file doesn't exist (KiCad not running, or plugin hasn't
    exported yet), return an empty rules dict so the caller knows no
    meaningful defaults are available.
    """
    try:
        with open(_get_design_defaults_path(), encoding="utf-8") as f:
            rules = _json.load(f)
        return {
            "success": True,
            "rules": rules,
            "defaults_used": True,
            "message": f"{message} Loaded KiCad defaults from plugin export.",
        }
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError, TypeError) as exc:
        return {
            "success": True,
            "rules": {},
            "message": f"{message} No KiCad defaults available ({exc}).",
        }


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
    # Validate field names against the known set
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


def remove_custom_rule_from_file(pcb_file: str, rule_name: str) -> dict[str, Any]:
    """Remove a custom design rule by name from the ``(custom_rules ...)`` section.

    A ``.bak`` backup is created automatically.

    Args:
        pcb_file: Absolute path to the ``.kicad_pcb`` file.
        rule_name: Name of the custom rule to remove (matches the ``name`` argument
                   from ``add_custom_rule``).

    Returns:
        ``{"success": True, "removed": rule_name, "backup_path": ...}`` or error.
    """
    try:
        tree = load_pcb(pcb_file)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    setup = _find_section(tree, "setup")
    if setup is None:
        return {"success": False, "error": "No (setup ...) section found in PCB file."}

    cr_section = _find_section(setup, "custom_rules")
    if cr_section is None:
        return {"success": False, "error": "No (custom_rules ...) section found."}

    removed = None
    new_children = [cr_section[0]]  # keep the "custom_rules" Symbol header
    for item in cr_section[1:]:
        if isinstance(item, list) and len(item) >= 2 and _str_symbol(item[0]) == "rule":
            if _str_symbol(item[1]) == rule_name:
                removed = item
                continue
        new_children.append(item)

    if removed is None:
        return {"success": False, "error": f"Custom rule '{rule_name}' not found."}

    # Replace cr_section contents in-place
    cr_section.clear()
    cr_section.extend(new_children)

    try:
        bak_path = save_pcb(pcb_file, tree)
    except OSError as exc:
        return {"success": False, "error": f"Failed to save PCB file: {exc}"}

    log.info("Removed custom rule '%s' from %s", rule_name, pcb_file)
    return {"success": True, "removed": rule_name, "backup_path": bak_path}


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
