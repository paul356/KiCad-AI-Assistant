"""Tests for the plugin-published active-project state file."""

import json
import os

import pytest

from kcaa.utils.path_validator import PathValidator
from kicad_plugin.server_manager import ACTIVE_PROJECT_STATE_FILENAME
import kicad_plugin.ui.panel as panel
from kicad_plugin.ui.panel import (
    _project_root_for_publish,
    _project_state_for_tick,
    _write_active_project_state,
)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestWriteActiveProjectState:
    def test_writes_project_root(self, tmp_path):
        path = _write_active_project_state(str(tmp_path), "/home/user/MyProject")

        assert path == os.path.join(str(tmp_path), ACTIVE_PROJECT_STATE_FILENAME)
        assert _read(path) == {"project_root": "/home/user/MyProject"}

    def test_writes_null_when_no_project(self, tmp_path):
        path = _write_active_project_state(str(tmp_path), None)

        assert _read(path) == {"project_root": None}

    def test_replaces_atomically(self, tmp_path):
        path = _write_active_project_state(str(tmp_path), "/old/project")
        inode_before = os.stat(path).st_ino

        _write_active_project_state(str(tmp_path), "/new/project")

        assert _read(path) == {"project_root": "/new/project"}
        # os.replace swaps the directory entry rather than truncating in place.
        assert os.stat(path).st_ino != inode_before
        # No temp file is left behind.
        assert sorted(p.name for p in tmp_path.iterdir()) == [ACTIVE_PROJECT_STATE_FILENAME]

    def test_creates_config_dir(self, tmp_path):
        config_dir = tmp_path / "nested" / "config"
        path = _write_active_project_state(str(config_dir), None)

        assert os.path.isfile(path)

    def test_replace_retries_transient_permission_error(self, tmp_path, monkeypatch):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("reader holds the file")
            return real_replace(src, dst)

        monkeypatch.setattr(panel.os, "replace", flaky_replace)
        monkeypatch.setattr(panel.time, "sleep", lambda _seconds: None)

        path = _write_active_project_state(str(tmp_path), "/project")

        assert calls["n"] == 3
        assert _read(path) == {"project_root": "/project"}

    def test_replace_reraises_after_bounded_retries(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def always_busy(src, dst):
            calls["n"] += 1
            raise PermissionError("reader holds the file")

        monkeypatch.setattr(panel.os, "replace", always_busy)
        monkeypatch.setattr(panel.time, "sleep", lambda _seconds: None)

        with pytest.raises(PermissionError):
            _write_active_project_state(str(tmp_path), "/project")

        assert calls["n"] == panel._REPLACE_ATTEMPTS
        # The temp file is cleaned up on final failure; nothing was published.
        assert list(tmp_path.iterdir()) == []


class TestPublishContract:
    """The panel publishes the project *directory*, not the .kicad_pro file."""

    def test_publishes_parent_directory_and_validates_siblings(self, tmp_path):
        project_file = tmp_path / "board.kicad_pro"
        project_file.write_text("{}", encoding="utf-8")
        schematic = tmp_path / "board.kicad_sch"
        schematic.write_text("", encoding="utf-8")

        root = _project_root_for_publish(str(project_file))
        assert root == os.path.dirname(str(project_file))

        state_path = _write_active_project_state(str(tmp_path / "cfg"), root)
        published = _read(state_path)
        assert published == {"project_root": str(tmp_path)}

        validator = PathValidator()
        validator.set_project_root(published["project_root"])
        assert validator.validate_path(str(schematic), must_exist=True) == os.path.realpath(
            schematic
        )

    def test_no_project_publishes_null(self):
        assert _project_root_for_publish(None) is None


class TestProjectTickTransitions:
    """Debounced null publishing when the project is closed."""

    PROJECT = "/home/user/MyProject/board.kicad_pro"

    def test_project_then_two_none_ticks_publishes_null(self):
        ticks, root, publish = _project_state_for_tick(self.PROJECT, 0, None)
        assert (ticks, root, publish) == (1, None, False)

        ticks, root, publish = _project_state_for_tick(self.PROJECT, ticks, None)
        assert (ticks, root, publish) == (2, None, True)

    def test_project_then_one_none_tick_then_project_does_not_publish_null(self):
        ticks, _root, publish = _project_state_for_tick(self.PROJECT, 0, None)
        assert publish is False

        ticks, root, publish = _project_state_for_tick(self.PROJECT, ticks, self.PROJECT)
        assert (ticks, root, publish) == (0, None, False)

    def test_reopening_after_cleared_root_republishes_directory(self):
        ticks, root, publish = _project_state_for_tick(self.PROJECT, 2, self.PROJECT)
        assert ticks == 0
        assert publish is True
        assert root == os.path.dirname(self.PROJECT)

    def test_switch_publishes_nothing_from_the_helper(self):
        other = "/home/user/Other/other.kicad_pro"
        ticks, root, publish = _project_state_for_tick(self.PROJECT, 0, other)
        assert (ticks, root, publish) == (0, None, False)
