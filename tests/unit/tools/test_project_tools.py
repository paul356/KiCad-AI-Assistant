"""
Tests for project management tools (project_tools.py).

Builds a minimal multi-sheet KiCad project on disk and verifies that
``get_project_structure`` reports the flat file set, the sheet
hierarchy, and lib-table presence.
"""

import json

import pytest


@pytest.fixture
def tmp_project(tmp_path):
    """Build a minimal multi-sheet KiCad project on disk."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "demo.kicad_pro").write_text(json.dumps({"metadata": {"version": 1}}))
    (proj / "demo.kicad_sch").write_text(
        '(kicad_sch (version 20231120) (generator "test")\n'
        "  (sheet (at 0 0) (size 10 10)\n"
        '    (property "Sheetname" "Root" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
        '    (property "Sheetfile" "power.kicad_sch" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
        "  )\n"
        ")\n"
    )
    (proj / "power.kicad_sch").write_text('(kicad_sch (version 20231120) (generator "test"))\n')
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb (version 20231120))\n")
    (proj / "sym-lib-table").write_text("(sym_lib_table (version 1))\n")
    (proj / "fp-lib-table").write_text("(fp_lib_table (version 1))\n")
    return proj / "demo.kicad_pro"


class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.project_tools import register_project_tools

    mock = _MockMCP()
    register_project_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


class TestGetProjectStructure:
    def _call(self, tools, project_path):
        return tools["get_project_structure"](project_path=project_path)

    def test_missing_project_returns_error(self, tools):
        result = self._call(tools, "/no/such/project.kicad_pro")
        assert "error" in result

    def test_flat_files_and_metadata(self, tools, tmp_project):
        result = self._call(tools, str(tmp_project))
        assert result["name"] == "demo"
        assert result["path"] == str(tmp_project)
        assert result["files"]["schematic"].endswith("demo.kicad_sch")
        assert result["files"]["pcb"].endswith("demo.kicad_pcb")
        assert result["metadata"] == {"version": 1}

    def test_sheet_hierarchy_follows_sheetfile_refs(self, tools, tmp_project):
        result = self._call(tools, str(tmp_project))
        assert len(result["sheets"]) == 1
        root = result["sheets"][0]
        assert root["path"].endswith("demo.kicad_sch")
        assert len(root["children"]) == 1
        child = root["children"][0]
        assert child["path"].endswith("power.kicad_sch")
        assert "children" not in child

    def test_sheet_cycle_is_cut(self, tools, tmp_path):
        """A child sheet referencing its parent must not recurse forever."""
        proj = tmp_path / "cyc"
        proj.mkdir()
        (proj / "cyc.kicad_pro").write_text("{}")
        parent = (
            "(kicad_sch (version 20231120)\n"
            "  (sheet (at 0 0) (size 10 10)\n"
            '    (property "Sheetfile" "child.kicad_sch" (at 0 0 0) '
            "(effects (font (size 1.27 1.27))))\n"
            "  )\n"
            ")\n"
        )
        child = (
            "(kicad_sch (version 20231120)\n"
            "  (sheet (at 0 0) (size 10 10)\n"
            '    (property "Sheetfile" "cyc.kicad_sch" (at 0 0 0) '
            "(effects (font (size 1.27 1.27))))\n"
            "  )\n"
            ")\n"
        )
        (proj / "cyc.kicad_sch").write_text(parent)
        (proj / "child.kicad_sch").write_text(child)

        result = self._call(tools, str(proj / "cyc.kicad_pro"))
        root = result["sheets"][0]
        assert len(root["children"]) == 1
        assert root["children"][0]["path"].endswith("child.kicad_sch")
        # Back-reference to the root appears as a leaf, not a new subtree.
        assert len(root["children"][0]["children"]) == 1
        assert "children" not in root["children"][0]["children"][0]

    def test_lib_tables(self, tools, tmp_project):
        result = self._call(tools, str(tmp_project))
        assert result["lib_tables"]["sym_lib_table"].endswith("sym-lib-table")
        assert result["lib_tables"]["fp_lib_table"].endswith("fp-lib-table")

    def test_absent_tables_are_none(self, tools, tmp_path):
        proj = tmp_path / "bare"
        proj.mkdir()
        (proj / "bare.kicad_pro").write_text("{}")
        (proj / "bare.kicad_sch").write_text("(kicad_sch (version 20231120))\n")

        result = self._call(tools, str(proj / "bare.kicad_pro"))
        assert result["lib_tables"] == {"sym_lib_table": None, "fp_lib_table": None}
