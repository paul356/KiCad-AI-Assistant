"""Tests for deterministic KiCad CLI detection across platforms."""

import os
from pathlib import Path
import platform
import shutil

import pytest

from kcaa.utils.config import config
from kcaa.utils.kicad_cli import KiCadCLIError, KiCadCLIManager


@pytest.fixture(autouse=True)
def _isolate_detection(monkeypatch):
    """Keep detection independent of the host environment."""
    for name in ("KICAD_APP_PATH", "APPDIR", "KICAD_CLI_PATH", "KCAA_KICAD_CLI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "_kicad_app_path", "")
    monkeypatch.setattr(config, "_kicad_version", "10.0")


@pytest.fixture
def windows_env(tmp_path, monkeypatch):
    """Simulate Windows with ProgramFiles pointing at tmp_path."""
    program_files = tmp_path / "ProgramFiles"
    program_files.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    for name in ("ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    return program_files


@pytest.fixture
def linux_env(monkeypatch):
    """Simulate Linux with no platform default roots."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")


def _make_cli(path: Path) -> Path:
    """Create an empty file standing in for kicad-cli."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def _patch_validation(manager: KiCadCLIManager, monkeypatch, valid_paths: list[str] | None):
    """Patch validation so only the given paths succeed (None = all succeed)."""
    valid = set(valid_paths) if valid_paths is not None else None
    calls: list[str] = []

    def fake_validate(path: str) -> bool:
        calls.append(path)
        return True if valid is None else path in valid

    monkeypatch.setattr(manager, "_validate_cli_path", fake_validate)
    return calls


def test_config_bin_path_wins_over_path(tmp_path, windows_env, monkeypatch):
    """The config-derived <app>/bin CLI is probed before PATH."""
    app_root = tmp_path / "app"
    config_exe = _make_cli(app_root / "bin" / "kicad-cli.exe")
    path_exe = _make_cli(tmp_path / "onpath" / "kicad-cli.exe")
    monkeypatch.setattr(config, "_kicad_app_path", str(app_root))
    monkeypatch.setattr(shutil, "which", lambda name: str(path_exe))
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(config_exe)
    assert diagnosis.candidates[0].source == "config"
    assert calls == [str(config_exe)]


def test_windows_default_root_uses_known_version(windows_env, monkeypatch):
    """%ProgramFiles%\\KiCad\\<ver>\\bin is the canonical Windows candidate."""
    kicad_root = windows_env / "KiCad"
    exe = _make_cli(kicad_root / "10.0" / "bin" / "kicad-cli.exe")
    manager = KiCadCLIManager()

    assert manager._get_common_installation_paths() == [str(exe)]

    _patch_validation(manager, monkeypatch, None)
    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(exe)
    assert diagnosis.candidates[0].source == "common"


def test_unversioned_windows_bin_is_ignored(windows_env, monkeypatch):
    """A version-less <root>\\bin CLI must never be used."""
    unversioned = _make_cli(windows_env / "KiCad" / "bin" / "kicad-cli.exe")
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    common_paths = manager._get_common_installation_paths()

    assert str(unversioned) not in common_paths
    assert manager.find_kicad_cli() is None
    assert str(unversioned) not in [candidate.path for candidate in manager._last_candidates]


def test_all_windows_roots_use_known_version(windows_env, monkeypatch):
    """Each Windows root contributes <root>\\<ver>\\bin only."""
    x86 = windows_env.parent / "ProgramFilesX86"
    w6432 = windows_env.parent / "ProgramW6432"
    local = windows_env.parent / "LocalAppData"
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    monkeypatch.setenv("ProgramW6432", str(w6432))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()

    assert paths == [
        os.path.join(str(windows_env / "KiCad"), "10.0", "bin", "kicad-cli.exe"),
        os.path.join(str(x86 / "KiCad"), "10.0", "bin", "kicad-cli.exe"),
        os.path.join(str(w6432 / "KiCad"), "10.0", "bin", "kicad-cli.exe"),
        os.path.join(str(local / "Programs" / "KiCad"), "10.0", "bin", "kicad-cli.exe"),
    ]


def test_invalid_candidate_does_not_abort_search(windows_env, monkeypatch):
    """A candidate that fails --version falls through to the next root."""
    first = _make_cli(windows_env / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    x86 = windows_env.parent / "ProgramFilesX86"
    second = _make_cli(x86 / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [str(second)])

    assert manager.find_kicad_cli() == str(second)
    assert calls == [str(first), str(second)]


def test_macos_common_bundle_path(monkeypatch):
    """macOS has a single version-less bundle location."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    manager = KiCadCLIManager()

    assert manager._get_common_installation_paths() == [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    ]


def test_linux_has_no_hardcoded_common_paths(linux_env):
    """Linux relies on config derivation and PATH, not fixed paths."""
    assert KiCadCLIManager()._get_common_installation_paths() == []


def test_config_path_requires_existing_file(tmp_path, linux_env, monkeypatch):
    """A configured root without a real CLI contributes no candidate."""
    app_root = tmp_path / "app"
    app_root.mkdir()
    monkeypatch.setattr(config, "_kicad_app_path", str(app_root))
    manager = KiCadCLIManager()

    assert manager._get_config_installation_paths() == []

    _make_cli(app_root / "bin" / "kicad-cli")
    assert manager._get_config_installation_paths() == [str(app_root / "bin" / "kicad-cli")]


def test_path_lookup_is_used_when_config_and_common_are_empty(tmp_path, linux_env, monkeypatch):
    """PATH remains the fallback for Linux distribution installs."""
    exe = _make_cli(tmp_path / "onpath" / "kicad-cli")
    monkeypatch.setattr(shutil, "which", lambda name: str(exe))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(exe)
    assert diagnosis.candidates[0].source == "path"


def test_error_message_lists_candidates_and_advises_app_path(linux_env, monkeypatch):
    """Detection failure reports candidates and the KICAD_APP_PATH remedy."""
    manager = KiCadCLIManager()
    candidates = [f"/nonexistent/candidate-{index}" for index in range(12)]
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: list(candidates))
    _patch_validation(manager, monkeypatch, [])

    with pytest.raises(KiCadCLIError) as excinfo:
        manager.get_cli_path()

    message = str(excinfo.value)
    assert message.startswith("KiCad CLI not found.")
    assert "KICAD_APP_PATH" in message
    assert candidates[0] in message
    assert candidates[10] not in message
    assert "Tried 12 candidate location(s)" in message
    assert "and 2 more" in message


def test_diagnose_reports_selection_and_sources(windows_env, monkeypatch):
    """diagnose() exposes probed candidates and the accepted path."""
    exe = _make_cli(windows_env / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.platform == "Windows"
    assert diagnosis.selected == str(exe)
    assert [(candidate.source, candidate.path) for candidate in diagnosis.candidates] == [
        ("common", str(exe))
    ]
    assert diagnosis.candidates[0].validated is True


def test_detection_is_cached_and_force_refresh_reprobes(windows_env, monkeypatch):
    """Repeated lookups reuse the cache; force_refresh probes again."""
    exe = _make_cli(windows_env / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, None)

    assert manager.find_kicad_cli() == str(exe)
    assert manager.find_kicad_cli() == str(exe)
    assert calls == [str(exe)]

    assert manager.find_kicad_cli(force_refresh=True) == str(exe)
    assert calls == [str(exe), str(exe)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell scripts are used")
class TestValidateCliPath:
    """Exercise the real subprocess validation, not a replacement."""

    @staticmethod
    def _script(tmp_path: Path, body: str) -> Path:
        script = tmp_path / "fake-kicad-cli"
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        return script

    def test_success_returns_true(self, tmp_path):
        script = self._script(tmp_path, "echo 10.0.0")
        assert KiCadCLIManager()._validate_cli_path(str(script)) is True

    def test_nonzero_exit_returns_false(self, tmp_path):
        script = self._script(tmp_path, "exit 3")
        assert KiCadCLIManager()._validate_cli_path(str(script)) is False

    def test_timeout_returns_false(self, tmp_path, monkeypatch):
        script = self._script(tmp_path, "sleep 5")
        monkeypatch.setattr(
            config,
            "_TIMEOUT_CONSTANTS",
            {**config._TIMEOUT_CONSTANTS, "kicad_cli_version_check": 0.05},
        )
        assert KiCadCLIManager()._validate_cli_path(str(script)) is False

    def test_missing_executable_returns_false(self, tmp_path):
        assert KiCadCLIManager()._validate_cli_path(str(tmp_path / "absent")) is False

    def test_undecodable_output_returns_false(self, monkeypatch):
        """Decoding errors are treated as validation failure on any locale."""

        def raise_unicode_error(*args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr("kcaa.utils.kicad_cli.subprocess.run", raise_unicode_error)
        assert KiCadCLIManager()._validate_cli_path("/virtual/kicad-cli") is False
