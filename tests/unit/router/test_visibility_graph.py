"""Unit tests for kcaa.router.visibility_graph."""

from __future__ import annotations

from shapely.geometry import Polygon

from kcaa.router.visibility_graph import (
    RouteNode,
    VisibilityGraph,
    build_visibility_graph,
)
from kcaa.router.world_model import Obstacle


def _rect_obstacle(x1: float, y1: float, x2: float, y2: float, layer: str = "F") -> Obstacle:
    return Obstacle(
        shape=Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]),
        layers=frozenset({layer}),
        net=None,
        kind="footprint",
    )


class TestRouteNode:
    def test_distance(self):
        a = RouteNode(0.0, 0.0, "F", 0)
        b = RouteNode(3.0, 4.0, "F", 1)
        assert a.distance(b) == 5.0

    def test_distance_ignores_layer(self):
        a = RouteNode(0.0, 0.0, "F", 0)
        b = RouteNode(3.0, 4.0, "B", 1)
        assert a.distance(b) == 5.0


class TestGraphBuilding:
    def test_empty_obstacles_yields_only_start_and_end(self):
        g = build_visibility_graph([], ["F"], (0.0, 0.0), (10.0, 10.0))
        # 2 nodes per layer × 1 layer = 2.
        assert len(g.nodes) == 2
        assert g.adj[0] == {1}
        assert g.adj[1] == {0}

    def test_obstacle_vertices_become_nodes(self):
        # 5x5 obstacle between S and G
        obs = _rect_obstacle(4.0, 4.0, 6.0, 6.0)
        g = build_visibility_graph([obs], ["F"], (0.0, 0.0), (10.0, 10.0))
        # 2 endpoints + 4 unique corner vertices (closed ring yields 5 coords)
        # We accept 6 OR 7 because shapely may dedupe.
        assert 6 <= len(g.nodes) <= 7

    def test_obstacle_blocks_some_edges(self):
        obs = _rect_obstacle(4.0, 4.0, 6.0, 6.0)
        g = build_visibility_graph([obs], ["F"], (0.0, 0.0), (10.0, 10.0))
        end_node = next(n for n in g.nodes if (n.x, n.y) == (10.0, 10.0))
        start_node = next(n for n in g.nodes if (n.x, n.y) == (0.0, 0.0))
        assert end_node.node_id not in g.adj[start_node.node_id]

    def test_same_layer_only(self):
        obs_f = _rect_obstacle(4.0, 4.0, 6.0, 6.0, layer="F")
        obs_b = _rect_obstacle(4.0, 4.0, 6.0, 6.0, layer="B")
        g = build_visibility_graph([obs_f, obs_b], ["F"], (0.0, 0.0), (10.0, 10.0))
        # Only the F obstacle contributes vertices (4 unique corners, possibly 5 with closing point)
        f_vertex_count = sum(1 for n in g.nodes if 4.0 <= n.x <= 6.0 and 4.0 <= n.y <= 6.0)
        assert f_vertex_count >= 4
        # No nodes from the B obstacle
        assert all(n.layer == "F" for n in g.nodes)

    def test_same_net_obstacle_excluded(self):
        # A track on the routing net — should not be added as obstacle
        obs = Obstacle(
            shape=Polygon([(0.0, 0.0), (5.0, 0.0), (5.0, 0.2), (0.0, 0.2)]),
            layers=frozenset({"F"}),
            net="VCC",  # same net as the route
            kind="track",
        )
        g = build_visibility_graph([obs], ["F"], (0.0, -1.0), (10.0, -1.0))
        # No obstacle vertices added (start + end on F only)
        assert len(g.nodes) == 2

    def test_empty_layers_raises(self):
        import pytest

        with pytest.raises(ValueError):
            build_visibility_graph([], [], (0.0, 0.0), (1.0, 1.0))


class TestGraphAddEdge:
    def test_add_node_and_edge(self):
        g = VisibilityGraph()
        n1 = RouteNode(0.0, 0.0, "F", 0)
        n2 = RouteNode(1.0, 0.0, "F", 1)
        g.add_node(n1)
        g.add_node(n2)
        g.add_edge(0, 1)
        assert 1 in g.adj[0]
        assert 0 in g.adj[1]

    def test_add_edge_to_self_is_noop(self):
        g = VisibilityGraph()
        g.add_node(RouteNode(0.0, 0.0, "F", 0))
        g.add_edge(0, 0)
        assert g.adj[0] == set()

    def test_add_via_edge_marks_via(self):
        g = VisibilityGraph()
        g.add_node(RouteNode(0.0, 0.0, "F", 0))
        g.add_node(RouteNode(0.0, 0.0, "B", 1))
        g.add_via_edge(0, 1)
        assert g.is_via_edge(0, 1)
        assert g.is_via_edge(1, 0)
        assert 1 in g.adj[0]
        assert 0 in g.adj[1]

    def test_neighbors(self):
        g = VisibilityGraph()
        for i in range(3):
            g.add_node(RouteNode(float(i), 0.0, "F", i))
        g.add_edge(0, 1)
        g.add_edge(0, 2)
        assert set(g.neighbors(0)) == {1, 2}
        assert set(g.neighbors(1)) == {0}


# ---------------------------------------------------------------------------
# Multi-layer
# ---------------------------------------------------------------------------


class TestMultiLayer:
    def test_nodes_built_on_each_layer(self):
        """Both F and B get the start/end vertices."""
        g = build_visibility_graph([], ["F", "B"], (0.0, 0.0), (10.0, 10.0))
        layers = {n.layer for n in g.nodes}
        assert layers == {"F", "B"}
        # 2 nodes per layer.
        assert len(g.nodes) == 4

    def test_via_edge_added_when_position_clear(self):
        g = build_visibility_graph(
            [],
            ["F", "B"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "B")],
        )
        f_start = next(n for n in g.nodes if n.layer == "F" and (n.x, n.y) == (0.0, 0.0))
        b_start = next(n for n in g.nodes if n.layer == "B" and (n.x, n.y) == (0.0, 0.0))
        assert g.is_via_edge(f_start.node_id, b_start.node_id)
        # Default: first via is 2.0 mm, each additional adds 0.5 mm.
        assert abs(g.via_cost_fn(0) - 2.0) < 1e-9
        assert abs(g.via_cost_fn(1) - 2.5) < 1e-9

    def test_no_via_edge_when_layer_not_in_pair(self):
        g = build_visibility_graph(
            [],
            ["F", "B", "In1"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "B")],  # no (F, In1) or (B, In1)
        )
        f_start = next(n for n in g.nodes if n.layer == "F" and (n.x, n.y) == (0.0, 0.0))
        in1_start = next(n for n in g.nodes if n.layer == "In1" and (n.x, n.y) == (0.0, 0.0))
        assert not g.is_via_edge(f_start.node_id, in1_start.node_id)

    def test_no_via_edge_when_position_blocked(self):
        # An obstacle covering (0,0) on B → the (0,0) position is not via-legal
        # → no via edge between F and B at that point.
        blocked = _rect_obstacle(-1.0, -1.0, 1.0, 1.0, layer="B")
        g = build_visibility_graph(
            [blocked],
            ["F", "B"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "B")],
        )
        f_start = next(n for n in g.nodes if n.layer == "F" and (n.x, n.y) == (0.0, 0.0))
        b_start = next(n for n in g.nodes if n.layer == "B" and (n.x, n.y) == (0.0, 0.0))
        # Position (0,0) is inside the B obstacle → not via-legal → no via edge.
        assert not g.is_via_edge(f_start.node_id, b_start.node_id)

    def test_via_edge_respects_via_pairs(self):
        g = build_visibility_graph(
            [],
            ["F", "B", "In1"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "In1")],
        )
        f_start = next(n for n in g.nodes if n.layer == "F" and (n.x, n.y) == (0.0, 0.0))
        b_start = next(n for n in g.nodes if n.layer == "B" and (n.x, n.y) == (0.0, 0.0))
        in1_start = next(n for n in g.nodes if n.layer == "In1" and (n.x, n.y) == (0.0, 0.0))
        # (F, In1) is requested → via edge.
        assert g.is_via_edge(f_start.node_id, in1_start.node_id)
        # (F, B) is not requested → no via edge.
        assert not g.is_via_edge(f_start.node_id, b_start.node_id)
        # (B, In1) is not requested → no via edge.
        assert not g.is_via_edge(b_start.node_id, in1_start.node_id)

    def test_via_legal_position_creates_missing_layer_node(self):
        # Place a small obstacle in the *middle* of the routing area on F only.
        # Its corner (4, 4) is a candidate on F (obstacle vertex); on B there
        # is no obstacle, so the (4, 4) position is not yet a B candidate.
        # The position (4, 4) is via-legal (free on both layers) so the
        # builder should add a B node at (4, 4) and connect it to F via a
        # via edge.
        obs = _rect_obstacle(4.0, 4.0, 6.0, 6.0, layer="F")
        g = build_visibility_graph(
            [obs],
            ["F", "B"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "B")],
        )
        f_corner = [n for n in g.nodes if n.layer == "F" and (n.x, n.y) == (4.0, 4.0)]
        b_corner = [n for n in g.nodes if n.layer == "B" and (n.x, n.y) == (4.0, 4.0)]
        assert f_corner, "F obstacle corner should be a candidate on F"
        assert b_corner, "via-legal point (4,4) should be added to B layer"
        assert g.is_via_edge(f_corner[0].node_id, b_corner[0].node_id)

    def test_single_layer_no_via_edges_when_pairs_empty(self):
        g = build_visibility_graph([], ["F"], (0.0, 0.0), (10.0, 10.0))
        assert g.via_edges == set()

    def test_custom_via_cost_fn(self):
        g = build_visibility_graph(
            [],
            ["F", "B"],
            (0.0, 0.0),
            (10.0, 10.0),
            via_pairs=[("F", "B")],
            via_cost_fn=lambda n: 7.5,
        )
        assert abs(g.via_cost_fn(1) - 7.5) < 1e-9
