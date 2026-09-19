"""
Walkaround: route a polyline around one obstacle hull (KiCad port).

Port of ``PNS::LINE::Walkaround`` (pcbnew/router/pns_line.cpp:297).  The
algorithm builds a directed graph of path vertices and hull vertices,
classifies each vertex as inside/on-edge/outside the hull, then walks the
graph from the path's first point to its last point:

* OUTSIDE vertex → move to the next non-inside vertex further along the
  path (that is the straight part of the original trace);
* ON_EDGE vertex → prefer leaving the hull to an outside neighbour, else
  keep traversing the hull ring in the requested direction (which is what
  produces the "hug the obstacle" shape).

The graph-walk formulation handles path vertices that coincide with hull
vertices (common at tangent points), start/end points sitting exactly on
the hull edge, and multi-segment hulls, all in one traversal.

Hulls are expected as closed Shapely polygons (buffered obstacles).  CW
vs CCW here refers to the ring order of the sampled hull vertex list.
"""

from __future__ import annotations

from collections.abc import Sequence
import math

from shapely.geometry import LineString, Point, Polygon

_EPS = 1e-9


def _near(a: tuple[float, float], b: tuple[float, float], eps: float = 1e-6) -> bool:
    """Point proximity test used for vertex merging (KiCad uses exact int
    equality; floats need a tolerance)."""
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= eps


class WalkFailure(RuntimeError):
    """Raised when no walkaround exists (start inside hull, stuck, or the
    graph traversal cannot reach the path end)."""


class _Vertex:
    __slots__ = ("pos", "is_hull", "indexp", "indexh", "neighbours", "visited", "_type")

    _UNSET = -1

    def __init__(self, pos: tuple[float, float]):
        self.pos = pos
        self.is_hull = False
        self.indexp = _Vertex._UNSET
        self.indexh = _Vertex._UNSET
        self.neighbours: list[_Vertex] = []
        self.visited = False
        self._type: int | None = None  # lazy: computed relative to the hull

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"_Vertex({round(self.pos[0], 2)},{round(self.pos[1], 2)}"
            f" ip={self.indexp} ih={self.indexh} t={self._type})"
        )


def _classify(pos: tuple[float, float], polygon: Polygon) -> int:
    """0 inside, 1 outside, 2 on edge (KiCad INSIDE/OUTSIDE/ON_EDGE)."""
    pt = Point(pos[0], pos[1])
    if polygon.boundary.distance(pt) < _EPS:
        return 2
    if polygon.contains(pt):
        return 0
    return 1


def _ring_points(polygon: Polygon, cw: bool) -> list[tuple[float, float]]:
    """Hull vertices in traversal order (KiCad stores hulls CW; ``cw``
    selects the traversal direction: True = clockwise, False = CCW).

    Shapely polygons default to CCW exteriors, so a CW traversal needs the
    ring reversed.  The ring is returned CLOSED (last == first) to match
    KiCad's SHAPE_LINE_CHAIN closure; ring-index arithmetic uses the open
    length (explicitly in the caller).
    """
    ring = list(polygon.exterior.coords)
    if len(ring) > 1 and _near(ring[-1], ring[0]):
        ring = ring[:-1]
    # KiCad default hull orientation is CW; aCw == false reverses the walk.
    if cw:
        ring.reverse()
    return ring + [ring[0]]


def walkaround_line(
    path: Sequence[tuple[float, float]],
    hull: Polygon,
    cw: bool = True,
) -> list[tuple[float, float]]:
    """Walk ``path`` around ``hull``, returning the new polyline.

    The start point must lie strictly outside the hull (KiCad returns
    failure if it is inside); a start exactly on the hull edge is allowed.
    Raises :class:`WalkFailure` when no path exists.
    """
    if len(path) < 2:
        raise WalkFailure("path has fewer than two points")

    p_first = path[0]
    if hull.contains(Point(*p_first)):
        raise WalkFailure("start point lies inside the obstacle hull")

    # Intersections of the path with the hull boundary, in path order.
    path_line = LineString(path)
    boundary = hull.boundary
    inter = path_line.intersection(boundary)

    ips: list[tuple[float, float]] = []
    if inter.is_empty:
        # No intersections at all: either the path fully avoids the hull
        # (return as-is) or it is entirely inside it (start check already
        # excluded that).
        if hull.contains(path_line):
            raise WalkFailure("entire path lies inside the obstacle hull")
        if path_line.intersection(hull).is_empty:
            return list(path)
        raise WalkFailure("unexpected intersection classification")

    if inter.geom_type == "Point":
        ips = [tuple(inter.coords)[0]]
    elif inter.geom_type == "MultiPoint":
        ips = [tuple(p.coords)[0] for p in inter.geoms]
    elif inter.geom_type == "GeometryCollection":
        # Mixed grazing: the path rides the hull edge and leaves, so the
        # intersection is LineString + Point pieces.  Recurse on each.
        for g in inter.geoms:
            sub = _collect_intersection_points(g, path_line, boundary, hull)
            for ip in sub:
                if not any(_near(ip, e) for e in ips):
                    ips.append(ip)
    elif inter.geom_type in ("LineString", "MultiLineString"):
        # Grazing intersection: the path runs along the hull edge.  The
        # tangency points are the endpoints of the overlap; treat those as
        # the entry/exit and let ON_EDGE handling carry the rest.
        ends: list[tuple[float, float]] = []
        geoms: list = [inter] if inter.geom_type == "LineString" else list(inter.geoms)
        for g in geoms:
            coords = list(g.coords)
            if coords:
                ends.append(coords[0])
                ends.append(coords[-1])
        # Keep only distinct points in path order.
        for e in ends:
            if not any(_near(e, ip) for ip in ips):
                ips.append(e)
    else:
        raise WalkFailure(f"unsupported intersection type {inter.geom_type!r}")

    # Split the path at every intersection so hull/path vertices coincide
    # there (KiCad: pnew.Split / hnew.Split).  We rebuild the polyline.
    pnew: list[tuple[float, float]] = []
    for p in path:
        pnew.append(p)
        # Insert any intersection that lies strictly between the previous
        # point and this one.
    _split_chain_in_place(pnew, ips)

    # Hull vertices, traversed in the requested direction.
    hnew = _ring_points(hull, cw)
    _split_ring_in_place(hnew, ips)

    vertices: list[_Vertex] = []
    by_pos: dict[tuple[float, float], _Vertex] = {}

    def _find(pos: tuple[float, float]) -> _Vertex | None:
        for v in by_pos:
            if _near(v, pos):
                return by_pos[v]
        return None

    # Path vertices.
    for i, p in enumerate(pnew):
        v = _Vertex(p)
        v.indexp = i
        v.is_hull = False
        vertices.append(v)
        by_pos[p] = v

    # Path adjacency (both directions).
    for i in range(len(vertices) - 1):
        vertices[i].neighbours.append(vertices[i + 1])
    for i in range(1, len(vertices)):
        vertices[i].neighbours.append(vertices[i - 1])

    # Hull vertices: merge with existing path vertices at shared locations.
    # hnew is CLOSED (last == first); indices are assigned on the open
    # vertex list (KiCad CPoint(i) with PointCount() == open count).
    n = len(hnew) - 1
    for i in range(n):
        hp = hnew[i]
        v = _find(hp)
        if v is not None:
            v.is_hull = True
            v.indexh = i
        else:
            nv = _Vertex(hp)
            nv.is_hull = True
            nv.indexh = i
            vertices.append(nv)
            by_pos[hp] = nv

    # Hull ring adjacency (the closing segment wraps index n-1 -> 0).
    for i in range(n):
        vc = _find(hnew[i])
        vnext = _find(hnew[(i + 1) % n])
        if vc is not None and vnext is not None:
            vc.neighbours.append(vnext)

    # Classify path vertices lazily via the hull polygon.
    for v in vertices:
        if v._type is None:
            v._type = _classify(v.pos, hull)

    in_last = hull.contains(Point(*path[-1]))
    last_point = path[-1]

    start = vertices[0]
    out: list[tuple[float, float]] = []
    v: _Vertex | None = start
    v_prev: _Vertex | None = None
    last_dst = float("inf")
    append_v = True
    iter_limit = 1000
    target_indexp = len(pnew) - 1

    while v is not None and v.indexp != target_indexp:
        iter_limit -= 1
        if iter_limit == 0:
            raise WalkFailure("walkaround graph traversal hit iteration limit")
        if v.visited:
            break  # loop found -> stop walking
        out.append(v.pos)

        v_next: _Vertex | None = None

        if v._type == 1:  # OUTSIDE
            # Next vertex further along the path that is not inside.
            fallback = None
            for vn in v.neighbours:
                if vn.indexp >= 0 and vn.indexp != v.indexp and vn._type != 0:
                    if not vn.visited:
                        v_next = vn
                        break
                    if vn is not v_prev:
                        fallback = vn
            if v_next is None:
                v_next = fallback
            if v_next is None:
                raise WalkFailure("outside vertex has no onward path neighbour")
        elif v._type == 2:  # ON_EDGE
            # Prefer stepping off the hull to an outside neighbour.
            for vn in v.neighbours:
                if vn._type == 1 and not vn.visited:
                    v_next = vn
                    break
            # Otherwise continue along the hull ring (next hull index).
            if v_next is None and v.indexh >= 0:
                for vn in v.neighbours:
                    if (
                        vn._type == 2
                        and not vn.is_hull
                        and vn.indexp >= 0
                        and vn.indexh == (v.indexh + 1) % n
                    ):
                        v_next = vn
                        break
            # Still nothing: step to the next hull vertex (dedup case where
            # the next ring vertex also lies on the path).
            if v_next is None and v.indexh >= 0:
                for vn in v.neighbours:
                    if vn._type == 2 and vn.indexh == (v.indexh + 1) % n:
                        v_next = vn
                        break
            # Path end inside the hull: once the ring walk starts coming
            # back toward the start (distance to the end point stops
            # shrinking), project the end point onto the current side and
            # finish there (KiCad does exactly this).
            if in_last and v_next is not None:
                d = (v_next.pos[0] - last_point[0]) ** 2 + (v_next.pos[1] - last_point[1]) ** 2
                if d >= last_dst:
                    proj = _project_point_to_segment(last_point, v.pos, v_next.pos)
                    if proj is not None and (not out or not _near(out[-1], proj)):
                        out.append(proj)
                    append_v = False
                    break
                last_dst = d
        # INSIDE (type 0) is never chosen as v_next by the rules above; if
        # we somehow land on one the traversal is stuck.

        v.visited = True
        v_prev = v
        v = v_next

    if v is not None and append_v:
        out.append(v.pos)

    # Deduplicate consecutive identical points (KiCad Simplify2).
    cleaned: list[tuple[float, float]] = []
    for p in out:
        if not cleaned or not _near(cleaned[-1], p):
            cleaned.append(p)

    # Snip collinear runs (KiCad Simplify2 does this too).
    cleaned = _simplify_collinear(cleaned)

    if not cleaned or not _near(cleaned[0], path[0]):
        raise WalkFailure("walkaround lost the path start point")
    return cleaned


def _split_chain_in_place(
    chain: list[tuple[float, float]],
    ips: list[tuple[float, float]],
) -> None:
    """Insert intersection points into ``chain`` at their positions along
    the polyline, keeping order."""
    if not ips:
        return
    # Compute cumulative distances of the original chain vertices.
    cum: list[float] = [0.0]
    for i in range(1, len(chain)):
        cum.append(
            cum[-1] + math.hypot(chain[i][0] - chain[i - 1][0], chain[i][1] - chain[i - 1][1])
        )

    path_line = LineString(chain)

    # Positions (arclength) of each intersection on the polyline.
    placed: list[tuple[float, tuple[float, float]]] = []
    for ip in ips:
        if any(_near(ip, p) for p in chain):
            continue  # already a vertex
        d = path_line.project(Point(*ip))
        placed.append((d, ip))

    placed.sort(key=lambda t: t[0])
    # Insert by walking the chain; for each intersection with parameter d,
    # find the segment whose arclength window contains d and place it.
    out: list[tuple[float, float]] = []
    seg_start = 0.0
    for i, p in enumerate(chain):
        if i > 0:
            seg_start = cum[i - 1]
        seg_end = cum[i]
        while placed and placed[0][0] <= seg_end + _EPS:
            d, ip = placed.pop(0)
            if d >= seg_start - _EPS:
                out.append(ip)
        out.append(p)
    chain[:] = out


def _collect_intersection_points(
    g,
    path_line: LineString,
    boundary,
    hull: Polygon,
) -> list[tuple[float, float]]:
    """Collect intersection points from one piece of the path/hull
    intersection (recursive: a piece may itself be a collection)."""
    if g.is_empty:
        return []
    if g.geom_type == "Point":
        return [tuple(g.coords)[0]]
    if g.geom_type == "MultiPoint":
        return [tuple(p.coords)[0] for p in g.geoms]
    if g.geom_type == "GeometryCollection":
        out: list[tuple[float, float]] = []
        for sub in g.geoms:
            out.extend(_collect_intersection_points(sub, path_line, boundary, hull))
        return out
    if g.geom_type in ("LineString", "MultiLineString"):
        geoms: list = [g] if g.geom_type == "LineString" else list(g.geoms)
        ends: list[tuple[float, float]] = []
        for ls in geoms:
            coords = list(ls.coords)
            if coords:
                ends.append(coords[0])
                ends.append(coords[-1])
        # Keep only distinct points (they are all on the hull edge).
        uniq: list[tuple[float, float]] = []
        for e in ends:
            if not any(_near(e, u) for u in uniq):
                uniq.append(e)
        return uniq
    return []


def _split_ring_in_place(
    ring: list[tuple[float, float]],
    ips: list[tuple[float, float]],
) -> None:
    """Insert intersection points into a CLOSED ring (last == first),
    keeping ring order.  KiCad splits the hull chain at each path
    intersection so shared hull/path vertices can merge in the graph.

    A ring of N open vertices has N segments (the last one closes back to
    the first); intersections on the closing segment belong after the last
    open vertex."""
    if not ips or len(ring) < 4:
        return
    open_chain = ring[:-1]
    n = len(open_chain)
    closed = open_chain + [open_chain[0]]
    line = LineString(closed)
    placed = sorted(
        (line.project(Point(*ip)), ip) for ip in ips if not any(_near(ip, p) for p in open_chain)
    )
    if not placed:
        return
    cum: list[float] = [0.0]
    for i in range(n):
        cum.append(
            cum[-1] + math.hypot(closed[i + 1][0] - closed[i][0], closed[i + 1][1] - closed[i][1])
        )
    out: list[tuple[float, float]] = []
    pending = list(placed)
    # Segment i runs open_chain[i] -> open_chain[(i+1) % n]; an intersection
    # with arc in (cum[i], cum[i+1]) belongs between those two vertices.
    # Start with the ring's first vertex, then walk segments.
    out.append(open_chain[0])
    for i in range(n):
        seg_start = cum[i]
        seg_end = cum[i + 1]
        while pending and pending[0][0] <= seg_end - _EPS:
            d, ip = pending.pop(0)
            if d > seg_start + _EPS:
                out.append(ip)
        if i < n - 1:
            out.append(open_chain[i + 1])
    # Re-close the ring: the last open vertex was emitted after segment
    # n-1 (the closing segment); append the closing duplicate.
    ring[:] = out + [open_chain[0]]


def _project_point_to_segment(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, float] | None:
    """Orthogonal projection of p onto segment a-b, clamped."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < _EPS:
        return None
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2
    t = max(0.0, min(1.0, t))
    return (ax + t * dx, ay + t * dy)


def _simplify_collinear(
    pts: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove middle points of collinear runs (angle within tolerance)."""
    if len(pts) < 3:
        return pts
    out = [pts[0], pts[1]]
    for p in pts[2:]:
        a = out[-2]
        b = out[-1]
        cross = (b[0] - a[0]) * (p[1] - b[1]) - (b[1] - a[1]) * (p[0] - b[0])
        if abs(cross) < 1e-9:
            out[-1] = p
        else:
            out.append(p)
    return out
