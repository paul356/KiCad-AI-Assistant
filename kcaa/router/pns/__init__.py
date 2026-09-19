"""
KiCad-style PNS routing engine (self-built, MIT clean).

Replaces the grid A* path search: skeleton trace generation
(``DIRECTION_45::BuildInitialTrace`` port), walkaround of fixed solids,
shove of movable tracks.  Pure Python + shapely; GPL KiCad code is used
only as an algorithm reference, never linked or copied verbatim.

This package is phase M0: geometry foundation (direction45 skeleton,
line data structures, hull primitives).  The engine flow in
``route_engine.py`` is wired in M1.
"""
