"""
Visibility graph: a sparse graph over candidate routing nodes.

For a 2D routing problem with polygonal obstacles, the **shortest** obstacle-
avoiding path between ``S`` and ``G`` has a property: every vertex of the path
is either ``S``, ``G``, or a vertex of some obstacle. We build a graph whose
nodes are exactly those candidate points, and whose edges are the pairs of
nodes whose straight-line segment is **clear of all obstacles** (visibility).

A\\* on this graph is fast and produces geometric shortest paths.

Node layer
----------

Every node carries a layer. Edges come in two flavours:

* **Track edges** connect nodes on the same layer; cost is Euclidean distance.
* **Via edges** connect nodes at the same (x, y) on different layers; cost is
  a per-via-count penalty (see :mod:`kcaa.router.router`).

Spatial prefilter
-----------------

Visibility checks are O(n) in the number of obstacles. We prefilter with an
rtree over obstacle bounding boxes so that only nearby obstacles are checked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import math

from rtree import index
from shapely.geometry import LineString

from kcaa.router.world_model import Obstacle


@dataclass(frozen=True)
class RouteNode:
    """A candidate point on the visibility graph."""

    x: float
    y: float
    layer: str
    node_id: int  # global integer id for graph indexing

    def distance(self, other: RouteNode) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


# Default via-cost function: first via is 2 mm, each additional adds 0.5 mm.
# ``n`` is the number of via edges already taken on the path BEFORE this
# via edge, so n=0 means "this is the first via".


def DEFAULT_VIA_COST_FN(n: int) -> float:
    return 2.0 + 0.5 * n


@dataclass
class VisibilityGraph:
    """Adjacency map from node_id to set of visible node_ids.

    Via edges (cross-layer) are tracked separately in :attr:`via_edges` so the
    A\\* search can compute their cost lazily from the running via count.
    """

    nodes: list[RouteNode] = field(default_factory=list)
    adj: dict[int, set[int]] = field(default_factory=dict)
    # set of (a, b) pairs that are via edges; both directions stored.
    via_edges: set[tuple[int, int]] = field(default_factory=set)
    via_cost_fn: Callable[[int], float] = DEFAULT_VIA_COST_FN

    def add_node(self, node: RouteNode) -> int:
        nid = node.node_id
        if nid in self.adj:
            return nid  # already added
        self.nodes.append(node)
        self.adj[nid] = set()
        return nid

    def add_edge(self, a: int, b: int) -> None:
        if a == b:
            return
        self.adj.setdefault(a, set()).add(b)
        self.adj.setdefault(b, set()).add(a)

    def add_via_edge(self, a: int, b: int) -> None:
        """Add a via (cross-layer) edge between ``a`` and ``b``."""
        if a == b:
            return
        self.adj.setdefault(a, set()).add(b)
        self.adj.setdefault(b, set()).add(a)
        self.via_edges.add((a, b))
        self.via_edges.add((b, a))

    def is_via_edge(self, a: int, b: int) -> bool:
        return (a, b) in self.via_edges

    def neighbors(self, nid: int) -> Iterable[int]:
        return self.adj.get(nid, ())


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_visibility_graph(
    obstacles: list[Obstacle],
    layers: list[str],
    start: tuple[float, float],
    end: tuple[float, float],
    via_pairs: list[tuple[str, str]] | None = None,
    via_cost_fn: Callable[[int], float] = DEFAULT_VIA_COST_FN,
    start_layer: str | None = None,
    end_layer: str | None = None,
) -> VisibilityGraph:
    """Construct a multi-layer visibility graph.

    The graph contains per-layer track edges and cross-layer via edges:

    1. **Per-layer nodes**: on each layer, candidate nodes are the start, end
       and the vertices of obstacles that sit on that layer.
    2. **Per-layer track edges**: visibility connections between same-layer
       nodes whose straight line crosses no obstacle on that layer.
    3. **Cross-layer via nodes**: an (x, y) is via-legal if it lies **outside
       every obstacle on every routing layer**. For every via-legal (x, y)
       that is missing from a layer, a node is added on that layer.
    4. **Via edges**: for each via-legal (x, y) and every pair in
       ``via_pairs`` whose two layers both have a node at that (x, y), add a
       via edge with cost ``via_cost_fn(via_count_so_far)``.

    Same-net obstacles (``net is not None``) are not added as obstacles and
    their vertices are also excluded from the graph — a track should not be
    told to "go around" itself.

    Args:
        obstacles: All obstacles; only those whose ``layers`` overlap with
            ``layers`` participate.
        layers: Routing layers (e.g. ``["F.Cu", "B.Cu"]``).
        start: Start point in world coordinates.
        end: End point in world coordinates.
        via_pairs: Optional list of ``(top_layer, bottom_layer)`` tuples.
            Via edges are only added for these layer pairs. Empty / ``None``
            disables via edges entirely.
        via_cost_fn: Function ``(n_vias_so_far) -> float`` giving the cost
            of a via edge when the running via count is ``n_vias_so_far``.
        start_layer: Layer the start point lives on.  When ``None`` the
            start is added to every routing layer (legacy behaviour, which
            creates a phantom goal at the start point on every other
            layer).  When set, the start node is added only to this
            layer — the canonical start is ``node_id == 0``.
        end_layer: Layer the end point lives on.  When ``None`` the end is
            added to every routing layer.  When set, the end node is
            added only to this layer — the canonical goal is
            ``node_id == 1``.

    Returns:
        A :class:`VisibilityGraph`.
    """
    via_pairs = list(via_pairs or [])
    if not layers:
        raise ValueError("layers must be a non-empty list")
    if start_layer is not None and start_layer not in layers:
        raise ValueError(f"start_layer {start_layer!r} not in routing layers {layers}")
    if end_layer is not None and end_layer not in layers:
        raise ValueError(f"end_layer {end_layer!r} not in routing layers {layers}")

    graph = VisibilityGraph(via_cost_fn=via_cost_fn)
    counter = 0

    # Per-layer rtree keyed by layer → (rtree, obstacles_on_layer).
    per_layer_rtree: dict[str, index.Index] = {}
    per_layer_obs: dict[str, list[Obstacle]] = {}
    for layer in layers:
        layer_obs = [o for o in obstacles if layer in o.layers]
        per_layer_obs[layer] = layer_obs
        rtree_idx = index.Index()
        for i, o in enumerate(layer_obs):
            rtree_idx.insert(i, o.shape.bounds)
        per_layer_rtree[layer] = rtree_idx

    # Per-layer candidate set: dict layer → list[(x, y)].
    per_layer_pts: dict[str, list[tuple[float, float]]] = {layer: [] for layer in layers}

    def _new_node(x: float, y: float, layer: str) -> RouteNode:
        nonlocal counter
        node = RouteNode(x=x, y=y, layer=layer, node_id=counter)
        graph.add_node(node)
        counter += 1
        per_layer_pts[layer].append((x, y))
        return node

    # Collect obstacle vertices and start/end on each layer.  The canonical
    # start is node 0 (created first) and the canonical goal is node 1
    # (created second).  Obstacle vertices are added afterwards.  On
    # every layer we also add a "via-legal" mirror of the start/end at
    # the same (x, y) (added as a *new* node so it does not collide with
    # the canonical start/end) so that via transitions can hop to the
    # correct layer.
    per_layer_vertex_nodes: dict[str, list[RouteNode]] = {}
    canonical_start_id: int | None = None
    canonical_end_id: int | None = None
    # Pass 1: create the canonical start/end on their assigned layers.
    if start_layer is not None:
        start_node = _new_node(start[0], start[1], start_layer)
        canonical_start_id = start_node.node_id
        per_layer_vertex_nodes[start_layer] = [start_node]
    if end_layer is not None:
        end_node = _new_node(end[0], end[1], end_layer)
        canonical_end_id = end_node.node_id
        per_layer_vertex_nodes.setdefault(end_layer, []).append(end_node)
    # Pass 2: every layer gets obstacle vertices AND a mirror of the
    # start/end (added as a new node so it does not collide with the
    # canonical one).
    for layer in layers:
        if layer not in per_layer_vertex_nodes:
            per_layer_vertex_nodes[layer] = []
    for layer in layers:
        nodes = per_layer_vertex_nodes[layer]
        # Start mirror (only on layers other than the canonical start layer).
        if layer != start_layer and not any(n.x == start[0] and n.y == start[1] for n in nodes):
            nodes.append(_new_node(start[0], start[1], layer))
        # End mirror (only on layers other than the canonical end layer).
        if layer != end_layer and not any(n.x == end[0] and n.y == end[1] for n in nodes):
            nodes.append(_new_node(end[0], end[1], layer))
        for o in per_layer_obs[layer]:
            # Skip same-net obstacles: their tracks are part of the route.
            if o.net is not None:
                continue
            for x, y in o.shape.exterior.coords:
                nodes.append(_new_node(x, y, layer))

    if canonical_start_id is not None and canonical_start_id != 0:
        raise RuntimeError(
            f"internal error: canonical start id is {canonical_start_id}, expected 0"
        )
    if canonical_end_id is not None and canonical_end_id != 1:
        raise RuntimeError(f"internal error: canonical end id is {canonical_end_id}, expected 1")

    # When ``start_layer``/``end_layer`` were not provided we added the
    # start/end to every layer.  The first one created is the canonical
    # start (id 0) and the second is the canonical end (id 1) only if the
    # legacy per-layer add added them in that order — which it does
    # because we always add start before end inside pass 2.  We can't
    # assert this from a single ``canonical_*_id`` any more; legacy
    # callers must rely on the graph structure.  Multi-layer callers
    # (the new code path) get the strict invariant.

    # Build per-layer track edges (visibility).
    for layer in layers:
        layer_obs = per_layer_obs[layer]
        rtree_idx = per_layer_rtree[layer]
        nodes = per_layer_vertex_nodes[layer]
        n = len(nodes)
        for i in range(n):
            for j in range(i + 1, n):
                a = nodes[i]
                b = nodes[j]
                if _is_visible(a, b, layer_obs, rtree_idx):
                    graph.add_edge(a.node_id, b.node_id)

    # Identify via-legal (x, y) positions: the union of all candidate (x, y)
    # that are free of obstacles on every layer. An (x, y) is free on a layer
    # if a tiny probe (eps-radius circle) at that point does not intersect
    # any obstacle's interior.
    eps = 1e-6
    all_pts: set[tuple[float, float]] = set()
    for pts in per_layer_pts.values():
        all_pts.update(pts)
    via_legal: set[tuple[float, float]] = set()
    for pt in all_pts:
        if all(_is_free(pt, layer_obs, eps) for layer_obs in per_layer_obs.values()):
            via_legal.add(pt)

    # For each via-legal (x, y), add a node on any layer that's missing one.
    # We keep the original node (whichever was created first on that layer) and
    # only create new nodes for layers without an entry at that (x, y).
    def _find_node(layer: str, pt: tuple[float, float]) -> RouteNode | None:
        for node in per_layer_vertex_nodes[layer]:
            if node.x == pt[0] and node.y == pt[1]:
                return node
        return None

    for pt in via_legal:
        for layer in layers:
            if _find_node(layer, pt) is None:
                new_node = _new_node(pt[0], pt[1], layer)
                per_layer_vertex_nodes[layer].append(new_node)

    # Add via edges. A via edge connects the same (x, y) across two layers
    # that are in ``via_pairs``; both layers must have a node at that point.
    for pt in via_legal:
        nodes_by_layer = {layer: _find_node(layer, pt) for layer in layers}
        for top, bot in via_pairs:
            a = nodes_by_layer.get(top)
            b = nodes_by_layer.get(bot)
            if a is not None and b is not None:
                graph.add_via_edge(a.node_id, b.node_id)

    return graph


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def _is_visible(
    a: RouteNode,
    b: RouteNode,
    obstacles: list[Obstacle],
    rtree_idx: index.Index,
) -> bool:
    """True iff the segment a→b crosses no obstacle interior or boundary."""
    seg = LineString([(a.x, a.y), (b.x, b.y)])
    # Prefilter: only obstacles whose bbox intersects the segment bbox.
    minx = min(a.x, b.x)
    maxx = max(a.x, b.x)
    miny = min(a.y, b.y)
    maxy = max(a.y, b.y)
    candidates = list(rtree_idx.intersection((minx, miny, maxx, maxy)))
    for i in candidates:
        o = obstacles[i]
        if seg.intersects(o.shape) and not seg.touches(o.shape):
            return False
    return True


def _is_free(
    pt: tuple[float, float],
    obstacles: list[Obstacle],
    eps: float,
) -> bool:
    """True iff ``pt`` does not sit in the interior of any obstacle.

    Used to test whether a via node is legal on a given layer. Boundary
    contact is treated as free (the centre of a footprint pad is allowed to
    be on the edge of an obstacle polygon).
    """
    from shapely.geometry import Point

    probe = Point(pt[0], pt[1])
    # Use a tiny buffer to detect "inside" — Point.within is exact.
    for o in obstacles:
        # Same-net obstacles are not in the way (their copper is the route).
        if o.net is not None:
            continue
        if o.shape.intersects(probe) and not o.shape.touches(probe):
            # Touches at the boundary counts as "on" — still free for via purposes.
            # But probe is a point so it can only touch at the boundary exactly.
            # A genuine interior intersection means blocked.
            # Distinguish: interior if contains but not on boundary.
            if o.shape.contains(probe):
                return False
            # Intersection at a single coordinate is "touches" — treat as free.
            if not o.shape.touches(probe):
                # Degenerate: intersection but neither contains nor touches —
                # treat as blocked.
                return False
    # ``eps`` is currently unused but kept as a parameter so we can switch to
    # buffered probes later if we hit edge cases with shapely's exact predicates.
    del eps
    return True
