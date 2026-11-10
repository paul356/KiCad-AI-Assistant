"""Tests for ``scripts/build_plugin_zip.py``.

Covers both archive layouts, the generated PCM metadata, and the deterministic
zip output the in-panel updater depends on.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import re
import sys
import zipfile

import pytest

from kicad_plugin import updater

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "build_plugin_zip.py"

_spec = importlib.util.spec_from_file_location("build_plugin_zip", _SCRIPT)
assert _spec is not None and _spec.loader is not None
builder = importlib.util.module_from_spec(_spec)
sys.modules["build_plugin_zip"] = builder
_spec.loader.exec_module(builder)

_PACKAGE_KEYS = {
    "name",
    "description",
    "description_full",
    "identifier",
    "type",
    "author",
    "license",
    "resources",
    "versions",
}
_VERSION_KEYS = {"version", "status", "kicad_version", "keep_on_update"}


def _namelist(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def _top_level(names: list[str]) -> set[str]:
    return {name.split("/", 1)[0] for name in names}


@pytest.fixture()
def project() -> dict:
    return builder.load_project()


@pytest.fixture()
def legacy_zip(tmp_path: Path) -> Path:
    return builder.build_legacy(tmp_path / builder.LEGACY_ARCHIVE)


@pytest.fixture()
def pcm_zip(tmp_path: Path) -> Path:
    return builder.build_pcm(tmp_path / builder.PCM_ARCHIVE)


def test_legacy_archive_layout(legacy_zip: Path, project: dict) -> None:
    names = _namelist(legacy_zip)

    assert _top_level(names) == {builder.LEGACY_ROOT}
    assert f"{builder.LEGACY_ROOT}/__init__.py" in names
    assert f"{builder.LEGACY_ROOT}/VERSION" in names
    assert (
        _member(legacy_zip, f"{builder.LEGACY_ROOT}/VERSION").decode().strip() == project["version"]
    )
    assert f"{builder.LEGACY_ROOT}/ui/panel.py" in names


def test_pcm_archive_layout(pcm_zip: Path, project: dict) -> None:
    names = _namelist(pcm_zip)

    assert "metadata.json" in names
    assert "plugins/__init__.py" in names
    assert "plugins/VERSION" in names
    assert "plugins/ui/panel.py" in names
    assert _member(pcm_zip, "plugins/VERSION").decode().strip() == project["version"]
    assert builder.LEGACY_ROOT not in _top_level(names)
    assert not any(name.startswith(f"{builder.LEGACY_ROOT}/") for name in names)
    assert not any(name.startswith("plugins/kicad_ai_assistant/") for name in names)


def test_pcm_metadata_contract(pcm_zip: Path, project: dict) -> None:
    metadata = json.loads(_member(pcm_zip, "metadata.json"))

    assert set(metadata) == _PACKAGE_KEYS
    assert metadata["type"] == "plugin"
    assert metadata["license"] == project["license"]["text"]
    assert metadata["author"]["name"] == project["authors"][0]["name"]
    assert isinstance(metadata["author"]["contact"], dict)
    assert isinstance(metadata["resources"], dict)
    assert re.fullmatch(r"^[a-zA-Z][-a-zA-Z0-9.]{0,98}[a-zA-Z0-9]$", metadata["identifier"])

    assert len(metadata["versions"]) == 1
    version = metadata["versions"][0]
    assert set(version) == _VERSION_KEYS
    assert version["version"] == project["version"]
    assert version["status"] == "stable"
    assert version["kicad_version"] == "10.0"
    assert any(".venv" in pattern for pattern in version["keep_on_update"])
    assert any(
        "freerouting" in pattern and pattern.endswith(".jar")
        for pattern in version["keep_on_update"]
    )

    # KiCad PCM replaces dots in the identifier when creating the install folder.
    assert builder.pcm_package_dirname(metadata["identifier"]) == (
        "com_github_paul356_kicad-ai-assistant"
    )


def test_archives_are_deterministic(tmp_path: Path) -> None:
    first_legacy = builder.build_legacy(tmp_path / "legacy-a.zip")
    second_legacy = builder.build_legacy(tmp_path / "legacy-b.zip")
    assert _namelist(first_legacy) == _namelist(second_legacy)
    assert first_legacy.read_bytes() == second_legacy.read_bytes()

    first_pcm = builder.build_pcm(tmp_path / "pcm-a.zip")
    second_pcm = builder.build_pcm(tmp_path / "pcm-b.zip")
    assert _namelist(first_pcm) == _namelist(second_pcm)
    assert first_pcm.read_bytes() == second_pcm.read_bytes()


def _simulate_pcm_package_member(name: str, clean_identifier: str) -> str | None:
    """Mirror KiCad's ``extract()`` under ``<3rdparty>/plugins``.

    PCM inserts the package id after the first path component, so a
    ``plugins/`` member lands directly inside the package directory.
    """
    parts = name.split("/")
    if parts[0] != "plugins" or len(parts) < 2:
        return None
    return "/".join([clean_identifier, *parts[1:]])


def test_pcm_extraction_places_plugin_contents_at_package_root(pcm_zip: Path) -> None:
    metadata = json.loads(_member(pcm_zip, "metadata.json"))
    clean_identifier = builder.pcm_package_dirname(metadata["identifier"])
    extracted = {
        mapped
        for name in _namelist(pcm_zip)
        if (mapped := _simulate_pcm_package_member(name, clean_identifier)) is not None
    }

    assert f"{clean_identifier}/__init__.py" in extracted
    assert f"{clean_identifier}/VERSION" in extracted
    assert f"{clean_identifier}/ui/panel.py" in extracted
    assert not any(name.startswith(f"{clean_identifier}/kicad_ai_assistant/") for name in extracted)


def _modes(path: Path) -> dict[str, int]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: info.external_attr >> 16 for info in archive.infolist()}


def test_archives_ship_complete_tree_and_executable_mode(legacy_zip: Path, pcm_zip: Path) -> None:
    source_names = [relative for relative, _ in builder.plugin_files()]
    assert source_names

    legacy_names = set(_namelist(legacy_zip))
    pcm_names = set(_namelist(pcm_zip))
    for relative in source_names:
        assert f"{builder.LEGACY_ROOT}/{relative}" in legacy_names
        assert f"plugins/{relative}" in pcm_names

    legacy_modes = _modes(legacy_zip)
    pcm_modes = _modes(pcm_zip)
    assert legacy_modes[f"{builder.LEGACY_ROOT}/setup_plugin.sh"] == 0o100755
    assert pcm_modes["plugins/setup_plugin.sh"] == 0o100755
    assert legacy_modes[f"{builder.LEGACY_ROOT}/__init__.py"] == 0o100644
    assert pcm_modes["plugins/__init__.py"] == 0o100644
    assert legacy_modes[f"{builder.LEGACY_ROOT}/"] == 0o40755
    assert pcm_modes["plugins/"] == 0o40755


def test_legacy_archive_matches_updater_expectations(legacy_zip: Path) -> None:
    assert builder.LEGACY_ROOT == updater.ZIP_ROOT
    assert builder.LEGACY_ARCHIVE == updater.ZIP_ASSET
    assert legacy_zip.name == updater.ZIP_ASSET

    names = _namelist(legacy_zip)
    assert names
    assert all(
        name == f"{updater.ZIP_ROOT}/" or name.startswith(f"{updater.ZIP_ROOT}/") for name in names
    )


@pytest.mark.parametrize("relative_path", ["README.md", "__init__.py"])
def test_shipped_plugin_files_use_real_install_paths(relative_path: str) -> None:
    text = (_REPO_ROOT / "kicad_plugin" / relative_path).read_text(encoding="utf-8")
    docs = ast.get_docstring(ast.parse(text)) if relative_path.endswith(".py") else text
    assert docs is not None

    assert "kicad_ai_plugin" not in docs
    assert "APPDATA\\kicad\\10.0\\scripting" not in docs
    assert "APPDATA\\kicad\\<ver>\\scripting" not in docs
    assert "Library/Preferences/kicad/10.0/scripting" not in docs
    assert "Library/Preferences/kicad/<ver>/scripting" not in docs
    assert "Documents\\KiCad" in docs
    assert "scripting\\plugins" in docs


def test_shipped_plugin_readme_mentions_api_toggle() -> None:
    text = (_REPO_ROOT / "kicad_plugin" / "README.md").read_text(encoding="utf-8")

    assert "Enable KiCad API" in text


@pytest.mark.parametrize("readme", ["README.md", "README_CN.md"])
def test_readmes_only_reference_old_folder_name_historically(readme: str) -> None:
    historical_markers = (
        "not `kicad_ai_plugin`",
        "not kicad_ai_plugin",
        "old instructions",
        "旧说明",
    )
    for line in (_REPO_ROOT / readme).read_text(encoding="utf-8").splitlines():
        if "kicad_ai_plugin" not in line:
            continue
        assert any(marker in line for marker in historical_markers)
