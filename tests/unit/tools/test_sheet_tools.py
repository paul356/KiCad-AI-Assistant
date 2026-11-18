"""Unit tests for sheet_tools.py."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
import sexpdata

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures/tools_test.kicad_sch")

# Fixed UUIDs keep the generated fixtures readable in assertions.
ROOT_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHILD_UUID = "cccccccc-dddd-eeee-ffff-000000000000"
PROJECT_UUID = "99999999-8888-7777-6666-555555555555"
OTHER_UUID = "11111111-2222-3333-4444-555555555555"
LEGACY_SHEET_UUID = "dddddddd-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------


def _load(path: str) -> list:
    """Parse a schematic into its raw sexpdata tree."""
    with open(path, encoding="utf-8") as fh:
        return sexpdata.loads(fh.read())


def _tag(node) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return node[0].value()
    return None


def _children(node: list, tag: str) -> list:
    return [child for child in node if _tag(child) == tag]


def _child(node: list, tag: str) -> list:
    matches = _children(node, tag)
    assert matches, f"missing ({tag} ...) in {sexpdata.dumps(node)}"
    return matches[0]


def _property_key(name) -> str:
    return name.replace(" ", "").replace("_", "").lower() if isinstance(name, str) else ""


def _sheet_node(tree: list, sheet_uuid: str) -> list:
    for node in tree:
        if _tag(node) != "sheet":
            continue
        uuid_node = _child(node, "uuid")
        if uuid_node[1] == sheet_uuid:
            return node
    raise AssertionError(f"sheet {sheet_uuid} not found")


def _instance_path(sheet: list) -> str:
    project = _child(_child(sheet, "instances"), "project")
    return _child(project, "path")[1]


def _instance_page(sheet: list) -> str:
    project = _child(_child(sheet, "instances"), "project")
    return _child(_child(project, "path"), "page")[1]


def _write_schematic(
    path,
    *,
    root_uuid: str | None = ROOT_UUID,
    instance_path: str = "/parent",
    pages: tuple[int, ...] = (),
    child_file: str = "child.kicad_sch",
    is_root: bool = True,
) -> None:
    """Write a minimal schematic whose sheets occupy *pages*.

    Each sheet references *child_file*; the parent path recorded in every
    ``instances`` block is *instance_path*.
    """
    parts = ['(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")\n']
    if root_uuid:
        parts.append(f'  (uuid "{root_uuid}")\n')
    parts.append('  (paper "A2")\n  (lib_symbols)\n')
    for index, page in enumerate(pages, start=1):
        parts.append(
            f"  (sheet (at {25.4 + index * 63.5} 25.4) (size 50.8 25.4)\n"
            "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
            "    (stroke (width 0) (type solid))\n"
            "    (fill (color 0 0 0 0))\n"
            f'    (uuid "00000000-0000-0000-0000-{index:012d}")\n'
            f'    (property "Sheetname" "S{index}" (at 0 0 0) (show_name no)'
            " (do_not_autoplace no) (effects (font (size 1.27 1.27))))\n"
            f'    (property "Sheetfile" "{child_file}" (at 0 0 0) (show_name no)'
            " (do_not_autoplace no) (effects (font (size 1.27 1.27))))\n"
            f'    (instances (project "project" (path "{instance_path}"'
            f' (page "{page}"))))\n'
            "  )\n"
        )
    if is_root:
        parts.append('  (sheet_instances (path "/" (page "1")))\n')
        parts.append("  (embedded_fonts no)\n")
    parts.append(")\n")
    Path(path).write_text("".join(parts), encoding="utf-8")


def _write_project(path, entries) -> None:
    """Write a project whose ``top_level_sheets`` are *entries*."""
    Path(path).write_text(
        json.dumps({"schematic": {"top_level_sheets": entries}}), encoding="utf-8"
    )


def _snapshot(path) -> tuple[str, int]:
    """Return the content hash and mtime of *path*."""
    stat = os.stat(path)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest(), stat.st_mtime_ns


def _assert_nothing_written(before, schematic_path, child_path) -> None:
    """Assert *schematic_path* is byte-identical and no new files appeared."""
    before_hash, before_mtime = before
    after_hash, after_mtime = _snapshot(schematic_path)
    assert (after_hash, after_mtime) == (before_hash, before_mtime)
    assert not Path(str(schematic_path) + ".bak").exists()
    assert not Path(child_path).exists()


def _add_sheet(tools: dict, schematic_path: str, **kwargs):
    params = {
        "schematic_path": schematic_path,
        "sheet_name": "New Sheet",
        "sheet_file": "new_sheet.kicad_sch",
        "x": 350.0,
        "y": 300.0,
        "width": 50.8,
        "height": 25.4,
    }
    params.update(kwargs)
    return asyncio.run(tools["add_sheet_symbol"](**params))


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.sheet_tools import register_sheet_tools

    mock = _MockMCP()
    register_sheet_tools(mock)
    return mock.tools


def _make_temp_copy() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False)
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


@pytest.fixture(autouse=True)
def _scoped_project_lookup(isolated_project_search):
    """Keep the upward project walk inside each test's tmp_path tree."""
    return isolated_project_search


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    path = _make_temp_copy()
    yield path
    for candidate in (path, path + ".bak"):
        if os.path.exists(candidate):
            os.unlink(candidate)


class TestAddSheetSymbol:
    def test_auto_place_uses_nearest_free_area(self, tools, tmp_sch):
        from kcaa.tools.placement_helpers import PlacementHelpers
        from kcaa.tools.sheet_tools import _align_to_grid

        requested_x = 100.0
        requested_y = 100.0
        width = 50.8
        height = 50.8
        snapped_x = _align_to_grid(requested_x)
        snapped_y = _align_to_grid(requested_y)
        candidate = PlacementHelpers.find_free_area(
            schematic_path=tmp_sch,
            width=_align_to_grid(width),
            height=_align_to_grid(height),
            prefer_near={"x": snapped_x, "y": snapped_y},
            max_candidates=1,
        )["candidates"][0]["origin"]
        assert (candidate["x"], candidate["y"]) != (snapped_x, snapped_y)

        result = asyncio.run(
            tools["add_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_name="Auto Sheet",
                sheet_file="auto_sheet.kicad_sch",
                x=requested_x,
                y=requested_y,
                width=width,
                height=height,
            )
        )

        assert result.get("success") is True, result
        assert result["position_adjusted"] is True
        assert result["requested_position"] == {"x": requested_x, "y": requested_y}
        assert result["position"]["x"] == pytest.approx(candidate["x"])
        assert result["position"]["y"] == pytest.approx(candidate["y"])
        assert result["note"] == "Position adjusted to nearest free area."

    def test_instances_use_owning_project_name(self, tools, tmp_path, isolated_project_search):
        """A nested sheet records the root project's name, not its own stem."""
        hardware = tmp_path / "hardware"
        hardware.mkdir()
        schematic = hardware / "main_bus.kicad_sch"
        shutil.copy(SCHEMATIC_PATH, schematic)
        _write_project(
            tmp_path / "aria.kicad_pro",
            [{"filename": "hardware/main_bus.kicad_sch", "uuid": PROJECT_UUID}],
        )

        result = asyncio.run(
            tools["add_sheet_symbol"](
                schematic_path=str(schematic),
                sheet_name="Child",
                sheet_file="child.kicad_sch",
                x=20,
                y=20,
                width=25.4,
                height=25.4,
            )
        )

        assert result["success"] is True, result
        text = schematic.read_text()
        assert '(project "aria"' in text
        assert '(project "main_bus"' not in text


class TestUpdateSheetSymbol:
    """Tests for update_sheet_symbol auto-placement."""

    def _add_sheet(self, tools, sch_path: str, name: str, x: float, y: float) -> str:
        """Add a sheet and return its UUID."""
        result = asyncio.run(
            tools["add_sheet_symbol"](
                schematic_path=sch_path,
                sheet_name=name,
                sheet_file=f"{name}.kicad_sch",
                x=x,
                y=y,
                width=50.8,
                height=50.8,
            )
        )
        assert result.get("success") is True, result
        return result["sheet_uuid"]

    def test_auto_place_true_avoids_overlap(self, tools, tmp_sch):
        """Moving a sheet to an occupied spot should be auto-adjusted."""
        from kcaa.tools.sheet_tools import _align_to_grid

        # Place a blocker sheet at (20, 20).
        self._add_sheet(tools, tmp_sch, "Blocker", 20.0, 20.0)
        # Place the target sheet at (200, 200) — far from blocker.
        target_uuid = self._add_sheet(tools, tmp_sch, "Target", 200.0, 200.0)

        # Try to move Target onto the blocker (20, 20) — should auto-adjust.
        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=20.0,
                y=20.0,
            )
        )
        assert result.get("success") is True, result
        assert result["position_adjusted"] is True
        assert result["requested_position"] == {"x": 20.0, "y": 20.0}
        assert result["note"] == "Position adjusted to nearest free area."
        # The sheet should NOT be at (20, 20).
        actual_pos = result.get("position") or {}
        if actual_pos:
            assert not (
                pytest.approx(actual_pos.get("x")) == _align_to_grid(20.0)
                and pytest.approx(actual_pos.get("y")) == _align_to_grid(20.0)
            )

    def test_no_conflict_keeps_exact_position(self, tools, tmp_sch):
        """When the requested position is free, it is used as-is."""

        # Place one sheet far away — no conflict with target position.
        self._add_sheet(tools, tmp_sch, "Obstacle2", 30.0, 30.0)
        target_uuid = self._add_sheet(tools, tmp_sch, "Target2", 250.0, 250.0)

        # Move to a free position (200, 10.16) — far from obstacle and components.
        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=200.0,
                y=10.16,
            )
        )
        assert result.get("success") is True, result
        # No conflict → no adjustment.
        assert result.get("position_adjusted") is False

    def test_size_only_change_skips_auto_place(self, tools, tmp_sch):
        """Resizing without moving should never trigger auto-placement."""
        target_uuid = self._add_sheet(tools, tmp_sch, "ResizeOnly", 150.0, 150.0)

        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                width=76.2,
                height=76.2,
            )
        )
        assert result.get("success") is True, result
        # position_adjusted not set for size-only changes.
        assert "position_adjusted" not in result

    def test_partial_move_x_only(self, tools, tmp_sch):
        """x-only move should not lose the y coordinate to bias."""
        target_uuid = self._add_sheet(tools, tmp_sch, "XOnly", 180.0, 180.0)

        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=target_uuid,
                x=185.0,
            )
        )
        assert result.get("success") is True, result
        # Should not error out; position_adjusted may be True or False.
        assert "error" not in result


@pytest.mark.usefixtures("isolated_project_search")
class TestCanonicalSheetData:
    """The written sheet node matches what KiCad 10 itself writes."""

    def test_written_sheet_node_is_canonical(self, tools, tmp_sch):
        root_uuid = _child(_load(tmp_sch), "uuid")[1]
        result = _add_sheet(tools, tmp_sch, x=200.0, y=25.4)
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(tmp_sch), result["sheet_uuid"])

        # D1: the instance path is the parent (root) sheet path, not the
        # new sheet's own UUID.
        assert _instance_path(sheet) == f"/{root_uuid}"
        # D2: the fixture already uses page 1 for the root sheet.
        assert int(_instance_page(sheet)) == 2

        # D3-D6: node order, flags, stroke and fill as KiCad 10 writes them.
        assert [_tag(child) for child in sheet[1:]] == [
            "at",
            "size",
            "exclude_from_sim",
            "in_bom",
            "on_board",
            "dnp",
            "stroke",
            "fill",
            "uuid",
            "property",
            "property",
            "instances",
        ]
        assert [prop[1] for prop in _children(sheet, "property")] == ["Sheetname", "Sheetfile"]
        assert {
            _tag(child): child[1].value()
            for child in sheet
            if _tag(child) in {"exclude_from_sim", "in_bom", "on_board", "dnp"}
        } == {"exclude_from_sim": "no", "in_bom": "yes", "on_board": "yes", "dnp": "no"}
        assert _child(_child(sheet, "stroke"), "type")[1].value() == "solid"
        assert _child(_child(sheet, "fill"), "color")[1:] == [0, 0, 0, 0]

        # The project name stays the schematic file stem.
        instances = _child(sheet, "instances")
        assert _child(instances, "project")[1] == Path(tmp_sch).stem

    def test_canonical_sheet_can_be_renamed(self, tools, tmp_sch):
        result = _add_sheet(tools, tmp_sch, x=200.0, y=25.4)
        assert result.get("success") is True, result

        update = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=tmp_sch,
                sheet_identifier=result["sheet_uuid"],
                sheet_name="Renamed",
            )
        )
        assert update["success"] is True, update

        properties = {
            _property_key(prop[1]): prop
            for prop in _children(_sheet_node(_load(tmp_sch), result["sheet_uuid"]), "property")
        }
        assert properties["sheetname"][1] == "Sheetname"
        assert properties["sheetname"][2] == "Renamed"


class TestPageAllocation:
    """D2: the page is the lowest positive integer unused in the project."""

    @pytest.mark.parametrize(
        ("pages", "expected"),
        [
            ((1, 2, 4), 3),
            ((1, 2, 3, 4), 5),
            ((1, 3), 2),
            ((), 2),
        ],
    )
    def test_allocates_lowest_unused_page(self, tools, tmp_path, pages, expected):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=pages)

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert int(_instance_page(sheet)) == expected

    def test_consecutive_inserts_get_distinct_pages(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID)

        first = _add_sheet(tools, str(root), y=300.0)
        second = _add_sheet(tools, str(root), y=350.0, sheet_name="Second Sheet")
        assert first["success"] is True, first
        assert second["success"] is True, second

        tree = _load(str(root))
        pages = [
            int(_instance_page(_sheet_node(tree, result["sheet_uuid"])))
            for result in (first, second)
        ]
        assert pages == [2, 3]

    def test_pages_used_by_child_schematics_are_counted(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        child = tmp_path / "child.kicad_sch"
        _write_schematic(
            child,
            root_uuid=CHILD_UUID,
            pages=(3,),
            child_file="leaf.kicad_sch",
            is_root=False,
        )
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,), child_file="child.kicad_sch")

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert int(_instance_page(sheet)) == 4

    def test_malformed_child_sheet_does_not_abort_page_collection(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        leaf = tmp_path / "leaf.kicad_sch"
        _write_schematic(leaf, root_uuid=CHILD_UUID, pages=(4,), is_root=False)
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2, 3), child_file="leaf.kicad_sch")

        (tmp_path / "broken.kicad_sch").write_text(
            "(kicad_sch (version 20260306)\n", encoding="utf-8"
        )
        text = root.read_text(encoding="utf-8")
        root.write_text(text.replace('"leaf.kicad_sch"', '"broken.kicad_sch"', 1), encoding="utf-8")

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        # Pages 2 and 3 belong to the root sheets, page 4 to the readable
        # sibling; the malformed child must not hide either.
        assert int(_instance_page(sheet)) == 5

    def test_deeply_nested_child_sheet_does_not_abort_page_collection(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,), child_file="deep.kicad_sch")
        (tmp_path / "deep.kicad_sch").write_text("(a " * 20_000 + ")" * 20_000, encoding="utf-8")

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert int(_instance_page(sheet)) == 3


class TestInstancePathResolution:
    """D1: resolve the parent path from real project data, never guess it."""

    def test_project_root_sheet_uuid_wins_over_file_uuid(self, tools, tmp_path):
        # KiCad keys instance paths by the project's top_level_sheets UUID,
        # which may differ from the schematic file's own (uuid ...).
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, instance_path="/stale", pages=(2,))
        (tmp_path / "root.kicad_pro").write_text(
            json.dumps(
                {
                    "schematic": {
                        "top_level_sheets": [
                            {"filename": "root.kicad_sch", "name": "Top", "uuid": PROJECT_UUID}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{PROJECT_UUID}"

    def test_project_not_listing_schematic_is_an_error(self, tools, tmp_path):
        # A project file exists, so the schematic is not standalone.  If the
        # project does not list this exact file its root UUID is unknown, and
        # falling back to the file UUID is the silent guess from #324.
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, instance_path="/stale", pages=(2,))
        _write_project(
            tmp_path / "root.kicad_pro",
            [{"filename": "other.kicad_sch", "name": "Top", "uuid": PROJECT_UUID}],
        )
        before = _snapshot(root)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "does not list this schematic" in result["error"]
        _assert_nothing_written(before, root, tmp_path / "orphan.kicad_sch")

    def test_renamed_copy_is_not_attributed_to_the_project(self, tools, tmp_path):
        # The #324 reproduction: the copy sits next to the project, which
        # lists only the original file.
        original = tmp_path / "two_ax_PCB.kicad_sch"
        _write_schematic(original, root_uuid=ROOT_UUID, pages=(2,))
        copy = tmp_path / "two_ax_PCB (Copy).kicad_sch"
        _write_schematic(copy, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(
            tmp_path / "two_ax_PCB.kicad_pro",
            [{"filename": "two_ax_PCB.kicad_sch", "name": "Top", "uuid": PROJECT_UUID}],
        )
        before = _snapshot(copy)

        result = _add_sheet(tools, str(copy))

        assert "error" in result
        _assert_nothing_written(before, copy, tmp_path / "new_sheet.kicad_sch")

    def test_file_uuid_used_when_schematic_is_standalone(self, tools, tmp_path):
        # No .kicad_pro anywhere in the tree (the module fixture confines the
        # upward walk to tmp_path): the file's own UUID keys its instances.
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, instance_path="/stale", pages=(2,))
        assert not list(tmp_path.glob("*.kicad_pro"))

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{ROOT_UUID}"

    def test_matching_entry_need_not_be_first(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(
            tmp_path / "root.kicad_pro",
            [
                {"filename": "other.kicad_sch", "name": "Other", "uuid": OTHER_UUID},
                {"filename": "root.kicad_sch", "name": "Top", "uuid": PROJECT_UUID},
            ],
        )

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{PROJECT_UUID}"

    def test_project_relative_filename_in_subdirectory(self, tools, tmp_path):
        # The project lives in an ancestor directory and records the path
        # relative to itself, not to the schematic.
        hardware = tmp_path / "hardware"
        hardware.mkdir()
        root = hardware / "main.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(
            tmp_path / "board.kicad_pro",
            [{"filename": "hardware/main.kicad_sch", "name": "Main", "uuid": PROJECT_UUID}],
        )

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{PROJECT_UUID}"

    def test_identical_basenames_resolve_independently(self, tools, tmp_path):
        hardware = tmp_path / "hardware"
        scratch = tmp_path / "scratch"
        hardware.mkdir()
        scratch.mkdir()
        main_hw = hardware / "main.kicad_sch"
        main_scratch = scratch / "main.kicad_sch"
        _write_schematic(main_hw, root_uuid=ROOT_UUID, pages=(2,))
        _write_schematic(main_scratch, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(
            tmp_path / "board.kicad_pro",
            [
                {"filename": "hardware/main.kicad_sch", "uuid": PROJECT_UUID},
                {"filename": "scratch/main.kicad_sch", "uuid": OTHER_UUID},
            ],
        )

        hw_result = _add_sheet(tools, str(main_hw))
        scratch_result = _add_sheet(tools, str(main_scratch))
        assert hw_result.get("success") is True, hw_result
        assert scratch_result.get("success") is True, scratch_result

        hw_sheet = _sheet_node(_load(str(main_hw)), hw_result["sheet_uuid"])
        scratch_sheet = _sheet_node(_load(str(main_scratch)), scratch_result["sheet_uuid"])
        assert _instance_path(hw_sheet) == f"/{PROJECT_UUID}"
        assert _instance_path(scratch_sheet) == f"/{OTHER_UUID}"

    def test_duplicate_entries_with_different_uuids_are_an_error(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(
            tmp_path / "root.kicad_pro",
            [
                {"filename": "root.kicad_sch", "uuid": PROJECT_UUID},
                {"filename": "./root.kicad_sch", "uuid": OTHER_UUID},
            ],
        )
        before = _snapshot(root)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "more than one distinct" in result["error"]
        _assert_nothing_written(before, root, tmp_path / "orphan.kicad_sch")

    def test_malformed_project_json_is_an_error(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        (tmp_path / "root.kicad_pro").write_text("{not json", encoding="utf-8")
        before = _snapshot(root)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "could not be read or is not valid JSON" in result["error"]
        _assert_nothing_written(before, root, tmp_path / "orphan.kicad_sch")

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"schematic": []},
            {"schematic": {}},
            {"schematic": {"top_level_sheets": []}},
            {"schematic": {"top_level_sheets": {}}},
        ],
        ids=["no-data", "schematic-not-object", "no-sheets", "empty-sheets", "sheets-not-list"],
    )
    def test_project_without_top_level_sheets_is_an_error(self, tools, tmp_path, payload):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        (tmp_path / "root.kicad_pro").write_text(json.dumps(payload), encoding="utf-8")
        before = _snapshot(root)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "Cannot resolve the root sheet UUID" in result["error"]
        _assert_nothing_written(before, root, tmp_path / "orphan.kicad_sch")

    @pytest.mark.parametrize(
        "entry",
        [
            {"filename": "root.kicad_sch", "name": "Top"},
            {"filename": "root.kicad_sch", "name": "Top", "uuid": ""},
            {"filename": "root.kicad_sch", "name": "Top", "uuid": "   "},
        ],
        ids=["absent", "empty", "whitespace"],
    )
    def test_matching_entry_without_uuid_is_an_error(self, tools, tmp_path, entry):
        # A blank UUID must fail closed, never reach the file-UUID fallback
        # (empty) or be written verbatim (whitespace).
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))
        _write_project(tmp_path / "root.kicad_pro", [entry])
        before = _snapshot(root)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "no string 'uuid'" in result["error"]
        _assert_nothing_written(before, root, tmp_path / "orphan.kicad_sch")

    def test_existing_sheet_instance_path_is_the_fallback(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, instance_path="/existing-root", pages=(2,))

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == "/existing-root"

    def test_root_without_uuid_or_instances_is_rejected(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, pages=())
        (tmp_path / "root.kicad_pro").write_text(
            json.dumps({"sheets": [[PROJECT_UUID, "root"]]}), encoding="utf-8"
        )
        before = root.read_text(encoding="utf-8")

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "Cannot resolve the root sheet UUID" in result["error"]
        assert root.read_text(encoding="utf-8") == before
        assert not (tmp_path / "root.kicad_sch.bak").exists()
        assert not (tmp_path / "orphan.kicad_sch").exists()

    def test_unresolvable_parent_path_fails_without_writing(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, pages=())
        before = root.read_text(encoding="utf-8")

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert root.read_text(encoding="utf-8") == before
        assert not (tmp_path / "root.kicad_sch.bak").exists()
        assert not (tmp_path / "orphan.kicad_sch").exists()

    def test_insert_into_child_schematic_is_rejected(self, tools, tmp_path):
        child = tmp_path / "child.kicad_sch"
        _write_schematic(child, root_uuid=CHILD_UUID, is_root=False)
        before = child.read_text(encoding="utf-8")

        result = _add_sheet(tools, str(child))

        assert "error" in result
        assert child.read_text(encoding="utf-8") == before

    def test_legacy_kcaa_child_with_sheet_instances_is_rejected(self, tools, tmp_path):
        child = tmp_path / "legacy_child.kicad_sch"
        _write_schematic(
            child, root_uuid=CHILD_UUID, pages=(2,), child_file="leaf.kicad_sch", is_root=False
        )
        text = child.read_text(encoding="utf-8").rstrip()
        child.write_text(
            text[:-1] + f'  (sheet_instances (path "/{CHILD_UUID}" (page "1")))\n)\n',
            encoding="utf-8",
        )
        before = child.read_text(encoding="utf-8")

        result = _add_sheet(tools, str(child), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert "not the project root" in result["error"]
        assert child.read_text(encoding="utf-8") == before
        assert not (tmp_path / "legacy_child.kicad_sch.bak").exists()
        assert not (tmp_path / "orphan.kicad_sch").exists()

    def test_instance_path_prefers_matching_project_clause(self):
        from kcaa.tools.sheet_tools import _first_instance_path

        node = sexpdata.loads(
            '(sheet (instances (project "other" (path "/other" (page "1")))'
            ' (project "root" (path "/root" (page "2")))))'
        )

        assert _first_instance_path(node, "root") == "/root"
        assert _first_instance_path(node, "unrelated") == "/other"


class TestEmittedPrecision:
    """#324 item 4: emitted coordinates carry no float noise."""

    @staticmethod
    def _at_size_numbers(node) -> list[float]:
        """Collect numeric values of every nested ``(at ...)`` / ``(size ...)``."""
        numbers: list[float] = []
        for child in node:
            if not isinstance(child, list) or not child:
                continue
            if _tag(child) in ("at", "size"):
                numbers.extend(v for v in child[1:] if isinstance(v, (int, float)))
            numbers.extend(TestEmittedPrecision._at_size_numbers(child))
        return numbers

    def test_added_sheet_and_pin_coordinates_have_no_float_noise(self, tools, tmp_path):
        # 41.91 is 33 grid steps; the raw grid product is 41.910000000000004.
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID)

        result = _add_sheet(
            tools,
            str(root),
            x=41.91,
            y=300.0,
            width=41.91,
            height=25.4,
            pins=[{"name": "P", "edge": "right", "distance_mm": 41.91}],
        )
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _child(sheet, "at")[1] == 41.91
        assert _child(sheet, "size")[1] == 41.91
        pin_at = _child(_child(sheet, "pin"), "at")
        assert pin_at[1] == 83.82
        assert pin_at[2] == 341.63
        for value in self._at_size_numbers(sheet):
            assert value == round(value, 6), value

    def test_added_pin_coordinates_have_no_float_noise(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))

        result = asyncio.run(
            tools["add_sheet_pin"](
                schematic_path=str(root),
                sheet_identifier="00000000-0000-0000-0000-000000000001",
                pin_name="P",
                edge="left",
                distance_mm=0.2,
            )
        )
        assert result.get("success") is True, result
        assert result["position"] == {"x": 88.9, "y": 25.6}

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        pin_at = _child(_child(sheet, "pin"), "at")
        assert pin_at[1] == 88.9
        assert pin_at[2] == 25.6
        for value in self._at_size_numbers(_child(sheet, "pin")):
            assert value == round(value, 6), value

    def test_updated_sheet_coordinates_have_no_float_noise(self, tools, tmp_path):
        # 41.91 is 33 grid steps; the raw grid product is 41.910000000000004.
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, pages=(2,))

        result = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=str(root),
                sheet_identifier="00000000-0000-0000-0000-000000000001",
                x=41.91,
                y=41.91,
                width=41.91,
                height=41.91,
            )
        )
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        at = _child(sheet, "at")
        size = _child(sheet, "size")
        assert (at[1], at[2]) == (41.91, 41.91)
        assert (size[1], size[2]) == (41.91, 41.91)
        assert result["position"] == {"x": 41.91, "y": 41.91}
        assert result["size"] == {"width": 41.91, "height": 41.91}
        for value in self._at_size_numbers(sheet):
            assert value == round(value, 6), value


class TestChildSchematic:
    """D7: child files carry only the sections KiCad keeps."""

    def test_child_file_has_no_root_only_sections(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID)

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="new_child.kicad_sch")
        assert result.get("success") is True, result

        child_path = tmp_path / "new_child.kicad_sch"
        assert Path(result["child_path"]) == child_path
        child = child_path.read_text(encoding="utf-8")
        child_tags = {_tag(node) for node in _load(str(child_path))}
        assert "sheet_instances" not in child_tags
        assert "embedded_fonts" not in child_tags
        assert "(uuid " in child
        assert '(paper "A4")' in child
        assert "(lib_symbols)" in child

    def test_new_sheet_precedes_root_only_sections(self, tools, tmp_sch):
        result = _add_sheet(tools, tmp_sch, x=200.0, y=25.4)
        assert result.get("success") is True, result

        tree = _load(tmp_sch)
        sheet_index = next(i for i, node in enumerate(tree) if _tag(node) == "sheet")
        for section in ("sheet_instances", "embedded_fonts"):
            section_index = next(i for i, node in enumerate(tree) if _tag(node) == section)
            assert sheet_index < section_index


class TestLegacySheetProperties:
    """Reads stay compatible with files written by older kcaa versions."""

    def test_legacy_property_names_are_read_and_renamed(self, tools, tmp_path):
        root = tmp_path / "legacy.kicad_sch"
        root.write_text(
            '(kicad_sch (version 20260306) (generator "kcaa") (generator_version "0.2.0")\n'
            f'  (uuid "{ROOT_UUID}")\n'
            '  (paper "A4")\n'
            "  (lib_symbols)\n"
            "  (sheet (at 25.4 25.4) (size 50.8 25.4)\n"
            "    (stroke (width 0) (type dash))\n"
            "    (fill (type none))\n"
            f'    (uuid "{LEGACY_SHEET_UUID}")\n'
            '    (property "Sheet name" "Legacy" (at 0 0 0) (show_name no)'
            " (do_not_autoplace yes) (effects (font (size 1.27 1.27)) (justify left)))\n"
            '    (property "Sheet file" "legacy_child.kicad_sch" (at 0 0 0) (show_name no)'
            " (do_not_autoplace yes) (effects (font (size 1.27 1.27)) (justify left)))\n"
            f'    (instances (project "legacy" (path "/{ROOT_UUID}" (page "2"))))\n'
            "  )\n"
            '  (sheet_instances (path "/" (page "1")))\n'
            "  (embedded_fonts no)\n"
            ")\n",
            encoding="utf-8",
        )

        listed = asyncio.run(tools["list_sheet_symbols"](schematic_path=str(root)))
        assert listed["sheets"][0]["sheet_name"] == "Legacy"
        assert listed["sheets"][0]["sheet_file"] == "legacy_child.kicad_sch"

        update = asyncio.run(
            tools["update_sheet_symbol"](
                schematic_path=str(root),
                sheet_identifier=LEGACY_SHEET_UUID,
                sheet_name="Renamed Legacy",
            )
        )
        assert update["success"] is True, update

        properties = {
            _property_key(prop[1]): prop
            for prop in _children(_sheet_node(_load(str(root)), LEGACY_SHEET_UUID), "property")
        }
        assert properties["sheetname"][1] == "Sheet name"
        assert properties["sheetname"][2] == "Renamed Legacy"


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli is not installed")
class TestKicadRoundTrip:
    """KiCad 10 must not rewrite any field of a generated sheet."""

    def test_upgrade_preserves_sheet_fields(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID)
        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        # The project file keeps KiCad from blanking the instances project name.
        (tmp_path / "root.kicad_pro").write_text(
            json.dumps(
                {
                    "meta": {"filename": "root.kicad_pro", "version": 3},
                    "sheets": [[ROOT_UUID, "root"]],
                }
            ),
            encoding="utf-8",
        )
        before = sexpdata.dumps(_sheet_node(_load(str(root)), result["sheet_uuid"]))

        subprocess.run(
            ["kicad-cli", "sch", "upgrade", "--force", str(root)],
            check=True,
            capture_output=True,
            text=True,
        )

        after = sexpdata.dumps(_sheet_node(_load(str(root)), result["sheet_uuid"]))
        assert after == before
