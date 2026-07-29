"""
PCB routing algorithm library for the KiCad MCP server.

Implements a simplified version of KiCad's PNS (Push and Shove) router,
**without** shoving support. Given a start pad and an end pad on the
same net, the router produces a DRC-clean sequence of ``segment`` and
``via`` S-expression nodes that connect them while avoiding obstacles.

Module map:

* :mod:`kcaa.router.world_model`       — PCB → obstacle list
* :mod:`kcaa.router.visibility_graph` — Obstacles → visibility graph
* :mod:`kcaa.router.a_star`            — A\\* search on the graph
* :mod:`kcaa.router.path_postprocess`  — Miter corners, segment emission
* :mod:`kcaa.router.router`            — Orchestration / public API
"""
