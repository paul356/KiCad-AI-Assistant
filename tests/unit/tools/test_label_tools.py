"""
Tests for the label tools in component_edit_tools.py:
  - add_label_to_schematic
  - list_labels_in_schematic
  - delete_label_from_schematic

All write tests work on a temporary copy of tools_test.kicad_sch so the
original fixture is never modified.

Fixture assumptions (tools_test.kicad_sch):
    The schematic contains no pre-existing local labels.
    Labels are placed at coordinates (e.g. 200.0, 200.0) that do not
    coincide with any placed symbol or pin.
"""

import asyncio
import os
from pathlib import Path
import shutil
import uuid

import pytest
import skip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures" / "tools_test.kicad_sch")


def _make_temp_copy() -> str:
    """Return path to a fresh temporary copy of tools_test.kicad_sch."""
    tmp_path = Path(__file__).parent / "fixtures" / f"tools_test_{uuid.uuid4().hex}.kicad_sch"
    shutil.copy(SCHEMATIC_PATH, tmp_path)
    return str(tmp_path)


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
    """Register component edit tools against a mock MCP and return the dict."""
    from kcaa.tools.component_edit_tools import register_component_edit_tools

    mock = _MockMCP()
    register_component_edit_tools(mock)
    return mock.tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    """Yield a temp copy of the schematic, then clean up."""
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


# ---------------------------------------------------------------------------
# add_label_to_schematic
# ---------------------------------------------------------------------------


class TestAddLabelToSchematic:
    def _add(
        self,
        tools,
        schematic_path,
        text="NET_A",
        x=200.0,
        y=200.0,
        angle=0,
        label_type="local",
        shape="input",
    ):
        return asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=schematic_path,
                text=text,
                x=x,
                y=y,
                angle=angle,
                label_type=label_type,
                shape=shape,
            )
        )

    def test_add_label_returns_success_and_correct_metadata(self, tools, tmp_sch):
        """Adding a valid label returns success=True with correct text/x/y/direction."""
        result = self._add(tools, tmp_sch, text="VCC_NET", x=200.0, y=200.0, angle=0)
        assert result.get("success") is True, result
        lbl = result["label"]
        assert lbl["text"] == "VCC_NET"
        assert abs(lbl["x"] - 200.0) < 0.01
        assert abs(lbl["y"] - 200.0) < 0.01
        assert lbl["direction"] == "right"

        # Verify label is persisted in the written schematic
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            str(lbl_obj.value) == "VCC_NET"
            and abs(float(lbl_obj.at.value[0]) - 200.0) < 0.01
            and abs(float(lbl_obj.at.value[1]) - 200.0) < 0.01
            for lbl_obj in sch2.label
        )
        assert found, "Added label 'VCC_NET' not found in saved schematic"

    def test_invalid_angle_returns_error(self, tools, tmp_sch):
        """angle=45 is not a valid KiCad label angle; should return error dict."""
        result = self._add(tools, tmp_sch, text="BAD_ANGLE", angle=45)
        assert "error" in result

    def test_empty_text_returns_error(self, tools, tmp_sch):
        """Empty label text should be rejected with an error dict."""
        result = self._add(tools, tmp_sch, text="")
        assert "error" in result

    def test_add_global_label(self, tools, tmp_sch):
        """Adding a global label stores its type and shape and makes it listable."""
        result = self._add(
            tools,
            tmp_sch,
            text="G_OUT",
            x=230.0,
            y=230.0,
            angle=180,
            label_type="global",
            shape="output",
        )
        assert result.get("success") is True, result
        assert result["label"]["label_type"] == "global"
        assert result["label"]["shape"] == "output"

        listed = asyncio.run(
            tools["list_labels_in_schematic"](schematic_path=tmp_sch, label_type="global")
        )
        matches = [lbl for lbl in listed["labels"] if lbl["text"] == "G_OUT"]
        assert len(matches) == 1
        assert matches[0]["shape"] == "output"
        assert matches[0]["label_type"] == "global"

    def test_add_hierarchical_label(self, tools, tmp_sch):
        """Adding a hierarchical label stores its type and shape and makes it listable."""
        result = self._add(
            tools,
            tmp_sch,
            text="H_BI",
            x=231.0,
            y=231.0,
            angle=270,
            label_type="hierarchical",
            shape="bidirectional",
        )
        assert result.get("success") is True, result
        assert result["label"]["label_type"] == "hierarchical"
        assert result["label"]["shape"] == "bidirectional"

        listed = asyncio.run(
            tools["list_labels_in_schematic"](schematic_path=tmp_sch, label_type="hierarchical")
        )
        matches = [lbl for lbl in listed["labels"] if lbl["text"] == "H_BI"]
        assert len(matches) == 1
        assert matches[0]["shape"] == "bidirectional"
        assert matches[0]["label_type"] == "hierarchical"

    def test_add_label_invalid_type(self, tools, tmp_sch):
        """Unknown label_type values should be rejected."""
        result = self._add(tools, tmp_sch, label_type="mystery")
        assert "error" in result
        assert "label_type" in result["error"]

    def test_add_label_invalid_shape(self, tools, tmp_sch):
        """Unknown shapes for global/hierarchical labels should be rejected."""
        result = self._add(tools, tmp_sch, label_type="global", shape="mystery")
        assert "error" in result
        assert "shape" in result["error"]

    def test_non_kicad_sch_extension_returns_error(self, tools):
        """A path without the .kicad_sch extension should be rejected immediately."""
        bogus_path = str(Path(__file__).parent / "fixtures" / "bogus.txt")
        result = self._add(tools, bogus_path)
        assert "error" in result

    def test_nonexistent_file_returns_error(self, tools):
        """A .kicad_sch path that does not exist on disk should return an error."""
        missing_path = str(Path(__file__).parent / "fixtures" / "no_such_file_label_test.kicad_sch")
        result = self._add(tools, missing_path)
        assert "error" in result


# ---------------------------------------------------------------------------
# list_labels_in_schematic
# ---------------------------------------------------------------------------


class TestListLabelsInSchematic:
    def test_no_labels_returns_empty_list_and_zero_count(self, tools, tmp_sch):
        """A freshly-copied schematic with no labels returns success=True, count=0, labels=[]."""
        result = asyncio.run(
            tools["list_labels_in_schematic"](
                schematic_path=tmp_sch,
            )
        )
        assert result.get("success") is True, result
        assert result["count"] == 0
        assert result["labels"] == []

    def test_list_labels_all_types(self, tools, tmp_sch):
        """Listing with no filter should include local, global, and hierarchical labels."""
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="LOCAL_ALL",
                x=240.0,
                y=240.0,
            )
        )
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="GLOBAL_ALL",
                x=241.0,
                y=241.0,
                label_type="global",
                shape="output",
            )
        )
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="HIER_ALL",
                x=242.0,
                y=242.0,
                label_type="hierarchical",
                shape="passive",
            )
        )

        result = asyncio.run(tools["list_labels_in_schematic"](schematic_path=tmp_sch))
        assert result.get("success") is True, result
        assert result["count"] == 3

        by_text = {label["text"]: label for label in result["labels"]}
        assert by_text["LOCAL_ALL"]["label_type"] == "local"
        assert by_text["LOCAL_ALL"]["shape"] is None
        assert by_text["GLOBAL_ALL"]["label_type"] == "global"
        assert by_text["GLOBAL_ALL"]["shape"] == "output"
        assert by_text["HIER_ALL"]["label_type"] == "hierarchical"
        assert by_text["HIER_ALL"]["shape"] == "passive"

    def test_list_labels_filter_by_type(self, tools, tmp_sch):
        """The label_type filter should only return matching label types."""
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="FILTER_LOCAL",
                x=243.0,
                y=243.0,
            )
        )
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="FILTER_GLOBAL",
                x=244.0,
                y=244.0,
                label_type="global",
                shape="input",
            )
        )

        result = asyncio.run(
            tools["list_labels_in_schematic"](schematic_path=tmp_sch, label_type="global")
        )
        assert result.get("success") is True, result
        assert result["count"] == 1
        assert [label["text"] for label in result["labels"]] == ["FILTER_GLOBAL"]
        assert all(label["label_type"] == "global" for label in result["labels"])

    def test_added_label_appears_in_list_with_correct_fields(self, tools, tmp_sch):
        """After adding a label, list_labels_in_schematic reports it with the correct fields."""
        asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=tmp_sch,
                text="SIGNAL_X",
                x=201.0,
                y=201.0,
                angle=90,
            )
        )

        result = asyncio.run(
            tools["list_labels_in_schematic"](
                schematic_path=tmp_sch,
            )
        )
        assert result.get("success") is True, result
        assert result["count"] >= 1

        matches = [lbl for lbl in result["labels"] if lbl["text"] == "SIGNAL_X"]
        assert len(matches) == 1, (
            f"Expected exactly one 'SIGNAL_X' label, found: {result['labels']}"
        )
        m = matches[0]
        assert abs(m["x"] - 201.0) < 0.01, m
        assert abs(m["y"] - 201.0) < 0.01, m
        assert m["direction"] == "down", m
        assert m["label_type"] == "local", m
        assert m["shape"] is None, m


# ---------------------------------------------------------------------------
# delete_label_from_schematic
# ---------------------------------------------------------------------------


class TestDeleteLabelFromSchematic:
    def _add(
        self,
        tools,
        sch_path,
        text="NET_DEL",
        x=210.0,
        y=210.0,
        angle=0,
        label_type="local",
        shape="input",
    ):
        return asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=sch_path,
                text=text,
                x=x,
                y=y,
                angle=angle,
                label_type=label_type,
                shape=shape,
            )
        )

    def _delete(self, tools, sch_path, x, y, **kwargs):
        return asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=sch_path,
                x=x,
                y=y,
                **kwargs,
            )
        )

    def test_delete_global_label(self, tools, tmp_sch):
        """Deleting a global label with label_type filter should remove it."""
        add_result = self._add(
            tools,
            tmp_sch,
            text="GLOBAL_DEL",
            x=213.0,
            y=213.0,
            label_type="global",
            shape="output",
        )
        assert add_result.get("success") is True, add_result

        result = self._delete(tools, tmp_sch, x=213.0, y=213.0, label_type="global")
        assert result.get("success") is True, result
        assert result["deleted_count"] == 1

        listed = asyncio.run(
            tools["list_labels_in_schematic"](schematic_path=tmp_sch, label_type="global")
        )
        assert listed["count"] == 0

    def test_delete_hierarchical_label(self, tools, tmp_sch):
        """Deleting a hierarchical label with label_type filter should remove it."""
        add_result = self._add(
            tools,
            tmp_sch,
            text="HIER_DEL",
            x=214.0,
            y=214.0,
            label_type="hierarchical",
            shape="bidirectional",
        )
        assert add_result.get("success") is True, add_result

        result = self._delete(tools, tmp_sch, x=214.0, y=214.0, label_type="hierarchical")
        assert result.get("success") is True, result
        assert result["deleted_count"] == 1

        listed = asyncio.run(
            tools["list_labels_in_schematic"](
                schematic_path=tmp_sch,
                label_type="hierarchical",
            )
        )
        assert listed["count"] == 0

    def test_delete_by_position_succeeds_and_removes_label(self, tools, tmp_sch):
        """Deleting a label by its position returns success=True/deleted_count=1 and removes it."""
        add_result = self._add(tools, tmp_sch, text="TO_DEL", x=210.0, y=210.0)
        assert add_result.get("success") is True, add_result

        result = self._delete(tools, tmp_sch, x=210.0, y=210.0)
        assert result.get("success") is True, result
        assert result["deleted_count"] == 1

        # Label must no longer exist in the saved schematic
        sch2 = skip.Schematic(tmp_sch)
        try:
            remaining = list(sch2.label)
        except AttributeError:
            remaining = []
        still_present = any(
            str(lbl.value) == "TO_DEL" and abs(float(lbl.at.value[0]) - 210.0) < 0.01
            for lbl in remaining
        )
        assert not still_present, "Deleted label 'TO_DEL' still present in schematic"

    def test_delete_with_matching_text_filter_succeeds(self, tools, tmp_sch):
        """Delete with a text argument that matches the label text should succeed."""
        add_result = self._add(tools, tmp_sch, text="MATCH_ME", x=211.0, y=211.0)
        assert add_result.get("success") is True, add_result

        result = self._delete(tools, tmp_sch, x=211.0, y=211.0, text="MATCH_ME")
        assert result.get("success") is True, result
        assert result["deleted_count"] == 1

    def test_delete_with_non_matching_text_filter_returns_error(self, tools, tmp_sch):
        """Delete with a text filter that does not match any label should return an error."""
        add_result = self._add(tools, tmp_sch, text="KEEP_ME", x=212.0, y=212.0)
        assert add_result.get("success") is True, add_result

        result = self._delete(tools, tmp_sch, x=212.0, y=212.0, text="WRONG_TEXT")
        assert "error" in result

        # Original label must still be present
        sch2 = skip.Schematic(tmp_sch)
        found = any(
            str(lbl.value) == "KEEP_ME" and abs(float(lbl.at.value[0]) - 212.0) < 0.01
            for lbl in sch2.label
        )
        assert found, "Label 'KEEP_ME' should not have been deleted"

    def test_delete_at_wrong_position_returns_error(self, tools, tmp_sch):
        """Delete at a position where no label exists should return an error dict."""
        result = self._delete(tools, tmp_sch, x=999.0, y=999.0)
        assert "error" in result


# ---------------------------------------------------------------------------
# delete_label_from_schematic – batch mode (positions=[...])
# ---------------------------------------------------------------------------


class TestDeleteLabelBatchMode:
    def _add(self, tools, sch_path, text, x, y, angle=0, label_type="local", shape="input"):
        return asyncio.run(
            tools["add_label_to_schematic"](
                schematic_path=sch_path,
                text=text,
                x=x,
                y=y,
                angle=angle,
                label_type=label_type,
                shape=shape,
            )
        )

    def test_batch_delete_two_labels(self, tools, tmp_sch):
        """Passing positions=[...] deletes all matching labels in one call."""
        self._add(tools, tmp_sch, text="B_NET1", x=220.0, y=220.0)
        self._add(tools, tmp_sch, text="B_NET2", x=221.0, y=221.0)

        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[
                    {"x": 220.0, "y": 220.0},
                    {"x": 221.0, "y": 221.0},
                ],
            )
        )
        assert result.get("success") is True, result
        assert result["total_deleted"] == 2
        assert len(result["results"]) == 2
        assert all("deleted_count" in r for r in result["results"])

        sch2 = skip.Schematic(tmp_sch)
        try:
            remaining_texts = {str(lbl.value) for lbl in sch2.label}
        except AttributeError:
            remaining_texts = set()
        assert "B_NET1" not in remaining_texts
        assert "B_NET2" not in remaining_texts

    def test_batch_delete_with_text_filter(self, tools, tmp_sch):
        """Batch entry with text= only deletes labels whose text matches."""
        self._add(tools, tmp_sch, text="CORRECT", x=222.0, y=222.0)

        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[{"x": 222.0, "y": 222.0, "text": "CORRECT"}],
            )
        )
        assert result.get("success") is True, result
        assert result["total_deleted"] == 1

    def test_batch_partial_not_found_reports_per_entry(self, tools, tmp_sch):
        """When some positions match and some don't, results are reported per entry."""
        self._add(tools, tmp_sch, text="EXIST", x=223.0, y=223.0)

        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[
                    {"x": 223.0, "y": 223.0},  # exists
                    {"x": 998.0, "y": 998.0},  # does not exist
                ],
            )
        )
        assert result.get("success") is True, result
        assert result["total_deleted"] == 1
        ok_entry = next(r for r in result["results"] if r.get("deleted_count"))
        err_entry = next(r for r in result["results"] if "error" in r)
        assert ok_entry["deleted_count"] == 1
        assert "998.0" in err_entry["error"] or 998.0 in (err_entry["x"], err_entry["y"])

    def test_batch_all_not_found_returns_success_false(self, tools, tmp_sch):
        """When no positions match at all, success should be False."""
        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[
                    {"x": 991.0, "y": 991.0},
                    {"x": 992.0, "y": 992.0},
                ],
            )
        )
        assert result.get("success") is False, result
        assert result["total_deleted"] == 0
        assert all("error" in r for r in result["results"])

    def test_batch_delete_respects_label_type(self, tools, tmp_sch):
        """Batch deletion should only consider the requested label_type."""
        self._add(tools, tmp_sch, text="GLOBAL_ONLY", x=224.0, y=224.0, label_type="global")

        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[{"x": 224.0, "y": 224.0}],
                label_type="local",
            )
        )
        assert result.get("success") is False, result
        assert result["total_deleted"] == 0

        listed = asyncio.run(
            tools["list_labels_in_schematic"](schematic_path=tmp_sch, label_type="global")
        )
        assert [label["text"] for label in listed["labels"]] == ["GLOBAL_ONLY"]

    def test_batch_empty_positions_returns_error(self, tools, tmp_sch):
        """An empty positions list should return an error immediately."""
        result = asyncio.run(
            tools["delete_label_from_schematic"](
                schematic_path=tmp_sch,
                positions=[],
            )
        )
        assert "error" in result
