"""Tests for the ``add_footprint_to_pcb`` MCP tool.

Covers: placing a library footprint onto a board copy with correct
``(at ...)`` / reference / pads-netting, the §4 local→world rotation
transform, net auto-add, every validation error branch (duplicate
reference, unknown footprint, unknown library, missing board, unsafe
name), backup creation, and the outline placement warning.
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


class TestPlacement:
    def test_places_bare_name_from_library_dir(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
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
        assert result["net"] == ""
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
                footprint="R_0402_1005Metric",
                reference="R9",
                x=50.0,
                y=40.0,
                rotation=45.0,
                net=None,
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

    def test_assigns_existing_net(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                net="VCC",
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["net"] == "VCC"
        checkout = _fp_node(board_copy, "R9")
        for pad in _pads(checkout):
            assert _pad_net(pad) == (1, "VCC")

    def test_auto_adds_missing_net(self, tools, board_copy, lib_dir):
        assert "NEWNET" not in _nets(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                net="NEWNET",
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        # Fixture max net number is 3 → auto-add gets 4.
        nets = _nets(board_copy)
        assert nets["NEWNET"] == 4
        for pad in _pads(_fp_node(board_copy, "R9")):
            assert _pad_net(pad) == (4, "NEWNET")


class TestPerPadNets:
    def test_per_pad_different_nets(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                pads={"1": "A", "2": "B"},
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["success"] is True
        assert result["pad_count"] == 2
        # Fallback net is None -> net 0; per-pad nets win.
        assert result["net"] == ""
        assert result["pads_net"] == [{"pad": "1", "net": "A"}, {"pad": "2", "net": "B"}]

        nets = _nets(board_copy)
        assert {"A", "B"} <= set(nets)
        pads = _pads(_fp_node(board_copy, "R9"))
        assert len(pads) == 2
        pad1_net, pad2_net = _pad_net(pads[0]), _pad_net(pads[1])
        assert pad1_net == (nets["A"], "A")
        assert pad2_net == (nets["B"], "B")
        assert pad1_net[0] != pad2_net[0]

    def test_pads_partial_with_net_fallback(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                pads={"1": "A"},
                net="Z",
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert result["net"] == "Z"
        assert result["pads_net"] == [{"pad": "1", "net": "A"}, {"pad": "2", "net": "Z"}]

        nets = _nets(board_copy)
        pads = _pads(_fp_node(board_copy, "R9"))
        assert _pad_net(pads[0]) == (nets["A"], "A")
        assert _pad_net(pads[1]) == (nets["Z"], "Z")
        # Z auto-added after A -> distinct numbers.
        assert nets["A"] != nets["Z"]

    def test_both_none_is_net_zero(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
                net=None,
                pads=None,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert "pads_net" not in result
        for pad in _pads(_fp_node(board_copy, "R9")):
            assert _pad_net(pad) == (0, "")


class TestValidation:
    def _board_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_duplicate_reference_rejected(self, tools, board_copy, lib_dir):
        before = self._board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
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
        assert self._board_bytes(board_copy) == before
        assert not os.path.exists(board_copy + ".bak")

    def test_unknown_footprint_rejected(self, tools, board_copy, lib_dir):
        before = self._board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
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
        assert self._board_bytes(board_copy) == before

    def test_unknown_library_rejected(self, tools, board_copy):
        before = self._board_bytes(board_copy)
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
                footprint="NoSuchLib:R_0402_1005Metric",
                reference="R9",
                x=20.0,
                y=20.0,
            )
        )
        assert "error" in result
        assert "NoSuchLib" in result["error"]
        assert "fp-lib-table" in result["error"]
        assert self._board_bytes(board_copy) == before

    def test_unknown_library_argument_rejected(self, tools, board_copy, lib_dir):
        result = _run(
            tools["add_footprint_to_pcb"](
                pcb_path=board_copy,
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
                footprint="R_0402_1005Metric",
                reference="R9",
                x=25.0,
                y=20.0,
                library=str(lib_dir),
            )
        )
        assert "error" not in result, result
        assert "warnings" not in result
