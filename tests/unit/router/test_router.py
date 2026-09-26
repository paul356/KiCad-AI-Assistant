"""
Unit tests for the high-level router orchestration.

Focus: DRC lookup (``_default_track_width``, ``_default_clearance``) and the
exception classes that propagate into :class:`RouteFailure`. The routing
algorithm itself is covered by
:mod:`tests.integration.test_pcb_routing`.

These tests enforce the *fail-loud* contract: when the project file is
missing, malformed, or lacks the netclass info we need, the router must
raise a specific subclass of :class:`RuntimeError` rather than silently
fall back to a guessed value.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kcaa.router.path_postprocess import OutputSegment, OutputVia
from kcaa.router.router import (
    DesignRulesUnavailable,
    NetClassUnresolved,
    ProFileMalformed,
    ProFileMissing,
    RouteFailure,
    RouteRequest,
    RouteResult,
    _check_segments_in_board,
    _check_vias_in_board,
    _default_clearance,
    _default_track_width,
    _find_footprint,
    _find_pad_center,
    _find_pad_size,
    _layers_used,
    _project_file_for,
    _routing_layers,
    auto_route_pair,
    connect_with_via,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

# ---------------------------------------------------------------------------
# _project_file_for
# ---------------------------------------------------------------------------


def test_project_file_for_returns_pro_sibling(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pro = tmp_path / "board.kicad_pro"
    pcb.write_text("(kicad_pcb)\n")
    pro.write_text("{}\n")
    assert _project_file_for(str(pcb)) == str(pro)


def test_project_file_for_missing_returns_none(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    assert _project_file_for(str(pcb)) is None


# ---------------------------------------------------------------------------
# Local fixture for tests that need a real PCB
# ---------------------------------------------------------------------------


import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "integration", "fixtures")
_BOARD_FIXTURE = os.path.normpath(os.path.join(_FIXTURE_DIR, "test_routing_board.kicad_pcb"))
_PRO_FIXTURE = os.path.normpath(os.path.join(_FIXTURE_DIR, "test_routing_board.kicad_pro"))


@pytest.fixture
def pcb_copy(tmp_path):
    """Copy the routing fixture to a writable temp dir and bring its .pro."""
    dst = tmp_path / "test_routing_board.kicad_pcb"
    shutil.copy(_BOARD_FIXTURE, dst)
    shutil.copy(_PRO_FIXTURE, tmp_path / "test_routing_board.kicad_pro")
    return str(dst)


# ---------------------------------------------------------------------------
# _default_track_width
# ---------------------------------------------------------------------------


def _write_pro(tmp_path: Path, payload: dict) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pro = tmp_path / "board.kicad_pro"
    pcb.write_text("(kicad_pcb)\n")
    pro.write_text(json.dumps(payload))
    return pcb


def _base_pro() -> dict:
    return {
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "track_width": 0.25,
                    "clearance": 0.2,
                }
            ],
            "netclass_patterns": [],
        }
    }


def test_default_track_width_uses_netclass_pattern(tmp_path: Path) -> None:
    payload = _base_pro()
    payload["net_settings"]["classes"].append(
        {"name": "Power", "track_width": 0.5, "clearance": 0.3}
    )
    payload["net_settings"]["netclass_patterns"].append({"netclass": "Power", "pattern": "VCC"})
    pcb = _write_pro(tmp_path, payload)
    assert _default_track_width(str(pcb), "VCC") == 0.5


def test_default_track_width_falls_back_to_default_class(tmp_path: Path) -> None:
    payload = _base_pro()
    pcb = _write_pro(tmp_path, payload)
    assert _default_track_width(str(pcb), "MysteryNet") == 0.25


def test_default_track_width_raises_when_pro_missing(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    with pytest.raises(ProFileMissing):
        _default_track_width(str(pcb), "VCC")


def test_default_track_width_raises_on_invalid_json(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pro = tmp_path / "board.kicad_pro"
    pcb.write_text("(kicad_pcb)\n")
    pro.write_text("{not valid json")
    with pytest.raises(ProFileMalformed):
        _default_track_width(str(pcb), "VCC")


def test_default_track_width_raises_on_non_object_root(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pro = tmp_path / "board.kicad_pro"
    pcb.write_text("(kicad_pcb)\n")
    pro.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ProFileMalformed):
        _default_track_width(str(pcb), "VCC")


def test_default_track_width_raises_when_no_default_and_no_match(tmp_path: Path) -> None:
    payload = {
        "net_settings": {
            "classes": [{"name": "Power", "track_width": 0.5, "clearance": 0.3}],
            "netclass_patterns": [{"netclass": "Power", "pattern": "VCC"}],
        }
    }
    pcb = _write_pro(tmp_path, payload)
    with pytest.raises(NetClassUnresolved):
        _default_track_width(str(pcb), "MysteryNet")


# ---------------------------------------------------------------------------
# _default_clearance
# ---------------------------------------------------------------------------


def test_default_clearance_reads_from_design_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``get_effective_design_rules_from_file`` returns a sane dict,
    the clearance flows through."""
    fake_rules = {"design_rules": {"min_clearance": 0.3}}
    monkeypatch.setattr(
        "kcaa.utils.pcb_design_rules.get_effective_design_rules_from_file",
        lambda _path: fake_rules,
    )
    assert _default_clearance("/some/path.kicad_pcb") == 0.3


def test_default_clearance_raises_when_section_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rules = {"net_classes": []}
    monkeypatch.setattr(
        "kcaa.utils.pcb_design_rules.get_effective_design_rules_from_file",
        lambda _path: fake_rules,
    )
    with pytest.raises(DesignRulesUnavailable):
        _default_clearance("/some/path.kicad_pcb")


def test_default_clearance_raises_when_min_clearance_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rules = {"design_rules": {"min_track_width": 0.2}}
    monkeypatch.setattr(
        "kcaa.utils.pcb_design_rules.get_effective_design_rules_from_file",
        lambda _path: fake_rules,
    )
    with pytest.raises(DesignRulesUnavailable):
        _default_clearance("/some/path.kicad_pcb")


def test_default_clearance_raises_when_reader_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_path: str) -> dict:
        raise OSError("kaboom")

    monkeypatch.setattr(
        "kcaa.utils.pcb_design_rules.get_effective_design_rules_from_file",
        boom,
    )
    with pytest.raises(DesignRulesUnavailable):
        _default_clearance("/some/path.kicad_pcb")


# ---------------------------------------------------------------------------
# auto_route_pair — DRC exception → RouteFailure translation
# ---------------------------------------------------------------------------


def _fixture_pcb() -> str:
    return str(
        Path(__file__).resolve().parents[2]
        / "integration"
        / "fixtures"
        / "test_routing_board.kicad_pcb"
    )


def test_auto_route_pair_translates_pro_missing_into_route_failure(tmp_path: Path) -> None:
    """If the .kicad_pro is missing and no width/clearance is given,
    auto_route_pair should raise RouteFailure, not silently pick a number."""
    # Make a temporary copy of the fixture so we don't depend on a sibling
    # .kicad_pro being present (it will be — but we delete it).
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(Path(src).read_text())
    # No .kicad_pro next to dst.

    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
    )
    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(req)
    msg = str(excinfo.value)
    assert "track width" in msg.lower()
    assert "width=" in msg  # the hint to pass width= explicitly


def test_auto_route_pair_explicit_width_skips_drc(tmp_path: Path) -> None:
    """When the user passes width= explicitly, missing .kicad_pro must not
    block routing — that's the whole point of the explicit override."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(Path(src).read_text())
    # No .kicad_pro next to dst.

    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.3,
        clearance=0.2,
    )
    # Should NOT raise — both DRC lookups are skipped.
    result = auto_route_pair(req)
    assert len(result.segments) > 0


# ---------------------------------------------------------------------------
# _check_segments_in_board
# ---------------------------------------------------------------------------


def _seg(x1: float, y1: float, x2: float, y2: float, width: float = 0.25) -> OutputSegment:
    return OutputSegment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer="F.Cu", net="VCC")


def test_check_segments_in_board_accepts_segment_inside() -> None:
    segs = [_seg(2.0, 2.0, 4.0, 2.0)]
    # No exception expected.
    _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0))


def test_check_segments_in_board_accepts_segment_on_boundary() -> None:
    """A segment whose centerline sits exactly on the Edge.Cuts boundary
    is fine — its copper would just touch the edge, not cross it."""
    segs = [_seg(0.5, 5.0, 5.0, 5.0)]
    _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0))


def test_check_segments_in_board_rejects_segment_outside() -> None:
    segs = [_seg(2.0, 2.0, 12.0, 5.0)]  # x2 is past maxx=10
    with pytest.raises(RouteFailure) as excinfo:
        _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0))
    assert "outside the Edge.Cuts boundary" in str(excinfo.value)


def test_check_segments_in_board_rejects_segment_through_wall() -> None:
    """A segment whose endpoints are inside but whose centerline would
    not cross a wall — this case should still pass. To trigger failure we
    need an endpoint past the boundary."""
    segs = [_seg(-1.0, 5.0, 5.0, 5.0)]
    with pytest.raises(RouteFailure):
        _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0))


def test_check_segments_in_board_rejects_when_width_wider_than_board() -> None:
    """A 20 mm track cannot possibly fit inside a 5 mm board."""
    segs = [_seg(2.0, 2.0, 3.0, 2.0, width=20.0)]
    with pytest.raises(RouteFailure) as excinfo:
        _check_segments_in_board(segs, (0.0, 0.0, 5.0, 5.0))
    msg = str(excinfo.value)
    assert "wider than the board" in msg or "outside" in msg


def test_check_segments_in_board_rejects_degenerate_bbox() -> None:
    segs = [_seg(0.0, 0.0, 1.0, 1.0)]
    with pytest.raises(RouteFailure) as excinfo:
        _check_segments_in_board(segs, (5.0, 5.0, 5.0, 10.0))
    assert "degenerate" in str(excinfo.value)


def test_check_segments_in_board_accepts_segment_onto_edge_pad_zone() -> None:
    """A segment ending past the shrunk boundary is legal when it lands on
    an endpoint pad that straddles the edge (edge connector)."""
    # Board 0..10; width 0.25 shrinks the fence to 0.125..9.875, so
    # x=9.9 alone would be out of bounds — the pad zone makes it legal.
    segs = [_seg(5.0, 5.0, 9.9, 5.0)]
    _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0), pad_zones=[(9.9, 5.0, 1.0, 1.0)])


def test_check_segments_in_board_rejects_past_edge_without_pad_zone() -> None:
    """The pad-zone exemption is scoped: same segment, different pad —
    still rejected."""
    segs = [_seg(5.0, 5.0, 4.0, 9.9)]
    with pytest.raises(RouteFailure) as excinfo:
        _check_segments_in_board(segs, (0.0, 0.0, 10.0, 10.0), pad_zones=[(9.9, 5.0, 1.0, 1.0)])
    assert "outside the Edge.Cuts boundary" in str(excinfo.value)


# ---------------------------------------------------------------------------
# auto_route_pair — board-bounds check integration
# ---------------------------------------------------------------------------


def test_auto_route_pair_warns_when_no_edge_cuts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PCB with no Edge.Cuts items is a workflow state, not an error.

    The user may be routing before drawing the board outline. We log a
    warning and proceed; final validation is the responsibility of KiCad
    DRC.
    """
    # Build a minimal PCB with the fixture's footprints but strip the
    # Edge.Cuts gr_rect we just added.
    pcb_text = Path(_fixture_pcb()).read_text()
    no_edge_cuts = "\n".join(
        line
        for line in pcb_text.splitlines()
        if "Edge.Cuts" not in line and "edge-cuts-rect" not in line
    )
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(no_edge_cuts)

    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.3,
        clearance=0.2,
    )
    with caplog.at_level("WARNING", logger="kcaa.router.router"):
        result = auto_route_pair(req)
    assert len(result.segments) > 0
    assert any("Edge.Cuts" in record.message for record in caplog.records), (
        f"Expected a warning mentioning Edge.Cuts; got {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Multi-layer request: layer / pad-layer validation
# ---------------------------------------------------------------------------


def test_unknown_layer_in_pcb_raises_route_failure(tmp_path: Path) -> None:
    """A via_pair layer that the PCB doesn't declare must fail loudly with
    a RouteFailure that names the offending layer."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(Path(src).read_text())

    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.3,
        clearance=0.2,
        via_pairs=(("F.Cu", "In2.Cu"),),  # 4-layer fixture has no In2.Cu
    )
    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(req)
    assert "In1.Cu" in str(excinfo.value)


def test_smd_pad_layer_hint_ignored_route_on_pad_layer(tmp_path: Path) -> None:
    """SMD pads fix their layer: layer_hint="B.Cu" is ignored for R1.1/C1.1
    (SMD on F.Cu), so the route proceeds on F.Cu and succeeds."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(Path(src).read_text())

    # R1.1 / C1.1 are SMD on F.Cu in the fixture; layer_hint="B.Cu" is
    # ignored for SMD pads (layer is fixed), so the route stays on F.Cu
    # and succeeds.
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.3,
        clearance=0.2,
        layer_hint="B.Cu",  # ignored for SMD pads; route stays F.Cu
    )
    # R1.1 / C1.1 are SMD on F.Cu — route should succeed on F.Cu.
    result = auto_route_pair(req)
    assert len(result.segments) > 0


# ---------------------------------------------------------------------------
# Multi-layer helper functions
# ---------------------------------------------------------------------------


def test_routing_layers_includes_start_end_and_via_pairs():
    req = RouteRequest(
        pcb_path="dummy",
        ref_a="A",
        pad_a="1",
        ref_b="B",
        pad_b="1",
        net="N",
        layer_hint="F.Cu",
        via_pairs=(("F.Cu", "B.Cu"),),
    )
    layers = _routing_layers(req, "F.Cu", "B.Cu")
    assert layers == ["F.Cu", "B.Cu"]


def test_routing_layers_dedupes():
    req = RouteRequest(
        pcb_path="dummy",
        ref_a="A",
        pad_a="1",
        ref_b="B",
        pad_b="1",
        net="N",
        layer_hint="F.Cu",
        via_pairs=(("F.Cu", "F.Cu"),),
    )
    layers = _routing_layers(req, "F.Cu", "F.Cu")
    assert layers == ["F.Cu"]


def test_layers_used_preserves_order_dedupes():
    from kcaa.router.visibility_graph import RouteNode

    path = [
        RouteNode(0.0, 0.0, "F.Cu", 0),
        RouteNode(5.0, 0.0, "F.Cu", 1),
        RouteNode(5.0, 0.0, "B.Cu", 2),
        RouteNode(5.0, 5.0, "B.Cu", 3),
        RouteNode(5.0, 5.0, "F.Cu", 4),
    ]
    assert _layers_used(path) == ["F.Cu", "B.Cu"]


def test_layers_used_empty():
    assert _layers_used([]) == []


# ---------------------------------------------------------------------------
# _check_vias_in_board
# ---------------------------------------------------------------------------


def test_check_vias_in_board_pass_when_inside():
    via = OutputVia(x=25.0, y=20.0, diameter=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="VCC")
    # Board 50x40; via at (25, 20) is well inside, even with 0.3 mm radius.
    _check_vias_in_board([via], (0.0, 0.0, 50.0, 40.0))


def test_check_vias_in_board_fail_when_outside():
    via = OutputVia(x=-1.0, y=20.0, diameter=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="VCC")
    with pytest.raises(RouteFailure, match="extend outside"):
        _check_vias_in_board([via], (0.0, 0.0, 50.0, 40.0))


def test_check_vias_in_board_fail_when_too_close_to_edge():
    # 0.6 mm diameter → 0.3 mm radius. Board ends at x=50, via at x=49.8.
    via = OutputVia(x=49.8, y=20.0, diameter=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="VCC")
    with pytest.raises(RouteFailure, match="extend outside"):
        _check_vias_in_board([via], (0.0, 0.0, 50.0, 40.0))


def test_check_vias_in_board_empty_list():
    # No vias to check — should be a no-op.
    _check_vias_in_board([], (0.0, 0.0, 50.0, 40.0))


def test_check_vias_in_board_degenerate_bbox():
    via = OutputVia(x=10.0, y=10.0, diameter=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="VCC")
    with pytest.raises(RouteFailure, match="degenerate"):
        _check_vias_in_board([via], (10.0, 10.0, 10.0, 10.0))


# ---------------------------------------------------------------------------
# connect_with_via helper
# ---------------------------------------------------------------------------


def test_connect_with_via_uses_seg_a_endpoint():
    seg_a = OutputSegment(x1=10.0, y1=5.0, x2=20.0, y2=5.0, width=0.25, layer="F.Cu", net="VCC")
    seg_b = OutputSegment(x1=20.0, y1=5.0, x2=30.0, y2=5.0, width=0.25, layer="B.Cu", net="VCC")
    via = connect_with_via(
        seg_a, seg_b, net="VCC", diameter=0.8, drill=0.4, layer_a="F.Cu", layer_b="B.Cu"
    )
    assert via.x == 20.0
    assert via.y == 5.0
    assert via.diameter == 0.8
    assert via.drill == 0.4
    assert via.layers == ("F.Cu", "B.Cu")
    assert via.net == "VCC"


# ---------------------------------------------------------------------------
# _find_footprint lookup
# ---------------------------------------------------------------------------


def test_find_footprint_returns_none_for_missing_ref():
    # Empty PCB tree.
    assert _find_footprint([], "R1") is None
    # PCB tree with a different footprint.
    fake_pcb = [
        [
            "footprint",
            ["property", "Reference", "OTHER"],
            ["pad", "1", "smd", "rect", ["at", 0.0, 0.0], ["size", 0.5, 0.5]],
        ]
    ]
    assert _find_footprint(fake_pcb, "R1") is None


def test_find_footprint_returns_footprint_for_existing_ref():
    target = [
        "footprint",
        ["property", "Reference", "R1"],
        ["pad", "1", "smd", "rect", ["at", 0.0, 0.0], ["size", 0.5, 0.5]],
    ]
    fake_pcb = [target, ["footprint", ["property", "Reference", "OTHER"]]]
    assert _find_footprint(fake_pcb, "R1") is target


# ---------------------------------------------------------------------------
# ref_b pad coord branch
# ---------------------------------------------------------------------------


def test_invalid_ref_b_pad_raises_failure(pcb_copy):
    # R1.2 exists; C1.999 does not.  The router must raise RouteFailure
    # for the ref_b side (symmetric to the ref_a side covered by
    # test_invalid_pad_raises_failure).
    req = RouteRequest(
        pcb_path=pcb_copy,
        ref_a="R1",
        pad_a="2",
        ref_b="C1",
        pad_b="999",
        net="VCC",
    )
    with pytest.raises(RouteFailure, match="C1/999 not found"):
        auto_route_pair(req)


# ---------------------------------------------------------------------------
# thru-hole pad (no SMD size on layer)
# ---------------------------------------------------------------------------


def test_smd_pad_layer_is_fixed(pcb_copy):
    # SMD pads are on exactly one copper layer.  layer_hint is ignored
    # for SMD pads — the layer is fixed by the pad itself.  R1.1 and
    # C1.2 are both SMD on F.Cu, so the route stays on F.Cu even when
    # layer_hint suggests B.Cu.
    req = RouteRequest(
        pcb_path=pcb_copy,
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.25,
        clearance=0.2,
        layer_hint="B.Cu",  # ignored — SMD pads are on F.Cu
    )
    result = auto_route_pair(req)
    assert result.layers_used == ["F.Cu"]


# ---------------------------------------------------------------------------
# duplicate pad names in one footprint (edge-connector style)
# ---------------------------------------------------------------------------


def test_duplicate_pad_name_resolves_to_layer_matching_pad(tmp_path: Path) -> None:
    """A footprint may declare several pads with the same name (e.g. edge-
    connector fingers sharing a net). Looking up that name on a given
    copper layer must resolve to the pad whose copper is actually there —
    the first same-named pad must not shadow the rest, and a pad with a
    drill hole (thru-hole) is a valid target."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    x1 = (
        "\n\t# ---- X1: duplicate 'T' pads; F.Cu finger first, thru-hole *.Cu second ----\n"
        '\t(footprint "user_add:edge-connector"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "66666666-0000-0000-0000-000000000006")\n'
        "\t\t(at 62.0 45.0 0.0)\n"
        '\t\t(property "Reference" "X1")\n'
        '\t\t(property "Value" "X1")\n'
        '\t\t(pad "T" smd rect\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 1.0 1.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        '\t\t(pad "T" thru_hole custom\n'
        "\t\t\t(at 2.0 0.0)\n"
        "\t\t\t(size 1.6 1.6)\n"
        "\t\t\t(drill 1.0)\n"
        '\t\t\t(layers "*.Cu" "*.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    dst.write_text(board[:-1] + x1 + ")\n")

    data = load_pcb(str(dst))
    # F.Cu resolves to the first pad; B.Cu skips it and finds the thru-hole
    # pad.  Center and size must describe the SAME pad.
    assert _find_pad_size(data, "X1", "T", "F.Cu") == (1.0, 1.0)
    assert _find_pad_size(data, "X1", "T", "B.Cu") == (1.6, 1.6)
    assert _find_pad_center(data, "X1", "T", "F.Cu") == pytest.approx((62.0, 45.0))
    assert _find_pad_center(data, "X1", "T", "B.Cu") == pytest.approx((64.0, 45.0))
    # Unfiltered lookup keeps the legacy first-match behaviour.
    assert _find_pad_center(data, "X1", "T") == pytest.approx((62.0, 45.0))

    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="X1",
        pad_b="T",
        net="VCC",
        layer_hint="B.Cu",
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    # The route must end on the thru-hole pad at world (62+2, 45), not on
    # the F.Cu-only finger at (62, 45).
    assert result.end == pytest.approx((64.0, 45.0), abs=0.1)
    assert result.layers_used == ["F.Cu", "B.Cu"]
    assert len(result.segments) > 0


def test_multi_layer_via_not_on_same_net_pad(tmp_path: Path) -> None:
    """A via must never land on any same-net pad face (DFM defect:
    solder wicking / annular-ring breakout).  Via-forbidden zones
    cover ALL same-net pads, not just the two endpoint pads."""
    from shapely.geometry import Point, box

    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    # X3: same-net SMD pads on F.Cu near the route corridor between
    # R1/1 (29.5, 30) and X1/T (64, 45).  Neither pad is an endpoint;
    # a multi-layer route's via must avoid both.
    x3 = (
        "\n\t# ---- X3: same-net SMD pads, via must avoid ----\n"
        '\t(footprint "user_add:via-dodge"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "99999999-0000-0000-0000-000000000009")\n'
        "\t\t(at 45.0 40.0 0.0)\n"
        '\t\t(property "Reference" "X3")\n'
        '\t\t(property "Value" "X3")\n'
        '\t\t(pad "1" smd rect\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 2.0 2.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        '\t\t(pad "2" smd rect\n'
        "\t\t\t(at 5.0 0.0)\n"
        "\t\t\t(size 2.0 2.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
        # X1: duplicate T pads — THT variant on B.Cu (endpoint).
        "\n\t# ---- X1: THT endpoint pad on B.Cu ----\n"
        '\t(footprint "user_add:edge-connector"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "66666666-0000-0000-0000-000000000006")\n'
        "\t\t(at 62.0 45.0 0.0)\n"
        '\t\t(property "Reference" "X1")\n'
        '\t\t(property "Value" "X1")\n'
        '\t\t(pad "T" thru_hole circle\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 1.6 1.6)\n"
        "\t\t\t(drill 1.0)\n"
        '\t\t\t(layers "*.Cu" "*.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    dst.write_text(board[:-1] + x3 + ")\n")

    # R1/1 F.Cu -> X1/T B.Cu (both VCC).  A via is unavoidable (layer
    # change).  The via must not land on X3/1 (45,40) or X3/2 (50,40).
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="X1",
        pad_b="T",
        net="VCC",
        layer_hint="B.Cu",
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    assert len(result.vias) > 0
    # Pad copper rectangles (unbuffered, world coords): 2×2 at (45,40)
    # and (50,40).
    pad_rects = [box(44, 39, 46, 41), box(49, 39, 51, 41)]
    for via in result.vias:
        pt = Point(via.x, via.y)
        for i, rect in enumerate(pad_rects):
            assert not rect.contains(pt), (
                f"Via at ({via.x:.3f},{via.y:.3f}) lands on X3/{i + 1} pad"
            )


def test_multi_layer_route_avoids_same_net_tht_hole(tmp_path: Path) -> None:
    """A same-net thru-hole pad's drill hole physically severs any track
    crossing it.  The router must re-add the hole as an obstacle (the
    world model drops same-net pad copper entirely) so A* routes around
    the hole instead of running a track straight through it."""
    from shapely.geometry import LineString, Point

    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    # X2: same-net THT pad sitting between R1/1 and C1/1.  Drill 3.0 mm
    # is large enough that a straight track at x≈45 would cross the hole.
    x2 = (
        "\n\t# ---- X2: same-net THT transit pad between R1 and C1 ----\n"
        '\t(footprint "user_add:tht-blocker"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "88888888-0000-0000-0000-000000000008")\n'
        "\t\t(at 45.0 30.0 0.0)\n"
        '\t\t(property "Reference" "X2")\n'
        '\t\t(property "Value" "X2")\n'
        '\t\t(pad "1" thru_hole circle\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 4.0 4.0)\n"
        "\t\t\t(drill 3.0)\n"
        '\t\t\t(layers "*.Cu" "*.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    dst.write_text(board[:-1] + x2 + ")\n")

    # R1/1 (29.5, 30) F.Cu -> C1/1 (60, 29.5) F.Cu, both VCC.
    # Without the hole obstacle, A* would run a track at y≈30 through
    # X2's drill (center 45,30, r=1.5).  The hole forces a detour.
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        # Both R1/1 and C1/1 are SMD on F.Cu → single-layer route.
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    assert len(result.segments) > 0
    # No segment may cross the actual drill hole (r=1.5 at (45, 30)).
    hole = Point(45.0, 30.0).buffer(1.5)
    for seg in result.segments:
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        assert not line.intersects(hole), (
            f"Segment {seg.x1:.3f},{seg.y1:.3f}->{seg.x2:.3f},{seg.y2:.3f} crosses X2 drill hole"
        )


def _board_with_bay_connector(tmp_path: Path) -> Path:
    """Fixture board + X1 edge connector at the top edge (baseline outline
    top y=60): X1 draws its bay with fp Edge.Cuts items (top at local y=4,
    world y=64) and carries pad T at local (2, 2) — world (64, 62) — which
    sits in the bay, above the gr-only outline."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    x1 = (
        "\n\t# ---- X1: edge connector bay drawn with fp Edge.Cuts ----\n"
        '\t(footprint "user_add:edge-connector"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "77777777-0000-0000-0000-000000000007")\n'
        "\t\t(at 62.0 60.0 0.0)\n"
        '\t\t(property "Reference" "X1")\n'
        '\t\t(property "Value" "X1")\n'
        "\t\t(fp_line\n"
        "\t\t\t(start -3.0 0.0)\n"
        "\t\t\t(end -3.0 4.0)\n"
        "\t\t\t(stroke (width 0.1) (type solid))\n"
        '\t\t\t(layer "Edge.Cuts")\n'
        "\t\t)\n"
        "\t\t(fp_line\n"
        "\t\t\t(start -3.0 4.0)\n"
        "\t\t\t(end 7.0 4.0)\n"
        "\t\t\t(stroke (width 0.1) (type solid))\n"
        '\t\t\t(layer "Edge.Cuts")\n'
        "\t\t)\n"
        "\t\t(fp_line\n"
        "\t\t\t(start 7.0 4.0)\n"
        "\t\t\t(end 7.0 0.0)\n"
        "\t\t\t(stroke (width 0.1) (type solid))\n"
        '\t\t\t(layer "Edge.Cuts")\n'
        "\t\t)\n"
        '\t\t(pad "T" smd rect\n'
        "\t\t\t(at 2.0 2.0)\n"
        "\t\t\t(size 1.0 1.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    dst.write_text(board[:-1] + x1 + ")\n")
    return dst


def test_board_bbox_includes_footprint_edge_cuts_profile(tmp_path: Path) -> None:
    """Edge-mounted connectors describe the notch they sit in with
    footprint-level Edge.Cuts items; those are part of the board outline
    and must widen the AABB (baseline outline (0,0)-(70,60) → top 64)."""
    from kcaa.router.world_model import _board_bbox

    dst = _board_with_bay_connector(tmp_path)
    data = load_pcb(str(dst))
    bbox = _board_bbox(data)
    assert bbox == (20.0, 20.0, 70.0, 64.0)


def test_auto_route_pair_reaches_pad_in_footprint_drawn_bay(tmp_path: Path) -> None:
    """A route into a pad sitting in a footprint-drawn bay (above the gr-only
    outline) must pass the board-bounds check once the outline includes the
    footprint Edge.Cuts profile."""
    dst = _board_with_bay_connector(tmp_path)
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="X1",
        pad_b="T",
        net="VCC",
        # R1/1 SMD F.Cu → X1/T THT; both share F.Cu → single-layer.
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    assert result.end == pytest.approx((64.0, 62.0), abs=0.02)
    assert len(result.segments) > 0


# ---------------------------------------------------------------------------
# Edge.Cuts internal openings
# ---------------------------------------------------------------------------


def _board_with_opening(tmp_path: Path, rect: tuple[float, float, float, float]) -> Path:
    """Fixture board + one internal gr_rect opening on Edge.Cuts.

    Baseline outline is (20,20)-(70,60); the opening must sit inside it.
    """
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    x1, y1, x2, y2 = rect
    opening = (
        "\n\t# ---- internal Edge.Cuts opening ----\n"
        "\t(gr_rect\n"
        f"\t\t(start {x1} {y1})\n"
        f"\t\t(end {x2} {y2})\n"
        "\t\t(stroke (width 0.1) (type solid))\n"
        "\t\t(fill none)\n"
        '\t\t(layer "Edge.Cuts")\n'
        "\t)\n"
    )
    dst.write_text(board[:-1] + opening + ")\n")
    return dst


def test_edge_cuts_internal_rect_becomes_opening_obstacle(tmp_path: Path) -> None:
    """A closed gr_rect fully inside the outline is a routing-slot opening:
    it must appear in the world model as an all-layer obstacle of kind
    "opening", not just widen the board AABB."""
    from kcaa.router.world_model import build_world_model

    dst = _board_with_opening(tmp_path, (40.0, 25.0, 50.0, 35.0))
    world = build_world_model(str(dst), net_filter=None)
    openings = [o for o in world.obstacles if o.kind == "opening"]
    assert len(openings) == 1
    o = openings[0]
    # Slot polygon matches the requested rect (world coords).
    assert o.shape.bounds == pytest.approx((40.0, 25.0, 50.0, 35.0))
    assert o.layers == frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"})
    assert o.net is None


def test_auto_route_pair_detours_around_internal_opening(tmp_path: Path) -> None:
    """R1 (30,30) -> C1 (60,30) crosses a 40..50 x 25..35 opening on the
    straight line; the router must route around it (no segment may cross
    the slot)."""
    from shapely.geometry import LineString, Polygon

    dst = _board_with_opening(tmp_path, (40.0, 25.0, 50.0, 35.0))
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    assert len(result.segments) > 0
    slot = Polygon([(40.0, 25.0), (50.0, 25.0), (50.0, 35.0), (40.0, 35.0)])
    for seg in result.segments:
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        assert not line.intersects(slot), (
            f"Segment {seg.x1:.3f},{seg.y1:.3f}->{seg.x2:.3f},{seg.y2:.3f} "
            f"crosses the Edge.Cuts opening"
        )


def test_auto_route_pair_detours_around_tall_opening(tmp_path: Path) -> None:
    """An opening TALLER than the pad +/-5 mm search box (it extends beyond
    the bbox on both sides, e.g. a slot between two connectors) previously
    looked like an impassable wall: the A* grid is clipped to route_bbox, so
    the detour around the slot was outside the search area and the route
    failed even though one exists.  The bbox must grow to cover the opening
    so the router can find the detour."""
    from shapely.geometry import LineString, Polygon

    # Baseline outline (20,20)-(70,60); opening is taller than the
    # R1(30,30) -> C1(60,30) search box (bbox y = 25..35, even with the
    # 2 mm grid margin: 23..37) on both sides, yet fully inside the
    # outline (y 20..60).
    dst = _board_with_opening(tmp_path, (40.0, 21.0, 50.0, 45.0))
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.25,
        clearance=0.2,
    )
    result = auto_route_pair(req)
    assert len(result.segments) > 0
    slot = Polygon([(40.0, 21.0), (50.0, 21.0), (50.0, 45.0), (40.0, 45.0)])
    for seg in result.segments:
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        assert not line.intersects(slot), (
            f"Segment {seg.x1:.3f},{seg.y1:.3f}->{seg.x2:.3f},{seg.y2:.3f} "
            f"crosses the tall Edge.Cuts opening"
        )


def test_auto_route_pair_rejects_pads_on_different_nets(tmp_path: Path) -> None:
    """Routing two pads that are on DIFFERENT nets must fail fast with a
    clear message instead of treating the target pad as a foreign-net
    obstacle and failing deep inside A*."""
    dst = _board_with_opening(tmp_path, (40.0, 25.0, 50.0, 35.0))
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",  # R1/1 is on VCC
        ref_b="C1",
        pad_b="1",  # C1/1 is on VCC
        net="NET_A",  # matches neither pad -> early rejection
        width=0.25,
        clearance=0.2,
    )
    with pytest.raises(RouteFailure, match="cannot route between different nets"):
        auto_route_pair(req)


def test_edge_cuts_open_notch_is_not_opening(tmp_path: Path) -> None:
    """The X1 bay notch touches the board outline (it is carved out of the
    edge), so it is part of the outline -- NOT an internal opening.  The
    world model must not produce an "opening" obstacle for it."""
    from kcaa.router.world_model import build_world_model

    dst = _board_with_bay_connector(tmp_path)
    world = build_world_model(str(dst), net_filter=None)
    openings = [o for o in world.obstacles if o.kind == "opening"]
    assert openings == []


# ---------------------------------------------------------------------------
# NPTH pad type index (type lives at [2], not [1])
# ---------------------------------------------------------------------------


def _fp_with_pad(pad_body: str, ref: str = "X1") -> str:
    """One footprint with a single pad; returned as raw PCB text snippet."""
    return (
        '(footprint "test:npth"\n'
        '\t(layer "F.Cu")\n'
        "\t(at 100 100)\n"
        f'\t(property "Reference" "{ref}")\n'
        f'\t(property "Value" "{ref}")\n'
        f"{pad_body}\n"
        ")\n"
    )


def _load_pcb_text(pcb_text: str, tmp_path: Path) -> Path:
    from kcaa.utils.pcb_sexp_utils import load_pcb as _lp

    dst = tmp_path / "npth.kicad_pcb"
    dst.write_text(f"(kicad_pcb (version 20240108) (generator pcbnew)\n{pcb_text}\n)\n")
    _lp(str(dst))  # parse-validity smoke
    return dst


def test_npth_pad_becomes_drill_obstacle_not_pad_obstacle(tmp_path: Path) -> None:
    """A KiCad NPTH pad (type at index [2], name '' at [1]) must produce a
    drill-kind circular obstacle, not a pad-kind rectangle."""
    from kcaa.router.world_model import build_world_model

    pcb = _load_pcb_text(
        _fp_with_pad(
            '\t(pad "" np_thru_hole circle\n'
            "\t\t(at 0 0)\n"
            "\t\t(size 3.2 3.2)\n"
            "\t\t(drill 1.6)\n"
            '\t\t(layers "*.Cu" "*.Mask")\n'
            "\t)\n"
        ),
        tmp_path,
    )
    world = build_world_model(str(pcb), net_filter=None)
    drills = [o for o in world.obstacles if o.kind == "drill"]
    pads = [o for o in world.obstacles if o.kind == "pad"]
    assert len(drills) == 1, f"expected one drill obstacle, got {len(drills)}"
    assert pads == [], "NPTH must not produce a pad-kind obstacle"
    d = drills[0]
    # Drill diameter 1.6 -> obstacle circle r=0.8 at footprint origin (100,100).
    assert d.shape.area == pytest.approx(3.14159 * 0.8**2, rel=0.05)
    assert (d.shape.centroid.x, d.shape.centroid.y) == pytest.approx((100.0, 100.0))
    assert d.layers == frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"})
    assert d.net is None


def test_npth_pad_rotated_with_footprint(tmp_path: Path) -> None:
    """The drill center follows the footprint placement/rotation."""
    from kcaa.router.world_model import build_world_model

    pcb = _load_pcb_text(
        _fp_with_pad(
            '\t(pad "" np_thru_hole circle\n'
            "\t\t(at 5 0)\n"
            "\t\t(size 3.2 3.2)\n"
            "\t\t(drill 1.6)\n"
            '\t\t(layers "*.Cu" "*.Mask")\n'
            "\t)\n",
            ref="X1",
        ).replace("(at 100 100)", "(at 100 100 90)"),
        tmp_path,
    )
    world = build_world_model(str(pcb), net_filter=None)
    drills = [o for o in world.obstacles if o.kind == "drill"]
    assert len(drills) == 1
    # Local (5,0) rotated 90 CW on screen -> world (100, 95).
    assert (drills[0].shape.centroid.x, drills[0].shape.centroid.y) == pytest.approx((100.0, 95.0))


def test_plated_thru_hole_pad_still_pad_obstacle(tmp_path: Path) -> None:
    """A regular plated THT pad must keep producing a pad-kind obstacle --
    the index fix must not misclassify non-NPTH pads."""
    from kcaa.router.world_model import build_world_model

    pcb = _load_pcb_text(
        _fp_with_pad(
            '\t(pad "1" thru_hole circle\n'
            "\t\t(at 0 0)\n"
            "\t\t(size 2.0 2.0)\n"
            "\t\t(drill 1.0)\n"
            '\t\t(layers "*.Cu" "*.Mask")\n'
            '\t\t(net 1 "VCC")\n'
            "\t)\n"
        ),
        tmp_path,
    )
    world = build_world_model(str(pcb), net_filter="OtherNet")
    pads = [o for o in world.obstacles if o.kind == "pad"]
    drills = [o for o in world.obstacles if o.kind == "drill"]
    assert len(pads) == 1, "plated THT pad must remain a pad-kind obstacle"
    assert drills == [], "plated THT pad must not become a drill obstacle"
    assert pads[0].net == "VCC"


# ---------------------------------------------------------------------------
# PNS engine adapter (corner_mode / arcs / shoved tracks)
# ---------------------------------------------------------------------------


def _make_clear_board(tmp_path: Path) -> str:
    """Minimal 2-pad single-layer board with no obstacles between them."""
    pcb = """(kicad_pcb
	(version 20260206)
	(generator "test")
	(layers
		(0 "F.Cu" signal)
		(44 "Edge.Cuts" user)
	)
	(net 0 "")
	(net 1 "VCC")
	(footprint "R"
		(layer "F.Cu")
		(at 30.0 30.0 0.0)
		(property "Reference" "R1")
		(pad "1" smd rect
			(at -0.5 0.0)
			(size 0.5 0.5)
			(layers "F.Cu" "F.Mask")
			(net 1 "VCC")
		)
	)
	(footprint "C"
		(layer "F.Cu")
		(at 60.0 40.0 0.0)
		(property "Reference" "C1")
		(pad "1" smd rect
			(at 0.0 -0.5)
			(size 0.5 0.5)
			(layers "F.Cu" "F.Mask")
			(net 1 "VCC")
		)
	)
	(gr_rect
		(start 20.0 20.0)
		(end 70.0 60.0)
		(stroke (width 0.1) (type solid))
		(fill none)
		(layer "Edge.Cuts")
	)
)
"""
    pcb_path = tmp_path / "clear_board.kicad_pcb"
    pcb_path.write_text(pcb)
    return str(pcb_path)


def _route_clear(req_extra: dict, tmp_path: Path):
    from kcaa.router.router import RouteRequest, auto_route_pair

    base = {
        "pcb_path": _make_clear_board(tmp_path),
        "ref_a": "R1",
        "pad_a": "1",
        "ref_b": "C1",
        "pad_b": "1",
        "net": "VCC",
        "width": 0.2,
        "clearance": 0.2,
        "via_pairs": (),
    }
    base.update(req_extra)
    return auto_route_pair(RouteRequest(**base))


def test_engine_rounded45_emits_arc(tmp_path: Path) -> None:
    """corner_mode=rounded45 on an unobstructed skeleton must produce
    OutputArc nodes whose start point sits on the route start pad."""
    result = _route_clear({"corner_mode": "rounded45", "algorithm": "pns"}, tmp_path)
    assert len(result.arcs) == 1
    a = result.arcs[0]
    assert a.width == 0.2
    assert a.layer == "F.Cu"
    assert a.net == "VCC"
    # Arc endpoints anchored on the skeleton legs.
    assert a.start[1] == pytest.approx(30.0, abs=1e-3)
    assert a.end == pytest.approx((60.0, 39.5), abs=1e-3)


def test_engine_mitered45_no_arcs(tmp_path: Path) -> None:
    result = _route_clear({"corner_mode": "mitered45"}, tmp_path)
    assert result.arcs == []
    assert len(result.segments) >= 2


def test_engine_default_corner_mode_is_rounded45(tmp_path: Path) -> None:
    """Omitting corner_mode defaults to rounded45: an unobstructed PNS
    skeleton emits its fillet arc and the result echoes the default."""
    result = _route_clear({"algorithm": "pns"}, tmp_path)
    assert result.corner_mode == "rounded45"
    assert len(result.arcs) == 1


def test_engine_rounded90_arc(tmp_path: Path) -> None:
    result = _route_clear({"corner_mode": "rounded90", "algorithm": "pns"}, tmp_path)
    assert len(result.arcs) == 1


def test_engine_corner_mode_echoed(tmp_path: Path) -> None:
    result = _route_clear({"corner_mode": "rounded90"}, tmp_path)
    assert result.corner_mode == "rounded90"


def test_engine_invalid_corner_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(RouteFailure) as excinfo:
        _route_clear({"corner_mode": "octagonal"}, tmp_path)
    assert "corner_mode" in str(excinfo.value)


def test_engine_empty_shoved_tracks_by_default(tmp_path: Path) -> None:
    result = _route_clear({}, tmp_path)
    assert result.shoved_tracks == []


def test_engine_detour_linearizes_arc(tmp_path: Path) -> None:
    """An obstacle on the skeleton forces a walkaround; the rounded arc
    must be linearized (no OutputArc) and the path must clear the
    obstacle by the DRC clearance."""
    from shapely.geometry import LineString

    pcb = """(kicad_pcb
	(version 20260206)
	(generator "test")
	(layers
		(0 "F.Cu" signal)
		(44 "Edge.Cuts" user)
	)
	(net 0 "")
	(net 1 "VCC")
	(footprint "R"
		(layer "F.Cu")
		(at 30.0 30.0 0.0)
		(property "Reference" "R1")
		(pad "1" smd rect
			(at -0.5 0.0)
			(size 0.5 0.5)
			(layers "F.Cu" "F.Mask")
			(net 1 "VCC")
		)
	)
	(footprint "C"
		(layer "F.Cu")
		(at 60.0 40.0 0.0)
		(property "Reference" "C1")
		(pad "1" smd rect
			(at 0.0 -0.5)
			(size 0.5 0.5)
			(layers "F.Cu" "F.Mask")
			(net 1 "VCC")
		)
	)
	(footprint "BLOCK"
		(layer "F.Cu")
		(at 46.0 32.0 0.0)
		(property "Reference" "U1")
		(pad "1" smd rect
			(at 0.0 0.0)
			(size 4.0 4.0)
			(layers "F.Cu" "F.Mask")
			(net 2 "OTHER")
		)
	)
	(gr_rect
		(start 20.0 20.0)
		(end 70.0 60.0)
		(stroke (width 0.1) (type solid))
		(fill none)
		(layer "Edge.Cuts")
	)
)
"""
    pcb_path = tmp_path / "blocked_board.kicad_pcb"
    pcb_path.write_text(pcb)
    from kcaa.router.router import RouteRequest, auto_route_pair

    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(pcb_path),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.2,
            clearance=0.2,
            via_pairs=(),
            corner_mode="rounded45",
        )
    )
    assert result.arcs == []
    # Every emitted segment clears the blocker by >= clearance.
    blocker = _rounded_square((46.0, 32.0), 4.0)
    for seg in result.segments:
        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
        assert line.distance(blocker) >= 0.2 - 0.01


def _rounded_square(center: tuple[float, float], size: float):
    """Axis-aligned square polygon (no rounding — clearance measured to the
    copper edge)."""
    from shapely.geometry import box

    half = size / 2.0
    return box(center[0] - half, center[1] - half, center[0] + half, center[1] + half)


# ---------------------------------------------------------------------------
# algorithm selector — astar (default) vs pns, single route single algorithm
# ---------------------------------------------------------------------------


def _route_board_copy(tmp_path: Path, src: str = "test_routing_board.kicad_pcb") -> Path:
    """Copy the routing fixture into tmp_path so board+project siblings travel
    together."""
    fixture = Path(__file__).resolve().parents[2] / "integration" / "fixtures" / src
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(Path(fixture).read_text())
    pro = fixture.with_suffix(".kicad_pro")
    if pro.exists():
        (tmp_path / pro.name).write_text(Path(pro).read_text())
    return dst


def test_algorithm_astar_single_layer_routes(tmp_path: Path) -> None:
    """algorithm='astar' on a single-layer pair restores the grid A*
    behaviour (hierarchical A*); result echoes algorithm."""
    dst = _route_board_copy(tmp_path)
    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.25,
            clearance=0.2,
            algorithm="astar",
        )
    )
    assert len(result.segments) > 0
    assert result.vias == []
    assert result.algorithm == "astar"


def test_algorithm_default_is_astar(tmp_path: Path) -> None:
    """Omitting algorithm routes with the grid A* planner (pure old
    behaviour) and echoes 'astar'."""
    dst = _route_board_copy(tmp_path)
    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.25,
            clearance=0.2,
        )
    )
    assert len(result.segments) > 0
    assert result.algorithm == "astar"


def test_algorithm_pns_single_layer_echoes_pns(tmp_path: Path) -> None:
    """algorithm='pns' routes the same pair with the walkaround + shove
    engine and echoes 'pns'."""
    dst = _route_board_copy(tmp_path)
    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
        )
    )
    assert len(result.segments) > 0
    assert result.algorithm == "pns"


def _x1_tht_on_b_cu() -> str:
    """X1: a thru-hole endpoint pad on B.Cu at (62, 45), on net VCC."""
    return (
        "\n\t# ---- X1: THT endpoint pad on B.Cu ----\n"
        '\t(footprint "user_add:edge-connector"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "66666666-0000-0000-0000-000000000006")\n'
        "\t\t(at 62.0 45.0 0.0)\n"
        '\t\t(property "Reference" "X1")\n'
        '\t\t(property "Value" "X1")\n'
        '\t\t(pad "T" thru_hole circle\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 1.6 1.6)\n"
        "\t\t\t(drill 1.0)\n"
        '\t\t\t(layers "*.Cu" "*.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )


def _write_pns_board(tmp_path: Path, extra: str) -> Path:
    """Fixture board + ``extra`` footprints, with the .kicad_pro paired
    under the matching base name (PNS via placement DRC-checks against the
    netclass rules)."""
    dst = tmp_path / "board.kicad_pcb"
    board = Path(_fixture_pcb()).read_text().rstrip()
    assert board.endswith(")")
    dst.write_text(board[:-1] + extra + ")\n")
    shutil.copy(_PRO_FIXTURE, tmp_path / "board.kicad_pro")
    return dst


def test_pns_multi_layer_route_crosses_layers(tmp_path: Path) -> None:
    """algorithm='pns' on a cross-layer pair (R1/1 F.Cu -> X1/T B.Cu routes
    one walkaround + shove leg per layer, joined by a DRC-clean through-via
    on the direct pad-to-pad line.  With an explicit mitered45 corner mode
    the route stays straight (no arcs) and the via never lands on a
    same-net pad."""
    from shapely.geometry import Point, box

    dst = _write_pns_board(tmp_path, _x1_tht_on_b_cu())

    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="X1",
            pad_b="T",
            net="VCC",
            layer_hint="B.Cu",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
            corner_mode="mitered45",
        )
    )
    assert result.algorithm == "pns"
    assert len(result.vias) >= 1
    assert result.layers_used == ["F.Cu", "B.Cu"]
    assert len(result.segments) > 0
    # mitered45 corners emit straight segments only.
    assert result.arcs == []
    # No via on any same-net pad copper (endpoint pads included): R1/1
    # 0.5x0.5 at (29.5,30), C1/1 0.5x0.5 at (60,29.5), X1/T r=0.8 at (62,45).
    same_net_pads = [
        box(29.25, 29.75, 29.75, 30.25),
        box(59.75, 29.25, 60.25, 29.75),
        Point(62.0, 45.0).buffer(0.8),
    ]
    for via in result.vias:
        pt = Point(via.x, via.y)
        for i, poly in enumerate(same_net_pads):
            assert not poly.contains(pt), (
                f"PNS via at ({via.x:.3f},{via.y:.3f}) lands on a same-net pad polygon #{i}"
            )


def test_pns_multi_layer_via_not_on_same_net_pad(tmp_path: Path) -> None:
    """The PNS via picker must avoid ALL same-net pad faces, not just the
    two endpoint pads (DFM defect: solder wicking / annular-ring breakout).
    Mirrors the multi-layer A* test of the same name."""
    from shapely.geometry import Point, box

    # X3: same-net SMD pads on F.Cu near the route corridor between
    # R1/1 (29.5, 30) and X1/T (64, 45).  Neither pad is an endpoint;
    # the PNS via must avoid both.
    x3 = (
        "\n\t# ---- X3: same-net SMD pads, via must avoid ----\n"
        '\t(footprint "user_add:via-dodge"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "99999999-0000-0000-0000-000000000009")\n'
        "\t\t(at 45.0 40.0 0.0)\n"
        '\t\t(property "Reference" "X3")\n'
        '\t\t(property "Value" "X3")\n'
        '\t\t(pad "1" smd rect\n'
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 2.0 2.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        '\t\t(pad "2" smd rect\n'
        "\t\t\t(at 5.0 0.0)\n"
        "\t\t\t(size 2.0 2.0)\n"
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 1 "VCC")\n'
        "\t\t)\n"
        "\t)\n"
    )
    dst = _write_pns_board(tmp_path, x3 + _x1_tht_on_b_cu())

    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="X1",
            pad_b="T",
            net="VCC",
            layer_hint="B.Cu",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
        )
    )
    assert len(result.vias) > 0
    # Pad copper rectangles (unbuffered, world coords): 2x2 at (45,40)
    # and (50,40).
    pad_rects = [box(44, 39, 46, 41), box(49, 39, 51, 41)]
    for via in result.vias:
        pt = Point(via.x, via.y)
        for i, rect in enumerate(pad_rects):
            assert not rect.contains(pt), (
                f"PNS via at ({via.x:.3f},{via.y:.3f}) lands on X3/{i + 1} pad"
            )


def test_pns_multi_layer_unreachable_via_pairs(tmp_path: Path) -> None:
    """When via_pairs cannot connect the start and end layers, the PNS
    multi-layer branch must raise RouteFailure with the missing transition,
    not guess a layer path."""
    dst = _route_board_copy(tmp_path)
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="D1",  # pad 1 on In1.Cu
        pad_a="1",
        ref_b="R1",  # pad 2 on F.Cu
        pad_b="2",
        net="GND",
        width=0.25,
        clearance=0.2,
        algorithm="pns",
        via_pairs=(("F.Cu", "B.Cu"),),  # no edge touches In1.Cu
    )
    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(req)
    msg = str(excinfo.value)
    assert "layer path" in msg
    assert "In1.Cu" in msg
    assert "F.Cu" in msg


def test_pns_multi_layer_missing_pro_rejects_unvalidated_vias(
    tmp_path: Path,
) -> None:
    """Via placement must stay strict when the .kicad_pro is missing: the
    route cannot silently place vias without netclass/DRC data (check_vias
    reports a project-level failure the candidate indices can't match)."""
    src = _fixture_pcb()
    dst = tmp_path / "board.kicad_pcb"
    board = Path(src).read_text().rstrip()
    assert board.endswith(")")
    dst.write_text(board[:-1] + _x1_tht_on_b_cu() + ")\n")
    # Deliberately no .kicad_pro next to the board.
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="X1",
        pad_b="T",
        net="VCC",
        layer_hint="B.Cu",
        width=0.25,  # explicit: skip width/clearance DRC lookup
        clearance=0.2,
        algorithm="pns",
    )
    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(req)
    assert "cannot DRC-check PNS vias" in str(excinfo.value)


def test_pns_multi_layer_three_legs_share_via_anchors(tmp_path: Path) -> None:
    """A 2-via / 3-leg PNS route must share each via anchor XY across the
    two legs it joins: every via touches a segment endpoint on BOTH of its
    layers (the per-leg engine paths rendezvous at the placed via)."""
    dst = _write_pns_board(tmp_path, _x1_tht_on_b_cu())

    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="R1",
            pad_a="1",
            ref_b="X1",
            pad_b="T",
            net="VCC",
            layer_hint="In1.Cu",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
            via_pairs=(("F.Cu", "B.Cu"), ("B.Cu", "In1.Cu")),
        )
    )
    assert result.layers_used == ["F.Cu", "B.Cu", "In1.Cu"]
    assert len(result.vias) == 2
    for via in result.vias:
        for layer in via.layers:
            touched = any(
                (abs(s.x1 - via.x) < 1e-3 and abs(s.y1 - via.y) < 1e-3)
                or (abs(s.x2 - via.x) < 1e-3 and abs(s.y2 - via.y) < 1e-3)
                for s in result.segments
                if s.layer == layer
            )
            assert touched, (
                f"Via at ({via.x:.3f},{via.y:.3f}) on {via.layers} is not "
                f"joined by a segment on layer {layer}"
            )


def test_pns_multi_layer_rounded45_emits_leg_arc(tmp_path: Path) -> None:
    """Multi-layer PNS with corner_mode='rounded45' emits rounded-corner
    arcs inside a leg whose skeleton survived walkaround/shove, and never
    places an arc on a via junction: the via stays a straight-through
    connection (the arc's start/mid/end avoid every via center)."""
    # Route X1/T (THT pad, B.Cu) -> R1/1 (F.Cu): the B.Cu leg is
    # unobstructed, so its rounded skeleton (interior corner) survives
    # and a fillet arc is emitted on that leg.
    dst = _write_pns_board(tmp_path, _x1_tht_on_b_cu())
    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="X1",
            pad_a="T",
            ref_b="R1",
            pad_b="1",
            net="VCC",
            layer_hint="B.Cu",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
            corner_mode="rounded45",
        )
    )
    assert result.algorithm == "pns"
    assert len(result.vias) >= 1
    assert len(result.arcs) >= 1
    for arc in result.arcs:
        assert arc.layer in ("F.Cu", "B.Cu", "In1.Cu")
        assert arc.net == "VCC"
        assert arc.width == pytest.approx(0.25)
        for pt in (arc.start, arc.mid, arc.end):
            for via in result.vias:
                assert not (abs(pt[0] - via.x) < 1e-6 and abs(pt[1] - via.y) < 1e-6), (
                    f"PNS arc point {pt} coincides with via center ({via.x:.3f}, {via.y:.3f})"
                )


def test_pns_multi_layer_mitered45_default_keeps_straight_segments(
    tmp_path: Path,
) -> None:
    """The same multi-layer pair with corner_mode='mitered45' (the
    default) emits straight segments + vias only — the rounded45 arc
    behavior must not leak into the default output."""
    dst = _write_pns_board(tmp_path, _x1_tht_on_b_cu())
    result = auto_route_pair(
        RouteRequest(
            pcb_path=str(dst),
            ref_a="X1",
            pad_a="T",
            ref_b="R1",
            pad_b="1",
            net="VCC",
            layer_hint="B.Cu",
            width=0.25,
            clearance=0.2,
            algorithm="pns",
            corner_mode="mitered45",
        )
    )
    assert len(result.vias) >= 1
    assert len(result.segments) > 0
    assert result.arcs == []


def test_algorithm_invalid_value_raises_route_failure(tmp_path: Path) -> None:
    """An unknown algorithm value must fail loudly, not fall back to a
    default planner."""
    dst = _route_board_copy(tmp_path)
    req = RouteRequest(
        pcb_path=str(dst),
        ref_a="R1",
        pad_a="1",
        ref_b="C1",
        pad_b="1",
        net="VCC",
        width=0.25,
        clearance=0.2,
        algorithm="bogus",
    )
    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(req)
    assert "algorithm" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Anchor chain (waypoints / via anchors) -- W2
# ---------------------------------------------------------------------------


def _make_two_layer_board(tmp_path: Path, extra: str = "") -> Path:
    """Minimal clear board with F.Cu + B.Cu endpoint pads and a paired
    .kicad_pro (netclass Default with via rules + design rules), so PNS
    via placement DRC-checks resolve.  ``extra`` footprints are spliced
    in before the final ``)``."""
    pcb = """(kicad_pcb
	(version 20260206)
	(generator "test")
	(layers
		(0 "F.Cu" signal)
		(31 "B.Cu" signal)
		(44 "Edge.Cuts" user)
	)
	(net 0 "")
	(net 1 "VCC")
	(net 2 "GND")
	(footprint "R"
		(layer "F.Cu")
		(at 30.0 30.0 0.0)
		(property "Reference" "R1")
		(pad "1" smd rect
			(at -0.5 0.0)
			(size 0.5 0.5)
			(layers "F.Cu" "F.Mask")
			(net 1 "VCC")
		)
	)
	(footprint "C"
		(layer "B.Cu")
		(at 60.0 40.0 0.0)
		(property "Reference" "C1")
		(pad "1" smd rect
			(at 0.0 -0.5)
			(size 0.5 0.5)
			(layers "B.Cu" "B.Mask")
			(net 1 "VCC")
		)
	)
	(gr_rect
		(start 20.0 20.0)
		(end 70.0 60.0)
		(stroke (width 0.1) (type solid))
		(fill none)
		(layer "Edge.Cuts")
	)
)
"""
    dst = tmp_path / "two_layer.kicad_pcb"
    dst.write_text(pcb.rstrip()[:-1] + extra + ")\n")
    pro = {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.2,
                    "min_track_width": 0.2,
                    "min_via_size": 0.5,
                    "min_through_drill": 0.25,
                }
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.25,
                    "via_diameter": 0.8,
                    "via_drill": 0.4,
                }
            ],
            "netclass_patterns": [],
        },
    }
    (tmp_path / "two_layer.kicad_pro").write_text(json.dumps(pro))
    return dst


def _route_pns_anchors(board: Path, anchors: list[dict]) -> RouteResult:
    return auto_route_pair(
        RouteRequest(
            pcb_path=str(board),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.2,
            clearance=0.2,
            via_diameter=0.8,
            via_drill=0.4,
            algorithm="pns",
            corner_mode="mitered45",
            anchors=anchors,
        )
    )


def test_anchors_waypoint_routes_through_point(tmp_path: Path) -> None:
    """A waypoint anchor forces the single-layer route through its
    tolerance circle; unreachable waypoints never fail the route."""
    from shapely.geometry import LineString, Point

    result = _route_clear(
        {
            "algorithm": "pns",
            "corner_mode": "mitered45",
            "anchors": [{"kind": "waypoint", "pos": (45.0, 30.0), "tol_mm": 1.0}],
        },
        tmp_path,
    )
    assert result.vias == []
    assert result.layers_used == ["F.Cu"]
    assert len(result.segments) > 0
    wpt = Point(45.0, 30.0)
    dists = [LineString([(s.x1, s.y1), (s.x2, s.y2)]).distance(wpt) for s in result.segments]
    assert min(dists) <= 1.0 + 1e-6
    assert result.waypoint_violated is False
    assert result.violated_waypoints == []
    assert result.via_sites == []


def test_anchors_waypoint_unreachable_is_soft_skip(tmp_path, monkeypatch) -> None:
    """A waypoint leg that fails (engine PnsFailure) is recorded as
    violated and skipped; the route still completes end to end."""
    from kcaa.router.route_engine import PnsFailure
    from kcaa.router.route_engine import route_engine as real_engine
    import kcaa.router.router as router_mod

    calls = {"n": 0}

    def flaky_engine(start, end, obstacles, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # the waypoint leg
            raise PnsFailure("synthetic waypoint blockage")
        return real_engine(start, end, obstacles, **kw)

    monkeypatch.setattr(router_mod, "route_engine", flaky_engine)
    result = _route_clear(
        {
            "algorithm": "pns",
            "corner_mode": "mitered45",
            "anchors": [{"kind": "waypoint", "pos": (45.0, 30.0), "tol_mm": 1.0}],
        },
        tmp_path,
    )
    assert calls["n"] == 2  # waypoint leg failed, final leg routed
    assert result.waypoint_violated is True
    assert result.violated_waypoints == [(45.0, 30.0)]
    assert len(result.segments) > 0
    # The route still ends on pad C1/1.
    assert result.end == pytest.approx((60.0, 39.5), abs=1e-3)


def test_anchors_via_explicit_cross_layer(tmp_path: Path) -> None:
    """An explicit via anchor switches the leg layer at its requested
    site: exactly one via, echoes the site + to_layer, route crosses."""
    result = _route_pns_anchors(
        _make_two_layer_board(tmp_path),
        [{"kind": "via", "pos": (45.0, 35.0), "to_layer": "B.Cu"}],
    )
    assert len(result.vias) == 1
    assert result.vias[0].x == pytest.approx(45.0, abs=1e-3)
    assert result.vias[0].y == pytest.approx(35.0, abs=1e-3)
    assert result.layers_used == ["F.Cu", "B.Cu"]
    assert len(result.segments) > 0
    assert len(result.via_sites) == 1
    assert result.via_sites[0]["to_layer"] == "B.Cu"
    assert result.via_sites[0]["pos"] == pytest.approx([45.0, 35.0], abs=1e-3)
    assert result.waypoint_violated is False


def test_anchors_via_shifts_off_blocked_site(tmp_path: Path) -> None:
    """A via anchor requested exactly on same-net pad copper micro-shifts
    to the nearest DRC-clean spot (within tol_mm) and routes anyway."""
    from shapely.geometry import Point, box

    same_net_pad = (
        "\n\t(footprint \"user_add:site-blocker\"\n"
        "\t\t(layer \"F.Cu\")\n"
        "\t\t(at 45.0 40.0 0.0)\n"
        "\t\t(property \"Reference\" \"X1\")\n"
        "\t\t(pad \"1\" smd rect\n"
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 0.3 0.3)\n"
        "\t\t\t(layers \"F.Cu\" \"F.Mask\")\n"
        "\t\t\t(net 1 \"VCC\")\n"
        "\t\t)\n"
        "\t)\n"
    )
    board = _make_two_layer_board(tmp_path, extra=same_net_pad)
    result = _route_pns_anchors(
        board,
        [{"kind": "via", "pos": (45.0, 40.0), "to_layer": "B.Cu"}],
    )
    assert len(result.via_sites) == 1
    site_x, site_y = result.via_sites[0]["pos"]
    assert (site_x, site_y) != pytest.approx((45.0, 40.0), abs=1e-6)
    assert math.hypot(site_x - 45.0, site_y - 40.0) <= 1.0 + 1e-6
    assert len(result.vias) == 1
    # The emitted via must not land on the same-net pad copper.
    pad = box(44.85, 39.85, 45.15, 40.15)
    assert not pad.contains(Point(result.vias[0].x, result.vias[0].y))
    assert len(result.segments) > 0


def test_anchors_failure_renders_png_evidence(tmp_path: Path) -> None:
    """A blocked via anchor with no DRC-clean spot inside tol_mm raises
    RouteFailure carrying the path of a rendered failure-evidence PNG."""
    foreign_blocker = (
        "\n\t(footprint \"user_add:wall\"\n"
        "\t\t(layer \"F.Cu\")\n"
        "\t\t(at 45.0 40.0 0.0)\n"
        "\t\t(property \"Reference\" \"X1\")\n"
        "\t\t(pad \"1\" smd rect\n"
        "\t\t\t(at 0.0 0.0)\n"
        "\t\t\t(size 4.0 4.0)\n"
        "\t\t\t(layers \"F.Cu\" \"F.Mask\")\n"
        "\t\t\t(net 2 \"GND\")\n"
        "\t\t)\n"
        "\t)\n"
    )
    board = _make_two_layer_board(tmp_path, extra=foreign_blocker)
    with pytest.raises(RouteFailure) as excinfo:
        _route_pns_anchors(
            board,
            [{"kind": "via", "pos": (45.0, 40.0), "tol_mm": 1.0, "to_layer": "B.Cu"}],
        )
    msg = str(excinfo.value)
    assert "no DRC-clean via spot" in msg
    assert "Failure evidence: " in msg
    png_path = msg.split("Failure evidence: ")[1].strip()
    assert Path(png_path).exists()
    assert png_path.endswith(".png")


def test_anchors_pad_not_supported_yet(tmp_path: Path) -> None:
    """Pad anchors are reserved (W4): the first pad anchor raises
    'not supported yet' instead of half-working."""
    with pytest.raises(RouteFailure, match="not supported yet"):
        _route_clear(
            {"algorithm": "pns", "anchors": [{"kind": "pad", "ref": "C1", "pad": "1"}]},
            tmp_path,
        )


def test_anchors_require_pns_algorithm(tmp_path: Path) -> None:
    """The A* planner must reject anchors loudly instead of ignoring them."""
    with pytest.raises(RouteFailure, match="only supported with algorithm"):
        _route_clear(
            {"algorithm": "astar", "anchors": [{"kind": "waypoint", "pos": (45.0, 30.0)}]},
            tmp_path,
        )


def test_anchors_unknown_kind_raises(tmp_path: Path) -> None:
    with pytest.raises(RouteFailure, match="unsupported anchor kind"):
        _route_clear(
            {"algorithm": "pns", "anchors": [{"kind": "jump", "pos": (45.0, 30.0)}]},
            tmp_path,
        )


# ---------------------------------------------------------------------------
# W3 — PNS candidates (multi-variant routes) + A* failure evidence
# ---------------------------------------------------------------------------


def _make_clear_board_with_track(tmp_path: Path) -> str:
    """Single-layer clear board plus one foreign-net GND track crossing the
    direct pad-to-pad line, so walkaround and shove diverge."""
    pcb_path = Path(_make_clear_board(tmp_path))
    text = pcb_path.read_text()
    text = text.replace('\t(net 1 "VCC")\n', '\t(net 1 "VCC")\n\t(net 2 "GND")\n')
    text = text.rstrip()[:-1] + (
        '\n\t(segment (start 40.0 25.0) (end 55.0 45.0) '
        '(width 0.25) (layer "F.Cu") (net 2 "GND"))\n'
    ) + ")\n"
    pcb_path.write_text(text)
    return str(pcb_path)


def _route_candidates(
    tmp_path: Path,
    *,
    candidates: int = 2,
    track: bool = False,
    algorithm: str = "pns",
    waypoints: list[dict] | None = None,
) -> RouteResult:
    from kcaa.router.router import RouteRequest, auto_route_pair

    pcb_path = _make_clear_board_with_track(tmp_path) if track else _make_clear_board(tmp_path)
    req = {
        "pcb_path": pcb_path,
        "ref_a": "R1",
        "pad_a": "1",
        "ref_b": "C1",
        "pad_b": "1",
        "net": "VCC",
        "width": 0.2,
        "clearance": 0.2,
        "via_pairs": (),
        "algorithm": algorithm,
        "corner_mode": "mitered45",
        "candidates": candidates,
    }
    if waypoints:
        req["anchors"] = waypoints
    return auto_route_pair(RouteRequest(**req))


def test_candidates_astar_rejected(tmp_path: Path) -> None:
    """``candidates > 1`` is the PNS control surface; A* is single-shot."""
    with pytest.raises(RouteFailure, match="only supported with algorithm"):
        _route_candidates(tmp_path, candidates=2, algorithm="astar")


def test_candidates_below_one_rejected(tmp_path: Path) -> None:
    with pytest.raises(RouteFailure, match="candidates must be >= 1"):
        _route_candidates(tmp_path, candidates=0)


def test_candidates_default_no_variants(tmp_path: Path) -> None:
    """candidates=1 (the default) keeps the single-route behavior: no
    candidates list, no render."""
    result = _route_candidates(tmp_path, candidates=1)
    assert result.candidates == []
    assert result.candidates_png is None
    assert len(result.segments) >= 1


def test_candidates_two_walkaround_and_shove(tmp_path: Path) -> None:
    """A foreign track across the line yields two distinct variants:
    walkaround-only (detour, nothing pushed) and walkaround+shove (track
    displaced).  The primary top-level result is exactly candidate 1."""
    result = _route_candidates(tmp_path, track=True, candidates=2)
    assert [c["variant"] for c in result.candidates] == ["walkaround", "shove"]
    wa, sh = result.candidates
    # Shove displacement happens on the track, not the routed polyline:
    # the variants differ in the shoved set (and usually the geometry).
    assert wa["shoved"] == []
    assert sh["shoved"], "shove variant must actually push the crossing track"
    distinct = (
        wa["segments"] != sh["segments"] or bool(wa["shoved"]) != bool(sh["shoved"])
    )
    assert distinct
    # Primary == first candidate (backwards compatible).
    assert result.shoved_tracks == []
    assert list(result.start) == result.candidates[0]["start"]
    assert list(result.end) == result.candidates[0]["end"]
    assert list(result.layers_used) == result.candidates[0]["layers_used"]
    assert result.via_sites == result.candidates[0]["via_sites"]
    assert result.waypoint_violated == result.candidates[0]["waypoint_violated"]
    all_tags = {c["variant"] for c in result.candidates}
    assert all_tags <= {"walkaround", "shove"}


def test_candidates_dedupe_collapses_identical(tmp_path: Path) -> None:
    """On a clear board every variant produces the same skeleton (nothing
    to detour, nothing to shove): the identical geometries dedupe to one
    candidate even when three are requested."""
    result = _route_candidates(tmp_path, candidates=3)
    assert len(result.candidates) == 1
    assert result.candidates[0]["variant"] == "walkaround"


def test_candidates_waypoint_tol_variants_dedupe(tmp_path: Path) -> None:
    """Waypoint-tolerance variants are informational today (W2 decision):
    they share the shove geometry and must dedupe, never duplicate it."""
    result = _route_candidates(
        tmp_path,
        track=True,
        candidates=3,
        waypoints=[{"kind": "waypoint", "pos": (45.0, 30.0)}],
    )
    tags = [c["variant"] for c in result.candidates]
    assert 2 <= len(tags) <= 5
    assert all(t in {"walkaround", "shove", "waypoint-tol-x0.5", "waypoint-tol-x1.0", "waypoint-tol-x2.0"} for t in tags)
    # No two candidates share the same (geometry, pushed) fingerprint.
    seen = set()
    for c in result.candidates:
        fp = (
            tuple(tuple(s) for s in c["segments"]),
            tuple(tuple(s) for s in c["arcs"]),
            len(c["shoved"]),
        )
        assert fp not in seen
        seen.add(fp)


def test_candidates_two_renders_candidates_png(tmp_path: Path) -> None:
    """candidates > 1 renders the side-by-side variant PNG next to the
    request and returns its path (best-effort, always safe)."""
    result = _route_candidates(tmp_path, track=True, candidates=2)
    assert result.candidates_png
    assert result.candidates_png.startswith(
        os.path.join(tempfile.gettempdir(), "kcaa_candidates_")
    )
    assert os.path.exists(result.candidates_png)
    assert os.path.getsize(result.candidates_png) > 0


def _make_blocked_single_layer_board(tmp_path: Path) -> str:
    """Single-layer clear board plus a foreign GND pad covering the R1.1
    start pad entirely: the A* start cell is inside an obstacle, so the
    search fails (grid_a_star refuses a blocked start)."""
    pcb_path = Path(_make_clear_board(tmp_path))
    text = pcb_path.read_text()
    text = text.replace('\t(net 1 "VCC")\n', '\t(net 1 "VCC")\n\t(net 2 "GND")\n')
    wall = (
        '\t(footprint "W"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(at 29.5 30.0 0.0)\n'
        '\t\t(property "Reference" "W1")\n'
        '\t\t(pad "1" smd rect\n'
        '\t\t\t(at -0.5 0.0)\n'
        '\t\t\t(size 6.0 6.0)\n'
        '\t\t\t(layers "F.Cu" "F.Mask")\n'
        '\t\t\t(net 2 "GND")\n'
        "\t\t)\n"
        "\t)\n"
    )
    text = text.rstrip()[:-1] + wall + ")\n"
    pcb_path.write_text(text)
    return str(pcb_path)


def test_astar_single_layer_blocked_renders_evidence(tmp_path: Path) -> None:
    """A foreign pad covering the start pad leaves A* no place to begin;
    the failure must carry the rendered evidence PNG (appended after the
    existing _dump_viz dump)."""
    from kcaa.router.router import RouteRequest, auto_route_pair

    with pytest.raises(RouteFailure) as excinfo:
        auto_route_pair(
            RouteRequest(
                pcb_path=_make_blocked_single_layer_board(tmp_path),
                ref_a="R1",
                pad_a="1",
                ref_b="C1",
                pad_b="1",
                net="VCC",
                width=0.2,
                clearance=0.2,
                via_pairs=(),
                algorithm="astar",
            )
        )
    msg = str(excinfo.value)
    assert "No obstacle-avoiding path from" in msg
    assert "Failure evidence: " in msg
    png = msg.split("Failure evidence: ", 1)[1].strip()
    assert png.startswith(os.path.join(tempfile.gettempdir(), "kcaa_route_failure_"))
    assert os.path.exists(png)


@pytest.mark.skip(
    reason=(
        "No cheap multi-layer A* failure fixture: the search box auto-expands "
        "to any obstacle that touches it, via edges may land inside a "
        "single-layer enclosing ring and escape it on the far layer, and "
        "endpoint-pad clearing punches holes in pad-covering walls — a "
        "guaranteed blocked case needs a full maze.  W3 taskbook test 8 "
        "allows skipping; the evidence-render chain is asserted by "
        "test_astar_single_layer_blocked_renders_evidence."
    )
)
def test_astar_multi_layer_blocked_renders_evidence(tmp_path: Path) -> None:
    """Multi-layer A* failure evidence — skipped: no cheap blocked fixture
    (see the skip reason); the single-layer failure evidence chain is
    asserted above."""
    board = _make_two_layer_board(tmp_path, extra="")
    auto_route_pair(
        RouteRequest(
            pcb_path=str(board),
            ref_a="R1",
            pad_a="1",
            ref_b="C1",
            pad_b="1",
            net="VCC",
            width=0.2,
            clearance=0.2,
            via_pairs=(("F.Cu", "B.Cu"),),
            algorithm="astar",
        )
    )
