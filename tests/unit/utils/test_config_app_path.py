"""Tests for deriving kicad_app_path from KICAD* directory variables."""

import ntpath
import os
from pathlib import Path

import pytest

import kcaa.utils.config as config_module
from kcaa.utils.config import ServerConfig, _derive_app_path_from_library_dirs


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Clear inherited KiCad directory variables for deterministic detection."""
    for name in (
        "KICAD_APP_PATH",
        "KICAD_STOCK_DATA_HOME",
        "KICAD10_SYMBOL_DIR",
        "KICAD_SYMBOL_DIR",
        "KICAD10_FOOTPRINT_DIR",
        "KICAD_FOOTPRINT_DIR",
        "KICAD10_TEMPLATE_DIR",
        "KICAD_TEMPLATE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KICAD_VERSION", "10.0")
    monkeypatch.setattr(config_module, "_SYSTEM", "Linux")


def _make_cli(path: Path) -> Path:
    """Create an empty executable placeholder at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_kicad_app_path_takes_priority(tmp_path, monkeypatch):
    """An explicit KICAD_APP_PATH is used as-is, even when derivation works."""
    explicit = tmp_path / "explicit"
    derived_root = tmp_path / "derived"
    _make_cli(derived_root / "bin" / "kicad-cli")
    monkeypatch.setenv("KICAD_APP_PATH", str(explicit))
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(derived_root / "share" / "kicad" / "symbols"))

    cfg = ServerConfig()

    assert cfg.kicad_app_path == str(explicit)


def test_windows_layout_derives_version_directory(tmp_path, monkeypatch):
    """C:\\...\\KiCad\\10.0\\share\\kicad\\symbols derives the version directory."""
    app = r"C:\Program Files\KiCad\10.0"
    symbol_dir = app + r"\share\kicad\symbols"
    exe = app + r"\bin\kicad-cli.exe"
    monkeypatch.setattr(config_module, "_SYSTEM", "Windows")
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", symbol_dir)
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == exe or real_isfile(path))

    assert _derive_app_path_from_library_dirs("10") == app
    cfg = ServerConfig()
    assert cfg.kicad_app_path == app
    assert ntpath.join(cfg.kicad_app_path, "bin", "kicad-cli.exe") == exe


def test_linux_mount_layout_derives_prefix(tmp_path, monkeypatch):
    """AppImage-style <mount>/share/kicad/symbols derives <mount>."""
    mount = tmp_path / "mount"
    _make_cli(mount / "bin" / "kicad-cli")
    symbol_dir = mount / "share" / "kicad" / "symbols"
    symbol_dir.mkdir(parents=True)
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(symbol_dir))

    assert _derive_app_path_from_library_dirs("10") == str(mount)
    assert ServerConfig().kicad_app_path == str(mount)


def test_distro_layout_derives_usr(monkeypatch):
    """The distribution layout /usr/share/kicad/symbols derives /usr."""
    exe = "/usr/bin/kicad-cli"
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: path == exe or real_isfile(path))

    assert _derive_app_path_from_library_dirs("10") == "/usr"


def test_stock_data_home_derivation(tmp_path, monkeypatch):
    """KICAD_STOCK_DATA_HOME identifies the install root."""
    root = tmp_path / "kicad-root"
    _make_cli(root / "bin" / "kicad-cli")
    monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(root / "share" / "kicad"))

    assert _derive_app_path_from_library_dirs("10") == str(root)


def test_no_cli_falls_back_to_platform_default(tmp_path, monkeypatch):
    """Derivation failure silently falls back to the platform default."""
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(tmp_path / "MyLibs" / "symbols"))

    cfg = ServerConfig()

    assert cfg.kicad_app_path == "/usr"


def test_derived_library_dirs_use_uniform_layout(tmp_path, monkeypatch):
    """Symbol/footprint/template dirs are <app>/share/kicad/<kind>."""
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(config_module, "_SYSTEM", "Windows")
    monkeypatch.setenv("KICAD_APP_PATH", str(app))

    cfg = ServerConfig()

    assert cfg.kicad_symbol_dir == str(app / "share" / "kicad" / "symbols")
    assert cfg.kicad_footprint_dir == str(app / "share" / "kicad" / "footprints")
    assert cfg.kicad_template_dir == str(app / "share" / "kicad" / "template")


def test_windows_default_uses_program_files_and_version(tmp_path, monkeypatch):
    """Without overrides Windows defaults to %ProgramFiles%\\KiCad\\<ver>."""
    program_files = tmp_path / "ProgramFiles"
    monkeypatch.setattr(config_module, "_SYSTEM", "Windows")
    monkeypatch.setenv("ProgramFiles", str(program_files))

    cfg = ServerConfig()

    assert cfg.kicad_app_path == str(program_files / "KiCad" / "10.0")


def test_derivation_tries_next_variable_when_first_has_no_cli(tmp_path, monkeypatch):
    """A declared-but-invalid variable must not stop later variables."""
    bad = tmp_path / "not-an-install"
    (bad / "share" / "kicad").mkdir(parents=True)
    root = tmp_path / "root"
    _make_cli(root / "bin" / "kicad-cli")
    symbol_dir = root / "share" / "kicad" / "symbols"
    symbol_dir.mkdir(parents=True)
    monkeypatch.setenv("KICAD_STOCK_DATA_HOME", str(bad / "share" / "kicad"))
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(symbol_dir))

    assert _derive_app_path_from_library_dirs("10") == str(root)


def test_ancestor_walk_finds_install_root(tmp_path, monkeypatch):
    """A non-standard layout without share/kicad is walked upwards."""
    root = tmp_path / "root"
    _make_cli(root / "bin" / "kicad-cli")
    symbol_dir = root / "libs" / "symbols"
    symbol_dir.mkdir(parents=True)
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(symbol_dir))

    assert _derive_app_path_from_library_dirs("10") == str(root)


def test_stripped_root_without_cli_is_rejected(tmp_path, monkeypatch):
    """A stripped candidate missing bin/kicad-cli is rejected, not returned."""
    bad = tmp_path / "not-an-install"
    symbol_dir = bad / "share" / "kicad" / "symbols"
    symbol_dir.mkdir(parents=True)
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(symbol_dir))

    assert _derive_app_path_from_library_dirs("10") is None
    assert ServerConfig().kicad_app_path == "/usr"
