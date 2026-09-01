"""
Helpers for locating and scanning KiCad footprint libraries.

Handles: finding fp-lib-table files, parsing them, resolving
${VAR} placeholders in URIs, and scanning .pretty directories
for .kicad_mod files.
"""

import contextlib
import logging
import os
import re
import sys
from typing import Any

import sexpdata

from kcaa.utils.config import ServerConfig
from kcaa.utils.pcb_board_utils import get_fp_edge_cuts_items

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# fp-lib-table location helpers
# ---------------------------------------------------------------------------


def _default_kicad_config_dirs() -> list[str]:
    """Return candidate KiCad config directories in priority order."""
    candidates = []
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
        for ver in ["10.0", "9.0", "8.0", "7.0"]:
            candidates.append(os.path.join(base, ver))
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "")
        for ver in ["10.0", "9.0", "8.0", "7.0"]:
            candidates.append(os.path.join(appdata, "kicad", ver))
    else:  # Linux
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        for ver in ["10.0", "9.0", "8.0", "7.0"]:
            candidates.append(os.path.join(xdg, "kicad", ver))
    return candidates


def find_fp_lib_tables(project_path: str | None = None) -> list[str]:
    """Return a list of fp-lib-table file paths that exist on this system.

    :param project_path: Optional path to a project directory; if given, the
        project-local fp-lib-table is searched first.
    :returns: List of existing fp-lib-table paths, most-specific first.
    """
    tables: list[str] = []
    if project_path:
        proj_table = os.path.join(os.path.dirname(project_path), "fp-lib-table")
        if os.path.isfile(proj_table):
            tables.append(proj_table)

    for config_dir in _default_kicad_config_dirs():
        candidate = os.path.join(config_dir, "fp-lib-table")
        if os.path.isfile(candidate):
            tables.append(candidate)

    return tables


# ---------------------------------------------------------------------------
# fp-lib-table parsing
# ---------------------------------------------------------------------------


def _build_env_map(project_dir: str | None = None) -> dict[str, str]:
    """Build a dict of KiCad ``${VAR}`` substitutions for fp-lib-table URIs.

    Sources, in increasing precedence:
      1. Defaults from :class:`ServerConfig` (KICAD{N}_SYMBOL_DIR,
         KICAD{N}_FOOTPRINT_DIR, KICAD{N}_TEMPLATE_DIR, KICAD{N}_3RD_PARTY) —
         derived from KICAD_APP_PATH or an auto-detected AppImage mount.
      2. Any ``KICAD*`` environment variables set in the current process.
      3. ``KIPRJMOD`` set to *project_dir* if given.
    """
    env: dict[str, str] = ServerConfig().get_env_vars()
    for key, val in os.environ.items():
        if key.startswith("KICAD"):
            env[key] = val
    if project_dir:
        env["KIPRJMOD"] = project_dir
    return env


def _resolve_uri(uri: str, env: dict[str, str]) -> str:
    """Expand ``${VAR}`` placeholders in a library URI.

    Logs a warning when any placeholder cannot be resolved so that missing
    KiCad environment variables (e.g. ``KICAD10_TEMPLATE_DIR`` when no
    AppImage is mounted) surface clearly instead of silently producing a
    broken path.
    """

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        return env.get(var, match.group(0))

    expanded = os.path.normpath(re.sub(r"\$\{([^}]+)\}", _replace, uri))
    unresolved = sorted(set(re.findall(r"\$\{([^}]+)\}", expanded)))
    if unresolved:
        log.warning(
            "Unresolved fp-lib-table variable(s) %s in URI: %s",
            unresolved,
            uri,
        )
    return expanded


def _parse_fp_lib_table_raw(table_path: str, env: dict[str, str]) -> list[dict[str, str]]:
    """Parse a single fp-lib-table file without recursion.

    :returns: List of dicts with keys ``nickname``, ``type``, ``uri``
        (resolved), ``raw_uri`` (unexpanded), ``description``.
    """
    libraries: list[dict[str, str]] = []
    try:
        with open(table_path, encoding="utf-8") as fh:
            raw = fh.read()
        data = sexpdata.loads(raw)
    except Exception:
        return libraries

    def _sym(v: Any) -> str:
        return str(v) if isinstance(v, sexpdata.Symbol) else str(v)

    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "lib"):
            continue
        entry: dict[str, str] = {
            "nickname": "",
            "type": "",
            "uri": "",
            "raw_uri": "",
            "description": "",
        }
        for sub in item[1:]:
            if not (isinstance(sub, list) and len(sub) >= 2):
                continue
            key = _sym(sub[0])
            val = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            if key == "name":
                entry["nickname"] = val
            elif key == "type":
                entry["type"] = val
            elif key == "uri":
                entry["raw_uri"] = val
                entry["uri"] = _resolve_uri(val, env)
            elif key == "descr":
                entry["description"] = val
        if entry["nickname"]:
            entry["table_path"] = os.path.realpath(table_path)
            libraries.append(entry)

    return libraries


def parse_fp_lib_table(table_path: str, project_dir: str | None = None) -> list[dict[str, str]]:
    """Parse an fp-lib-table file and return library entries.

    Handles ``type="Table"`` indirection: when an entry's type is ``Table``,
    its ``uri`` points to another fp-lib-table file whose entries are included
    inline.  A visited-path set prevents infinite recursion.

    :param table_path: Absolute path to an fp-lib-table file.
    :param project_dir: Optional project directory used to resolve
        ``${KIPRJMOD}`` in library URIs.
    :returns: List of dicts with keys ``nickname``, ``type``, ``uri``
        (resolved), ``raw_uri`` (unexpanded), ``description``.
    """
    env = _build_env_map(project_dir)
    return _parse_fp_lib_table_recursive(table_path, env, visited=set())


def _parse_fp_lib_table_recursive(
    table_path: str,
    env: dict[str, str],
    visited: set,
) -> list[dict[str, str]]:
    """Recursive helper for parse_fp_lib_table."""
    real_path = os.path.realpath(table_path)
    if real_path in visited:
        return []
    visited.add(real_path)

    result: list[dict[str, str]] = []
    for entry in _parse_fp_lib_table_raw(table_path, env):
        if entry["type"].lower() == "table":
            sub_table = entry["uri"]
            if os.path.isfile(sub_table):
                result.extend(_parse_fp_lib_table_recursive(sub_table, env, visited))
            else:
                log.warning(
                    "fp-lib-table indirection target not found: %s (entry '%s' in %s, raw uri %r)",
                    sub_table,
                    entry.get("nickname", ""),
                    table_path,
                    entry.get("raw_uri", ""),
                )
        else:
            result.append(entry)
    return result


def build_effective_library_list(
    project_path: str | None = None,
) -> list[dict[str, str]]:
    """Return a deduplicated, precedence-ordered list of footprint libraries.

    Reads all fp-lib-table files (project first, then global), resolves
    ``type="Table"`` indirections, and deduplicates by nickname — the first
    occurrence wins (project libraries override global ones).

    :param project_path: Optional path to a ``.kicad_pro`` file; its directory
        is checked for a project-local fp-lib-table and used to resolve
        ``${KIPRJMOD}`` in library URIs.
    :returns: List of dicts: ``nickname``, ``type``, ``uri`` (resolved),
        ``raw_uri`` (unexpanded), ``description``.
    """
    project_dir = os.path.dirname(project_path) if project_path else None
    table_paths = find_fp_lib_tables(project_path)
    seen_nicknames: set = set()
    result: list[dict[str, str]] = []
    for tpath in table_paths:
        for lib in parse_fp_lib_table(tpath, project_dir=project_dir):
            if lib["nickname"] not in seen_nicknames:
                seen_nicknames.add(lib["nickname"])
                result.append(lib)
    return result


# ---------------------------------------------------------------------------
# .pretty directory scanning
# ---------------------------------------------------------------------------


def scan_footprint_library(library_path: str) -> list[str]:
    """Return a list of footprint names (without .kicad_mod) in *library_path*.

    :param library_path: Path to a .pretty directory.  May be a nested
        fp-lib-table URI resolved to a local path.
    :returns: Sorted list of footprint name strings.
    """
    if not os.path.isdir(library_path):
        return []
    names = []
    for fname in os.listdir(library_path):
        if fname.endswith(".kicad_mod"):
            names.append(fname[: -len(".kicad_mod")])
    return sorted(names)


def parse_kicad_mod(path: str) -> dict[str, Any]:
    """Extract metadata and pad information from a .kicad_mod file.

    :param path: Absolute path to a .kicad_mod file.
    :returns: Dict with keys ``name``, ``description``, ``tags``, ``layer``,
        ``attr`` (e.g. ``"smd"``, ``"through_hole"``, or ``""``),
        ``has_3d_model`` (bool), ``pads`` (list of pad dicts),
        ``courtyard_bbox`` (``{min_x, min_y, max_x, max_y}`` or None).
    """
    result: dict[str, Any] = {
        "name": os.path.splitext(os.path.basename(path))[0],
        "description": "",
        "tags": "",
        "layer": "",
        "attr": "",
        "has_3d_model": False,
        "pads": [],
        "courtyard_bbox": None,
        "edge_cuts": [],
    }

    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        data = sexpdata.loads(raw)
    except Exception:
        return result

    def _sym(v: Any) -> str:
        return str(v) if isinstance(v, sexpdata.Symbol) else str(v)

    courtyard_points: list[tuple] = []

    for item in data:
        if not isinstance(item, list) or len(item) < 2:
            continue
        key = _sym(item[0])
        if key == "descr":
            result["description"] = item[1] if isinstance(item[1], str) else _sym(item[1])
        elif key == "tags":
            result["tags"] = item[1] if isinstance(item[1], str) else _sym(item[1])
        elif key == "layer":
            result["layer"] = item[1] if isinstance(item[1], str) else _sym(item[1])
        elif key == "attr":
            # (attr smd) or (attr through_hole) — unquoted value is a Symbol
            result["attr"] = _sym(item[1])
        elif key == "model":
            result["has_3d_model"] = True
        elif key == "pad":
            pad = _parse_pad(item, _sym)
            if pad:
                result["pads"].append(pad)
        elif key in ("fp_line", "fp_arc", "fp_rect") and _is_courtyard(item, _sym):
            courtyard_points.extend(_extract_line_points(item, _sym))

    if courtyard_points:
        xs = [p[0] for p in courtyard_points]
        ys = [p[1] for p in courtyard_points]
        result["courtyard_bbox"] = {
            "min_x": min(xs),
            "min_y": min(ys),
            "max_x": max(xs),
            "max_y": max(ys),
        }

    result["edge_cuts"] = get_fp_edge_cuts_items(data)

    return result


def _parse_pad(pad_node: list[Any], sym: Any) -> dict[str, Any] | None:
    """Extract pad info from a pad S-expression node."""
    if len(pad_node) < 4:
        return None
    pad_num = pad_node[1] if isinstance(pad_node[1], str) else sym(pad_node[1])
    pad_type = pad_node[2] if isinstance(pad_node[2], str) else sym(pad_node[2])
    pad_shape = pad_node[3] if isinstance(pad_node[3], str) else sym(pad_node[3])
    x, y = 0.0, 0.0
    layer = ""
    for sub in pad_node:
        if isinstance(sub, list) and len(sub) >= 3 and sym(sub[0]) == "at":
            with contextlib.suppress(ValueError, TypeError):
                x, y = float(sub[1]), float(sub[2])
        elif (
            isinstance(sub, list)
            and len(sub) >= 2
            and sym(sub[0]) == "layers"
            or isinstance(sub, list)
            and len(sub) >= 2
            and sym(sub[0]) == "layer"
        ):
            layer = sub[1] if isinstance(sub[1], str) else sym(sub[1])
    return {
        "number": str(pad_num),
        "type": str(pad_type),
        "shape": str(pad_shape),
        "x": x,
        "y": y,
        "layer": layer,
    }


def _is_courtyard(node: list[Any], sym: Any) -> bool:
    """Return True if node belongs to a courtyard layer."""
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 2 and sym(sub[0]) == "layer":
            layer = sub[1] if isinstance(sub[1], str) else sym(sub[1])
            return "Courtyard" in str(layer)
    return False


def _extract_line_points(node: list[Any], sym: Any) -> list[tuple]:
    """Extract all (x, y) coordinate pairs from a line/arc/rect node."""
    points = []
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 3 and sym(sub[0]) in ("start", "end", "mid"):
            with contextlib.suppress(ValueError, TypeError):
                points.append((float(sub[1]), float(sub[2])))
    return points
