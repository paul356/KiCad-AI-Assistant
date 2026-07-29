"""
Pre-flight checks for adding vias to a PCB.

Used by :func:`kcaa.tools.pcb_routing_tools.pcb_add_vias` before any
write to the .kicad_pcb file.  A single failure rejects the whole
batch; the file is left untouched.

Two layers of rules are enforced:

1. **Netclass (upper limit)** — the via's net resolves to a netclass
   in the matching ``.kicad_pro``; the requested ``diameter`` /
   ``drill`` should not exceed the netclass's
   ``via_diameter`` / ``via_drill``.  A mismatch is reported as
   a "netclass" violation.
2. **Board DRC + position** — every request must respect the
   project-level constraints from
   ``board.design_settings.rules``:

   * ``min_via_size``            — via pad diameter >= this (mm)
   * ``min_through_drill``       — drill diameter >= this (mm)
   * ``min_via_annular_width``   — (diameter - drill) / 2 >= this (mm)
   * ``min_hole_to_hole``        — centre-to-centre distance between
                                   this via's hole and every existing
                                   via's hole (and the other vias in
                                   the same batch) >= this (mm)
   * ``min_clearance``           — the via pad ring, buffered by this
                                   distance, must not overlap any
                                   foreign-net track / pad / via
   * ``copper_edge_clearance``   — via centre must stay this far
                                   inside the board outline

   Any of those rules failing produces a "drc" violation.

The check is intentionally **strict**:

* ``.kicad_pro`` not found → error (no silent skip).
* net has no resolvable netclass and no ``Default`` → error.
* any footprint / track / via / keepout overlap → error.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import fnmatch
import json
import os
import re
from typing import Any

from shapely.geometry import Point

from kcaa.router.world_model import Obstacle, WorldModel, build_world_model

# Match the project's .kicad_pro next to a .kicad_pcb.
_PROJECT_FILE_RE = re.compile(r".+\.kicad_pro$")


def find_project_file(pcb_path: str) -> str | None:
    """Return the absolute path of the project's ``.kicad_pro``, or ``None``."""
    base = os.path.splitext(os.path.basename(pcb_path))[0]
    d = os.path.dirname(pcb_path)
    if not base or not d or not os.path.isdir(d):
        return None
    for entry in os.listdir(d):
        if entry.startswith(base + ".") and _PROJECT_FILE_RE.match(entry):
            return os.path.join(d, entry)
    return None


@dataclass(frozen=True)
class ProposedVia:
    """The via the user wants to drop.  Same shape as ``OutputVia`` minus net."""

    x: float
    y: float
    diameter: float
    drill: float
    layers: tuple[str, str]
    net: str


@dataclass(frozen=True)
class Violation:
    """One rule violation.  ``index`` is the position in the batch."""

    index: int
    kind: str  # "netclass" | "footprint" | "track" | "via" | "board_edge" | "keepout"
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


def check_vias(pcb_path: str, vias: list[ProposedVia]) -> list[Violation]:
    """Run all pre-flight checks.  Returns an empty list when everything's OK.

    Raises nothing on bad data — failures are returned as :class:`Violation`
    records so the caller can format a single error message and reject the
    whole batch.
    """
    violations: list[Violation] = []

    # Netclass checks need the .kicad_pro up front; failure here short-circuits.
    nc_rules = _resolve_netclass_rules(pcb_path, [v.net for v in vias])
    if isinstance(nc_rules, str):
        # Single string means a hard error (no pro file / malformed).
        violations.append(Violation(-1, "project", nc_rules))
        return violations
    # nc_rules is now dict[net_name -> {"via_diameter": float|None, "via_drill": float|None}]
    for i, via in enumerate(vias):
        rule = nc_rules.get(via.net)
        if rule is None:
            violations.append(
                Violation(
                    i,
                    "netclass",
                    f"net {via.net!r} has no resolvable netclass "
                    f"(no matching pattern and no Default netclass)",
                    {"net": via.net},
                )
            )
            continue
        want_d = rule.get("via_diameter")
        want_r = rule.get("via_drill")
        if want_d is not None and abs(via.diameter - want_d) > 1e-3:
            violations.append(
                Violation(
                    i,
                    "netclass",
                    f"net {via.net!r} netclass expects via diameter {want_d} mm, "
                    f"got {via.diameter} mm",
                    {
                        "net": via.net,
                        "expected_diameter": want_d,
                        "actual_diameter": via.diameter,
                    },
                )
            )
        if want_r is not None and abs(via.drill - want_r) > 1e-3:
            violations.append(
                Violation(
                    i,
                    "netclass",
                    f"net {via.net!r} netclass expects via drill {want_r} mm, got {via.drill} mm",
                    {
                        "net": via.net,
                        "expected_drill": want_r,
                        "actual_drill": via.drill,
                    },
                )
            )

    # Position checks: build the world model once (without any of the new vias).
    # build_world_model already treats all existing tracks/vias as obstacles
    # except those on the via's own net — perfect for our needs.
    world = build_world_model(pcb_path)

    # Board DRC minimums (via size, drill, annular, clearance, hole-to-hole,
    # copper-edge clearance).  Empty dict if .kicad_pro is missing or the
    # project has no design_rules block.
    board = _load_board_constraints(pcb_path)

    for i, via in enumerate(vias):
        violations.extend(_check_position(i, via, world, board, vias))

    return violations


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_netclass_rules(
    pcb_path: str, nets: Iterable[str]
) -> dict[str, dict[str, float]] | str:
    """Return ``{net: {"via_diameter": ..., "via_drill": ...}}``.

    Returns a string error message if the project file is missing or
    malformed (caller treats as a hard failure).
    """
    pro_path = find_project_file(pcb_path)
    if pro_path is None:
        return f"no .kicad_pro found next to {pcb_path!r} (cannot resolve netclass rules)"
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return f"cannot read {pro_path!r}: {exc}"
    if not isinstance(data, dict):
        return f"{pro_path!r}: top-level JSON is not an object"

    ns = data.get("net_settings", {})
    if not isinstance(ns, dict):
        return f"{pro_path!r}: net_settings is not an object"

    classes_raw = ns.get("classes", [])
    patterns_raw = ns.get("netclass_patterns", [])
    if not isinstance(classes_raw, list) or not isinstance(patterns_raw, list):
        return f"{pro_path!r}: malformed net_settings.classes or netclass_patterns"

    # Class name -> {via_diameter, via_drill, ...}
    class_rules: dict[str, dict[str, float]] = {}
    default_name: str | None = None
    for c in classes_raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str):
            continue
        rule: dict[str, float] = {}
        for key in ("via_diameter", "via_drill"):
            val = c.get(key)
            if isinstance(val, int | float):
                rule[key] = float(val)
        class_rules[name] = rule
        if name == "Default":
            default_name = name

    # Net -> class name (explicit nets table first, then patterns).
    net_to_class: dict[str, str] = {}
    pat_list: list[tuple[str, str]] = []
    for p in patterns_raw:
        if not isinstance(p, dict):
            continue
        nc = p.get("netclass")
        pat = p.get("pattern")
        if isinstance(nc, str) and isinstance(pat, str):
            pat_list.append((pat, nc))

    nets_table = ns.get("nets", [])
    if isinstance(nets_table, list):
        for n in nets_table:
            if not isinstance(n, dict):
                continue
            name = n.get("name")
            nc = n.get("netclass") or n.get("class")
            if isinstance(name, str) and isinstance(nc, str):
                net_to_class[name] = nc

    # We also support an explicit assignment via class rules themselves,
    # since some project files attach nets to a class directly.
    for cls in classes_raw:
        if not isinstance(cls, dict):
            continue
        cls_name = cls.get("name")
        nets_field = cls.get("nets")
        if isinstance(cls_name, str) and isinstance(nets_field, list):
            for n in nets_field:
                if isinstance(n, str) and n not in net_to_class:
                    net_to_class[n] = cls_name

    out: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for net in set(nets):
        cls = net_to_class.get(net)
        if cls is None:
            for pat, nc in pat_list:
                if fnmatch.fnmatchcase(net, pat):
                    cls = nc
                    break
        if cls is None and default_name is not None:
            cls = default_name
        if cls is None or cls not in class_rules:
            out[net] = None  # type: ignore[assignment]
        else:
            out[net] = class_rules[cls]
        seen.add(net)
    return out


def _load_board_constraints(pcb_path: str) -> dict[str, float]:
    """Read the project's board-level DRC constraints.

    Returns a dict keyed by the same user-facing names as
    :func:`kcaa.utils.pcb_design_rules.get_effective_design_rules_from_file`.
    Keys that aren't set in the project file are omitted (callers must
    default each rule to ``None`` / "skip").

    Recognised keys:

    * ``min_via_size``            — minimum via pad diameter (mm)
    * ``min_through_drill``       — minimum drill diameter (mm)
    * ``min_via_annular_width``   — minimum copper ring width (mm)
    * ``min_clearance``           — minimum copper-to-copper clearance (mm)
    * ``hole_to_hole_min``        — minimum centre-to-centre distance
                                    between holes / vias (mm)
    * ``copper_edge_clearance``   — minimum distance from copper to the
                                    board edge (mm)
    """
    try:
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file
    except ImportError:
        return {}
    result = get_effective_design_rules_from_file(pcb_path)
    if not result.get("success"):
        return {}
    return dict(result.get("design_rules") or {})


def _check_position(
    index: int,
    via: ProposedVia,
    world: WorldModel,
    board: dict[str, float],
    batch: list[ProposedVia],
) -> list[Violation]:
    out: list[Violation] = []

    # ------------------------------------------------------------------
    # 1. Board DRC size minimums (lower bounds; fail if requested value
    #    is below the project's design rule).
    # ------------------------------------------------------------------
    min_size = board.get("min_via_size")
    if min_size is not None and via.diameter < float(min_size) - 1e-9:
        out.append(
            Violation(
                index,
                "drc",
                f"via diameter {via.diameter} mm is below board min_via_size {min_size} mm",
                {
                    "diameter": via.diameter,
                    "min_via_size": float(min_size),
                },
            )
        )
    min_drill = board.get("min_through_drill")
    if min_drill is not None and via.drill < float(min_drill) - 1e-9:
        out.append(
            Violation(
                index,
                "drc",
                f"via drill {via.drill} mm is below board min_through_drill {min_drill} mm",
                {
                    "drill": via.drill,
                    "min_through_drill": float(min_drill),
                },
            )
        )
    min_ann = board.get("min_via_annular_width")
    if min_ann is not None:
        annular = (via.diameter - via.drill) / 2.0
        if annular < float(min_ann) - 1e-9:
            out.append(
                Violation(
                    index,
                    "drc",
                    f"via annular ring {annular} mm is below board "
                    f"min_via_annular_width {min_ann} mm",
                    {
                        "annular_width": annular,
                        "min_via_annular_width": float(min_ann),
                    },
                )
            )

    # ------------------------------------------------------------------
    # 2. Hole-to-hole minimum distance.  Checked against every existing
    #    via (any layer) and the other vias in the same batch (each
    #    reported once at the later index).
    # ------------------------------------------------------------------
    min_h2h = board.get("hole_to_hole_min")
    if min_h2h is not None:
        threshold = float(min_h2h)
        # Existing vias in the board.
        for obs in world.obstacles:
            if obs.kind != "via":
                continue
            c = obs.shape.centroid
            ox, oy = c.x, c.y
            dx = via.x - ox
            dy = via.y - oy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < threshold - 1e-9:
                out.append(
                    Violation(
                        index,
                        "drc",
                        f"via at ({via.x}, {via.y}) is {dist:.3f} mm from existing "
                        f"via at ({ox}, {oy}); board min_hole_to_hole is "
                        f"{threshold} mm",
                        {
                            "x": via.x,
                            "y": via.y,
                            "other_x": ox,
                            "other_y": oy,
                            "distance": dist,
                            "min_hole_to_hole": threshold,
                            "other": "existing",
                        },
                    )
                )
        # Other vias in this batch (later index only).
        for j, other in enumerate(batch):
            if j <= index:
                continue
            dx = via.x - other.x
            dy = via.y - other.y
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < threshold - 1e-9:
                out.append(
                    Violation(
                        index,
                        "drc",
                        f"via at ({via.x}, {via.y}) is {dist:.3f} mm from batch "
                        f"via #{j} at ({other.x}, {other.y}); board "
                        f"min_hole_to_hole is {threshold} mm",
                        {
                            "x": via.x,
                            "y": via.y,
                            "other_x": other.x,
                            "other_y": other.y,
                            "distance": dist,
                            "min_hole_to_hole": threshold,
                            "other": "batch",
                            "other_index": j,
                        },
                    )
                )

    # ------------------------------------------------------------------
    # 3. Board edge: via center must stay inside the outline with the
    #    required copper-edge clearance.
    # ------------------------------------------------------------------
    edge_clear = float(board.get("copper_edge_clearance") or 0.0)
    if world.board_bbox is not None:
        minx, miny, maxx, maxy = world.board_bbox
        if (
            via.x < minx + edge_clear
            or via.x > maxx - edge_clear
            or via.y < miny + edge_clear
            or via.y > maxy - edge_clear
        ):
            out.append(
                Violation(
                    index,
                    "board_edge",
                    f"via at ({via.x}, {via.y}) is outside the board outline "
                    f"({minx}, {miny})-({maxx}, {maxy}) "
                    f"with required edge clearance {edge_clear} mm",
                    {
                        "x": via.x,
                        "y": via.y,
                        "board_bbox": [minx, miny, maxx, maxy],
                        "edge_clearance": edge_clear,
                    },
                )
            )

    # ------------------------------------------------------------------
    # 4. Pad ring collision on each copper layer the via occupies.
    #    The ring is buffered by ``min_clearance`` so we report the
    #    violation only when the new copper would be *closer* to a
    #    foreign obstacle than the project allows.
    # ------------------------------------------------------------------
    min_clear = float(board.get("min_clearance") or 0.0)
    radius = via.diameter / 2.0
    ring = Point(via.x, via.y).buffer(radius + min_clear)
    layers = set(via.layers)

    for obs in world.obstacles:
        if not (layers & obs.layers):
            continue
        # Same-net existing track/via isn't a collision.
        if obs.net == via.net and obs.kind in ("track", "via"):
            continue
        if obs.shape.intersects(ring):
            desc = _describe_obstacle(obs, via)
            out.append(
                Violation(
                    index,
                    obs.kind,
                    f"via at ({via.x}, {via.y}) overlaps {obs.kind} {desc}"
                    + (f" with required clearance {min_clear} mm" if min_clear > 0 else ""),
                    {
                        "x": via.x,
                        "y": via.y,
                        "obstacle_kind": obs.kind,
                        "obstacle_net": obs.net,
                        "obstacle_ref": obs.ref,
                        "layers": sorted(layers & obs.layers),
                        "min_clearance": min_clear,
                    },
                )
            )
    return out


def _describe_obstacle(obs: Obstacle, via: ProposedVia) -> str:
    """Short description for the error message."""
    if obs.ref:
        return f"on footprint {obs.ref!r}"
    if obs.net:
        return f"on net {obs.net!r}"
    return "(net unknown)"
