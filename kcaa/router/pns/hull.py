"""
Obstacle hulls for the PNS engine.

KiCad rolls the obstacle line along the hull of a colliding item:
``SEGMENT::Hull( clearance, width )`` / ``ArcHull( arc, clearance,
walkaroundThickness )``.  The hull is the shortest closed loop that keeps a
track of ``width`` a distance ``clearance`` away from the item, so it is the
item's shape buffered by ``clearance + width / 2``.

Both helpers return Shapely polygons; ``ArcHull`` samples the arc and
buffers the polyline, falling back to an octagon around the center when the
arc spans > 180 deg with a chord shorter than the clearance (KiCad's
octagon fast path — buffering a near-full-circle sample is numerically
noisy).
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon

from kcaa.router.pns.direction45 import ArcSeg


def buffer_distance(clearance: float, width: float) -> float:
    """One-sided offset: half the item width plus the clearance."""
    return clearance + width / 2.0


def seg_hull(
    p0: tuple[float, float],
    p1: tuple[float, float],
    clearance: float,
    width: float,
) -> Polygon:
    """Closed hull around segment ``p0 -> p1`` (KiCad SEGMENT::Hull).

    A track of ``width`` routed at center-line ``p0 -> p1`` stays at least
    ``clearance`` away from everything outside this polygon.
    """
    line = LineString([p0, p1])
    poly = line.buffer(buffer_distance(clearance, width), cap_style="round")
    if poly.is_empty:
        return Polygon()
    return poly


def arc_hull(arc: ArcSeg, clearance: float, width: float, arc_pts: int = 32) -> Polygon:
    """Closed hull around an arc (KiCad ArcHull port).

    Samples the arc and buffers the sampled polyline; the octagon fallback
    mirrors KiCad's ``ArcHull`` for a CCW sweep > 180 deg with a chord
    shorter than the clearance (center-anchored regular octagon of radius
    ``arc_radius + buffer``, 8-point, flat-to-flat oriented).
    """
    if arc.radius <= 0:
        return Polygon()
    sweep_ccw = arc.total_sweep()
    radius = arc.radius
    chord = math.hypot(arc.end[0] - arc.start[0], arc.end[1] - arc.start[1])
    buf = buffer_distance(clearance, width)
    if sweep_ccw > math.pi and chord < clearance:
        return _octagon(arc.center(), radius + buf)
    pts = arc.as_polyline(n=arc_pts)
    poly = LineString(pts).buffer(buf, cap_style="round", join_style="round")
    if poly.is_empty:
        return Polygon()
    return poly


def _octagon(center: tuple[float, float], inradius: float) -> Polygon:
    """Regular octagon around ``center`` with the given inradius.

    Vertices at 22.5° offsets so the flat sides face N/S/E/W; every point
    at distance <= ``inradius`` from the center is covered.  Mirrors the
    covering intent of KiCad's ``OctagonalHull(bbox, clearance, chamfer)``.
    """
    cx, cy = center
    R = inradius / math.cos(math.pi / 8.0)  # circumradius
    pts = [
        (
            cx + R * math.cos(math.pi / 8.0 + k * math.pi / 4.0),
            cy + R * math.sin(math.pi / 8.0 + k * math.pi / 4.0),
        )
        for k in range(8)
    ]
    pts.append(pts[0])
    return Polygon(pts)
