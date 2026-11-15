"""Tests for project archive include-file support in version_manager."""

import os
import tarfile

import pytest

from kcaa.utils import version_manager


def _project(tmp_path):
    """Create aria project with root sheet and a nested hardware sheet."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    pro = project_dir / "aria.kicad_pro"
    pro.write_text("{}\n")
    root_sch = project_dir / "aria.kicad_sch"
    root_sch.write_text("root v1\n")
    hardware = project_dir / "hardware"
    hardware.mkdir()
    sheet = hardware / "main_bus.kicad_sch"
    sheet.write_text("nested v1\n")
    return pro, root_sch, sheet


class TestProjectRelativePath:
    def test_nested_file_is_relative_to_project(self, tmp_path):
        pro, _, sheet = _project(tmp_path)

        assert version_manager.project_relative_path(str(pro), str(sheet)) == (
            "hardware/main_bus.kicad_sch"
        )

    def test_parent_traversal_is_rejected(self, tmp_path):
        pro, _, _ = _project(tmp_path)
        outside = tmp_path / "outside.kicad_sch"
        outside.write_text("outside\n")

        with pytest.raises(ValueError, match="outside the project directory"):
            version_manager.project_relative_path(str(pro), str(outside))
        with pytest.raises(ValueError, match="outside the project directory"):
            version_manager.project_relative_path(
                str(pro), os.path.join(os.path.dirname(str(pro)), "..", "outside.kicad_sch")
            )

    def test_absolute_outside_is_rejected(self, tmp_path):
        pro, _, _ = _project(tmp_path)
        outside = tmp_path / "elsewhere.kicad_sch"
        outside.write_text("outside\n")

        with pytest.raises(ValueError, match="outside the project directory"):
            version_manager.project_relative_path(str(pro), str(outside))

    def test_symlink_escape_is_rejected(self, tmp_path):
        pro, _, _ = _project(tmp_path)
        outside = tmp_path / "linked.kicad_sch"
        outside.write_text("outside\n")
        link = tmp_path / "proj" / "hardware" / "link.kicad_sch"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")

        with pytest.raises(ValueError, match="outside the project directory"):
            version_manager.project_relative_path(str(pro), str(link))

    def test_missing_file_is_rejected(self, tmp_path):
        pro, _, _ = _project(tmp_path)

        with pytest.raises(ValueError, match="not an existing file"):
            version_manager.project_relative_path(str(pro), str(pro.parent / "gone.kicad_sch"))


class TestProjectFileSet:
    def test_include_adds_nested_sheet(self, tmp_path):
        pro, root_sch, sheet = _project(tmp_path)

        files = version_manager.project_file_set(str(pro), include_files=[str(sheet)])

        assert files["hardware/main_bus.kicad_sch"] == str(sheet)
        assert files["aria.kicad_sch"] == str(root_sch)
        assert files["aria.kicad_pro"] == str(pro)

    def test_default_set_entry_is_skipped(self, tmp_path):
        pro, root_sch, _ = _project(tmp_path)

        files = version_manager.project_file_set(str(pro), include_files=[str(root_sch)])

        assert files["aria.kicad_sch"] == str(root_sch)
        assert list(files).count("aria.kicad_sch") == 1


class TestSaveAndRestoreWithIncludeFiles:
    def test_archive_contains_nested_sheet_under_relative_path(self, tmp_path):
        pro, _, sheet = _project(tmp_path)

        archive = version_manager.save_project_version(str(pro), include_files=[str(sheet)])

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert "hardware/main_bus.kicad_sch" in names
        manifest = version_manager._read_project_manifest(archive)
        assert "hardware/main_bus.kicad_sch" in manifest["files"]

    def test_restore_round_trips_edited_nested_sheet(self, tmp_path):
        pro, _, sheet = _project(tmp_path)
        archive = version_manager.save_project_version(str(pro), include_files=[str(sheet)])
        version_id = version_manager.project_version_id(archive, str(pro))

        sheet.write_text("nested v2\n")
        result = version_manager.restore_project_version(str(pro), version_id)

        assert sheet.read_text() == "nested v1\n"
        assert "hardware/main_bus.kicad_sch" in result["files"]
