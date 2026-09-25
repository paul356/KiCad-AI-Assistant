"""
Unit tests for W1 rendering upgrade: pad labels in render_board and
failure-evidence rendering in render_route_state.
"""

import os

from kcaa.tools.render_board_tools import render_board
from kcaa.tools.render_route_state import BlockingEvidence, render_route_attempt

FIXTURE = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "integration",
        "fixtures",
        "test_routing_board.kicad_pcb",
    )
)


def test_render_board_pad_labels_default_on():
    lines, png, report = render_board(FIXTURE, dpi=100)
    assert len(png) > 0
    assert report["pad_labels"] >= 1


def test_render_board_pad_labels_off():
    lines, png, report = render_board(FIXTURE, dpi=100, show_pad_labels=False)
    assert len(png) > 0
    assert report["pad_labels"] == 0


def test_render_route_attempt_basic():
    path = [(29.5, 30.0), (60.0, 29.5)]
    blockers = [
        BlockingEvidence(ref="R1", net="VCC", layer="F.Cu", point=(30.5, 30.0), kind="track")
    ]
    anchors = [(29.5, 30.0), (60.0, 29.5)]
    lines, png, report = render_route_attempt(
        FIXTURE,
        attempted_path=path,
        blocking_items=blockers,
        anchors=anchors,
        dpi=100,
    )
    assert len(png) > 0
    blob = "\n".join(lines)
    assert "R1" in blob and "VCC" in blob
    assert report["blocking_items"] == 1
    assert report["attempted_path_points"] == 2
    assert report["anchors"] == 2


def test_render_route_attempt_empty_fallback():
    lines, png, report = render_route_attempt(FIXTURE, dpi=100)
    assert len(png) > 0
    assert report["attempted_path_points"] == 0
    assert report["blocking_items"] == 0
    assert report["anchors"] == 0


def test_render_route_attempt_blocking_render():
    base = render_route_attempt(FIXTURE, dpi=100)[1]
    with_blocker = render_route_attempt(
        FIXTURE,
        blocking_items=[
            BlockingEvidence(ref="U1", net=None, layer="F.Cu", point=(45.0, 30.0), kind="footprint")
        ],
        dpi=100,
    )
    assert with_blocker[2]["blocking_items"] == 1
    # The red highlight must actually change pixels vs the plain render.
    assert with_blocker[1] != base
