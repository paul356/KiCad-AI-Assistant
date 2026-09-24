"""
PCB routing algorithm library for the KiCad MCP server.

Implements a simplified version of KiCad's PNS (Push and Shove) router.
Given a start pad and an end pad on the same net, the router produces a
DRC-clean sequence of ``segment`` and ``via`` S-expression nodes that
connect them while avoiding obstacles: the route walks around fixed
solids and shoves movable tracks out of the way.  Single-layer routes
may emit rounded-corner arcs; multi-layer routes run one walkaround +
shove leg per layer joined by DRC-validated through-vias and emit
straight segments only.

Module map:

* :mod:`kcaa.router.world_model`       — PCB → obstacle list
* :mod:`kcaa.router.visibility_graph` — Obstacles → visibility graph
* :mod:`kcaa.router.a_star`            — A\\* search on the graph
* :mod:`kcaa.router.path_postprocess`  — Miter corners, segment emission
* :mod:`kcaa.router.router`            — Orchestration / public API
"""
