"""Tests for project archive include-file support in version_manager."""

import io
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

    def test_pre_restore_backup_covers_nested_sheet(self, tmp_path):
        pro, _, sheet = _project(tmp_path)
        archive = version_manager.save_project_version(str(pro), include_files=[str(sheet)])
        version_id = version_manager.project_version_id(archive, str(pro))
        sheet.write_text("nested v2\n")

        result = version_manager.restore_project_version(str(pro), version_id)

        with tarfile.open(result["backup_of_current"], "r:gz") as tar:
            assert "hardware/main_bus.kicad_sch" in tar.getnames()
        assert sheet.read_text() == "nested v1\n"

        version_manager.restore_project_version(str(pro), result["backup_version_id"])
        assert sheet.read_text() == "nested v2\n"

    def test_backup_skips_archived_member_missing_from_disk(self, tmp_path):
        pro, _, sheet = _project(tmp_path)
        archive = version_manager.save_project_version(str(pro), include_files=[str(sheet)])
        version_id = version_manager.project_version_id(archive, str(pro))
        sheet.unlink()

        result = version_manager.restore_project_version(str(pro), version_id)

        with tarfile.open(result["backup_of_current"], "r:gz") as tar:
            assert "hardware/main_bus.kicad_sch" not in tar.getnames()
        assert sheet.read_text() == "nested v1\n"


def _write_archive(project_file, version_id, members, *, manifest=True):
    """Write a project archive directly, optionally without a manifest."""
    archive_dir = os.path.join(os.path.dirname(project_file), ".versions", "project")
    os.makedirs(archive_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(project_file))[0]
    archive_path = os.path.join(archive_dir, f"{stem}.project.{version_id}.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        if manifest:
            data = b'{"version": 1, "files": {}}'
            info = tarfile.TarInfo(".manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return archive_path


class TestRestoreSafety:
    def test_restore_refuses_escaping_member_and_backs_up_safe_one(self, tmp_path):
        pro, root_sch, _ = _project(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (pro.parent / "link").symlink_to(outside)
        version_id = "20260101_000000_000000"
        archive = _write_archive(
            str(pro),
            version_id,
            {
                "aria.kicad_sch": b"restored\n",
                "link/evil.kicad_sch": b"evil\n",
            },
        )
        assert os.path.isfile(archive)

        result = version_manager.restore_project_version(str(pro), version_id)

        assert result["backup_skipped"] == 1
        assert result["restored_from"] == version_id
        assert root_sch.read_text() == "restored\n"
        assert not (outside / "evil.kicad_sch").exists()
        with tarfile.open(result["backup_of_current"], "r:gz") as tar:
            names = tar.getnames()
        assert "aria.kicad_sch" in names
        assert "link/evil.kicad_sch" not in names

    def test_restore_refuses_dotdot_member_that_normalizes_inside(self, tmp_path):
        pro, root_sch, _ = _project(tmp_path)
        version_id = "20260103_000000_000000"
        _write_archive(
            str(pro),
            version_id,
            {
                "aria.kicad_sch": b"safe\n",
                "hardware/../aria.kicad_sch": b"traversal\n",
            },
        )

        result = version_manager.restore_project_version(str(pro), version_id)

        assert result["backup_skipped"] == 1
        assert root_sch.read_text() == "safe\n"

    def test_restore_handles_legacy_archive_without_manifest(self, tmp_path):
        pro, root_sch, _ = _project(tmp_path)
        version_id = "20260102_000000_000000"
        _write_archive(
            str(pro),
            version_id,
            {"aria.kicad_sch": b"legacy restored\n"},
            manifest=False,
        )

        result = version_manager.restore_project_version(str(pro), version_id)

        assert result["restored_from"] == version_id
        assert root_sch.read_text() == "legacy restored\n"

    def test_restore_does_not_prune_its_source_archive(self, tmp_path):
        pro, root_sch, _ = _project(tmp_path)
        version_ids = []
        for index in range(11):
            root_sch.write_text(f"v{index}\n")
            archive = version_manager.save_project_version(str(pro), keep=100)
            version_ids.append(version_manager.project_version_id(archive, str(pro)))
        source_id = version_ids[0]
        archive_dir = os.path.join(os.path.dirname(pro), ".versions", "project")
        source_archive = os.path.join(archive_dir, f"aria.project.{source_id}.tar.gz")
        assert os.path.isfile(source_archive)

        # Make the current state differ so the safety backup creates a new
        # archive and runs the pruning pass.
        root_sch.write_text("current\n")
        version_manager.restore_project_version(str(pro), source_id)

        assert os.path.isfile(source_archive)
        assert len(version_manager.list_project_versions(str(pro))) == 10
