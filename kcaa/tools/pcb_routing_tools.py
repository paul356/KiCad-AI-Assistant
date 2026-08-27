"""
PCB routing tools for the KiCad MCP server.

Exposes the no-shove PNS router as an MCP tool that connects two pads with
a track on a single layer.  The tool writes the resulting segments and (if
present) vias back to the .kicad_pcb file, with the usual ``.bak`` backup.

This is the no-shove variant: if a route is blocked, the tool fails rather
than displacing existing tracks.  Use the placement / edit tools to clear
the path first, or call with a different layer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.router.path_postprocess import OutputSegment, OutputVia
from kcaa.router.router import (
    RouteFailure,
    RouteRequest,
    auto_route_pair,
    connect_with_via,
)
from kcaa.router.via_check import ProposedVia, check_vias
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb

log = logging.getLogger(__name__)


def register_pcb_routing_tools(mcp: FastMCP) -> None:
    """Register PCB routing tools with the MCP server."""

    @mcp.tool()
    async def pcb_route_pad_to_pad(
        pcb_path: str,
        ref_a: str,
        pad_a: str,
        ref_b: str,
        pad_b: str,
        net: str,
        ctx: Context | None,
        width: float | None = None,
        layer_hint: str | None = None,
        via_pairs: tuple[tuple[str, str], ...] | None = None,
        turn_penalty: float | None = None,
    ) -> dict[str, Any]:
        """Connect two pads with an obstacle-avoiding track, optionally across layers.

        Uses the no-shove PNS router: if the path is blocked by an existing
        track or footprint courtyard, the call fails rather than moving
        anything.  Run the placement tools first to clear the way, or call
        again with a different ``layer_hint``.

        PCB coordinates: mm, +X right, **+Y down**, rotation
        **clockwise-positive** (KiCad PCB convention).

        The track's width defaults to the net's netclass ``track_width`` from
        the matching ``.kicad_pro`` (or 0.25 mm if no project file is
        found).  Clearance is taken from the board's effective design rules
        (see :mod:`kcaa.utils.pcb_design_rules`).

        Layer selection is automatic: SMD pads use their fixed layer;
        thru-hole pads pick a shared copper layer, preferring
        ``layer_hint``.  When both pads are thru-hole and share a layer,
        the route stays on a single layer (no vias).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ref_a: Reference designator of the first footprint (e.g. ``"R1"``).
            pad_a: Pad number on ``ref_a`` (e.g. ``"1"``).
            ref_b: Reference designator of the second footprint.
            pad_b: Pad number on ``ref_b``.
            net: Net name to assign to the new segments.
            ctx: MCP context (unused).
            width: Override the netclass track width (mm).  ``None`` uses the
                DRC default for the net.
            layer_hint: Preferred copper layer for thru-hole pads.  When
                ``None`` (default) the router picks automatically.  Ignored
                for SMD pads whose layer is fixed by the pad itself.
            via_pairs: Optional tuple of ``(from_layer, to_layer)`` pairs
                the router is allowed to use as via transitions.  Defaults
                to ``(("F.Cu", "B.Cu"),)`` when the resolved layers differ;
                ignored otherwise.
            turn_penalty: Cost added when the path changes direction (mm).
                ``None`` uses the default (0.3 mm).  Set to 0 for pure
                shortest-path routing (more zigzag).

        Returns:
            dict with:
                segment_count: number of segments written.
                segments: list of dicts ``{x1, y1, x2, y2, width, layer, net}``.
                via_count: number of vias written (0 for single-layer).
                vias: list of dicts ``{x, y, diameter, drill, layers, net}``.
                layers_used: ordered list of layers touched by the path.
                start: ``(x, y)`` exit point of pad_a.
                end: ``(x, y)`` entry point of pad_b.
                backup_path: path to the ``.bak`` created before writing.
                pcb_path: echo of the input path.

            Or ``{"error": "<message>"}`` on failure.
        """
        if via_pairs is None:
            via_pairs = (("F.Cu", "B.Cu"),)
        req = RouteRequest(
            pcb_path=pcb_path,
            ref_a=ref_a,
            pad_a=pad_a,
            ref_b=ref_b,
            pad_b=pad_b,
            net=net,
            layer_hint=layer_hint,
            width=width,
            via_pairs=via_pairs or (),
            turn_penalty=turn_penalty if turn_penalty is not None else 0.3,
        )
        try:
            result = auto_route_pair(req)
        except RouteFailure as exc:
            return {"error": str(exc)}
        except (FileNotFoundError, ValueError) as exc:
            return {"error": f"Routing input error: {exc}"}

        # Load the PCB and append the new segments and vias.
        data = load_pcb(pcb_path)
        for seg in result.segments:
            data.append(_segment_to_sexp(seg))
        for via in result.vias:
            data.append(_via_to_sexp(via))
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "segment_count": len(result.segments),
            "segments": [
                {
                    "x1": s.x1,
                    "y1": s.y1,
                    "x2": s.x2,
                    "y2": s.y2,
                    "width": s.width,
                    "layer": s.layer,
                    "net": s.net,
                }
                for s in result.segments
            ],
            "via_count": len(result.vias),
            "vias": [
                {
                    "x": v.x,
                    "y": v.y,
                    "diameter": v.diameter,
                    "drill": v.drill,
                    "layers": [v.layers[0], v.layers[1]],
                    "net": v.net,
                }
                for v in result.vias
            ],
            "layers_used": list(result.layers_used),
            "start": list(result.start),
            "end": list(result.end),
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def pcb_add_vias(
        pcb_path: str,
        vias: list[dict[str, Any]],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Add one or more through-hole vias to the PCB in a single write.

        Each element of ``vias`` is a dict with the keys ``x``, ``y``,
        ``net`` plus optional ``diameter`` (default 0.8), ``drill``
        (default 0.4), ``layers`` (default ``("F.Cu", "B.Cu")``).  Pass a
        single-element list for a one-off via, or many for ground-plane
        stitching / fan-out.  All vias are written in one PCB rewrite so
        a single ``.bak`` covers the whole batch.

        Before writing, the tool checks each via against:

        * the matching ``.kicad_pro`` netclass rules — ``via_diameter``
          and ``via_drill`` must match the net's netclass (within
          1 micron); the project file must exist and the net must
          resolve to a class (or ``Default``).
        * the existing board geometry — the via's pad ring must not
          overlap any footprint courtyard, other-net track/via, or
          zone keepout, and must stay inside the board outline with
          the configured ``min_copper_edge_clearance``.

        Any violation rejects the whole batch; the file is left
        untouched.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            vias: List of via descriptor dicts (1 or more).
            ctx: MCP context (unused).

        Returns:
            dict with ``via_count``, ``vias`` (list of written via
            dicts), and ``backup_path``.  An empty list is a no-op
            (no write, no backup).  An ``{"error": "..."}`` return
            indicates the entire batch was rejected; the file is left
            untouched.
        """
        try:
            out_vias: list[OutputVia] = []
            for spec in vias:
                out_vias.append(
                    OutputVia(
                        x=float(spec["x"]),
                        y=float(spec["y"]),
                        diameter=float(spec.get("diameter", 0.8)),
                        drill=float(spec.get("drill", 0.4)),
                        layers=tuple(spec.get("layers", ("F.Cu", "B.Cu"))),
                        net=str(spec["net"]),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"Invalid via descriptor: {exc}"}
        if not out_vias:
            return {"via_count": 0, "vias": [], "backup_path": None, "pcb_path": pcb_path}

        # Pre-flight: check netclass rules and position.  Any violation
        # rejects the whole batch; the file is not modified.
        proposed = [
            ProposedVia(
                x=v.x,
                y=v.y,
                diameter=v.diameter,
                drill=v.drill,
                layers=v.layers,
                net=v.net,
            )
            for v in out_vias
        ]
        violations = check_vias(pcb_path, proposed)
        if violations:
            lines = [f"rejected {len(violations)} via violation(s):"]
            for vio in violations:
                idx = vio.index if vio.index >= 0 else "*"
                lines.append(f"  - via #{idx} [{vio.kind}] {vio.message}")
            return {
                "error": "\n".join(lines),
                "violations": [
                    {
                        "index": v.index,
                        "kind": v.kind,
                        "message": v.message,
                        **v.detail,
                    }
                    for v in violations
                ],
            }
        data = load_pcb(pcb_path)
        for via in out_vias:
            data.append(_via_to_sexp(via))
        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}
        return {
            "via_count": len(out_vias),
            "vias": [
                {
                    "x": v.x,
                    "y": v.y,
                    "diameter": v.diameter,
                    "drill": v.drill,
                    "layers": list(v.layers),
                    "net": v.net,
                }
                for v in out_vias
            ],
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def pcb_delete_tracks(
        pcb_path: str,
        segments: list[dict[str, Any]],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Delete specific track segments by their endpoint coordinates.

        Each element of ``segments`` is a dict with ``x1``, ``y1``, ``x2``,
        ``y2`` and optional ``layer``.  A matching ``(segment ...)`` entry is
        removed when both endpoints match within 0.01 mm and (if specified)
        the layer matches.

        A ``.bak`` backup is created before any modification.  An empty
        match list (``[]``) is a no-op — no backup, no write.  Returns the
        count of segments actually deleted (some may have already been
        removed by a previous call).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            segments: List of segment descriptors, each with
                ``x1``, ``y1``, ``x2``, ``y2`` and optional ``layer``.
            ctx: MCP context (unused).

        Returns:
            dict with ``deleted_count``, ``matched_count`` (how many of
            the input descriptors found a match), ``not_found`` (descriptors
            that did not match any segment), ``backup_path``, and
            ``pcb_path``.
        """
        if not segments:
            return {
                "deleted_count": 0,
                "matched_count": 0,
                "not_found": [],
                "backup_path": None,
                "pcb_path": pcb_path,
            }

        data = load_pcb(pcb_path)
        tol = 0.01  # mm

        # Collect existing (segment ...) items with their endpoint info.
        existing: list[tuple[list, float, float, float, float, str | None]] = []
        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            if not _is_sym(item[0], "segment"):
                continue
            start_node = _find_sub(item, "start")
            end_node = _find_sub(item, "end")
            layer_node = _find_sub(item, "layer")
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
            item_layer = str(layer_node[1]) if layer_node and len(layer_node) >= 2 else None
            existing.append((item, sx, sy, ex, ey, item_layer))

        to_remove: set[int] = set()
        not_found: list[dict[str, Any]] = []

        for desc in segments:
            try:
                dx1 = float(desc["x1"])
                dy1 = float(desc["y1"])
                dx2 = float(desc["x2"])
                dy2 = float(desc["y2"])
                d_layer = desc.get("layer")
            except (KeyError, TypeError, ValueError) as exc:
                not_found.append(
                    {
                        "x1": desc.get("x1"),
                        "y1": desc.get("y1"),
                        "x2": desc.get("x2"),
                        "y2": desc.get("y2"),
                        "error": str(exc),
                    }
                )
                continue

            matched = False
            for idx, (item, sx, sy, ex, ey, item_layer) in enumerate(existing):
                if idx in to_remove:
                    continue
                # Check endpoint match (either direction)
                forward = (
                    abs(sx - dx1) < tol
                    and abs(sy - dy1) < tol
                    and abs(ex - dx2) < tol
                    and abs(ey - dy2) < tol
                )
                backward = (
                    abs(sx - dx2) < tol
                    and abs(sy - dy2) < tol
                    and abs(ex - dx1) < tol
                    and abs(ey - dy1) < tol
                )
                if not forward and not backward:
                    continue
                # Optional layer filter
                if d_layer is not None and item_layer is not None and item_layer != d_layer:
                    continue
                to_remove.add(idx)
                matched = True
                break

            if not matched:
                not_found.append({"x1": dx1, "y1": dy1, "x2": dx2, "y2": dy2})

        if not to_remove:
            return {
                "deleted_count": 0,
                "matched_count": 0,
                "not_found": not_found,
                "backup_path": None,
                "pcb_path": pcb_path,
            }

        # Remove in reverse index order to preserve positions.
        for idx in sorted(to_remove, reverse=True):
            data.remove(existing[idx][0])

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "deleted_count": len(to_remove),
            "matched_count": len(segments) - len(not_found),
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }

    @mcp.tool()
    async def pcb_delete_vias(
        pcb_path: str,
        vias: list[dict[str, Any]],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Delete specific through-hole vias by their position.

        Each element of ``vias`` is a dict with ``x`` and ``y``.  A
        matching ``(via ...)`` entry is removed when its position matches
        within 0.01 mm.

        A ``.bak`` backup is created before any modification.  An empty
        list (``[]``) is a no-op — no backup, no write.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            vias: List of via position dicts, each with ``x`` and ``y``.
            ctx: MCP context (unused).

        Returns:
            dict with ``deleted_count``, ``matched_count``,
            ``not_found`` (positions that did not match any via),
            ``backup_path``, and ``pcb_path``.
        """
        if not vias:
            return {
                "deleted_count": 0,
                "matched_count": 0,
                "not_found": [],
                "backup_path": None,
                "pcb_path": pcb_path,
            }

        data = load_pcb(pcb_path)
        tol = 0.01  # mm

        # Collect existing vias with positions.
        existing: list[tuple[list, float, float]] = []
        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            if not _is_sym(item[0], "via"):
                continue
            at_node = _find_sub(item, "at")
            if at_node is None or len(at_node) < 3:
                continue
            vx = float(at_node[1]) if not isinstance(at_node[1], str) else float(str(at_node[1]))
            vy = float(at_node[2]) if not isinstance(at_node[2], str) else float(str(at_node[2]))
            existing.append((item, vx, vy))

        to_remove: set[int] = set()
        not_found: list[dict[str, float]] = []

        for desc in vias:
            try:
                dx = float(desc["x"])
                dy = float(desc["y"])
            except (KeyError, TypeError, ValueError) as exc:
                not_found.append({"x": desc.get("x"), "y": desc.get("y"), "error": str(exc)})
                continue

            matched = False
            for idx, (item, vx, vy) in enumerate(existing):
                if idx in to_remove:
                    continue
                if abs(vx - dx) < tol and abs(vy - dy) < tol:
                    to_remove.add(idx)
                    matched = True
                    break

            if not matched:
                not_found.append({"x": dx, "y": dy})

        if not to_remove:
            return {
                "deleted_count": 0,
                "matched_count": 0,
                "not_found": not_found,
                "backup_path": None,
                "pcb_path": pcb_path,
            }

        for idx in sorted(to_remove, reverse=True):
            data.remove(existing[idx][0])

        try:
            backup_path = save_pcb(pcb_path, data)
        except OSError as exc:
            return {"error": f"Failed to write PCB file: {exc}"}

        return {
            "deleted_count": len(to_remove),
            "matched_count": len(vias) - len(not_found),
            "not_found": not_found,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
        }


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------


def _is_sym(value: Any, name: str) -> bool:
    """Check if *value* is an sexpdata.Symbol matching *name*."""
    return isinstance(value, sexpdata.Symbol) and str(value) == name


def _find_sub(items: list, name: str) -> list | None:
    """Find the first sub-list whose first element matches *name*."""
    for sub in items:
        if isinstance(sub, list) and len(sub) >= 2 and _is_sym(sub[0], name):
            return sub
    return None


def _get_net_name(net_node: list) -> str | None:
    """Extract the net name from a (net ...) reference.

    Handles both KiCad 8 ``(net <id> "<name>")`` and
    KiCad 10 ``(net "<name>")`` formats.
    """
    if len(net_node) >= 3:
        raw = net_node[2]
    elif len(net_node) >= 2:
        raw = net_node[1]
    else:
        return None
    return raw if isinstance(raw, str) else str(raw)


# ---------------------------------------------------------------------------
# S-expression emission (board-format strings)
# ---------------------------------------------------------------------------


def _segment_to_sexp(seg: OutputSegment) -> list:
    """Build a (segment ...) node in the standard board format."""
    return [
        sexpdata.Symbol("segment"),
        [sexpdata.Symbol("start"), seg.x1, seg.y1],
        [sexpdata.Symbol("end"), seg.x2, seg.y2],
        [sexpdata.Symbol("width"), seg.width],
        [sexpdata.Symbol("layer"), seg.layer],
        [sexpdata.Symbol("net"), seg.net],
    ]


def _via_to_sexp(via: OutputVia) -> list:
    """Build a (via ...) node in the standard board format."""
    layers_node = [sexpdata.Symbol("layers"), via.layers[0], via.layers[1]]
    return [
        sexpdata.Symbol("via"),
        [sexpdata.Symbol("at"), via.x, via.y],
        [sexpdata.Symbol("size"), via.diameter],
        [sexpdata.Symbol("drill"), via.drill],
        layers_node,
        [sexpdata.Symbol("net"), via.net],
    ]


# Re-exported for callers that want to assemble multi-layer routes by hand.
__all__ = [
    "register_pcb_routing_tools",
    "connect_with_via",
]
