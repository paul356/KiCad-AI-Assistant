# PCB Routing Guide

The KiCad MCP server provides a **no-shove PNS router** that connects
two pads with an obstacle-avoiding track.  It is exposed through the
`pcb_route_pad_to_pad` MCP tool.

## Single-layer routing

```python
await pcb_route_pad_to_pad(
    pcb_path="/path/to/board.kicad_pcb",
    ref_a="R1", pad_a="2",
    ref_b="C1", pad_b="2",
    net="VCC",
    layer="F.Cu",        # optional, default "F.Cu"
    width=0.5,           # optional; uses netclass track width if omitted
)
```

The router walks the pad-exit points of `R1.2` and `C1.2` and A*'s a
visibility graph over the obstacle-free regions.  The track is added
to the board on the same layer as the source pad.  Obstacles include:

* Same-net tracks are *not* obstacles (you are extending them).
* Footprint courtyards for components other than the two endpoints.
* Existing tracks of *other* nets.
* Keepout zones on the active layer.

## Multi-layer routing (via insertion)

If the source pad is on a different layer from the destination pad,
the router will insert through-hole vias at every layer transition.
You must supply the destination layer and the set of via pairs the
router is allowed to use:

```python
await pcb_route_pad_to_pad(
    pcb_path="/path/to/board.kicad_pcb",
    ref_a="R1", pad_a="2",
    ref_b="U1", pad_b="5",
    net="VCC",
    layer="F.Cu",                # start layer
    target_layer="In1.Cu",        # end layer
    via_pairs=(("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
)
```

If `target_layer` is supplied and differs from `layer` and
`via_pairs` is omitted, the router defaults to a single F<->B pair.

### Response shape

The tool returns a dict with these keys (in addition to the keys for
the single-layer case):

| Key | Type | Description |
| --- | --- | --- |
| `via_count` | `int` | Number of vias written to the board. |
| `vias` | `list[dict]` | Each dict has `x`, `y`, `diameter`, `drill`, `layers` (`[from, to]`), and `net`. |
| `layers_used` | `list[str]` | Ordered, deduplicated list of layers the path traversed. |

### Via cost

The default via cost is `2.0 + 0.5 * n` millimetres, where `n` is the
number of vias the route has already taken.  A two-via path costs
`2.0 + 2.5 = 4.5 mm` of via overhead.  This biases the router toward
fewer-layer solutions when both are viable.

### Adding vias

Use ``pcb_add_vias`` to drop one or more through-hole vias.  Pass a
single-element list for a one-off via, or many for ground-plane
stitching / fan-out.  All vias are written in a single PCB rewrite
with one ``.bak`` covering the whole batch.

```python
# Single via
await pcb_add_vias(
    pcb_path="/path/to/board.kicad_pcb",
    vias=[{"x": 40.0, "y": 25.0, "net": "GND"}],
)

# Many vias (ground stitching / fan-out)
await pcb_add_vias(
    pcb_path="/path/to/board.kicad_pcb",
    vias=[
        {"x": 30.0, "y": 35.0, "net": "VCC"},
        {"x": 40.0, "y": 35.0, "net": "GND", "diameter": 1.0, "drill": 0.5},
        {"x": 50.0, "y": 35.0, "net": "GND", "layers": ("F.Cu", "In1.Cu")},
    ],
)
```

Each descriptor accepts optional ``diameter`` (default 0.8), ``drill``
(default 0.4), ``layers`` (default ``("F.Cu", "B.Cu")``); only
``x``, ``y`` and ``net`` are required.  An empty list is a no-op (no
write, no backup).  An invalid descriptor (missing ``net``,
non-numeric coordinate, etc.) rejects the *whole* batch and leaves
the file untouched.

#### Pre-flight checks

Before any write, every via is checked against:

* the matching ``.kicad_pro`` — the via's ``net`` must resolve to a
  netclass (or fall back to ``Default``), and the requested
  ``diameter`` / ``drill`` must not exceed that class.  Missing
  ``.kicad_pro`` is a hard error: there is no silent skip.
* the project-level DRC rules in ``board.design_settings.rules`` —
  ``min_via_size`` and ``min_through_drill`` are lower bounds on the
  via dimensions; ``min_via_annular_width`` is the lower bound on
  ``(diameter - drill) / 2``; ``min_clearance`` is the minimum
  copper-to-copper distance (the via pad ring is buffered by this
  before the collision check); ``hole_to_hole_min`` is the minimum
  centre-to-centre distance between this via's hole and every
  existing via's hole (and every other via in the same batch);
  ``copper_edge_clearance`` keeps the via's centre inside the board
  outline.
* the board geometry — the via's pad ring must not overlap any
  footprint courtyard, other-net track/via, or zone keepout, and
  must stay inside the board outline with the configured
  ``copper_edge_clearance``.

Any violation rejects the whole batch and leaves the file
untouched.  The error response includes a `violations` array with
`index` (the position in the input list), `kind`, and `message`
fields per violation, plus a single concatenated `error` string
for the human-readable summary.

## Failure modes

| Failure | Meaning |
| --- | --- |
| `RouteFailure: Pad <ref>/<pad> not found` | Pad name is wrong or pad is on a different layer than requested. |
| `RouteFailure: Pad <ref>/<pad> has no copper shape on layer '<layer>'` | The destination pad does not have a copper shape on the requested `end_layer`. |
| `RouteFailure: No obstacle-avoiding path from <a> to <b> across layers [...]` | No combination of exit points / via transitions yields a clear path.  Check obstacles and via pairs. |
| `RouteFailure: Via at (x, y) would extend outside the board` | At least one via pad does not fit inside the Edge.Cuts board outline.  Move the route or shrink the board. |

The tool returns `{"error": "..."}` on any of the above; the board
file is **not** modified on failure.
