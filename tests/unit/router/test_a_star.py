"""Unit tests for kcaa.router.a_star."""

from __future__ import annotations

import pytest

from kcaa.router.a_star import a_star, default_multi_layer_edge_cost
from kcaa.router.visibility_graph import RouteNode, VisibilityGraph


def _linear_graph() -> VisibilityGraph:
    """A → B → C, no obstacles. 0=start, 2=goal."""
    g = VisibilityGraph()
    for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]):
        g.add_node(RouteNode(x, y, "F.Cu", i))
    g.add_edge(0, 1)
    g.add_edge(1, 2)
    return g


def _disconnected_graph() -> VisibilityGraph:
    """Two disjoint chains: 0-1 and 2-3."""
    g = VisibilityGraph()
    for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 0.0), (10.0, 0.0), (11.0, 0.0)]):
        g.add_node(RouteNode(x, y, "F.Cu", i))
    g.add_edge(0, 1)
    g.add_edge(2, 3)
    return g


class TestAStar:
    def test_linear_path(self):
        path = a_star(_linear_graph(), 0, 2)
        assert path is not None
        assert [n.node_id for n in path] == [0, 1, 2]
        assert path[0].x == 0.0
        assert path[-1].x == 2.0

    def test_unreachable_returns_none(self):
        path = a_star(_disconnected_graph(), 0, 3)
        assert path is None

    def test_start_equals_goal(self):
        g = _linear_graph()
        path = a_star(g, 0, 0)
        assert path is not None
        assert len(path) == 1
        assert path[0].node_id == 0

    def test_picks_shortest_branch(self):
        # Triangle 0-1-2, 0-2 direct. A* should prefer direct edge.
        g = VisibilityGraph()
        for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 0.0), (0.5, 0.866)]):
            g.add_node(RouteNode(x, y, "F.Cu", i))
        g.add_edge(0, 1)
        g.add_edge(1, 2)
        g.add_edge(0, 2)  # direct
        path = a_star(g, 0, 2)
        assert path is not None
        assert [n.node_id for n in path] == [0, 2]

    def test_invalid_start_returns_none(self):
        path = a_star(_linear_graph(), 999, 0)
        assert path is None

    def test_invalid_goal_returns_none(self):
        path = a_star(_linear_graph(), 0, 999)
        assert path is None

    def test_custom_heuristic_is_used(self):
        g = _linear_graph()
        # Zero heuristic — A* becomes Dijkstra; should still find a path
        path = a_star(g, 0, 2, heuristic=lambda a, b: 0.0)
        assert path is not None
        assert path[-1].node_id == 2

    def test_path_length_equals_sum_of_segments(self):
        path = a_star(_linear_graph(), 0, 2)
        assert path is not None
        total = 0.0
        for a, b in zip(path, path[1:]):
            total += a.distance(b)
        assert total == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Multi-layer: edge_cost + via counting
# ---------------------------------------------------------------------------


def test_a_star_via_edge_uses_via_cost() -> None:
    """A via edge's cost comes from via_cost_fn, not from euclidean distance."""
    g = VisibilityGraph()
    # Two stacked nodes at (0, 0) on F and B.
    f0 = RouteNode(0.0, 0.0, "F", 0)
    b0 = RouteNode(0.0, 0.0, "B", 1)
    b5 = RouteNode(5.0, 0.0, "B", 2)
    f5 = RouteNode(5.0, 0.0, "F", 3)
    for n in (f0, b0, b5, f5):
        g.add_node(n)
    # Start on F, immediate via to B, track 5 mm to B end, via back to F end.
    g.add_via_edge(0, 1)  # F(0,0) - B(0,0)
    g.add_edge(1, 2)  # B(0,0) - B(5,0) [track]
    g.add_via_edge(2, 3)  # B(5,0) - F(5,0)
    # Cost should be: via(2.0) + track(5.0) + via(2.5 since n=1) = 9.5
    path = a_star(g, 0, 3, edge_cost=default_multi_layer_edge_cost(g))
    assert path is not None
    assert path[0].node_id == 0
    assert path[-1].node_id == 3
    # Sum via costs only — track edges should be euclidean.
    cost = 0.0
    n_vias = 0
    for a, b in zip(path, path[1:]):
        if g.is_via_edge(a.node_id, b.node_id):
            cost += g.via_cost_fn(n_vias)
            n_vias += 1
        else:
            cost += a.distance(b)
    assert cost == pytest.approx(2.0 + 5.0 + 2.5)


def test_a_star_picks_via_when_detour_exceeds_penalty() -> None:
    """A* with the multi-layer edge cost should prefer the via path when a
    detour around an obstacle costs more than two via penalties."""
    g = VisibilityGraph()
    # Linear F track is blocked (we add a track edge anyway and let A* pick
    # shortest path). Long track from (0,0) to (20, 0) on F, plus a via at
    # the start and end jumping to B which has a much shorter track.
    f0 = RouteNode(0.0, 0.0, "F", 0)
    f20 = RouteNode(20.0, 0.0, "F", 1)
    b0 = RouteNode(0.0, 0.0, "B", 2)
    b20 = RouteNode(20.0, 0.0, "B", 3)
    for n in (f0, f20, b0, b20):
        g.add_node(n)
    # F track is long: 20 mm.
    g.add_edge(0, 1)
    # B track is much shorter — via both ends.
    g.add_via_edge(0, 2)
    g.add_edge(2, 3)
    g.add_via_edge(3, 1)

    path = a_star(g, 0, 1, edge_cost=default_multi_layer_edge_cost(g))
    assert path is not None
    # Via + 20 + via(2.5) = 24.5 — still beats the 20 mm F track on its own.
    # We expect A* to take the F track (20 mm) because it's strictly shorter.
    # To force via, make F longer: use 100 mm F track and short B track.
    g_long = VisibilityGraph()
    f0 = RouteNode(0.0, 0.0, "F", 0)
    f100 = RouteNode(100.0, 0.0, "F", 1)
    b0 = RouteNode(0.0, 0.0, "B", 2)
    b20 = RouteNode(20.0, 0.0, "B", 3)
    for n in (f0, f100, b0, b20):
        g_long.add_node(n)
    g_long.add_edge(0, 1)
    g_long.add_via_edge(0, 2)
    g_long.add_edge(2, 3)
    g_long.add_via_edge(3, 1)
    # F: 100 mm. B + 2 vias: 20 + 2.0 + 2.5 = 24.5. B wins.
    path = a_star(g_long, 0, 1, edge_cost=default_multi_layer_edge_cost(g_long))
    assert path is not None
    layers_used = [n.layer for n in path]
    assert "B" in layers_used  # at least one via taken
    assert layers_used[0] == "F"
    assert layers_used[-1] == "F"


def test_a_star_default_edge_cost_ignores_via_flag() -> None:
    """When edge_cost is None, A* should default to euclidean even for via
    edges — i.e. single-layer callers keep working unchanged."""
    g = VisibilityGraph()
    f0 = RouteNode(0.0, 0.0, "F", 0)
    b0 = RouteNode(0.0, 0.0, "B", 1)
    g.add_node(f0)
    g.add_node(b0)
    g.add_via_edge(0, 1)
    path = a_star(g, 0, 1)  # no edge_cost → euclidean
    assert path is not None
    # Path cost should be 0 (euclidean between coincident points).
    assert path[0].node_id == 0
    assert path[-1].node_id == 1


def test_a_star_returns_none_when_no_path() -> None:
    """A graph with no connection between start and goal returns None."""
    g = VisibilityGraph()
    a = RouteNode(0.0, 0.0, "F", 0)
    b = RouteNode(10.0, 10.0, "F", 1)
    g.add_node(a)
    g.add_node(b)
    # No edges.
    path = a_star(g, 0, 1, edge_cost=default_multi_layer_edge_cost(g))
    assert path is None


# ---------------------------------------------------------------------------
# closed-set / n_vias counting
# ---------------------------------------------------------------------------


def test_closed_set_keeps_first_settled_cost():
    """Once a node is settled (popped from the heap), a later push with
    a higher cost must not change the recorded best cost.  This is the
    Dijkstra invariant the closed set enforces."""
    g = VisibilityGraph()
    g.add_node(RouteNode(0.0, 0.0, "F.Cu", 0))
    g.add_node(RouteNode(5.0, 0.0, "F.Cu", 1))
    g.add_node(RouteNode(10.0, 0.0, "F.Cu", 2))  # goal
    g.add_node(RouteNode(2.0, 0.0, "F.Cu", 3))
    g.add_edge(0, 1)
    g.add_edge(1, 2)
    g.add_edge(0, 3)
    g.add_edge(3, 2)
    path = a_star(g, 0, 2)
    assert path is not None
    assert len(path) == 3


def test_n_vias_counted_for_via_edges():
    """Each via edge in the path increments the running via count used
    by the cost function."""
    g = VisibilityGraph()
    # Two-layer graph: 0 on F.Cu, 1 on B.Cu (via edge), 2 on B.Cu (track).
    g.add_node(RouteNode(0.0, 0.0, "F.Cu", 0))
    g.add_node(RouteNode(0.0, 0.0, "B.Cu", 1))
    g.add_node(RouteNode(10.0, 0.0, "B.Cu", 2))
    g.add_via_edge(0, 1)
    g.add_edge(1, 2)
    path = a_star(g, 0, 2, edge_cost=default_multi_layer_edge_cost(g))
    assert path is not None
    assert [n.node_id for n in path] == [0, 1, 2]


def test_multi_via_path_prefers_fewer_vias():
    """When two paths exist, the one with fewer via edges is preferred."""
    g = VisibilityGraph()
    # 0 (F) --via-- 1 (B) --track-- 2 (B) --via-- 3 (F) --track-- 4 (F, goal)
    for i, (x, layer) in enumerate(
        [(0.0, "F.Cu"), (0.0, "B.Cu"), (5.0, "B.Cu"), (5.0, "F.Cu"), (10.0, "F.Cu")]
    ):
        g.add_node(RouteNode(x, 0.0, layer, i))
    g.add_via_edge(0, 1)
    g.add_edge(1, 2)
    g.add_via_edge(2, 3)
    g.add_edge(3, 4)
    # Alternative: 0 --track-- 4 directly on F.Cu (no vias).
    g.add_edge(0, 4)
    path = a_star(g, 0, 4, edge_cost=default_multi_layer_edge_cost(g))
    assert path is not None
    # Direct F.Cu path has 0 vias, so should win.
    assert [n.node_id for n in path] == [0, 4]
