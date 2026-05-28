"""
PCB zone tools for KiCad MCP server.

Provides tools to:
  - List zones (copper pour and keepout), distinguished by type.
  - Add a zone (copper pour or keepout).
  - Delete a zone by UUID.

PCB coordinate convention: millimetres, +X right, +Y down,
rotation clockwise-positive (KiCad PCB convention).

All mutation tools create a ``.kicad_pcb.bak`` backup before writing.
"""

import logging
from typing import Any
import uuid

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sym(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain string."""
    if isinstance(value, sexpdata.Symbol):
        return value.value()
    return str(value)


def _parse_zone(node: list[Any]) -> dict[str, Any]:
    """Parse a ``(zone ...)`` S-expression node into a plain dict.

    Returns a dict with:
        uuid         – zone UUID string (empty string if absent)
        zone_type    – ``"keepout"`` or ``"copper_pour"``
        net          – net number (int), 0 for keepout
        net_name     – net name string
        layer        – layer string (single-layer zones) or list of layers
        hatch_style  – hatch style string (e.g. ``"edge"``)
        polygon_pts  – list of ``{"x": float, "y": float}`` for the first
                       polygon outline (empty list if absent)
        keepout_rules – dict of keepout restrictions (only for keepout zones)
        fill         – whether fill is enabled (bool, copper_pour only)
    """
    result: dict[str, Any] = {
        "uuid": "",
        "zone_type": "copper_pour",
        "net": 0,
        "net_name": "",
        "layer": "",
        "hatch_style": "",
        "polygon_pts": [],
        "keepout_rules": {},
        "fill": False,
    }

    for sub in node[1:]:
        if not isinstance(sub, list) or len(sub) < 1:
            continue
        key = _sym(sub[0])

        if key == "uuid":
            result["uuid"] = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
        elif key == "net":
            try:
                result["net"] = int(sub[1])
            except (IndexError, ValueError, TypeError):
                if len(sub) >= 2 and sub[1] != "":
                    result["net"] = None
                    result["net_name"] = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
        elif key == "net_name":
            result["net_name"] = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
        elif key == "layer":
            result["layer"] = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
        elif key == "layers":
            result["layer"] = [(s if isinstance(s, str) else _sym(s)) for s in sub[1:]]
        elif key == "hatch":
            if len(sub) >= 2:
                result["hatch_style"] = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
        elif key == "keepout":
            result["zone_type"] = "keepout"
            rules: dict[str, str] = {}
            for rule in sub[1:]:
                if isinstance(rule, list) and len(rule) >= 2:
                    rules[_sym(rule[0])] = _sym(rule[1])
            result["keepout_rules"] = rules
        elif key == "fill":
            # (fill yes ...) or (fill (thermal_gap ...) ...)
            if len(sub) >= 2:
                val = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                result["fill"] = val not in ("no", "none")
            else:
                result["fill"] = True
        elif key == "polygon":
            # First polygon only — the outline
            for psub in sub[1:]:
                if isinstance(psub, list) and _sym(psub[0]) == "pts":
                    pts = []
                    for pt in psub[1:]:
                        if isinstance(pt, list) and _sym(pt[0]) == "xy" and len(pt) >= 3:
                            pts.append({"x": float(pt[1]), "y": float(pt[2])})
                    result["polygon_pts"] = pts
                    break  # only first polygon

    return result


def _find_zones(data: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    """Return ``(index, parsed_zone)`` for every top-level zone in *data*."""
    results = []
    for idx, item in enumerate(data):
        if isinstance(item, list) and len(item) >= 1 and _sym(item[0]) == "zone":
            results.append((idx, _parse_zone(item)))
    return results


def _get_layer_names(data: list[Any]) -> set[str]:
    """Return the set of declared board layer names."""
    for item in data:
        if isinstance(item, list) and len(item) >= 1 and _sym(item[0]) == "layers":
            names: set[str] = set()
            for sub in item[1:]:
                if isinstance(sub, list) and len(sub) >= 2:
                    names.add(sub[1] if isinstance(sub[1], str) else _sym(sub[1]))
            return names
    return set()


def _find_net_by_name(data: list[Any], net_name: str) -> tuple[int | None, str] | None:
    """Return ``(net_id, canonical_name)`` for *net_name*, or None if absent."""
    for item in data:
        if isinstance(item, list) and len(item) >= 3 and _sym(item[0]) == "net":
            try:
                net_id = int(item[1])
            except (TypeError, ValueError):
                continue
            name = item[2] if isinstance(item[2], str) else _sym(item[2])
            if name == net_name:
                return net_id, name

    # KiCad 9/10 boards may omit the top-level net table and use name-only net
    # references directly in pads: (net "GND").
    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
            continue
        for sub in item:
            if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                continue
            for psub in sub:
                if not (isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net"):
                    continue
                if len(psub) >= 3:
                    raw_name = psub[2] if isinstance(psub[2], str) else _sym(psub[2])
                else:
                    raw_name = psub[1] if isinstance(psub[1], str) else _sym(psub[1])
                if raw_name == net_name:
                    return None, raw_name
    return None


def _build_xy_pts(points: list[dict[str, float]]) -> list[Any]:
    """Build a KiCad ``(pts (xy ...) ...)`` node from point dicts."""
    return [
        sexpdata.Symbol("pts"),
        *[[sexpdata.Symbol("xy"), float(point["x"]), float(point["y"])] for point in points],
    ]


def _build_zone_node(
    *,
    zone_uuid: str,
    zone_type: str,
    layer: str,
    polygon_pts: list[dict[str, float]],
    net_id: int | None,
    net_name: str,
    hatch_style: str,
    hatch_pitch: float,
    clearance: float,
    min_thickness: float,
    fill: bool,
    thermal_gap: float,
    thermal_bridge_width: float,
    keepout_tracks: str,
    keepout_vias: str,
    keepout_copperpour: str,
    keepout_footprints: str,
    keepout_text: str,
) -> list[Any]:
    """Build a top-level KiCad ``(zone ...)`` node."""
    zone_node: list[Any] = [
        sexpdata.Symbol("zone"),
        [sexpdata.Symbol("layer"), layer],
        [sexpdata.Symbol("uuid"), zone_uuid],
        [sexpdata.Symbol("hatch"), sexpdata.Symbol(hatch_style), float(hatch_pitch)],
    ]

    if net_id is None:
        zone_node.insert(1, [sexpdata.Symbol("net"), net_name])
        if net_name:
            zone_node.insert(2, [sexpdata.Symbol("net_name"), net_name])
    else:
        zone_node.insert(1, [sexpdata.Symbol("net"), net_id])
        zone_node.insert(2, [sexpdata.Symbol("net_name"), net_name])

    if zone_type == "keepout":
        zone_node.append(
            [
                sexpdata.Symbol("keepout"),
                [sexpdata.Symbol("tracks"), sexpdata.Symbol(keepout_tracks)],
                [sexpdata.Symbol("vias"), sexpdata.Symbol(keepout_vias)],
                [sexpdata.Symbol("copperpour"), sexpdata.Symbol(keepout_copperpour)],
                [sexpdata.Symbol("footprints"), sexpdata.Symbol(keepout_footprints)],
                [sexpdata.Symbol("text"), sexpdata.Symbol(keepout_text)],
            ]
        )
    else:
        zone_node.extend(
            [
                [sexpdata.Symbol("connect_pads"), [sexpdata.Symbol("clearance"), float(clearance)]],
                [sexpdata.Symbol("min_thickness"), float(min_thickness)],
                (
                    [
                        sexpdata.Symbol("fill"),
                        sexpdata.Symbol("yes"),
                        [sexpdata.Symbol("thermal_gap"), float(thermal_gap)],
                        [sexpdata.Symbol("thermal_bridge_width"), float(thermal_bridge_width)],
                    ]
                    if fill
                    else [sexpdata.Symbol("fill"), sexpdata.Symbol("no")]
                ),
            ]
        )

    zone_node.append([sexpdata.Symbol("polygon"), _build_xy_pts(polygon_pts)])
    return zone_node


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_pcb_zone_tools(mcp: FastMCP) -> None:
    """Register PCB zone tools with the MCP server."""

    @mcp.tool()
    async def list_zones(
        pcb_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List all zones in a KiCad PCB file.

        Zones are returned with their type distinguished:
        - ``"copper_pour"`` – filled copper area tied to a net.
        - ``"keepout"``     – area that restricts routing/placement.

        PCB coordinates: mm, +X right, +Y down.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).

        Returns:
            dict with:
                zones: list of zone dicts, each containing:
                    uuid, zone_type, net, net_name, layer,
                    hatch_style, polygon_pts, keepout_rules, fill.
                count: total number of zones.
                copper_pour_count: number of copper-pour zones.
                keepout_count: number of keepout zones.
        """
        data = load_pcb(pcb_path)
        indexed = _find_zones(data)
        zones = [z for _, z in indexed]
        copper = sum(1 for z in zones if z["zone_type"] == "copper_pour")
        keepout = sum(1 for z in zones if z["zone_type"] == "keepout")
        return {
            "zones": zones,
            "count": len(zones),
            "copper_pour_count": copper,
            "keepout_count": keepout,
        }

    @mcp.tool()
    async def add_zone(
        pcb_path: str,
        layer: str,
        polygon_pts: list[dict[str, float]],
        ctx: Context | None,
        zone_type: str = "copper_pour",
        net_name: str | None = None,
        hatch_style: str = "edge",
        hatch_pitch: float = 0.508,
        clearance: float = 0.5,
        min_thickness: float = 0.25,
        fill: bool = True,
        thermal_gap: float = 0.5,
        thermal_bridge_width: float = 0.5,
        keepout_tracks: str = "not_allowed",
        keepout_vias: str = "not_allowed",
        keepout_copperpour: str = "not_allowed",
        keepout_footprints: str = "allowed",
        keepout_text: str = "allowed",
    ) -> dict[str, Any]:
        """Add a copper-pour zone or keepout zone to a KiCad PCB file.

        For ``zone_type="copper_pour"``, ``net_name`` is required and must match
        an existing board net. For ``zone_type="keepout"``, ``net_name`` is
        ignored and the zone is created on net 0.

        Polygon points are in board coordinates (mm, +X right, +Y down) and are
        used as the zone outline in the given order. The polygon must contain at
        least 3 points.
        """
        if zone_type not in {"copper_pour", "keepout"}:
            return {"error": "zone_type must be 'copper_pour' or 'keepout'."}
        if len(polygon_pts) < 3:
            return {"error": "polygon_pts must contain at least 3 points."}
        if hatch_style not in {"edge", "full"}:
            return {"error": "hatch_style must be 'edge' or 'full'."}
        for point in polygon_pts:
            if not isinstance(point, dict) or "x" not in point or "y" not in point:
                return {"error": "Each polygon point must be a dict with numeric 'x' and 'y'."}
            try:
                float(point["x"])
                float(point["y"])
            except (TypeError, ValueError):
                return {"error": "Each polygon point must use numeric 'x' and 'y' values."}

        data = load_pcb(pcb_path)
        if layer not in _get_layer_names(data):
            return {"error": f"Layer '{layer}' is not declared in this PCB file."}

        if zone_type == "keepout":
            resolved_net_id = 0
            resolved_net_name = ""
        else:
            if not net_name:
                return {"error": "net_name is required for copper_pour zones."}
            resolved_net = _find_net_by_name(data, net_name)
            if resolved_net is None:
                return {"error": f"Net '{net_name}' was not found in this PCB file."}
            resolved_net_id, resolved_net_name = resolved_net

        zone_uuid = str(uuid.uuid4())
        zone_node = _build_zone_node(
            zone_uuid=zone_uuid,
            zone_type=zone_type,
            layer=layer,
            polygon_pts=polygon_pts,
            net_id=resolved_net_id,
            net_name=resolved_net_name,
            hatch_style=hatch_style,
            hatch_pitch=hatch_pitch,
            clearance=clearance,
            min_thickness=min_thickness,
            fill=fill,
            thermal_gap=thermal_gap,
            thermal_bridge_width=thermal_bridge_width,
            keepout_tracks=keepout_tracks,
            keepout_vias=keepout_vias,
            keepout_copperpour=keepout_copperpour,
            keepout_footprints=keepout_footprints,
            keepout_text=keepout_text,
        )
        data.append(zone_node)

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "added": True,
            "zone_uuid": zone_uuid,
            "zone_type": zone_type,
            "net": resolved_net_id,
            "net_name": resolved_net_name,
            "layer": layer,
            "fill": fill if zone_type == "copper_pour" else False,
            "polygon_pts": [{"x": float(p["x"]), "y": float(p["y"])} for p in polygon_pts],
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def delete_zone(
        pcb_path: str,
        zone_uuid: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Delete a zone from a KiCad PCB file by its UUID.

        Works for both copper-pour zones and keepout zones.
        A ``.kicad_pcb.bak`` backup is created before writing.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            zone_uuid: UUID of the zone to delete (from ``list_zones``).
            ctx: MCP context (unused).

        Returns:
            dict with:
                deleted: bool — True if the zone was found and removed.
                zone_uuid: the UUID that was targeted.
                backup_path: path to the backup file (if write succeeded).
                pcb_path: echoed back.
        """
        data = load_pcb(pcb_path)
        indexed = _find_zones(data)

        target_idx = None
        for idx, zone in indexed:
            if zone["uuid"] == zone_uuid:
                target_idx = idx
                break

        if target_idx is None:
            return {
                "deleted": False,
                "zone_uuid": zone_uuid,
                "error": f"Zone with UUID '{zone_uuid}' not found.",
                "pcb_path": pcb_path,
            }

        del data[target_idx]

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {
                "deleted": False,
                "zone_uuid": zone_uuid,
                "error": f"Failed to write PCB file: {exc}",
            }

        return {
            "deleted": True,
            "zone_uuid": zone_uuid,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def refill_zones(ctx: Context | None = None) -> dict:
        """Refill all zones (copper pours) on the currently open PCB.

        After adding or modifying zones via file-based tools, call this to
        trigger KiCad to recalculate the copper fill for all zones. This
        uses the IPC API to send a RefillZones command to the running KiCad
        instance.

        The tool first reverts the board in KiCad to sync with the latest
        disk state (which includes file-based zone changes), then refills
        all zones. The operation blocks until the refill is complete (up
        to 30 seconds).

        Returns:
            dict with keys:
                success (bool): True if the refill succeeded.
                error (str): Present only if the refill failed.
        """
        try:
            from kcaa.tools.kipy_tools import _connect  # noqa: PLC0415

            kicad = _connect(timeout_ms=35000)
            board = kicad.get_board()
            if board is None:
                return {"success": False, "error": "No board is currently open in KiCad"}
            # Revert first so KiCad's in-memory state matches the disk file
            # (which may have been modified by file-based tools like add_zone).
            board.revert()
            board.refill_zones(block=True, max_poll_seconds=30.0)
            # Save the filled zones back to disk so they persist after reload.
            board.save()
            return {"success": True}
        except RuntimeError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log.exception("Unexpected error in refill_zones")
            return {"success": False, "error": str(exc)}
