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

    # Find the target net class and the Default net class (as fallback for auto-creation)
    target = None
    default_class = None
    for nc in classes:
        if isinstance(nc, dict):
            name = nc.get("name")
            if name == class_name:
                target = nc
            if name == "Default":
                default_class = nc

    created = False
    if target is None:
        # Auto-create: start from Default values, then apply user updates
        if default_class is None:
            return {
                "success": False,
                "error": f"Net class '{class_name}' not found and no Default net class exists to copy from",
            }
        target = dict(default_class)
        target["name"] = class_name
        classes.append(target)
        created = True
        changed = []
        for key, value in updates.items():
            old_val = target.get(key)
            target[key] = value
            changed.append(f"{key}: {old_val}mm → {value}mm")
    else:
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

    action = "Created & updated" if created else "Updated"
    log.info("%s net class '%s' in %s: %s", action, class_name, pro_file, changed)
    result: dict[str, Any] = {
        "success": True,
        "updated": changed,
        "backup_path": bak_path,
        "warning": f"Changes saved to {pro_file}. Reopen the project in KiCad for net class values to take effect.",
    }
    if created:
        result["created"] = True
    return result


def assign_nets_to_class_in_pro(pro_file: str, class_name: str, nets: list[str]) -> dict[str, Any]:
    """Assign nets to a net class via netclass_patterns in the project file.

    Adds exact-match entries to ``net_settings.netclass_patterns`` so each
    listed net appears in the **Members** tab of Board Setup → Net Classes
    and receives the class's design constraints.

    If a net was previously assigned to a different class, the old pattern
    is **removed** (moved to the new class).

    Nets already assigned to the target class are silently skipped (returned
    in ``existing``).  Creates a ``.kicad_pro.bak`` backup automatically.

    Args:
        pro_file: Absolute path to the .kicad_pro file.
        class_name: Name of the net class to assign nets to.
        nets: List of net names (e.g. ``["/tp4056/VBUS", "VCC_SYS"]``).

    Returns:
        ``{"success": True, "assigned": [...], "existing": [...], "backup_path": ...}``
        or error dict.
    """
    if not nets:
        return {"success": False, "error": "No net names provided"}

    try:
        with open(pro_file, encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, PermissionError) as exc:
        return {"success": False, "error": f"Cannot read project file: {exc}"}

    ns = data.get("net_settings")
    if ns is None:
        return {"success": False, "error": "No net_settings section in project file"}

    # Verify the target class exists
    classes = ns.get("classes", [])
    class_exists = any(isinstance(nc, dict) and nc.get("name") == class_name for nc in classes)
    if not class_exists:
        return {
            "success": False,
            "error": f"Net class '{class_name}' not found. "
            f"Use set_net_class_rules to create it first.",
        }

    # Load existing patterns (may be null, missing, or array)
    patterns: list[dict] = ns.get("netclass_patterns")
    if not isinstance(patterns, list):
        patterns = []

    net_set = {n.strip() for n in nets if n.strip()}
    if not net_set:
        return {"success": False, "error": "No valid net names to assign"}

    assigned = []
    existing = []

    # Build lookup: pattern_text → current class (for dedup and move)
    pattern_map: dict[str, dict] = {}
    for p in patterns:
        pat = p.get("pattern", "")
        if pat:
            pattern_map[pat] = p

    # Check which nets already have the right class; identify nets to move
    for net in sorted(net_set):
        cur = pattern_map.get(net)
        if cur is not None and cur.get("netclass") == class_name:
            existing.append(net)
        else:
            assigned.append(net)

    if not assigned and not existing:
        return {"success": False, "error": "No valid net names to assign"}

    # Add / update patterns for newly assigned nets
    for net in assigned:
        if net in pattern_map:
            # Update existing pattern (move from other class)
            old_class = pattern_map[net].get("netclass")
            pattern_map[net]["netclass"] = class_name
            log.info("Moved net '%s' from class '%s' to '%s'", net, old_class, class_name)
        else:
            # New pattern
            new_entry = {"netclass": class_name, "pattern": net}
            pattern_map[net] = new_entry
            patterns.append(new_entry)

    # Rebuild patterns array preserving order of unchanged entries
    # (patterns that weren't touched stay in original positions)
    seen_patterns = set()
    new_patterns = []
    for p in patterns:
        pat = p.get("pattern", "")
        if pat in pattern_map and pat not in seen_patterns:
            new_patterns.append(pattern_map[pat])
            seen_patterns.add(pat)

    ns["netclass_patterns"] = new_patterns

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

    log.info(
        "Assigned %d net(s) to class '%s' in %s: %s",
        len(assigned),
        class_name,
        pro_file,
        assigned,
    )
    return {
        "success": True,
        "assigned": assigned,
        "existing": existing,
        "backup_path": bak_path,
        "warning": f"Changes saved to {pro_file}. Reopen the project in KiCad for net assignments to take effect.",
    }


def _netclass_to_dict(nc: dict) -> dict[str, Any]:
    """Convert a raw netclass dict to our canonical format (only design-relevant fields)."""
    entry: dict[str, Any] = {"name": nc.get("name", "")}
    for field in _NETCLASS_FIELDS:
        if field in nc:
            entry[field] = nc[field]
    return entry
