"""Unit tests for kcaa.router.pns.node (STRtree nearest-obstacle queries)."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from kcaa.router.pns.node import CLEARANCE_EPS, ObstacleNode
from kcaa.router.world_model import Obstacle

EPS = 1e-6

GND_OB = Obstacle(shape=box(-2, -2, 2, 2), layers=frozenset({"F.Cu"}), net="GND", kind="track")


class TestObstacleNode:
    def test_empty(self):
        assert len(ObstacleNode([])) == 0
        assert ObstacleNode([]).nearest([(0, 0), (1, 1)]) is None
        assert not ObstacleNode([]).collides([(0, 0), (1, 1)])

    def test_nearest_blocking_path(self):
        node = ObstacleNode([GND_OB])
        hit = node.nearest([(-10, 0), (10, 0)])
        assert hit is not None
        assert hit.obstacle is GND_OB
        assert hit.distance == pytest.approx(0.0, abs=EPS)

    def test_nearest_skimming_inside_demarc(self):
        node = ObstacleNode([GND_OB])
        # 0.1 above the hull, dfence 0.2 -> collision at distance 0.1.
        hit = node.nearest([(-10, 2.1), (10, 2.1)], dfence=0.2)
        assert hit is not None
        assert hit.distance == pytest.approx(0.1, abs=EPS)

    def test_nearest_clear_returns_none(self):
        node = ObstacleNode([GND_OB])
        assert node.nearest([(-10, 5), (10, 5)]) is None
        assert node.nearest([(-10, 3), (10, 3)], dfence=0.2) is None

    def test_nearest_picks_closest_of_many(self):
        near_ob = Obstacle(
            shape=box(3, -1, 4, 1), layers=frozenset({"F.Cu"}), net="X", kind="track"
        )
        node = ObstacleNode([GND_OB, near_ob])
        hit = node.nearest([(-10, 0), (10, 0)])
        assert hit.obstacle is GND_OB  # 0 distance beats the far one
        hit = node.nearest([(-10, 0), (10, 0)], dfence=0.0)
        assert hit.distance == pytest.approx(0.0, abs=EPS)
        # Only the far obstacle in range when querying off the GND square
        # (its bbox is disjoint from the query line).
        far = node.nearest([(2.5, 0), (8, 0)])
        assert far is not None
        assert far.obstacle is near_ob
        assert far.distance == pytest.approx(0.0, abs=EPS)

    def test_collides(self):
        node = ObstacleNode([GND_OB])
        assert node.collides([(-10, 0), (10, 0)])
        assert not node.collides([(-10, 5), (10, 5)])
        assert node.collides([(-10, 2.1), (10, 2.1)], dfence=0.2)
        assert not node.collides([(-10, 2.5), (10, 2.5)], dfence=0.2)

    def test_touching_path_point_is_on_hull(self):
        node = ObstacleNode([GND_OB])
        hit = node.nearest([(-10, 2), (10, 2)], dfence=1.0)
        assert hit is not None
        # Touching point must be a hull boundary vertex (y == 2 or -2).
        assert abs(hit.point[1]) == pytest.approx(2.0, abs=EPS)

    def test_obstacles_view(self):
        node = ObstacleNode([GND_OB])
        assert node.obstacles() == [GND_OB]
        # Mutating the returned copy does not affect the node.
        node.obstacles().clear()
        assert len(node.obstacles()) == 1

    def test_point_touching_not_blocked_beyond_eps(self):
        node = ObstacleNode([GND_OB])
        # Exactly on the hull edge at distance 0 minus epsilon is blocked,
        # but a hair beyond is clear without dfence (eps guarded).
        hit = node.nearest([(-10, 2 + 2 * CLEARANCE_EPS), (10, 2 + 2 * CLEARANCE_EPS)])
        assert hit is None
