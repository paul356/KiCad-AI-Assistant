"""macOS-specific tests for the KiCad AI Assistant plugin.

These tests verify that the plugin correctly detects macOS paths, installs into
KiCad's macOS plugin directory, and uses the correct virtual-environment and
IPC/socket conventions on Darwin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess  # nosec B404
import sys
import types
from unittest.mock import patch
import zipfile

import pytest

from kicad_plugin import llm_client as llm_client_module
from kicad_plugin import server_manager
from kicad_plugin.llm_client import _resolve_plugin_python
from kicad_plugin.settings import (
    PluginSettings,
    _detect_kicad_version,
    _get_kcaa_data_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run_shell_script(script_path: Path, *, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell script and return its result."""
    merged_env = {**os.environ, "KCAA_HEADLESS": "1", **(env or {})}
    return subprocess.run(  # nosec B603, B607
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        env=merged_env,
        check=check,
    )


# ---------------------------------------------------------------------------
# Platform detection & settings paths
# ---------------------------------------------------------------------------
class TestMacOSPaths:
    def test_detect_kicad_version_prefers_env_var(self, monkeypatch):
        monkeypatch.setenv("KICAD_VERSION", "11.0")
        monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
        monkeypatch.delenv("KICAD11_SYMBOL_DIR", raising=False)
        assert _detect_kicad_version() == "11.0"

    def test_detect_kicad_version_from_macos_plugin_path(self, monkeypatch):
        monkeypatch.delenv("KICAD_VERSION", raising=False)
        monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
        with patch(
            "kicad_plugin.settings.__file__",
            "/Users/alice/Library/Preferences/kicad/10.0/scripting/plugins/kicad_ai_assistant/settings.py",
        ):
            assert _detect_kicad_version() == "10.0"

    def test_detect_kicad_version_from_macos_plugin_path_with_spaces(self, monkeypatch):
        monkeypatch.delenv("KICAD_VERSION", raising=False)
        monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
        with patch(
            "kicad_plugin.settings.__file__",
            "/Users/alice/Library/Application Support/KiCad/10.0/scripting/plugins/kicad_ai_assistant/settings.py",
        ):
            assert _detect_kicad_version() == "10.0"

    def test_detect_kicad_version_from_kicad_env_vars(self, monkeypatch):
        monkeypatch.delenv("KICAD_VERSION", raising=False)
        monkeypatch.setenv("KICAD10_SYMBOL_DIR", "/tmp/symbols")
        monkeypatch.delenv("KICAD11_SYMBOL_DIR", raising=False)
        assert _detect_kicad_version() == "10.0"

    def test_detect_kicad_version_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("KICAD_VERSION", raising=False)
        for key in list(os.environ):
            if key.startswith("KICAD"):
                monkeypatch.delenv(key, raising=False)
        with patch("kicad_plugin.settings.__file__", "/tmp/kicad_ai_assistant/settings.py"):
            assert _detect_kicad_version() == "10.0"

    def test_detect_kicad_version_uses_pcbnew_when_available(self, monkeypatch):
        monkeypatch.delenv("KICAD_VERSION", raising=False)
        fake_pcbnew = types.ModuleType("pcbnew")
        fake_pcbnew.GetMajorMinorVersion = lambda: "9.0"
        with patch.dict(sys.modules, {"pcbnew": fake_pcbnew}):
            assert _detect_kicad_version() == "9.0"

    def test_get_kcaa_data_dir_on_darwin(self, monkeypatch):
        if platform.system() != "Darwin":
            pytest.skip("Darwin-only test")
        monkeypatch.setenv("KICAD_VERSION", "10.0")
        data_dir = _get_kcaa_data_dir()
        expected_suffix = "Library/Preferences/kicad/10.0/kcaa"
        assert data_dir.endswith(expected_suffix)

    def test_plugin_settings_macos_config_dir(self, monkeypatch, tmp_path):
        if platform.system() != "Darwin":
            pytest.skip("Darwin-only test")
        monkeypatch.setenv("KICAD_VERSION", "10.0")
        s = PluginSettings()
        assert s.config_dir.endswith("Library/Preferences/kicad/10.0/kcaa")
        assert s.settings_path.endswith("kicad_ai_assistant.json")

    def test_plugin_settings_respects_config_dir_override(self, tmp_path):
        custom = str(tmp_path / "custom-config")
        s = PluginSettings(config_dir=custom)
        assert s.config_dir == custom
        assert s.settings_path == os.path.join(custom, "kicad_ai_assistant.json")



# ---------------------------------------------------------------------------
# ServerManager / venv resolution
# ---------------------------------------------------------------------------
class TestMacOSServerManager:
    def _make_settings(self, **kwargs):
        defaults = {
            "server_port": 0,
            "server_log_dir": "",
            "resolved_log_dir": "",
            "python_executable": "",
            "config_dir": "/tmp",
        }
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_resolve_python_prefers_venv_python_on_macos(self, tmp_path, monkeypatch):
        if platform.system() != "Darwin":
            pytest.skip("Darwin-only test")

        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        venv_python = plugin_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        venv_python.chmod(0o755)

        with patch.object(server_manager, "__file__", str(plugin_dir / "server_manager.py")):
            mgr = server_manager.ServerManager(self._make_settings())
            resolved = mgr._resolve_python()
            assert resolved == str(venv_python)

    def test_resolve_python_falls_back_to_system_python(self, tmp_path, monkeypatch):
        if platform.system() != "Darwin":
            pytest.skip("Darwin-only test")

        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        with patch.object(server_manager, "__file__", str(plugin_dir / "server_manager.py")):
            mgr = server_manager.ServerManager(self._make_settings())
            resolved = mgr._resolve_python()
            # Should fall back to something on PATH (python3 or python).
            assert ("bin/python3" in resolved or "bin/python" in resolved or shutil.which("python3") in resolved)

    def test_build_env_does_not_inherit_kicad_appimage_vars(self):
        mgr = server_manager.ServerManager(self._make_settings())
        with patch.dict(
            os.environ,
            {
                "PYTHONHOME": "/Applications/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.10",
                "PYTHONPATH": "/Applications/KiCad.app/Contents/SharedSupport/plugins",
                "LD_LIBRARY_PATH": "/usr/local/lib",
                "DYLD_LIBRARY_PATH": "/usr/local/lib",
                "LD_PRELOAD": "/tmp/lib.so",
            },
        ):
            env = mgr._build_env(1234)
        assert "PYTHONHOME" not in env
        assert "PYTHONPATH" not in env
        assert "LD_LIBRARY_PATH" not in env
        assert "DYLD_LIBRARY_PATH" not in env
        assert "LD_PRELOAD" not in env

    def test_build_env_preserves_macos_user_vars(self):
        mgr = server_manager.ServerManager(self._make_settings())
        with patch.dict(
            os.environ,
            {
                "HOME": "/Users/alice",
                "USER": "alice",
                "TMPDIR": "/var/folders/abc/T",
                "LANG": "en_US.UTF-8",
            },
        ):
            env = mgr._build_env(1234)
        assert env["HOME"] == "/Users/alice"
        assert env["USER"] == "alice"
        assert env["TMPDIR"] == "/var/folders/abc/T"
        assert env["LANG"] == "en_US.UTF-8"


# ---------------------------------------------------------------------------
# LLM client / HTTPS subprocess fallback
# ---------------------------------------------------------------------------
class TestMacOSLLMClient:
    def test_resolve_plugin_python_on_macos(self, tmp_path, monkeypatch):
        if platform.system() != "Darwin":
            pytest.skip("Darwin-only test")

        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        venv_python = plugin_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        venv_python.chmod(0o755)

        with patch.object(
            llm_client_module,
            "__file__",
            str(plugin_dir / "llm_client.py"),
        ):
            # _resolve_plugin_python lives in llm_client.py and mirrors ServerManager logic
            resolved = _resolve_plugin_python()
            assert resolved == str(venv_python)

    def test_macos_forces_subprocess_ssl_fallback(self, monkeypatch):
        """On macOS, _in_process_ssl is set to False when the venv Python exists."""
        real_isfile = os.path.isfile

        def fake_isfile(path: str | os.PathLike[str]) -> bool:
            if str(path).endswith(".venv/bin/python"):
                return True
            return real_isfile(path)

        monkeypatch.setattr(os.path, "isfile", fake_isfile)
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        import importlib

        reloaded = importlib.reload(llm_client_module)
        assert reloaded._in_process_ssl is False


# ---------------------------------------------------------------------------
# KiCad CLI detection on macOS
# ---------------------------------------------------------------------------
class TestMacOSKiCadCLI:
    def test_manager_includes_macos_application_bundle_path(self):
        from kcaa.utils.kicad_cli import KiCadCLIManager

        mgr = KiCadCLIManager()
        with patch.object(mgr, "_system", "Darwin"):
            paths = mgr._get_common_installation_paths()
        assert "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" in paths
        assert "/opt/homebrew/bin/kicad-cli" in paths


# ---------------------------------------------------------------------------
# IPC socket detection on macOS
# ---------------------------------------------------------------------------
class TestMacOSIPCSocket:
    def test_find_kicad_socket_uses_tempdir_on_macos(self, monkeypatch, tmp_path):
        from kcaa.tools.kipy_tools import _find_kicad_socket

        # Force non-Windows, clear environment socket override.
        monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
        fake_tmp = tmp_path / "tmp"
        fake_tmp.mkdir()
        sock_dir = fake_tmp / "kicad"
        sock_dir.mkdir()
        sock_file = sock_dir / "api.sock"
        sock_file.write_text("")

        with patch("tempfile.gettempdir", return_value=str(fake_tmp)):
            with patch("platform.system", return_value="Darwin"):
                result = _find_kicad_socket()

        assert result.startswith("ipc://")
        assert "api.sock" in result
        # Must not be the legacy hard-coded Linux fallback.
        assert result != "ipc:///tmp/kicad/api.sock"
        assert str(fake_tmp) in result


# ---------------------------------------------------------------------------
# Plugin distribution zip
# ---------------------------------------------------------------------------
class TestMacOSPluginZip:
    def test_dist_plugin_zip_contains_macos_files(self, tmp_path):
        """Run `make dist-plugin` and assert macOS files are in the archive."""
        import subprocess  # nosec B404

        repo_root = Path(__file__).parent.parent.parent.parent
        result = subprocess.run(  # nosec B603
            ["make", "dist-plugin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Created dist/kicad_ai_assistant.zip" in result.stdout

        zip_path = repo_root / "dist" / "kicad_ai_assistant.zip"
        assert zip_path.exists()

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
        assert "kicad_ai_assistant/setup_plugin_macos.command" in names
        assert "kicad_ai_assistant/install_macos.sh" in names
        assert "kicad_ai_assistant/setup_plugin.sh" in names
        assert "kicad_ai_assistant/VERSION" in names


# ---------------------------------------------------------------------------
# macOS setup script (setup_plugin_macos.command)
# ---------------------------------------------------------------------------
class TestMacOSSetupScript:
    @pytest.fixture
    def plugin_dir_with_version(self, tmp_path):
        """Create a minimal plugin dir with a VERSION file for dry-run tests."""
        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        (plugin_dir / "VERSION").write_text("0.1.9\n")
        # Copy the real setup script into the temp plugin dir.
        repo_root = Path(__file__).parent.parent.parent.parent
        real_script = repo_root / "kicad_plugin" / "setup_plugin_macos.command"
        script = plugin_dir / "setup_plugin_macos.command"
        script.write_text(real_script.read_text())
        script.chmod(0o755)
        return plugin_dir

    def test_setup_script_dry_run_detects_version_from_path(self, plugin_dir_with_version, tmp_path):
        # Put the plugin under a versioned KiCad path so path-based detection works.
        versioned_plugin = (
            tmp_path
            / "Library"
            / "Preferences"
            / "kicad"
            / "10.0"
            / "scripting"
            / "plugins"
            / "kicad_ai_assistant"
        )
        versioned_plugin.parent.mkdir(parents=True)
        plugin_dir_with_version.rename(versioned_plugin)

        result = _run_shell_script(
            versioned_plugin / "setup_plugin_macos.command",
            env={"DRY_RUN": "1", "KICAD_VERSION": ""},
            check=True,
        )
        assert "DRY RUN mode" in result.stdout
        assert "Detected KiCad version: 10.0" in result.stdout
        assert "Would create virtual environment" in result.stdout
        assert "Would install kcaa==0.1.9" in result.stdout
        assert "Would download FreeRouting JAR" in result.stdout

    def test_setup_script_dry_run_uses_kicad_version_env(self, plugin_dir_with_version):
        result = _run_shell_script(
            plugin_dir_with_version / "setup_plugin_macos.command",
            env={"DRY_RUN": "1", "KICAD_VERSION": "11.0"},
            check=True,
        )
        assert "Detected KiCad version: 11.0" in result.stdout

    def test_setup_script_defaults_version_when_undetectable(self, plugin_dir_with_version):
        result = _run_shell_script(
            plugin_dir_with_version / "setup_plugin_macos.command",
            env={"DRY_RUN": "1", "KICAD_VERSION": ""},
            check=True,
        )
        assert "defaulting to 10.0" in result.stdout
        assert "Detected KiCad version: 10.0" in result.stdout

    def test_setup_script_fails_without_version_file(self, tmp_path):
        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        repo_root = Path(__file__).parent.parent.parent.parent
        real_script = repo_root / "kicad_plugin" / "setup_plugin_macos.command"
        script = plugin_dir / "setup_plugin_macos.command"
        script.write_text(real_script.read_text())
        script.chmod(0o755)

        result = _run_shell_script(
            script,
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0"},
            check=False,
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "VERSION file not found" in combined or "pyproject.toml not found" in combined

    def test_setup_script_falls_back_to_pyproject_toml(self, tmp_path):
        plugin_dir = tmp_path / "kicad_ai_assistant"
        plugin_dir.mkdir()
        (plugin_dir / "pyproject.toml").write_text('[project]\nname = "kcaa"\nversion = "9.9.9"\n')
        repo_root = Path(__file__).parent.parent.parent.parent
        real_script = repo_root / "kicad_plugin" / "setup_plugin_macos.command"
        script = plugin_dir / "setup_plugin_macos.command"
        script.write_text(real_script.read_text())
        script.chmod(0o755)

        result = _run_shell_script(
            script,
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0"},
            check=True,
        )
        assert "kcaa version: 9.9.9 (from pyproject.toml)" in result.stdout

    def test_setup_script_falls_back_to_parent_pyproject_toml(self, tmp_path):
        # Mirror the repo layout: setup script in kicad_plugin/, pyproject.toml in repo root.
        repo_dir = tmp_path / "repo"
        plugin_dir = repo_dir / "kicad_plugin"
        plugin_dir.mkdir(parents=True)
        (repo_dir / "pyproject.toml").write_text('[project]\nname = "kcaa"\nversion = "8.8.8"\n')
        repo_root = Path(__file__).parent.parent.parent.parent
        real_script = repo_root / "kicad_plugin" / "setup_plugin_macos.command"
        script = plugin_dir / "setup_plugin_macos.command"
        script.write_text(real_script.read_text())
        script.chmod(0o755)

        result = _run_shell_script(
            script,
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0"},
            check=True,
        )
        assert "kcaa version: 8.8.8 (from pyproject.toml)" in result.stdout

    def test_setup_script_reads_version_from_repo_pyproject_toml(self):
        # Run the actual script from the repository tree.
        repo_root = Path(__file__).parent.parent.parent.parent
        script = repo_root / "kicad_plugin" / "setup_plugin_macos.command"
        result = _run_shell_script(
            script,
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0"},
            check=True,
        )
        # The repo pyproject.toml version is expected to be present in output.
        expected = "kcaa version:"
        assert expected in result.stdout


# ---------------------------------------------------------------------------
# macOS installer (install_macos.sh)
# ---------------------------------------------------------------------------
class TestMacOSInstaller:
    @pytest.fixture
    def plugin_dir_with_version(self, tmp_path):
        """Create a minimal plugin dir with VERSION and both scripts for dry-run tests."""
        plugin_dir = tmp_path / "kicad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "VERSION").write_text("0.1.9\n")
        repo_root = Path(__file__).parent.parent.parent.parent
        for name in ("setup_plugin_macos.command", "install_macos.sh"):
            src = repo_root / "kicad_plugin" / name
            dst = plugin_dir / name
            dst.write_text(src.read_text())
            dst.chmod(0o755)
        return plugin_dir

    def test_installer_uses_kicad_version_env(self, plugin_dir_with_version):
        result = _run_shell_script(
            plugin_dir_with_version / "install_macos.sh",
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0"},
            check=True,
        )
        assert "Detected KiCad version: 10.0" in result.stdout
        expected_dst = "~/Library/Preferences/kicad/10.0/scripting/plugins/kicad_ai_assistant".replace(
            "~", os.environ["HOME"]
        )
        assert expected_dst in result.stdout
        assert "Would run setup script" in result.stdout

    def test_installer_detects_version_from_fake_kicad_app(self, plugin_dir_with_version, tmp_path, monkeypatch):
        # Override /Applications/KiCad path by patching inside the script via env not possible,
        # so we create a fake app bundle under a temporary Applications dir and patch HOME.
        home = tmp_path / "home"
        home.mkdir()
        app = home / "Applications" / "KiCad" / "KiCad.app"
        plist = app / "Contents" / "Info.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
            '<plist version="1.0"><dict>'
            '<key>CFBundleShortVersionString</key><string>10.0.2</string>'
            '</dict></plist>'
        )

        # Patch the script to look at our fake Applications dir.
        script_path = plugin_dir_with_version / "install_macos.sh"
        script_text = script_path.read_text()
        script_text = script_text.replace(
            'KICAD_APP="/Applications/KiCad/KiCad.app"',
            f'KICAD_APP="{app}"',
        )
        patched_script = plugin_dir_with_version / "install_macos_patched.sh"
        patched_script.write_text(script_text)
        patched_script.chmod(0o755)

        result = _run_shell_script(
            patched_script,
            env={"DRY_RUN": "1", "KICAD_VERSION": ""},
            check=True,
        )
        assert "Detected KiCad version: 10.0" in result.stdout

    def test_installer_detects_version_from_existing_prefs(self, plugin_dir_with_version, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        prefs = home / "Library" / "Preferences" / "kicad"
        (prefs / "10.0").mkdir(parents=True)
        (prefs / "9.0").mkdir(parents=True)

        result = _run_shell_script(
            plugin_dir_with_version / "install_macos.sh",
            env={"DRY_RUN": "1", "KICAD_VERSION": "", "HOME": str(home)},
            check=True,
        )
        assert "Detected KiCad version: 10.0" in result.stdout

    def test_installer_fails_when_version_undetectable(self, plugin_dir_with_version, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        # Patch the script to look at a non-existent app path so neither the app
        # nor existing prefs can provide a version.
        script_path = plugin_dir_with_version / "install_macos.sh"
        script_text = script_path.read_text()
        script_text = script_text.replace(
            'KICAD_APP="/Applications/KiCad/KiCad.app"',
            'KICAD_APP="/nonexistent/KiCad.app"',
        )
        patched_script = plugin_dir_with_version / "install_macos_patched_fail.sh"
        patched_script.write_text(script_text)
        patched_script.chmod(0o755)

        result = _run_shell_script(
            patched_script,
            env={"DRY_RUN": "1", "KICAD_VERSION": "", "HOME": str(home)},
            check=False,
        )
        assert result.returncode != 0
        assert "Could not detect KiCad version" in result.stdout or "Could not detect KiCad version" in result.stderr

    def test_installer_generates_version_file_from_pyproject_toml(self, tmp_path):
        """When the plugin dir has no VERSION, install_macos.sh must generate one
        from pyproject.toml so the setup script can read the kcaa version."""
        home = tmp_path / "home"
        home.mkdir()
        repo_dir = tmp_path / "repo"
        plugin_dir = repo_dir / "kicad_plugin"
        plugin_dir.mkdir(parents=True)
        (repo_dir / "pyproject.toml").write_text('[project]\nname = "kcaa"\nversion = "7.7.7"\n')

        repo_root = Path(__file__).parent.parent.parent.parent
        real_installer = repo_root / "kicad_plugin" / "install_macos.sh"
        installer = plugin_dir / "install_macos.sh"
        installer.write_text(real_installer.read_text())
        installer.chmod(0o755)

        # Use a no-op setup script so the test doesn't create a venv/download JAR.
        (plugin_dir / "setup_plugin_macos.command").write_text("#!/bin/bash\necho 'setup noop'\n")
        (plugin_dir / "setup_plugin_macos.command").chmod(0o755)

        result = _run_shell_script(
            installer,
            env={"DRY_RUN": "0", "KICAD_VERSION": "10.0", "HOME": str(home)},
            check=True,
        )
        assert "Generated VERSION file" in result.stdout

        dst_version = (
            home
            / "Library"
            / "Preferences"
            / "kicad"
            / "10.0"
            / "scripting"
            / "plugins"
            / "kicad_ai_assistant"
            / "VERSION"
        )
        assert dst_version.exists()
        assert dst_version.read_text().strip() == "7.7.7"

    def test_installer_enables_kicad_api_when_disabled(self, tmp_path):
        """The installer should turn on api.enable_server in kicad_common.json."""
        home = tmp_path / "home"
        home.mkdir()
        prefs = home / "Library" / "Preferences" / "kicad" / "10.0"
        prefs.mkdir(parents=True)
        common_json = prefs / "kicad_common.json"
        common_json.write_text('{"api": {"enable_server": false}}')

        repo_dir = tmp_path / "repo"
        plugin_dir = repo_dir / "kicad_plugin"
        plugin_dir.mkdir(parents=True)

        repo_root = Path(__file__).parent.parent.parent.parent
        real_installer = repo_root / "kicad_plugin" / "install_macos.sh"
        installer = plugin_dir / "install_macos.sh"
        installer.write_text(real_installer.read_text())
        installer.chmod(0o755)
        (plugin_dir / "setup_plugin_macos.command").write_text("#!/bin/bash\necho 'setup noop'\n")
        (plugin_dir / "setup_plugin_macos.command").chmod(0o755)

        result = _run_shell_script(
            installer,
            env={"DRY_RUN": "0", "KICAD_VERSION": "10.0", "HOME": str(home)},
            check=True,
        )
        assert "Enabling KiCad API server" in result.stdout

        data = json.loads(common_json.read_text())
        assert data["api"]["enable_server"] is True
        backup = prefs / "kicad_common.json.kcaa-backup"
        assert backup.exists()
        assert '"enable_server": false' in backup.read_text()

    def test_installer_dry_run_reports_api_enable(self, tmp_path):
        """In dry-run mode the installer reports that it would enable the API."""
        home = tmp_path / "home"
        home.mkdir()
        prefs = home / "Library" / "Preferences" / "kicad" / "10.0"
        prefs.mkdir(parents=True)
        common_json = prefs / "kicad_common.json"
        common_json.write_text('{"api": {"enable_server": false}}')

        repo_dir = tmp_path / "repo"
        plugin_dir = repo_dir / "kicad_plugin"
        plugin_dir.mkdir(parents=True)

        repo_root = Path(__file__).parent.parent.parent.parent
        real_installer = repo_root / "kicad_plugin" / "install_macos.sh"
        installer = plugin_dir / "install_macos.sh"
        installer.write_text(real_installer.read_text())
        installer.chmod(0o755)
        (plugin_dir / "setup_plugin_macos.command").write_text("#!/bin/bash\necho 'setup noop'\n")
        (plugin_dir / "setup_plugin_macos.command").chmod(0o755)

        result = _run_shell_script(
            installer,
            env={"DRY_RUN": "1", "KICAD_VERSION": "10.0", "HOME": str(home)},
            check=True,
        )
        assert "Would enable KiCad API server" in result.stdout
        # Original file must remain untouched in dry-run mode.
        assert '"enable_server": false' in common_json.read_text()


