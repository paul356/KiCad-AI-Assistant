"""
PCB routing tools for the KiCad MCP server.

Exposes the router as an MCP tool that connects two pads with a track,
optionally across layers (through-vias on the direct pad-to-pad line).
The tool writes the resulting segments and vias back to the .kicad_pcb
file, with the usual ``.bak`` backup.

The ``pns`` engine walks around fixed solids and shoves movable tracks
out of the way; if a route is blocked, the tool fails rather than
guessing.  Use the placement / edit tools to clear the path first, or
call with a different layer.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import logging
import os
import tempfile
import time
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image
import sexpdata

from kcaa.router.path_postprocess import OutputArc, OutputSegment, OutputVia
from kcaa.router.pns.shove import TrackObstacle
from kcaa.router.router import (
    RouteFailure,
    RouteRequest,
    _find_pad_center,
    auto_route_pair,
    connect_with_via,
)
from kcaa.router.via_check import ProposedVia, check_vias
from kcaa.tools.render_route_state import render_route_attempt
from kcaa.utils.config import model_supports_vision
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
        algorithm: str | None = None,
        waypoints: list[dict] | None = None,
        dry_run: bool = False,
        strategy: str = "shove",
        options: dict | None = None,
    ) -> tuple[str, Image] | str:
        """Connect two pads with an obstacle-avoiding track, optionally across layers.

        Uses ``algorithm`` to route: omitted, it auto-selects by model —
        ``pns`` (walkaround + shove engine) for vision-capable models,
        ``astar`` (grid A* planner) for text-only models (see
        ``KICAD_MCP_SUPPORTS_VISION``).  Passing an explicit value always
        wins.  A multi-layer ``pns`` route decomposes into one
        walkaround + shove leg per layer (shortest layer path through
        ``via_pairs``), joined by through-vias DRC-validated along the
        direct pad-to-pad line.  Each leg emits rounded-corner arcs
        (``rounded45``/``rounded90``) when its skeleton survives
        walkaround/shove and the corner sits away from a via junction;
        legs whose skeleton was disturbed, or whose fillet would end on
        a via, fall back to straight segments — via junctions stay
        straight-through connections.

        ``options`` bundles the optional tuning knobs; omit it (or pass
        ``{}``) for defaults.  ``corner_mode`` defaults to
        ``mitered45`` — sharp 45-degree corners on plain 0/45/90
        segments (closest to KiCad's optimizer output); switch to
        ``rounded45`` for short fillets or ``rounded90`` for larger
        fillet arcs.

        Interface v3: the VLM control surface — ``waypoints``, ``dry_run``,
        ``strategy`` — lives at the TOP LEVEL (the caller touches these
        often); board-stable config and rare tweaks stay in ``options``,
        which gained ``layer_hint`` (moved out of the top level).

        PCB coordinates: mm, +X right, **+Y down**, rotation
        **CCW-positive on screen** (KiCad PCB convention).

        The track's width defaults to the net's netclass ``track_width`` from
        the matching ``.kicad_pro`` (or 0.25 mm if no project file is
        found).  Clearance is taken from the board's effective design rules
        (see :mod:`kcaa.utils.pcb_design_rules`).

        Layer selection is automatic: SMD pads use their fixed layer;
        thru-hole pads pick a shared copper layer, preferring the
        ``layer_hint`` option (see ``options`` below).

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
            algorithm: ``pns`` (walkaround + shove engine) or ``astar``
                (grid-based A* planner); ``None`` (default) auto-selects:
                ``pns`` for vision-capable models, ``astar`` for
                text-only models.  A route always uses exactly one
                algorithm.
            waypoints: Anchor-chain control surface for the ``pns``
                algorithm (a list of dicts): either
                ``{"kind": "waypoint", "pos": [x, y], "tol_mm": 1.0}``
                — route through a soft pass-through point on the current
                layer (unreachable waypoints are recorded in
                ``waypoint_violated`` and skipped, never a failure) — or
                ``{"kind": "via", "pos": [x, y], "to_layer": "B.Cu"}`` —
                insert a DRC-validated through-via near ``pos``,
                micro-shifted within ``tol_mm`` when the exact spot is
                blocked.  N waypoints split the route into N+1 legs.
                Any other ``kind`` (incl. ``"pad"``) is rejected with
                "unsupported anchor kind".
            dry_run: True -> route and return the full result without
                writing anything to the PCB file (no reload, no .bak;
                the file stays byte-identical).  ``route_png`` is still
                rendered (the render reads the board and writes only to
                the system temp dir).
            strategy: Explicit PNS shove-mode knob: ``"shove"`` (default;
                walkaround + shove with the default depth),
                ``"walkaround"`` (no movable push — foreign tracks are
                treated as fixed obstacles and the route detours around
                them).  Unknown values are rejected.  Only affects the
                ``pns`` engine; ``astar`` ignores it (it has no shove
                stage).

            options: Optional dict of advanced options, all optional:
                ``corner_mode``: ``mitered45`` (default) | ``rounded45`` |
                    ``rounded90`` | ``mitered90``.  Rounded modes emit arc
                    track nodes on an unobstructed skeleton (a detour
                    linearizes them).  ``mitered45`` emits plain 0/45/90
                    segments — the closest to KiCad's optimizer output.
                ``via_pairs``: tuple of ``(from_layer, to_layer)`` pairs;
                    each pair is one allowed through-via layer transition
                    edge, traversable in both directions.  Default
                    ``(("F.Cu", "B.Cu"),)`` when the resolved layers
                    differ; ignored otherwise.  On a 4-layer board
                    (F.Cu / In1.Cu / In2.Cu / B.Cu) the default forbids
                    landing on the inner layers; pass
                    ``(("F.Cu", "In1.Cu"), ("In1.Cu", "In2.Cu"),
                    ("In2.Cu", "B.Cu"))`` to force a step through the
                    inner stack, or ``((("F.Cu", "B.Cu"),))`` alone to
                    keep every transition a straight outer-to-outer jump.
                ``turn_penalty``: cost added when the path changes
                    direction (mm); default 0.3.  Set to 0 for pure
                    shortest-path routing (more zigzag).
                ``layer_hint``: Preferred copper layer for thru-hole
                    pads.  ``None`` (default) lets the router pick
                    automatically.  Ignored for SMD pads whose layer is
                    fixed by the pad itself.

        Returns:
            dict with:
                segment_count / segments: track segments written.
                arc_count / arcs: rounded-corner arcs written
                    (``{start, mid, end, width, layer, net}`` each).
                shoved: list of tracks that were pushed out of the way
                    into clear space (``{net, layer, width, points}``
                    each; endpoints stay pinned, and every displacement
                    keeps DRC clearance from pads/vias/keepouts).
                corner_mode: echoed corner_mode.
                algorithm: echoed algorithm (``astar`` | ``pns``).
                via_count / vias: vias written (0 for single-layer).
                via_sites: emitted via sites, one dict per waypoint:
                    ``{"pos": [x, y], "to_layer": ...}`` (the actual
                    DRC-clean site used, possibly micro-shifted).
                waypoint_violated / violated_waypoints: True plus the
                    ``(x, y)`` list when any waypoint anchor was
                    unreachable and got skipped (the route still
                    completed).
                layers_used: ordered list of layers touched by the path.
                start: ``(x, y)`` exit point of pad_a.
                end: ``(x, y)`` entry point of pad_b.
                strategy: echo of the requested strategy knob.
                route_png: path of the rendered single-route image (the
                    VLM inspects it to see what was routed; ``None``
                    only if the best-effort render failed).  Rendered
                    for dry_run previews and commits alike — the render
                    reads the board file and writes only to the system
                    temp dir.
                backup_path: path to the ``.bak`` created before writing
                    (``None`` with ``dry_run``).
                pcb_path: echo of the input path.
                dry_run: echo of the ``dry_run`` option.

            VLM flow: preview with ``dry_run=True`` + ``route_png``,
            then commit the same request with ``dry_run=False``;
            ``strategy`` is the explicit knob that decides whether the
            engine may shove tracks out of the way.

            Or ``{"error": "<message>", "route_png": "<path>"}`` on failure:
            the error message plus a best-effort PNG of the current board
            with the failed route's endpoint pads marked (``route_png`` is
            ``None`` when rendering is unavailable; the error is never
            masked by the render).

            The tool result is an MCP text + image pair: the JSON envelope
            described above is the text block, and the rendered route PNG
            (success) or failure-evidence PNG (error) is an image content
            block the plugin relays to the model as the ``_image`` field
            — same convention as ``export_pcb_layer_image``.  When the
            render is unavailable the result is the bare JSON text.
        """
        corner_mode = "mitered45"
        via_pairs: tuple[tuple[str, str], ...] = (("F.Cu", "B.Cu"),)
        turn_penalty = 0.3
        layer_hint: str | None = None
        if options:
            via_pairs = options.get("via_pairs", via_pairs)
            turn_penalty = options.get("turn_penalty", turn_penalty)
            corner_mode = options.get("corner_mode", corner_mode)
            layer_hint = options.get("layer_hint", layer_hint)
        waypoints = list(waypoints or [])
        if algorithm is None:
            # Vision-capable models drive the visual routing loop (PNS
            # walkaround + shove, render feedback); text-only models get
            # the deterministic grid A* planner.  The plugin sets
            # KICAD_MCP_SUPPORTS_VISION when spawning the server.
            algorithm = "pns" if model_supports_vision() else "astar"
        if strategy not in ("shove", "walkaround"):
            return _route_payload(
                {
                    "error": (
                        f"strategy={strategy!r} is invalid; supported values are "
                        "'shove' or 'walkaround'."
                    )
                }
            )
        req = RouteRequest(
            pcb_path=pcb_path,
            ref_a=ref_a,
            pad_a=pad_a,
            ref_b=ref_b,
            pad_b=pad_b,
            net=net,
            layer_hint=layer_hint,
            width=width,
            via_pairs=via_pairs,
            turn_penalty=turn_penalty,
            corner_mode=corner_mode,
            algorithm=algorithm,
            waypoints=waypoints,
            dry_run=dry_run,
            strategy=strategy,
        )
        try:
            result = auto_route_pair(req)
        except RouteFailure as exc:
            png_path, png_bytes = _route_failure_evidence(pcb_path, req)
            return _route_payload({"error": str(exc), "route_png": png_path}, png_bytes)
        except (FileNotFoundError, ValueError) as exc:
            png_path, png_bytes = _route_failure_evidence(pcb_path, req)
            return _route_payload(
                {"error": f"Routing input error: {exc}", "route_png": png_path},
                png_bytes,
            )

        # ---- Write path: dry_run short-circuits before reloading
        #      (auto_route_pair already parsed the board) and never
        #      creates the .bak or mutates the file.
        backup_path: str | None = None
        if not dry_run:
            data = load_pcb(pcb_path)
            for seg in result.segments:
                data.append(_segment_to_sexp(seg))
            for arc in result.arcs:
                data.append(_arc_to_sexp(arc))
            for via in result.vias:
                data.append(_via_to_sexp(via))
            # Persist shoved-track displacements: delete the original
            # file segment(s), append the displaced polyline as new
            # segments (same width/layer/net).  Never touches the file
            # under dry_run (short-circuited above).
            _apply_shoved_tracks(data, result.moved_pairs)
            try:
                backup_path = save_pcb(pcb_path, data)
            except OSError as exc:
                return {"error": f"Failed to write PCB file: {exc}"}

        resp = {
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
            "arc_count": len(result.arcs),
            "arcs": [
                {
                    "start": list(a.start),
                    "mid": list(a.mid),
                    "end": list(a.end),
                    "width": a.width,
                    "layer": a.layer,
                    "net": a.net,
                }
                for a in result.arcs
            ],
            "shoved": [
                {
                    "net": t.net,
                    "layer": t.layer,
                    "points": [list(pt) for pt in t.points],
                    "width": t.width,
                }
                for t in result.shoved_tracks
            ],
            "corner_mode": result.corner_mode,
            "algorithm": result.algorithm,
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
            "via_sites": [
                {
                    "pos": list(site["pos"]),
                    "to_layer": site["to_layer"],
                }
                for site in result.via_sites
            ],
            "waypoint_violated": result.waypoint_violated,
            "violated_waypoints": [list(pt) for pt in result.violated_waypoints],
            "strategy": result.strategy,
            "route_png": result.route_png,
            "backup_path": backup_path,
            "pcb_path": pcb_path,
            "dry_run": dry_run,
        }
        png_bytes = _png_bytes(result.route_png)
        return _route_payload(resp, png_bytes)

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
        ``y2`` and optional ``layer``.  A matching ``(segment ...)`` or
        ``(arc ...)`` entry is removed when both endpoints match within
        0.01 mm (either direction) and (if specified) the layer matches.
        Arc tracks match on their start/end endpoints — pass the arc's
        two ends as reported by the router (the mid point is ignored).

        A ``.bak`` backup is created before any modification.  An empty
        match list (``[]``) is a no-op — no backup, no write.  Returns the
        count of segments actually deleted (some may have already been
        removed by a previous call).

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
            segments: List of track descriptors, each with
                ``x1``, ``y1``, ``x2``, ``y2`` and optional ``layer``
                (matches straight segments and arc tracks alike).
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

        # Collect existing (segment ...) and (arc ...) items with their
        # endpoint info.  Arc track nodes share the (start) / (end) fields,
        # so endpoint matching is identical; the mid point is not used for
        # lookup (a caller deleting a rounded corner passes the arc's two
        # endpoints, exactly what the router reported as the segment).
        existing: list[tuple[list, float, float, float, float, str | None]] = []
        for item in data:
            if not (isinstance(item, list) and len(item) > 0):
                continue
            if not (_is_sym(item[0], "segment") or _is_sym(item[0], "arc")):
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


def _segment_fields(node: list) -> dict | None:
    """Extract ``{start, end, width, layer, net}`` from a ``(segment ...)`` node.

    Returns ``None`` for anything that is not a segment node.  Field
    order is not assumed (the node is scanned); ``net`` is the net *name*,
    tolerating both KiCad 8 ``(net <id> "<name>")`` and KiCad 10
    ``(net "<name>")``.
    """
    if not isinstance(node, list) or len(node) < 2 or node[0] != sexpdata.Symbol("segment"):
        return None
    fields: dict = {}
    for sub in node[1:]:
        if not isinstance(sub, list) or len(sub) < 2:
            continue
        key = sub[0]
        if key == sexpdata.Symbol("start") and len(sub) >= 3:
            fields["start"] = (float(sub[1]), float(sub[2]))
        elif key == sexpdata.Symbol("end") and len(sub) >= 3:
            fields["end"] = (float(sub[1]), float(sub[2]))
        elif key == sexpdata.Symbol("width") and len(sub) >= 2:
            fields["width"] = float(sub[1])
        elif key == sexpdata.Symbol("layer") and len(sub) >= 2:
            fields["layer"] = str(sub[1])
        elif key == sexpdata.Symbol("net") and len(sub) >= 2:
            fields["net"] = _get_net_name(sub)
    if not all(k in fields for k in ("start", "end", "width", "layer")):
        return None
    return fields


def _track_matches_segment(track: TrackObstacle, fields: dict, eps: float = 1e-6) -> bool:
    """True when the file segment fields equal the track's geometry.

    Full identity match (start/end/width/layer/net) so unrelated same-net
    tracks sharing only the layer are never touched.  Endpoint order is
    tolerated either way — a track read back from the file and pushed
    through the engine keeps its direction, but a symmetric match is
    unambiguous.
    """
    if (
        abs(track.start[0] - fields["start"][0]) > eps
        or abs(track.start[1] - fields["start"][1]) > eps
    ):
        rev = (
            abs(track.start[0] - fields["end"][0]) <= eps
            and abs(track.start[1] - fields["end"][1]) <= eps
            and abs(track.end[0] - fields["start"][0]) <= eps
            and abs(track.end[1] - fields["start"][1]) <= eps
        )
        if not rev:
            return False
    else:
        rev = False
    if not rev and (
        abs(track.end[0] - fields["end"][0]) > eps or abs(track.end[1] - fields["end"][1]) > eps
    ):
        return False
    if abs(track.width - fields["width"]) > eps:
        return False
    if fields["layer"] != track.layer:
        return False
    if track.net is not None and fields["net"] != track.net:
        return False
    return True


def _displaced_to_segments(orig: TrackObstacle, displaced: TrackObstacle) -> list[list]:
    """Serialize a displaced track as consecutive ``(segment ...)`` nodes.

    Every consecutive point pair of the displaced centerline becomes one
    segment with the original width/layer/net.  Zero-length hops are
    skipped (the shove can emit coincident chain vertices).
    """
    segs: list[list] = []
    pts = displaced.points
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        if abs(x1 - x2) <= 1e-9 and abs(y1 - y2) <= 1e-9:
            continue
        segs.append(
            [
                sexpdata.Symbol("segment"),
                [sexpdata.Symbol("start"), x1, y1],
                [sexpdata.Symbol("end"), x2, y2],
                [sexpdata.Symbol("width"), displaced.width],
                [sexpdata.Symbol("layer"), displaced.layer],
                [sexpdata.Symbol("net"), displaced.net],
            ]
        )
    return segs


def _apply_shoved_tracks(
    data: list,
    moved_pairs: Sequence[tuple[TrackObstacle, TrackObstacle]],
) -> None:
    """Persist shoved-track displacements into the parsed board ``data``.

    For every ``(original, displaced)`` pair: remove every file segment
    identical to the original, then append the displaced polyline as new
    segments.  Originals that are already gone from the file (e.g. a
    track shoved twice in one multi-leg route) are simply not matched —
    appending the displaced polyline is idempotent per final position.

    The same original re-shoved by several legs collapses to its LAST
    displacement (each original's file segment is deleted once; writing
    every displaced polyline would fork/double the physical track).  The
    router already collapses pairs before handing them over — this is
    defense in depth for any other caller.
    """
    collapsed: dict[tuple, tuple[TrackObstacle, TrackObstacle]] = {}
    for orig, displaced in moved_pairs:
        key = (orig.start, orig.end, orig.width, orig.layer, orig.net)
        collapsed[key] = (orig, displaced)
    moved_pairs = list(collapsed.values())

    kept: list = []
    displaced_segs: list[list] = []
    written: set[tuple | None] = set()
    for node in data:
        fields = _segment_fields(node)
        if fields is not None and any(
            _track_matches_segment(orig, fields) for orig, _disp in moved_pairs
        ):
            continue  # original track: replaced by the displaced polyline
        kept.append(node)
    for orig, displaced in moved_pairs:
        for seg in _displaced_to_segments(orig, displaced):
            seg_fields = _segment_fields(seg)
            fp = (
                None
                if seg_fields is None
                else (
                    round(seg_fields["start"][0], 6),
                    round(seg_fields["start"][1], 6),
                    round(seg_fields["end"][0], 6),
                    round(seg_fields["end"][1], 6),
                    round(seg_fields["width"], 6),
                    seg_fields["layer"],
                    seg_fields["net"],
                )
            )
            if fp in written:
                continue  # duplicate displacement (multi-leg overlap)
            written.add(fp)
            displaced_segs.append(seg)
    data[:] = kept + displaced_segs


# ---------------------------------------------------------------------------
# Failure evidence render (best-effort)
# ---------------------------------------------------------------------------


def _route_anchors(pcb_path: str, req: RouteRequest) -> list[tuple[float, float]]:
    """Best-effort anchor points (resolved pad centres) for failure evidence.

    Resolves the two endpoint pad centres from the board; a pad that
    cannot be located is simply skipped — the evidence render must never
    mask the original routing failure.
    """
    anchors: list[tuple[float, float]] = []
    try:
        data = load_pcb(pcb_path)
    except Exception:  # noqa: BLE001 - evidence must not mask the failure
        return anchors
    for ref, pad in ((req.ref_a, req.pad_a), (req.ref_b, req.pad_b)):
        try:
            center = _find_pad_center(data, ref, pad)
        except Exception:  # noqa: BLE001
            continue  # nosec B112 — pad lookup failed; skip anchor, evidence must not mask the route failure
        if center is not None:
            anchors.append((float(center[0]), float(center[1])))
    return anchors


def _route_payload(payload: dict, png_bytes: bytes | None = None) -> tuple[str, Image] | str:
    """Serialize a routing-tool payload to MCP content blocks.

    Returns ``(json_text, Image)`` when a render is available AND the
    calling model is vision-capable (``KICAD_MCP_SUPPORTS_VISION``, set
    by the plugin from its vision setting) — the text block carries the
    result envelope, the image block carries the rendered route/evidence
    PNG, exactly the shape the plugin's ``call_mcp_tool`` splits into
    the result dict + ``_image`` field (same convention as
    ``export_pcb_layer_image``).  A text-only model gets the bare JSON
    text (no image block, no render payload); the payload itself is
    unchanged in both cases.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if png_bytes and model_supports_vision():
        return text, Image(data=png_bytes, format="png")
    return text


def _png_bytes(png_path: str | None) -> bytes | None:
    """Read a rendered PNG file back into bytes; ``None`` when absent."""
    if not png_path:
        return None
    try:
        with open(png_path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _route_failure_evidence(pcb_path: str, req: RouteRequest) -> tuple[str | None, bytes | None]:
    """Render best-effort failure-evidence PNG; ``(path, bytes)`` or
    ``(None, None)`` when unavailable.

    The image shows the current board with the failed route's endpoint
    pads marked; it is written to the system temp dir and its path
    returned alongside the bytes.  Rendering must never mask the
    original failure, so any exception collapses to ``(None, None)``.
    """
    try:
        _lines, png_bytes, _report = render_route_attempt(
            pcb_path, anchors=_route_anchors(pcb_path, req) or None
        )
        if not png_bytes:
            return None, None
        out = os.path.join(
            tempfile.gettempdir(),
            f"kcaa_route_{time.time_ns()}_{os.getpid()}.png",
        )
        with open(out, "wb") as f:
            f.write(png_bytes)
        return out, png_bytes
    except Exception:  # noqa: BLE001 - evidence must not mask the failure
        return None, None


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


def _arc_to_sexp(arc: OutputArc) -> list:
    """Build a (arc ...) node in KiCad's 3-point track form."""
    return [
        sexpdata.Symbol("arc"),
        [sexpdata.Symbol("start"), arc.start[0], arc.start[1]],
        [sexpdata.Symbol("mid"), arc.mid[0], arc.mid[1]],
        [sexpdata.Symbol("end"), arc.end[0], arc.end[1]],
        [sexpdata.Symbol("width"), arc.width],
        [sexpdata.Symbol("layer"), arc.layer],
        [sexpdata.Symbol("net"), arc.net],
    ]


# Re-exported for callers that want to assemble multi-layer routes by hand.
__all__ = [
    "register_pcb_routing_tools",
    "connect_with_via",
]
