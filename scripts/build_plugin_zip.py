#!/usr/bin/env python3
"""Build the KiCad plugin archives for manual and PCM installation.

``legacy`` produces ``dist/kicad_ai_assistant.zip``, the layout the in-panel
updater expects::

    kicad_ai_assistant/
        ...plugin tree...
        VERSION

``pcm`` produces ``dist/kicad_ai_assistant_pcm.zip`` in the layout KiCad's
Plugin and Content Manager accepts for "Install from File...":

    metadata.json
    plugins/
        ...plugin tree (same contents as kicad_plugin/)...
        VERSION
    resources/icon.png        (only when the repository already ships one)

KiCad's PCM installer inserts the package id after the first path component,
so ``plugins/__init__.py`` becomes ``<3rdparty>/plugins/<package-id>/__init__.py``.

Both layouts are written deterministically: entries are sorted, timestamps are
fixed, and permissions are constant, so rebuilding an unchanged tree produces
byte-identical archives.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "kicad_plugin"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DIST_DIR = REPO_ROOT / "dist"

LEGACY_ROOT = "kicad_ai_assistant"
LEGACY_ARCHIVE = "kicad_ai_assistant.zip"

PCM_PLUGIN_ROOT = "plugins"
PCM_IDENTIFIER = "com.github.paul356.kicad-ai-assistant"
PCM_KICAD_VERSION = "10.0"
PCM_ARCHIVE = "kicad_ai_assistant_pcm.zip"

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_EXCLUDED_DIRS = {".backup", ".venv", "__pycache__"}


def load_project(pyproject: Path = PYPROJECT) -> dict[str, Any]:
    """Return the ``[project]`` table from *pyproject*.

    Python 3.11+ uses :mod:`tomllib`. The repository still supports Python
    3.10, so a small subset parser covers the fields this builder needs there.
    """
    text = pyproject.read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        return _parse_project_subset(text)
    return tomllib.loads(text)["project"]


def _parse_project_subset(text: str) -> dict[str, Any]:
    """Parse only the ``[project]`` / ``[project.urls]`` values used here."""
    project: dict[str, Any] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip()
            continue
        if section == "project":
            if line == "authors = [":
                project["authors"] = []
                continue
            author = re.fullmatch(r'\{\s*name\s*=\s*"([^"]*)"\s*\}', line)
            if author and "authors" in project:
                project["authors"].append({"name": author.group(1)})
                continue
            license_match = re.fullmatch(r'license\s*=\s*\{\s*text\s*=\s*"([^"]*)"\s*\}', line)
            if license_match:
                project["license"] = {"text": license_match.group(1)}
                continue
            key_value = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"', line)
            if key_value:
                project[key_value.group(1)] = key_value.group(2)
        elif section == "project.urls":
            url = re.fullmatch(r'"([^"]+)"\s*=\s*"([^"]*)"', line)
            if url:
                project.setdefault("urls", {})[url.group(1)] = url.group(2)
    return project


def plugin_files(source: Path = DEFAULT_SOURCE) -> list[tuple[str, Path]]:
    """Return ``(relative_posix_path, absolute_path)`` pairs, sorted."""
    files: list[tuple[str, Path]] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        # VERSION is generated from pyproject.toml, never copied from source.
        if path.suffix == ".pyc" or relative.as_posix() == "VERSION":
            continue
        files.append((relative.as_posix(), path))
    return sorted(files, key=lambda item: item[0])


def find_icon(source: Path = DEFAULT_SOURCE) -> Path | None:
    """Return an existing repository icon, or ``None``; never invent one."""
    for candidate in (source / "icon.png", REPO_ROOT / "icon.png", REPO_ROOT / "docs" / "icon.png"):
        if candidate.is_file():
            return candidate
    return None


def pcm_package_dirname(identifier: str = PCM_IDENTIFIER) -> str:
    """Directory KiCad PCM uses for the installed package."""
    return identifier.replace(".", "_")


def pcm_metadata(project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build ``metadata.json`` satisfying KiCad's ``pcm.v1`` schema."""
    project = project if project is not None else load_project()
    urls = project.get("urls") or {}
    license_info = project.get("license") or {}
    license_id = license_info.get("text") if isinstance(license_info, dict) else license_info
    authors = project.get("authors") or [{"name": "KiCad MCP Contributors"}]
    contact = {"web": urls["Homepage"]} if urls.get("Homepage") else {}
    resources = {}
    if urls.get("Homepage"):
        resources["homepage"] = urls["Homepage"]
    if urls.get("Bug Tracker"):
        resources["bug tracker"] = urls["Bug Tracker"]

    return {
        "name": "KiCad AI Assistant",
        "description": project["description"],
        "description_full": project["description"],
        "identifier": PCM_IDENTIFIER,
        "type": "plugin",
        "author": {"name": authors[0].get("name", "KiCad MCP Contributors"), "contact": contact},
        "license": license_id or "MIT",
        "resources": resources,
        "versions": [
            {
                "version": project["version"],
                "status": "stable",
                "kicad_version": PCM_KICAD_VERSION,
            }
        ],
    }


def _file_mode(path: Path) -> int:
    """Return the stored zip mode, preserving the source executable bit."""
    return 0o100755 if path.stat().st_mode & 0o111 else 0o100644


def _tree_entries(prefix: str, source: Path, version: str) -> list[tuple[str, bytes, int]]:
    """Return directory, plugin-tree and generated ``VERSION`` entries."""
    files = plugin_files(source)
    directories = {prefix}
    for relative, _ in files:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add(f"{prefix}/{'/'.join(parts[:index])}")

    entries: list[tuple[str, bytes, int]] = [
        (directory + "/", b"", 0o40755) for directory in directories
    ]
    entries.extend(
        (f"{prefix}/{relative}", path.read_bytes(), _file_mode(path)) for relative, path in files
    )
    entries.append((f"{prefix}/VERSION", f"{version}\n".encode(), 0o100644))
    return entries


def _write_archive(output: Path, entries: list[tuple[str, bytes, int]]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = mode << 16
            if name.endswith("/"):
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr |= 0x10
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return output


def build_legacy(
    output: Path = DIST_DIR / LEGACY_ARCHIVE,
    *,
    source: Path = DEFAULT_SOURCE,
    project: dict[str, Any] | None = None,
) -> Path:
    """Build the manual-install / updater archive rooted at ``kicad_ai_assistant``."""
    project = project if project is not None else load_project()
    entries = _tree_entries(LEGACY_ROOT, source, str(project["version"]))
    return _write_archive(output, entries)


def build_pcm(
    output: Path = DIST_DIR / PCM_ARCHIVE,
    *,
    source: Path = DEFAULT_SOURCE,
    project: dict[str, Any] | None = None,
) -> Path:
    """Build the Plugin and Content Manager archive."""
    project = project if project is not None else load_project()
    entries = _tree_entries(PCM_PLUGIN_ROOT, source, str(project["version"]))
    metadata = json.dumps(pcm_metadata(project), indent=2, sort_keys=True) + "\n"
    entries.append(("metadata.json", metadata.encode("utf-8"), 0o100644))

    icon = find_icon(source)
    if icon is not None:
        entries.append(("resources/", b"", 0o40755))
        entries.append(("resources/icon.png", icon.read_bytes(), _file_mode(icon)))

    return _write_archive(output, entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("layout", choices=("legacy", "pcm"))
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="plugin source directory (default: kicad_plugin)",
    )
    args = parser.parse_args(argv)

    output = args.output or DIST_DIR / (LEGACY_ARCHIVE if args.layout == "legacy" else PCM_ARCHIVE)
    if args.layout == "legacy":
        created = build_legacy(output, source=args.source)
    else:
        created = build_pcm(output, source=args.source)
    print(f"Created {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
