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

        PCB coordinate convention (used by every PCB tool): millimetres,
        +X right, **+Y down** (KiCad PCB screen coords), and rotation in
        **degrees, clockwise-positive**. The ``x``/``y`` here are the
        footprint anchor in **board world coordinates**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with footprints: list of {reference, value, x, y (mm,
            world), rotation (deg, CW+), layer (e.g. "F.Cu"/"B.Cu")},
            count.
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

        Coordinates are mm, +Y down, rotation in degrees clockwise-positive
        (KiCad PCB convention). The footprint's ``x``/``y``/``rotation``
        are in **board world coordinates**, but each pad's ``local_x``/
        ``local_y`` are in **footprint-local coordinates** (relative to
        the footprint anchor, before applying its rotation). To get pad
        positions in world coordinates, transform with the footprint's
        rotation:

            world_x = fp.x + local_x * cos(θ) + local_y * sin(θ)
            world_y = fp.y - local_x * sin(θ) + local_y * cos(θ)
            (θ in radians; sign matches KiCad's clockwise-positive convention)

        Or use ``get_ratsnest`` which returns world pad coordinates directly.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"R1"``.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, value, x/y/rotation (world, mm/deg CW+),
            layer, properties (dict of all property name→value), pads
            (list of {number, type, shape, local_x, local_y, net_name}).
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
                "local_x": pad_x,
                "local_y": pad_y,
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

        Pad ``x``/``y`` in the result are **world coordinates** (mm,
        +Y down) — the footprint rotation has already been applied, so
        you can feed them straight into routing/placement reasoning.
        Contrast with ``get_footprint`` which returns *local* pad coords.

        Note: This is an approximation based on net membership and track
        endpoint proximity, not a full topological connectivity analysis.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with:
                unconnected: list of {net, from: {ref, pad, x, y}, to: {ref, pad, x, y}}
                    where x/y are world mm.
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

        # Collect all pads grouped by net_id with correct world coordinates
        # (apply footprint rotation using KiCad's clockwise-positive convention)
        import math
        pads_by_net: Dict[int, List[Tuple]] = defaultdict(list)
        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or "?"
            fp_x, fp_y, fp_rot_deg = get_fp_at(item)
            theta = math.radians(fp_rot_deg)
            cos_t = math.cos(theta)
            sin_t = math.sin(theta)
            for sub in item:
                if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                    continue
                pad_num = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                rel_x, rel_y = 0.0, 0.0
                net_id = None
                for psub in sub:
                    if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                        try:
                            rel_x, rel_y = float(psub[1]), float(psub[2])
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        try:
                            net_id = int(psub[1])
                        except (ValueError, TypeError):
                            pass
                if net_id is not None and net_id != 0:
                    # KiCad rotation is clockwise-positive; transform to world coords
                    abs_x = fp_x + rel_x * cos_t + rel_y * sin_t
                    abs_y = fp_y - rel_x * sin_t + rel_y * cos_t
                    pads_by_net[net_id].append((ref, str(pad_num), abs_x, abs_y))

        # Build track endpoint set keyed by (net_id, rounded_x, rounded_y)
        # so track endpoints from one net cannot falsely mark another net connected
        track_endpoints: set = set()
        _TOLERANCE = 0.01  # mm

        def _rounded(v: float) -> int:
            return round(v / _TOLERANCE)

        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            key = _sym(item[0])
            if key in ("segment", "via"):
                # Read the net id for this segment/via
                seg_net: Optional[int] = None
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "net":
                        try:
                            seg_net = int(sub[1])
                        except (ValueError, TypeError):
                            pass
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) in ("start", "end", "at"):
                        try:
                            track_endpoints.add((seg_net, _rounded(float(sub[1])), _rounded(float(sub[2]))))
                        except (ValueError, TypeError):
                            pass

        # For each net with ≥2 pads, report ALL pairs where neither pad
        # has a track endpoint at its position (simple heuristic)
        unconnected = []
        for net_id, pad_list in sorted(pads_by_net.items()):
            if len(pad_list) < 2:
                continue
            net_name = net_id_to_name.get(net_id, str(net_id))
            connected_indices = {
                i
                for i, (_, _, px, py) in enumerate(pad_list)
                if (net_id, _rounded(px), _rounded(py)) in track_endpoints
            }
            disconnected = [p for i, p in enumerate(pad_list) if i not in connected_indices]
            # Report all disconnected pairs (not just first)
            for i in range(len(disconnected)):
                for j in range(i + 1, len(disconnected)):
                    a, b = disconnected[i], disconnected[j]
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
