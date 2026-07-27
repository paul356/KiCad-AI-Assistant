"""
A\\* shortest-path search on the visibility graph.

Standard textbook implementation with a Euclidean heuristic. Returns the
ordered list of :class:`kcaa.router.visibility_graph.RouteNode` from start
to goal, or ``None`` if no path exists.

Multi-layer
-----------

For multi-layer graphs the graph carries :attr:`VisibilityGraph.via_edges`
(per-edge via-flag) and :attr:`VisibilityGraph.via_cost_fn` (per-via-count
penalty). Pass ``edge_cost`` to a function that distinguishes track edges
from via edges — see :func:`default_multi_layer_edge_cost` for a ready-made
implementation that uses the graph's own ``via_edges``/``via_cost_fn``.

Heuristic admissibility: the Euclidean distance between two nodes is a
lower bound on the true cost of any path between them **only if** the
true edge cost is non-negative. Via penalties are non-negative by
construction (``via_cost_fn(n) >= 0`` for all ``n``), so the Euclidean
heuristic remains admissible.
"""

from __future__ import annotations

from collections.abc import Callable
import heapq

from kcaa.router.visibility_graph import RouteNode, VisibilityGraph


def default_multi_layer_edge_cost(
    graph: VisibilityGraph,
) -> Callable[[RouteNode, RouteNode, int], float]:
    """Build a default ``edge_cost`` function for a multi-layer graph.

    Returns a closure ``(a, b, n_vias_so_far) -> float`` that returns the
    graph's ``via_cost_fn(n_vias_so_far)`` for via edges and the Euclidean
    distance ``a.distance(b)`` for track edges.

    Args:
        graph: The visibility graph to query for via edges and cost.

    Returns:
        The edge-cost function suitable for ``a_star(..., edge_cost=...)``.
    """

    def edge_cost(a: RouteNode, b: RouteNode, n_vias_so_far: int) -> float:
        if graph.is_via_edge(a.node_id, b.node_id):
            return graph.via_cost_fn(n_vias_so_far)
        return a.distance(b)

    return edge_cost


def a_star(
    graph: VisibilityGraph,
    start_id: int,
    goal_id: int,
    heuristic: Callable[[RouteNode, RouteNode], float] | None = None,
    edge_cost: Callable[[RouteNode, RouteNode, int], float] | None = None,
) -> list[RouteNode] | None:
    """A\\* shortest-path search.

    Args:
        graph: The visibility graph to search.
        start_id: ``node_id`` of the start node.
        goal_id: ``node_id`` of the goal node.
        heuristic: Optional heuristic function. Defaults to Euclidean
            (admissible and consistent for non-negative edge costs).
        edge_cost: Optional per-edge cost function. Receives the two
            ``RouteNode`` endpoints and the running via count (number of
            via edges taken so far on the path). Defaults to Euclidean
            distance with a via count of 0 (i.e. via edges still cost
            euclidean distance). Multi-layer callers should pass
            :func:`default_multi_layer_edge_cost`.

    Returns:
        Ordered list of :class:`RouteNode` from start to goal, or ``None``
        if no path exists.
    """
    if start_id == goal_id:
        node = _node_by_id(graph, start_id)
        return [node] if node else None

    if heuristic is None:
        # Single-layer: plain Euclidean is admissible + consistent since
        # every edge cost equals euclidean distance.
        if edge_cost is None:
            heuristic = lambda a, b: a.distance(b)  # noqa: E731
        else:
            # Multi-layer: via costs are not Lipschitz-continuous w.r.t.
            # Euclidean distance, so we cannot construct a non-trivial
            # heuristic that is provably consistent for arbitrary via
            # penalty functions. Use h=0 (Dijkstra) for safety. Visibility
            # graphs are sparse and small enough that the lost speed is
            # negligible compared to the per-edge visibility cost.
            heuristic = lambda a, b: 0.0  # noqa: E731

    if edge_cost is None:
        # Single-layer default: every edge is a track edge.
        def edge_cost(a: RouteNode, b: RouteNode, n_vias_so_far: int) -> float:  # noqa: ARG001
            return a.distance(b)

    start_node = _node_by_id(graph, start_id)
    goal_node = _node_by_id(graph, goal_id)
    if start_node is None or goal_node is None:
        return None

    open_heap: list[tuple[float, int, RouteNode]] = []
    heapq.heappush(open_heap, (0.0, 0, start_node))
    counter = 0  # tiebreaker to avoid comparing RouteNode

    g_score: dict[int, float] = {start_id: 0.0}
    n_vias: dict[int, int] = {start_id: 0}
    came_from: dict[int, int] = {}
    closed: set[int] = set()

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        # Skip if we already finalized this node (lazy deletion).
        if current.node_id in closed:
            continue
        closed.add(current.node_id)
        if current.node_id == goal_id:
            return _reconstruct(graph, came_from, current.node_id)
        current_n_vias = n_vias[current.node_id]
        for nid in graph.neighbors(current.node_id):
            neighbor = _node_by_id(graph, nid)
            if neighbor is None or nid in closed:
                continue
            step_cost = edge_cost(current, neighbor, current_n_vias)
            tentative_g = g_score[current.node_id] + step_cost
            if tentative_g < g_score.get(nid, float("inf")):
                came_from[nid] = current.node_id
                g_score[nid] = tentative_g
                n_vias[nid] = current_n_vias + (1 if graph.is_via_edge(current.node_id, nid) else 0)
                f = tentative_g + heuristic(neighbor, goal_node)
                counter += 1
                heapq.heappush(open_heap, (f, counter, neighbor))
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _node_by_id(graph: VisibilityGraph, nid: int) -> RouteNode | None:
    if 0 <= nid < len(graph.nodes):
        return graph.nodes[nid]
    return None


def _reconstruct(
    graph: VisibilityGraph,
    came_from: dict[int, int],
    end_id: int,
) -> list[RouteNode]:
    path_ids = [end_id]
    cur = end_id
    while cur in came_from:
        cur = came_from[cur]
        path_ids.append(cur)
    path_ids.reverse()
    return [graph.nodes[nid] for nid in path_ids]
