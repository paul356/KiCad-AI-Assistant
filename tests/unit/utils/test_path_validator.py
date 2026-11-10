"""
Tests for path validation utility.

The trust roots are the open project (when published through the plugin's
state file), the configured KiCad directories, and the server's private temp
subdirectory. The process working directory and the rest of the kcaa data
directory are deliberately not roots.
"""

import json
import os
import stat
import tempfile

import pytest

from kcaa.utils.config import config
from kcaa.utils.path_validator import (
    PathValidationError,
    PathValidator,
    get_default_validator,
    validate_directory,
    validate_kicad_file,
    validate_path,
)


def _inside_kicad_user_dir(*parts: str) -> str:
    return os.path.join(config.kicad_user_dir, *parts)


def _write_state(state_file: str, project_root: str | None) -> None:
    """Write the plugin-published state file (no timestamp manipulation)."""
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"project_root": project_root}, f)


def _make_state_validator(monkeypatch, state_file: str) -> PathValidator:
    monkeypatch.setenv("KCAA_PROJECT_STATE_FILE", state_file)
    return PathValidator()


class TestDefaultRoots:
    """Default roots: KiCad directories only — never the cwd."""

    def test_no_project_root_by_default(self):
        validator = PathValidator()
        assert validator._project_root is None

    def test_path_under_kicad_user_dir_validates(self):
        validator = PathValidator()
        path = _inside_kicad_user_dir("kcaa_test_project", "board.kicad_pcb")
        assert validator.validate_path(path) == os.path.realpath(path)

    def test_cwd_is_not_a_trust_root(self):
        validator = PathValidator()
        assert os.path.realpath(os.getcwd()) not in validator.trusted_roots
        path = os.path.join(os.getcwd(), "not_in_any_kicad_dir.txt")
        with pytest.raises(PathValidationError, match="outside trusted directories"):
            validator.validate_path(path)

    def test_init_with_custom_trusted_roots(self):
        roots = {"/tmp", "/home/user"}
        validator = PathValidator(trusted_roots=roots)

        # Explicit roots are added on top of the KiCad directory roots.
        expected_roots = {os.path.realpath(root) for root in roots}
        assert expected_roots <= validator.trusted_roots

    def test_default_roots_include_configured_kicad_dirs(self):
        validator = PathValidator()
        for root in (
            config.kicad_user_dir,
            config.kicad_symbol_dir,
            config.kicad_footprint_dir,
            config.kicad_3rd_party,
            *config.additional_search_paths,
        ):
            if root:
                assert os.path.realpath(root) in validator.trusted_roots


class TestDataDirectoryBoundary:
    """Only <data dir>/tmp is trusted, never the data directory itself."""

    def test_private_temp_dir_trusted_but_not_data_dir(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "kcaa_data"
        data_dir.mkdir()
        settings_file = data_dir / "settings.json"
        settings_file.write_text("{}", encoding="utf-8")
        temp_dir = data_dir / "tmp"
        temp_dir.mkdir()
        temp_file = temp_dir / "erc_report.json"
        temp_file.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(config, "get_kcaa_data_dir", lambda: str(data_dir))
        validator = PathValidator()

        assert validator.validate_path(str(temp_file), must_exist=True) == str(temp_file)
        with pytest.raises(PathValidationError, match="outside trusted directories"):
            validator.validate_path(str(settings_file), must_exist=True)

    def test_symlinked_temp_dir_is_not_a_trust_root(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "kcaa_data"
        data_dir.mkdir()
        target = tmp_path / "symlink_target"
        target.mkdir()
        trusted_file = target / "secret.kicad_sch"
        trusted_file.write_text("", encoding="utf-8")
        os.symlink(target, data_dir / "tmp")

        monkeypatch.setattr(config, "get_kcaa_data_dir", lambda: str(data_dir))
        validator = PathValidator()

        assert os.path.realpath(target) not in validator.trusted_roots
        with pytest.raises(PathValidationError, match="outside trusted directories"):
            validator.validate_path(str(trusted_file), must_exist=True)


class TestProjectRoot:
    """The project root is optional, single, and replaceable in place."""

    def test_project_root_grants_access(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        schematic = project / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        validator = PathValidator()
        with pytest.raises(PathValidationError):
            validator.validate_path(str(schematic))

        validator.set_project_root(str(project))
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)

    def test_set_project_root_twice_drops_the_old_root(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        first_file = first / "a.kicad_sch"
        second_file = second / "b.kicad_sch"
        first_file.write_text("", encoding="utf-8")
        second_file.write_text("", encoding="utf-8")

        validator = PathValidator()
        validator.set_project_root(str(first))
        assert validator.validate_path(str(first_file), must_exist=True) == str(first_file)

        validator.set_project_root(str(second))
        assert validator.validate_path(str(second_file), must_exist=True) == str(second_file)
        with pytest.raises(PathValidationError):
            validator.validate_path(str(first_file))

    def test_set_project_root_none_clears_it(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        schematic = project / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        validator = PathValidator()
        validator.set_project_root(str(project))
        validator.set_project_root(None)

        assert validator._project_root is None
        with pytest.raises(PathValidationError):
            validator.validate_path(str(schematic))
        # KiCad directory matching still works.
        assert validator.validate_path(_inside_kicad_user_dir("x.kicad_pcb"))

    def test_set_project_root_rejects_regular_file(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        project_file = project / "board.kicad_pro"
        project_file.write_text("{}", encoding="utf-8")

        validator = PathValidator()
        with pytest.raises(PathValidationError, match="must be a directory"):
            validator.set_project_root(str(project_file))
        assert validator._project_root is None


class TestStateFile:
    """Plugin-published project root: read on validate, keep current on error."""

    def test_new_project_path_picked_up_on_next_validate(self, tmp_path, monkeypatch):
        """A fresh state value wins even when the file's mtime is unchanged."""
        state_file = tmp_path / "active_project.json"
        project_a = tmp_path / "a"
        project_b = tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()
        file_a = project_a / "a.kicad_sch"
        file_b = project_b / "b.kicad_sch"
        file_a.write_text("", encoding="utf-8")
        file_b.write_text("", encoding="utf-8")

        _write_state(str(state_file), str(project_a))
        validator = _make_state_validator(monkeypatch, str(state_file))
        assert validator.validate_path(str(file_a), must_exist=True) == str(file_a)

        # Rewrite with a new value, then restore the mtime an mtime-caching
        # implementation would have stored: only re-reading can see it.
        before = os.stat(state_file)
        _write_state(str(state_file), str(project_b))
        os.utime(state_file, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert validator.validate_path(str(file_b), must_exist=True) == str(file_b)
        with pytest.raises(PathValidationError):
            validator.validate_path(str(file_a))

    def test_null_clears_project_root(self, tmp_path, monkeypatch):
        state_file = tmp_path / "active_project.json"
        project = tmp_path / "project"
        project.mkdir()
        schematic = project / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        _write_state(str(state_file), str(project))
        validator = _make_state_validator(monkeypatch, str(state_file))
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)

        _write_state(str(state_file), None)
        with pytest.raises(PathValidationError):
            validator.validate_path(str(schematic))
        assert validator._project_root is None

    def test_malformed_json_keeps_previous_root(self, tmp_path, monkeypatch):
        state_file = tmp_path / "active_project.json"
        project = tmp_path / "project"
        project.mkdir()
        schematic = project / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        _write_state(str(state_file), str(project))
        validator = _make_state_validator(monkeypatch, str(state_file))
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)

        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{not json")

        # Re-read fails -> previous root kept, no exception escapes.
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)
        assert validator._project_root == os.path.realpath(project)

    def test_missing_state_file_keeps_previous_root(self, tmp_path, monkeypatch):
        state_file = tmp_path / "active_project.json"
        project = tmp_path / "project"
        project.mkdir()
        schematic = project / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        _write_state(str(state_file), str(project))
        validator = _make_state_validator(monkeypatch, str(state_file))
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)

        os.unlink(state_file)
        assert validator.validate_path(str(schematic), must_exist=True) == str(schematic)


class TestContainment:
    """Traversal and symlink escapes are rejected."""

    def test_parent_traversal_rejected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        validator = PathValidator()
        validator.set_project_root(str(project))

        traversal = os.path.join(str(project), "..", "outside", "secret.kicad_sch")
        with pytest.raises(PathValidationError, match="outside trusted directories"):
            validator.validate_path(traversal)

    def test_escaping_symlink_rejected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        outside_file = tmp_path / "outside.kicad_sch"
        outside_file.write_text("", encoding="utf-8")
        link = project / "link.kicad_sch"
        os.symlink(outside_file, link)

        validator = PathValidator()
        validator.set_project_root(str(project))

        with pytest.raises(PathValidationError, match="outside trusted directories"):
            validator.validate_path(str(link), must_exist=True)


class TestCreateSafeTempPath:
    """Server temp files work with no project root and stay private."""

    def test_works_without_project_root(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "kcaa_data"
        monkeypatch.setattr(config, "get_kcaa_data_dir", lambda: str(data_dir))

        validator = PathValidator()
        assert validator._project_root is None
        temp_path = validator.create_safe_temp_path("erc", ".json")

        temp_dir = data_dir / "tmp"
        assert os.path.dirname(temp_path) == str(temp_dir)
        assert os.path.isfile(temp_path)
        assert temp_path.endswith(".json")
        assert stat.S_IMODE(os.stat(temp_dir).st_mode) == 0o700

        os.unlink(temp_path)

    def test_symlinked_temp_dir_refused(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "kcaa_data"
        data_dir.mkdir()
        target = tmp_path / "elsewhere"
        target.mkdir()
        os.symlink(target, data_dir / "tmp")
        monkeypatch.setattr(config, "get_kcaa_data_dir", lambda: str(data_dir))

        validator = PathValidator()
        with pytest.raises(PathValidationError, match="symlinked temp directory"):
            validator.create_safe_temp_path("erc", ".json")


class TestValidatePathBasics:
    """Existing validation semantics are unchanged."""

    def test_validate_path_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            test_file = os.path.join(temp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            result = validator.validate_path(test_file, must_exist=True)
            assert result == os.path.realpath(test_file)

    def test_validate_path_empty_string(self):
        validator = PathValidator()
        with pytest.raises(PathValidationError, match="non-empty string"):
            validator.validate_path("")

    def test_validate_path_none(self):
        validator = PathValidator()
        with pytest.raises(PathValidationError, match="non-empty string"):
            validator.validate_path(None)

    def test_validate_path_nonexistent_when_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            nonexistent_file = os.path.join(temp_dir, "nonexistent.txt")

            with pytest.raises(PathValidationError, match="does not exist"):
                validator.validate_path(nonexistent_file, must_exist=True)

    def test_validate_kicad_file_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            project_file = os.path.join(temp_dir, "test.kicad_pro")
            with open(project_file, "w") as f:
                f.write("{}")

            result = validator.validate_kicad_file(project_file, "project")
            assert result == os.path.realpath(project_file)

    def test_validate_kicad_file_wrong_extension(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            wrong_file = os.path.join(temp_dir, "test.txt")
            with open(wrong_file, "w") as f:
                f.write("test")

            with pytest.raises(PathValidationError, match="must have .kicad_pro extension"):
                validator.validate_kicad_file(wrong_file, "project")

    def test_validate_kicad_file_unknown_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            test_file = os.path.join(temp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            with pytest.raises(PathValidationError, match="Unknown KiCad file type"):
                validator.validate_kicad_file(test_file, "unknown_type")

    def test_validate_directory_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            sub_dir = os.path.join(temp_dir, "subdir")
            os.makedirs(sub_dir)

            result = validator.validate_directory(sub_dir)
            assert result == os.path.realpath(sub_dir)

    def test_validate_directory_not_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            test_file = os.path.join(temp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            with pytest.raises(PathValidationError, match="not a directory"):
                validator.validate_directory(test_file)

    def test_validate_project_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            project_file = os.path.join(temp_dir, "test.kicad_pro")
            with open(project_file, "w") as f:
                f.write("{}")

            result = validator.validate_project_directory(project_file)
            assert result == os.path.realpath(temp_dir)

    def test_symlink_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            validator = PathValidator(trusted_roots={temp_dir})
            real_file = os.path.join(temp_dir, "real.txt")
            link_file = os.path.join(temp_dir, "link.txt")

            with open(real_file, "w") as f:
                f.write("test")
            os.symlink(real_file, link_file)

            real_result = validator.validate_path(real_file, must_exist=True)
            link_result = validator.validate_path(link_file, must_exist=True)

            assert real_result == link_result == os.path.realpath(real_file)


class TestConvenienceFunctions:
    """Test convenience functions."""

    @pytest.fixture(autouse=True)
    def _clear_project_root(self):
        yield
        get_default_validator().set_project_root(None)

    def test_validate_path_convenience(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            get_default_validator().set_project_root(temp_dir)

            test_file = os.path.join(temp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            result = validate_path(test_file, must_exist=True)
            assert result == os.path.realpath(test_file)

    def test_validate_kicad_file_convenience(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            get_default_validator().set_project_root(temp_dir)

            project_file = os.path.join(temp_dir, "test.kicad_pro")
            with open(project_file, "w") as f:
                f.write("{}")

            result = validate_kicad_file(project_file, "project")
            assert result == os.path.realpath(project_file)

    def test_validate_directory_convenience(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            get_default_validator().set_project_root(temp_dir)

            result = validate_directory(temp_dir)
            assert result == os.path.realpath(temp_dir)
