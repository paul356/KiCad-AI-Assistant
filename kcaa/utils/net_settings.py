"""
Net class settings read/write from KiCad project files (.kicad_pro).

Handles the ``net_settings.classes`` JSON array inside the project file.
No KiCad process or kicad-cli is required — all operations are file-based.
"""

import json as _json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Valid netclass fields in a .kicad_pro file
_NETCLASS_FIELDS = {
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "microvia_diameter",
    "microvia_drill",
    "diff_pair_width",
    "diff_pair_gap",
    "diff_pair_via_gap",
}


def _find_pro_file(pcb_file: str) -> str | None:
    """Given a .kicad_pcb path, return the corresponding .kicad_pro path."""
    pro_file = pcb_file.replace(".kicad_pcb", ".kicad_pro")
    if os.path.isfile(pro_file):
        return pro_file
    return None


def get_net_classes_from_pro(pro_file: str) -> dict[str, Any]:
    """Read net class settings from a .kicad_pro project file.

    Args:
        pro_file: Absolute path to the .kicad_pro file.

    Returns:
        ``{"success": True, "classes": [...], "default_netclass": {...}}``
        or an error dict.
    """
    try:
        with open(pro_file, encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError) as exc:
        return {"success": False, "error": f"Cannot read project file: {exc}"}

    ns = data.get("net_settings", {})
    classes_raw = ns.get("classes", [])
    if not isinstance(classes_raw, list):
        return {"success": False, "error": "Invalid net_settings.classes in project file"}

    classes = []
    default_netclass = None
    for nc in classes_raw:
        if not isinstance(nc, dict):
            continue
        entry = _netclass_to_dict(nc)
        classes.append(entry)
        if nc.get("name") == "Default":
            default_netclass = entry

    return {
        "success": True,
        "classes": classes,
        "default_netclass": default_netclass or {},
    }


def set_net_class_in_pro(
    pro_file: str, class_name: str, updates: dict[str, float]
) -> dict[str, Any]:
    """Update one net class's parameters in the project file.

    Creates a ``.kicad_pro.bak`` backup automatically.

    Args:
        pro_file: Absolute path to the .kicad_pro file.
        class_name: Name of the net class to update (e.g. ``"Default"``).
        updates: Mapping of field names (e.g. ``"clearance"``) to new values in mm.

    Returns:
        ``{"success": True, "updated": [...], "backup_path": ...}`` or error.
    """
    invalid = [k for k in updates if k not in _NETCLASS_FIELDS]
    if invalid:
        return {
            "success": False,
            "error": f"Unknown net class field(s): {', '.join(invalid)}. "
            f"Valid fields: {', '.join(sorted(_NETCLASS_FIELDS))}",
        }

    try:
        with open(pro_file, encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError) as exc:
        return {"success": False, "error": f"Cannot read project file: {exc}"}

    ns = data.get("net_settings")
    if ns is None:
        return {"success": False, "error": "No net_settings section in project file"}

    classes = ns.get("classes")
    if not isinstance(classes, list):
        return {"success": False, "error": "No classes array in net_settings"}

    target = None
    for nc in classes:
        if isinstance(nc, dict) and nc.get("name") == class_name:
            target = nc
            break

    if target is None:
        return {"success": False, "error": f"Net class '{class_name}' not found"}

    changed = []
    for key, value in updates.items():
        old_val = target.get(key)
        target[key] = value
        changed.append(f"{key}: {old_val}mm → {value}mm")

    # Create backup
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

    log.info("Updated net class '%s' in %s: %s", class_name, pro_file, changed)
    return {"success": True, "updated": changed, "backup_path": bak_path}


def _netclass_to_dict(nc: dict) -> dict[str, Any]:
    """Convert a raw netclass dict to our canonical format (only design-relevant fields)."""
    entry: dict[str, Any] = {"name": nc.get("name", "")}
    for field in _NETCLASS_FIELDS:
        if field in nc:
            entry[field] = nc[field]
    return entry
