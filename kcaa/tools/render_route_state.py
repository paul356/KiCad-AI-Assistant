"""
Render-first failure evidence for PNS route attempts (W1).

Turns a route attempt into a PNG over the board render: the grey attempted
skeleton, red-highlighted blocking items (with ``ref / net`` text labels)
and green numbered anchors.  Structured data survives only as annotation on
the image — this is the feedback channel the VLM reads (docs/plans/
pns-vlm-routing.md Part II §8).

Pure functions, no MCP registration needed.  Reuses the board drawing
facilities from :mod:`kcaa.tools.render_board_tools` (``parse_board``,
``_new_board_figure``, ``_draw_board_layers``).
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.patheffects as mpatheffects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from kcaa.tools.render_board_tools import (
    _BG_COLOR,
    _PT_PER_MM,
    _bounds,
    _draw_board_layers,
    _new_board_figure,
    parse_board,
)

# Overlay zorders — all above the static board layers (max _Z_PAD_LABEL 9).
_Z_SKELETON = 10
_Z_BLOCKING = 11
_Z_ANNOTATION = 12

_SKELETON_COLOR = "#C8C8C8"
_BLOCKING_COLOR = "#FF3B30"
_ANCHOR_COLOR = "#34C759"
# Near-background stroke so annotation text stays readable over light fills.
_ANNOT_STROKE = "#101216"
_BLOCKING_RING_MM = 0.8  # highlight radius at a blocking point, mm
_VIA_RING_MM = 0.55  # tighter ring for via blockers


@dataclass
class BlockingEvidence:
    """One obstacle that blocked a route attempt.

    ``kind`` picks the highlight shape: ``"footprint"`` draws the outline of
    ``points`` (fallback: a ring at ``point``), ``"track"`` a red ring on the
    offending segment, ``"via"`` a hollow red circle at the via center.
    """

    ref: str  # footprint reference, e.g. "R5"
    net: str | None  # net name, e.g. "VCC" (used for the text label)
    layer: str | None  # copper layer the obstacle lives on
    point: tuple[float, float]  # blocking position, mm
    kind: str = "track"  # "track" | "footprint" | "via"
    points: list[tuple[float, float]] | None = None  # outline for footprints


def _annot_text(ax, x: float, y: float, text: str, color: str, size_mm: float) -> None:
    """Small annotation text with a near-background stroke."""
    ax.text(
        x,
        y,
        text,
        ha="left",
        va="top",
        fontsize=size_mm * _PT_PER_MM,
        color=color,
        zorder=_Z_ANNOTATION,
        path_effects=[mpatheffects.withStroke(linewidth=0.3, foreground=_ANNOT_STROKE)],
    )


def _draw_blocking(ax, item: BlockingEvidence) -> None:
    if item.kind == "footprint" and len(item.points or []) >= 3:
        ax.add_patch(
            mpatches.Polygon(
                item.points,
                closed=True,
                fill=False,
                edgecolor=_BLOCKING_COLOR,
                linewidth=0.3 * _PT_PER_MM,
                zorder=_Z_BLOCKING,
            )
        )
    else:
        r = _VIA_RING_MM if item.kind == "via" else _BLOCKING_RING_MM
        ax.add_patch(
            mpatches.Circle(
                item.point,
                r,
                fill=False,
                edgecolor=_BLOCKING_COLOR,
                linewidth=0.3 * _PT_PER_MM,
                zorder=_Z_BLOCKING,
            )
        )
    label = f"{item.ref} / {item.net}" if item.net else item.ref
    _annot_text(
        ax,
        item.point[0] + _BLOCKING_RING_MM,
        item.point[1] - _BLOCKING_RING_MM,
        label,
        _BLOCKING_COLOR,
        0.4,
    )


def render_route_attempt(
    pcb_path: str,
    *,
    attempted_path: list[tuple[float, float]] | None = None,
    blocking_items: list[BlockingEvidence] | None = None,
    anchors: list[tuple[float, float]] | None = None,
    dpi: int = 200,
) -> tuple[list[str], bytes, dict[str, Any]]:
    """Render a board with route-attempt evidence overlayed.

    * ``attempted_path`` — grey semi-transparent polyline: the skeleton the
      engine walked before failing.
    * ``blocking_items`` — red highlights at the obstacles that blocked the
      route, each with a ``ref / net`` text label.
    * ``anchors`` — green dots numbered 1..N at pad/waypoint/via positions.

    Any or all of these may be empty or missing: the result then degenerates
    to a plain board render (never errors).

    Returns ``(report_lines, png_bytes, report_dict)`` like
    :func:`kcaa.tools.render_board_tools.render_board`.
    """
    board = parse_board(pcb_path)

    # Frame: board bounds, widened to include any overlay geometry.
    xmin, ymin, xmax, ymax = _bounds(board, [])
    xs_ovl: list[float] = []
    ys_ovl: list[float] = []
    for pt in attempted_path or []:
        xs_ovl.append(pt[0])
        ys_ovl.append(pt[1])
    for b in blocking_items or []:
        xs_ovl.append(b.point[0])
        ys_ovl.append(b.point[1])
        for pt in b.points or []:
            xs_ovl.append(pt[0])
            ys_ovl.append(pt[1])
    for a in anchors or []:
        xs_ovl.append(a[0])
        ys_ovl.append(a[1])
    if xs_ovl:
        xmin = min(xmin, min(xs_ovl) - 2.0)
        ymin = min(ymin, min(ys_ovl) - 2.0)
        xmax = max(xmax, max(xs_ovl) + 2.0)
        ymax = max(ymax, max(ys_ovl) + 2.0)
    fig, ax, eff_dpi = _new_board_figure(xmin, ymin, xmax, ymax, dpi)

    _draw_board_layers(ax, board, layer=None, show_pad_labels=False)

    # Grey attempted skeleton — above the board, below the evidence.
    pts = attempted_path or []
    if len(pts) >= 2:
        ax.plot(
            [pt[0] for pt in pts],
            [pt[1] for pt in pts],
            color=_SKELETON_COLOR,
            alpha=0.6,
            linewidth=0.35 * _PT_PER_MM,
            solid_capstyle="round",
            zorder=_Z_SKELETON,
        )

    # Red blocking highlights.
    for item in blocking_items or []:
        _draw_blocking(ax, item)

    # Green numbered anchors.
    for i, a in enumerate(anchors or [], start=1):
        ax.add_patch(
            mpatches.Circle(
                a,
                0.3,
                facecolor=_ANCHOR_COLOR,
                edgecolor="white",
                linewidth=0.15,
                zorder=_Z_ANNOTATION,
            )
        )
        _annot_text(ax, a[0] + 0.5, a[1] - 0.5, str(i), _ANCHOR_COLOR, 0.35)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG_COLOR, dpi=eff_dpi)
    plt.close(fig)

    blocking = blocking_items or []
    anchors_ = anchors or []
    report: dict[str, Any] = {
        "pads": len(board.pads),
        "copper_layers": board.copper_layers,
        "attempted_path_points": len(pts),
        "blocking_items": len(blocking),
        "anchors": len(anchors_),
    }
    lines = [
        f"Rendered route attempt on {os.path.basename(pcb_path)}: "
        f"{len(board.pads)} pads, skeleton points={len(pts)}, "
        f"blocking items={len(blocking)}, anchors={len(anchors_)}."
    ]
    for item in blocking:
        label = f"{item.ref} / {item.net}" if item.net else item.ref
        lines.append(
            f"  blocker: {label} at ({item.point[0]:.3f},{item.point[1]:.3f}) "
            f"layer={item.layer or 'any'} kind={item.kind}"
        )
    return lines, buf.getvalue(), report
