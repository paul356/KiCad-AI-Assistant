"""Tests for ``scripts/build_plugin_zip.py``.

Covers both archive layouts, the generated PCM metadata, and the deterministic
zip output the in-panel updater depends on.
"""

from __future__ import annotations

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
_VERSION_KEYS = {"version", "status", "kicad_version"}


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
    assert f"{builder.PCM_PLUGIN_ROOT}/__init__.py" in names
    assert f"{builder.PCM_PLUGIN_ROOT}/VERSION" in names
    assert (
        _member(pcm_zip, f"{builder.PCM_PLUGIN_ROOT}/VERSION").decode().strip()
        == project["version"]
    )
    assert builder.LEGACY_ROOT not in _top_level(names)
    assert not any(name.startswith(f"{builder.LEGACY_ROOT}/") for name in names)


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
    assert version == {
        "version": project["version"],
        "status": "stable",
        "kicad_version": "10.0",
    }

    # KiCad PCM replaces dots in the identifier when creating the install folder.
    folder = builder.pcm_package_dirname(metadata["identifier"])
    assert folder == metadata["identifier"].replace(".", "_")
    assert "." not in folder


def test_archives_are_deterministic(tmp_path: Path) -> None:
    first_legacy = builder.build_legacy(tmp_path / "legacy-a.zip")
    second_legacy = builder.build_legacy(tmp_path / "legacy-b.zip")
    assert _namelist(first_legacy) == _namelist(second_legacy)
    assert first_legacy.read_bytes() == second_legacy.read_bytes()

    first_pcm = builder.build_pcm(tmp_path / "pcm-a.zip")
    second_pcm = builder.build_pcm(tmp_path / "pcm-b.zip")
    assert _namelist(first_pcm) == _namelist(second_pcm)
    assert first_pcm.read_bytes() == second_pcm.read_bytes()


def test_legacy_archive_matches_updater_expectations(legacy_zip: Path) -> None:
    assert builder.LEGACY_ROOT == updater.ZIP_ROOT
    assert builder.LEGACY_ARCHIVE == updater.ZIP_ASSET
    assert legacy_zip.name == updater.ZIP_ASSET

    names = _namelist(legacy_zip)
    assert names
    assert all(
        name == f"{updater.ZIP_ROOT}/" or name.startswith(f"{updater.ZIP_ROOT}/") for name in names
    )


def test_shipped_plugin_readme_uses_real_install_paths() -> None:
    text = (_REPO_ROOT / "kicad_plugin" / "README.md").read_text(encoding="utf-8")

    assert "kicad_ai_plugin" not in text
    assert "APPDATA\\kicad\\10.0" not in text
    assert "Library/Preferences/kicad/10.0" not in text
    assert "Documents\\KiCad\\10.0\\scripting\\plugins" in text
    assert "Documents/KiCad/10.0/scripting/plugins" in text
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
