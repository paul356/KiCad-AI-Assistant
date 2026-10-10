"""
PCB routing algorithm library for the KiCad MCP server.

Implements a simplified version of KiCad's PNS (Push and Shove) router.
Given a start pad and an end pad on the same net, the router produces a
DRC-clean sequence of ``segment`` and ``via`` S-expression nodes that
connect them while avoiding obstacles: the route walks around fixed
solids and shoves movable tracks out of the way.  Single-layer and
multi-layer routes emit straight 0/45/90 segments — corner mode is
fixed (mitered45); the sole exception is a cocircular waypoint chain,
which emits one covering arc.  Disturbed legs and via junctions
themselves stay straight segments.

Module map:

* :mod:`kcaa.router.world_model`       — PCB → obstacle list
* :mod:`kcaa.router.visibility_graph` — Obstacles → visibility graph
* :mod:`kcaa.router.path_postprocess`  — Miter corners, segment emission
* :mod:`kcaa.router.router`            — Orchestration / public API
"""
