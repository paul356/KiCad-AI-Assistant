"""Unit tests for sheet_tools.py."""

import asyncio
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

    def test_instances_use_owning_project_name(self, tools, tmp_path):
        """A nested sheet records the root project's name, not its own stem."""
        (tmp_path / "aria.kicad_pro").write_text("{}")
        hardware = tmp_path / "hardware"
        hardware.mkdir()
        schematic = hardware / "main_bus.kicad_sch"
        shutil.copy(SCHEMATIC_PATH, schematic)

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

    def test_root_uuid_wins_over_stale_sheet_path(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=ROOT_UUID, instance_path="/stale", pages=(2,))

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{ROOT_UUID}"

    def test_existing_sheet_instance_path_is_the_fallback(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, instance_path="/existing-root", pages=(2,))

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == "/existing-root"

    @pytest.mark.parametrize("root_name", ["root", "Root", "Top"])
    def test_project_file_root_sheet_is_the_last_resort(self, tools, tmp_path, root_name):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, pages=())
        (tmp_path / "root.kicad_pro").write_text(
            json.dumps({"sheets": [[PROJECT_UUID, root_name]]}), encoding="utf-8"
        )

        result = _add_sheet(tools, str(root))
        assert result.get("success") is True, result

        sheet = _sheet_node(_load(str(root)), result["sheet_uuid"])
        assert _instance_path(sheet) == f"/{PROJECT_UUID}"

    def test_foreign_project_file_is_not_adopted(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, pages=())
        (tmp_path / "other.kicad_pro").write_text(
            json.dumps({"sheets": [[PROJECT_UUID, "Top"]]}), encoding="utf-8"
        )
        before = root.read_text(encoding="utf-8")

        result = _add_sheet(tools, str(root), create_child=True, sheet_file="orphan.kicad_sch")

        assert "error" in result
        assert root.read_text(encoding="utf-8") == before
        assert not (tmp_path / "root.kicad_sch.bak").exists()
        assert not (tmp_path / "orphan.kicad_sch").exists()

    def test_sibling_project_file_with_wrong_root_name_is_rejected(self, tools, tmp_path):
        root = tmp_path / "root.kicad_sch"
        _write_schematic(root, root_uuid=None, pages=())
        (tmp_path / "root.kicad_pro").write_text(
            json.dumps({"sheets": [[PROJECT_UUID, "Nope"]]}), encoding="utf-8"
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
