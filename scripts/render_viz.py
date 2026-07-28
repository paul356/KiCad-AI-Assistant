#!/usr/bin/env python3
"""
Render visualization dumps from the router pipeline as images.

Usage:
    python render_viz.py /tmp/kcaa_viz/

Depends on: matplotlib, shapely
"""

from __future__ import annotations

import glob
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
from matplotlib.collections import LineCollection
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt


def _segment_colors(pts: list[tuple[float, float]]) -> list:
    """Color each segment by angle: blue=horiz, green=vert, red=diag, gray=other."""
    cols = []
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        if abs(dy) < 1e-6:
            cols.append("#1f77b4")  # blue - horiz
        elif abs(dx) < 1e-6:
            cols.append("#2ca02c")  # green - vert
        elif abs(abs(dx) - abs(dy)) < 1e-6:
            cols.append("#d62728")  # red - diag
        else:
            cols.append("#7f7f7f")  # gray - other
    return cols


def _render_stage(fname: str, out_dir: str) -> None:
    """Render a single stage JSON file to a PNG."""
    with open(fname) as f:
        data = json.load(f)

    stage = data["stage"]
    path = data["path"]
    pads = data["pads"]
    obstacles = data.get("obstacles", [])

    fig, ax = plt.subplots(1, 1, figsize=(14, 10))
    ax.set_aspect("equal")

    # Filter obstacles to only those near the path (culling invisible ones).
    if obstacles:
        # Get path bounding box.
        px = [p[0] for p in path]
        py = [p[1] for p in path]
        p_minx, p_maxx = min(px), max(px)
        p_miny, p_maxy = min(py), max(py)
        margin = 5.0  # mm around path
        near_obs = []
        for coords, kind, ref in obstacles:
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            ox_min, ox_max = min(xs), max(xs)
            oy_min, oy_max = min(ys), max(ys)
            if (
                ox_max >= p_minx - margin
                and ox_min <= p_maxx + margin
                and oy_max >= p_miny - margin
                and oy_min <= p_maxy + margin
            ):
                near_obs.append((coords, kind, ref))
    else:
        near_obs = []

    # Obstacles (faint gray fill, near path only).
    for coords, kind, ref in near_obs[:60]:
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        ax.fill(xs, ys, alpha=0.05, color="#888888", linewidth=0.2, edgecolor="#cccccc")

    # Pad AABBs (thick rectangles).
    for name, aabb in pads:
        minx, miny, maxx, maxy = aabb
        rect = mpatches.Rectangle(
            (minx, miny),
            maxx - minx,
            maxy - miny,
            linewidth=2.5,
            edgecolor="#ff7f0e",
            facecolor="none",
            linestyle="--",
        )
        ax.add_patch(rect)
        ax.text(
            (minx + maxx) / 2,
            maxy + 0.15,
            name,
            ha="center",
            va="bottom",
            fontsize=8,
            color="#ff7f0e",
        )

    # Path with segment-colored lines.
    if len(path) >= 2:
        segs = []
        cols = _segment_colors(path)
        for i in range(len(path) - 1):
            segs.append([(path[i][0], path[i][1]), (path[i + 1][0], path[i + 1][1])])
        lc = LineCollection(segs, colors=cols, linewidths=3.5, zorder=5)
        ax.add_collection(lc)

    # Point markers (larger, all points numbered).
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    ax.scatter(xs, ys, s=40, c="#1f77b4", zorder=6, edgecolors="white", linewidths=0.8)
    for idx in range(len(path)):
        ax.annotate(
            str(idx),
            (path[idx][0], path[idx][1]),
            textcoords="offset points",
            xytext=(4, 4),
            fontsize=6,
            color="#333333",
            fontweight="bold",
        )

    # Zoom to path + pads, not the full route bbox.
    all_xs = [p[0] for p in path]
    all_ys = [p[1] for p in path]
    for _name, aabb in pads:
        all_xs.extend([aabb[0], aabb[2]])
        all_ys.extend([aabb[1], aabb[3]])
    margin = 1.5  # mm
    ax.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
    ax.set_ylim(max(all_ys) + margin, min(all_ys) - margin)  # flip Y for KiCad
    ax.invert_yaxis()

    ax.set_title(f"Stage: {stage}  ({len(path)} pts)", fontsize=12)
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")

    # Legend
    legend_patches = [
        mpatches.Patch(color="#1f77b4", label="Horiz"),
        mpatches.Patch(color="#2ca02c", label="Vert"),
        mpatches.Patch(color="#d62728", label="45° diag"),
        mpatches.Patch(color="#7f7f7f", label="Other"),
        mpatches.Patch(color="#ff7f0e", alpha=0.3, label="Pad AABB"),
    ]
    ax.legend(handles=legend_patches, loc="upper right", fontsize=8)

    out_path = os.path.join(out_dir, f"{stage}.png")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  → {out_path}")


def main():
    viz_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/kcaa_viz"
    out_dir = os.path.join(viz_dir, "png")
    os.makedirs(out_dir, exist_ok=True)

    json_files = sorted(glob.glob(os.path.join(viz_dir, "*.json")))
    if not json_files:
        print(f"No JSON files found in {viz_dir}")
        sys.exit(1)

    print(f"Rendering {len(json_files)} stages from {viz_dir} → {out_dir}")
    for fname in json_files:
        try:
            _render_stage(fname, out_dir)
        except Exception as exc:
            print(f"  FAILED {fname}: {exc}")

    print("Done.")


if __name__ == "__main__":
    main()
