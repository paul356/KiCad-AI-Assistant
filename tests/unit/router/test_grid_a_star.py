"""Unit tests for :mod:`kcaa.router.grid_a_star` — multi_layer_a_star and helpers."""

from __future__ import annotations

from shapely.geometry import Polygon

from kcaa.router.grid_a_star import (
    multi_layer_a_star,
)
from kcaa.router.world_model import Obstacle


def _make_box_obstacle(x: float, y: float, w: float, h: float, layer: str) -> Obstacle:
    """Create a rectangular obstacle on a single layer."""
    return Obstacle(
        shape=Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]),
        layers=frozenset({layer}),
        net="TEST",
        kind="keepout",
    )


# ── multi_layer_a_star ────────────────────────────────────────────────


class TestMultiLayerAStar:
    """Tests for the multi-layer A* grid router."""

    EMPTY_BBOX = (0.0, 0.0, 20.0, 20.0)

    def test_same_layer_returns_direct_path(self):
        """F.Cu → F.Cu should produce a path on a single layer (no via)."""
        obs_by_layer = {
            "F.Cu": [],
            "B.Cu": [],
        }
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),
            (18.0, 10.0),
            "F.Cu",
            "F.Cu",
            (("F.Cu", "B.Cu"),),
            self.EMPTY_BBOX,
        )
        assert result.path is not None
        assert result.cells_visited > 0
        # All nodes should be on F.Cu.
        for node in result.path:
            assert node.layer == "F.Cu"

    def test_cross_layer_inserts_via(self):
        """F.Cu → B.Cu inserts at least one via on an empty board."""
        obs_by_layer = {
            "F.Cu": [],
            "B.Cu": [],
        }
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),
            (18.0, 10.0),
            "F.Cu",
            "B.Cu",
            (("F.Cu", "B.Cu"),),
            self.EMPTY_BBOX,
        )
        assert result.path is not None
        layers_seen = [n.layer for n in result.path]
        # Must contain both layers.
        assert "F.Cu" in layers_seen
        assert "B.Cu" in layers_seen

    def test_unreachable_returns_none(self):
        """Block both layers at start point → no path."""
        obs = _make_box_obstacle(1.0, 9.0, 2.0, 2.0, "F.Cu")
        obs_by_layer = {
            "F.Cu": [obs],
            "B.Cu": [obs],  # same block on both layers
        }
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),  # inside the blocked box
            (18.0, 10.0),
            "F.Cu",
            "B.Cu",
            (("F.Cu", "B.Cu"),),
            self.EMPTY_BBOX,
        )
        assert result.path is None

    def test_via_cost_discourages_unnecessary_vias(self):
        """With high via_cost, a single-layer path is preferred even when vias available."""
        # Two paths from F.Cu→B.Cu:
        #   A: cross via immediately (short in xy, but pays via_cost)
        #   B: stay on F.Cu long, then via at the last moment
        # With high via_cost, path B is preferred.
        obs_by_layer = {
            "F.Cu": [],
            "B.Cu": [],
        }
        # via_cost = 100.0 should make A* prefer staying on the start
        # layer as long as possible, then via at the end.
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),
            (18.0, 10.0),
            "F.Cu",
            "B.Cu",
            (("F.Cu", "B.Cu"),),
            self.EMPTY_BBOX,
            via_cost=100.0,
        )
        assert result.path is not None
        # Count via transitions (layer changes).
        transitions = sum(
            1
            for i in range(1, len(result.path))
            if result.path[i].layer != result.path[i - 1].layer
        )
        # Should have exactly 1 via transition (no unnecessary stacking).
        assert transitions == 1

    def test_three_layer_route(self):
        """F.Cu → In1.Cu via B.Cu on an empty board."""
        obs_by_layer = {
            "F.Cu": [],
            "B.Cu": [],
            "In1.Cu": [],
        }
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),
            (18.0, 10.0),
            "F.Cu",
            "In1.Cu",
            (("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
            self.EMPTY_BBOX,
        )
        assert result.path is not None
        layers_seen = [n.layer for n in result.path]
        assert "F.Cu" in layers_seen
        assert "B.Cu" in layers_seen
        assert "In1.Cu" in layers_seen

    def test_via_blocked_by_destination_layer_obstacle(self):
        """Via cannot land at (x,y) if that cell is blocked on the target layer."""
        # Block B.Cu at a narrow corridor — via must find a clear spot.
        obs = _make_box_obstacle(8.0, 9.0, 4.0, 2.0, "B.Cu")
        obs_by_layer = {
            "F.Cu": [],
            "B.Cu": [obs],
        }
        result = multi_layer_a_star(
            obs_by_layer,
            (2.0, 10.0),
            (18.0, 10.0),
            "F.Cu",
            "B.Cu",
            (("F.Cu", "B.Cu"),),
            self.EMPTY_BBOX,
        )
        assert result.path is not None
        # The via point should not land on the blocked B.Cu cell.
        for i in range(1, len(result.path)):
            prev = result.path[i - 1]
            cur = result.path[i]
            if prev.layer != cur.layer:
                # via transition — cur is on the destination layer.
                # Its position should be outside the obstacle.
                assert not (8.0 <= cur.x <= 12.0 and 9.0 <= cur.y <= 11.0), (
                    f"via landed on blocked cell at ({cur.x:.2f},{cur.y:.2f})"
                )

    def test_default_via_cost_is_2mm(self):
        """The default via_cost should be 2.0 mm (constant value)."""
        from kcaa.router.grid_a_star import _VIA_COST

        assert _VIA_COST == 2.0


# ── _subtract_pad_aabb ────────────────────────────────────────────────


class TestSubtractPadAABB:
    """Tests for :func:`_subtract_pad_aabb` in router.py."""

    def test_subtract_fully_contained_pad(self):
        """Pad completely inside obstacle → obstacle split into donut-like shapes."""
        from kcaa.router.router import _subtract_pad_aabb

        obs = Obstacle(
            shape=Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            layers=frozenset({"F.Cu"}),
            net="VCC",
            kind="track",
        )
        # Pad (4, 4) → (6, 6) — fully inside the obstacle.
        result = _subtract_pad_aabb([obs], (5.0, 5.0), (2.0, 2.0))
        # Should still have one non-empty result.
        assert len(result) >= 1
        # The result area should equal original minus pad.
        total_area = sum(r.shape.area for r in result)
        expected_area = 100.0 - 4.0  # 10x10 minus 2x2
        assert abs(total_area - expected_area) < 0.01

    def test_subtract_edge_pad(self):
        """Pad overlapping obstacle edge → obstacle trimmed."""
        from kcaa.router.router import _subtract_pad_aabb

        obs = Obstacle(
            shape=Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
            layers=frozenset({"F.Cu"}),
            net="VCC",
            kind="track",
        )
        # Pad (3, 3) → (7, 7) — half overlapping.
        result = _subtract_pad_aabb([obs], (5.0, 5.0), (4.0, 4.0))
        assert len(result) >= 1

    def test_subtract_non_overlapping_pad(self):
        """Pad nowhere near obstacle → no change."""
        from kcaa.router.router import _subtract_pad_aabb

        obs = Obstacle(
            shape=Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
            layers=frozenset({"F.Cu"}),
            net="VCC",
            kind="track",
        )
        # Pad far away.
        result = _subtract_pad_aabb([obs], (50.0, 50.0), (2.0, 2.0))
        assert len(result) == 1
        # Original area unchanged.
        assert abs(result[0].shape.area - 25.0) < 0.01

    def test_subtract_from_multiple_obstacles(self):
        """Pad clearing from a list of obstacles."""
        from kcaa.router.router import _subtract_pad_aabb

        obs1 = Obstacle(
            shape=Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
            layers=frozenset({"F.Cu"}),
            net="NET_A",
            kind="track",
        )
        obs2 = Obstacle(
            shape=Polygon([(8, 0), (13, 0), (13, 5), (8, 5)]),
            layers=frozenset({"F.Cu"}),
            net="NET_B",
            kind="track",
        )
        # Pad overlaps obs1 only.
        result = _subtract_pad_aabb([obs1, obs2], (3.0, 2.5), (4.0, 3.0))
        # obs1 may be split into multiple pieces or one trimmed piece.
        # obs2 should be untouched.
        assert len(result) >= 2  # at least obs1 (trimmed) + obs2 (untouched)
        total_area = sum(r.shape.area for r in result)
        # obs1 was 25, pad subtracts 12 (4x3), so 13 + 25 = 38
        assert abs(total_area - 38.0) < 0.01

    def test_preserves_obstacle_metadata(self):
        """Subtracted obstacles keep their net, kind, layers, ref."""
        from kcaa.router.router import _subtract_pad_aabb

        obs = Obstacle(
            shape=Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            layers=frozenset({"B.Cu", "In1.Cu"}),
            net="GND",
            kind="pad",
            ref="U1",
        )
        result = _subtract_pad_aabb([obs], (5.0, 5.0), (2.0, 2.0))
        for r in result:
            assert r.layers == frozenset({"B.Cu", "In1.Cu"})
            assert r.net == "GND"
            assert r.kind == "pad"
            assert r.ref == "U1"
