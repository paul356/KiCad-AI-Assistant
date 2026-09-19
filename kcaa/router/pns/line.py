"""
Track line data structure for the PNS engine.

A :class:`Line` wraps the ``direction45.Trace`` skeleton with the routing
attributes KiCad's ``PNS::LINE`` carries (net, layer, width) and adds
geometric accessors used by walkaround/shove/cleanup (M1+): polyline
sampling, length, shapely geometry and per-segment iteration that knows
which segments are arcs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from shapely.geometry import LineString

from kcaa.router.pns.direction45 import ArcSeg, Trace


@dataclass
class Line:
    """Route polyline with arcs.

    ``points`` are the skeleton anchors; ``arcs[i]`` (when not None)
    replaces the straight segment ``points[i] -> points[i+1]`` — the same
    layout as :class:`kcaa.router.pns.direction45.Trace`.
    """

    points: list[tuple[float, float]]
    arcs: list[ArcSeg | None]
    width: float = 0.0
    net: str = ""
    layer: str = ""

    @classmethod
    def from_trace(cls, trace: Trace, width: float, net: str, layer: str) -> Line:
        """Wrap a skeleton trace with routing attributes."""
        return cls(
            points=list(trace.points), arcs=list(trace.arcs), width=width, net=net, layer=layer
        )

    # -- geometry ----------------------------------------------------------

    def start(self) -> tuple[float, float]:
        return self.points[0]

    def end(self) -> tuple[float, float]:
        return self.points[-1]

    def length(self) -> float:
        total = 0.0
        for i in range(len(self.points) - 1):
            arc = self.arcs[i] if i < len(self.arcs) else None
            if arc is not None:
                total += arc.length()
            else:
                p, q = self.points[i], self.points[i + 1]
                total += ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
        return total

    def as_polyline(self, arc_pts: int = 32) -> list[tuple[float, float]]:
        """Sample arcs and return a plain continuous polyline."""
        out = [self.points[0]]
        for i in range(len(self.points) - 1):
            arc = self.arcs[i] if i < len(self.arcs) else None
            if arc is None:
                out.append(self.points[i + 1])
            else:
                out.extend(arc.as_polyline(n=arc_pts)[1:])
        return out

    def as_shapely(self, arc_pts: int = 32) -> LineString:
        """The line as a Shapely LineString (arcs sampled)."""
        return LineString(self.as_polyline(arc_pts))

    def iter_segments(self, arc_pts: int = 32) -> Iterator[tuple[int, list[tuple[float, float]]]]:
        """Yield ``(idx, sampled_points)`` for every segment.

        ``idx`` is the segment index; arcs yield their sampled polyline
        (including both endpoints), straight segments yield
        ``[p_i, p_{i+1}]``.  Useful for collision checks that must treat
        an arc as one obstacle.
        """
        for i in range(len(self.points) - 1):
            arc = self.arcs[i] if i < len(self.arcs) else None
            if arc is None:
                yield i, [self.points[i], self.points[i + 1]]
            else:
                yield i, arc.as_polyline(n=arc_pts)
