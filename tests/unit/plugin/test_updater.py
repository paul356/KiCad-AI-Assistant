"""Unit tests for the plugin updater.

Every network and subprocess call is faked: no real HTTP, no real venv, and
install targets are temporary directories.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import subprocess
import threading
import zipfile

import pytest

from kicad_plugin import updater

_ZIP_URL = "https://objects.githubusercontent.com/kicad_ai_assistant.zip"
_SUMS_URL = "https://github.com/paul356/KiCad-AI-Assistant/releases/download/v0.2.9/SHA256SUMS"


class _FakeResponse:
    """Minimal opener response with a Content-Length header."""

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


def _fake_open(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> None:
    def _open(request, timeout=None):
        url = request.full_url
        if url not in routes:
            raise AssertionError(f"unexpected URL: {url}")
        entry = routes[url]
        if isinstance(entry, BaseException):
            raise entry
        return _FakeResponse(entry)  # type: ignore[arg-type]

    monkeypatch.setattr(updater, "_open_url", _open)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _symlink_zip_bytes(name: str, target: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.create_system = 3  # Unix: external_attr carries the file mode
        info.external_attr = 0o120777 << 16
        archive.writestr(info, target)
    return buffer.getvalue()


def _make_update(
    zip_payload: bytes,
    *,
    checksum_url: str = _SUMS_URL,
    zip_url: str = _ZIP_URL,
) -> tuple[updater.UpdateInfo, str]:
    digest = hashlib.sha256(zip_payload).hexdigest()
    update = updater.UpdateInfo(
        version="0.2.9",
        zip_url=zip_url,
        checksum_url=checksum_url,
        page_url="https://example.test/releases/v0.2.9",
    )
    return update, f"{digest}  kicad_ai_assistant.zip\n"


def _release_zip() -> bytes:
    return _zip_bytes(
        {
            "kicad_ai_assistant/__init__.py": b"new init",
            "kicad_ai_assistant/VERSION": b"0.2.9",
            "kicad_ai_assistant/ui/new_module.py": b"new ui",
            "kicad_ai_assistant/README.md": b"new readme",
        }
    )


def _site_packages(plugin_dir: Path) -> Path:
    if os.name == "nt":
        return plugin_dir / ".venv" / "Lib" / "site-packages"
    return plugin_dir / ".venv" / "lib" / "python3.10" / "site-packages"


def _write_plugin(plugin_dir: Path, version: str = "0.2.8") -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "VERSION").write_text(version, encoding="utf-8")
    (plugin_dir / "__init__.py").write_text("old init", encoding="utf-8")
    (plugin_dir / "ui").mkdir(exist_ok=True)
    (plugin_dir / "ui" / "greeting.py").write_text("old ui", encoding="utf-8")
    if os.name == "nt":
        venv_python = plugin_dir / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = plugin_dir / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")

    site = _site_packages(plugin_dir)
    (site / "kcaa").mkdir(parents=True)
    (site / "kcaa" / "__init__.py").write_text('__version__ = "0.2.8"', encoding="utf-8")
    dist_info = site / "kcaa-0.2.8.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: kcaa\nVersion: 0.2.8\n", encoding="utf-8")
    return venv_python


def _completed(command, returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _mock_uv(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dry_run_returncode: int = 0,
    dry_run_stderr: str = "",
    install_returncode: int = 0,
    install_stderr: str = "",
    calls: list[list[str]] | None = None,
    on_install=None,
) -> None:
    """Fake uv discovery and subprocess runs for pre-flight + install."""
    monkeypatch.setattr(
        updater.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    def _run(command, **kwargs):
        if calls is not None:
            calls.append(list(command))
        if "--dry-run" in command:
            return _completed(command, dry_run_returncode, stderr=dry_run_stderr)
        if on_install is not None:
            on_install(command)
        return _completed(command, install_returncode, stderr=install_stderr)

    monkeypatch.setattr(updater.subprocess, "run", _run)


# 1. Update detection ------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.2.9", "0.2.9", False),
        ("v0.2.9", "0.2.9", False),
        ("0.2.9+abc", "0.2.9", False),
        ("0.2.9", "0.2.8", True),
        ("0.2.4-bugfix2", "0.2.4-bugfix1", True),
        ("", "0.2.9", False),
        ("0.2.9", "", False),
        ("   ", "0.2.9", False),
        ("v+", "0.2.9", False),
        ("0.2.9", "  v0.2.9+local  ", False),
        ("0.2.9-RC1", "0.2.9-rc1", False),
    ],
)
def test_is_update_available_compares_normalized_tags(
    latest: str, current: str, expected: bool
) -> None:
    assert updater.is_update_available(latest, current) is expected


def test_panel_update_branch_uses_tag_inequality() -> None:
    from kicad_plugin.ui import panel

    assert panel._should_offer_update("0.2.9", "0.2.8") is True
    assert panel._should_offer_update("v0.2.9+local", "0.2.9") is False


@pytest.mark.parametrize(
    "bad_version",
    [
        "1.0; rm -rf /",
        "1.0 --index-url evil",
        "../../x",
        "",
    ],
)
def test_unsafe_version_rejected(tmp_path: Path, bad_version: str) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    plugin_dir.mkdir()

    with pytest.raises(updater.UpdateError, match="Unsafe version"):
        updater.install_update(tmp_path / "release.zip", plugin_dir, bad_version)

    assert not (plugin_dir / ".backup").exists()


# 2. URL validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/x",
        "file:///etc/passwd",
        "https://evil.example.com/kicad_ai_assistant.zip",
        "https://api.github.com.evil.example.com/kicad_ai_assistant.zip",
    ],
)
def test_url_validation_rejects_non_https_and_unknown_host(url: str) -> None:
    with pytest.raises(updater.UpdateError, match="Refusing"):
        updater._checked_url(url)


def test_url_validation_accepts_github_hosts() -> None:
    for url in (
        updater.RELEASES_API,
        "https://github.com/paul356/KiCad-AI-Assistant/releases/download/v0.2.9/kicad_ai_assistant.zip",
        "https://objects.githubusercontent.com/kicad_ai_assistant.zip",
        "https://release-assets.githubusercontent.com/github-production-release-asset/x",
    ):
        assert updater._checked_url(url) == url


def test_redirect_handler_rejects_unknown_host() -> None:
    handler = updater._CheckedRedirectHandler()
    with pytest.raises(updater.UpdateError, match="untrusted host"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/x")


def test_redirect_handler_allows_release_cdn() -> None:
    handler = updater._CheckedRedirectHandler()
    request = updater.Request("https://github.com/paul356/KiCad-AI-Assistant/releases")
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://release-assets.githubusercontent.com/github-production-release-asset/x",
    )
    assert redirected is not None
    assert redirected.full_url.startswith("https://release-assets.githubusercontent.com/")


def test_download_validates_url_at_request_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    update = updater.UpdateInfo(
        version="0.2.9",
        zip_url=_ZIP_URL,
        checksum_url="https://evil.example.com/SHA256SUMS",
        page_url=updater.RELEASE_PAGE,
    )

    def _no_network(*args, **kwargs):
        pytest.fail("network was touched before URL validation")

    monkeypatch.setattr(updater, "_open_url", _no_network)
    with pytest.raises(updater.UpdateError, match="untrusted host"):
        updater.download_and_verify(update, tmp_path)


# 3. check_latest ----------------------------------------------------------------


def test_check_latest_parses_github_release(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b'{"tag_name": "v0.2.9", "html_url": "https://example.test/tag/v0.2.9",'
        b' "assets": ['
        b'{"name": "kicad_ai_assistant.zip", "browser_download_url": "' + _ZIP_URL.encode() + b'"},'
        b'{"name": "SHA256SUMS", "browser_download_url": "' + _SUMS_URL.encode() + b'"}]}'
    )
    _fake_open(monkeypatch, {updater.RELEASES_API: payload})

    info = updater.check_latest()

    assert info is not None
    assert info.version == "0.2.9"
    assert info.zip_url == _ZIP_URL
    assert info.checksum_url == _SUMS_URL
    assert info.page_url == "https://example.test/tag/v0.2.9"


def _release_data(tag: str) -> dict[str, object]:
    return {
        "tag_name": tag,
        "html_url": "https://example.test/tag/latest",
        "assets": [
            {"name": "kicad_ai_assistant.zip", "browser_download_url": _ZIP_URL},
            {"name": "SHA256SUMS", "browser_download_url": _SUMS_URL},
        ],
    }


def test_update_from_release_drops_pep440_local_segment() -> None:
    info = updater._update_from_release(_release_data("v0.2.9+build"))

    assert info is not None
    assert info.version == "0.2.9"
    assert updater.is_update_available(info.version, "0.2.8")
    assert updater._validate_version(info.version) == "0.2.9"


@pytest.mark.parametrize("tag", ["v+", "+", "v", "   +  "])
def test_update_from_release_rejects_tag_that_normalizes_to_empty(tag: str) -> None:
    assert updater._update_from_release(_release_data(tag)) is None


def test_check_latest_rejects_tag_with_only_local_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        b'{"tag_name": "v+", "assets": ['
        b'{"name": "kicad_ai_assistant.zip", "browser_download_url": "' + _ZIP_URL.encode() + b'"},'
        b'{"name": "SHA256SUMS", "browser_download_url": "' + _SUMS_URL.encode() + b'"}]}'
    )
    _fake_open(monkeypatch, {updater.RELEASES_API: payload})

    assert updater.check_latest() is None


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("0.2.4-bugfix2", "0.2.4-bugfix2"), ("0.2.9-rc1", "0.2.9-rc1")],
)
def test_update_from_release_preserves_non_local_suffixes(tag: str, expected: str) -> None:
    info = updater._update_from_release(_release_data(tag))

    assert info is not None
    assert info.version == expected


@pytest.mark.parametrize(
    "failure",
    [
        OSError("boom"),
        TimeoutError("timed out"),
        b"{not valid json",
    ],
)
def test_check_latest_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch, failure) -> None:
    _fake_open(monkeypatch, {updater.RELEASES_API: failure})
    assert updater.check_latest(timeout=0.01) is None


# 4. Download + checksum ---------------------------------------------------------


def test_download_and_verify_accepts_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zip_payload = _zip_bytes({"kicad_ai_assistant/__init__.py": b"new"})
    update, sums = _make_update(zip_payload)
    _fake_open(monkeypatch, {_SUMS_URL: sums.encode(), _ZIP_URL: zip_payload})

    path = updater.download_and_verify(update, tmp_path)

    assert path == tmp_path / "kicad_ai_assistant.zip"
    assert path.read_bytes() == zip_payload


def test_download_and_verify_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zip_payload = _zip_bytes({"kicad_ai_assistant/__init__.py": b"new"})
    update, _ = _make_update(zip_payload)
    bad_sums = f"{'0' * 64}  kicad_ai_assistant.zip\n"
    _fake_open(monkeypatch, {_SUMS_URL: bad_sums.encode(), _ZIP_URL: zip_payload})

    with pytest.raises(updater.UpdateError, match="checksum mismatch"):
        updater.download_and_verify(update, tmp_path)

    assert list(tmp_path.iterdir()) == []  # no partial zip left behind


def test_checksum_parser_accepts_dist_prefix_and_rejects_conflicts() -> None:
    digest = "a" * 64
    assert updater._lookup_checksum(
        f"{digest}  dist/kicad_ai_assistant.zip\n", "kicad_ai_assistant.zip"
    )
    conflicting = f"{'a' * 64}  kicad_ai_assistant.zip\n{'b' * 64}  dist/kicad_ai_assistant.zip\n"
    with pytest.raises(updater.UpdateError, match="Conflicting checksums"):
        updater._lookup_checksum(conflicting, "kicad_ai_assistant.zip")


def test_checksum_parser_ignores_malformed_decoy_lines() -> None:
    digest = "c" * 64
    text = (
        f"{'d' * 64}  other.zip\n"
        f"deadbeef  kicad_ai_assistant.zip\n"
        f"{digest}  kicad_ai_assistant.zip\n"
    )
    assert updater._lookup_checksum(text, "kicad_ai_assistant.zip") == digest


def test_fetch_text_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    zip_payload = _zip_bytes({"kicad_ai_assistant/__init__.py": b"new"})
    update, _ = _make_update(zip_payload)
    monkeypatch.setattr(updater, "_MAX_TEXT_BYTES", 8)
    _fake_open(monkeypatch, {_SUMS_URL: b"x" * 9, _ZIP_URL: zip_payload})

    with pytest.raises(updater.UpdateError, match="unexpectedly large"):
        updater.download_and_verify(update, tmp_path)


def test_download_stream_rejects_oversized_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(updater, "_MAX_ARCHIVE_BYTES", 4)
    _fake_open(monkeypatch, {_ZIP_URL: b"12345"})

    with pytest.raises(updater.UpdateError, match="too large"):
        updater._download_file(_ZIP_URL, tmp_path / "release.zip", None)


# 5. Successful install ----------------------------------------------------------


def test_install_update_replaces_tree_updates_venv_and_keeps_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    venv_python = _write_plugin(plugin_dir)
    jar = plugin_dir / "freerouting-1.9.jar"
    jar.write_bytes(b"jar-bytes")
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    commands: list[list[str]] = []

    def _on_install(command):
        # The plugin tree must be swapped before the venv package is touched.
        assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.9"
        assert (plugin_dir / ".backup").is_dir()

    _mock_uv(monkeypatch, calls=commands, on_install=_on_install)

    updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "new init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.9"
    assert (plugin_dir / "ui" / "new_module.py").exists()
    assert not (plugin_dir / "ui" / "greeting.py").exists()  # stale file removed
    assert venv_python.exists()  # .venv untouched by the tree swap
    assert jar.read_bytes() == b"jar-bytes"  # setup_plugin artifact survives

    backups = list((plugin_dir / ".backup").iterdir())
    assert len(backups) == 1
    backup = backups[0]
    assert backup.name.startswith("0.2.8-")
    assert (backup / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (backup / "ui" / "greeting.py").read_text(encoding="utf-8") == "old ui"
    assert not (backup / ".venv").exists()  # venv tree is excluded
    assert not (backup / "_venv").exists()  # no venv snapshot is taken
    assert not updater.has_pending_recovery(plugin_dir)

    dry_run = [
        "/usr/bin/uv",
        "pip",
        "install",
        "--dry-run",
        "--python",
        str(venv_python),
        "kcaa==0.2.9",
    ]
    install_command = [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        str(venv_python),
        "--upgrade",
        "kcaa==0.2.9",
    ]
    assert dry_run in commands
    assert install_command in commands
    assert commands.index(dry_run) < commands.index(install_command)


def test_install_update_rehashes_when_expected_digest_given(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    with pytest.raises(updater.UpdateError, match="checksum mismatch"):
        updater.install_update(
            zip_path,
            plugin_dir,
            "0.2.9",
            expected_sha256="0" * 64,
        )

    assert not (plugin_dir / ".backup").exists()
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"


# 6. Failure rolls back the plugin tree ----------------------------------------


def test_install_update_restores_tree_when_venv_step_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    site = _site_packages(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    calls: list[list[str]] = []
    _mock_uv(
        monkeypatch,
        install_returncode=1,
        install_stderr="resolver exploded",
        calls=calls,
    )

    with pytest.raises(updater.UpdateError) as excinfo:
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    message = str(excinfo.value)
    assert "kcaa package failed" in message
    assert "setup_plugin.sh" in message
    assert excinfo.value.restored is None
    assert excinfo.value.backup_dir is not None
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "ui" / "greeting.py").read_text(encoding="utf-8") == "old ui"
    assert not (plugin_dir / "ui" / "new_module.py").exists()
    assert (site / "kcaa" / "__init__.py").read_text(encoding="utf-8") == '__version__ = "0.2.8"'
    assert not updater.has_pending_recovery(plugin_dir)

    backups = list((plugin_dir / ".backup").iterdir())
    assert len(backups) == 1
    assert not (backups[0] / ".venv").exists()
    assert not (backups[0] / "_venv").exists()
    assert any("--dry-run" in command for command in calls)
    assert any("--dry-run" not in command for command in calls)


def test_install_update_restores_previous_tree_on_extract_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    sentinel = plugin_dir / "data" / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"\x00sentinel\xff")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    jar = plugin_dir / "freerouting-1.9.jar"
    jar.write_bytes(b"jar-bytes")

    bad_zip = tmp_path / "broken.zip"
    bad_zip.write_bytes(b"this is not a zip archive")

    calls: list[list[str]] = []
    _mock_uv(monkeypatch, calls=calls)

    with pytest.raises(updater.UpdateError, match="not a valid zip"):
        updater.install_update(bad_zip, plugin_dir, "0.2.9")

    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
    assert jar.read_bytes() == b"jar-bytes"
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "ui" / "greeting.py").read_text(encoding="utf-8") == "old ui"
    assert calls and all("--dry-run" in command for command in calls)
    assert not updater.has_pending_recovery(plugin_dir)


def test_install_update_reports_inconsistent_state_when_restore_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    bad_zip = tmp_path / "broken.zip"
    bad_zip.write_bytes(b"not a zip")

    def _fail_restore(*args, **kwargs):
        raise OSError("files are locked")

    monkeypatch.setattr(updater, "_restore_tree_from_backup", _fail_restore)
    _mock_uv(monkeypatch)

    with pytest.raises(updater.UpdateError) as excinfo:
        updater.install_update(bad_zip, plugin_dir, "0.2.9")

    assert excinfo.value.restored is False
    assert excinfo.value.backup_dir is not None
    assert "could not be fully restored" in str(excinfo.value)
    assert updater.has_pending_recovery(plugin_dir)


def test_install_update_aborts_when_cancel_event_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())
    cancel = threading.Event()
    cancel.set()
    _mock_uv(monkeypatch)

    with pytest.raises(updater.UpdateError, match="cancelled"):
        updater.install_update(zip_path, plugin_dir, "0.2.9", cancel_event=cancel)

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert not (plugin_dir / "ui" / "new_module.py").exists()
    assert not updater.has_pending_recovery(plugin_dir)
    backups = list((plugin_dir / ".backup").iterdir())
    assert len(backups) == 1
    assert not (backups[0] / ".venv").exists()
    assert not (backups[0] / "_venv").exists()


# 7. Dev-mode guard + uv pre-flight ---------------------------------------------


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


def test_install_update_requires_uv_before_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    monkeypatch.setattr(updater.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess must not run without uv"),
    )

    with pytest.raises(updater.UpdateError, match="uv was not found"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert not (plugin_dir / ".backup").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"
    assert not updater.has_pending_recovery(plugin_dir)


def test_install_update_requires_existing_plugin_dir(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    with pytest.raises(updater.UpdateError, match="Plugin directory not found"):
        updater.install_update(zip_path, missing, "0.2.9")

    assert not missing.exists()


def test_install_update_requires_venv_before_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    plugin_dir.mkdir()
    (plugin_dir / "VERSION").write_text("0.2.8", encoding="utf-8")
    (plugin_dir / "__init__.py").write_text("old init", encoding="utf-8")
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess must not run without a venv"),
    )

    with pytest.raises(updater.UpdateError, match="virtual environment not found"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert not (plugin_dir / ".backup").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"


def test_install_update_dry_run_failure_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())

    calls: list[list[str]] = []
    _mock_uv(
        monkeypatch,
        dry_run_returncode=1,
        dry_run_stderr="No solution found for kcaa==0.2.9",
        calls=calls,
    )

    with pytest.raises(updater.UpdateError, match="pre-flight") as excinfo:
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert "No solution found" in str(excinfo.value)
    assert not (plugin_dir / ".backup").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"
    assert len(calls) == 1 and "--dry-run" in calls[0]
    assert not updater.has_pending_recovery(plugin_dir)


def test_preflight_retries_alternate_dry_run_placement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    venv_python = _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())
    calls: list[list[str]] = []

    def _run(command, **kwargs):
        calls.append(list(command))
        if "--dry-run" in command and command[3] == "--dry-run":
            return _completed(command, 2, stderr="error: unexpected argument '--dry-run' found")
        return _completed(command, 0)

    monkeypatch.setattr(
        updater.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(updater.subprocess, "run", _run)

    updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert calls[0][3] == "--dry-run"
    assert calls[1][3] == "--python" and calls[1][5] == "--dry-run"
    assert calls[2] == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        str(venv_python),
        "--upgrade",
        "kcaa==0.2.9",
    ]


def test_preflight_skips_when_uv_rejects_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_release_zip())
    calls: list[list[str]] = []

    def _run(command, **kwargs):
        calls.append(list(command))
        if "--dry-run" in command:
            return _completed(command, 2, stderr="error: unexpected argument '--dry-run' found")
        return _completed(command, 0)

    monkeypatch.setattr(
        updater.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(updater.subprocess, "run", _run)

    updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.9"
    assert len(calls) == 3
    assert all("--dry-run" in command for command in calls[:2])
    assert "--dry-run" not in calls[2]


# 8. Archive path safety ---------------------------------------------------------


@pytest.mark.parametrize(
    "member",
    [
        "kicad_ai_assistant/C:/x",
        "kicad_ai_assistant/C:\\x",
        "kicad_ai_assistant//x",
        "../evil.txt",
        "/abs/x",
    ],
)
def test_install_update_rejects_unsafe_archive_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, member: str
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_payload = _zip_bytes(
        {
            "kicad_ai_assistant/__init__.py": b"new init",
            member: b"escaped",
        }
    )
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(zip_payload)

    _mock_uv(monkeypatch)

    with pytest.raises(updater.UpdateError, match="Unsafe"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert not (tmp_path / "evil.txt").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"


def test_extract_rejects_archive_with_too_many_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_zip_bytes({f"kicad_ai_assistant/file{i}.py": b"x" for i in range(3)}))
    monkeypatch.setattr(updater, "_MAX_ARCHIVE_ENTRIES", 2)
    _mock_uv(monkeypatch)

    with pytest.raises(updater.UpdateError, match="too many entries"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"


def test_extract_materializes_symlink_entry_as_regular_file(tmp_path: Path) -> None:
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(_symlink_zip_bytes("kicad_ai_assistant/link", "/etc/passwd"))
    dest = tmp_path / "extract"

    updater._extract_zip(zip_path, dest)

    extracted = dest / "link"
    assert extracted.exists()
    assert not extracted.is_symlink()
    assert extracted.read_text(encoding="utf-8") == "/etc/passwd"
    assert extracted.resolve().is_relative_to(dest.resolve())


@pytest.mark.parametrize(
    "member",
    [
        "../evil.txt",
        "/abs/evil.txt",
        "kicad_ai_assistant/../evil.txt",
        "kicad_ai_assistant/..\\evil.txt",
        "kicad_ai_assistant/C:/evil.txt",
        "C:\\evil.txt",
        "kicad_ai_assistant//evil.txt",
        "kicad_ai_assistant/evil.txt:stream",
    ],
)
def test_safe_member_rejects_unsafe_paths(member: str) -> None:
    with pytest.raises(updater.UpdateError, match="Unsafe"):
        updater._safe_member(member)


def test_safe_member_accepts_relative_paths_and_skips_directories() -> None:
    assert updater._safe_member("./kicad_ai_assistant/ui/panel.py") == PurePosixPath(
        "kicad_ai_assistant/ui/panel.py"
    )
    assert updater._safe_member("kicad_ai_assistant/ui/") is None
    assert updater._safe_member("__MACOSX/kicad_ai_assistant/x") is None


def test_install_update_rejects_zip_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    zip_path = tmp_path / "release.zip"
    zip_path.write_bytes(
        _zip_bytes(
            {
                "kicad_ai_assistant/__init__.py": b"new init",
                "kicad_ai_assistant/VERSION": b"0.2.8",
            }
        )
    )
    _mock_uv(monkeypatch)

    with pytest.raises(updater.UpdateError, match="does not match"):
        updater.install_update(zip_path, plugin_dir, "0.2.9")

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
    assert (plugin_dir / "VERSION").read_text(encoding="utf-8") == "0.2.8"
    assert not updater.has_pending_recovery(plugin_dir)


def test_backup_name_sanitizes_legacy_version(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)

    backup = updater._backup_tree(plugin_dir, "../../evil")

    assert backup.parent == plugin_dir / ".backup"
    assert ".." not in backup.name
    assert backup.is_dir()


# 9. Recovery --------------------------------------------------------------------


def test_recover_incomplete_install_restores_newest_backup_and_marker(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    backup_root = plugin_dir / ".backup"
    backup = backup_root / "0.2.8-20260101-000000"
    backup.mkdir(parents=True)
    (backup / "__init__.py").write_text("restored", encoding="utf-8")
    (backup_root / ".incomplete").write_text(backup.name, encoding="utf-8")

    (plugin_dir / "__init__.py").write_text("broken", encoding="utf-8")

    restored = updater.recover_incomplete_install(plugin_dir)

    assert restored == backup
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "restored"
    assert not updater.has_pending_recovery(plugin_dir)
    assert not (plugin_dir / ".backup" / ".incomplete").exists()
    assert not (backup / ".venv").exists()
    assert not (backup / "_venv").exists()


def test_completed_marker_is_not_treated_as_interrupted(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "kicad_ai_assistant"
    _write_plugin(plugin_dir)
    marker = plugin_dir / ".backup" / ".incomplete"
    marker.parent.mkdir(parents=True)
    marker.write_text("complete", encoding="utf-8")

    assert not updater.has_pending_recovery(plugin_dir)
    with pytest.raises(updater.UpdateError, match="completed"):
        updater.recover_incomplete_install(plugin_dir)
    assert not marker.exists()


# 10. Unwritable plugin dir ------------------------------------------------------


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
    _mock_uv(monkeypatch)

    plugin_dir.chmod(0o500)
    try:
        with pytest.raises(updater.UpdateError, match="not writable"):
            updater.install_update(zip_path, plugin_dir, "0.2.9")
    finally:
        plugin_dir.chmod(0o700)

    assert not (plugin_dir / ".backup").exists()
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old init"
