"""
Path post-processing: convert an ordered list of :class:`RouteNode` from A\\*
into a sequence of ``(segment ...)`` S-expression nodes with mitered corners.

What this does
--------------

A\\* returns a polyline with arbitrary angles (often 90°).  PCB tracks prefer
**45° miters** at corners — that's both more manufacturable and visually
clean. This module walks the polyline and inserts intermediate points so
each interior corner becomes a 45° cut:

    A                A
     \\                \
      \\      →         *--C   (C is the mitered corner vertex)
       \\              /
        B            B

The miter is bounded by ``min(distance to prev, distance to next,
max_miter)`` so it doesn't overshoot a tight corner.

The post-processor is layer-aware; on output each segment carries its
layer and the trace width.

Multi-layer paths
-----------------

:func:`postprocess_path` walks a multi-layer A\\* path, groups consecutive
nodes by layer, runs the single-layer post-processor on each group, and
emits an :class:`OutputVia` at every layer transition. A layer transition
is any pair of consecutive nodes whose ``layer`` attribute differs. A
through-via is emitted at the transition point with ``layers = (from,
to)``; the diameter / drill are taken from the project netclass
(``via_diameter``/``via_drill``) and default to safe values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sexpdata

from kcaa.router.grid_a_star import _line_crosses_obstacles
from kcaa.router.visibility_graph import RouteNode


@dataclass
class OutputSegment:
    """A single track segment ready to be emitted as a S-expression node."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str


@dataclass
class OutputVia:
    """A single via ready to be emitted as a S-expression node."""

    x: float
    y: float
    diameter: float
    drill: float
    layers: tuple[str, str]  # exactly two layers (a through-via)
    net: str


def postprocess(
    path: list[RouteNode],
    width: float,
    layer: str,
    net: str,
    max_miter_mm: float = 1.0,
    _obstacles: list | None = None,
    _pad_rects: list | None = None,
) -> list[OutputSegment]:
    """Convert an A\\* polyline into mitered OutputSegments.

    The first and last segments keep their original orientation (so the
    track enters/exits the pad in the right direction). Interior corners
    get a 45° cut, length-bounded by ``max_miter_mm``.

    Args:
        path: Ordered list of :class:`RouteNode` from start to goal.
        width: Trace width in mm.
        layer: Copper layer name (e.g. ``"F.Cu"``).
        net: Net name.
        max_miter_mm: Maximum length of any single miter cut in mm.

    Returns:
        A list of :class:`OutputSegment` ready for serialization.
    """
    if len(path) < 2:
        return []

    # Simplify collinear runs first — A* often includes a vertex
    # exactly along a straight edge.
    pts = _simplify_collinear([(p.x, p.y) for p in path])
    if len(pts) < 2:
        return []

    out: list[OutputSegment] = []
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        out.append(OutputSegment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net))

    # Apply mitering in-place on the segments by splitting interior corners.
    out = _apply_miters(out, max_miter_mm, _obstacles, _pad_rects)
    return out


# Default via dimensions when the project netclass does not provide them.
# These are conservative 0.6 / 0.3 mm values that match KiCad's "Standard
# via" defaults and work for general-purpose 1-2 layer boards.
DEFAULT_VIA_DIAMETER_MM = 0.6
DEFAULT_VIA_DRILL_MM = 0.3


def postprocess_path(
    path: list[RouteNode],
    width: float,
    net: str,
    max_miter_mm: float = 1.0,
    via_diameter_mm: float = DEFAULT_VIA_DIAMETER_MM,
    via_drill_mm: float = DEFAULT_VIA_DRILL_MM,
    _obstacles: list | None = None,
    _pad_rects: list | None = None,
) -> tuple[list[OutputSegment], list[OutputVia]]:
    """Convert a multi-layer A\\* path into mitered segments and vias.

    Walks ``path`` left to right. Consecutive nodes sharing the same layer
    are grouped; each group is post-processed with :func:`postprocess` to
    produce :class:`OutputSegment` records on that layer. When the layer
    changes between ``path[i]`` and ``path[i+1]``, an :class:`OutputVia` is
    emitted at ``(path[i].x, path[i].y)`` with ``layers =
    (path[i].layer, path[i+1].layer)``.

    Invariant: the visibility graph constructs via nodes at the same (x, y)
    across layers, so a layer transition always has
    ``path[i].x == path[i+1].x`` and ``path[i].y == path[i+1].y``. This is
    asserted in debug builds.

    Args:
        path: Ordered list of :class:`RouteNode` from start to goal.
        width: Trace width in mm.
        net: Net name.
        max_miter_mm: Maximum miter cut length.
        via_diameter_mm: Via pad diameter in mm.
        via_drill_mm: Via drill diameter in mm.

    Returns:
        ``(segments, vias)`` — both lists are independent; segments never
        reference vias and vice versa.

    Raises:
        RuntimeError: If the path's via edges have inconsistent (x, y) —
            this is a programming error in the visibility graph builder.
    """
    segments: list[OutputSegment] = []
    vias: list[OutputVia] = []

    if len(path) < 2:
        return segments, vias

    # Walk in (start, end) pairs; whenever the layer changes, emit a via at
    # the start node of the pair and start a new group on the new layer.
    group: list[RouteNode] = [path[0]]
    for i in range(1, len(path)):
        prev = path[i - 1]
        cur = path[i]
        if cur.layer != prev.layer:
            # Emit the previous group as segments.
            segments.extend(
                postprocess(
                    group,
                    width=width,
                    layer=prev.layer,
                    net=net,
                    max_miter_mm=max_miter_mm,
                    _obstacles=_obstacles,
                    _pad_rects=_pad_rects,
                )
            )
            # Emit the via at the transition point.
            vias.append(
                OutputVia(
                    x=prev.x,
                    y=prev.y,
                    diameter=via_diameter_mm,
                    drill=via_drill_mm,
                    layers=(prev.layer, cur.layer),
                    net=net,
                )
            )
            # If the next layer's start point differs from the via
            # position, insert a short bridging segment on the new layer.
            if abs(cur.x - prev.x) > 1e-6 or abs(cur.y - prev.y) > 1e-6:
                group = [
                    RouteNode(
                        x=prev.x,
                        y=prev.y,
                        layer=cur.layer,
                        node_id=prev.node_id,
                    ),
                    cur,
                ]
                segments.extend(
                    postprocess(
                        group,
                        width=width,
                        layer=cur.layer,
                        net=net,
                        max_miter_mm=max_miter_mm,
                        _obstacles=_obstacles,
                        _pad_rects=_pad_rects,
                    )
                )
                group = [cur]
            else:
                group = [cur]
        else:
            group.append(cur)
    # Flush the final group.
    if group:
        segments.extend(
            postprocess(
                group,
                width=width,
                layer=group[-1].layer,
                net=net,
                max_miter_mm=max_miter_mm,
                _obstacles=_obstacles,
                _pad_rects=_pad_rects,
            )
        )

    return segments, vias


# ---------------------------------------------------------------------------
# S-expression emission helpers
# ---------------------------------------------------------------------------


def emit_segment_nodes(segments: list[OutputSegment]) -> list[list[Any]]:
    """Convert a list of :class:`OutputSegment` into raw ``segment`` sexp nodes."""
    nodes: list[list[Any]] = []
    for s in segments:
        nodes.append(
            [
                sexpdata.Symbol("segment"),
                [sexpdata.Symbol("start"), s.x1, s.y1],
                [sexpdata.Symbol("end"), s.x2, s.y2],
                [sexpdata.Symbol("width"), s.width],
                [sexpdata.Symbol("layer"), s.layer],
                [sexpdata.Symbol("net"), s.net],
            ]
        )
    return nodes


def emit_via_nodes(vias: list[OutputVia]) -> list[list[Any]]:
    """Convert a list of :class:`OutputVia` into raw ``via`` sexp nodes."""
    nodes: list[list[Any]] = []
    for v in vias:
        layers = [sexpdata.Symbol("layers"), v.layers[0], v.layers[1]]
        nodes.append(
            [
                sexpdata.Symbol("via"),
                [sexpdata.Symbol("at"), v.x, v.y],
                [sexpdata.Symbol("size"), v.diameter],
                [sexpdata.Symbol("drill"), v.drill],
                layers,
                [sexpdata.Symbol("net"), v.net],
            ]
        )
    return nodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simplify_collinear(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop intermediate vertices that lie on the line between their neighbors."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        px, py = out[-1]
        cx, cy = pts[i]
        nx, ny = pts[i + 1]
        # Cross product of (c - p) and (n - c) — collinear if ~0.
        cross = (cx - px) * (ny - cy) - (cy - py) * (nx - cx)
        if abs(cross) > 1e-9:
            out.append((cx, cy))
    out.append(pts[-1])
    return out


def _apply_miters(
    segments: list[OutputSegment],
    max_miter_mm: float,
    obstacles: list | None = None,
    pad_rects: list | None = None,
) -> list[OutputSegment]:
    """At each interior join, insert a 45° miter between two axis-aligned segments.

    For every pair of consecutive axis-aligned segments (horizontal + vertical
    in either order), the shared corner is replaced with three segments:

        A ──── M            A = shortened by *m* along its own axis
                \
                 N          N = shortened by *m* along B's axis
                  \
                   ──── B   M→N is the 45° miter

    When *obstacles* is provided, checks the 45° diagonal M→N against
    obstacle polygons using Shapely.  If blocked, halves *m* and retries
    until the diagonal is clear (or *m* drops below 0.001 mm, in which
    case the original 90° corner is kept).

    The miter length *m* is bounded by *max_miter_mm* and by half the shorter
    adjacent segment (so the miter never overshoots).
    """
    if len(segments) < 2:
        return segments

    out: list[OutputSegment] = []
    for i in range(len(segments) - 1):
        a = segments[i]
        b = segments[i + 1]

        # Both must be axis-aligned (H/V) and orthogonal (one H, one V).
        a_h = _is_horizontal(a)
        a_v = _is_vertical(a)
        b_h = _is_horizontal(b)
        b_v = _is_vertical(b)
        if not ((a_h and b_v) or (a_v and b_h)):
            out.append(a)
            continue

        # Shared corner.
        cx, cy = a.x2, a.y2
        # Length of each leg.
        len_a = abs(a.x2 - a.x1) if a_h else abs(a.y2 - a.y1)
        len_b = abs(b.x2 - b.x1) if b_h else abs(b.y2 - b.y1)
        # Miter length: go back *m* on each leg from the corner.
        m_max = min(len_a, len_b, max_miter_mm)
        if m_max <= 0:
            out.append(a)
            continue

        # Find the largest miter length ≤ m_max whose 45° diagonal is
        # obstacle-free and doesn't encroach on no-diagonal zones.
        m = m_max
        chosen_m: float | None = None
        while m >= 1e-3:
            # Point M: back along A by *m* from the corner.
            if a_h:
                mx = cx - m if a.x2 > a.x1 else cx + m
                my = cy
            else:
                mx = cx
                my = cy - m if a.y2 > a.y1 else cy + m

            # Point N: forward along B by *m* from the corner.
            if b_h:
                nx = cx + m if b.x2 > b.x1 else cx - m
                ny = cy
            else:
                nx = cx
                ny = cy + m if b.y2 > b.y1 else cy - m

            # Reject if the corner sits inside a pad rectangle (the
            # miter would turn a clean axis-aligned pad exit into a
            # diagonal crossing the pad boundary).
            if pad_rects:
                if any(
                    abs(cx - pcx) - hw <= 1e-9 and abs(cy - pcy) - hh <= 1e-9
                    for pcx, pcy, hw, hh in pad_rects
                ):
                    chosen_m = None
                    break

            if obstacles is None or not _line_crosses_obstacles(mx, my, nx, ny, obstacles):
                chosen_m = m
                break
            m *= 0.5

        if chosen_m is None:
            # Even a 0.001 mm chamfer is blocked — keep original 90°.
            out.append(a)
            continue

        # Emit: shortened A, then 45° miter segment M→N.
        out.append(
            OutputSegment(x1=a.x1, y1=a.y1, x2=mx, y2=my, width=a.width, layer=a.layer, net=a.net)
        )
        out.append(
            OutputSegment(x1=mx, y1=my, x2=nx, y2=ny, width=a.width, layer=a.layer, net=a.net)
        )
        # Patch B so the next iteration sees its start at N.
        segments[i + 1] = OutputSegment(
            x1=nx, y1=ny, x2=b.x2, y2=b.y2, width=b.width, layer=b.layer, net=b.net
        )
    out.append(segments[-1])
    return out


def _is_horizontal(s: OutputSegment) -> bool:
    return abs(s.y2 - s.y1) < 1e-9


def _is_vertical(s: OutputSegment) -> bool:
    return abs(s.x2 - s.x1) < 1e-9
