"""
PCB board read / query tools for KiCad MCP server.

Provides read-only tools to inspect a .kicad_pcb file: board metadata,
footprint list, individual footprint detail, footprint/board bounding
boxes, net list, and ratsnest (unconnected pad pairs).
"""
import logging
import math
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from fastmcp import Context, FastMCP

from kicad_mcp.utils.pcb_sexp_utils import load_pcb
from kicad_mcp.utils.pcb_board_utils import get_fp_courtyard_bbox
from kicad_mcp.utils.pcb_footprint_utils import (
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
    _sym,
)
from kicad_mcp.tools.pcb_placement_helpers import (
    _classify_footprint,
    _compute_hpwl,
    _get_all_footprint_bboxes,
    _get_board_bounds_or_fallback,
    _get_fp_pads_world,
    _TIER_NAMES,
)

log = logging.getLogger(__name__)


def _collect_top_level_nets(data: List[Any]) -> tuple[Dict[int, str], Dict[str, int]]:
    """Return board net lookup tables from top-level ``(net ...)`` entries.
    
    Supports both KiCad 8 format ``(net <id> "<name>")`` and KiCad 10 format
    ``(net "<name>")``. For KiCad 10, net IDs are assigned sequentially starting
    from 1 (ID 0 is reserved for unconnected).
    """
    net_id_to_name: Dict[int, str] = {}
    net_name_to_id: Dict[str, int] = {}
    next_id = 1
    for item in data:
        if not (isinstance(item, list) and len(item) >= 2 and _sym(item[0]) == "net"):
            continue
        # KiCad 8: (net <id> "<name>")
        if len(item) >= 3:
            try:
                net_id = int(item[1])
                net_name = item[2] if isinstance(item[2], str) else _sym(item[2])
                net_id_to_name[net_id] = net_name
                if net_name:
                    net_name_to_id[net_name] = net_id
                if net_id >= next_id:
                    next_id = net_id + 1
                continue
            except (TypeError, ValueError):
                pass
        # KiCad 10: (net "<name>")
        net_name = item[1] if isinstance(item[1], str) else _sym(item[1])
        if net_name and net_name not in net_name_to_id:
            net_id_to_name[next_id] = net_name
            net_name_to_id[net_name] = next_id
            next_id += 1
    return net_id_to_name, net_name_to_id


def _parse_net_ref(
    net_node: list[Any],
    net_name_to_id: Dict[str, int],
    net_id_to_name: Dict[int, str],
) -> tuple[int | None, str]:
    """Parse a KiCad net reference in either legacy or name-only form.
    
    Supports:
    - KiCad 8: ``(net <id> "<name>")``
    - KiCad 10: ``(net "<name>")``
    """
    if len(net_node) >= 3:
        try:
            net_id = int(net_node[1])
            net_name = net_node[2] if isinstance(net_node[2], str) else _sym(net_node[2])
            return net_id, net_name
        except (TypeError, ValueError):
            pass

    # KiCad 10 format: (net "<name>")
    if len(net_node) >= 2:
        raw = net_node[1] if isinstance(net_node[1], str) else _sym(net_node[1])
        if raw in net_name_to_id:
            return net_name_to_id[raw], raw
        return None, raw

    return None, ""


def _net_sort_key(net: Dict[str, Any]) -> tuple[int, Any, str]:
    """Sort nets by known numeric id first, then by name."""
    net_id = net.get("net_id")
    name = str(net.get("name", ""))
    return (0, net_id, name) if isinstance(net_id, int) else (1, name.lower(), name)


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
        pad_net_names: set[str] = set()

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
                for sub in item[1:]:
                    if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                        continue
                    for psub in sub:
                        if isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                            _, pad_net_name = _parse_net_ref(psub, {}, {})
                            if pad_net_name:
                                pad_net_names.add(pad_net_name)
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

        # KiCad 8 has net 0 with empty name, KiCad 10 doesn't
        has_empty_net = any(name == "" for name in pad_net_names) or net_count == 0
        adjusted_net_count = max(0, net_count - 1) if has_empty_net else net_count

        return {
            "thickness_mm": thickness,
            "copper_layer_count": len(copper_layers),
            "all_layers": layers,
            "footprint_count": footprint_count,
            "net_count": adjusted_net_count if net_count else len(pad_net_names),
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
                elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                    _, net_name = _parse_net_ref(psub, {}, {})
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
    async def get_footprint_bbox(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Return the world-coordinate bounding box of a footprint's courtyard.

        The bounding box is computed from ``F.Courtyard`` / ``B.Courtyard``
        graphic items in the footprint, transformed to board world
        coordinates by applying the footprint's position and rotation
        (clockwise-positive).  If the footprint has no courtyard items the
        tool falls back to all ``fp_line``/``fp_rect``/``fp_circle`` items.

        Use this to check for footprint overlaps before placement or to
        size the board outline around all components.

        PCB coordinates: mm, +X right, **+Y down**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"U1"``.
            ctx: MCP context (unused).

        Returns:
            dict with reference, x/y/rotation (anchor), bbox
            {min_x, min_y, max_x, max_y, width, height} in world mm,
            or ``error`` if not found / no geometry.
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        fp_x, fp_y, fp_rot = get_fp_at(fp)
        bbox = get_fp_courtyard_bbox(fp, fp_x, fp_y, fp_rot)
        if bbox is None:
            return {"error": f"No courtyard or graphic geometry found for '{reference}'."}

        return {
            "reference": reference,
            "x": fp_x,
            "y": fp_y,
            "rotation": fp_rot,
            "bbox": bbox,
        }

    @mcp.tool()
    async def get_board_bounding_box(
        pcb_path: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Return the union bounding box of all footprint courtyards on the board.

        Useful for determining the minimum board size needed to contain all
        placed footprints, and for checking whether all footprints fit
        within the current board outline.

        PCB coordinates: mm, +X right, **+Y down**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).

        Returns:
            dict with bbox {min_x, min_y, max_x, max_y, width, height}
            in world mm covering all footprints, footprint_count,
            footprints_without_courtyard (list of references that had to
            fall back to raw graphics or were skipped).
        """

        def _sym_local(v: Any) -> str:
            return str(v) if isinstance(v, sexpdata.Symbol) else str(v)

        data = load_pcb(pcb_path)
        all_min_x: list[float] = []
        all_min_y: list[float] = []
        all_max_x: list[float] = []
        all_max_y: list[float] = []
        fp_count = 0
        no_courtyard: list[str] = []

        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            if _sym_local(item[0]) != "footprint":
                continue
            fp_count += 1
            ref = ""
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3 and _sym_local(sub[0]) == "property":
                    if (sub[1] if isinstance(sub[1], str) else _sym_local(sub[1])) == "Reference":
                        ref = sub[2] if isinstance(sub[2], str) else _sym_local(sub[2])
            fp_x, fp_y, fp_rot = 0.0, 0.0, 0.0
            for sub in item:
                if isinstance(sub, list) and len(sub) >= 3 and _sym_local(sub[0]) == "at":
                    fp_x, fp_y = float(sub[1]), float(sub[2])
                    fp_rot = float(sub[3]) if len(sub) > 3 else 0.0

            bbox = get_fp_courtyard_bbox(item, fp_x, fp_y, fp_rot)
            if bbox is None:
                no_courtyard.append(ref)
                continue
            all_min_x.append(bbox["min_x"])
            all_min_y.append(bbox["min_y"])
            all_max_x.append(bbox["max_x"])
            all_max_y.append(bbox["max_y"])

        if not all_min_x:
            return {
                "error": "No footprint geometry found.",
                "footprint_count": fp_count,
                "footprints_without_courtyard": no_courtyard,
            }

        min_x = min(all_min_x)
        min_y = min(all_min_y)
        max_x = max(all_max_x)
        max_y = max(all_max_y)

        return {
            "bbox": {
                "min_x": round(min_x, 4),
                "min_y": round(min_y, 4),
                "max_x": round(max_x, 4),
                "max_y": round(max_y, 4),
                "width": round(max_x - min_x, 4),
                "height": round(max_y - min_y, 4),
            },
            "footprint_count": fp_count,
            "footprints_without_courtyard": no_courtyard,
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

        net_id_to_name, net_name_to_id = _collect_top_level_nets(data)
        nets_by_name: Dict[str, Dict[str, Any]] = {}
        for net_id, net_name in net_id_to_name.items():
            if net_id == 0 or not net_name:
                continue
            nets_by_name[net_name] = {"net_id": net_id, "name": net_name, "pad_count": 0}

        # Count pads per net by scanning footprints.
        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            for sub in item:
                if not (isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"):
                    continue
                for psub in sub:
                    if isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        net_id, net_name = _parse_net_ref(psub, net_name_to_id, net_id_to_name)
                        if not net_name:
                            continue
                        entry = nets_by_name.setdefault(
                            net_name,
                            {"net_id": net_id, "name": net_name, "pad_count": 0},
                        )
                        if entry["net_id"] is None and net_id is not None:
                            entry["net_id"] = net_id
                        entry["pad_count"] += 1

        nets = sorted(nets_by_name.values(), key=_net_sort_key)

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

        net_id_to_name, net_name_to_id = _collect_top_level_nets(data)

        # Collect all pads grouped by net key with correct world coordinates.
        # (apply footprint rotation using KiCad's clockwise-positive convention)
        import math
        pads_by_net: Dict[str, List[Tuple]] = defaultdict(list)
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
                net_key = ""
                for psub in sub:
                    if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                        try:
                            rel_x, rel_y = float(psub[1]), float(psub[2])
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        net_id, net_name = _parse_net_ref(psub, net_name_to_id, net_id_to_name)
                        if net_id == 0 and not net_name:
                            net_key = ""
                        else:
                            net_key = net_name or (str(net_id) if net_id is not None else "")
                if net_key:
                    # KiCad rotation is clockwise-positive; transform to world coords
                    abs_x = fp_x + rel_x * cos_t + rel_y * sin_t
                    abs_y = fp_y - rel_x * sin_t + rel_y * cos_t
                    pads_by_net[net_key].append((ref, str(pad_num), abs_x, abs_y))

        # Build track endpoint set keyed by (net_key, rounded_x, rounded_y)
        # so track endpoints from one net cannot falsely mark another net connected.
        track_endpoints: set = set()
        _TOLERANCE = 0.01  # mm

        def _rounded(v: float) -> int:
            return round(v / _TOLERANCE)

        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            key = _sym(item[0])
            if key in ("segment", "via"):
                # Read the net reference for this segment/via.
                seg_net_key = ""
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "net":
                        seg_net_id, seg_net_name = _parse_net_ref(sub, net_name_to_id, net_id_to_name)
                        if seg_net_id == 0 and not seg_net_name:
                            seg_net_key = ""
                        else:
                            seg_net_key = seg_net_name or (
                                str(seg_net_id) if seg_net_id is not None else ""
                            )
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) in ("start", "end", "at"):
                        try:
                            track_endpoints.add(
                                (seg_net_key, _rounded(float(sub[1])), _rounded(float(sub[2])))
                            )
                        except (ValueError, TypeError):
                            pass

        # For each net with ≥2 pads, report ALL pairs where neither pad
        # has a track endpoint at its position (simple heuristic)
        unconnected = []
        for net_name, pad_list in sorted(pads_by_net.items()):
            if len(pad_list) < 2:
                continue
            connected_indices = {
                i
                for i, (_, _, px, py) in enumerate(pad_list)
                if (net_name, _rounded(px), _rounded(py)) in track_endpoints
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

    @mcp.tool()
    async def score_placement(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> Dict[str, Any]:
        """Score the current PCB component placement quality.

        Computes three metrics from the existing pad positions — no routing
        required.  All metrics are lower-is-better.

        Metrics:
          - **hpwl_mm**: Total Half-Perimeter Wirelength.  Sum of per-net
            bounding-box half-perimeters across all nets.  Estimates the
            minimum copper length needed to route the board.
          - **congestion**: Peak component density in a 5 mm grid.
            ``peak_density`` is the number of components in the most crowded
            cell; ``hotspot_x/y`` locates that cell in board coordinates.
          - **decap_proximity_mm**: Mean distance between each decoupling
            capacitor and the nearest power-pad on an IC that shares its net.
            ``null`` when no decoupling capacitors are detected.
          - **worst_contributors**: Top-5 footprints whose connections
            contribute most to HPWL — the best candidates to reposition first.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.

        Returns:
            dict with hpwl_mm, congestion, decap_proximity_mm,
            worst_contributors.
        """
        data = load_pcb(pcb_path)

        # --- HPWL ----------------------------------------------------------------
        hpwl = _compute_hpwl(data)

        # Per-net HPWL contributions (for worst_contributors)
        net_pads: Dict[str, List[Tuple[str, float, float]]] = defaultdict(list)
        fp_position: Dict[str, Tuple[float, float]] = {}
        fp_pad_count: Dict[str, int] = defaultdict(int)

        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or ""
            x, y, _ = get_fp_at(item)
            fp_position[ref] = (x, y)
            for pad in _get_fp_pads_world(item):
                if pad["net"]:
                    net_pads[pad["net"]].append((ref, pad["x"], pad["y"]))
                    fp_pad_count[ref] += 1

        # Per-net HPWL
        net_hpwl: Dict[str, float] = {}
        for net_name, pads in net_pads.items():
            if len(pads) < 2:
                continue
            xs = [p[1] for p in pads]
            ys = [p[2] for p in pads]
            net_hpwl[net_name] = (max(xs) - min(xs)) + (max(ys) - min(ys))

        # Per-footprint displacement: distance from component to its net centroids
        fp_displacement: Dict[str, float] = {}
        for ref, (fx, fy) in fp_position.items():
            connected_nets = [n for n, pads in net_pads.items() if any(p[0] == ref for p in pads)]
            if not connected_nets:
                continue
            total_dist = 0.0
            for net_name in connected_nets:
                pads = net_pads[net_name]
                cx = sum(p[1] for p in pads) / len(pads)
                cy = sum(p[2] for p in pads) / len(pads)
                total_dist += math.hypot(fx - cx, fy - cy)
            fp_displacement[ref] = total_dist / len(connected_nets)

        worst = sorted(fp_displacement.items(), key=lambda kv: kv[1], reverse=True)[:5]
        worst_contributors = [
            {"reference": ref, "avg_displacement_mm": round(dist, 2)}
            for ref, dist in worst
        ]

        # --- Congestion grid (5 mm cells) ----------------------------------------
        GRID = 5.0
        bounds = _get_board_bounds_or_fallback(data)
        cell_counts: Dict[Tuple[int, int], int] = defaultdict(int)

        for fp_bbox in _get_all_footprint_bboxes(data):
            cx = (fp_bbox["min_x"] + fp_bbox["max_x"]) / 2
            cy = (fp_bbox["min_y"] + fp_bbox["max_y"]) / 2
            cell = (
                int((cx - bounds["min_x"]) / GRID),
                int((cy - bounds["min_y"]) / GRID),
            )
            cell_counts[cell] += 1

        if cell_counts:
            peak_cell = max(cell_counts, key=lambda k: cell_counts[k])
            peak_density = cell_counts[peak_cell]
            hotspot_x = round(bounds["min_x"] + (peak_cell[0] + 0.5) * GRID, 2)
            hotspot_y = round(bounds["min_y"] + (peak_cell[1] + 0.5) * GRID, 2)
        else:
            peak_density, hotspot_x, hotspot_y = 0, 0.0, 0.0

        # --- Decap proximity -----------------------------------------------------
        _POWER_NET_RE = re.compile(
            r"VCC|VDD|VEE|VSS|VBAT|3V3|3\.3V|5V|12V|\bPWR\b|AVCC|DVCC", re.IGNORECASE
        )
        # Ground-return nets connect to a copper plane, so their physical
        # proximity to IC pads is irrelevant for decoupling effectiveness.
        # VSS and VEE match _POWER_NET_RE but must be excluded when recording
        # decap supply pads to avoid measuring GND-plane distance instead of
        # supply-trace distance.  The pad ordering in the S-expression is not
        # guaranteed, so a ground-pad-first footprint would silently record the
        # wrong pad if we relied on _POWER_NET_RE alone.
        _GROUND_NET_RE = re.compile(r"VSS|VEE|GND|AGND|DGND|PGND", re.IGNORECASE)

        # Collect IC power pads: U/IC prefix, on a power net
        ic_power_pads: List[Tuple[str, str, float, float]] = []  # (ref, net, x, y)
        decap_positions: List[Tuple[str, float, float]] = []     # (net, x, y)

        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or ""
            m = re.match(r"[A-Za-z]+", ref)
            prefix = (m.group(0).upper() if m else "")
            pads = _get_fp_pads_world(item)
            pad_count = len(pads)

            if prefix in ("U", "IC") or pad_count > 8:
                for pad in pads:
                    if pad["net"] and _POWER_NET_RE.search(pad["net"]):
                        ic_power_pads.append((ref, pad["net"], pad["x"], pad["y"]))
            elif prefix == "C" and pad_count <= 2:
                # Record the supply-rail pad of the decoupling cap (not the GND
                # return pad — that connects to a copper plane and its proximity
                # to IC GND pins is irrelevant).  break after the first supply
                # pad because a decap typically has exactly one supply net.
                for pad in pads:
                    if (pad["net"]
                            and _POWER_NET_RE.search(pad["net"])
                            and not _GROUND_NET_RE.search(pad["net"])):
                        decap_positions.append((pad["net"], pad["x"], pad["y"]))
                        break

        decap_proximity_mm: Optional[float] = None
        if decap_positions and ic_power_pads:
            distances = []
            for cap_net, cap_x, cap_y in decap_positions:
                same_net_pads = [(px, py) for _, pnet, px, py in ic_power_pads if pnet == cap_net]
                if same_net_pads:
                    min_dist = min(math.hypot(cap_x - px, cap_y - py) for px, py in same_net_pads)
                    distances.append(min_dist)
            if distances:
                decap_proximity_mm = round(sum(distances) / len(distances), 2)

        return {
            "hpwl_mm": round(hpwl, 2),
            "congestion": {
                "peak_density": peak_density,
                "hotspot_x": hotspot_x,
                "hotspot_y": hotspot_y,
                "grid_size_mm": GRID,
            },
            "decap_proximity_mm": decap_proximity_mm,
            "worst_contributors": worst_contributors,
        }

    @mcp.tool()
    async def suggest_placement_order(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> Dict[str, Any]:
        """Return footprints sorted by recommended placement order.

        Classifies each footprint into one of four priority tiers and returns
        them sorted tier-first (anchors first, free passives last).  Place
        higher-tier components before lower-tier ones for best results with
        the push-and-shove and group placement tools.

        Tier definitions:
          1 ``anchor``    — connectors, mounting holes, test points.
          2 ``semi-fixed``— ICs, transistors, voltage regulators.
          3 ``flexible``  — crystals, relays, larger passives.
          4 ``free``      — resistors, small capacitors, inductors, diodes.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.

        Returns:
            dict with ``ordered`` (list of footprints sorted by tier then
            reference) and ``tier_counts`` (count per tier).
        """
        data = load_pcb(pcb_path)
        ordered = []

        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or ""
            value = get_fp_property(item, "Value") or ""
            x, y, _ = get_fp_at(item)
            layer = get_fp_layer(item) or ""

            # count pads
            pad_count = sum(
                1 for sub in item
                if isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"
            )
            tier = _classify_footprint(ref, pad_count, value)
            ordered.append({
                "reference": ref,
                "value": value,
                "x": x,
                "y": y,
                "layer": layer,
                "pad_count": pad_count,
                "tier": tier,
                "tier_name": _TIER_NAMES.get(tier, "unknown"),
            })

        ordered.sort(key=lambda fp: (fp["tier"], fp["reference"]))

        tier_counts: Dict[str, int] = defaultdict(int)
        for fp in ordered:
            tier_counts[fp["tier_name"]] += 1

        return {
            "ordered": ordered,
            "tier_counts": dict(tier_counts),
        }
