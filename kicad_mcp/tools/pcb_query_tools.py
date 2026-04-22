"""
PCB board read / query tools for KiCad MCP server.

Provides read-only tools to inspect a .kicad_pcb file: board metadata,
footprint list, individual footprint detail, net list, and ratsnest
(unconnected pad pairs).
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from fastmcp import Context, FastMCP

from kicad_mcp.utils.pcb_sexp_utils import load_pcb
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    _sym,
)

log = logging.getLogger(__name__)


def register_pcb_query_tools(mcp: FastMCP) -> None:
    """Register PCB board read/query tools with the MCP server."""

    @mcp.tool()
    async def get_board_info(pcb_path: str, ctx: Context | None) -> Dict[str, Any]:
        """Get general information about a KiCad PCB board.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with thickness (mm), copper_layer_count, all_layers (list of
            {id, name, type}), footprint_count, net_count, segment_count,
            via_count, and generator information.
        """
        data = load_pcb(pcb_path)

        thickness = None
        layers: List[Dict] = []
        footprint_count = 0
        net_count = 0
        segment_count = 0
        via_count = 0
        generator = ""
        generator_version = ""

        for item in data:
            if not isinstance(item, list) or len(item) < 2:
                continue
            key = _sym(item[0])
            if key == "general":
                for sub in item[1:]:
                    if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "thickness":
                        thickness = float(sub[1])
            elif key == "layers":
                for sub in item[1:]:
                    if isinstance(sub, list) and len(sub) >= 3:
                        layers.append({
                            "id": int(sub[0]) if isinstance(sub[0], int) else sub[0],
                            "name": sub[1] if isinstance(sub[1], str) else _sym(sub[1]),
                            "type": sub[2] if isinstance(sub[2], str) else _sym(sub[2]),
                        })
            elif key == "footprint":
                footprint_count += 1
            elif key == "net":
                net_count += 1
            elif key == "segment":
                segment_count += 1
            elif key == "via":
                via_count += 1
            elif key == "generator":
                generator = item[1] if isinstance(item[1], str) else _sym(item[1])
            elif key == "generator_version":
                generator_version = item[1] if isinstance(item[1], str) else _sym(item[1])

        copper_layers = [lay for lay in layers if lay["type"] in ("signal", "mixed", "power")]

        return {
            "thickness_mm": thickness,
            "copper_layer_count": len(copper_layers),
            "all_layers": layers,
            "footprint_count": footprint_count,
            "net_count": max(0, net_count - 1),  # net 0 is always the unconnected net
            "segment_count": segment_count,
            "via_count": via_count,
            "generator": generator,
            "generator_version": generator_version,
        }

    @mcp.tool()
    async def list_footprints(pcb_path: str, ctx: Context | None) -> Dict[str, Any]:
        """List all footprints placed on a KiCad PCB board.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with footprints: list of {reference, value, x, y, rotation, layer}
        """
        data = load_pcb(pcb_path)
        footprints = []

        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or ""
            value = get_fp_property(item, "Value") or ""
            x, y, rot = get_fp_at(item)
            layer = get_fp_layer(item) or ""
            footprints.append({
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "rotation": rot,
                "layer": layer,
            })

        return {"footprints": footprints, "count": len(footprints)}

    @mcp.tool()
    async def get_footprint(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Get detailed information about a specific footprint on the board.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"R1"``.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, value, x, y, rotation, layer, all properties
            (dict), and pads list of {number, net_name, x, y, type, shape}.
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        x, y, rot = get_fp_at(fp)
        layer = get_fp_layer(fp) or ""

        # Collect all properties
        props: Dict[str, str] = {}
        for sub in fp:
            if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "property":
                name = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                val = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                props[name] = val

        # Collect pads
        pads = []
        for sub in fp:
            if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                continue
            pad_num = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            pad_type = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
            pad_shape = sub[3] if isinstance(sub[3], str) else _sym(sub[3])
            pad_x, pad_y = 0.0, 0.0
            net_name = ""
            for psub in sub:
                if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                    pad_x, pad_y = float(psub[1]), float(psub[2])
                elif isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "net":
                    net_name = psub[2] if isinstance(psub[2], str) else _sym(psub[2])
            pads.append({
                "number": str(pad_num),
                "type": str(pad_type),
                "shape": str(pad_shape),
                "x": pad_x,
                "y": pad_y,
                "net_name": net_name,
            })

        return {
            "reference": reference,
            "value": props.get("Value", ""),
            "x": x,
            "y": y,
            "rotation": rot,
            "layer": layer,
            "properties": props,
            "pads": pads,
        }

    @mcp.tool()
    async def list_nets(pcb_path: str, ctx: Context | None) -> Dict[str, Any]:
        """List all nets in a KiCad PCB board.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with nets: list of {net_id, name, pad_count}
        """
        data = load_pcb(pcb_path)

        net_id_to_name: Dict[int, str] = {}
        for item in data:
            if isinstance(item, list) and len(item) >= 3 and _sym(item[0]) == "net":
                net_id = int(item[1])
                net_name = item[2] if isinstance(item[2], str) else _sym(item[2])
                net_id_to_name[net_id] = net_name

        # Count pads per net by scanning footprints
        pad_counts: Dict[int, int] = defaultdict(int)
        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            for sub in item:
                if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                    continue
                for psub in sub:
                    if isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        nid = int(psub[1])
                        pad_counts[nid] += 1

        nets = []
        for net_id, net_name in sorted(net_id_to_name.items()):
            if net_id == 0:
                continue  # skip the unconnected pseudo-net
            nets.append({
                "net_id": net_id,
                "name": net_name,
                "pad_count": pad_counts.get(net_id, 0),
            })

        return {"nets": nets, "count": len(nets)}

    @mcp.tool()
    async def get_ratsnest(pcb_path: str, ctx: Context | None) -> Dict[str, Any]:
        """Get unconnected pad pairs (ratsnest) for a KiCad PCB board.

        Identifies pads that share a net but are not yet connected by
        copper tracks or vias.  Returns a list of unconnected pairs —
        an empty list means the board is fully routed.

        Note: This is an approximation based on net membership and track
        endpoint proximity, not a full topological connectivity analysis.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with:
                unconnected: list of {net, from: {ref, pad, x, y}, to: {ref, pad, x, y}}
                unconnected_count: number of unconnected pairs
                fully_routed: True if no unconnected pairs found
        """
        data = load_pcb(pcb_path)

        net_id_to_name: Dict[int, str] = {}
        for item in data:
            if isinstance(item, list) and len(item) >= 3 and _sym(item[0]) == "net":
                net_id_to_name[int(item[1])] = (
                    item[2] if isinstance(item[2], str) else _sym(item[2])
                )

        # Collect all pads grouped by net_id
        # Each entry: (ref, pad_num, abs_x, abs_y)
        pads_by_net: Dict[int, List[Tuple]] = defaultdict(list)
        for item in data:
            if not (isinstance(item, list) and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or "?"
            fp_x, fp_y, fp_rot = get_fp_at(item)
            for sub in item:
                if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                    continue
                pad_num = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                rel_x, rel_y = 0.0, 0.0
                net_id = None
                for psub in sub:
                    if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                        rel_x, rel_y = float(psub[1]), float(psub[2])
                    elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        net_id = int(psub[1])
                if net_id is not None and net_id != 0:
                    abs_x = fp_x + rel_x
                    abs_y = fp_y + rel_y
                    pads_by_net[net_id].append((ref, str(pad_num), abs_x, abs_y))

        # Build track endpoint set for connectivity check
        track_endpoints: set = set()
        _TOLERANCE = 0.01  # mm

        def _rounded(v: float) -> int:
            return round(v / _TOLERANCE)

        for item in data:
            if not isinstance(item, list):
                continue
            key = _sym(item[0])
            if key in ("segment", "via"):
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) in ("start", "end", "at"):
                        try:
                            track_endpoints.add((_rounded(float(sub[1])), _rounded(float(sub[2]))))
                        except (ValueError, TypeError):
                            pass

        # For each net with ≥2 pads, report pairs where neither pad
        # has a track endpoint at its position (simple heuristic)
        unconnected = []
        for net_id, pad_list in sorted(pads_by_net.items()):
            if len(pad_list) < 2:
                continue
            net_name = net_id_to_name.get(net_id, str(net_id))
            connected = {
                i
                for i, (_, _, px, py) in enumerate(pad_list)
                if (_rounded(px), _rounded(py)) in track_endpoints
            }
            disconnected = [p for i, p in enumerate(pad_list) if i not in connected]
            if len(disconnected) >= 2:
                # Report first pair as representative
                a, b = disconnected[0], disconnected[1]
                unconnected.append({
                    "net": net_name,
                    "from": {"ref": a[0], "pad": a[1], "x": a[2], "y": a[3]},
                    "to": {"ref": b[0], "pad": b[1], "x": b[2], "y": b[3]},
                })

        return {
            "unconnected": unconnected,
            "unconnected_count": len(unconnected),
            "fully_routed": len(unconnected) == 0,
        }
