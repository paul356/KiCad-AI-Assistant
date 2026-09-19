"""
Obstacle space with nearest-obstacle queries (STRtree-backed).

Port of the KiCad ``PNS::NODE::NearestObstacle`` role: given a tentative
polyline, find the closest solid that blocks it.  The walkaround driver
(``route_engine.py``) iterates this query: bump the nearest obstacle, then
re-check the result until nothing collides.

Unlike the grid A* world (discretized cells), this is an exact vector
query: candidates come from the STRtree bbox, and the distance to each is
computed exactly on the Shapely geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

from kcaa.router.world_model import Obstacle

# Anything closer than this is treated as a collision; injected clearance
# comes from the caller via ``dfence``.
CLEARANCE_EPS = 1e-6


@dataclass(frozen=True)
class ObstacleHit:
    """Closest blocking obstacle for a query path."""

    obstacle: Obstacle
    # Distance from the query path to the obstacle hull.
    distance: float
    # Point on the query path closest to the obstacle.
    point: tuple[float, float]


class ObstacleNode:
    """Obstacle index: nearest/blocking queries over a fixed set."""

    def __init__(self, obstacles: Sequence[Obstacle]):
        self._obstacles = list(obstacles)
        geoms = [o.shape for o in self._obstacles]
        # STRtree requires non-empty input; keep the tree index aligned.
        self._tree = STRtree(geoms) if geoms else None  # type: ignore[arg-type]
        self._geoms = geoms

    def __len__(self) -> int:
        return len(self._obstacles)

    def _candidate_indices(self, line: LineString, pad: float) -> list[int]:
        """Candidates whose bbox overlaps the line bbox inflated by
        ``pad``.  Without the inflation, a path skimming just outside a
        hull has a disjoint bbox and the STRtree query drops it."""
        if self._tree is None:
            return []
        minx, miny, maxx, maxy = line.bounds
        probe = box(minx - pad, miny - pad, maxx + pad, maxy + pad)
        hits = self._tree.query(probe)
        return [int(h) for h in hits]

    def nearest(
        self, path: Sequence[tuple[float, float]], dfence: float = 0.0
    ) -> ObstacleHit | None:
        """Return the closest obstacle within ``dfence`` of the path, or
        ``None`` when the path is clear.  ``dfence`` is the collision
        margin (clearance + half width already inflated into hulls by the
        caller; pass extra clearance here)."""
        if not self._obstacles or len(path) < 2 or self._tree is None:
            return None
        line = LineString(path)
        if line.is_empty:
            return None
        best: ObstacleHit | None = None
        for gi in self._candidate_indices(line, pad=dfence + CLEARANCE_EPS):
            geom = self._geoms[gi]
            if geom.is_empty:
                continue
            d = float(geom.distance(line))
            if d > dfence + CLEARANCE_EPS:
                continue
            if best is not None and d >= best.distance - CLEARANCE_EPS:
                continue
            closest = self._closest_point_on_line(line, geom, d)
            best = ObstacleHit(obstacle=self._obstacles[gi], distance=d, point=closest)
        return best

    def _closest_point_on_line(self, line: LineString, geom, d: float) -> tuple[float, float]:
        if d <= CLEARANCE_EPS:
            # Touching: nearest vertex of the obstacle hull to the path.
            hull = geom.boundary
            fallback = tuple(line.coords)[0]
            if hull.is_empty:
                return fallback
            proj_on_hull = hull.interpolate(hull.project(Point(*fallback)))
            return (proj_on_hull.x, proj_on_hull.y)
        # Nearest point of the path to the hull.
        pt = line.interpolate(d) if d > 0 else line.interpolate(0.5)
        return (pt.x, pt.y)

    def collides(self, path: Sequence[tuple[float, float]], dfence: float = 0.0) -> bool:
        """True if any hull comes within ``dfence`` of the path (clearance
        + half-width applied as inflation by the caller)."""
        if not self._obstacles or len(path) < 2 or self._tree is None:
            return False
        line = LineString(path)
        if line.is_empty:
            return False
        return any(
            not self._geoms[gi].is_empty
            and float(self._geoms[gi].distance(line)) <= dfence + CLEARANCE_EPS
            for gi in self._candidate_indices(line, pad=dfence + CLEARANCE_EPS)
        )

    def obstacles(self) -> list[Obstacle]:
        return list(self._obstacles)
