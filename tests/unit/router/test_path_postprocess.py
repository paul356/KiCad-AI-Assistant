"""Unit tests for kcaa.router.path_postprocess."""

from __future__ import annotations

import pytest

from kcaa.router.path_postprocess import (
    OutputSegment,
    OutputVia,
    emit_segment_nodes,
    emit_via_nodes,
    postprocess,
    postprocess_path,
)
from kcaa.router.visibility_graph import RouteNode


def _path(*coords: tuple[float, float], layer: str = "F.Cu") -> list[RouteNode]:
    return [RouteNode(x, y, layer, i) for i, (x, y) in enumerate(coords)]


class TestPostprocess:
    def test_empty_path(self):
        assert postprocess([], 0.25, "F.Cu", "VCC") == []

    def test_single_node(self):
        assert postprocess(_path((0.0, 0.0)), 0.25, "F.Cu", "VCC") == []

    def test_straight_line(self):
        segs = postprocess(_path((0.0, 0.0), (10.0, 0.0)), 0.25, "F.Cu", "VCC")
        assert len(segs) == 1
        assert segs[0].x1 == 0.0
        assert segs[0].x2 == 10.0

    def test_collinear_simplified(self):
        # 3 collinear points should collapse to 1 segment
        segs = postprocess(_path((0.0, 0.0), (5.0, 0.0), (10.0, 0.0)), 0.25, "F.Cu", "VCC")
        assert len(segs) == 1

    def test_l_shape_gets_miter(self):
        # Path: (0,0) → (5,0) → (5,5)
        # Miter cuts a 45° chamfer at the corner: A shortened by m, then
        # 45° segment, then B shortened by m. We expect 3 segments.
        segs = postprocess(
            _path((0.0, 0.0), (5.0, 0.0), (5.0, 5.0)), 0.25, "F.Cu", "VCC", max_miter_mm=1.0
        )
        assert len(segs) == 3
        # First segment shortened by 1 mm → ends at (4, 0)
        assert segs[0].x2 == 4.0
        assert segs[0].y2 == 0.0
        # Second segment is the 45° miter cut from (4,0) to (5,1)
        assert segs[1].x1 == 4.0
        assert segs[1].y1 == 0.0
        assert segs[1].x2 == 5.0
        assert segs[1].y2 == 1.0
        # Third segment is the shortened vertical from (5,1) to (5,5)
        assert segs[2].x1 == 5.0
        assert segs[2].y1 == 1.0
        assert segs[2].x2 == 5.0
        assert segs[2].y2 == 5.0

    def test_miter_capped_by_max(self):
        # Long L — miter capped at max_miter_mm
        segs = postprocess(
            _path((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), 0.25, "F.Cu", "VCC", max_miter_mm=2.0
        )
        # First segment is shortened by 2 mm: (0,0)→(8,0)
        assert segs[0].x2 == 8.0

    def test_segments_carry_layer_net_width(self):
        segs = postprocess(_path((0.0, 0.0), (5.0, 0.0)), 0.5, "B.Cu", "GND")
        assert segs[0].layer == "B.Cu"
        assert segs[0].net == "GND"
        assert segs[0].width == 0.5


class TestEmission:
    def test_emit_segment_node(self):
        seg = OutputSegment(0.0, 0.0, 5.0, 0.0, 0.25, "F.Cu", "VCC")
        nodes = emit_segment_nodes([seg])
        assert len(nodes) == 1
        node = nodes[0]
        # First element is the symbol "segment"
        assert str(node[0]) == "segment"
        # Width and layer are present
        flat = str(node)
        assert "0.25" in flat
        assert "F.Cu" in flat
        assert "VCC" in flat

    def test_emit_via_node(self):
        via = OutputVia(1.0, 2.0, 0.8, 0.4, ("F.Cu", "B.Cu"), "GND")
        nodes = emit_via_nodes([via])
        assert len(nodes) == 1
        node = nodes[0]
        assert str(node[0]) == "via"
        flat = str(node)
        assert "F.Cu" in flat
        assert "B.Cu" in flat
        assert "GND" in flat


# ---------------------------------------------------------------------------
# Multi-layer: postprocess_path
# ---------------------------------------------------------------------------


def test_postprocess_path_single_layer_emits_no_vias():
    """A path with no layer transitions must produce no vias."""
    path = _path((0.0, 0.0), (5.0, 0.0), (5.0, 5.0), layer="F.Cu")
    segs, vias = postprocess_path(path, width=0.25, net="VCC")
    assert vias == []
    # Two segments (after collinear simplification only 2 points).
    assert len(segs) >= 1
    assert all(s.layer == "F.Cu" for s in segs)


def test_postprocess_path_one_via_two_layers():
    """A path that switches layer once emits exactly one via at the switch."""
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "B.Cu", 2),  # via
        RouteNode(5.0, 5.0, "B.Cu", 3),
    ]
    segs, vias = postprocess_path(path, width=0.25, net="VCC")
    assert len(vias) == 1
    via = vias[0]
    assert via.x == 5.0
    assert via.y == 0.0
    assert via.layers == ("F.Cu", "B.Cu")
    assert via.net == "VCC"
    # Two segments: one on F (0,0)->(5,0) and one on B (5,0)->(5,5).
    layer_seq = [s.layer for s in segs]
    assert "F.Cu" in layer_seq
    assert "B.Cu" in layer_seq


def test_postprocess_path_two_vias_three_layers():
    """A path crossing three layers emits two vias."""
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "In1.Cu", 2),
        RouteNode(5.0, 5.0, "In1.Cu", 3),
        RouteNode(5.0, 5.0, "B.Cu", 4),
    ]
    segs, vias = postprocess_path(path, width=0.25, net="VCC")
    assert len(vias) == 2
    assert {v.layers for v in vias} == {("F.Cu", "In1.Cu"), ("In1.Cu", "B.Cu")}


def test_postprocess_path_via_dimensions_from_call():
    """Custom via_diameter and via_drill flow through to OutputVia."""
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(1.0, 0.0, "F.Cu", 1),
        RouteNode(1.0, 0.0, "B.Cu", 2),
    ]
    _, vias = postprocess_path(path, width=0.25, net="GND", via_diameter_mm=0.8, via_drill_mm=0.4)
    assert vias[0].diameter == 0.8
    assert vias[0].drill == 0.4


def test_postprocess_path_offset_via_emits_bridge():
    """Layer transition with slightly offset (x, y) emits a bridging segment."""
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 1.0, "B.Cu", 2),  # via at non-matching (x, y)
    ]
    segs, vias = postprocess_path(path, width=0.25, net="VCC")
    # Via is emitted at the previous node's position.
    assert len(vias) == 1
    assert vias[0].x == 5.0
    assert vias[0].y == 0.0
    assert vias[0].layers == ("F.Cu", "B.Cu")
    # A bridging segment connects (5.0, 0.0)→(5.0, 1.0) on B.Cu.
    assert len(segs) >= 2  # at least F.Cu segment + B.Cu bridge


def test_postprocess_path_empty_returns_empty():
    segs, vias = postprocess_path([], width=0.25, net="VCC")
    assert segs == []
    assert vias == []


def test_postprocess_path_singleton_returns_empty():
    path = [RouteNode(0.0, 0.0, "F.Cu", 0)]
    segs, vias = postprocess_path(path, width=0.25, net="VCC")
    assert segs == []
    assert vias == []


# ---------------------------------------------------------------------------
# Via diameter / drill defaults
# ---------------------------------------------------------------------------


def test_default_via_diameter_is_sane():
    from kcaa.router.path_postprocess import DEFAULT_VIA_DIAMETER_MM

    assert 0.3 <= DEFAULT_VIA_DIAMETER_MM <= 1.0


def test_default_via_drill_is_sane():
    from kcaa.router.path_postprocess import DEFAULT_VIA_DRILL_MM

    assert 0.1 <= DEFAULT_VIA_DRILL_MM <= 0.5


def test_explicit_via_diameter_overrides_default():
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "B.Cu", 2),
    ]
    segs, vias = postprocess_path(
        path, width=0.25, net="VCC", via_diameter_mm=1.2, via_drill_mm=0.6
    )
    assert len(vias) == 1
    assert vias[0].diameter == pytest.approx(1.2)
    assert vias[0].drill == pytest.approx(0.6)


def test_via_transition_carries_layer_pair():
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "B.Cu", 2),
    ]
    _, vias = postprocess_path(path, width=0.25, net="VCC")
    assert vias[0].layers == ("F.Cu", "B.Cu")


def test_via_transition_three_layers():
    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "B.Cu", 2),
        RouteNode(5.0, 5.0, "B.Cu", 3),
        RouteNode(5.0, 5.0, "In1.Cu", 4),
    ]
    _, vias = postprocess_path(path, width=0.25, net="VCC")
    assert len(vias) == 2
    assert vias[0].layers == ("F.Cu", "B.Cu")
    assert vias[1].layers == ("B.Cu", "In1.Cu")
