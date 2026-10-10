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
import math
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
from kcaa.utils.config import model_supports_vision, render_route_png_enabled
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
                the file stays byte-identical).  Route renders still
                fire (in memory; nothing is written to disk) unless
                disabled via ``KICAD_MCP_RENDER_ROUTE_PNG=0``.
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
                    into clear space (``{net, layer, width, points,
                    source_points}`` each; endpoints stay pinned, and
                    every displacement keeps DRC clearance from
                    pads/vias/keepouts).  ``points`` is the exact chain
                    persisted to the board file — straight legs, and the
                    circular corners re-emitted as 45-degree-family
                    segments, never arc nodes — while ``source_points``
                    is the un-collapsed walkaround polyline the shove
                    returned.
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
                backup_path: path to the ``.bak`` created before writing
                    (``None`` with ``dry_run``).
                pcb_path: echo of the input path.
                dry_run: echo of the ``dry_run`` option.

            The rendered route image is delivered as the image content
            block the model inspects (bytes in memory; no temp file on
            disk) — the JSON envelope never carries a ``route_png`` key
            — rendered for dry_run previews and commits alike, unless
            disabled (``KICAD_MCP_RENDER_ROUTE_PNG=0``).

            VLM flow: preview with ``dry_run=True``, then commit the
            same request with ``dry_run=False``; ``strategy`` is the
            explicit knob that decides whether the engine may shove
            tracks out of the way.

            Or ``{"error": "<message>"}`` on failure: the error message
            plus a best-effort PNG of the current board with the failed
            route's endpoint pads marked (the render rides the image
            content block; the JSON envelope has no ``route_png`` key,
            and the error is never masked by the render).

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
            png_bytes = _route_failure_evidence(pcb_path, req)
            return _route_payload({"error": str(exc)}, png_bytes)
        except (FileNotFoundError, ValueError) as exc:
            png_bytes = _route_failure_evidence(pcb_path, req)
            return _route_payload(
                {"error": f"Routing input error: {exc}"},
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
                    "net": disp.net,
                    "layer": disp.layer,
                    "width": disp.width,
                    # The exact chain the write path persists (45-degree
                    # family segments for the circular corners) — NOT the
                    # dense walkaround polyline.  `source_points` keeps
                    # the un-collapsed displacement for inspection.
                    "points": [
                        [float(x), float(y)] for x, y in _displaced_chain_points(orig, disp)
                    ],
                    "source_points": [list(pt) for pt in disp.points],
                }
                for orig, disp in result.moved_pairs
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
            "backup_path": backup_path,
            "pcb_path": pcb_path,
            "dry_run": dry_run,
        }
        png_bytes = result.route_png
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

    Returns ``None`` for anything that is not a segment node, and for a
    segment whose numeric fields cannot be parsed (malformed coordinates
    in a hand-edited board).  Field order is not assumed (the node is
    scanned); ``net`` is the net *name*, tolerating both KiCad 8
    ``(net <id> "<name>")`` and KiCad 10 ``(net "<name>")``.
    """
    if not isinstance(node, list) or len(node) < 2 or node[0] != sexpdata.Symbol("segment"):
        return None
    fields: dict = {}
    for sub in node[1:]:
        if not isinstance(sub, list) or len(sub) < 2:
            continue
        key = sub[0]
        try:
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
        except (TypeError, ValueError):
            # Malformed numeric field (hand-edited board): treat the
            # node as unparseable rather than crashing the route write
            # path — the world model tolerates the same and skips.
            return None
    if not all(k in fields for k in ("start", "end", "width", "layer")):
        return None
    return fields


def _node_fingerprint(node: list) -> tuple | None:
    """Deduplication identity for a written ``(segment ...)``/``(arc ...)``.

    ``_segment_fields`` deliberately ignores arc nodes (it describes a
    segment for removal matching).  The write path must dedupe arcs too:
    a shoved corner coalesces into *several* arc nodes and every one
    must survive, while an identical node written twice (multi-leg
    overlap) must collapse.  Fingerprint includes start/end/mid so two
    distinct arc halves are never considered duplicates; ``None`` for
    nodes outside the two track shapes.
    """
    if not isinstance(node, list) or len(node) < 2:
        return None
    # sexpdata.Symbol subclasses ``str`` but overrides equality, so
    # ``node[0] in ("segment", "arc")`` is False for a Symbol; always
    # compare the .value() string.
    kind = node[0].value() if isinstance(node[0], sexpdata.Symbol) else node[0]
    if kind not in ("segment", "arc"):
        return None
    fields: dict = {}
    for sub in node[1:]:
        if not isinstance(sub, list) or len(sub) < 2:
            continue
        key = sub[0]
        if isinstance(key, sexpdata.Symbol):
            key = key.value()
        if key == "start" and len(sub) >= 3:
            fields["start"] = (round(float(sub[1]), 6), round(float(sub[2]), 6))
        elif key == "mid" and len(sub) >= 3:
            fields["mid"] = (round(float(sub[1]), 6), round(float(sub[2]), 6))
        elif key == "end" and len(sub) >= 3:
            fields["end"] = (round(float(sub[1]), 6), round(float(sub[2]), 6))
        elif key == "width" and len(sub) >= 2:
            fields["width"] = round(float(sub[1]), 6)
        elif key == "layer" and len(sub) >= 2:
            fields["layer"] = str(sub[1])
        elif key == "net" and len(sub) >= 2:
            fields["net"] = _get_net_name(sub)
    if not all(k in fields for k in ("start", "end", "width", "layer")):
        return None
    return (
        kind,
        fields.get("start"),
        fields.get("mid"),  # None for segments
        fields.get("end"),
        fields.get("width"),
        fields.get("layer"),
        fields.get("net"),
    )


def _track_matches_segment(track: TrackObstacle, fields: dict, eps: float = 1e-6) -> bool:
    """True when the file segment fields equal a segment of the track.

    The shoved track is a whole LINE (a physical track stored as
    consecutive file segments); ``track.points`` may hold many segments,
    and the file entry matches when its start/end equal ANY consecutive
    point pair (either direction) — the write path then knows exactly
    which file segment was displaced.  Full identity match
    (start/end/width/layer/net) so unrelated same-net tracks sharing
    only the layer are never touched.
    """
    pts = track.points
    for a, b in zip(pts, pts[1:]):
        fwd = (
            abs(a[0] - fields["start"][0]) <= eps
            and abs(a[1] - fields["start"][1]) <= eps
            and abs(b[0] - fields["end"][0]) <= eps
            and abs(b[1] - fields["end"][1]) <= eps
        )
        rev = (
            abs(a[0] - fields["end"][0]) <= eps
            and abs(a[1] - fields["end"][1]) <= eps
            and abs(b[0] - fields["start"][0]) <= eps
            and abs(b[1] - fields["start"][1]) <= eps
        )
        if not (fwd or rev):
            continue
        if abs(track.width - fields["width"]) > eps:
            continue
        if fields["layer"] != track.layer:
            continue
        if track.net is not None and fields["net"] != track.net:
            continue
        return True
    return False


# A walkaround arc's sampled vertices stay within this of one circle.
# The real-board shove arc (r=0.717mm, 69.3deg) fits to <=12um; a 45deg
# chord across it would be ~110um deep and violate the ~9um DRC headroom,
# so the arc must round-trip as a KiCad (arc ...) node, not a straight
# cut or a chain of slivers.
ARC_FIT_TOLERANCE_MM = 25.0 * 1e-3
# Longest collinear run before splitting (guard against giga-vertices).
MAX_STRAIGHT_RUN_PTS = 16
# An arc spanning more than this cannot be faithfully encoded by one
# 3-point (arc ...) node (the three points would be nigh-collinear on a
# ~180deg arc, making the circumcircle degenerate); split such arcs.
MAX_ARC_SPAN_DEG = 165.0
# Minimum vertices a circular run must span to count as an arc (three
# points always define *a* circle; five make the fit meaningful).
MIN_ARC_PTS = 5
# Arc samples from the hull walkaround are densely polygonized: every
# step is <=~0.3mm, typically 10-40um.  A straight shove leg between
# far-apart vertices (1.6mm+) fits *some* big circle (r~2.7mm) with
# residual under ARC_FIT_TOLERANCE_MM, so a bare residual check lets a
# "straight + diagonal + straight" window masquerade as one arc and
# swallow the corner geometry.  One hop of MAX_ARC_STEP_MM or more in
# the window is a leg, not an arc sample.
MAX_ARC_STEP_MM = 0.5


def _collinear_k(
    pts: list[tuple[float, float]],
    i: int,
    k: int,
    eps: float = 1e-6,
) -> bool:
    """True when vertex ``k`` lies on the line through ``pts[i]``..``pts[k-1]``.

    Uses the normalized cross product of the cumulative direction; the
    tolerance is an angle (radians), so long runs of a true line stay
    merged while a 45deg corner (cross ~ 0.7) breaks the run.
    """
    a = pts[i]
    b = pts[k - 1]
    c = pts[k]
    len_ab = math.hypot(b[0] - a[0], b[1] - a[1])
    len_bc = math.hypot(c[0] - b[0], c[1] - b[1])
    denom = max(len_ab * len_bc, 1e-12)
    cross = abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    return cross / denom <= eps


def _circle_from_three(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> tuple[tuple[float, float], float] | None:
    """Circumcenter + radius of the circle through ``a``, ``b``, ``c``.

    ``None`` when the three points are (nearly) collinear — their
    circumcircle is degenerate.
    """
    d = 2.0 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    a2 = a[0] * a[0] + a[1] * a[1]
    b2 = b[0] * b[0] + b[1] * b[1]
    c2 = c[0] * c[0] + c[1] * c[1]
    ux = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    uy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    r = math.hypot(a[0] - ux, a[1] - uy)
    return (ux, uy), r


def _arc_span_deg(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
) -> float:
    """Signed angle between ``start`` and ``end`` around ``center``."""
    cx, cy = center
    a1 = math.atan2(start[1] - cy, start[0] - cx)
    a2 = math.atan2(end[1] - cy, end[0] - cx)
    span = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
    return math.degrees(abs(span))


def _fit_circle_lsq(
    pts: list[tuple[float, float]],
) -> tuple[tuple[float, float], float] | None:
    """Least-squares circle through ``pts`` (center, radius).

    Minimizes the algebraic fit ``x^2+y^2+Dx+Ey+F=0`` via normal
    equations on mean-centered coordinates.  Mean-centering is not
    optional: the raw normal matrix has entries like ``sum x^4 ~ 1e9``
    against a constant row of ``n ~ 1e2`` (condition number ~ 1e12),
    which amplifies round-off into a wrong circle.  In centered
    coordinates every moment is O(span^2) and the fit is stable.

    ``None`` when the points are (nearly) collinear (radius blows up).
    """
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    # Normal equations for a x^2 + b xy + c y^2 + ... on centered points:
    # solve  [Sxx  Sxy  Sx ] [D]   [ -Sx3  ]
    #        [Sxy  Syy  Sy ] [E] = [ -Sy3  ]
    #        [Sx   Sy   n  ] [F]   [ -(Sxx+Syy) ]
    # with Sx3 = sum x*(x^2+y^2), Sy3 = sum y*(x^2+y^2).  The naive
    # split of ``sum x*(x^2+y^2)`` into ``sum x^3 + sum x y^2`` invites
    # double-counting bugs; keep the single rotated moment instead.
    S = {"x": 0.0, "y": 0.0, "xx": 0.0, "yy": 0.0, "xy": 0.0, "x3": 0.0, "y3": 0.0}
    for px, py in pts:
        x = px - mx
        y = py - my
        S["x"] += x
        S["y"] += y
        S["xx"] += x * x
        S["yy"] += y * y
        S["xy"] += x * y
        r2 = x * x + y * y
        S["x3"] += x * r2
        S["y3"] += y * r2
    a = [[S["xx"], S["xy"], S["x"]], [S["xy"], S["yy"], S["y"]], [S["x"], S["y"], float(n)]]
    b = [-S["x3"], -S["y3"], -(S["xx"] + S["yy"])]
    # Gaussian elimination with partial pivoting (3x3).
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        b[col], b[piv] = b[piv], b[col]
        for r in range(3):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, 3):
                a[r][c] -= f * a[col][c]
            b[r] -= f * b[col]
    d = b[0] / a[0][0]
    e = b[1] / a[1][1]
    f = b[2] / a[2][2]
    cx = -d / 2.0 + mx
    cy = -e / 2.0 + my
    radius = math.sqrt(max(d * d / 4.0 + e * e / 4.0 - f, 0.0))
    if radius < 1e-9 or radius > 1e4:
        return None
    return (cx, cy), radius


def _best_arc_run(
    pts: list[tuple[float, float]],
    i: int,
    tolerance: float,
) -> tuple[int, tuple[float, float], float] | None:
    """Longest circular run ``[i..k]`` with ``>= MIN_ARC_PTS`` vertices.

    The circle is fit by least squares over the WHOLE window (not three
    anchor points): a hull arc is polygonized with ~13um of noise per
    vertex, and a 3-point circumcircle is anchor-sensitive at that
    noise, shedding vertices or swallowing a straight leg.  Growing the
    window re-fits over all samples; the first k whose window no longer
    fits one circle stops the run.

    A residual check alone is not enough: two long straight legs joined
    by a short diagonal fit one big circle (r~2.7mm) with every vertex
    inside ARC_FIT_TOLERANCE_MM, swallowing the corner geometry.  The
    step-length gate rejects that: a hull arc never hops
    MAX_ARC_STEP_MM+ in one sample, a shove leg does.

    Returns ``(k, center, radius)`` or ``None``.
    """
    n = len(pts)
    best: tuple[int, tuple[float, float], float] | None = None
    for k in range(i + MIN_ARC_PTS - 1, n):
        # One hop of MAX_ARC_STEP_MM+ anywhere in the window is a
        # straight leg, not an arc sample; stop growing (a later vertex
        # cannot repair the length evidence).
        if any(
            math.hypot(pts[t + 1][0] - pts[t][0], pts[t + 1][1] - pts[t][1]) > MAX_ARC_STEP_MM
            for t in range(i, k)
        ):
            break
        circ = _fit_circle_lsq(pts[i : k + 1])
        if circ is None:
            break
        center, radius = circ
        if all(
            abs(math.hypot(x - center[0], y - center[1]) - radius) <= tolerance
            for x, y in pts[i : k + 1]
        ):
            best = (k, center, radius)
        else:
            break
    return best


def _track_node(
    kind: str,
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float] | None,
    displaced: TrackObstacle,
) -> list:
    """Build a ``(segment ...)`` or ``(arc start mid end ...)`` sexp node."""
    node = [sexpdata.Symbol(kind)]
    node.append([sexpdata.Symbol("start"), p1[0], p1[1]])
    if kind == "arc":
        node.append([sexpdata.Symbol("mid"), p2[0], p2[1]])
        node.append([sexpdata.Symbol("end"), p3[0], p3[1]])  # type: ignore[index]
    else:
        node.append([sexpdata.Symbol("end"), p2[0], p2[1]])
    node.append([sexpdata.Symbol("width"), displaced.width])
    node.append([sexpdata.Symbol("layer"), displaced.layer])
    node.append([sexpdata.Symbol("net"), displaced.net])
    return node


def _quantize_dir45(angle: float) -> float:
    """Nearest 45-degree family direction (0..315) to ``angle``."""
    return round(angle / 45.0) % 8 * 45.0


def _arc_turn_sign(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
) -> int:
    """+1 for counter-clockwise travel (math convention) of the arc from
    ``start`` to ``end`` around ``center``, -1 for clockwise."""
    a1 = math.atan2(start[1] - center[1], start[0] - center[0])
    a2 = math.atan2(end[1] - center[1], end[0] - center[0])
    delta = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
    return 1 if delta >= 0 else -1


def _segment_dir(node: list) -> float | None:
    """Direction (deg, 0..360) of a written ``(segment ...)`` node."""
    if node[0].value() != "segment" or len(node) < 3:
        return None
    s = node[1]
    e = node[2]
    return math.degrees(math.atan2(e[2] - s[2], e[1] - s[1])) % 360.0


def _arc_to_45_nodes(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    radius: float,
    entry_dir: float,
    samples: list[tuple[float, float]],
    displaced: TrackObstacle,
    depth: int = 0,
) -> list[list]:
    """Approximate a circular run as a 45-degree-family segment chain.

    Every emitted leg is quantized to the 45-degree family
    (0/45/90/135/180 — KiCad miter geometry) and consecutive legs differ
    by exactly 45 degrees, the all-45 routing style.  The chain ALWAYS
    stays on or outside the fitted circle: the arc center faces the
    obstacle, so any chord inside the circle bites the shove clearance;
    an outside chain only gains margin (verified on the real board: the
    outer 3-leg chain keeps every sample >= r from the center, i.e. 0um
    intrusion against a 9.4um headroom).

    Leg count follows the span: up to 45 degrees one 45-degree turn (2
    legs, uniquely determined); up to 90 degrees two turns (3 legs, one
    free length scanned); wider runs split at the real sample closest to
    the 90-degree mark (each half again 2-3 legs).  ``entry_dir`` is the
    incoming straight's direction; the first leg snaps it to the 45
    family so the corner itself miteres 45 deg at a time.
    ``start``/``end`` are real window vertices (chain stays locked
    head-to-tail to the walkaround polyline).  Returns ``[]`` when no
    leg solution keeps the chain outside the circle — the caller then
    emits the window verbatim (still continuous, just denser).
    """
    span = _arc_span_deg(start, end, center)
    sgn = _arc_turn_sign(start, end, center)
    if span > 90.0 and len(samples) >= 7 and depth < 4:
        # Split at the ANGULAR midpoint (not a fixed 90-degree turn):
        # a wide run gets cut into two roughly equal halves, each of
        # which then fits 2-3 legs.  Pick the real sample closest to
        # that angle so the mid joint stays locked to the walkaround
        # polyline.
        a0 = math.atan2(start[1] - center[1], start[0] - center[0])
        target = a0 + math.radians(span / 2.0) * sgn
        m = 1
        best_ang = float("inf")
        for q, pt in enumerate(samples[1:-1], start=1):
            ang = (math.atan2(pt[1] - center[1], pt[0] - center[0]) - target) % (2 * math.pi)
            ang = min(ang, 2 * math.pi - ang)
            if ang < best_ang:
                best_ang = ang
                m = q
        mid = samples[m]
        first = _arc_to_45_nodes(
            start, mid, center, radius, entry_dir, samples[: m + 1], displaced, depth + 1
        )
        if not first:
            return []
        last_dir = _segment_dir(first[-1])
        if last_dir is None:
            return []
        second = _arc_to_45_nodes(
            mid, end, center, radius, last_dir, samples[m:], displaced, depth + 1
        )
        if not second:
            return first
        # The split sample may land mid-leg: the first half's last leg
        # and the second half's first leg then continue in the SAME
        # direction (zero turn at the joint).  Collapse them into one
        # segment so the emitted chain turns exactly 45 deg per joint.
        out = list(first)
        d1 = _segment_dir(first[-1])
        d2 = _segment_dir(second[0])
        if d1 is not None and d2 is not None and min(abs(d1 - d2), 360.0 - abs(d1 - d2)) < 1e-6:
            out[-1] = _track_node(
                "segment",
                (first[-1][1][1], first[-1][1][2]),
                (second[0][2][1], second[0][2][2]),
                None,
                displaced,
            )
            out.extend(second[1:])
        else:
            out.extend(second)
        return out

    d0 = _quantize_dir45(entry_dir)
    nlegs = 3 if span > 45.0 else 2
    dirs = [(d0 + 45.0 * sgn * k) % 360.0 for k in range(nlegs)]
    u = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in dirs]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if math.hypot(dx, dy) < 1e-9:
        return []

    def _candidates() -> list[tuple[float, ...]]:
        if nlegs == 2:
            det = u[0][0] * u[1][1] - u[0][1] * u[1][0]
            if abs(det) < 1e-9:
                return []
            # Solve L0 u0 + L1 u1 = d.
            L0 = (dx * u[1][1] - dy * u[1][0]) / det
            L1 = (u[0][0] * dy - u[0][1] * dx) / det
            return [(L0, L1)]
        det = u[0][0] * u[2][1] - u[0][1] * u[2][0]
        if abs(det) < 1e-9:
            return []
        out: list[tuple[float, ...]] = []
        t_max = 2.0 * math.hypot(dx, dy)
        prev: tuple[float, ...] | None = None
        for kk in range(0, 401):
            t = t_max * kk / 400.0
            bx = dx - t * u[1][0]
            by = dy - t * u[1][1]
            L0 = (bx * u[2][1] - by * u[2][0]) / det
            L2 = (u[0][0] * by - u[0][1] * bx) / det
            if min(L0, t, L2) < -1e-6:
                continue
            cand = (L0, t, L2)
            if prev is not None and abs(cand[1] - prev[1]) < 1e-9:
                continue
            prev = cand
            out.append(cand)
        return out

    # Clearance tolerance = worst fit residual over the real window:
    # the fitted circle can sit a few um inside a sampled vertex (LSQ
    # compromises), so "on/outside the circle" must allow that noise —
    # the chain then never gets closer to the center than the walkaround
    # polyline itself, i.e. the geometry is DRC-equivalent to the arc.
    fit_noise = max(abs(math.hypot(x - center[0], y - center[1]) - radius) for x, y in samples)
    best: tuple[float, tuple[float, ...]] | None = None
    for cand in _candidates():
        pts_s = [start]
        for L, (ux, uy) in zip(cand, u):
            pts_s.append((pts_s[-1][0] + L * ux, pts_s[-1][1] + L * uy))
        if math.hypot(pts_s[-1][0] - end[0], pts_s[-1][1] - end[1]) > 1e-6:
            continue  # numerical endpoint mismatch
        # No degenerate legs (a zero-length leg makes the 45-degree
        # family step invisible).
        if any(L < 1e-4 for L in cand):
            continue
        # Never inside the circle (clearance is on the center side).
        inside = 0.0
        for a, b in zip(pts_s, pts_s[1:]):
            n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.005))
            for q in range(n + 1):
                p = (a[0] + (b[0] - a[0]) * q / n, a[1] + (b[1] - a[1]) * q / n)
                inside = max(inside, radius - math.hypot(p[0] - center[0], p[1] - center[1]))
        if inside > fit_noise:
            continue
        # Fidelity: how tightly the chain hugs the circle.
        dev = 0.0
        for a, b in zip(pts_s, pts_s[1:]):
            n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.02))
            for q in range(n + 1):
                p = (a[0] + (b[0] - a[0]) * q / n, a[1] + (b[1] - a[1]) * q / n)
                dev = max(dev, abs(math.hypot(p[0] - center[0], p[1] - center[1]) - radius))
        if best is None or dev < best[0]:
            best = (dev, cand)
    if best is None:
        return []
    cand = best[1]
    pts_s = [start]
    for L, (ux, uy) in zip(cand, u):
        pts_s.append((pts_s[-1][0] + L * ux, pts_s[-1][1] + L * uy))
    nodes: list[list] = []
    for a, b in zip(pts_s, pts_s[1:]):
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            continue
        nodes.append(_track_node("segment", a, b, None, displaced))
    return nodes


def _displaced_to_segments(orig: TrackObstacle, displaced: TrackObstacle) -> list[list]:
    """Serialize a displaced track as ``(segment ...)`` nodes.

    The shoved polyline comes out of the hull walkaround as a dense
    vertex chain: straight legs carry sub-millimeter intermediate samples
    and a corner becomes ~20-100 vertices on one circle.  Emitting every
    pair as its own ``(segment ...)`` floods the board file with slivers
    (KiCad shows the corner as a mass of tiny tracks).  Instead:

      * maximal collinear runs collapse into one segment,
      * maximal circular runs (every vertex on one circle within
        ``ARC_FIT_TOLERANCE_MM``) collapse into 2-3 short segments of
        the 45-degree family (``_arc_to_45_nodes``) — never an arc node,
        so the written track stays fully shovable by a later route, and
      * leftover isolated pairs emit as before.

    Zero-length hops are skipped (the shove can emit coincident chain
    vertices).
    """
    nodes: list[list] = []
    pts = list(displaced.points)
    n = len(pts)
    i = 0
    while i < n - 1:
        # --- Collinear run: extend while pts[k] stays on the line.
        j = i + 1
        while j < n - 1 and j - i < MAX_STRAIGHT_RUN_PTS and _collinear_k(pts, i, j + 1):
            j += 1
        if j > i + 1:
            nodes.append(_track_node("segment", pts[i], pts[j], None, displaced))
            i = j
            continue

        # --- Circular run from here.  The step-length gate inside
        #     ``_best_arc_run`` already refutes legs: a window that
        #     contains a straight shove leg (one hop >= MAX_ARC_STEP_MM)
        #     fits *some* big circle under the residual tolerance, so
        #     growing the run refuses it.  Whatever survives is a dense
        #     hull-arc polygon and is safe to re-emit as 45-family
        #     segments (never an ``(arc ...)`` node — arcs cannot be
        #     shoved by a later route, so the write path must keep the
        #     track shovable).
        arc = _best_arc_run(pts, i, ARC_FIT_TOLERANCE_MM)
        if arc is not None and arc[0] > i:
            best_k, best_c, best_r = arc
            window = pts[i : best_k + 1]
            entry_dir = _segment_dir(nodes[-1]) if nodes else None
            if entry_dir is None:
                cx, cy = best_c
                entry_dir = math.degrees(math.atan2(pts[i][1] - cy, pts[i][0] - cx)) + 90.0
            sub = _arc_to_45_nodes(
                pts[i], pts[best_k], best_c, best_r, entry_dir, window, displaced
            )
            if sub:
                nodes.extend(sub)
                i = best_k
                continue
            # Fall back: the run refused a 45-family outer chain
            # (degenerate geometry); emit the window as plain pairs.
            for a, b in zip(window, window[1:]):
                if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
                    continue
                nodes.append(_track_node("segment", a, b, None, displaced))
            i = best_k
            continue

        # --- Isolated pair (straight leg between two far vertices).
        nodes.append(_track_node("segment", pts[i], pts[i + 1], None, displaced))
        i += 1
    return nodes


def _displaced_chain_points(
    orig: TrackObstacle, displaced: TrackObstacle
) -> list[tuple[float, float]]:
    """The persisted vertex chain for a displaced track.

    ``_displaced_to_segments`` collapses the dense walkaround polyline
    (straight legs, 45-degree-family corner chain).  This returns the
    head-to-tail vertex sequence of exactly what the write path persists,
    so the tool response ``shoved[].points`` matches the board file —
    not the un-collapsed source polyline.
    """
    nodes = _displaced_to_segments(orig, displaced)
    if not nodes:
        return list(displaced.points)
    pts: list[tuple[float, float]] = [(nodes[0][1][1], nodes[0][1][2])]
    for node in nodes[1:]:
        pts.append((node[1][1], node[1][2]))
    pts.append((nodes[-1][2][1], nodes[-1][2][2]))
    return pts


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
            fp = _node_fingerprint(seg)
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

    Returns ``(json_text, Image)`` when a render is available AND route
    rendering is enabled (``KICAD_MCP_RENDER_ROUTE_PNG``, default on;
    also off for text-only models via ``KICAD_MCP_SUPPORTS_VISION``) —
    the text block carries the result envelope, the image block carries
    the rendered route/evidence PNG, exactly the shape the plugin's
    ``call_mcp_tool`` splits into the result dict + ``_image`` field
    (same convention as ``export_pcb_layer_image``).  When rendering is
    disabled or the render failed the result is the bare JSON text
    (no image block); the payload itself is unchanged in both cases.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if png_bytes and render_route_png_enabled():
        return text, Image(data=png_bytes, format="png")
    return text


def _route_failure_evidence(pcb_path: str, req: RouteRequest) -> bytes | None:
    """Render best-effort failure-evidence PNG bytes; ``None`` when
    unavailable.

    The image shows the current board with the failed route's endpoint
    pads marked.  Bytes only — the tool payload attaches them as an
    image content block; no temp file is written (a persistent server
    would otherwise leave a board-layout PNG in the shared temp dir for
    its lifetime, world-readable).  Rendering must never mask the
    original failure, so any exception collapses to ``None``; skipped
    entirely when route rendering is disabled (``KICAD_MCP_RENDER_
    ROUTE_PNG=0`` or a text-only model).
    """
    from kcaa.utils.config import render_route_png_enabled

    if not render_route_png_enabled():
        return None
    try:
        _lines, png_bytes, _report = render_route_attempt(
            pcb_path, anchors=_route_anchors(pcb_path, req) or None
        )
        return png_bytes
    except Exception:  # noqa: BLE001 - evidence must not mask the failure
        return None


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
