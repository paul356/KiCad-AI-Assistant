"""Unit tests for the plugin updater.

Every network and subprocess call is faked: no real HTTP, no real venv, and
the updater never touches this checkout (install targets are tmp dirs).
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from kicad_plugin import updater

_ZIP_URL = "https://example.test/kicad_ai_assistant.zip"
_SUMS_URL = "https://example.test/SHA256SUMS"


class _FakeResponse:
    """Minimal urlopen() response with a Content-Length header."""

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info) -> bool:
        self._body.close()
        return False


def _fake_urlopen(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> None:
    def _open(request, timeout=None):
        url = request.full_url
        if url not in routes:
            raise AssertionError(f"unexpected URL: {url}")
        entry = routes[url]
        if isinstance(entry, BaseException):
            raise entry
        return _FakeResponse(entry)  # type: ignore[arg-type]

    monkeypatch.setattr(updater, "urlopen", _open)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _make_update(zip_payload: bytes) -> tuple[updater.UpdateInfo, str]:
    digest = hashlib.sha256(zip_payload).hexdigest()
    update = updater.UpdateInfo(
        version="0.2.9",
        zip_url=_ZIP_URL,
        checksum_url=_SUMS_URL,
        page_url="https://example.test/releases/v0.2.9",
    )
    return update, f"{digest}  kicad_ai_assistant.zip\n"


def _write_plugin(plugin_dir: Path, version: str = "0.2.8") -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "VERSION").write_text(version, encoding="utf-8")
    (plugin_dir / "__init__.py").write_text("old init", encoding="utf-8")
    (plugin_dir / "ui").mkdir(exist_ok=True)
    (plugin_dir / "ui" / "greeting.py").write_text("old ui", encoding="utf-8")
    venv_python = plugin_dir / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    return venv_python


# 1. Version parsing / comparison ------------------------------------------------


def test_version_comparison_handles_prefix_and_suffix() -> None:
    assert updater._parse_version("0.2.8") == (0, 2, 8)
    assert updater._parse_version("v0.2.9") == (0, 2, 9)
    assert updater._parse_version("0.2.9-rc1") == (0, 2, 9)
    assert updater.is_newer("0.2.9", "0.2.8")
    assert updater.is_newer("v0.2.9", "0.2.8")
    assert not updater.is_newer("0.2.9", "v0.2.9")
    assert not updater.is_newer("0.2.8", "0.2.9")


# 2. check_latest ----------------------------------------------------------------


def test_check_latest_parses_github_release(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b'{"tag_name": "v0.2.9", "html_url": "https://example.test/tag/v0.2.9",'
        b' "assets": ['
        b'{"name": "kicad_ai_assistant.zip", "browser_download_url": "' + _ZIP_URL.encode() + b'"},'
        b'{"name": "SHA256SUMS", "browser_download_url": "' + _SUMS_URL.encode() + b'"}]}'
    )
    _fake_urlopen(monkeypatch, {updater.RELEASES_API: payload})

    info = updater.check_latest()

    assert info is not None
    assert info.version == "0.2.9"
    assert info.zip_url == _ZIP_URL
    assert info.checksum_url == _SUMS_URL
    assert info.page_url == "https://example.test/tag/v0.2.9"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("boom"),
        TimeoutError("timed out"),
        b"{not valid json",
    ],
)
def test_check_latest_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch, failure) -> None:
    _fake_urlopen(monkeypatch, {updater.RELEASES_API: failure})
    assert updater.check_latest(timeout=0.01) is None


# 3. Download + checksum ---------------------------------------------------------


def test_download_and_verify_accepts_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zip_payload = _zip_bytes({"kicad_ai_assistant/__init__.py": b"new"})
    update, sums = _make_update(zip_payload)
    _fake_urlopen(monkeypatch, {_SUMS_URL: sums.encode(), _ZIP_URL: zip_payload})

    path = updater.download_and_verify(update, tmp_path)

    assert path == tmp_path / "kicad_ai_assistant.zip"
    assert path.read_bytes() == zip_payload


def test_download_and_verify_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zip_payload = _zip_bytes({"kicad_ai_assistant/__init__.py": b"new"})
    update, _ = _make_update(zip_payload)
    bad_sums = f"{'0' * 64}  kicad_ai_assistant.zip\n"
    _fake_urlopen(monkeypatch, {_SUMS_URL: bad_sums.encode(), _ZIP_URL: zip_payload})

    with pytest.raises(updater.UpdateError, match="checksum mismatch"):
        updater.download_and_verify(update, tmp_path)

    assert list(tmp_path.iterdir()) == []  # no partial zip left behind


# 4. Successful install ----------------------------------------------------------


def test_install_update_replaces_tree_and_keeps_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    venv_python = _write_plugin(plugin_dir)
    zip_payload = _zip_bytes(
        {
            "kicad_ai_assistant/__init__.py": b"new init",
            "kicad_ai_assistant/VERSION": b"0.2.9",
            "kicad_ai_assistant/ui/new_module.py": b"new ui",
        }
    )
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(zip_payload)

    commands: list[list[str]] = []

    def _fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(updater.subprocess, "run", _fake_run)

    updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "new init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.9"
    assert (plugin_dir / "ui" / "new_module.py").exists()
    assert not (plugin_dir / "ui" / "greeting.py").exists()  # stale file removed
    assert venv_python.exists()  # .venv untouched by the tree swap

    backups = list((plugin_dir / ".backup").iterdir())
    assert len(backups) == 1
    backup = backups[0]
    assert backup.name.startswith("0.2.8-")
    assert (backup / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (backup / "ui" / "greeting.py").read_text(encoding="utf-8") == "old ui"
    assert not (backup / ".venv").exists()  # venv is excluded from backups

    assert commands == [
        [
            "/usr/bin/uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            "kcaa==0.2.9",
        ]
    ]


# 5. Failure rolls back ----------------------------------------------------------


def test_install_update_restores_previous_tree_on_extract_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    sentinel = plugin_dir / "data" / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"\x00sentinel\xff")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    bad_zip = tmp_path / "broken.zip"
    bad_zip.write_bytes(b"this is not a zip archive")

    called = {"run": False}
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **k: called.__setitem__("run", True),
    )

    with pytest.raises(updater.UpdateError, match="not a valid zip"):
        updater.install_update(bad_zip, plugin_dir, "0.2.9")

    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "ui" / "greeting.py").read_text(encoding="utf-8") == "old ui"
    assert not called["run"]  # venv never touched after extract failed


# 6. Dev-mode guard --------------------------------------------------------------


def test_install_update_refuses_git_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    (plugin_dir / ".git").mkdir()
    sentinel = plugin_dir / "__init__.py"
    before = sentinel.read_text(encoding="utf-8")

    called = {"run": False}
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **k: called.__setitem__("run", True),
    )

    with pytest.raises(updater.UpdateError, match="git checkout"):
        updater.install_update(tmp_path / "whatever.zip", plugin_dir, "0.2.9")

    assert not (plugin_dir / ".backup").exists()
    assert sentinel.read_text(encoding="utf-8") == before
    assert not called["run"]


# 7. Zip path traversal ----------------------------------------------------------


def test_install_update_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_payload = _zip_bytes(
        {
            "kicad_ai_assistant/__init__.py": b"new init",
            "../evil.txt": b"escaped",
        }
    )
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(zip_payload)

    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: None)

    with pytest.raises(updater.UpdateError, match="Unsafe path"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert not (tmp_path / "evil.txt").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"


# 8. Unwritable plugin dir -------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="permission checks are not enforceable here (Windows or root)",
)
def test_install_update_rejects_unwritable_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(b"unused")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: None)

    plugin_dir.chmod(0o500)
    try:
        with pytest.raises(updater.UpdateError, match="not writable"):
            updater.install_update(zip_path, plugin_dir, "0.2.9")
        assert not (plugin_dir / ".backup").exists()
        assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    finally:
        plugin_dir.chmod(0o700)
