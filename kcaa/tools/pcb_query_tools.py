"""
PCB board read / query tools for KiCad MCP server.

Provides read-only tools to inspect a .kicad_pcb file: board metadata,
footprint list, individual footprint detail, footprint/board bounding
boxes, net list, and ratsnest (unconnected pad pairs).
"""

from collections import defaultdict
import contextlib
import logging
import math
import re
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.tools.pcb_placement_helpers import (
    _TIER_NAMES,
    _classify_footprint,
    _compute_hpwl,
    _get_all_footprint_bboxes,
    _get_board_bounds_or_fallback,
    _get_fp_pads_world,
)
from kcaa.utils.pcb_board_utils import get_fp_courtyard_bbox, get_fp_edge_cuts_items
from kcaa.utils.pcb_footprint_utils import (
    _sym,
    find_footprint,
    get_fp_at,
    get_fp_layer,
    get_fp_property,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

log = logging.getLogger(__name__)


def _collect_top_level_nets(data: list[Any]) -> tuple[dict[int, str], dict[str, int]]:
    """Return board net lookup tables from top-level ``(net ...)`` entries.

    Supports both KiCad 8 format ``(net <id> "<name>")`` and KiCad 10 format
    ``(net "<name>")``. For KiCad 10, net IDs are assigned sequentially starting
    from 1 (ID 0 is reserved for unconnected).
    """
    net_id_to_name: dict[int, str] = {}
    net_name_to_id: dict[str, int] = {}
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
    net_name_to_id: dict[str, int],
    net_id_to_name: dict[int, str],
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


def _net_sort_key(net: dict[str, Any]) -> tuple[int, Any, str]:
    """Sort nets by known numeric id first, then by name."""
    net_id = net.get("net_id")
    name = str(net.get("name", ""))
    return (0, net_id, name) if isinstance(net_id, int) else (1, name.lower(), name)


def register_pcb_query_tools(mcp: FastMCP) -> None:
    """Register PCB board read/query tools with the MCP server."""

    @mcp.tool()
    async def get_board_info(pcb_path: str, ctx: Context | None) -> dict[str, Any]:
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
        layers: list[dict] = []
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
                        layers.append(
                            {
                                "id": int(sub[0]) if isinstance(sub[0], int) else sub[0],
                                "name": sub[1] if isinstance(sub[1], str) else _sym(sub[1]),
                                "type": sub[2] if isinstance(sub[2], str) else _sym(sub[2]),
                            }
                        )
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
    async def list_footprints(pcb_path: str, ctx: Context | None) -> dict[str, Any]:
        """List all footprints placed on a KiCad PCB board.

        PCB coordinate convention (used by every PCB tool): millimetres,
        +X right, **+Y down** (KiCad PCB front-view coords), and rotation
        in **degrees, CCW-positive on screen** (0=right, 90=up). The
        ``x``/``y`` here are the
        footprint anchor in **board world coordinates**.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.

        Returns:
            dict with footprints: list of {reference, value, x, y (mm,
            world), rotation (deg, CCW+), layer (e.g. "F.Cu"/"B.Cu")},
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
            footprints.append(
                {
                    "reference": ref,
                    "value": value,
                    "x": x,
                    "y": y,
                    "rotation": rot,
                    "layer": layer,
                }
            )

        return {"footprints": footprints, "count": len(footprints)}

    @mcp.tool()
    async def get_footprint(
        pcb_path: str,
        reference: str,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific footprint on the board.

        Coordinates are mm, +Y down; rotation is CCW-positive on screen
        (KiCad PCB convention: 0=right, 90=up). The footprint's
        ``x``/``y``/``rotation``
        are in **board world coordinates**, but each pad's ``local_x``/
        ``local_y`` are in **footprint-local coordinates** (relative to
        the footprint anchor, before applying its rotation). To get pad
        positions in world coordinates, transform with the footprint's
        rotation:

            world_x = fp.x + local_x * cos(θ) + local_y * sin(θ)
            world_y = fp.y - local_x * sin(θ) + local_y * cos(θ)
            (θ in radians; matches KiCad's CCW-positive-on-screen convention)

        Or use ``get_ratsnest`` which returns world pad coordinates directly.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            reference: Footprint reference designator, e.g. ``"R1"``.
            ctx: MCP context for progress reporting.

        Returns:
            dict with reference, value, x/y/rotation (world, mm/deg CCW+),
            layer, properties (dict of all property name→value), pads
            (list of {number, type, shape, local_x, local_y,
             local_w, local_h, world_w, world_h, net_name}),
             edge_cuts (list of fp_line/fp_rect/fp_arc/fp_circle/fp_curve
             items on the footprint's Edge.Cuts layer, in footprint-local
             mm; transform to world coordinates the same way as pads).
             ``world_w``/``world_h`` account for pad rotation (KiCad 10
             stores pad rotation as absolute board-space angle in the same
             CCW convention).
        """
        data = load_pcb(pcb_path)
        try:
            fp = find_footprint(data, reference)
        except KeyError as exc:
            return {"error": str(exc)}

        x, y, rot = get_fp_at(fp)
        layer = get_fp_layer(fp) or ""

        # Collect all properties
        props: dict[str, str] = {}
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
            pad_rot = 0.0
            pad_w, pad_h = 0.0, 0.0
            net_name = ""
            for psub in sub:
                if isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "at":
                    pad_x, pad_y = float(psub[1]), float(psub[2])
                    pad_rot = float(psub[3]) if len(psub) > 3 else 0.0
                elif isinstance(psub, list) and len(psub) >= 3 and _sym(psub[0]) == "size":
                    pad_w, pad_h = float(psub[1]), float(psub[2])
                elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                    _, net_name = _parse_net_ref(psub, {}, {})
            # World-oriented size.
            # Pad rotation in KiCad 10 is stored as absolute board-space
            # (same CCW convention as the footprint), so we use it directly
            # without adding fp rotation.
            if abs(pad_rot % 180.0 - 90.0) < 0.1:
                wworld, hworld = pad_h, pad_w
            else:
                wworld, hworld = pad_w, pad_h
            pads.append(
                {
                    "number": str(pad_num),
                    "type": str(pad_type),
                    "shape": str(pad_shape),
                    "local_x": pad_x,
                    "local_y": pad_y,
                    "local_w": pad_w,
                    "local_h": pad_h,
                    "world_w": wworld,
                    "world_h": hworld,
                    "net_name": net_name,
                }
            )

        return {
            "reference": reference,
            "value": props.get("Value", ""),
            "x": x,
            "y": y,
            "rotation": rot,
            "layer": layer,
            "properties": props,
            "pads": pads,
            "edge_cuts": get_fp_edge_cuts_items(fp),
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
        coordinates by applying the footprint's position and rotation (CCW-positive
        on screen).  If the footprint has no courtyard items the
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
    async def list_nets(
        pcb_path: str,
        ctx: Context | None,
        classify: bool = False,
    ) -> dict[str, Any]:
        """List all nets in a KiCad PCB board.

        When *classify* is ``True``, each net also includes its
        **netclass** (resolved from the matching ``.kicad_pro`` via
        ``netclass_patterns``) and **type** (``"power"`` /
        ``"ground"`` / ``"signal"``).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context for progress reporting.
            classify: When ``True``, also resolve netclass and type.
                Requires a ``.kicad_pro`` next to the ``.kicad_pcb``.
                Default ``False`` for efficiency.

        Returns:
            dict with nets: list of {net_id, name, pad_count,
             netclass (str or None), type (str or None)}.
        """
        data = load_pcb(pcb_path)

        net_id_to_name, net_name_to_id = _collect_top_level_nets(data)
        nets_by_name: dict[str, dict[str, Any]] = {}
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

        # Optional: resolve netclass & type from .kicad_pro
        if classify:
            _resolve_net_data(nets_by_name, pcb_path)

        nets = sorted(nets_by_name.values(), key=_net_sort_key)

        return {"nets": nets, "count": len(nets)}

    @mcp.tool()
    async def get_ratsnest(
        pcb_path: str,
        ctx: Context | None,
        get_connected_pads: bool = False,
    ) -> dict[str, Any]:
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
            get_connected_pads: When ``True``, also return ``connected_pads``
                — pads that already have a track or via endpoint at their
                centre.  Default ``False`` for efficiency.

        Returns:
            dict with:
                unconnected: list of {net, from: {ref, pad, x, y}, to: {ref, pad, x, y}}
                    where x/y are world mm.
                connected_pads: list of {net, ref, pad, x, y} (only when
                    *get_connected_pads* is ``True``).
                unconnected_count: number of unconnected pairs
                connected_count: number of connected pads (0 when
                    *get_connected_pads* is ``False``).
                fully_routed: True if no unconnected pairs found
        """
        data = load_pcb(pcb_path)

        net_id_to_name, net_name_to_id = _collect_top_level_nets(data)

        # Collect all pads grouped by net key with correct world coordinates.
        # (apply footprint rotation using KiCad's CCW-positive convention)
        import math

        pads_by_net: dict[str, list[tuple]] = defaultdict(list)
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
                        with contextlib.suppress(ValueError, TypeError):
                            rel_x, rel_y = float(psub[1]), float(psub[2])
                    elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        net_id, net_name = _parse_net_ref(psub, net_name_to_id, net_id_to_name)
                        if net_id == 0 and not net_name:
                            net_key = ""
                        else:
                            net_key = net_name or (str(net_id) if net_id is not None else "")
                if net_key:
                    # KiCad rotation is CCW-positive on screen; transform to world coords
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
                        seg_net_id, seg_net_name = _parse_net_ref(
                            sub, net_name_to_id, net_id_to_name
                        )
                        if seg_net_id == 0 and not seg_net_name:
                            seg_net_key = ""
                        else:
                            seg_net_key = seg_net_name or (
                                str(seg_net_id) if seg_net_id is not None else ""
                            )
                for sub in item:
                    if (
                        isinstance(sub, list)
                        and len(sub) >= 3
                        and _sym(sub[0]) in ("start", "end", "at")
                    ):
                        with contextlib.suppress(ValueError, TypeError):
                            track_endpoints.add(
                                (seg_net_key, _rounded(float(sub[1])), _rounded(float(sub[2])))
                            )

        # ── Zone coverage: pads inside a filled copper pour on the same
        #     net+layer are considered connected (zone acts as a plane). ──
        try:
            from shapely.geometry import Point as ShapelyPoint
            from shapely.geometry import Polygon as ShapelyPolygon
        except ImportError:
            ShapelyPoint = ShapelyPolygon = None  # type: ignore[assignment]

        if ShapelyPoint is not None:
            for item in data:
                if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "zone"):
                    continue
                zone_net = ""
                polygon_pts: list[tuple[float, float]] = []
                is_keepout = False
                for sub in item:
                    if not isinstance(sub, list) or len(sub) < 2:
                        continue
                    zk = _sym(sub[0])
                    if zk == "net":
                        if len(sub) >= 3:
                            try:
                                int(sub[1])
                                zone_net = sub[2] if isinstance(sub[2], str) else _sym(sub[2])
                            except (TypeError, ValueError):
                                zone_net = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                        elif len(sub) >= 2:
                            zone_net = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
                    elif zk == "keepout":
                        is_keepout = True
                    elif zk == "polygon":
                        # First polygon only — the outline
                        for psub in sub[1:]:
                            if isinstance(psub, list) and _sym(psub[0]) == "pts":
                                pts = []
                                for pt in psub[1:]:
                                    if (
                                        isinstance(pt, list)
                                        and len(pt) >= 3
                                        and _sym(pt[0]) == "xy"
                                    ):
                                        pts.append((float(pt[1]), float(pt[2])))
                                polygon_pts = pts
                                break
                if is_keepout or not zone_net or len(polygon_pts) < 3:
                    continue
                try:
                    zone_poly = ShapelyPolygon(polygon_pts)
                except (ValueError, TypeError):
                    continue
                # Mark any pad on the same net that falls inside the zone
                # polygon as connected (copper pour acts as a plane).
                for pad_idx, (ref, pad_num, px, py) in enumerate(pads_by_net.get(zone_net, [])):
                    pt = ShapelyPoint(px, py)
                    if zone_poly.covers(pt):
                        track_endpoints.add((zone_net, _rounded(px), _rounded(py)))

        # For each net with ≥2 pads, report ALL pairs where neither pad
        # has a track endpoint at its position (simple heuristic)
        unconnected = []
        connected_pads: list[dict[str, Any]] = []
        for net_name, pad_list in sorted(pads_by_net.items()):
            if len(pad_list) < 2:
                continue
            connected_indices = {
                i
                for i, (_, _, px, py) in enumerate(pad_list)
                if (net_name, _rounded(px), _rounded(py)) in track_endpoints
            }
            if get_connected_pads:
                for i in connected_indices:
                    ref, pad_num, px, py = pad_list[i]
                    connected_pads.append(
                        {
                            "net": net_name,
                            "ref": ref,
                            "pad": pad_num,
                            "x": px,
                            "y": py,
                        }
                    )
            disconnected = [p for i, p in enumerate(pad_list) if i not in connected_indices]
            # Report all disconnected pairs (not just first)
            for i in range(len(disconnected)):
                for j in range(i + 1, len(disconnected)):
                    a, b = disconnected[i], disconnected[j]
                    unconnected.append(
                        {
                            "net": net_name,
                            "from": {"ref": a[0], "pad": a[1], "x": a[2], "y": a[3]},
                            "to": {"ref": b[0], "pad": b[1], "x": b[2], "y": b[3]},
                        }
                    )

        if get_connected_pads:
            return {
                "unconnected": unconnected,
                "connected_pads": connected_pads,
                "unconnected_count": len(unconnected),
                "connected_count": len(connected_pads),
                "fully_routed": len(unconnected) == 0,
            }
        return {
            "unconnected": unconnected,
            "unconnected_count": len(unconnected),
            "fully_routed": len(unconnected) == 0,
        }

    @mcp.tool()
    async def score_placement(
        pcb_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
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
        net_pads: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
        fp_position: dict[str, tuple[float, float]] = {}
        fp_pad_count: dict[str, int] = defaultdict(int)

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
        net_hpwl: dict[str, float] = {}
        for net_name, pads in net_pads.items():
            if len(pads) < 2:
                continue
            xs = [p[1] for p in pads]
            ys = [p[2] for p in pads]
            net_hpwl[net_name] = (max(xs) - min(xs)) + (max(ys) - min(ys))

        # Per-footprint displacement: distance from component to its net centroids
        fp_displacement: dict[str, float] = {}
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
            {"reference": ref, "avg_displacement_mm": round(dist, 2)} for ref, dist in worst
        ]

        # --- Congestion grid (5 mm cells) ----------------------------------------
        GRID = 5.0
        bounds = _get_board_bounds_or_fallback(data)
        cell_counts: dict[tuple[int, int], int] = defaultdict(int)

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
        ic_power_pads: list[tuple[str, str, float, float]] = []  # (ref, net, x, y)
        decap_positions: list[tuple[str, float, float]] = []  # (net, x, y)

        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "footprint"):
                continue
            ref = get_fp_property(item, "Reference") or ""
            m = re.match(r"[A-Za-z]+", ref)
            prefix = m.group(0).upper() if m else ""
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
                    if (
                        pad["net"]
                        and _POWER_NET_RE.search(pad["net"])
                        and not _GROUND_NET_RE.search(pad["net"])
                    ):
                        decap_positions.append((pad["net"], pad["x"], pad["y"]))
                        break

        decap_proximity_mm: float | None = None
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
    ) -> dict[str, Any]:
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
                1
                for sub in item
                if isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "pad"
            )
            tier = _classify_footprint(ref, pad_count, value)
            ordered.append(
                {
                    "reference": ref,
                    "value": value,
                    "x": x,
                    "y": y,
                    "layer": layer,
                    "pad_count": pad_count,
                    "tier": tier,
                    "tier_name": _TIER_NAMES.get(tier, "unknown"),
                }
            )

        ordered.sort(key=lambda fp: (fp["tier"], fp["reference"]))

        tier_counts: dict[str, int] = defaultdict(int)
        for fp in ordered:
            tier_counts[fp["tier_name"]] += 1

        return {
            "ordered": ordered,
            "tier_counts": dict(tier_counts),
        }

    @mcp.tool()
    async def list_tracks(
        pcb_path: str,
        ctx: Context | None = None,
        net: str | None = None,
        layer: str | None = None,
    ) -> dict[str, Any]:
        """List all track segments on the PCB, grouped by trace connectivity.

        Returns segments grouped into **traces** — two segments belong to
        the same trace when they share an endpoint (within 0.01 mm).
        Segments within each trace are ordered end-to-end as polylines.
        A trace with a T-junction branches into multiple polylines.

        When ``net`` and/or ``layer`` are provided, only matching segments
        are considered (and traces that cross layers or nets don't merge).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).
            net: Optional net name filter (e.g. ``"VCC"``).
            layer: Optional copper layer filter (e.g. ``"F.Cu"``).

        Returns:
            dict with:
                traces: list of trace groups.  Each trace has ``polylines``
                    (list of point-lists) and shared metadata
                    ``width``, ``layer``, ``net``.
                segment_count: total number of segments.
                trace_count: number of connected trace groups.
        """
        data = load_pcb(pcb_path)
        tol = 0.01  # mm — same as delete tools

        # ── Collect all raw segment entries ──────────────────────────
        raw_segs: list[dict[str, Any]] = []
        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "segment"):
                continue
            start_node = _find_sub_pq(item, "start")
            end_node = _find_sub_pq(item, "end")
            width_node = _find_sub_pq(item, "width")
            layer_node = _find_sub_pq(item, "layer")
            net_node = _find_sub_pq(item, "net")
            if start_node is None or end_node is None:
                continue

            sx = (
                float(start_node[1])
                if not isinstance(start_node[1], str)
                else float(str(start_node[1]))
            )
            sy = (
                float(start_node[2])
                if not isinstance(start_node[2], str)
                else float(str(start_node[2]))
            )
            ex = float(end_node[1]) if not isinstance(end_node[1], str) else float(str(end_node[1]))
            ey = float(end_node[2]) if not isinstance(end_node[2], str) else float(str(end_node[2]))
            sw = float(width_node[1]) if width_node and len(width_node) >= 2 else 0.0
            item_layer = str(layer_node[1]) if layer_node and len(layer_node) >= 2 else ""
            item_net = _get_net_name_pq(net_node) if net_node else ""

            if net is not None and item_net != net:
                continue
            if layer is not None and item_layer != layer:
                continue

            raw_segs.append(
                {
                    "x1": sx,
                    "y1": sy,
                    "x2": ex,
                    "y2": ey,
                    "width": sw,
                    "layer": item_layer,
                    "net": item_net,
                }
            )

        if not raw_segs:
            return {"traces": [], "segment_count": 0, "trace_count": 0}

        # ── Collect pad centre positions (world coords) ──────────────
        # Segments that meet at a pad centre are NOT connected (the
        # connection goes through the pad, not directly segment-to-segment).
        # Also build a reverse lookup: (rounded_x, rounded_y) → pad info.
        pad_centres: set[tuple[float, float]] = set()
        pad_at: dict[tuple[float, float], list[dict[str, Any]]] = {}
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
                        with contextlib.suppress(ValueError, TypeError):
                            rel_x, rel_y = float(psub[1]), float(psub[2])
                    elif isinstance(psub, list) and len(psub) >= 2 and _sym(psub[0]) == "net":
                        net_id, net_name = _parse_net_ref(psub, {}, {})
                        net_key = net_name if net_name else ""
                if not net_key:
                    continue
                abs_x = fp_x + rel_x * cos_t + rel_y * sin_t
                abs_y = fp_y - rel_x * sin_t + rel_y * cos_t
                rkey = (round(abs_x, 2), round(abs_y, 2))
                pad_centres.add(rkey)
                pad_at.setdefault(rkey, []).append(
                    {
                        "ref": ref,
                        "pad": str(pad_num),
                        "net": net_key,
                    }
                )

        # ── Build adjacency ──────────────────────────────────────────
        # Each segment has two endpoints (a, b).  Two segments share an
        # endpoint if any endpoint pair is within tol — *unless* that
        # point is a pad centre (connection goes through the pad, not
        # directly segment-to-segment).
        n = len(raw_segs)
        adj: list[list[int]] = [[] for _ in range(n)]

        def _pt(p: tuple[float, float], q: tuple[float, float]) -> bool:
            return abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol

        seg_pts = [((s["x1"], s["y1"]), (s["x2"], s["y2"])) for s in raw_segs]

        for i in range(n):
            a1, b1 = seg_pts[i]
            for j in range(i + 1, n):
                # Only connect if same layer + net (don't bridge different nets)
                if raw_segs[i]["layer"] != raw_segs[j]["layer"]:
                    continue
                if raw_segs[i]["net"] != raw_segs[j]["net"]:
                    continue
                # Check if any pair of endpoints match
                match_pt = None
                if _pt(a1, seg_pts[j][0]) or _pt(a1, seg_pts[j][1]):
                    match_pt = a1
                elif _pt(b1, seg_pts[j][0]) or _pt(b1, seg_pts[j][1]):
                    match_pt = b1
                if match_pt is None:
                    continue
                # Don't connect across a pad centre
                rkey = (round(match_pt[0], 2), round(match_pt[1], 2))
                if rkey in pad_centres:
                    continue
                adj[i].append(j)
                adj[j].append(i)

        # ── Find connected components (traces) ────────────────────────
        visited = [False] * n
        traces: list[list[int]] = []

        for i in range(n):
            if visited[i]:
                continue
            comp: list[int] = []
            stack = [i]
            visited[i] = True
            while stack:
                v = stack.pop()
                comp.append(v)
                for u in adj[v]:
                    if not visited[u]:
                        visited[u] = True
                        stack.append(u)
            traces.append(comp)

        # ── Order each trace into polylines ───────────────────────────
        result: list[dict[str, Any]] = []
        for comp in traces:
            # Build dense degree info for endpoints within this component
            # endpoint → list of segment indices
            ep_map: dict[tuple[float, float], list[int]] = {}
            for idx in comp:
                s = raw_segs[idx]
                p1 = (s["x1"], s["y1"])
                p2 = (s["x2"], s["y2"])
                ep_map.setdefault(p1, []).append(idx)
                ep_map.setdefault(p2, []).append(idx)

            # Deduplicate endpoints that are within tol of each other
            # (simple: round to 0.01mm grid for binning)
            bins: dict[tuple[float, float], list[tuple[float, float]]] = {}
            for ep in ep_map:
                key = (round(ep[0], 2), round(ep[1], 2))
                bins.setdefault(key, []).append(ep)
            merged_map: dict[tuple[float, float], list[int]] = {}
            for key, pts in bins.items():
                merged_ep = pts[0]
                merged_list: list[int] = []
                for p in pts:
                    merged_list.extend(ep_map[p])
                merged_map[merged_ep] = list(set(merged_list))

            # Walk polylines — start from leaf endpoints (degree 1)
            remaining = set(comp)
            polylines: list[list[tuple[float, float]]] = []

            while remaining:
                # Pick a start segment
                start_idx = next(iter(remaining))
                s = raw_segs[start_idx]
                # Prefer starting from a leaf endpoint (degree 1)
                ep_a = (round(s["x1"], 2), round(s["y1"], 2))
                deg_a = len(merged_map.get(ep_a, []))

                if deg_a == 1:
                    cur_pt = (s["x1"], s["y1"])
                    next_pt = (s["x2"], s["y2"])
                else:
                    cur_pt = (s["x2"], s["y2"])
                    next_pt = (s["x1"], s["y1"])

                poly = [cur_pt, next_pt]
                remaining.remove(start_idx)

                # Walk forward from next_pt
                changed = True
                while changed:
                    changed = False
                    rkey = (round(next_pt[0], 2), round(next_pt[1], 2))
                    neighbors = [i for i in merged_map.get(rkey, []) if i in remaining]
                    if len(neighbors) == 1:
                        nxt = neighbors[0]
                        s2 = raw_segs[nxt]
                        # Determine other endpoint
                        if _pt((s2["x1"], s2["y1"]), next_pt):
                            next_pt = (s2["x2"], s2["y2"])
                        else:
                            next_pt = (s2["x1"], s2["y1"])
                        poly.append(next_pt)
                        remaining.remove(nxt)
                        changed = True

                # Try walking backward from cur_pt
                changed = True
                while changed:
                    changed = False
                    ckey = (round(cur_pt[0], 2), round(cur_pt[1], 2))
                    neighbors = [i for i in merged_map.get(ckey, []) if i in remaining]
                    if len(neighbors) == 1:
                        nxt = neighbors[0]
                        s2 = raw_segs[nxt]
                        if _pt((s2["x1"], s2["y1"]), cur_pt):
                            cur_pt = (s2["x2"], s2["y2"])
                        else:
                            cur_pt = (s2["x1"], s2["y1"])
                        poly.insert(0, cur_pt)
                        remaining.remove(nxt)
                        changed = True

                polylines.append(poly)

            polylines_out = []
            for poly in polylines:
                segs_in_poly = []
                for i in range(len(poly) - 1):
                    a, b = poly[i], poly[i + 1]
                    for s in raw_segs:
                        if (
                            abs(s["x1"] - a[0]) < tol
                            and abs(s["y1"] - a[1]) < tol
                            and abs(s["x2"] - b[0]) < tol
                            and abs(s["y2"] - b[1]) < tol
                        ):
                            segs_in_poly.append({"start": a, "end": b, "width": s["width"]})
                            break
                        elif (
                            abs(s["x1"] - b[0]) < tol
                            and abs(s["y1"] - b[1]) < tol
                            and abs(s["x2"] - a[0]) < tol
                            and abs(s["y2"] - a[1]) < tol
                        ):
                            segs_in_poly.append({"start": a, "end": b, "width": s["width"]})
                            break
                polylines_out.append(segs_in_poly)

            # Collect pads connected to this trace's endpoints
            trace_pads: list[dict[str, Any]] = []
            seen_pads: set[tuple[str, str]] = set()
            for poly in polylines:
                for pt in (poly[0], poly[-1]):
                    rkey = (round(pt[0], 2), round(pt[1], 2))
                    for p in pad_at.get(rkey, []):
                        key = (p["ref"], p["pad"])
                        if key not in seen_pads:
                            seen_pads.add(key)
                            trace_pads.append(p)

            result.append(
                {
                    "width": raw_segs[comp[0]]["width"],
                    "layer": raw_segs[comp[0]]["layer"],
                    "net": raw_segs[comp[0]]["net"],
                    "segment_count": len(comp),
                    "segments": [seg for poly in polylines_out for seg in poly],
                    "pads": trace_pads,
                }
            )

        return {
            "traces": result,
            "segment_count": n,
            "trace_count": len(result),
        }

    @mcp.tool()
    async def list_vias(
        pcb_path: str,
        ctx: Context | None = None,
        net: str | None = None,
    ) -> dict[str, Any]:
        """List all through-hole vias on the PCB, optionally filtered.

        Returns every ``(via ...)`` entry with its position, size,
        drill, layers, and net.  When ``net`` is provided, only
        vias on that net are returned.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ctx: MCP context (unused).
            net: Optional net name filter.

        Returns:
            dict with ``vias`` (list of via dicts) and ``count``.
        """
        data = load_pcb(pcb_path)

        vias: list[dict[str, Any]] = []
        for item in data:
            if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "via"):
                continue

            at_node = _find_sub_pq(item, "at")
            size_node = _find_sub_pq(item, "size")
            drill_node = _find_sub_pq(item, "drill")
            layers_node = _find_sub_pq(item, "layers")
            net_node = _find_sub_pq(item, "net")
            if at_node is None or len(at_node) < 3:
                continue

            vx = float(at_node[1]) if not isinstance(at_node[1], str) else float(str(at_node[1]))
            vy = float(at_node[2]) if not isinstance(at_node[2], str) else float(str(at_node[2]))
            vs = float(size_node[1]) if size_node and len(size_node) >= 2 else 0.0
            vd = float(drill_node[1]) if drill_node and len(drill_node) >= 2 else 0.0
            via_layers = [str(ln) for ln in layers_node[1:]] if layers_node else []
            item_net = _get_net_name_pq(net_node) if net_node else ""

            if net is not None and item_net != net:
                continue

            vias.append(
                {
                    "x": vx,
                    "y": vy,
                    "diameter": vs,
                    "drill": vd,
                    "layers": via_layers,
                    "net": item_net,
                }
            )

        return {"vias": vias, "count": len(vias)}


# ---------------------------------------------------------------------------
# Helpers — netclass / type
# ---------------------------------------------------------------------------


def _resolve_net_data(
    nets_by_name: dict[str, dict[str, Any]],
    pcb_path: str,
) -> None:
    """Augment each net in *nets_by_name* with ``netclass`` and ``type``.

    Reads the matching ``.kicad_pro`` JSON, resolves net→netclass via
    ``netclass_patterns``, and classifies each net as power/ground/signal.

    If the ``.kicad_pro`` cannot be found or parsed, the helper silently
    returns (the extra fields remain as ``None``).
    """
    import json
    import os
    import re

    # Derive .kicad_pro path
    base = os.path.splitext(os.path.basename(pcb_path))[0]
    d = os.path.dirname(pcb_path)
    pro_path: str | None = None
    for f in os.listdir(d):
        if f.startswith(base + ".") and re.match(r".+\.kicad_pro$", f):
            pro_path = os.path.join(d, f)
            break

    # Always set netclass/type (to None if .kicad_pro unavailable)
    for net_name in nets_by_name:
        nets_by_name[net_name].setdefault("netclass", None)
        nets_by_name[net_name].setdefault("type", None)

    if pro_path is None or not os.path.isfile(pro_path):
        return

    try:
        with open(pro_path, encoding="utf-8") as fh:
            pro_data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return

    ns = pro_data.get("net_settings", {}) if isinstance(pro_data, dict) else {}
    if not isinstance(ns, dict):
        return

    # Build net→netclass map (explicit table + pattern matching)
    net_to_class: dict[str, str | None] = {}
    for net_name in nets_by_name:
        net_to_class[net_name] = None

    # Explicit per-net assignments
    for n in ns.get("nets", []):
        if isinstance(n, dict):
            name = n.get("name")
            nc = n.get("netclass") or n.get("class")
            if isinstance(name, str) and isinstance(nc, str) and name in net_to_class:
                net_to_class[name] = nc

    # Pattern-based assignments (first match wins)
    import fnmatch

    for p in ns.get("netclass_patterns", []):
        if not isinstance(p, dict):
            continue
        pat = p.get("pattern")
        nc = p.get("netclass")
        if not isinstance(pat, str) or not isinstance(nc, str):
            continue
        for net_name in net_to_class:
            if net_to_class[net_name] is None and fnmatch.fnmatchcase(net_name, pat):
                net_to_class[net_name] = nc

    # Regex for signal integrity type
    _POWER_RE = re.compile(
        r"VCC|VDD|VEE|VBAT|3V3|3\.3V|5V|12V|\bPWR\b|AVCC|DVCC|VUSB",
        re.IGNORECASE,
    )
    _GROUND_RE = re.compile(r"VSS|VEE|GND|AGND|DGND|PGND|VSS", re.IGNORECASE)

    for net_name, entry in nets_by_name.items():
        entry["netclass"] = net_to_class.get(net_name)
        if _GROUND_RE.search(net_name):
            entry["type"] = "ground"
        elif _POWER_RE.search(net_name):
            entry["type"] = "power"
        else:
            entry["type"] = "signal"


# ---------------------------------------------------------------------------
# Helpers (module-level, shared by tools in this module)
# ---------------------------------------------------------------------------


def _find_sub_pq(items: list, name: str) -> list | None:
    """Find the first sub-list whose first Symbol matches *name*."""
    for sub in items:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == name:
            return sub
    return None


def _get_net_name_pq(net_node: list) -> str | None:
    """Extract net name from a (net ...) reference.

    Handles KiCad 8 ``(net <id> "<name>")`` and KiCad 10 ``(net "<name>")``.
    """
    if len(net_node) >= 3:
        raw = net_node[2]
    elif len(net_node) >= 2:
        raw = net_node[1]
    else:
        return None
    return raw if isinstance(raw, str) else str(raw)
