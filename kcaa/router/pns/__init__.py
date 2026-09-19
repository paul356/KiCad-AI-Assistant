"""
KiCad-style PNS routing engine (self-built, MIT clean).

Replaces the grid A* path search: skeleton trace generation
(``DIRECTION_45::BuildInitialTrace`` port), walkaround of fixed solids,
shove of movable tracks.  Pure Python + shapely; GPL KiCad code is used
only as an algorithm reference, never linked or copied verbatim.

Phases:
* M0 — geometry foundation (direction45 skeleton, line data structures,
  hull primitives).
* M1 — single-obstacle walkaround (``walkaround.py``, a port of KiCad
  ``PNS::LINE::Walkaround`` graph traversal) plus the STRtree obstacle
  space (``node.py``).  The two connect in ``route_engine.py`` (M1/M2).
"""
