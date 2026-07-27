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
        layer: str = "F.Cu",
        width: float | None = None,
        target_layer: str | None = None,
        via_pairs: tuple[tuple[str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """Connect two pads with an obstacle-avoiding track, optionally across layers.

        Uses the no-shove PNS router: if the path is blocked by an existing
        track or footprint courtyard, the call fails rather than moving
        anything.  Run the placement tools first to clear the way, or call
        again with a different ``layer``.

        PCB coordinates: mm, +X right, **+Y down**, rotation
        **clockwise-positive** (KiCad PCB convention).

        The track's width defaults to the net's netclass ``track_width`` from
        the matching ``.kicad_pro`` (or 0.25 mm if no project file is
        found).  Clearance is taken from the board's effective design rules
        (see :mod:`kcaa.utils.pcb_design_rules`).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            ref_a: Reference designator of the first footprint (e.g. ``"R1"``).
            pad_a: Pad number on ``ref_a`` (e.g. ``"1"``).
            ref_b: Reference designator of the second footprint.
            pad_b: Pad number on ``ref_b``.
            net: Net name to assign to the new segments.
            ctx: MCP context (unused).
            layer: Starting copper layer (``"F.Cu"`` by default).
            width: Override the netclass track width (mm).  ``None`` uses the
                DRC default for the net.
            target_layer: Destination copper layer.  When ``None`` (default)
                the route stays on ``layer``.  When set, the router may
                insert through-hole vias to reach the destination.
            via_pairs: Optional tuple of ``(from_layer, to_layer)`` pairs
                the router is allowed to use as via transitions.  Defaults
                to ``(("F.Cu", "B.Cu"),)`` when ``target_layer`` differs
                from ``layer``; ignored otherwise.

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
        if target_layer is None:
            target_layer = layer
        if via_pairs is None and target_layer != layer:
            via_pairs = (("F.Cu", "B.Cu"),)
        req = RouteRequest(
            pcb_path=pcb_path,
            ref_a=ref_a,
            pad_a=pad_a,
            ref_b=ref_b,
            pad_b=pad_b,
            net=net,
            start_layer=layer,
            end_layer=target_layer,
            width=width,
            via_pairs=via_pairs or (),
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
