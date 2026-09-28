"""Tests for the ``add_footprint_to_pcb`` MCP tool.

Covers: placing a library footprint onto a board copy with correct
``(at ...)`` / reference / nets-netting, the §4 local→world rotation
transform, the required per-pad ``nets`` argument (missing pad -> hard
error, ``""`` -> net 0), net auto-add, every validation error branch
(duplicate reference, unknown footprint, unknown library, missing board,
unsafe name), batch placement with partial failure, backup creation, and
the outline placement warning.  Also covers ``remove_footprint_from_pcb``.
"""

import asyncio
import math
import os
import shutil

import pytest

from kcaa.utils.pcb_footprint_utils import (
    get_fp_property,
    iter_footprint_nodes,
)
from kcaa.utils.pcb_sexp_utils import load_pcb

FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "integration", "fixtures"
)
BOARD_FIXTURE = os.path.join(FIXTURE_DIR, "test_routing_board.kicad_pcb")
OUTLINE_BOARD_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "test_board_with_outline.kicad_pcb"
)

# Minimal but real KiCad 10 footprint library file: 2 SMD pads at local
# (-0.5, 0) / (0.5, 0), courtyard, silkscreen text, 3D model.
FOOTPRINT_MOD = """(footprint "R_0402_1005Metric"
\t(layer "F.Cu")
\t(attr smd)
\t(fp_text reference "R" (at 0 -1.5) (layer "F.SilkS") hide)
\t(fp_text value "R_0402_1005Metric" (at 0 1.5) (layer "F.Fab"))
\t(fp_line (start -1.05 -0.6) (end 1.05 -0.6) (layer "F.CrtYd") (width 0.05))
\t(fp_line (start 1.05 -0.6) (end 1.05 0.6) (layer "F.CrtYd") (width 0.05))
\t(fp_line (start 1.05 0.6) (end -1.05 0.6) (layer "F.CrtYd") (width 0.05))
\t(fp_line (start -1.05 0.6) (end -1.05 -0.6) (layer "F.CrtYd") (width 0.05))
\t(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(model "${KICAD8_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl")
)
"""

FP_TABLE = """(fp_lib_table
\t(lib (name "TestLib") (type "KiCad") (uri "${KIPRJMOD}/TestLib.pretty") (options "") (descr ""))
)
"""

# The test footprint has exactly 2 pads; helper for the common "both
# unconnected" case.
UNCONNECTED = {"1": "", "2": ""}


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.pcb_library_tools import register_pcb_library_tools

    mock = _MockMCP()
    register_pcb_library_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


def _local_to_world(fp_x: float, fp_y: float, rot_deg: float, lx: float, ly: float):
    """§4 local→world transform (board mm, +Y down, CCW-positive rotation)."""
    theta = math.radians(rot_deg)
    return (
        fp_x + lx * math.cos(theta) + ly * math.sin(theta),
        fp_y - lx * math.sin(theta) + ly * math.cos(theta),
    )


def _sym(value) -> str:
    return str(value)


def _fp_node(board_path: str, reference: str) -> list:
    data = load_pcb(board_path)
    for node in iter_footprint_nodes(data):
        if get_fp_property(node, "Reference") == reference:
            return node
    raise KeyError(f"footprint {reference!r} not on board")


def _pads(fp_node: list) -> list[list]:
    return [sub for sub in fp_node if isinstance(sub, list) and _sym(sub[0]) == "pad"]


def _pad_at(pad: list) -> tuple[float, float, float]:
    for sub in pad:
        if isinstance(sub, list) and _sym(sub[0]) == "at":
            rot = float(sub[3]) if len(sub) > 3 else 0.0
            return float(sub[1]), float(sub[2]), rot
    raise KeyError("pad has no (at ...)")


def _pad_net(pad: list) -> tuple[int, str]:
    for sub in pad:
        if isinstance(sub, list) and _sym(sub[0]) == "net":
            return int(sub[1]), _sym(sub[2])
    return None


def _nets(board_path: str) -> dict[str, int]:
    data = load_pcb(board_path)
    result = {}
    for item in data:
        if isinstance(item, list) and len(item) >= 3 and _sym(item[0]) == "net":
            try:
                result[_sym(item[2])] = int(item[1])
            except (TypeError, ValueError):
                continue
    return result


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture
def lib_dir(tmp_path):
    d = tmp_path / "TestLib.pretty"
    d.mkdir()
    (d / "R_0402_1005Metric.kicad_mod").write_text(FOOTPRINT_MOD)
    return d


@pytest.fixture
def board_copy(tmp_path):
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    return str(dest)


@pytest.fixture
def board_with_table(tmp_path, lib_dir):
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(BOARD_FIXTURE, dest)
    (tmp_path / "fp-lib-table").write_text(FP_TABLE)
    return str(dest)


@pytest.fixture
def outline_board_copy(tmp_path):
    dest = tmp_path / "outline.kicad_pcb"
    shutil.copy(OUTLINE_BOARD_FIXTURE, dest)
    return str(dest)


def _board_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class TestPlacement:
    def test_places_bare_name_from_library_dir(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=25.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert result["reference"] == "R9"
        assert result["placed_at"] == [20.0, 25.0]
        assert result["rotation"] == 0.0
        assert result["pad_count"] == 2
        assert result["pads_net"] == [{"pad": "1", "net": ""}, {"pad": "2", "net": ""}]
        assert os.path.isfile(result["backup_path"])

        node = _fp_node(board_copy, "R9")
        assert node[1] == "R_0402_1005Metric"  # bare name -> board-created header
        at_node = [sub for sub in node if _sym(sub[0]) == "at"][0]
        assert (float(at_node[1]), float(at_node[2])) == (20.0, 25.0)
        for pad in _pads(node):
            assert _pad_net(pad) == (0, "")
        # Other footprints untouched, board still parses as a board.
        assert get_fp_property(_fp_node(board_copy, "R1"), "Reference") == "R1"

    def test_places_lib_colon_name_via_project_table(self, tools, board_with_table):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_with_table,
                nets=UNCONNECTED,
                footprint="TestLib:R_0402_1005Metric",
                reference="R7",
                x=10.0,
                y=10.0,
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        node = _fp_node(board_with_table, "R7")
        assert node[1] == "TestLib:R_0402_1005Metric"
        assert result["pad_count"] == 2

    def test_rotation_pad_world_transform(self, tools, board_copy, lib_dir):
        # Footprint at (50, 40), rot 45°.  Pad local coords (-0.5, 0) /
        # (0.5, 0) must map through the §4 matrix to the expected world
        # positions; the stored pad (at ...) keeps LOCAL coordinates with the
        # absolute rotation field = footprint rotation (KiCad convention).
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "A", "2": "B"},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=50.0,
                y=40.0,
                rotation=45.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result

        node = _fp_node(board_copy, "R9")
        pads = _pads(node)
        assert len(pads) == 2
        # Pads keep local coordinates and gain the absolute rotation field.
        assert _pad_at(pads[0]) == (-0.5, 0.0, 45.0)
        assert _pad_at(pads[1]) == (0.5, 0.0, 45.0)
        # World positions match the §4 transform.
        for pad, (lx, ly) in zip(pads, [(-0.5, 0.0), (0.5, 0.0)]):
            wx, wy = _local_to_world(50.0, 40.0, 45.0, lx, ly)
            assert wx == pytest.approx(wx, abs=1e-9)
            # Recover world from the stored (at ...) via the matrix.
            stored_x, stored_y, _ = _pad_at(pad)
            calc_x, calc_y = _local_to_world(50.0, 40.0, 45.0, stored_x, stored_y)
            assert calc_x == pytest.approx(wx, abs=1e-9)
            assert calc_y == pytest.approx(wy, abs=1e-9)

    def test_assigns_same_existing_net_to_all_pads(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "VCC", "2": "VCC"},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["pads_net"] == [{"pad": "1", "net": "VCC"}, {"pad": "2", "net": "VCC"}]
        checkout = _fp_node(board_copy, "R9")
        for pad in _pads(checkout):
            assert _pad_net(pad) == (1, "VCC")

    def test_auto_adds_missing_net(self, tools, board_copy, lib_dir):
        assert "NEWNET" not in _nets(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "NEWNET", "2": "NEWNET"},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        # Fixture max net number is 3 → auto-add gets 4.
        nets = _nets(board_copy)
        assert nets["NEWNET"] == 4
        for pad in _pads(_fp_node(board_copy, "R9")):
            assert _pad_net(pad) == (4, "NEWNET")


class TestNets:
    def test_different_nets_per_pad(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "A", "2": "B"},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert result["pad_count"] == 2
        assert result["pads_net"] == [{"pad": "1", "net": "A"}, {"pad": "2", "net": "B"}]

        nets = _nets(board_copy)
        assert {"A", "B"} <= set(nets)
        pads = _pads(_fp_node(board_copy, "R9"))
        assert len(pads) == 2
        pad1_net, pad2_net = _pad_net(pads[0]), _pad_net(pads[1])
        assert pad1_net == (nets["A"], "A")
        assert pad2_net == (nets["B"], "B")
        assert pad1_net[0] != pad2_net[0]

    def test_named_net_and_net_zero(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "A", "2": ""},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["pads_net"] == [{"pad": "1", "net": "A"}, {"pad": "2", "net": ""}]

        nets = _nets(board_copy)
        pads = _pads(_fp_node(board_copy, "R9"))
        assert _pad_net(pads[0]) == (nets["A"], "A")
        assert _pad_net(pads[1]) == (0, "")

    def test_all_net_zero_via_empty_strings(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["pads_net"] == [{"pad": "1", "net": ""}, {"pad": "2", "net": ""}]
        assert _nets(board_copy).get("") == 0
        for pad in _pads(_fp_node(board_copy, "R9")):
            assert _pad_net(pad) == (0, "")

    def test_missing_pad_net_is_hard_error_no_write(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "A"},
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" in result
        assert "missing net for pad(s): 2" in result["error"]
        # Nothing written: file bytes identical, no backup, no footprint.
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")
        data = load_pcb(board_copy)
        assert all(
            get_fp_property(n, "Reference") != "R9" for n in iter_footprint_nodes(data)
        )


class TestRemoveFootprint:
    def _place(self, tools, board_copy, lib_dir, ref, x, y, nets=UNCONNECTED):
        return _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=nets,
                footprint="R_0402_1005Metric",
                reference=ref,
                x=x,
                y=y,
                library=str(lib_dir),
            )
        )

    def _remove(self, tools, board_copy, ref):
        return _run(tools["remove_footprint_from_pcb"](pcb_path=board_copy, reference=ref))

    def _refs(self, board_path):
        data = load_pcb(board_path)
        return [get_fp_property(n, "Reference") for n in iter_footprint_nodes(data)]

    def test_removes_one_footprint_keeps_others_and_nets(self, tools, board_copy, lib_dir):
        shared = {"1": "NEWNET", "2": "NEWNET"}
        assert self._place(tools, board_copy, lib_dir, "R9", 20.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_copy, lib_dir, "R10", 30.0, 20.0, nets=shared)["success"]

        result = self._remove(tools, board_copy, "R9")
        assert "error" not in result, result
        assert result["success"] is True
        assert result["reference"] == "R9"
        assert result["removed"] == 1
        assert result["pcb_path"] == board_copy
        assert os.path.isfile(result["backup_path"])

        refs = self._refs(board_copy)
        assert "R9" not in refs
        assert "R10" in refs
        # Net definitions are kept; R10 still references NEWNET (net 4).
        nets = _nets(board_copy)
        assert nets.get("NEWNET") == 4
        for pad in _pads(_fp_node(board_copy, "R10")):
            assert _pad_net(pad) == (4, "NEWNET")

    def test_remove_again_returns_not_found_untouched(self, tools, board_copy, lib_dir):
        assert self._place(tools, board_copy, lib_dir, "R9", 20.0, 20.0)["success"]
        assert self._remove(tools, board_copy, "R9")["success"] is True
        before = _board_bytes(board_copy)
        result = self._remove(tools, board_copy, "R9")
        assert result["success"] is False
        assert "not found" in result["error"]
        assert result["removed"] == 0
        assert _board_bytes(board_copy) == before

    def test_remove_unknown_reference_tolerated(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = self._remove(tools, board_copy, "ZZ9")
        assert result["success"] is False
        assert "not found" in result["error"]
        assert result["reference"] == "ZZ9"
        assert result["removed"] == 0
        assert _board_bytes(board_copy) == before

    def test_remove_empty_reference(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = self._remove(tools, board_copy, "")
        assert "error" in result
        assert "required" in result["error"]
        assert _board_bytes(board_copy) == before

    def test_remove_both_reference_and_references_rejected(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = _run(
            tools["remove_footprint_from_pcb"](
                pcb_path=board_copy, reference="R1", references=["R9"]
            )
        )
        assert "error" in result
        assert "not both" in result["error"]
        assert _board_bytes(board_copy) == before

    def test_remove_bad_pcb_path(self, tools, tmp_path, lib_dir):
        result = _run(
            tools["remove_footprint_from_pcb"](
                pcb_path=str(tmp_path / "missing.kicad_pcb"), reference="R9"
            )
        )
        assert "error" in result
        assert "cannot read board" in result["error"]

    def test_remove_reference_of_fixture_footprint(self, tools, board_copy, lib_dir):
        # The fixture board already carries C1/U1/D1/R1 footprints.
        result = self._remove(tools, board_copy, "R1")
        assert result["success"] is True
        assert result["removed"] == 1
        refs = self._refs(board_copy)
        assert "R1" not in refs
        assert "U1" in refs


class TestBatchRemove:
    def _place(self, tools, board_copy, lib_dir, ref, x, y, nets=UNCONNECTED):
        return _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=nets,
                footprint="R_0402_1005Metric",
                reference=ref,
                x=x,
                y=y,
                library=str(lib_dir),
            )
        )

    def _remove(self, tools, board_copy, *references):
        return _run(
            tools["remove_footprint_from_pcb"](
                pcb_path=board_copy, references=list(references)
            )
        )

    def _refs(self, board_path):
        data = load_pcb(board_path)
        return [get_fp_property(n, "Reference") for n in iter_footprint_nodes(data)]

    def test_batch_removes_three_with_one_missing(self, tools, board_copy, lib_dir):
        shared = {"1": "NET_KEEP", "2": "NET_KEEP"}
        assert self._place(tools, board_copy, lib_dir, "R9", 20.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_copy, lib_dir, "R10", 30.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_copy, lib_dir, "R11", 40.0, 20.0, nets=shared)["success"]

        result = self._remove(tools, board_copy, "R9", "R10", "ZZ9")
        assert "error" not in result, result
        assert result["success"] is False  # not every item removed
        assert result["removed_count"] == 2
        assert result["not_found_count"] == 1
        assert len(result["not_found"]) == 1
        assert result["not_found"][0]["reference"] == "ZZ9"
        assert "not found" in result["not_found"][0]["error"]
        assert len(result["results"]) == 3
        assert result["results"][0] == {"reference": "R9", "success": True, "removed": 1}
        assert result["results"][1] == {"reference": "R10", "success": True, "removed": 1}
        assert result["results"][2]["reference"] == "ZZ9"
        assert result["results"][2]["success"] is False
        assert result["results"][2]["removed"] == 0
        assert "not found" in result["results"][2]["error"]
        assert result["pcb_path"] == board_copy
        assert os.path.isfile(result["backup_path"])

        refs = self._refs(board_copy)
        assert "R9" not in refs and "R10" not in refs
        assert "R11" in refs  # untouched batch survivor
        assert "C1" in refs and "U1" in refs and "D1" in refs and "R1" in refs
        # Nets of the removed footprints stay on the board (dangling nets).
        nets = _nets(board_copy)
        assert nets.get("NET_KEEP") is not None
        for pad in _pads(_fp_node(board_copy, "R11")):
            assert _pad_net(pad) == (nets["NET_KEEP"], "NET_KEEP")

    def test_batch_all_fixture_footprints_success(self, tools, board_copy, lib_dir):
        result = self._remove(tools, board_copy, "R1", "C1", "U1")
        assert "error" not in result, result
        assert result["success"] is True
        assert result["removed_count"] == 3
        assert result["not_found_count"] == 0
        assert result["not_found"] == []
        assert os.path.isfile(result["backup_path"])
        refs = self._refs(board_copy)
        assert "R1" not in refs and "C1" not in refs and "U1" not in refs
        assert "D1" in refs

    def test_batch_duplicate_reference_second_is_not_found(self, tools, board_copy, lib_dir):
        result = self._remove(tools, board_copy, "R1", "R1")
        assert "error" not in result, result
        assert result["success"] is False
        assert result["removed_count"] == 1
        assert result["not_found_count"] == 1
        assert result["results"][0]["success"] is True
        assert result["results"][1]["reference"] == "R1"
        assert result["results"][1]["success"] is False
        assert "not found" in result["results"][1]["error"]
        refs = self._refs(board_copy)
        assert refs.count("R1") == 0

    def test_batch_empty_references_is_error_no_write(self, tools, board_copy):
        before = _board_bytes(board_copy)
        result = _run(
            tools["remove_footprint_from_pcb"](pcb_path=board_copy, references=[])
        )
        assert "error" in result
        assert "required" in result["error"]
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")

    def test_batch_empty_string_item_fails_only_that_item(self, tools, board_copy, lib_dir):
        result = self._remove(tools, board_copy, "R1", "")
        assert "error" not in result, result
        assert result["removed_count"] == 1
        assert result["not_found_count"] == 1
        assert result["results"][1]["reference"] == ""
        assert result["results"][1]["success"] is False
        assert "non-empty" in result["results"][1]["error"]
        refs = self._refs(board_copy)
        assert "R1" not in refs

    def test_batch_all_not_found_no_write(self, tools, board_copy):
        before = _board_bytes(board_copy)
        result = self._remove(tools, board_copy, "ZZ1", "ZZ2")
        assert "error" not in result, result
        assert result["success"] is False
        assert result["removed_count"] == 0
        assert result["not_found_count"] == 2
        assert result["backup_path"] is None
        assert {nf["reference"] for nf in result["not_found"]} == {"ZZ1", "ZZ2"}
        # Nothing written: bytes identical, no backup, footprints intact.
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")
        assert len(self._refs(board_copy)) == 4


class TestBatchPlacement:
    def test_batch_places_three_with_partial_failure(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,  # required by schema; unused because every item carries nets
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                     "nets": {"1": "BATCH_A", "2": "BATCH_A"}},
                    {"footprint": "NoSuchPart", "reference": "R10", "x": 30.0, "y": 20.0,
                     "nets": {"1": "BATCH_B", "2": "BATCH_B"}},
                    {"footprint": "R_0402_1005Metric", "reference": "R11", "x": 40.0, "y": 20.0,
                     "nets": {"1": "BATCH_A", "2": "BATCH_A"}},
                ],
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is False  # not all placed
        assert result["placed_count"] == 2
        assert result["failed_count"] == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["reference"] == "R10"
        assert "not found" in result["failed"][0]["error"]
        # Per-item results in order: ok, fail, ok.
        assert result["results"][0]["success"] is True
        assert result["results"][0]["reference"] == "R9"
        assert result["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": "BATCH_A"},
            {"pad": "2", "net": "BATCH_A"},
        ]
        assert result["results"][1]["success"] is False
        assert result["results"][1]["reference"] == "R10"
        assert "not found" in result["results"][1]["error"]
        assert result["results"][2]["success"] is True

        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_copy))]
        assert "R9" in refs and "R11" in refs
        assert "R10" not in refs  # failed item never landed
        nets = _nets(board_copy)
        assert nets.get("BATCH_A") is not None
        for pad in _pads(_fp_node(board_copy, "R9")) + _pads(_fp_node(board_copy, "R11")):
            assert _pad_net(pad) == (nets["BATCH_A"], "BATCH_A")

    def test_batch_all_ok(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R7", "x": 10.0, "y": 10.0,
                     "nets": {"1": "A", "2": "B"}},
                    {"footprint": "R_0402_1005Metric", "reference": "R8", "x": 12.0, "y": 10.0,
                     "rotation": 90.0, "nets": {"1": "C", "2": "D"}},
                ],
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert result["placed_count"] == 2
        assert result["failed_count"] == 0
        assert result["results"][1]["result"]["rotation"] == 90.0

    def test_batch_item_missing_pad_fails_that_item(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                     "nets": {"1": "A", "2": "A"}},
                    {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 30.0, "y": 20.0,
                     "nets": {"1": "B"}},  # covers pad 1 only -> item fails
                ],
                library=str(lib_dir),
            )
        )
        assert result["placed_count"] == 1
        assert result["failed_count"] == 1
        assert "missing net for pad(s): 2" in result["failed"][0]["error"]
        assert result["failed"][0]["reference"] == "R10"
        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_copy))]
        assert "R9" in refs and "R10" not in refs

    def test_batch_uses_tool_level_nets_as_item_default(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets={"1": "DEFAULT_N", "2": "DEFAULT_N"},
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0},
                    {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 30.0, "y": 20.0},
                ],
                library=str(lib_dir),
            )
        )
        assert result["success"] is True
        assert result["placed_count"] == 2
        nets = _nets(board_copy)
        for ref in ("R9", "R10"):
            for pad in _pads(_fp_node(board_copy, ref)):
                assert _pad_net(pad) == (nets["DEFAULT_N"], "DEFAULT_N")

    def test_batch_duplicate_reference_fails_that_item(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                     "nets": UNCONNECTED},
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 30.0, "y": 20.0,
                     "nets": UNCONNECTED},
                ],
                library=str(lib_dir),
            )
        )
        assert result["placed_count"] == 1
        assert result["failed_count"] == 1
        assert "already exists" in result["failed"][0]["error"]
        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_copy))]
        assert refs.count("R9") == 1

    def test_batch_missing_x_y_and_junk_item(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                     "nets": UNCONNECTED},
                    {"footprint": "R_0402_1005Metric", "reference": "R10", "nets": UNCONNECTED},  # missing x/y
                    "not-an-object",
                ],
                library=str(lib_dir),
            )
        )
        assert result["placed_count"] == 1
        assert result["failed_count"] == 2
        assert "x and y are required" in result["failed"][0]["error"]
        assert result["failed"][0]["reference"] == "R10"
        assert result["failed"][1]["reference"] == ""
        assert "must be an object" in result["failed"][1]["error"]

    def test_batch_and_single_args_conflict(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                footprints=[
                    {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 30.0, "y": 20.0,
                     "nets": UNCONNECTED}
                ],
                library=str(lib_dir),
            )
        )
        assert "error" in result
        assert "not both" in result["error"]
        assert _board_bytes(board_copy) == before


class TestValidation:
    def test_duplicate_reference_rejected(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R1",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" in result
        assert "already exists" in result["error"]
        # File untouched, no backup created.
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")

    def test_unknown_footprint_rejected(self, tools, board_copy, lib_dir):
        before = _board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="NoSuchPart",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" in result
        assert "not found" in result["error"]
        assert str(lib_dir) in result["error"]
        assert _board_bytes(board_copy) == before

    def test_unknown_library_rejected(self, tools, board_copy):
        before = _board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="NoSuchLib:R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
            )
        )
        assert "error" in result
        assert "NoSuchLib" in result["error"]
        assert "fp-lib-table" in result["error"]
        assert _board_bytes(board_copy) == before

    def test_unknown_library_argument_rejected(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library="NoSuchLib",
            )
        )
        assert "error" in result
        assert "NoSuchLib" in result["error"]

    def test_missing_pcb_file_rejected(self, tools, tmp_path, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=str(tmp_path / "missing.kicad_pcb"),
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" in result
        assert "cannot read board" in result["error"]

    def test_empty_reference_rejected(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="",
                x=20.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" in result

    def test_backup_created_on_success(self, tools, board_copy, lib_dir):
        assert not os.path.exists(board_copy + ".bak")
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=1.0,
                y=2.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert os.path.exists(board_copy + ".bak")
        # .bak holds the ORIGINAL board (pre-placement).
        assert "R9" not in open(board_copy + ".bak", encoding="utf-8").read()


class TestOutlineWarning:
    def test_outside_outline_warns(self, tools, outline_board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=outline_board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=5000.0,
                y=5000.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert "warnings" in result
        assert "outside the board outline" in result["warnings"][0]

    def test_inside_outline_no_warning(self, tools, outline_board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=outline_board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=25.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert "warnings" not in result

    def test_outline_less_board_no_warning(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                nets=UNCONNECTED,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=25.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert "warnings" not in result
