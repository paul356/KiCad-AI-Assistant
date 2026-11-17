"""
Unit tests for project-level version tools in kcaa/tools/version_tools.py.

Exercises the full archive round-trip against real temp project files;
version_manager internals are only mocked for error-path tests.
"""

import asyncio
import json
import os
import tarfile
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# MockMCP — captures @mcp.tool()-decorated coroutines
# ---------------------------------------------------------------------------


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
    """Register version tools against a mock MCP and return the captured dict."""
    from kcaa.tools.version_tools import register_version_tools

    mock = _MockMCP()
    register_version_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tools():
    return _get_tools()


@pytest.fixture()
def mock_ctx():
    ctx = MagicMock()
    ctx.info = MagicMock()
    ctx.report_progress = MagicMock(return_value=asyncio.sleep(0))
    return ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path, name: str = "board") -> str:
    """Create a minimal KiCad project (pro + sch + pcb) and return the pro path."""
    pro = tmp_path / f"{name}.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 1}}))
    (tmp_path / f"{name}.kicad_sch").write_text("kicad_sch v1\n")
    (tmp_path / f"{name}.kicad_pcb").write_text("kicad_pcb v1\n")
    return str(pro)


def _archive_dir(project_file: str) -> str:
    """Return the project-archive directory for a project file."""
    return os.path.join(os.path.dirname(project_file), ".versions", "project")


# ---------------------------------------------------------------------------
# save_project_version
# ---------------------------------------------------------------------------


class TestSaveProjectVersion:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["save_project_version"]

    def test_save_new_archive_bundles_all_files(self, tmp_path):
        """Saving creates one .tar.gz bundling pro + sch + pcb."""
        pro = _make_project(tmp_path)
        result = _run(self.fn(pro, ctx=None))

        assert result["success"] is True
        assert result["created"] is True
        assert result["version_id"]
        assert result["archive_path"].endswith(".tar.gz")
        assert set(result["files"]) == {"board.kicad_pro", "board.kicad_sch", "board.kicad_pcb"}
        assert os.path.isfile(result["archive_path"])

        with tarfile.open(result["archive_path"], "r:gz") as tar:
            names = tar.getnames()
        assert sorted(names) == [
            ".manifest.json",
            "board.kicad_pcb",
            "board.kicad_pro",
            "board.kicad_sch",
        ]

    def test_save_unchanged_reuses_latest(self, tmp_path):
        """Identical file set reuses the latest archive (created=False)."""
        pro = _make_project(tmp_path)
        r1 = _run(self.fn(pro, ctx=None))
        r2 = _run(self.fn(pro, ctx=None))

        assert r2["created"] is False
        assert r2["archive_path"] == r1["archive_path"]
        assert r2["version_id"] == r1["version_id"]

    def test_save_change_creates_new_archive(self, tmp_path):
        """Changing a file creates a distinct archive under a new version id."""
        pro = _make_project(tmp_path)
        r1 = _run(self.fn(pro, ctx=None))
        (tmp_path / "board.kicad_sch").write_text("kicad_sch v2\n")
        r2 = _run(self.fn(pro, ctx=None))

        assert r2["created"] is True
        assert r2["version_id"] != r1["version_id"]

    def test_missing_project_file(self, tmp_path):
        """Nonexistent .kicad_pro -> error."""
        pro = str(tmp_path / "nope.kicad_pro")
        result = _run(self.fn(pro, ctx=None))
        assert "error" in result
        assert "File not found" in result["error"]

    def test_pro_alone_archives_just_the_pro(self, tmp_path):
        """.kicad_pro without sibling sch/pcb still archives the pro alone."""
        pro = tmp_path / "solo.kicad_pro"
        pro.write_text("{}")
        result = _run(self.fn(str(pro), ctx=None))
        assert result["success"] is True
        assert result["files"] == ["solo.kicad_pro"]
        with tarfile.open(result["archive_path"], "r:gz") as tar:
            assert "solo.kicad_pro" in tar.getnames()

    def test_keep_prunes_oldest(self, tmp_path):
        """keep=2 retains only the two newest archives after three saves."""
        pro = _make_project(tmp_path)
        ids = []
        for i in range(3):
            (tmp_path / "board.kicad_sch").write_text(f"kicad_sch v{i}\n")
            r = _run(self.fn(pro, keep=2, ctx=None))
            ids.append(r["version_id"])
        assert len(set(ids)) == 3

        remaining = sorted(os.listdir(_archive_dir(pro)))
        assert len(remaining) == 2
        assert f"board.project.{ids[0]}.tar.gz" not in remaining
        assert f"board.project.{ids[1]}.tar.gz" in remaining
        assert f"board.project.{ids[2]}.tar.gz" in remaining

    @patch("kcaa.utils.version_manager.save_project_version")
    def test_os_error(self, mock_save, tmp_path):
        """When save_project_version raises OSError."""
        pro = _make_project(tmp_path)
        mock_save.side_effect = OSError("Permission denied")
        result = _run(self.fn(pro, ctx=None))
        assert "error" in result
        assert "Failed to save project version" in result["error"]


# ---------------------------------------------------------------------------
# list_project_versions
# ---------------------------------------------------------------------------


class TestListProjectVersions:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["list_project_versions"]

    def test_list_after_saves_newest_first(self, tmp_path):
        """Multiple saves list newest-first with file sets attached."""
        pro = _make_project(tmp_path)
        save = self.tools["save_project_version"]
        _run(save(pro, ctx=None))
        (tmp_path / "board.kicad_sch").write_text("kicad_sch v2\n")
        _run(save(pro, ctx=None))

        result = _run(self.fn(pro, ctx=None))

        assert result["success"] is True
        assert result["count"] == 2
        assert result["versions"][0]["timestamp"] >= result["versions"][1]["timestamp"]
        assert set(result["versions"][0]["files"]) == {
            "board.kicad_pro",
            "board.kicad_sch",
            "board.kicad_pcb",
        }
        assert all(v["id"] and v["size_bytes"] > 0 for v in result["versions"])

    def test_list_empty(self, tmp_path):
        """No archives yet -> empty list."""
        pro = _make_project(tmp_path)
        result = _run(self.fn(pro, ctx=None))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["versions"] == []


# ---------------------------------------------------------------------------
# restore_project_version
# ---------------------------------------------------------------------------


class TestRestoreProjectVersion:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["restore_project_version"]

    def test_restore_reverts_all_files(self, tmp_path):
        """Restoring reverts sch + pcb + pro as one consistent set."""
        pro = _make_project(tmp_path)
        save = self.tools["save_project_version"]
        r = _run(save(pro, ctx=None))

        # Mutate all three files
        (tmp_path / "board.kicad_sch").write_text("kicad_sch v2\n")
        (tmp_path / "board.kicad_pcb").write_text("kicad_pcb v2\n")
        pro_file = tmp_path / "board.kicad_pro"
        pro_file.write_text(json.dumps({"meta": {"filename": "board.kicad_pro", "version": 2}}))

        result = _run(self.fn(str(pro_file), r["version_id"], ctx=None))

        assert result["success"] is True
        assert result["restored_from"] == r["version_id"]
        assert set(result["files"]) == {"board.kicad_pro", "board.kicad_sch", "board.kicad_pcb"}
        assert "backup_of_current" in result
        assert "backup_version_id" in result
        assert (tmp_path / "board.kicad_sch").read_text() == "kicad_sch v1\n"
        assert (tmp_path / "board.kicad_pcb").read_text() == "kicad_pcb v1\n"
        assert json.loads(pro_file.read_text())["meta"]["version"] == 1

    def test_restore_is_undoable_via_backup(self, tmp_path):
        """The pre-restore state is archived and can itself be restored."""
        pro = _make_project(tmp_path)
        save = self.tools["save_project_version"]
        r = _run(save(pro, ctx=None))

        (tmp_path / "board.kicad_sch").write_text("kicad_sch v2\n")
        result = _run(self.fn(pro, r["version_id"], ctx=None))
        assert (tmp_path / "board.kicad_sch").read_text() == "kicad_sch v1\n"

        # Undo: restore back to the pre-restore state
        undo = _run(self.fn(pro, result["backup_version_id"], ctx=None))
        assert undo["success"] is True
        assert (tmp_path / "board.kicad_sch").read_text() == "kicad_sch v2\n"

    def test_restore_unknown_version(self, tmp_path):
        """Unknown version id -> error."""
        pro = _make_project(tmp_path)
        result = _run(self.fn(pro, "20260908_000000_000000", ctx=None))
        assert "error" in result
        assert "not found" in result["error"]

    def test_restore_missing_project_file(self, tmp_path):
        """Nonexistent .kicad_pro -> error."""
        pro = str(tmp_path / "nope.kicad_pro")
        result = _run(self.fn(pro, "20260908_000000_000000", ctx=None))
        assert "error" in result
        assert "File not found" in result["error"]

    @patch("kcaa.utils.version_manager.restore_project_version")
    def test_os_error(self, mock_restore, tmp_path):
        """When restore_project_version raises OSError."""
        pro = _make_project(tmp_path)
        mock_restore.side_effect = OSError("Permission denied")
        result = _run(self.fn(pro, "20260908_000000_000000", ctx=None))
        assert "error" in result
        assert "Failed to restore project version" in result["error"]


# ---------------------------------------------------------------------------
# save_project_version(include_files=...)
# ---------------------------------------------------------------------------


class TestSaveProjectVersionIncludeFiles:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["save_project_version"]

    @staticmethod
    def _nested_project(tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        pro = project_dir / "aria.kicad_pro"
        pro.write_text("{}")
        (project_dir / "aria.kicad_sch").write_text("root\n")
        hardware = project_dir / "hardware"
        hardware.mkdir()
        sheet = hardware / "main_bus.kicad_sch"
        sheet.write_text("nested\n")
        return pro, sheet

    def test_nested_sheet_archived_under_relative_path(self, tmp_path):
        pro, sheet = self._nested_project(tmp_path)

        result = _run(self.fn(str(pro), include_files=[str(sheet)], ctx=None))

        assert result["success"] is True
        assert "hardware/main_bus.kicad_sch" in result["files"]
        with tarfile.open(result["archive_path"], "r:gz") as tar:
            assert "hardware/main_bus.kicad_sch" in tar.getnames()

    def test_outside_entry_fails_the_tool_call(self, tmp_path):
        pro, _ = self._nested_project(tmp_path)
        outside = tmp_path / "outside.kicad_sch"
        outside.write_text("outside\n")

        result = _run(self.fn(str(pro), include_files=[str(outside)], ctx=None))

        assert "error" in result
        assert "outside.kicad_sch" in result["error"]
        assert "outside the project directory" in result["error"]
        assert not (pro.parent / ".versions").exists()

    def test_parent_traversal_entry_fails_the_tool_call(self, tmp_path):
        pro, _ = self._nested_project(tmp_path)
        outside = tmp_path / "outside.kicad_sch"
        outside.write_text("outside\n")
        traversal = os.path.join(str(pro.parent), "..", "outside.kicad_sch")

        result = _run(self.fn(str(pro), include_files=[traversal], ctx=None))

        assert "error" in result
        assert "outside the project directory" in result["error"]

    def test_missing_entry_fails_the_tool_call_without_an_archive(self, tmp_path):
        pro, sheet = self._nested_project(tmp_path)
        missing = str(pro.parent / "gone.kicad_sch")

        result = _run(self.fn(str(pro), include_files=[missing, str(sheet)], ctx=None))

        assert "error" in result
        assert missing in result["error"]
        assert "not an existing file" in result["error"]
        assert not (pro.parent / ".versions").exists()

    def test_default_set_duplicate_is_skipped(self, tmp_path):
        pro, _ = self._nested_project(tmp_path)
        root_sch = pro.parent / "aria.kicad_sch"

        result = _run(self.fn(str(pro), include_files=[str(root_sch)], ctx=None))

        assert result["success"] is True
        assert result["files"].count("aria.kicad_sch") == 1


class TestSaveProjectVersionStateMerge:
    """A published state record lets one explicit call cover the whole project."""

    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["save_project_version"]

    @staticmethod
    def _project(tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        pro = root / "two_ax_PCB.kicad_pro"
        pro.write_text("{}")
        (root / "two_ax_PCB.kicad_sch").write_text("root\n")
        (root / "two_ax_PCB.kicad_pcb").write_text("(kicad_pcb)\n")
        (root / "sym-lib-table").write_text("(sym_lib_table)\n")
        (root / "fp-lib-table").write_text("(fp_lib_table)\n")
        hardware = root / "hardware"
        hardware.mkdir()
        (hardware / "board2.kicad_pcb").write_text("(kicad_pcb)\n")
        subdirectory = root / "subdirectory"
        subdirectory.mkdir()
        (subdirectory / "SubSheet.kicad_sch").write_text("sub\n")
        for name in ("MotorCell.kicad_sch", "MotorCell_noPow.kicad_sch", "AuxHalfH.kicad_sch"):
            (root / name).write_text("sheet\n")
        return root, pro

    @staticmethod
    def _publish(monkeypatch, state_file, root, pro, **overrides):
        record = {
            "version": 2,
            "written_at": "2026-11-18T09:12:33Z",
            "project_root": str(root),
            "profile_file": pro.name,
            "pcb_files": ["two_ax_PCB.kicad_pcb", "hardware/board2.kicad_pcb"],
            "schematic_files": [
                "two_ax_PCB.kicad_sch",
                "MotorCell.kicad_sch",
                "MotorCell_noPow.kicad_sch",
                "AuxHalfH.kicad_sch",
                "subdirectory/SubSheet.kicad_sch",
            ],
        }
        record.update(overrides)
        state_file.write_text(json.dumps(record), encoding="utf-8")
        monkeypatch.setenv("KCAA_PROJECT_STATE_FILE", str(state_file))
        return state_file

    def test_matching_state_merges_every_board_and_sheet(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        self._publish(monkeypatch, tmp_path / "state.json", root, pro)

        result = _run(
            self.fn(
                str(pro),
                include_files=["sym-lib-table", "fp-lib-table", "subdirectory/SubSheet.kicad_sch"],
                ctx=None,
            )
        )

        assert result["success"] is True
        for rel in (
            "two_ax_PCB.kicad_pro",
            "two_ax_PCB.kicad_sch",
            "two_ax_PCB.kicad_pcb",
            "hardware/board2.kicad_pcb",
            "MotorCell.kicad_sch",
            "MotorCell_noPow.kicad_sch",
            "AuxHalfH.kicad_sch",
            "subdirectory/SubSheet.kicad_sch",
            "sym-lib-table",
            "fp-lib-table",
        ):
            assert rel in result["files"]
        with tarfile.open(result["archive_path"], "r:gz") as tar:
            names = set(tar.getnames())
        assert "subdirectory/SubSheet.kicad_sch" in names
        assert "hardware/board2.kicad_pcb" in names
        assert "MotorCell.kicad_sch" in names

    def test_mismatched_profile_does_not_merge(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        (root / "other.kicad_pro").write_text("{}")
        self._publish(
            monkeypatch,
            tmp_path / "state.json",
            root,
            pro,
            profile_file="other.kicad_pro",
        )

        result = _run(self.fn(str(pro), include_files=["sym-lib-table"], ctx=None))

        assert result["success"] is True
        assert "MotorCell.kicad_sch" not in result["files"]
        assert "hardware/board2.kicad_pcb" not in result["files"]

    def test_missing_state_file_does_not_merge(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        monkeypatch.delenv("KCAA_PROJECT_STATE_FILE", raising=False)

        result = _run(self.fn(str(pro), include_files=["sym-lib-table"], ctx=None))

        assert result["success"] is True
        assert "MotorCell.kicad_sch" not in result["files"]
        assert "sym-lib-table" in result["files"]

    def test_unreadable_state_file_does_not_merge_and_does_not_fail(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        monkeypatch.setenv("KCAA_PROJECT_STATE_FILE", str(tmp_path / "absent.json"))

        result = _run(self.fn(str(pro), include_files=["sym-lib-table"], ctx=None))

        assert result["success"] is True
        assert "MotorCell.kicad_sch" not in result["files"]

    def test_mismatched_root_does_not_merge(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        other = tmp_path / "elsewhere"
        other.mkdir()
        other_pro = other / "two_ax_PCB.kicad_pro"
        other_pro.write_text("{}")
        self._publish(monkeypatch, tmp_path / "state.json", other, other_pro)

        result = _run(self.fn(str(pro), include_files=["sym-lib-table"], ctx=None))

        assert result["success"] is True
        assert "MotorCell.kicad_sch" not in result["files"]

    def test_explicit_entry_already_in_the_manifest_appears_once(self, tmp_path, monkeypatch):
        root, pro = self._project(tmp_path)
        self._publish(monkeypatch, tmp_path / "state.json", root, pro)

        result = _run(self.fn(str(pro), include_files=["MotorCell.kicad_sch"], ctx=None))

        assert result["success"] is True
        assert result["files"].count("MotorCell.kicad_sch") == 1
