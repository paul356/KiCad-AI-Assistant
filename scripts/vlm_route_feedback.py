#!/usr/bin/env python3
"""
VLM-feedback routing experiment driver (issue #124).

Closes the loop between a vision LLM and the existing A* router:

    render board state -> ask VLM (image + prompt) -> parse feedback
    -> auto_route_pair -> success: write to PCB, render new state, next round
                       -> failure: feed RouteFailure back, VLM adjusts, retry

Feedback dimensions (all within existing ``RouteRequest`` knobs):
    * routing order  — which pad pair to connect next
    * layer choice   — ``layer_hint`` for thru-hole pads (SMD layers are fixed)
    * retry strategy — layer hint / pair choice after a RouteFailure

Output protocol expected from the VLM (one line each):

    route: <ref_a>.<pad_a> -> <ref_b>.<pad_b>
    layer: <F.Cu|B.Cu|auto>          (thru-hole only; ignored for SMD)
    reason: <one sentence>

On failure the prompt is extended with:

    last_error: <RouteFailure message>
    advice: <layer / order / give-up recommendation>

Usage:
    export LARK_LLM_BASE_URL, LARK_LLM_MODEL, LARK_LLM_API_KEY
    python scripts/vlm_route_feedback.py                  # default test board
    python scripts/vlm_route_feedback.py --pcb X.kicad_pcb --rounds 10
    python scripts/vlm_route_feedback.py --dry-run        # no LLM: fixed order

Dependencies: matplotlib, shapely, sexpdata (repo already requires them).
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
import sys
import tempfile
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# Make the repo root importable (script lives in scripts/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from kcaa.router.router import (  # noqa: E402
    RouteFailure,
    RouteRequest,
    auto_route_pair,
)
from kcaa.tools.pcb_routing_tools import (  # noqa: E402
    _segment_to_sexp,
    _via_to_sexp,
)
from kcaa.utils.pcb_sexp_utils import load_pcb, save_pcb  # noqa: E402

DEFAULT_PCB = os.path.join(
    _REPO_ROOT, "tests", "integration", "fixtures", "test_routing_board.kicad_pcb"
)

# Layer hops the router may use for multi-layer routes on the experiment
# board (F.Cu top, B.Cu bottom, In1.Cu inner).  Pass --via-pairs to override.
DEFAULT_VIA_PAIRS: tuple[tuple[str, str], ...] = (("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu"))

_SYSTEM_PROMPT = """You are a PCB routing planner. The image shows the world
model of a printed circuit board as several panels, one per copper layer
(F.Cu, B.Cu, In1.Cu, ...). The render follows the KiCad default colour
theme (dark background).

Image encoding:
- Traces, pads and via rings are coloured by copper layer: F.Cu = red,
  B.Cu = blue, inner layers = green/amber/purple (legend in the header).
- Copper pads are rectangles labelled ref.pad (e.g. "R1.1").
- Dashed white lines connect pad pairs that still need routing.
- Grey hatched polygons are keepout zones; vias are dark rings with a
  layer-coloured edge; light grey outlines are component bodies.

Your job: decide what to route next. Reply with exactly three lines:

  route: <ref_a>.<pad_a> -> <ref_b>.<pad_b>
  layer: <F.Cu|B.Cu|auto>
  reason: <one short sentence>

Rules:
- Connect one pad pair that is still unconnected (from the asked list).
- If you are given a last_error and asked for advice, either change the
  layer (thru-hole only), pick a different pair, or give up on that pair.
- Pads on different nets cannot be connected: only connect pairs from the
  asked list.
- Do not invent pad numbers that were not shown."""  # noqa: E501

# KiCad default theme approximations: copper layer colours, dark canvas.
_KICAD_LAYER_COLORS = {
    "F.Cu": "#E31A1C",
    "B.Cu": "#1E5BC6",
    "In1.Cu": "#2E9E44",
    "In2.Cu": "#E6A71D",
    "In3.Cu": "#9B59B6",
    "In4.Cu": "#0FA3B1",
    "Edge.Cuts": "#F2DA57",
}
_BG_COLOR = "#17181D"
_BODY_COLOR = "#C9C9C9"
_ZONE_FACE = "#26262B"
_ZONE_EDGE = "#707070"
_VIA_FACE = "#1F1F23"
_PENDING_COLOR = "#F0F0F0"


def _sym(value: Any) -> str:
    """Return the string form of a sexpdata Symbol or plain string.

    sexpdata.Symbol subclasses str but overrides ``__eq__``, so
    ``Symbol('pad') == 'pad'`` is False.  Always compare via this helper.
    """
    return str(value)


def _layer_color(layer: str) -> str:
    return _KICAD_LAYER_COLORS.get(layer, "#9A9A9A")


@dataclass
class VLMFeedback:
    """Parsed semantic feedback from the VLM."""

    route_a: str | None = None
    route_b: str | None = None
    layer: str | None = None
    reason: str = ""


def parse_feedback(text: str) -> VLMFeedback:
    """Parse the VLM's reply into structured feedback.

    Accepts the documented ``route:`` / ``layer:`` / ``reason:`` lines
    regardless of surrounding prose.  Returns a :class:`VLMFeedback` with
    ``None`` pads when no ``route:`` line is found.
    """
    fb = VLMFeedback()
    for line in text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("route:"):
            value = stripped[len("route:") :].strip()
            parts = [p.strip() for p in value.replace("->", "→").replace("→", " ").split()]
            # Only pad specs carry a dot ("R1.1"); drop separators ("-", "to").
            parts = [p for p in parts if "." in p]
            if len(parts) >= 2:
                fb.route_a = parts[0]
                fb.route_b = parts[1]
        elif low.startswith("layer:"):
            fb.layer = stripped[len("layer:") :].strip()
        elif low.startswith("reason:"):
            fb.reason = stripped[len("reason:") :].strip()
    return fb


def parse_pad(spec: str) -> tuple[str, str]:
    """Split ``R1.1`` into ``("R1", "1")``.  Raises ValueError on bad input."""
    if "." not in spec:
        raise ValueError(f"expected '<ref>.<pad>', got {spec!r}")
    ref, pad = spec.rsplit(".", 1)
    ref = ref.strip()
    pad = pad.strip()
    if not ref or not pad:
        raise ValueError(f"empty ref/pad in {spec!r}")
    return ref, pad


@dataclass
class PairSpec:
    """One pad pair to route, plus the net that joins them."""

    ref_a: str
    pad_a: str
    ref_b: str
    pad_b: str
    net: str
    via_pairs: tuple[tuple[str, str], ...] | None = None
    attempts: int = 0
    status: str = "pending"  # pending | done | failed

    @property
    def key(self) -> str:
        return f"{self.ref_a}.{self.pad_a}-{self.ref_b}.{self.pad_b}"

    @property
    def description(self) -> str:
        return f"{self.ref_a}.{self.pad_a} -> {self.ref_b}.{self.pad_b} (net {self.net})"


class VLMClient:
    """Minimal OpenAI-compatible chat client for the experiment.

    Sends one image + text prompt and returns the assistant text.  Uses
    stdlib ``urllib`` only, configured from environment variables so the
    script needs no config-file plumbing:

    - ``LARK_LLM_BASE_URL``  (default https://api.openai.com/v1)
    - ``LARK_LLM_MODEL``     (default gpt-4o)
    - ``LARK_LLM_API_KEY``   (default empty)
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("LARK_LLM_BASE_URL", "https://api.openai.com/v1")
        self.model = os.environ.get("LARK_LLM_MODEL", "gpt-4o")
        self.api_key = os.environ.get("LARK_LLM_API_KEY", "")

    def ask(self, png_bytes: bytes, user_prompt: str) -> str:
        """Send a vision request; return the assistant's text reply."""
        import urllib.request

        b64 = base64.b64encode(png_bytes).decode("ascii")
        content: list[dict[str, Any]] = [
            {"type": "text", "text": user_prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
        ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": 512,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"

        req = urllib.request.Request(  # nosec B310 -- user-configured LLM endpoint
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"VLM request failed: {exc}") from exc

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected VLM response: {body!r}") from exc


def _footprint_ref(fp: list) -> str | None:
    for sub in fp:
        if (
            isinstance(sub, list)
            and len(sub) >= 3
            and _sym(sub[0]) == "property"
            and sub[1] == "Reference"
        ):
            return sub[2]
    return None


def _footprint_place(fp: list) -> tuple[float, float, float]:
    """World (x, y, rotation) of a footprint, from its (at ...) node."""
    for sub in fp:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "at":
            try:
                x = float(sub[1])
                y = float(sub[2])
            except (TypeError, ValueError):
                return (0.0, 0.0, 0.0)
            rot = float(sub[3]) if len(sub) >= 4 else 0.0
            return (x, y, rot)
    return (0.0, 0.0, 0.0)


def _pad_net(pad: list) -> str | None:
    for sub in pad:
        if isinstance(sub, list) and len(sub) >= 1 and _sym(sub[0]) == "net":
            if len(sub) >= 3:
                return str(sub[2])
            if len(sub) >= 2:
                return str(sub[1])
    return None


def collect_nets(pcb_path: str) -> dict[str, list[tuple[str, str]]]:
    """Map net name -> list of (ref, pad) belonging to it.

    Reads pads straight from the S-expression so we do not depend on the
    router's internals to enumerate what could be routed.
    """
    data = load_pcb(pcb_path)
    nets: dict[str, list[tuple[str, str]]] = {}
    for node in data:
        if not isinstance(node, list) or len(node) < 2:
            continue
        if _sym(node[0]) != "footprint":
            continue
        ref = _footprint_ref(node)
        if ref is None:
            continue
        for sub in node:
            if not isinstance(sub, list) or len(sub) < 2:
                continue
            if _sym(sub[0]) != "pad":
                continue
            pad_num = sub[1] if isinstance(sub[1], str) else str(sub[1])
            net = _pad_net(sub)
            if net:
                nets.setdefault(net, []).append((ref, pad_num))
    return nets


def routable_pairs(
    pcb_path: str, via_pairs: tuple[tuple[str, str], ...] | None = None
) -> list[PairSpec]:
    """Enumerate connectable pad pairs: every pair within a net with 2+ pads."""
    nets = collect_nets(pcb_path)
    pairs: list[PairSpec] = []
    for net, pads in sorted(nets.items()):
        pads_sorted = sorted(pads)
        for i in range(len(pads_sorted)):
            for j in range(i + 1, len(pads_sorted)):
                pairs.append(
                    PairSpec(
                        ref_a=pads_sorted[i][0],
                        pad_a=pads_sorted[i][1],
                        ref_b=pads_sorted[j][0],
                        pad_b=pads_sorted[j][1],
                        net=net,
                        via_pairs=via_pairs,
                    )
                )
    return pairs


def route_pair(pcb_path: str, pair: PairSpec, layer_hint: str | None = None) -> str | None:
    """Route one pair in place.  Returns None on success, error string on failure."""
    req = RouteRequest(
        pcb_path=pcb_path,
        ref_a=pair.ref_a,
        pad_a=pair.pad_a,
        ref_b=pair.ref_b,
        pad_b=pair.pad_b,
        net=pair.net,
        layer_hint=layer_hint,
        via_pairs=pair.via_pairs or DEFAULT_VIA_PAIRS,
    )
    try:
        result = auto_route_pair(req)
    except (RouteFailure, ValueError, RuntimeError) as exc:
        return str(exc)

    data = load_pcb(pcb_path)
    for seg in result.segments:
        data.append(_segment_to_sexp(seg))
    for via in result.vias:
        data.append(_via_to_sexp(via))
    try:
        save_pcb(pcb_path, data)
    except OSError as exc:
        return f"failed to write PCB: {exc}"
    return None


def _node_coord(node: list, name: str) -> tuple[float, float] | None:
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == name:
            try:
                return float(sub[1]), float(sub[2])
            except (TypeError, ValueError):
                return None
    return None


def _pad_geometry(pad: list, fp_at: tuple[float, float], fp_rot: float) -> Any:
    """Return a shapely box for a pad node in world coordinates, or None.

    KiCad stores pad ``(at ...)`` / ``(size ...)`` in the *footprint's*
    local frame; the pad centre must be rotated by the footprint rotation
    and translated by the footprint origin to land on the board.
    """
    from math import cos, radians, sin

    from shapely.geometry import box

    at = _node_coord(pad, "at")
    size = None
    for sub in pad:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "size":
            size = (float(sub[1]), float(sub[2]))
    if at is None or size is None:
        return None

    # Pad rotation (rare) rotates the pad within the footprint frame.
    pad_rot = 0.0
    for sub in pad:
        if isinstance(sub, list) and len(sub) >= 4 and _sym(sub[0]) == "at":
            try:
                pad_rot = float(sub[3])
            except (TypeError, ValueError):
                pad_rot = 0.0

    total_rot = pad_rot + fp_rot
    w, h = size
    tha = radians(total_rot)
    c, s = cos(tha), sin(tha)
    # Local pad centre, then rotate CCW (KiCad world is Y-down so CCW on
    # screen == CW in math coords — the board file convention) and translate.
    lx, ly = at[0], at[1]
    corners = [
        (lx - w / 2, ly - h / 2),
        (lx + w / 2, ly - h / 2),
        (lx + w / 2, ly + h / 2),
        (lx - w / 2, ly + h / 2),
    ]
    xs = [fp_at[0] + x * c - y * s for x, y in corners]
    ys = [fp_at[1] + x * s + y * c for x, y in corners]
    return box(min(xs), min(ys), max(xs), max(ys))


def _pad_center(data: list, ref: str, pad_num: str) -> tuple[float, float] | None:
    """World centre of a pad (footprint origin + rotated local centre)."""
    for node in data:
        if not isinstance(node, list) or len(node) < 2:
            continue
        if _sym(node[0]) != "footprint":
            continue
        if _footprint_ref(node) != ref:
            continue
        fp_at = _footprint_place(node)
        for sub in node:
            if not isinstance(sub, list) or len(sub) < 2:
                continue
            if _sym(sub[0]) != "pad":
                continue
            pnum = sub[1] if isinstance(sub[1], str) else str(sub[1])
            if pnum != pad_num:
                continue
            geo = _pad_geometry(sub, (fp_at[0], fp_at[1]), fp_at[2])
            if geo is None:
                return None
            c = geo.centroid
            return (c.x, c.y)
    return None


def _draw_pad(ax, geo: Any, layer: str, alpha: float = 1.0) -> None:
    color = _layer_color(layer)
    x, y = geo.bounds[0], geo.bounds[1]
    w = geo.bounds[2] - geo.bounds[0]
    h = geo.bounds[3] - geo.bounds[1]
    ax.add_patch(
        mpatches.Rectangle(
            (x, y),
            w,
            h,
            facecolor=color,
            edgecolor="black",
            linewidth=0.5,
            alpha=alpha,
            zorder=4,
        )
    )


_COPPER_ORDER = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")


def _draw_segment(ax, node: list, panel: str) -> None:
    """Draw a track/line node; only copper segments on *panel* are drawn."""
    start = _node_coord(node, "start")
    end = _node_coord(node, "end")
    if start is None or end is None:
        return
    layers = [str(s) for s in node]
    if "Edge.Cuts" in layers or "CrtYd" in layers:
        edge = "#F2DA57" if "Edge.Cuts" in layers else "#5A5A5A"
        ax.plot(
            [start[0], end[0]],
            [start[1], end[1]],
            color=edge,
            linewidth=0.6,
            alpha=0.9,
            zorder=1,
        )
        return
    layer = None
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
            layer = str(sub[1])
            break
    if layer != panel:
        return
    ax.plot(
        [start[0], end[0]],
        [start[1], end[1]],
        color=_layer_color(layer),
        linewidth=1.4,
        zorder=3,
    )


def _draw_via(ax, node: list, panel: str) -> None:
    """Draw a via as a dark ring with a layer-coloured edge (spans the stack)."""
    at = _node_coord(node, "at")
    if at is None:
        return
    diameter = 0.8
    for sub in node:
        if isinstance(sub, list) and len(sub) >= 3 and _sym(sub[0]) == "size":
            try:
                diameter = float(sub[1])
            except (TypeError, ValueError):
                pass
    ax.add_patch(
        mpatches.Circle(
            at,
            diameter / 2,
            facecolor=_VIA_FACE,
            edgecolor=_layer_color(panel),
            linewidth=1.2,
            zorder=4,
        )
    )


def _fp_shape_layer(shape: list) -> str | None:
    for sub in shape:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
            return str(sub[1])
    return None


def _footprint_copper_layers(fp: list, all_copper: list[str]) -> set[str]:
    """Copper layers a footprint has pads on (``*.Cu`` pads span the stack)."""
    layers: set[str] = set()
    for sub in fp:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "pad":
            layers.update(_pad_copper_layers(sub, all_copper))
    return layers


def _draw_footprint_body(ax, fp: list, panel: str, all_copper: list[str]) -> None:
    """Draw the component silhouette (silkscreen/courtyard) in world coords.

    The body is only drawn on panels where the footprint itself has copper;
    a component whose pads live on another layer must not leave a hollow
    outline on a layer it does not occupy.
    """
    from math import cos, radians, sin

    copper = _footprint_copper_layers(fp, all_copper)
    if panel not in copper:
        return
    fx, fy, rot = _footprint_place(fp)
    a = radians(rot)
    c, s = cos(a), sin(a)

    def wpt(x: float, y: float) -> tuple[float, float]:
        return (fx + x * c - y * s, fy + x * s + y * c)

    for sub in fp:
        if not isinstance(sub, list) or len(sub) < 4:
            continue
        kind = _sym(sub[0])
        if kind not in ("fp_line", "fp_rect", "fp_circle"):
            continue
        layer = _fp_shape_layer(sub)
        if layer is None or not (".SilkS" in layer or "CrtYd" in layer or ".Fab" in layer):
            continue
        if kind == "fp_line":
            start, end = _node_coord(sub, "start"), _node_coord(sub, "end")
            if start and end:
                p1, p2 = wpt(*start), wpt(*end)
                ax.plot(
                    [p1[0], p2[0]],
                    [p1[1], p2[1]],
                    color=_BODY_COLOR,
                    linewidth=0.6,
                    zorder=1,
                )
        elif kind == "fp_rect":
            start, end = _node_coord(sub, "start"), _node_coord(sub, "end")
            if start and end:
                x0, y0, x1, y1 = start[0], start[1], end[0], end[1]
                pts = [wpt(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
                xs, ys = zip(*pts)
                ax.plot(xs, ys, color=_BODY_COLOR, linewidth=0.6, zorder=1)
        elif kind == "fp_circle":
            center = _node_coord(sub, "center")
            if center is None:
                continue
            radius = None
            for ss in sub:
                if isinstance(ss, list) and len(ss) >= 3 and _sym(ss[0]) == "end":
                    try:
                        dx = float(ss[1]) - center[0]
                        dy = float(ss[2]) - center[1]
                        radius = (dx * dx + dy * dy) ** 0.5
                    except (TypeError, ValueError):
                        pass
            if radius is not None:
                ax.add_patch(
                    mpatches.Circle(
                        wpt(*center),
                        radius,
                        fill=False,
                        edgecolor=_BODY_COLOR,
                        linewidth=0.6,
                        zorder=1,
                    )
                )


def _zone_points(zone: list) -> list[tuple[float, float]]:
    """Polygon vertices of a zone/keepout node in world coordinates."""
    pts: list[tuple[float, float]] = []
    for sub in zone:
        if not isinstance(sub, list) or _sym(sub[0]) != "polygon":
            continue
        for pts_node in sub:
            if not isinstance(pts_node, list) or _sym(pts_node[0]) != "pts":
                continue
            for xy in pts_node[1:]:
                if isinstance(xy, list) and len(xy) >= 3 and _sym(xy[0]) == "xy":
                    try:
                        pts.append((float(xy[1]), float(xy[2])))
                    except (TypeError, ValueError):
                        pass
    return pts


def _zone_layer(zone: list) -> str | None:
    for sub in zone:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
            return str(sub[1])
    return None


def _draw_zone(ax, zone: list, panel: str) -> None:
    if _zone_layer(zone) != panel:
        return
    pts = _zone_points(zone)
    if len(pts) >= 3:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.fill(
            xs,
            ys,
            facecolor=_ZONE_FACE,
            edgecolor=_ZONE_EDGE,
            alpha=0.5,
            hatch="//",
            zorder=2,
        )


def _pad_layers(pad: list) -> list[str]:
    """Raw layer list of a pad node (copper + mask + paste)."""
    for sub in pad:
        if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layers":
            return [str(v) for v in sub[1:] if isinstance(v, str)]
    return []


def _pad_copper_layers(pad: list, all_copper: list[str]) -> list[str]:
    """Copper layers a pad occupies; ``*.Cu`` expands to the full stack."""
    out: list[str] = []
    for lay in _pad_layers(pad):
        if lay == "*.Cu":
            out.extend(all_copper)
        elif lay.endswith(".Cu") and lay not in out:
            out.append(lay)
    return list(dict.fromkeys(out))


def _copper_layers(data: list) -> list[str]:
    """Copper layers present on the board, in KiCad stack order."""
    found: set[str] = set()
    for node in data:
        if not isinstance(node, list) or len(node) < 2:
            continue
        head = _sym(node[0])
        if head == "segment":
            for sub in node:
                if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layer":
                    lay = str(sub[1])
                    if lay.endswith(".Cu"):
                        found.add(lay)
        elif head == "via":
            for sub in node:
                if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "layers":
                    for v in sub[1:]:
                        if isinstance(v, str) and v != "*.Cu" and v.endswith(".Cu"):
                            found.add(v)
        elif head == "footprint":
            for sub in node:
                if isinstance(sub, list) and len(sub) >= 2 and _sym(sub[0]) == "pad":
                    for lay in _pad_layers(sub):
                        if lay != "*.Cu" and lay.endswith(".Cu"):
                            found.add(lay)
    if not found:
        return ["F.Cu", "B.Cu"]
    return sorted(
        found,
        key=lambda l: _COPPER_ORDER.index(l) if l in _COPPER_ORDER else len(_COPPER_ORDER),
    )


def _collect_pads(data: list, all_copper: list[str]) -> list[dict[str, Any]]:
    """Pad info in world coords: shape, net, copper layers and centre."""
    out: list[dict[str, Any]] = []
    for node in data:
        if not isinstance(node, list) or len(node) < 2 or _sym(node[0]) != "footprint":
            continue
        ref = _footprint_ref(node)
        if ref is None:
            continue
        fp_at = _footprint_place(node)
        for sub in node:
            if not isinstance(sub, list) or len(sub) < 2 or _sym(sub[0]) != "pad":
                continue
            geo = _pad_geometry(sub, (fp_at[0], fp_at[1]), fp_at[2])
            if geo is None:
                continue
            out.append(
                {
                    "ref": ref,
                    "pad": str(sub[1]) if not isinstance(sub[1], list) else "",
                    "geo": geo,
                    "net": _pad_net(sub) or "?",
                    "layers": _pad_copper_layers(sub, all_copper),
                    "center": (geo.centroid.x, geo.centroid.y),
                    "fp_center": (fp_at[0], fp_at[1]),
                }
            )
    return out


def _annotate_pad(ax, pad: dict[str, Any]) -> None:
    """Label a pad away from its footprint centre; alternating pads on the
    same footprint are tilted up/down so neighbour labels do not collide."""
    cx, cy = pad["center"]
    fx, fy = pad.get("fp_center", (cx, cy))
    dx, dy = cx - fx, cy - fy
    norm = (dx * dx + dy * dy) ** 0.5 or 1.0
    ox, oy = 0.8 * dx / norm, 0.8 * dy / norm
    try:
        tilt = 0.4 if int(pad["pad"]) % 2 == 0 else -0.4
    except (TypeError, ValueError):
        tilt = 0.4
    ax.annotate(
        f"{pad['ref']}.{pad['pad']}",
        xy=(cx, cy),
        xytext=(cx + ox, cy + oy + tilt),
        fontsize=9,
        color="#F2F2F2",
        zorder=7,
        path_effects=[pe.withStroke(linewidth=2.4, foreground="#111111")],
    )


def _snapshot_bounds(data: list, pads: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    """Union of pad, zone and segment extents, with a default fallback."""
    xmin = ymin = 1e9
    xmax = ymax = -1e9

    def grow(x: float, y: float) -> None:
        nonlocal xmin, ymin, xmax, ymax
        xmin = min(xmin, x)
        ymin = min(ymin, y)
        xmax = max(xmax, x)
        ymax = max(ymax, y)

    for p in pads:
        b = p["geo"].bounds
        grow(b[0], b[1])
        grow(b[2], b[3])
    for node in data:
        if not isinstance(node, list):
            continue
        head = _sym(node[0])
        if head == "zone":
            for px, py in _zone_points(node):
                grow(px, py)
        elif head in ("segment", "gr_line"):
            for name in ("start", "end"):
                pt = _node_coord(node, name)
                if pt:
                    grow(pt[0], pt[1])
    if xmin >= xmax or ymin >= ymax:
        return (0.0, 0.0, 1.0, 1.0)
    return (xmin, ymin, xmax, ymax)


def _style_axes(ax, bounds: tuple[float, float, float, float]) -> None:
    xmin, ymin, xmax, ymax = bounds
    margin = 2.0
    ax.set_xlim(xmin - margin, xmax + margin)
    ax.set_ylim(ymin - margin, ymax + margin)
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", color="#3A3A3A", alpha=0.6)
    ax.invert_yaxis()  # KiCad PCB convention: +Y down.


def render_board_snapshot(pcb_path: str, out_path: str, pairs: list[PairSpec]) -> None:
    """Render the router's world model: one panel per copper layer.

    KiCad default theme: dark canvas, traces/pads/via rings coloured by
    layer (F.Cu red, B.Cu blue, inner layers green/amber/purple), dashed
    white lines = pad pairs still to connect, grey hatched polygons =
    keepout zones, light grey outlines = component bodies.
    Pad labels are ``ref.pad`` (e.g. ``R1.1``).
    """
    data = load_pcb(pcb_path)
    all_copper = _copper_layers(data)
    pads = _collect_pads(data, all_copper)
    bounds = _snapshot_bounds(data, pads)
    pending = [p for p in pairs if p.status != "done"]

    # Only pads referenced by a pair get labels (dense decoy pads stay clean).
    labeled = {(pair.ref_a, pair.pad_a) for pair in pairs} | {
        (pair.ref_b, pair.pad_b) for pair in pairs
    }

    panels = list(all_copper)
    cols = 2 if len(panels) > 1 else 1
    rows = (len(panels) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(7.2 * cols, 5.0 * rows), squeeze=False)
    fig.patch.set_facecolor(_BG_COLOR)
    flat = [axes[r][c] for r in range(rows) for c in range(cols)]

    for i, ax in enumerate(flat):
        if i >= len(panels):
            ax.set_visible(False)
            continue
        panel = panels[i]
        ax.set_facecolor(_BG_COLOR)

        for node in data:
            if not isinstance(node, list) or len(node) < 2:
                continue
            head = _sym(node[0])
            if head == "zone":
                _draw_zone(ax, node, panel)
            elif head == "footprint":
                _draw_footprint_body(ax, node, panel, all_copper)
            elif head in ("segment", "gr_line"):
                _draw_segment(ax, node, panel)
            elif head == "via":
                _draw_via(ax, node, panel)

        for p in pads:
            if panel in p["layers"]:
                _draw_pad(
                    ax,
                    p["geo"],
                    panel,
                    alpha=0.45 if len(p["layers"]) > 1 else 1.0,
                )
        for p in pads:
            if panel in p["layers"] and (p["ref"], p["pad"]) in labeled:
                _annotate_pad(ax, p)
        ax.set_title(panel, color="#F0F0F0")

        # A pending pair connects pads; a ratline is drawn only on panels
        # where both endpoint pads are visible (thru-hole pads span every
        # layer, so they never hide their pairs).
        pad_layers = {(p["ref"], p["pad"]): set(p["layers"]) for p in pads}
        for pair in pending:
            la = pad_layers.get((pair.ref_a, pair.pad_a), set(all_copper))
            lb = pad_layers.get((pair.ref_b, pair.pad_b), set(all_copper))
            if panel not in la or panel not in lb:
                continue
            a = _pad_center(data, pair.ref_a, pair.pad_a)
            b = _pad_center(data, pair.ref_b, pair.pad_b)
            if a is None or b is None:
                continue
            ax.plot(
                [a[0], b[0]],
                [a[1], b[1]],
                color=_PENDING_COLOR,
                linestyle="--",
                linewidth=1.0,
                alpha=0.9,
                zorder=6,
            )

        _style_axes(ax, bounds)

    fig.suptitle(
        "KiCad default theme — F.Cu red, B.Cu blue, inner green/amber; "
        "dashed white = still to route",
        fontsize=10,
        color="#F0F0F0",
    )
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def build_prompt(pairs: list[PairSpec], last_error: str | None) -> str:
    """Compose the text prompt describing what remains to route."""
    pending = [p for p in pairs if p.status == "pending"]
    lines = ["Current board state is in the image.", "Still to connect:"]
    for p in pending:
        lines.append(f"  - {p.description}")
    if last_error:
        lines += [
            "",
            "The last routing attempt failed with:",
            f"  last_error: {last_error}",
            "advice: change layer (thru-hole only), pick a different pair first,",
            "        or give up on this pair.",
        ]
    lines.append("Reply with your chosen route, layer and a one-line reason.")
    return "\n".join(lines)


def run_experiment(
    pcb_path: str,
    client: VLMClient | None,
    rounds: int,
    work_dir: str,
    dry_run: bool = False,
    via_pairs: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    """Run the feedback loop, writing snapshots into *work_dir*.

    Returns a metrics dict describing what happened.
    """
    pairs = routable_pairs(pcb_path, via_pairs=via_pairs)
    if not pairs:
        raise ValueError(f"no routable pad pairs found in {pcb_path}")

    metrics: dict[str, Any] = {
        "pcb": pcb_path,
        "pairs_total": len(pairs),
        "pairs_done": 0,
        "pairs_failed": 0,
        "rounds": 0,
        "attempts": 0,
        "feedback_errors": 0,
        "per_pair": {},
    }

    os.makedirs(work_dir, exist_ok=True)
    last_error: str | None = None

    for rnd in range(1, rounds + 1):
        pending = [p for p in pairs if p.status == "pending"]
        if not pending:
            break
        snapshot = os.path.join(work_dir, f"round_{rnd:02d}.png")
        render_board_snapshot(pcb_path, snapshot, pairs)
        metrics["rounds"] = rnd

        if dry_run or client is None:
            # No feedback: try pairs in fixed order, no layer hint.
            pick = pending[0]
            layer_hint = None
        else:
            prompt = build_prompt(pairs, last_error)
            with open(snapshot, "rb") as fh:
                png_bytes = fh.read()
            try:
                reply = client.ask(png_bytes, prompt)
            except RuntimeError as exc:
                metrics["feedback_errors"] += 1
                metrics["last_error"] = str(exc)
                last_error = str(exc)
                continue

            fb = parse_feedback(reply)
            if not fb.route_a or not fb.route_b:
                metrics["feedback_errors"] += 1
                last_error = f"VLM reply unparseable: {reply!r}"
                metrics["last_error"] = last_error
                continue
            try:
                ref_a, pad_a = parse_pad(fb.route_a)
                ref_b, pad_b = parse_pad(fb.route_b)
            except ValueError as exc:
                metrics["feedback_errors"] += 1
                last_error = f"bad pad spec in reply: {exc}"
                metrics["last_error"] = last_error
                continue

            cand = None
            for p in pending:
                if {p.ref_a, p.pad_a} == {ref_a, pad_a} and {p.ref_b, p.pad_b} == {ref_b, pad_b}:
                    cand = p
                    break
            if cand is None:
                metrics["feedback_errors"] += 1
                last_error = (
                    f"VLM chose {fb.route_a} -> {fb.route_b} which is not a pending pair "
                    f"({[p.key for p in pending]}); pick one of those."
                )
                metrics["last_error"] = last_error
                continue
            pick = cand
            layer_hint = fb.layer if fb.layer and fb.layer.lower() != "auto" else None

        metrics["attempts"] += 1
        pick.attempts += 1
        error = route_pair(pcb_path, pick, layer_hint)
        if error is None:
            pick.status = "done"
            metrics["pairs_done"] += 1
            last_error = None
            metrics.pop("last_error", None)
        else:
            pick.status = "failed"
            metrics["pairs_failed"] += 1
            last_error = error
            metrics["last_error"] = last_error
        metrics["per_pair"][pick.key] = {
            "net": pick.net,
            "attempts": pick.attempts,
            "status": pick.status,
            "layer_hint": layer_hint,
        }

    # Final snapshot regardless of completion.
    render_board_snapshot(pcb_path, os.path.join(work_dir, "final.png"), pairs)

    metrics["pairs_remaining"] = len([p for p in pairs if p.status == "pending"])
    return metrics


def _parse_via_pairs(text: str) -> tuple[tuple[str, str], ...]:
    """Parse 'F.Cu:B.Cu,B.Cu:In1.Cu' into (('F.Cu','B.Cu'),('B.Cu','In1.Cu'))."""
    out: list[tuple[str, str]] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise argparse.ArgumentTypeError(f"expected 'layerA:layerB', got {token!r}")
        a, b = token.split(":", 1)
        out.append((a.strip(), b.strip()))
    if not out:
        raise argparse.ArgumentTypeError("via pairs list is empty")
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.splitlines()[0])
    parser.add_argument("--pcb", default=DEFAULT_PCB, help=".kicad_pcb file (default: test board)")
    parser.add_argument("--rounds", type=int, default=8, help="max feedback rounds (default 8)")
    parser.add_argument("--work-dir", default=None, help="where to write snapshots/metrics")
    parser.add_argument(
        "--via-pairs",
        default=None,
        type=_parse_via_pairs,
        help="allowed via layer hops, comma-separated pairs (default: F.Cu:B.Cu,B.Cu:In1.Cu)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="no VLM: connect pairs in fixed order (control arm)",
    )
    parser.add_argument("--json", default=None, help="write metrics JSON to this path")
    args = parser.parse_args(argv)

    if not os.path.exists(args.pcb):
        print(f"error: PCB not found: {args.pcb}", file=sys.stderr)
        return 2
    pro_hint = os.path.join(
        os.path.dirname(args.pcb), os.path.basename(args.pcb)[:-10] + ".kicad_pro"
    )
    if not os.path.exists(pro_hint):
        print(
            "warning: no sibling .kicad_pro found; router will fail width/clearance lookup",
            file=sys.stderr,
        )

    work_dir = args.work_dir or tempfile.mkdtemp(prefix="vlm_route_")
    client = None if args.dry_run else VLMClient()

    try:
        metrics = run_experiment(
            args.pcb,
            client,
            args.rounds,
            work_dir,
            dry_run=args.dry_run,
            via_pairs=args.via_pairs,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"snapshots: {work_dir}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
