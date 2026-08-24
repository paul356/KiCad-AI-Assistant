#!/usr/bin/env python3
"""
Render visualization dumps from the router pipeline as images.

Usage:
    python render_viz.py <viz_dir> [run_prefix]

Scans <viz_dir> recursively. PCB pipeline dumps land under
kcaa_viz/pcb_viz/, schematic routing dumps under kcaa_viz/sch_viz/; PNGs
are written beside each JSON in a png/ subdirectory.

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


def _render_scene(data: dict, out_path: str, highlight: int | None = None) -> None:
    """Draw one scene from a stage JSON to *out_path*.

    ``highlight`` optionally selects one rejected candidate (index into
    ``data["candidates"]``) to draw thick red; the others are shown as faint
    gray reference lines so a single failure can be inspected in isolation.
    """
    stage = data["stage"]
    path = data.get("path") or []
    pads = data.get("pads") or []
    obstacles = data.get("obstacles", [])
    candidates = data.get("candidates") or []
    notes = data.get("notes") or ""

    fig, ax = plt.subplots(1, 1, figsize=(14, 10))
    ax.set_aspect("equal")

    # Filter obstacles to only those near the path (culling invisible ones).
    if obstacles:
        if path:
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
            near_obs = list(obstacles)
    else:
        near_obs = []

    # Pin symbol stubs: the lib-defined pin lines (2.54–3.81 mm) that wires
    # must not run on top of.  Drawn as thin blue lines at a low zorder.
    for sx0, sy0, sx1, sy1 in data.get("pin_stubs") or []:
        ax.plot(
            [sx0, sx1],
            [sy0, sy1],
            color="#2f6fd0",
            linewidth=1.0,
            alpha=0.7,
            zorder=1,
        )

    # Obstacles (gray fill, near path only). Dark enough to read as existing
    # wires while staying below the candidate/path zorder.
    for coords, kind, ref in near_obs[:60]:
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        ax.fill(xs, ys, alpha=0.3, color="#666666", linewidth=0.4, edgecolor="#999999")

    # Rejected routing candidates (schematic dumps): thin lines + reason labels.
    for i, cand in enumerate(candidates):
        reasons = cand.get("reasons") or []
        if not reasons:
            continue  # selected candidate is drawn as the main path
        segs = [
            [(s[0], s[1]), (s[2], s[3])]
            for s in cand.get("segments", [])
            if abs(s[0] - s[2]) > 1e-9 or abs(s[1] - s[3]) > 1e-9
        ]
        if not segs:
            continue
        if highlight is not None and i == highlight:
            lc = LineCollection(segs, colors="#d62728", linewidths=2.4, alpha=0.95, zorder=4)
            ax.add_collection(lc)
            label = ",".join(reasons)
            mid = segs[len(segs) // 2][0]
            ax.annotate(
                label,
                mid,
                textcoords="offset points",
                xytext=(3, -9),
                fontsize=8,
                color="#8a2020",
                fontweight="bold",
            )
        elif highlight is None:
            # Overview: all rejected candidates in thin red with reason labels.
            lc = LineCollection(segs, colors="#d62728", linewidths=1.0, alpha=0.45, zorder=2)
            ax.add_collection(lc)
            label = ",".join(reasons)
            mid = segs[len(segs) // 2][0]
            ax.annotate(
                label,
                mid,
                textcoords="offset points",
                xytext=(3, -9),
                fontsize=6,
                color="#8a2020",
            )
        else:
            # Split view: other candidates are faint gray reference lines.
            lc = LineCollection(segs, colors="#999999", linewidths=0.6, alpha=0.35, zorder=2)
            ax.add_collection(lc)

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
        if name:
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
    if path:
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

    # Zoom bounds: min/max extent of ALL segments (path + candidate routes),
    # padded by a buffer so wire collisions outside the pads stay visible.
    # (Pads seed the view when a stage has no segments, e.g. lead-rejected pairs.)
    all_xs = [p[0] for p in path]
    all_ys = [p[1] for p in path]
    for _name, aabb in pads:
        all_xs.extend([aabb[0], aabb[2]])
        all_ys.extend([aabb[1], aabb[3]])
    for cand in candidates:
        for s in cand.get("segments", []):
            all_xs.extend([s[0], s[2]])
            all_ys.extend([s[1], s[3]])
    margin = 3.0  # mm buffer around the visible extent
    ax.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
    ax.set_ylim(min(all_ys) - margin, max(all_ys) + margin)
    ax.invert_yaxis()  # KiCad +Y=down

    title = f"Stage: {stage}  ({len(path)} pts)"
    if highlight is not None:
        hl_reasons = ",".join(candidates[highlight].get("reasons") or [])
        title += f" — cand {highlight + 1}/{len([c for c in candidates if c.get('reasons')])}: {hl_reasons}"
    if notes:
        title += f" — {notes}" if len(title) + len(notes) < 110 else ""
    ax.set_title(title, fontsize=12)
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")

    # Legend
    legend_patches = [
        mpatches.Patch(color="#1f77b4", label="Horiz"),
        mpatches.Patch(color="#2ca02c", label="Vert"),
        mpatches.Patch(color="#d62728", label="45° diag"),
        mpatches.Patch(color="#7f7f7f", label="Other"),
        mpatches.Patch(color="#ff7f0e", alpha=0.3, label="Pad AABB"),
        mpatches.Patch(color="#d62728", alpha=0.45, label="Rejected candidate"),
        mpatches.Patch(color="#999999", alpha=0.35, label="Other candidates"),
    ]
    ax.legend(handles=legend_patches, loc="upper right", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  → {out_path}")


def _render_stage(fname: str, out_dir: str) -> None:
    """Render a single stage JSON file to an overview PNG.

    Handles both formats:
    * PCB pipeline dumps: ``path`` polyline + ``pads`` AABBs + obstacle
      polygons (``kcaa/router/router.py``).
    * Schematic dumps: same base fields (pins become small ``pads``, wires
      become thin ``obstacles``) plus an optional ``candidates`` list of
      rejected routes with reasons and a ``notes`` string
      (``kcaa/tools/wire_edit_tools.py``).
    """
    with open(fname) as f:
        data = json.load(f)

    stage = data["stage"]
    ts = os.path.basename(fname).split("_")[0]
    _render_scene(data, os.path.join(out_dir, f"{ts}_{stage}.png"))

    # One image per rejected candidate so overlapping failures (the red
    # "grid") can be inspected in isolation.
    rejected = [
        (i, c)
        for i, c in enumerate(data.get("candidates") or [])
        if (c.get("reasons") or [])
        and any(abs(s[0] - s[2]) > 1e-9 or abs(s[1] - s[3]) > 1e-9 for s in c.get("segments", []))
    ]
    if len(rejected) > 1:
        for idx, (_i, _c) in enumerate(rejected):
            base = os.path.basename(fname)[:-5]  # strip .json
            out_path = os.path.join(out_dir, f"{base}_cand{idx + 1}.png")
            _render_scene(data, out_path, highlight=_i)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <viz_dir> [run_prefix]")
        print("  viz_dir     Path to a viz directory. May be the top kcaa_viz")
        print("              dir (recurses into sch_viz/ pcb_viz/) or a single")
        print("              subdir such as kcaa_viz/sch_viz")
        print("  run_prefix  Optional: render only runs matching this prefix (e.g. 102451)")
        sys.exit(1)
    viz_dir = sys.argv[1]

    json_files = sorted(glob.glob(os.path.join(viz_dir, "**", "*.json"), recursive=True))
    if not json_files:
        print(f"No JSON files found in {viz_dir}")
        sys.exit(1)

    # Group by timestamp prefix (first 6 chars HHMMSS).
    from collections import defaultdict

    groups: dict[str, list[str]] = defaultdict(list)
    for fname in json_files:
        base = os.path.basename(fname)
        ts = base[:6] if "_" in base else base
        groups[ts].append(fname)

    target_ts = sys.argv[2] if len(sys.argv) > 2 else max(groups.keys())
    # Allow partial match — match all groups starting with the given prefix.
    matched = {ts: files for ts, files in groups.items() if ts.startswith(target_ts)}

    if not matched:
        print(f"No runs matching '{target_ts}'. Available: {', '.join(sorted(groups))}")
        sys.exit(1)

    older = len(json_files) - sum(len(v) for v in matched.values())
    if older:
        print(f"Ignoring {older} files from other runs.")

    for ts in sorted(matched):
        print(f"Rendering {len(matched[ts])} stages ({ts})")
        for fname in matched[ts]:
            out_dir = os.path.join(os.path.dirname(fname), "png")
            os.makedirs(out_dir, exist_ok=True)
            try:
                _render_stage(fname, out_dir)
            except Exception as exc:
                print(f"  FAILED {fname}: {exc}")

    print("Done.")


if __name__ == "__main__":
    main()
