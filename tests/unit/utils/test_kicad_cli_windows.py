"""Tests for Windows KiCad CLI discovery in versioned install directories."""

import os
from pathlib import Path
import platform
import shutil
import sys

import pytest

from kcaa.utils.config import config
from kcaa.utils.kicad_cli import (
    KiCadCLIError,
    KiCadCLIManager,
    _iter_ancestor_dirs,
    _iter_appimage_mount_dirs,
    _parse_version_directory,
    _sort_version_directories,
)

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
WOW6432_UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
HKCU_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

RegistryEntry = tuple[str, str, str, str, str | None]

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


@pytest.fixture(autouse=True)
def _isolate_external_discovery(monkeypatch):
    """Keep tests independent of host config, AppImage mounts and drives."""
    monkeypatch.setattr(
        "kcaa.utils.kicad_cli._iter_appimage_mount_dirs",
        lambda max_mounts=8: iter(()),
    )
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr("kcaa.utils.kicad_cli._list_windows_drives", lambda: [])
    for attr in (
        "_kicad_app_path",
        "_kicad_symbol_dir",
        "_kicad_footprint_dir",
        "_kicad_template_dir",
    ):
        monkeypatch.setattr(config, attr, "")


@pytest.fixture
def windows_env(tmp_path, monkeypatch):
    """Simulate a Windows environment with ProgramFiles pointing at tmp_path."""
    program_files = tmp_path / "ProgramFiles"
    program_files.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    # Registry probing stays opt-in per test; this also keeps the suite from
    # reading a real registry when run on Windows.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    for name in ("ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA", "KICAD_CLI_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return program_files


def _patch_validation(manager: KiCadCLIManager, monkeypatch, valid_paths: list[str] | None):
    """Patch validation so only the given paths succeed (None = all succeed)."""
    valid = set(valid_paths) if valid_paths is not None else None
    calls: list[str] = []

    def fake_validate(path: str) -> bool:
        calls.append(path)
        return True if valid is None else path in valid

    monkeypatch.setattr(manager, "_validate_cli_path", fake_validate)
    return calls


def _install_entry(
    install_location: str | None,
    *,
    hive: str = "HKLM",
    uninstall_key: str = UNINSTALL_KEY,
    subkey: str = "KiCad 10.0",
    display_name: str = "KiCad 10.0",
) -> RegistryEntry:
    """Build an in-memory uninstall entry for :class:`_FakeWinreg`."""
    return (hive, uninstall_key, subkey, display_name, install_location)


class _FakeWinreg:
    """Winreg stand-in backing uninstall entries in HKLM, WOW6432 and HKCU."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, entries: list[RegistryEntry] | None = None):
        self._entries = list(entries or [])
        self._handles: dict[int, dict[str, str | None]] = {}
        self._next_handle = 0
        self.opened: list[int] = []
        self.closed: list[int] = []

    def _new_handle(self, info: dict[str, str | None]) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = info
        self.opened.append(handle)
        return handle

    def OpenKey(self, hive, path):
        for entry_hive, uninstall_key, subkey, display_name, install_location in self._entries:
            if hive != entry_hive:
                continue
            if path == uninstall_key:
                return self._new_handle({"hive": hive, "uninstall": uninstall_key, "subkey": None})
            if path == uninstall_key + "\\" + subkey:
                return self._new_handle(
                    {
                        "hive": hive,
                        "uninstall": uninstall_key,
                        "subkey": subkey,
                        "display": display_name,
                        "location": install_location,
                    }
                )
        raise FileNotFoundError(path)

    def EnumKey(self, key, index):
        info = self._handles[key]
        if info["subkey"] is not None:
            raise OSError("key has no subkeys")
        names = [
            entry[2]
            for entry in self._entries
            if entry[0] == info["hive"] and entry[1] == info["uninstall"]
        ]
        if index < len(names):
            return names[index]
        raise OSError("no more data")

    def QueryValueEx(self, key, name):
        info = self._handles[key]
        if info["subkey"] is None:
            raise OSError("unexpected key")
        if name == "DisplayName":
            return (info["display"], 1)
        if name == "InstallLocation":
            if info["location"] is None:
                raise FileNotFoundError(name)
            return (info["location"], 1)
        raise FileNotFoundError(name)

    def CloseKey(self, key):
        self.closed.append(key)

    def key_info(self, handle: int) -> tuple[str, str, str | None]:
        """Return ``(hive, uninstall key, subkey)`` for an opened handle."""
        info = self._handles[handle]
        return (str(info["hive"]), str(info["uninstall"]), info["subkey"])

    @property
    def opened_keys(self) -> list[tuple[str, str, str | None]]:
        return [self.key_info(handle) for handle in self.opened]

    @property
    def closed_keys(self) -> list[tuple[str, str, str | None]]:
        return [self.key_info(handle) for handle in self.closed]


def _assert_all_keys_closed(fake_winreg: _FakeWinreg) -> None:
    """Every handle opened by the registry reader must be closed exactly once."""
    assert fake_winreg.opened
    assert sorted(fake_winreg.closed) == sorted(fake_winreg.opened)


class _RaisingWinreg:
    """winreg stand-in whose key operations always fail."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

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
    versioned = [path for path in paths if path.startswith(str(windows_env.parent))]

    assert versioned == [
        str(program_files_install["8.0"]),
        str(x86_install["9.0"]),
        str(local_install["10.0"]),
    ]


def test_programw6432_root_is_used(windows_env, monkeypatch):
    """ProgramW6432 exposes 64-bit installs to 32-bit interpreters."""
    program_w6432 = windows_env.parent / "ProgramW6432"
    installed = _make_install(program_w6432 / "KiCad", ["11.0"])
    monkeypatch.setenv("ProgramW6432", str(program_w6432))
    manager = KiCadCLIManager()

    assert str(installed["11.0"]) in manager._get_common_installation_paths()


def test_duplicate_install_roots_are_deduped(windows_env, monkeypatch):
    """Two environment roots pointing at the same location yield one candidate."""
    installed = _make_install(windows_env / "KiCad", ["9.0"])
    monkeypatch.setenv("ProgramFiles(x86)", str(windows_env))
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()

    assert paths.count(str(installed["9.0"])) == 1


def test_case_variant_duplicate_roots_are_deduped(windows_env, monkeypatch):
    """Windows paths differing only in case must not yield duplicate candidates."""
    monkeypatch.setenv("ProgramW6432", str(windows_env).upper())
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_list_version_directories", lambda root: ["9.0"])

    paths = manager._get_common_installation_paths()
    versioned = [path for path in paths if path.lower().startswith(str(windows_env.parent).lower())]

    assert len(versioned) == 1


def test_non_version_directory_noise_is_filtered(windows_env):
    """share/docs dirs are dropped; version-like dirs and real nightly installs stay."""
    kicad_root = windows_env / "KiCad"
    (kicad_root / "share" / "doc").mkdir(parents=True)
    (kicad_root / "docs").mkdir()
    (kicad_root / "8.0").mkdir()
    installed = _make_install(kicad_root, ["9.0", "nightly"])
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()
    versioned = [path for path in paths if path.startswith(str(windows_env))]

    assert versioned == [
        str(installed["9.0"]),
        os.path.join(str(kicad_root), "8.0", "bin", "kicad-cli.exe"),
        str(installed["nightly"]),
    ]
    assert all("share" not in path and "docs" not in path for path in versioned)


def test_unicode_digit_directory_does_not_raise(windows_env):
    """A superscript-digit directory must not escape as ValueError."""
    installed = _make_install(windows_env / "KiCad", ["9.0", "9.0²"])
    manager = KiCadCLIManager()

    paths = manager._get_common_installation_paths()

    assert _parse_version_directory("9.0²") is None
    assert str(installed["9.0"]) in paths
    assert str(installed["9.0²"]) in paths


def test_version_sort_orders_910_above_99():
    """Numeric segments compare numerically, not lexically."""
    assert _sort_version_directories(["9.9", "9.10", "10.0"]) == ["10.0", "9.10", "9.9"]


def test_version_sort_places_prereleases_and_nightly_last():
    """Non-numeric names follow numeric versions in deterministic order."""
    assert _sort_version_directories(["nightly", "10.0", "9.0.0-rc1", "10.0.0"]) == [
        "10.0.0",
        "10.0",
        "9.0.0-rc1",
        "nightly",
    ]


def test_newest_version_selected_when_several_are_valid(windows_env, monkeypatch):
    """The highest installed version wins when several candidates work."""
    installed = _make_install(windows_env / "KiCad", ["8.0", "9.0.4", "10.0"])
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, None)

    selected = manager.find_kicad_cli()

    assert selected == str(installed["10.0"])
    assert calls == [str(installed["10.0"])]


def test_invalid_candidate_does_not_abort_search(windows_env, monkeypatch):
    """A candidate that exists but fails --version falls through to the next."""
    installed = _make_install(windows_env / "KiCad", ["10.0", "9.0.4"])
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [str(installed["9.0.4"])])

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
    """A KICAD_CLI_PATH that does not exist is reported and the search continues."""
    installed = _make_install(windows_env / "KiCad", ["9.0"])
    missing = windows_env / "missing" / "kicad-cli.exe"
    monkeypatch.setenv("KICAD_CLI_PATH", str(missing))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [str(installed["9.0"])])

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(installed["9.0"])
    assert diagnosis.candidates[0].source == "environment"
    assert diagnosis.candidates[0].path == str(missing)
    assert diagnosis.candidates[0].exists is False


def test_invalid_environment_file_falls_back_to_enumeration(windows_env, monkeypatch):
    """KICAD_CLI_PATH exists but fails --version: other candidates are tried."""
    broken = windows_env / "custom" / "kicad-cli.exe"
    broken.parent.mkdir()
    broken.touch()
    installed = _make_install(windows_env / "KiCad", ["9.0"])
    monkeypatch.setenv("KICAD_CLI_PATH", str(broken))
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [str(installed["9.0"])])

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(installed["9.0"])
    assert diagnosis.candidates[0].source == "environment"
    assert diagnosis.candidates[0].exists is True
    assert diagnosis.candidates[0].validated is False
    assert calls == [str(broken), str(installed["9.0"])]


def test_registry_fallback_finds_install_location(windows_env, monkeypatch):
    """Registry uninstall entries provide a last-resort candidate."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_winreg = _FakeWinreg([_install_entry(r"C:\Program Files\KiCad\10.0")])
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()
    expected = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"

    assert manager._get_registry_installation_paths() == [expected]
    assert expected in [candidate.path for candidate in manager._detect_cli_path()]
    assert fake_winreg.opened_keys[0] == ("HKLM", UNINSTALL_KEY, None)
    assert ("HKLM", UNINSTALL_KEY, "KiCad 10.0") in fake_winreg.opened_keys
    _assert_all_keys_closed(fake_winreg)


def test_registry_finds_hkcu_per_user_install(windows_env, monkeypatch):
    """A KiCad entry under HKCU is detected when HKLM has nothing."""
    monkeypatch.setattr(sys, "platform", "win32")
    install_location = r"C:\Users\user\AppData\Local\Programs\KiCad\9.0"
    fake_winreg = _FakeWinreg(
        [
            _install_entry(
                install_location,
                hive="HKCU",
                uninstall_key=HKCU_UNINSTALL_KEY,
                subkey="KiCad 9.0",
                display_name="KiCad 9.0",
            )
        ]
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == [install_location + r"\bin\kicad-cli.exe"]
    assert ("HKCU", HKCU_UNINSTALL_KEY, None) in fake_winreg.opened_keys
    assert ("HKCU", HKCU_UNINSTALL_KEY, "KiCad 9.0") in fake_winreg.opened_keys
    _assert_all_keys_closed(fake_winreg)


def test_registry_finds_wow6432node_install(windows_env, monkeypatch):
    """A KiCad entry under the WOW6432Node uninstall key is detected."""
    monkeypatch.setattr(sys, "platform", "win32")
    install_location = r"C:\Program Files (x86)\KiCad\8.0"
    fake_winreg = _FakeWinreg(
        [
            _install_entry(
                install_location,
                uninstall_key=WOW6432_UNINSTALL_KEY,
                subkey="KiCad 8.0",
                display_name="KiCad 8.0",
            )
        ]
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == [install_location + r"\bin\kicad-cli.exe"]
    assert ("HKLM", WOW6432_UNINSTALL_KEY, None) in fake_winreg.opened_keys
    _assert_all_keys_closed(fake_winreg)


def test_registry_runs_after_existing_candidate_fails_validation(windows_env, monkeypatch):
    """A stale install must not stop the registry fallback from being consulted."""
    stale = _make_install(windows_env / "KiCad", ["8.0"])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg([_install_entry(r"D:\CustomKiCad\9.0")]))
    manager = KiCadCLIManager()
    registry_exe = r"D:\CustomKiCad\9.0\bin\kicad-cli.exe"
    # Simulate the executable existing on the (real) Windows volume.
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == registry_exe or real_isfile(path))
    calls = _patch_validation(manager, monkeypatch, [registry_exe])
    registry_reads: list[int] = []
    original_registry = manager._get_registry_installation_paths

    def counting_registry():
        registry_reads.append(1)
        return original_registry()

    monkeypatch.setattr(manager, "_get_registry_installation_paths", counting_registry)

    selected = manager.find_kicad_cli()

    assert selected == registry_exe
    assert calls == [str(stale["8.0"]), registry_exe]
    assert registry_reads == [1]


def test_registry_is_not_read_when_first_candidate_validates(windows_env, monkeypatch):
    """A successful early candidate must leave the registry untouched."""
    installed = _make_install(windows_env / "KiCad", ["10.0"])
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)
    monkeypatch.setattr(
        manager,
        "_get_registry_installation_paths",
        lambda: pytest.fail("registry must not be read when a candidate validates"),
    )

    assert manager.find_kicad_cli() == str(installed["10.0"])


def test_registry_install_location_pointing_at_bin_is_not_doubled(windows_env, monkeypatch):
    """InstallLocation already ending in bin is joined directly."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg([_install_entry(r"C:\KiCad\bin")]))
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == [r"C:\KiCad\bin\kicad-cli.exe"]


def test_registry_permission_errors_are_swallowed(windows_env, monkeypatch):
    """Registry permission failures never propagate and yield no candidates."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _RaisingWinreg(PermissionError("denied")))
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == []


def test_registry_skips_non_kicad_entries(windows_env, monkeypatch):
    """Entries whose DisplayName is not KiCad are ignored."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_winreg = _FakeWinreg(
        [_install_entry(r"C:\Program Files\KiCad\10.0", display_name="Other EDA Suite")]
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == []
    _assert_all_keys_closed(fake_winreg)


def test_registry_skips_kicad_entries_without_install_location(windows_env, monkeypatch):
    """KiCad-named docs entries without InstallLocation are ignored, keys closed."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_winreg = _FakeWinreg([_install_entry(None, display_name="KiCad Documentation")])
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    manager = KiCadCLIManager()

    assert manager._get_registry_installation_paths() == []
    assert fake_winreg.closed_keys == [
        ("HKLM", UNINSTALL_KEY, "KiCad 10.0"),
        ("HKLM", UNINSTALL_KEY, None),
    ]
    _assert_all_keys_closed(fake_winreg)


def test_unreadable_installation_root_is_ignored(windows_env, monkeypatch):
    """PermissionError while scanning a root does not abort detection."""
    manager = KiCadCLIManager()

    def raising_scandir(path):
        raise PermissionError(13, "permission denied", str(path))

    monkeypatch.setattr(os, "scandir", raising_scandir)

    assert manager._get_common_installation_paths() == UNVERSIONED_WINDOWS_PATHS


def test_linux_is_unaffected_by_windows_environment(tmp_path, monkeypatch):
    """On Linux only Unix locations are returned and no Windows root is read."""
    populated = tmp_path / "ProgramFiles" / "KiCad" / "10.0" / "bin"
    populated.mkdir(parents=True)
    (populated / "kicad-cli.exe").touch()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    for name in ("ProgramFiles(x86)", "ProgramW6432", "KICAD_CLI_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    manager = KiCadCLIManager()

    expected = [
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/kicad/bin/kicad-cli",
        "/snap/kicad/current/usr/bin/kicad-cli",
    ]
    assert manager._get_common_installation_paths() == expected
    assert manager._get_registry_installation_paths() == []
    assert [candidate.path for candidate in manager._detect_cli_path()] == expected


def test_error_message_starts_with_not_found_and_truncates(tmp_path, monkeypatch):
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
    assert message.startswith("KiCad CLI not found.")
    assert "not found" in message
    assert "KICAD_CLI_PATH is not set." in message
    assert candidates[0] in message
    assert candidates[9] in message
    assert candidates[10] not in message
    assert "Tried 12 candidate location(s)" in message
    assert "and 2 more" in message
    assert "PATH lookup for kicad-cli: no executable found." in message
    assert "add KiCad's bin directory to PATH" in message

    assert manager.get_cli_path(required=False) is None


def test_error_message_distinguishes_missing_environment_path(tmp_path, monkeypatch):
    """A non-existent KICAD_CLI_PATH is described as missing, not unusable."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    missing = tmp_path / "missing" / "kicad-cli"
    monkeypatch.setenv("KICAD_CLI_PATH", str(missing))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: [])

    with pytest.raises(KiCadCLIError) as excinfo:
        manager.get_cli_path()

    message = str(excinfo.value)
    assert f"KICAD_CLI_PATH={str(missing)!r} does not point to an existing file." in message
    assert f"[environment] {missing} (missing)" in message


def test_error_message_reports_unusable_existing_environment_path(tmp_path, monkeypatch):
    """An existing but failing KICAD_CLI_PATH is described as not working."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    broken = tmp_path / "custom" / "kicad-cli"
    broken.parent.mkdir()
    broken.touch()
    monkeypatch.setenv("KICAD_CLI_PATH", str(broken))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: [])
    monkeypatch.setattr(manager, "_validate_cli_path", lambda path: False)

    with pytest.raises(KiCadCLIError) as excinfo:
        manager.get_cli_path()

    message = str(excinfo.value)
    assert f"KICAD_CLI_PATH={str(broken)!r} exists but is not a working kicad-cli." in message
    assert f"[environment] {broken} (exists but failed --version)" in message


def test_diagnose_reports_candidates_and_selection(windows_env, monkeypatch):
    """diagnose() exposes every probed candidate and the selected one."""
    installed = _make_install(windows_env / "KiCad", ["10.0", "9.0.4"])
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [str(installed["9.0.4"])])

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


def test_diagnose_candidate_count_matches_probe(windows_env, monkeypatch):
    """diagnose() reports exactly the candidates that were attempted."""
    installed = _make_install(windows_env / "KiCad", ["10.0"])
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, [])

    diagnosis = manager.diagnose()
    probed_paths = [candidate.path for candidate in manager._detect_cli_path()]

    assert diagnosis.selected is None
    assert [candidate.path for candidate in diagnosis.candidates] == probed_paths
    assert diagnosis.candidates[0].path == str(installed["10.0"])
    assert diagnosis.candidates[0].exists is True
    assert diagnosis.candidates[0].validated is False


def test_detection_is_cached_and_force_refresh_reprobes(windows_env, monkeypatch):
    """Repeated lookups reuse the cache; force_refresh probes again."""
    installed = _make_install(windows_env / "KiCad", ["9.0"])
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [str(installed["9.0"])])

    assert manager.find_kicad_cli() == str(installed["9.0"])
    assert manager.find_kicad_cli() == str(installed["9.0"])
    assert calls == [str(installed["9.0"])]

    assert manager.find_kicad_cli(force_refresh=True) == str(installed["9.0"])
    assert calls == [str(installed["9.0"]), str(installed["9.0"])]


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


def _linux_manager(monkeypatch) -> KiCadCLIManager:
    """Create a Linux manager isolated from host installation paths."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: [])
    return manager


def test_config_windows_symbol_dir_finds_cli(windows_env, monkeypatch):
    """Config-derived Windows symbol dir maps to the sibling bin directory."""
    exe = r"D:\KiCad\10.0\bin\kicad-cli.exe"
    monkeypatch.setattr(config, "_kicad_symbol_dir", r"D:\KiCad\10.0\share\kicad\symbols")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == exe or real_isfile(path))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == exe
    assert [(candidate.source, candidate.path) for candidate in diagnosis.candidates] == [
        ("config", exe)
    ]


def test_config_linux_symbol_dir_ancestor_finds_cli(tmp_path, monkeypatch):
    """Walking up from a Linux symbol dir finds <prefix>/bin/kicad-cli."""
    app_root = tmp_path / "app"
    symbols = app_root / "usr" / "share" / "kicad" / "symbols"
    symbols.mkdir(parents=True)
    exe = app_root / "usr" / "bin" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.touch()
    manager = _linux_manager(monkeypatch)
    monkeypatch.setattr(config, "_kicad_symbol_dir", str(symbols))
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(exe)
    assert [(candidate.source, candidate.path) for candidate in diagnosis.candidates] == [
        ("config", str(exe))
    ]


def test_config_macos_app_bundle_finds_cli(tmp_path, monkeypatch):
    """macOS app bundles resolve through Contents/MacOS/kicad-cli."""
    contents = tmp_path / "KiCad.app" / "Contents"
    symbols = contents / "SharedSupport" / "symbols"
    symbols.mkdir(parents=True)
    exe = contents / "MacOS" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(config, "_kicad_symbol_dir", str(symbols))
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_get_common_installation_paths", lambda: [])
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(exe)
    assert [candidate.source for candidate in diagnosis.candidates] == ["config"]


def test_config_custom_library_without_bin_yields_no_candidates(tmp_path, monkeypatch):
    """A user library directory must not create fake config candidates."""
    symbols = tmp_path / "MyLibs" / "symbols"
    symbols.mkdir(parents=True)
    manager = _linux_manager(monkeypatch)
    monkeypatch.setattr(config, "_kicad_symbol_dir", str(symbols))
    _patch_validation(manager, monkeypatch, [])

    assert manager._get_config_installation_paths() == []
    assert manager.find_kicad_cli() is None


def test_config_candidate_priority_over_common_installations(windows_env, monkeypatch):
    """Config-derived candidates are probed before platform default roots."""
    config_root = windows_env / "config-install"
    config_bin = config_root / "10.0" / "bin"
    config_bin.mkdir(parents=True)
    config_exe = config_bin / "kicad-cli.exe"
    config_exe.touch()
    _make_install(windows_env / "KiCad", ["10.0"])
    monkeypatch.setattr(config, "_kicad_app_path", str(config_root))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(config_exe)
    assert diagnosis.candidates[0].source == "config"


def test_environment_variable_takes_priority_over_config(windows_env, monkeypatch):
    """KICAD_CLI_PATH still wins before config-derived candidates are probed."""
    env_exe = windows_env / "custom" / "kicad-cli.exe"
    env_exe.parent.mkdir()
    env_exe.touch()
    config_bin = windows_env / "config-install" / "10.0" / "bin"
    config_bin.mkdir(parents=True)
    (config_bin / "kicad-cli.exe").touch()
    monkeypatch.setenv("KICAD_CLI_PATH", str(env_exe))
    monkeypatch.setattr(config, "_kicad_app_path", str(windows_env / "config-install"))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)
    monkeypatch.setattr(
        manager,
        "_get_config_installation_paths",
        lambda: pytest.fail("config must not be enumerated when KICAD_CLI_PATH works"),
    )

    assert manager.find_kicad_cli() == str(env_exe)


def test_appimage_appdir_cli_is_detected(tmp_path, monkeypatch):
    """APPDIR identifies the running AppImage's CLI."""
    mount = tmp_path / "mnt"
    exe = mount / "usr" / "bin" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv("APPDIR", str(mount))
    manager = _linux_manager(monkeypatch)
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()

    assert diagnosis.selected == str(exe)
    assert [(candidate.source, candidate.path) for candidate in diagnosis.candidates] == [
        ("appimage", str(exe))
    ]


def test_appimage_mount_candidates_skip_empty_mounts(tmp_path, monkeypatch):
    """Mounts without kicad-cli are probed but skipped for the next mount."""
    first = tmp_path / "m1"
    (first / "usr" / "bin").mkdir(parents=True)
    second = tmp_path / "m2"
    exe = second / "usr" / "bin" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(
        "kcaa.utils.kicad_cli._iter_appimage_mount_dirs",
        lambda max_mounts=8: iter([str(first), str(second)]),
    )
    manager = _linux_manager(monkeypatch)
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()
    appimage = [c for c in diagnosis.candidates if c.source == "appimage"]

    assert diagnosis.selected == str(exe)
    assert [candidate.path for candidate in appimage] == [
        os.path.join(str(first), "usr", "bin", "kicad-cli"),
        str(exe),
    ]
    assert [candidate.exists for candidate in appimage] == [False, True]


def test_appimage_empty_mount_list_is_skipped(monkeypatch):
    """No APPDIR and no mounts yields no appimage candidates and no error."""
    manager = _linux_manager(monkeypatch)
    _patch_validation(manager, monkeypatch, [])

    assert manager.find_kicad_cli() is None
    assert all(candidate.source != "appimage" for candidate in manager._last_candidates)


def test_appimage_mount_dirs_sorted_by_mtime_and_limited(tmp_path, monkeypatch):
    """Mount dirs are newest-first and capped at eight entries."""
    mounts = []
    for index in range(10):
        mount = tmp_path / f"mount{index}"
        mount.mkdir()
        os.utime(mount, (1000 + index, 1000 + index))
        mounts.append(str(mount))
    monkeypatch.setattr("kcaa.utils.kicad_cli.glob.glob", lambda pattern: list(mounts))

    assert list(_iter_appimage_mount_dirs()) == list(reversed(mounts))[:8]


def test_windows_other_drive_install_detected(windows_env, monkeypatch):
    """Versioned KiCad roots on non-system drives are enumerated."""
    target = r"D:\KiCad\10.0\bin\kicad-cli.exe"
    monkeypatch.setattr("kcaa.utils.kicad_cli._list_windows_drives", lambda: ["C:\\", "D:\\"])
    monkeypatch.setattr(
        KiCadCLIManager,
        "_list_version_directories",
        lambda self, root: ["10.0"] if root == r"D:\KiCad" else [],
    )
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == target or real_isfile(path))
    manager = KiCadCLIManager()
    _patch_validation(manager, monkeypatch, None)

    diagnosis = manager.diagnose()
    drive = [candidate for candidate in diagnosis.candidates if candidate.source == "drive"]

    assert diagnosis.selected == target
    assert [candidate.path for candidate in drive] == [target]


def test_nonexistent_drive_roots_do_not_raise(windows_env, monkeypatch):
    """A drive that cannot be scanned contributes no candidates and no error."""
    monkeypatch.setattr("kcaa.utils.kicad_cli._list_windows_drives", lambda: ["Z:\\"])
    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path).startswith("Z:"):
            raise FileNotFoundError(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    manager = KiCadCLIManager()

    assert manager._get_drive_installation_paths() == []


def test_config_and_drive_duplicate_path_appears_once(windows_env, monkeypatch):
    """The same path produced by config and drive sources is deduplicated."""
    target = r"D:\KiCad\10.0\bin\kicad-cli.exe"
    monkeypatch.setattr(config, "_kicad_symbol_dir", r"D:\KiCad\10.0\share\kicad\symbols")
    monkeypatch.setattr("kcaa.utils.kicad_cli._list_windows_drives", lambda: ["D:\\"])
    monkeypatch.setattr(
        KiCadCLIManager,
        "_list_version_directories",
        lambda self, root: ["10.0"] if root == r"D:\KiCad" else [],
    )
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == target or real_isfile(path))
    manager = KiCadCLIManager()

    candidates = list(manager._detect_cli_path())

    assert [candidate.path for candidate in candidates if candidate.source == "config"] == [target]
    assert [candidate.path for candidate in candidates if candidate.source == "drive"] == []
    assert [candidate.path for candidate in candidates].count(target) == 1


def test_registry_is_used_after_config_and_common_fail(windows_env, monkeypatch):
    """Registry lookup remains the final fallback after all earlier sources."""
    config_bin = windows_env / "config-install" / "10.0" / "bin"
    config_bin.mkdir(parents=True)
    config_exe = config_bin / "kicad-cli.exe"
    config_exe.touch()
    stale_exe = _make_install(windows_env / "KiCad", ["8.0"])["8.0"]
    monkeypatch.setattr(config, "_kicad_app_path", str(windows_env / "config-install"))
    monkeypatch.setattr(sys, "platform", "win32")
    registry_install = r"D:\CustomKiCad\9.0"
    registry_exe = registry_install + r"\bin\kicad-cli.exe"
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg([_install_entry(registry_install)]))
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == registry_exe or real_isfile(path))
    manager = KiCadCLIManager()
    calls = _patch_validation(manager, monkeypatch, [registry_exe])
    registry_reads: list[int] = []
    original_registry = manager._get_registry_installation_paths

    def counting_registry():
        registry_reads.append(1)
        return original_registry()

    monkeypatch.setattr(manager, "_get_registry_installation_paths", counting_registry)

    assert manager.find_kicad_cli() == registry_exe
    assert calls == [str(config_exe), str(stale_exe), registry_exe]
    assert registry_reads == [1]


def test_ancestor_traversal_is_bounded(tmp_path, monkeypatch):
    """Ancestor walking stops after five levels even for deep directories."""
    deep = tmp_path
    for level in range(8):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    lengths: list[int] = []
    real_iter = _iter_ancestor_dirs

    def counting_ancestors(path, max_levels=5):
        ancestors = list(real_iter(path, max_levels))
        lengths.append(len(ancestors))
        yield from ancestors

    monkeypatch.setattr("kcaa.utils.kicad_cli._iter_ancestor_dirs", counting_ancestors)
    manager = _linux_manager(monkeypatch)
    monkeypatch.setattr(config, "_kicad_symbol_dir", str(deep))

    assert manager._get_config_installation_paths() == []
    assert lengths == [5]
    assert len(list(real_iter(str(deep)))) == 5
