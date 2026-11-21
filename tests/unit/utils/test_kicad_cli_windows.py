"""Tests for Windows KiCad CLI discovery in versioned install directories."""

import os
from pathlib import Path
import platform
import shutil
import sys

import pytest

from kcaa.utils.kicad_cli import KiCadCLIError, KiCadCLIManager

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
WOW6432_UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
HKCU_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

UNVERSIONED_WINDOWS_PATHS = [
    r"C:\Program Files\KiCad\bin\kicad-cli.exe",
    r"C:\Program Files (x86)\KiCad\bin\kicad-cli.exe",
    r"C:\KiCad\bin\kicad-cli.exe",
]


def _make_install(root: Path, versions: list[str]) -> dict[str, Path]:
    """Create ``root/<version>/bin/kicad-cli.exe`` files and return their paths."""
    installed: dict[str, Path] = {}
    for version in versions:
        bin_dir = root / version / "bin"
        bin_dir.mkdir(parents=True)
        exe = bin_dir / "kicad-cli.exe"
        exe.touch()
        installed[version] = exe
    return installed


@pytest.fixture
def windows_env(tmp_path, monkeypatch):
    """Simulate a Windows environment with ProgramFiles pointing at tmp_path."""
    program_files = tmp_path / "ProgramFiles"
    program_files.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return program_files


def _patch_validation(manager: KiCadCLIManager, monkeypatch, valid_paths: list[Path] | None):
    """Patch validation so only the given paths succeed (None = all succeed)."""
    valid = {str(path) for path in valid_paths} if valid_paths is not None else None
    calls: list[str] = []

    def fake_validate(path: str) -> bool:
        calls.append(path)
        return True if valid is None else path in valid

    monkeypatch.setattr(manager, "_validate_cli_path", fake_validate)
    return calls


class _FakeWinreg:
    """Minimal winreg stand-in exposing one KiCad uninstall entry."""

    HKEY_LOCAL_MACHINE = object()
    HKEY_CURRENT_USER = object()

    def __init__(self, install_location: str, display_name: str = "KiCad 10.0"):
        self.install_location = install_location
        self.display_name = display_name
        self.closed: list[object] = []

    def OpenKey(self, hive, path):
        if path == UNINSTALL_KEY:
            return ("uninstall", hive)
        if path == UNINSTALL_KEY + "\\KiCad 10.0":
            return ("product", hive)
        raise FileNotFoundError(path)

    def EnumKey(self, key, index):
        if key[0] == "uninstall" and index == 0:
            return "KiCad 10.0"
        raise OSError("no more data")

    def QueryValueEx(self, key, name):
        if key[0] != "product":
            raise OSError("unexpected key")
        if name == "DisplayName":
            return (self.display_name, 1)
        if name == "InstallLocation":
            return (self.install_location, 1)
        raise FileNotFoundError(name)

    def CloseKey(self, key):
        self.closed.append(key)


class _RaisingWinreg:
    """winreg stand-in whose key operations always fail."""

    HKEY_LOCAL_MACHINE = object()
    HKEY_CURRENT_USER = object()

    def __init__(self, error: Exception):
        self.error = error

    def OpenKey(self, hive, path):
        raise self.error

    def EnumKey(self, key, index):
        raise self.error

    def QueryValueEx(self, key, name):
        raise self.error

    def CloseKey(self, key):
        pass


def test_versioned_directories_sorted_newest_first(windows_env):
    """Versioned install dirs sort numerically descending, unparsable last."""
    kicad_root = windows_env / "KiCad"
    _make_install(kicad_root, ["8.0", "9.0.4", "10.0", "nightly"])
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()
    versioned = [path for path in paths if path.startswith(str(windows_env))]

    assert versioned == [
        os.path.join(str(kicad_root), "10.0", "bin", "kicad-cli.exe"),
        os.path.join(str(kicad_root), "9.0.4", "bin", "kicad-cli.exe"),
        os.path.join(str(kicad_root), "8.0", "bin", "kicad-cli.exe"),
        os.path.join(str(kicad_root), "nightly", "bin", "kicad-cli.exe"),
    ]
    assert paths[len(versioned) :] == UNVERSIONED_WINDOWS_PATHS


def test_all_windows_install_roots_are_enumerated(windows_env, monkeypatch):
    """ProgramFiles, ProgramFiles(x86) and LOCALAPPDATA roots are all scanned."""
    program_files_x86 = windows_env.parent / "ProgramFilesX86"
    local_appdata = windows_env.parent / "LocalAppData"
    program_files_install = _make_install(windows_env / "KiCad", ["8.0"])
    x86_install = _make_install(program_files_x86 / "KiCad", ["9.0"])
    local_install = _make_install(local_appdata / "Programs" / "KiCad", ["10.0"])
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files_x86))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()
    versioned = [path for path in paths if not path.startswith("C:")]

    assert versioned == [
        str(program_files_install["8.0"]),
        str(x86_install["9.0"]),
        str(local_install["10.0"]),
    ]


def test_duplicate_install_roots_are_deduped(windows_env, monkeypatch):
    """Two environment roots pointing at the same location yield one candidate."""
    installed = _make_install(windows_env / "KiCad", ["9.0"])
    monkeypatch.setenv("ProgramFiles(x86)", str(windows_env))
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()

    assert paths.count(str(installed["9.0"])) == 1


def test_newest_version_selected_when_several_are_valid(windows_env, monkeypatch):
    """The highest installed version wins when several candidates work."""
    kicad_root = windows_env / "KiCad"
    installed = _make_install(kicad_root, ["8.0", "9.0.4", "10.0"])
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, None)

    selected = manager.find_kicad_cli()

    assert selected == str(installed["10.0"])
    assert calls == [str(installed["10.0"])]


def test_invalid_candidate_does_not_abort_search(windows_env, monkeypatch):
    """A candidate that exists but fails --version falls through to the next."""
    kicad_root = windows_env / "KiCad"
    installed = _make_install(kicad_root, ["10.0", "9.0.4"])
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [installed["9.0.4"]])

    selected = manager.find_kicad_cli()

    assert selected == str(installed["9.0.4"])
    assert calls == [str(installed["10.0"]), str(installed["9.0.4"])]


def test_environment_variable_takes_priority(windows_env, monkeypatch):
    """A working KICAD_CLI_PATH wins without enumerating install dirs."""
    exe = windows_env / "custom" / "kicad-cli.exe"
    exe.parent.mkdir()
    exe.touch()
    monkeypatch.setenv("KICAD_CLI_PATH", str(exe))
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, None)

    def fail_enumeration():
        raise AssertionError("common installation paths must not be enumerated")

    monkeypatch.setattr(manager, "_get_common_installation_paths", fail_enumeration)

    assert manager.find_kicad_cli() == str(exe)
    assert calls == [str(exe)]


def test_missing_environment_path_falls_back_to_enumeration(windows_env, monkeypatch):
    """An unusable KICAD_CLI_PATH is reported and the search continues."""
    kicad_root = windows_env / "KiCad"
    installed = _make_install(kicad_root, ["9.0"])
    missing = windows_env / "missing" / "kicad-cli.exe"
    monkeypatch.setenv("KICAD_CLI_PATH", str(missing))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [installed["9.0"]])

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(installed["9.0"])
    assert diagnosis.candidates[0].source == "environment"
    assert diagnosis.candidates[0].path == str(missing)
    assert diagnosis.candidates[0].exists is False


def test_registry_fallback_finds_install_location(windows_env, monkeypatch):
    """Registry uninstall entries provide a last-resort candidate."""
    monkeypatch.setattr(sys, "platform", "win32")
    install_location = r"C:\Program Files\KiCad\10.0"
    fake_winreg = _FakeWinreg(install_location)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()
    expected = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"

    assert manager._get_registry_installation_paths() == [expected]
    assert expected in [candidate.path for candidate in manager._detect_cli_path()]


def test_registry_install_location_pointing_at_bin_is_not_doubled(windows_env, monkeypatch):
    """InstallLocation already ending in bin is joined directly."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_winreg = _FakeWinreg(r"C:\KiCad\bin")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == [r"C:\KiCad\bin\kicad-cli.exe"]


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
def test_registry_errors_are_swallowed(windows_env, monkeypatch, error):
    """Registry failures never propagate and yield no candidates."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _RaisingWinreg(error))
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == []


def test_registry_skips_non_kicad_and_missing_install_location(windows_env, monkeypatch):
    """Entries without KiCad DisplayName or InstallLocation are ignored."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_winreg = _FakeWinreg(r"C:\Program Files\KiCad\10.0", display_name="Other EDA Suite")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == []


def test_unreadable_installation_root_is_ignored(windows_env, monkeypatch):
    """PermissionError while scanning a root does not abort detection."""
    manager = KiCadCLIManager()

    def raising_scandir(path):
        raise PermissionError(13, "permission denied", str(path))

    monkeypatch.setattr(os, "scandir", raising_scandir)

    assert manager._get_common_installation_paths() == UNVERSIONED_WINDOWS_PATHS


def test_linux_is_unaffected_by_windows_environment(tmp_path, monkeypatch):
    """On Linux only Unix locations are returned; ProgramFiles/winreg untouched."""
    populated = tmp_path / "ProgramFiles" / "KiCad" / "10.0" / "bin"
    populated.mkdir(parents=True)
    (populated / "kicad-cli.exe").touch()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setitem(sys.modules, "winreg", _RaisingWinreg(AssertionError("winreg used")))
    manager = KiCadCLIManager()

    assert manager._get_common_installation_paths() == [
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/kicad/bin/kicad-cli",
        "/snap/kicad/current/usr/bin/kicad-cli",
    ]
    assert manager._get_registry_installation_paths() == []
    assert all("ProgramFiles" not in candidate.path for candidate in manager._detect_cli_path())


def test_error_message_lists_tried_candidates_and_truncates(tmp_path, monkeypatch):
    """Detection failure reports actionable, bounded diagnostics."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    manager = KiCadCLIManager()
    candidates = [str(tmp_path / f"candidate-{index}" / "kicad-cli") for index in range(12)]
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: list(candidates))

    with pytest.raises(KiCadCLIError) as excinfo:
        manager.get_cli_path()

    message = str(excinfo.value)
    assert "KICAD_CLI_PATH" in message
    assert candidates[0] in message
    assert candidates[9] in message
    assert candidates[10] not in message
    assert "Tried 12 candidate location(s)" in message
    assert "and 2 more" in message
    assert "PATH lookup for kicad-cli: no executable found." in message
    assert "add KiCad's bin directory to PATH" in message

    assert manager.get_cli_path(required=False) is None


def test_diagnose_reports_candidates_and_selection(windows_env, monkeypatch):
    """diagnose() exposes every probed candidate and the selected one."""
    kicad_root = windows_env / "KiCad"
    installed = _make_install(kicad_root, ["10.0", "9.0.4"])
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [installed["9.0.4"]])

    diagnosis = manager.diagnose()

    assert diagnosis.platform == "Windows"
    assert [candidate.path for candidate in diagnosis.candidates] == [
        str(installed["10.0"]),
        str(installed["9.0.4"]),
    ]
    assert [candidate.source for candidate in diagnosis.candidates] == ["common", "common"]
    assert [candidate.exists for candidate in diagnosis.candidates] == [True, True]
    assert [candidate.validated for candidate in diagnosis.candidates] == [False, True]
    assert diagnosis.selected == str(installed["9.0.4"])


def test_diagnose_reports_no_selection_when_all_candidates_fail(windows_env, monkeypatch):
    """diagnose() marks a missing selection when nothing validates."""
    _make_install(windows_env / "KiCad", ["10.0"])
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [])

    diagnosis = manager.diagnose()

    assert diagnosis.selected is None
    # The versioned install plus the three unversioned fallbacks were probed.
    assert len(diagnosis.candidates) == 4
    assert diagnosis.candidates[0].exists is True
    assert diagnosis.candidates[0].validated is False
    assert [candidate.exists for candidate in diagnosis.candidates[1:]] == [False, False, False]
