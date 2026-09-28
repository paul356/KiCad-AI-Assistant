"""Tests for the pure-batch ``add_footprints_to_pcb`` / ``remove_footprints_from_pcb`` MCP tools.

Covers: placing library footprints onto a board copy with correct
``(at ...)`` / reference / nets-netting, the §4 local→world rotation
transform, per-item required ``nets`` (missing pad -> hard error, ``""`` ->
net 0), net auto-add, every validation error branch (duplicate reference,
unknown footprint, unknown library prefix, missing board, unsafe name),
batch placement with partial failure, backup creation, and the outline
placement warning.  Also covers batch removal with partial not-found,
repeated references and the no-write guarantees.  No single-footprint call
form exists anywhere in these tests.
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


@pytest.fixture
def outline_board_with_table(tmp_path, lib_dir):
    dest = tmp_path / "outline.kicad_pcb"
    shutil.copy(OUTLINE_BOARD_FIXTURE, dest)
    (tmp_path / "fp-lib-table").write_text(FP_TABLE)
    return str(dest)


def _board_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _add(tools, pcb_path, footprints):
    """Call add_footprints_to_pcb (pure batch) and return the batch dict."""
    return _run(tools["add_footprints_to_pcb"](pcb_path=pcb_path, footprints=footprints))


def _add_one(tools, pcb_path, footprint, reference, x, y, nets=UNCONNECTED, rotation=0.0):
    """One-item batch; returns the batch dict (results[0] is that item)."""
    return _add(
        tools,
        pcb_path,
        [{"footprint": footprint, "reference": reference, "x": x, "y": y,
          "rotation": rotation, "nets": nets}],
    )


def _remove(tools, pcb_path, *references):
    """Call remove_footprints_from_pcb (pure batch) and return the dict."""
    return _run(
        tools["remove_footprints_from_pcb"](pcb_path=pcb_path, references=list(references))
    )


class TestPlacement:
    def test_places_bare_name_from_project_table(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 25.0
        )
        assert "error" not in res, res
        assert res["success"] is True
        result = res["results"][0]["result"]
        assert result["reference"] == "R9"
        assert result["placed_at"] == [20.0, 25.0]
        assert result["rotation"] == 0.0
        assert result["pad_count"] == 2
        assert result["pads_net"] == [{"pad": "1", "net": ""}, {"pad": "2", "net": ""}]
        assert os.path.isfile(result["backup_path"])

        node = _fp_node(board_with_table, "R9")
        assert node[1] == "R_0402_1005Metric"  # bare name -> board-created header
        at_node = [sub for sub in node if _sym(sub[0]) == "at"][0]
        assert (float(at_node[1]), float(at_node[2])) == (20.0, 25.0)
        for pad in _pads(node):
            assert _pad_net(pad) == (0, "")
        # Other footprints untouched, board still parses as a board.
        assert get_fp_property(_fp_node(board_with_table, "R1"), "Reference") == "R1"

    def test_places_lib_colon_name(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "TestLib:R_0402_1005Metric", "R7", 10.0, 10.0
        )
        assert "error" not in res, res
        assert res["success"] is True
        node = _fp_node(board_with_table, "R7")
        assert node[1] == "TestLib:R_0402_1005Metric"
        assert res["results"][0]["result"]["pad_count"] == 2

    def test_rotation_pad_world_transform(self, tools, board_with_table):
        # Footprint at (50, 40), rot 45°.  Pad local coords (-0.5, 0) /
        # (0.5, 0) must map through the §4 matrix to the expected world
        # positions; the stored pad (at ...) keeps LOCAL coordinates with the
        # absolute rotation field = footprint rotation (KiCad convention).
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 50.0, 40.0,
            nets={"1": "A", "2": "B"}, rotation=45.0,
        )
        assert "error" not in res, res
        assert res["success"] is True

        node = _fp_node(board_with_table, "R9")
        pads = _pads(node)
        assert len(pads) == 2
        # Pads keep local coordinates and gain the absolute rotation field.
        assert _pad_at(pads[0]) == (-0.5, 0.0, 45.0)
        assert _pad_at(pads[1]) == (0.5, 0.0, 45.0)
        # World positions match the §4 transform.
        for pad, (lx, ly) in zip(pads, [(-0.5, 0.0), (0.5, 0.0)]):
            wx, wy = _local_to_world(50.0, 40.0, 45.0, lx, ly)
            # Recover world from the stored (at ...) via the matrix.
            stored_x, stored_y, _ = _pad_at(pad)
            calc_x, calc_y = _local_to_world(50.0, 40.0, 45.0, stored_x, stored_y)
            assert calc_x == pytest.approx(wx, abs=1e-9)
            assert calc_y == pytest.approx(wy, abs=1e-9)

    def test_assigns_same_existing_net_to_all_pads(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "VCC", "2": "VCC"},
        )
        assert "error" not in res, res
        assert res["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": "VCC"},
            {"pad": "2", "net": "VCC"},
        ]
        for pad in _pads(_fp_node(board_with_table, "R9")):
            assert _pad_net(pad) == (1, "VCC")

    def test_auto_adds_missing_net(self, tools, board_with_table):
        assert "NEWNET" not in _nets(board_with_table)
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "NEWNET", "2": "NEWNET"},
        )
        assert "error" not in res, res
        # Fixture max net number is 3 → auto-add gets 4.
        nets = _nets(board_with_table)
        assert nets["NEWNET"] == 4
        for pad in _pads(_fp_node(board_with_table, "R9")):
            assert _pad_net(pad) == (4, "NEWNET")


class TestNets:
    def test_different_nets_per_pad(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "A", "2": "B"},
        )
        assert "error" not in res, res
        assert res["success"] is True
        result = res["results"][0]["result"]
        assert result["pad_count"] == 2
        assert result["pads_net"] == [{"pad": "1", "net": "A"}, {"pad": "2", "net": "B"}]

        nets = _nets(board_with_table)
        assert {"A", "B"} <= set(nets)
        pads = _pads(_fp_node(board_with_table, "R9"))
        assert len(pads) == 2
        pad1_net, pad2_net = _pad_net(pads[0]), _pad_net(pads[1])
        assert pad1_net == (nets["A"], "A")
        assert pad2_net == (nets["B"], "B")
        assert pad1_net[0] != pad2_net[0]

    def test_named_net_and_net_zero(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "A", "2": ""},
        )
        assert "error" not in res, res
        assert res["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": "A"},
            {"pad": "2", "net": ""},
        ]

        nets = _nets(board_with_table)
        pads = _pads(_fp_node(board_with_table, "R9"))
        assert _pad_net(pads[0]) == (nets["A"], "A")
        assert _pad_net(pads[1]) == (0, "")

    def test_all_net_zero_via_empty_strings(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0
        )
        assert "error" not in res, res
        assert res["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": ""},
            {"pad": "2", "net": ""},
        ]
        assert _nets(board_with_table).get("") == 0
        for pad in _pads(_fp_node(board_with_table, "R9")):
            assert _pad_net(pad) == (0, "")

    def test_none_net_value_means_net_zero(self, tools, board_with_table):
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "A", "2": None},
        )
        assert "error" not in res, res
        assert res["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": "A"},
            {"pad": "2", "net": ""},
        ]
        pads = _pads(_fp_node(board_with_table, "R9"))
        assert _pad_net(pads[1]) == (0, "")

    def test_missing_pad_net_is_hard_error_no_write(self, tools, board_with_table):
        before = _board_bytes(board_with_table)
        res = _add_one(
            tools, board_with_table, "R_0402_1005Metric", "R9", 20.0, 20.0,
            nets={"1": "A"},
        )
        assert res["success"] is False
        assert "missing net for pad(s): 2" in res["failed"][0]["error"]
        # Nothing written: file bytes identical, no backup, no footprint.
        assert _board_bytes(board_with_table) == before
        assert not os.path.exists(board_with_table + ".bak")
        data = load_pcb(board_with_table)
        assert all(
            get_fp_property(n, "Reference") != "R9" for n in iter_footprint_nodes(data)
        )


class TestRemoveFootprint:
    def _place(self, tools, board_path, ref, x, y, nets=UNCONNECTED):
        return _add_one(
            tools, board_path, "R_0402_1005Metric", ref, x, y, nets=nets
        )

    def _refs(self, board_path):
        data = load_pcb(board_path)
        return [get_fp_property(n, "Reference") for n in iter_footprint_nodes(data)]

    def test_removes_one_footprint_keeps_others_and_nets(self, tools, board_with_table):
        shared = {"1": "NEWNET", "2": "NEWNET"}
        assert self._place(tools, board_with_table, "R9", 20.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_with_table, "R10", 30.0, 20.0, nets=shared)["success"]

        result = _remove(tools, board_with_table, "R9")
        assert "error" not in result, result
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert result["not_found_count"] == 0
        assert result["results"][0] == {"reference": "R9", "success": True, "removed": 1}
        assert result["pcb_path"] == board_with_table
        assert os.path.isfile(result["backup_path"])

        refs = self._refs(board_with_table)
        assert "R9" not in refs
        assert "R10" in refs
        # Net definitions are kept; R10 still references NEWNET (net 4).
        nets = _nets(board_with_table)
        assert nets.get("NEWNET") == 4
        for pad in _pads(_fp_node(board_with_table, "R10")):
            assert _pad_net(pad) == (4, "NEWNET")

    def test_remove_again_returns_not_found_untouched(self, tools, board_with_table):
        assert self._place(tools, board_with_table, "R9", 20.0, 20.0)["success"]
        assert _remove(tools, board_with_table, "R9")["success"] is True
        before = _board_bytes(board_with_table)
        result = _remove(tools, board_with_table, "R9")
        assert result["success"] is False
        assert result["removed_count"] == 0
        assert result["results"][0]["success"] is False
        assert "not found" in result["results"][0]["error"]
        assert _board_bytes(board_with_table) == before

    def test_remove_unknown_reference_tolerated(self, tools, board_copy):
        before = _board_bytes(board_copy)
        result = _remove(tools, board_copy, "ZZ9")
        assert result["success"] is False
        assert result["removed_count"] == 0
        assert result["not_found_count"] == 1
        assert result["results"][0]["reference"] == "ZZ9"
        assert "not found" in result["results"][0]["error"]
        assert _board_bytes(board_copy) == before

    def test_remove_empty_references_is_error_no_write(self, tools, board_copy):
        before = _board_bytes(board_copy)
        result = _run(
            tools["remove_footprints_from_pcb"](pcb_path=board_copy, references=[])
        )
        assert "error" in result
        assert "non-empty" in result["error"]
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")

    def test_remove_bad_pcb_path(self, tools, tmp_path):
        result = _run(
            tools["remove_footprints_from_pcb"](
                pcb_path=str(tmp_path / "missing.kicad_pcb"), references=["R9"]
            )
        )
        assert "error" in result
        assert "cannot read board" in result["error"]

    def test_remove_reference_of_fixture_footprint(self, tools, board_copy):
        # The fixture board already carries C1/U1/D1/R1 footprints.
        result = _remove(tools, board_copy, "R1")
        assert result["success"] is True
        assert result["removed_count"] == 1
        refs = self._refs(board_copy)
        assert "R1" not in refs
        assert "U1" in refs


class TestBatchRemove:
    def _place(self, tools, board_path, ref, x, y, nets=UNCONNECTED):
        return _add_one(
            tools, board_path, "R_0402_1005Metric", ref, x, y, nets=nets
        )

    def _refs(self, board_path):
        data = load_pcb(board_path)
        return [get_fp_property(n, "Reference") for n in iter_footprint_nodes(data)]

    def test_batch_removes_three_with_one_missing(self, tools, board_with_table):
        shared = {"1": "NET_KEEP", "2": "NET_KEEP"}
        assert self._place(tools, board_with_table, "R9", 20.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_with_table, "R10", 30.0, 20.0, nets=shared)["success"]
        assert self._place(tools, board_with_table, "R11", 40.0, 20.0, nets=shared)["success"]

        result = _remove(tools, board_with_table, "R9", "R10", "ZZ9")
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
        assert result["pcb_path"] == board_with_table
        assert os.path.isfile(result["backup_path"])

        refs = self._refs(board_with_table)
        assert "R9" not in refs and "R10" not in refs
        assert "R11" in refs  # untouched batch survivor
        assert "C1" in refs and "U1" in refs and "D1" in refs and "R1" in refs
        # Nets of the removed footprints stay on the board (dangling nets).
        nets = _nets(board_with_table)
        assert nets.get("NET_KEEP") is not None
        for pad in _pads(_fp_node(board_with_table, "R11")):
            assert _pad_net(pad) == (nets["NET_KEEP"], "NET_KEEP")

    def test_batch_all_fixture_footprints_success(self, tools, board_copy):
        result = _remove(tools, board_copy, "R1", "C1", "U1")
        assert "error" not in result, result
        assert result["success"] is True
        assert result["removed_count"] == 3
        assert result["not_found_count"] == 0
        assert result["not_found"] == []
        assert os.path.isfile(result["backup_path"])
        refs = self._refs(board_copy)
        assert "R1" not in refs and "C1" not in refs and "U1" not in refs
        assert "D1" in refs

    def test_batch_duplicate_reference_second_is_not_found(self, tools, board_copy):
        result = _remove(tools, board_copy, "R1", "R1")
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

    def test_batch_empty_string_item_fails_only_that_item(self, tools, board_copy):
        result = _remove(tools, board_copy, "R1", "")
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
        result = _remove(tools, board_copy, "ZZ1", "ZZ2")
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
    def test_batch_places_three_with_partial_failure(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                 "nets": {"1": "BATCH_A", "2": "BATCH_A"}},
                {"footprint": "NoSuchPart", "reference": "R10", "x": 30.0, "y": 20.0,
                 "nets": {"1": "BATCH_B", "2": "BATCH_B"}},
                {"footprint": "R_0402_1005Metric", "reference": "R11", "x": 40.0, "y": 20.0,
                 "nets": {"1": "BATCH_A", "2": "BATCH_A"}},
            ],
        )
        assert "error" not in res, res
        assert res["success"] is False  # not all placed
        assert res["placed_count"] == 2
        assert res["failed_count"] == 1
        assert len(res["failed"]) == 1
        assert res["failed"][0]["reference"] == "R10"
        assert "not found" in res["failed"][0]["error"]
        # Per-item results in order: ok, fail, ok.
        assert res["results"][0]["success"] is True
        assert res["results"][0]["reference"] == "R9"
        assert res["results"][0]["result"]["pads_net"] == [
            {"pad": "1", "net": "BATCH_A"},
            {"pad": "2", "net": "BATCH_A"},
        ]
        assert res["results"][1]["success"] is False
        assert res["results"][1]["reference"] == "R10"
        assert "not found" in res["results"][1]["error"]
        assert res["results"][2]["success"] is True

        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_with_table))]
        assert "R9" in refs and "R11" in refs
        assert "R10" not in refs  # failed item never landed
        nets = _nets(board_with_table)
        assert nets.get("BATCH_A") is not None
        for pad in _pads(_fp_node(board_with_table, "R9")) + _pads(_fp_node(board_with_table, "R11")):
            assert _pad_net(pad) == (nets["BATCH_A"], "BATCH_A")

    def test_batch_all_ok(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R7", "x": 10.0, "y": 10.0,
                 "nets": {"1": "A", "2": "B"}},
                {"footprint": "R_0402_1005Metric", "reference": "R8", "x": 12.0, "y": 10.0,
                 "rotation": 90.0, "nets": {"1": "C", "2": "D"}},
            ],
        )
        assert "error" not in res, res
        assert res["success"] is True
        assert res["placed_count"] == 2
        assert res["failed_count"] == 0
        assert res["results"][1]["result"]["rotation"] == 90.0

    def test_batch_item_missing_pad_fails_that_item(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                 "nets": {"1": "A", "2": "A"}},
                {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 30.0, "y": 20.0,
                 "nets": {"1": "B"}},  # covers pad 1 only -> item fails
            ],
        )
        assert res["placed_count"] == 1
        assert res["failed_count"] == 1
        assert "missing net for pad(s): 2" in res["failed"][0]["error"]
        assert res["failed"][0]["reference"] == "R10"
        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_with_table))]
        assert "R9" in refs and "R10" not in refs

    def test_batch_item_missing_nets_fails_that_item(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                 "nets": {"1": "A", "2": "A"}},
                {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 30.0, "y": 20.0},
            ],
        )
        assert res["placed_count"] == 1
        assert res["failed_count"] == 1
        assert "nets must be an object" in res["failed"][0]["error"]
        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_with_table))]
        assert "R9" in refs and "R10" not in refs

    def test_batch_duplicate_reference_fails_that_item(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                 "nets": UNCONNECTED},
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 30.0, "y": 20.0,
                 "nets": UNCONNECTED},
            ],
        )
        assert res["placed_count"] == 1
        assert res["failed_count"] == 1
        assert "already exists" in res["failed"][0]["error"]
        refs = [get_fp_property(n, "Reference") for n in iter_footprint_nodes(load_pcb(board_with_table))]
        assert refs.count("R9") == 1

    def test_batch_missing_x_y_and_junk_item(self, tools, board_with_table):
        res = _add(
            tools,
            board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
                 "nets": UNCONNECTED},
                {"footprint": "R_0402_1005Metric", "reference": "R10", "nets": UNCONNECTED},  # missing x/y
                "not-an-object",
            ],
        )
        assert res["placed_count"] == 1
        assert res["failed_count"] == 2
        assert "x and y are required" in res["failed"][0]["error"]
        assert res["failed"][0]["reference"] == "R10"
        assert res["failed"][1]["reference"] == ""
        assert "must be an object" in res["failed"][1]["error"]

    def test_batch_new_net_shared_across_items(self, tools, board_with_table):
        # Auto-added nets are resolved against the live board, so a net
        # created by the first item is reused (same number) by the second.
        items = []
        for i, ref in enumerate(("R9", "R10")):
            items.append(
                {"footprint": "R_0402_1005Metric", "reference": ref, "x": 20.0 + 10.0 * i,
                 "y": 20.0, "nets": {"1": "SHARED_NEW", "2": "SHARED_NEW"}}
            )
        res = _add(tools, board_with_table, items)
        assert "error" not in res, res
        assert res["success"] is True
        nets = _nets(board_with_table)
        assert nets.get("SHARED_NEW") is not None
        for ref in ("R9", "R10"):
            for pad in _pads(_fp_node(board_with_table, ref)):
                assert _pad_net(pad) == (nets["SHARED_NEW"], "SHARED_NEW")

    def test_batch_outline_warning_only_on_outside_item(self, tools, outline_board_with_table):
        res = _add(
            tools,
            outline_board_with_table,
            [
                {"footprint": "R_0402_1005Metric", "reference": "R9", "x": 25.0, "y": 20.0,
                 "nets": UNCONNECTED},
                {"footprint": "R_0402_1005Metric", "reference": "R10", "x": 5000.0, "y": 5000.0,
                 "nets": UNCONNECTED},
            ],
        )
        assert "error" not in res, res
        assert res["success"] is True
        assert "warnings" not in res["results"][0]["result"]
        warnings = res["results"][1]["result"].get("warnings") or []
        assert "outside the board outline" in warnings[0]

    def test_batch_item_library_key_is_ignored(self, tools, board_with_table):
        # The per-item library restriction was removed; a stray key does not
        # break placement and the footprint resolves through the fp-lib-table.
        res = _add(
            tools,
            board_with_table,
            [{"footprint": "R_0402_1005Metric", "reference": "R9", "x": 20.0, "y": 20.0,
              "nets": UNCONNECTED, "library": "Whatever"}],
        )
        assert "error" not in res, res
        assert res["success"] is True
        assert _fp_node(board_with_table, "R9")[1] == "R_0402_1005Metric"


class TestValidation:
    def test_duplicate_reference_rejected(self, tools, board_copy):
        before = _board_bytes(board_copy)
        res = _add_one(tools, board_copy, "R_0402_1005Metric", "R1", 20.0, 20.0)
        assert res["success"] is False
        assert "already exists" in res["failed"][0]["error"]
        # File untouched, no backup created.
        assert _board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")

    def test_unknown_footprint_rejected(self, tools, board_with_table):
        before = _board_bytes(board_with_table)
        res = _add_one(tools, board_with_table, "NoSuchPart", "R9", 20.0, 20.0)
        assert res["success"] is False
        assert "not found" in res["failed"][0]["error"]
        assert "NoSuchPart" in res["failed"][0]["error"]
        assert _board_bytes(board_with_table) == before

    def test_unknown_library_prefix_rejected(self, tools, board_copy):
        before = _board_bytes(board_copy)
        res = _add_one(tools, board_copy, "NoSuchLib:R_0402_1005Metric", "R9", 20.0, 20.0)
        assert res["success"] is False
        assert "NoSuchLib" in res["failed"][0]["error"]
        assert "fp-lib-table" in res["failed"][0]["error"]
        assert _board_bytes(board_copy) == before

    def test_missing_pcb_file_rejected(self, tools, tmp_path):
        res = _add_one(tools, str(tmp_path / "missing.kicad_pcb"), "R_0402_1005Metric", "R9", 20.0, 20.0)
        assert res["success"] is False
        assert "cannot read board" in res["failed"][0]["error"]

    def test_empty_reference_rejected(self, tools, board_copy):
        res = _add_one(tools, board_copy, "R_0402_1005Metric", "", 20.0, 20.0)
        assert res["success"] is False
        assert "non-empty" in res["failed"][0]["error"]

    def test_empty_footprints_list_rejected(self, tools, board_copy):
        before = _board_bytes(board_copy)
        res = _run(tools["add_footprints_to_pcb"](pcb_path=board_copy, footprints=[]))
        assert "error" in res
        assert "non-empty" in res["error"]
        assert _board_bytes(board_copy) == before

    def test_backup_created_on_success(self, tools, board_with_table):
        assert not os.path.exists(board_with_table + ".bak")
        res = _add_one(tools, board_with_table, "R_0402_1005Metric", "R9", 1.0, 2.0)
        assert "error" not in res, res
        assert res["success"] is True
        assert os.path.exists(board_with_table + ".bak")
        # .bak holds the ORIGINAL board (pre-placement).
        assert "R9" not in open(board_with_table + ".bak", encoding="utf-8").read()


class TestOutlineWarning:
    def test_outside_outline_warns(self, tools, outline_board_with_table):
        res = _add_one(tools, outline_board_with_table, "R_0402_1005Metric", "R9", 5000.0, 5000.0)
        assert "error" not in res, res
        assert res["success"] is True
        warnings = res["results"][0]["result"].get("warnings") or []
        assert "outside the board outline" in warnings[0]

    def test_inside_outline_no_warning(self, tools, outline_board_with_table):
        res = _add_one(tools, outline_board_with_table, "R_0402_1005Metric", "R9", 25.0, 20.0)
        assert "error" not in res, res
        assert res["success"] is True
        assert "warnings" not in res["results"][0]["result"]

    def test_outline_less_board_no_warning(self, tools, board_with_table):
        res = _add_one(tools, board_with_table, "R_0402_1005Metric", "R9", 25.0, 20.0)
        assert "error" not in res, res
        assert res["success"] is True
        assert "warnings" not in res["results"][0]["result"]
