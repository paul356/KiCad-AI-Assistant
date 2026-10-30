"""Self-update support for the KiCad AI Assistant plugin.

Pure logic only: this module NEVER imports wx.  The panel owns every dialog
and marshals results back onto the GUI thread with ``wx.CallAfter``.

The update payload is the release zip (``kicad_ai_assistant.zip``) published
by ``make dist-plugin`` together with a ``SHA256SUMS`` asset.  Installation
replaces the plugin tree (backed up beforehand and restored on failure) and
then refreshes ``kcaa`` inside the plugin's ``.venv`` with ``uv``, which
stages and renames its install atomically.  A pre-flight dry-run catches
dependency/index errors before the plugin tree is touched.  An
``.incomplete`` marker keeps a crashed install recoverable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import fnmatch
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess  # nosec B404 - fixed argv, shell=False; runs only the plugin venv's uv/pip
import tempfile
from threading import Event
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

try:  # KiCad's bundled Python may not ship packaging.
    from packaging.version import InvalidVersion as _InvalidVersion
    from packaging.version import Version as _PackagingVersion
except ImportError:  # pragma: no cover - exercised only without packaging
    _InvalidVersion = None
    _PackagingVersion = None

log = logging.getLogger(__name__)

_REPO = "paul356/KiCad-AI-Assistant"
RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{_REPO}/releases/latest"
ZIP_ASSET = "kicad_ai_assistant.zip"
CHECKSUM_ASSET = "SHA256SUMS"
ZIP_ROOT = "kicad_ai_assistant"

USER_AGENT = "KiCad-AI-Assistant-Updater"
CHECK_TIMEOUT = 3.0
DOWNLOAD_TIMEOUT = 60.0
_VENV_TIMEOUT = 300.0
_CHUNK_SIZE = 64 * 1024

# Directories/patterns never copied into a backup, never deleted on restore,
# and never taken from the update archive.
_UNTOUCHED_DIRS = frozenset({".venv", ".backup", "__pycache__"})
_BACKUP_IGNORES = frozenset({"*.log"})
# Root artifacts written by setup_plugin.* that the release zip never ships.
# They must survive both a successful update and a rollback.
_PRESERVED_ROOT_GLOBS = ("freerouting*.jar",)
# Recovery marker lives inside .backup; its content is the backup dir name.
_INCOMPLETE_MARKER = ".incomplete"
# Written before removing the marker so a successful install can never be
# rolled back merely because the marker file could not be deleted.
_COMPLETE_MARKER = "complete"

# Response/archive size guards.
_MAX_TEXT_BYTES = 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 5000
_MAX_MEMBER_BYTES = 200 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

ProgressCallback = Callable[[int, int | None], None]

_SAFE_VERSION_RE = re.compile(r"^\d+(\.\d+)*([.-][A-Za-z0-9]+)*$")
_SUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(?:dist/)?([^/\s]+)$")

# urllib only receives GitHub URLs: the API, the release page host, and the
# asset CDNs.  _checked_url() rejects any other scheme or host before a
# request is made, and _CheckedRedirectHandler applies the same check to
# every redirect hop.
_ALLOWED_UPDATE_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

_VersionKey = tuple[int, tuple[int, ...], int, int, int, int, int, int]


class UpdateError(RuntimeError):
    """A user-facing update failure.

    ``restored`` is ``True`` when the plugin tree was restored, ``False``
    when the plugin tree is inconsistent, and ``None`` when no tree swap
    happened or when the venv update failed after the tree was restored (the
    venv state is deliberately not claimed).
    """

    def __init__(
        self,
        message: str,
        *,
        restored: bool | None = None,
        backup_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.restored = restored
        self.backup_dir = str(backup_dir) if backup_dir is not None else None


@dataclass(frozen=True)
class UpdateInfo:
    """A newer release found on GitHub."""

    version: str
    zip_url: str
    checksum_url: str
    page_url: str


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def _pep440_key(version) -> _VersionKey:
    """Build a total-order key from a packaging ``Version``."""
    release = list(version.release)
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre = version.pre
    post = version.post
    dev = version.dev
    if pre is not None:
        phase = -2
        label_rank = {"a": 0, "b": 1, "rc": 2}.get(pre[0], 3)
        pre_num = pre[1]
    elif post is not None:
        phase = 1
        label_rank = 0
        pre_num = 0
    elif dev is not None:
        phase = -3
        label_rank = 0
        pre_num = 0
    else:
        phase = 0
        label_rank = 0
        pre_num = 0
    return (
        version.epoch,
        tuple(release),
        phase,
        label_rank,
        pre_num,
        post if post is not None else 0,
        # A dev release sorts below the same version without one.
        0 if dev is None else -1,
        dev if dev is not None else 0,
    )


def _fallback_version_key(raw: str) -> _VersionKey:
    """Order versions packaging cannot parse (e.g. ``0.2.4-bugfix3``)."""
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", raw)
    if match is None:
        return (0, (), 0, 0, 0, 0, 0, 0)
    release = [int(part) for part in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    suffix = match.group(2).strip(".-_")
    if not suffix:
        return (0, tuple(release), 0, 0, 0, 0, 0, 0)
    token = re.match(r"^([A-Za-z]+)?(\d+)?$", suffix)
    if token is None:
        return (0, tuple(release), 1, 0, 0, 0, 0, 0)
    label = (token.group(1) or "").lower()
    number = int(token.group(2)) if token.group(2) else 0
    pre_labels = {
        "a": 0,
        "alpha": 0,
        "b": 1,
        "beta": 1,
        "c": 2,
        "pre": 2,
        "preview": 2,
        "rc": 2,
    }
    if label in pre_labels:
        return (0, tuple(release), -2, pre_labels[label], number, 0, 0, 0)
    if label == "dev":
        return (0, tuple(release), -3, 0, 0, 0, -1, number)
    # post/rev/r and repo tags such as ``-bugfix3`` all order after the
    # plain release; their number breaks the tie.
    return (0, tuple(release), 1, 0, 0, number, 0, 0)


def _parse_version(text: str) -> _VersionKey:
    """Return a comparable key, normalising ``v`` prefixes and ``+local``."""
    raw = str(text).strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    raw = raw.split("+", 1)[0]
    if _PackagingVersion is not None:
        try:
            return _pep440_key(_PackagingVersion(raw))
        except _InvalidVersion:
            pass
    return _fallback_version_key(raw)


def is_newer(candidate: str, current: str) -> bool:
    """Return True when ``candidate`` is a strictly newer version."""
    return _parse_version(candidate) > _parse_version(current)


def current_version(plugin_dir: str | os.PathLike[str]) -> str:
    """Read the plugin's version from ``VERSION``, falling back to kcaa."""
    try:
        text = (Path(plugin_dir) / "VERSION").read_text(encoding="utf-8").strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    try:
        from kcaa import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - version lookup must never raise
        return "0.0.0"


def _validate_version(version: str) -> str:
    """Reject version strings that could become extra pip arguments."""
    if not _SAFE_VERSION_RE.fullmatch(str(version)):
        raise UpdateError("Unsafe version string")
    return version


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def _checked_url(url: str) -> str:
    """Validate scheme and host before a URL reaches urllib."""
    parsed = urlsplit(str(url))
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing non-HTTPS update URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_UPDATE_HOSTS:
        raise UpdateError(f"Refusing update URL with untrusted host: {url!r}")
    return url


class _CheckedRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that leave the GitHub allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_CheckedRedirectHandler())


def _open_url(request: Request, timeout: float):
    """Open a validated request through the redirect-checking opener."""
    return _OPENER.open(request, timeout=timeout)


# ---------------------------------------------------------------------------
# Release check
# ---------------------------------------------------------------------------


def _release_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }


def _update_from_release(data: object) -> UpdateInfo | None:
    """Turn the GitHub ``releases/latest`` payload into an UpdateInfo."""
    if not isinstance(data, dict):
        return None
    # Normalise once here: strip the tag prefix and any PEP 440 local
    # segment (``v0.2.9+build`` -> ``0.2.9``).  install_update() validates
    # strictly and rejects ``+``, so the release path must never offer a
    # version it cannot install.  Non-local suffixes such as ``-rc1`` stay.
    tag = str(data.get("tag_name") or "").strip().lstrip("vV")
    version = tag.split("+", 1)[0].strip()
    if not version:
        return None
    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    urls: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name and url:
            urls[name] = url
    zip_url = urls.get(ZIP_ASSET)
    checksum_url = urls.get(CHECKSUM_ASSET)
    if not zip_url or not checksum_url:
        # Without both the zip and its checksum there is no safe upgrade path.
        return None
    page_url = str(data.get("html_url") or RELEASE_PAGE)
    return UpdateInfo(
        version=version,
        zip_url=zip_url,
        checksum_url=checksum_url,
        page_url=page_url,
    )


def check_latest(timeout: float = CHECK_TIMEOUT) -> UpdateInfo | None:
    """Ask GitHub for the latest release; NEVER raises.

    Returns ``None`` on any network, redirect, or parse failure so a
    background check can stay silent.
    """
    try:
        request = Request(_checked_url(RELEASES_API), headers=_release_headers())
        with _open_url(request, timeout) as response:
            payload = response.read(_MAX_TEXT_BYTES + 1)
        if len(payload) > _MAX_TEXT_BYTES:
            raise UpdateError("Release metadata is unexpectedly large")
        return _update_from_release(json.loads(payload.decode("utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
        log.debug("Update check failed", exc_info=True)
        return None
    except Exception:  # noqa: BLE001 - never surface a background-check failure
        log.debug("Update check failed unexpectedly", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Download + integrity
# ---------------------------------------------------------------------------


def _lookup_checksum(text: str, filename: str) -> str | None:
    """Find ``filename`` in a strict sha256sum file.

    Only ``<hash>  <name>`` and ``<hash>  dist/<name>`` rows are accepted.
    Conflicting rows for the same filename are rejected outright so a decoy
    line cannot override the real digest.
    """
    found: str | None = None
    for line in text.splitlines():
        match = _SUM_LINE_RE.match(line.strip())
        if match is None:
            continue
        digest, name = match.group(1).lower(), match.group(2)
        if name != filename:
            continue
        if found is not None and not hmac.compare_digest(found, digest):
            raise UpdateError(f"Conflicting checksums for {filename} in {CHECKSUM_ASSET}")
        found = digest
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _notify(callback: ProgressCallback | None, done: int, total: int | None) -> None:
    if callback is None:
        return
    try:
        callback(done, total)
    except Exception:  # noqa: BLE001 - UI progress must not break the update
        log.debug("Update progress callback failed", exc_info=True)


def _response_total(response) -> int | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("Content-Length")
        total = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return total if total and total > 0 else None


def _fetch_text(url: str, timeout: float) -> str:
    try:
        request = Request(_checked_url(url), headers={"User-Agent": USER_AGENT})
        with _open_url(request, timeout) as response:
            payload = response.read(_MAX_TEXT_BYTES + 1)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc
    if len(payload) > _MAX_TEXT_BYTES:
        raise UpdateError(f"{CHECKSUM_ASSET} response is unexpectedly large")
    return payload.decode("utf-8")


def _download_file(url: str, dest: Path, progress_cb: ProgressCallback | None) -> None:
    try:
        request = Request(_checked_url(url), headers={"User-Agent": USER_AGENT})
        with _open_url(request, DOWNLOAD_TIMEOUT) as response:
            total = _response_total(response)
            received = 0
            deadline = monotonic() + DOWNLOAD_TIMEOUT
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if received > _MAX_ARCHIVE_BYTES:
                        raise UpdateError("Downloaded archive is too large.")
                    if monotonic() > deadline:
                        raise UpdateError("Download timed out.")
                    _notify(progress_cb, received, total)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def download_and_verify(
    update: UpdateInfo,
    dest_dir: str | os.PathLike[str],
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Download the release zip and verify it against ``SHA256SUMS``.

    Returns the path of the verified zip.  On a checksum mismatch no zip
    (partial or otherwise) is left behind.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    sums_text = _fetch_text(update.checksum_url, DOWNLOAD_TIMEOUT)
    expected = _lookup_checksum(sums_text, ZIP_ASSET)
    if expected is None:
        raise UpdateError(f"{CHECKSUM_ASSET} has no entry for {ZIP_ASSET}")

    final_path = dest / ZIP_ASSET
    partial_path = dest / f"{ZIP_ASSET}.part"
    try:
        _download_file(update.zip_url, partial_path, progress_cb)
        actual = _sha256_file(partial_path)
        if not hmac.compare_digest(actual.lower(), expected.lower()):
            raise UpdateError(
                f"checksum mismatch for {ZIP_ASSET}: expected {expected}, got {actual}"
            )
        os.replace(partial_path, final_path)
    finally:
        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError:
                log.debug("Could not remove partial download %s", partial_path)
    return final_path


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


def _ensure_writable(plugin_dir: Path) -> None:
    if not plugin_dir.is_dir():
        raise UpdateError(f"Plugin directory not found: {plugin_dir}")
    if not os.access(plugin_dir, os.W_OK):
        raise UpdateError(
            f"Plugin directory is not writable: {plugin_dir}. "
            "Close KiCad, fix the folder permissions, and try again."
        )
    # os.access can lie on Windows/network shares; probe by creating a file.
    try:
        fd, probe = tempfile.mkstemp(prefix=".kcaa-write-test-", dir=plugin_dir)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise UpdateError(
            f"Plugin directory is not writable: {plugin_dir} ({exc}). Close KiCad and try again."
        ) from exc


def _backup_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in _UNTOUCHED_DIRS:
            ignored.add(name)
        elif name.endswith(".log"):
            ignored.add(name)
    return ignored


def _safe_dir_component(version: str) -> str:
    """Make a VERSION-derived string safe to use as one path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(version)).strip("._")
    return cleaned[:64] or "unknown"


def _backup_tree(plugin_dir: Path, old_version: str) -> Path:
    backup_root = plugin_dir / ".backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{_safe_dir_component(old_version)}-{stamp}"
    backup_dir = backup_root / base
    counter = 1
    while backup_dir.exists():
        counter += 1
        backup_dir = backup_root / f"{base}-{counter}"
    shutil.copytree(plugin_dir, backup_dir, ignore=_backup_ignore)
    log.info("Backed up plugin %s to %s", plugin_dir, backup_dir)
    return backup_dir


def _backup_dirs(backup_root: Path) -> list[Path]:
    try:
        return [path for path in backup_root.iterdir() if path.is_dir()]
    except OSError:
        return []


def _newest_backup(backup_root: Path) -> Path | None:
    backups = _backup_dirs(backup_root)
    if not backups:
        return None
    return max(backups, key=lambda path: path.stat().st_mtime)


def _is_preserved_root_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _PRESERVED_ROOT_GLOBS)


def _clear_plugin_tree(plugin_dir: Path, preserve_files: set[str] | None = None) -> None:
    preserve = set(preserve_files or ())
    for entry in list(plugin_dir.iterdir()):
        if entry.name in _UNTOUCHED_DIRS:
            continue
        if entry.is_file() and (_is_preserved_root_file(entry.name) or entry.name in preserve):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _restore_tree_from_backup(backup_dir: Path, plugin_dir: Path) -> None:
    _clear_plugin_tree(plugin_dir)
    shutil.copytree(
        backup_dir,
        plugin_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "_venv"),
    )
    log.warning("Restored plugin files from backup %s", backup_dir)


def _prune_backups(backup_root: Path, keep: int = 1) -> None:
    backups = sorted(
        _backup_dirs(backup_root),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _marker_path(plugin_dir: Path) -> Path:
    return plugin_dir / ".backup" / _INCOMPLETE_MARKER


def has_pending_recovery(plugin_dir: str | os.PathLike[str]) -> bool:
    """True when a previous install stopped before it could finish/roll back."""
    marker = _marker_path(Path(plugin_dir))
    if not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() != _COMPLETE_MARKER
    except OSError:
        # Unreadable marker: stay conservative and offer recovery.
        return True


def recover_incomplete_install(plugin_dir: str | os.PathLike[str]) -> Path:
    """Restore the newest backup of an interrupted install and clear the marker."""
    plugin_dir = Path(plugin_dir)
    marker = _marker_path(plugin_dir)
    if not marker.is_file():
        raise UpdateError("No interrupted update to recover.")
    backup_root = plugin_dir / ".backup"
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UpdateError(f"Could not read the recovery marker: {exc}") from exc
    if name == _COMPLETE_MARKER:
        try:
            marker.unlink()
        except OSError as exc:
            raise UpdateError(
                f"The previous update completed, but its marker could not be cleared: {exc}"
            ) from exc
        raise UpdateError("The previous update completed; nothing to restore.")

    backup_dir: Path | None = None
    if name and PurePosixPath(name).name == name and name not in (".", ".."):
        candidate = backup_root / name
        if candidate.is_dir():
            backup_dir = candidate
    if backup_dir is None:
        backup_dir = _newest_backup(backup_root)
    if backup_dir is None:
        raise UpdateError(f"Cannot recover: no backup found under {backup_root}.")

    _restore_tree_from_backup(backup_dir, plugin_dir)
    try:
        marker.unlink()
    except OSError as exc:
        raise UpdateError(
            f"Restored {backup_dir} but could not clear the recovery marker: {exc}"
        ) from exc
    log.warning("Recovered interrupted update from %s", backup_dir)
    return backup_dir


# ---------------------------------------------------------------------------
# venv discovery and uv update
# ---------------------------------------------------------------------------


def _venv_python(plugin_dir: Path) -> Path | None:
    candidates = (
        plugin_dir / ".venv" / "Scripts" / "python.exe",  # Windows
        plugin_dir / ".venv" / "bin" / "python",  # POSIX
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Archive validation / extraction
# ---------------------------------------------------------------------------


def _safe_member(name: str) -> PurePosixPath | None:
    """Normalise an archive member, rejecting unsafe Windows/POSIX paths."""
    raw = name.strip()
    if not raw or raw.endswith("/"):
        return None  # directory entry
    if "\\" in raw:
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    if ":" in raw:
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw == ".":
        return None
    if any(part == "" for part in raw.split("/")):
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    if raw.startswith("/"):
        raise UpdateError(f"Unsafe absolute path in update archive: {name!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    if any(part == "" or re.fullmatch(r"[A-Za-z]:", part) for part in rel.parts):
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    if rel.parts[0] == "__MACOSX" or rel.name == ".DS_Store":
        return None
    return rel


def _member_target(dest: Path, parts: tuple[str, ...]) -> Path:
    """Resolve a member target and prove it stays inside ``dest``."""
    root = dest.resolve()
    target = dest.joinpath(*parts).resolve()
    if target != root and not target.is_relative_to(root):
        raise UpdateError(f"Unsafe path in update archive: {'/'.join(parts)!r}")
    return target


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract the release zip into ``dest``, stripping the zip root."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"Downloaded archive is not a valid zip: {exc}") from exc

    with archive:
        members: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
        for info in archive.infolist():
            rel = _safe_member(info.filename)
            if rel is not None:
                members.append((rel, info))

        if not members:
            raise UpdateError("Downloaded archive contains no files.")
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise UpdateError("Downloaded archive contains too many entries.")

        # Release zips may be rooted at ``kicad_ai_assistant/`` or flat.
        rooted = all(len(rel.parts) > 1 and rel.parts[0] == ZIP_ROOT for rel, _ in members)
        total_uncompressed = 0
        total_written = 0
        for rel, info in members:
            if info.file_size > _MAX_MEMBER_BYTES:
                raise UpdateError("Downloaded archive member is too large.")
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_ARCHIVE_BYTES:
                raise UpdateError("Downloaded archive is too large.")
            parts = rel.parts[1:] if rooted else rel.parts
            if not parts or parts[0] in (".venv", ".backup"):
                continue
            target = _member_target(dest, parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info) as source, open(target, "wb") as handle:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_MEMBER_BYTES:
                        raise UpdateError("Downloaded archive member is too large.")
                    handle.write(chunk)
            total_written += written
            if total_written > _MAX_ARCHIVE_BYTES:
                raise UpdateError("Downloaded archive is too large.")


# ---------------------------------------------------------------------------
# venv update + install orchestration
# ---------------------------------------------------------------------------


_UNSUPPORTED_FLAG_HINTS = (
    "unexpected argument",
    "unexpected option",
    "unrecognised",
    "unrecognized",
    "unknown option",
    "no such option",
    "invalid option",
)


def _require_uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise UpdateError(
            "uv was not found on PATH. Rerun setup_plugin.sh (or install uv) "
            "and try the update again."
        )
    return uv


def _command_error(completed: subprocess.CompletedProcess[str]) -> str:
    output = (completed.stderr or completed.stdout or "").strip()
    return output[-800:] or f"exit code {completed.returncode}"


def _looks_like_unsupported_flag(output: str) -> bool:
    lowered = output.lower()
    return any(hint in lowered for hint in _UNSUPPORTED_FLAG_HINTS)


def _dry_run_install(uv: str, python: Path, new_version: str) -> None:
    """Run ``uv pip install --dry-run`` without mutating the venv."""
    _validate_version(new_version)
    package = f"kcaa=={new_version}"
    attempts = (
        [uv, "pip", "install", "--dry-run", "--python", str(python), package],
        [uv, "pip", "install", "--python", str(python), "--dry-run", package],
    )
    for index, command in enumerate(attempts):
        try:
            completed = subprocess.run(  # nosec B603 - fixed argv, version validated
                command,
                capture_output=True,
                text=True,
                timeout=_VENV_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"Update pre-flight failed: {exc}") from exc
        if completed.returncode == 0:
            return
        output = _command_error(completed)
        if _looks_like_unsupported_flag(output):
            if index == 0:
                log.info("uv rejected the first --dry-run placement; retrying")
                continue
            log.warning("uv does not support --dry-run; skipping pre-flight: %s", output)
            return
        raise UpdateError(f"Update pre-flight failed: {output}")


def _run_venv_install(plugin_dir: Path, new_version: str) -> None:
    """Install the validated ``kcaa`` version with uv (atomic rename)."""
    _validate_version(new_version)
    python = _venv_python(plugin_dir)
    if python is None:
        raise UpdateError(
            f"Plugin virtual environment not found under {plugin_dir / '.venv'}. "
            "Rerun setup_plugin.sh to recreate it."
        )
    uv = _require_uv()
    command = [
        uv,
        "pip",
        "install",
        "--python",
        str(python),
        "--upgrade",
        f"kcaa=={new_version}",
    ]
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, version validated
            command,
            capture_output=True,
            text=True,
            timeout=_VENV_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"Could not update the kcaa package: {exc}") from exc
    if completed.returncode != 0:
        raise UpdateError(f"Could not update the kcaa package: {_command_error(completed)}")


def _verify_installed_version(plugin_dir: Path, expected: str) -> None:
    try:
        installed = (plugin_dir / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise UpdateError("Updated plugin is missing a readable VERSION file.") from exc
    if installed != expected:
        raise UpdateError(f"Updated plugin VERSION {installed!r} does not match {expected!r}.")


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateError("Update cancelled.")


def install_update(
    zip_path: str | os.PathLike[str],
    plugin_dir: str | os.PathLike[str],
    new_version: str,
    progress_cb: ProgressCallback | None = None,
    *,
    cancel_event: Event | None = None,
    expected_sha256: str | None = None,
) -> None:
    """Install a release zip over the plugin directory.

    Refuses git checkouts, unwritable directories, and leftovers from an
    interrupted install.  Before any filesystem change it resolves the
    plugin venv, requires uv, and runs a dry-run install.  It then backs up
    the plugin tree, replaces it, verifies VERSION, and updates ``kcaa`` with
    uv.  The tree is restored on failure; if the venv step fails after the
    swap, the error says the plugin files were restored but the package
    update did not complete.  Pass ``expected_sha256`` to re-verify a zip
    obtained outside :func:`download_and_verify`.
    """
    plugin_dir = Path(plugin_dir)
    zip_path = Path(zip_path)
    _validate_version(new_version)

    if not plugin_dir.is_dir():
        raise UpdateError(f"Plugin directory not found: {plugin_dir}")
    if (plugin_dir / ".git").exists():
        raise UpdateError(
            "This plugin directory is a git checkout; update it with 'git pull' "
            "instead of the in-panel updater."
        )
    if has_pending_recovery(plugin_dir):
        raise UpdateError(
            "A previous update did not finish. Restart KiCad to recover the "
            "previous version before updating again."
        )

    if expected_sha256 is not None:
        try:
            actual = _sha256_file(zip_path)
        except OSError as exc:
            raise UpdateError(f"Could not read the update archive: {exc}") from exc
        if not hmac.compare_digest(actual.lower(), str(expected_sha256).lower()):
            raise UpdateError("checksum mismatch for the update archive")

    # Pre-flight: fail before backup/clear/copy when the venv or uv is
    # missing, or when dependency/index resolution cannot succeed.
    python = _venv_python(plugin_dir)
    if python is None:
        raise UpdateError(
            f"Plugin virtual environment not found under {plugin_dir / '.venv'}. "
            "Rerun setup_plugin.sh to recreate it."
        )
    uv = _require_uv()
    _dry_run_install(uv, python, new_version)

    _ensure_writable(plugin_dir)

    old_version = current_version(plugin_dir)
    try:
        backup_dir = _backup_tree(plugin_dir, old_version)
    except OSError as exc:
        raise UpdateError(f"Could not back up the current plugin: {exc}") from exc

    marker = _marker_path(plugin_dir)
    try:
        marker.write_text(backup_dir.name, encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"Could not record the interrupted-update marker: {exc}") from exc

    staging: Path | None = None
    venv_started = False
    try:
        staging = Path(tempfile.mkdtemp(prefix="kcaa-install-"))
        _extract_zip(zip_path, staging)
        _check_cancel(cancel_event)

        shipped_root_files = {path.name for path in staging.iterdir() if path.is_file()}
        _clear_plugin_tree(plugin_dir, preserve_files=shipped_root_files)
        shutil.copytree(
            staging,
            plugin_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*_UNTOUCHED_DIRS),
        )
        _verify_installed_version(plugin_dir, new_version)
        _notify(progress_cb, 2, 3)

        venv_started = True
        _run_venv_install(plugin_dir, new_version)
        _notify(progress_cb, 3, 3)
    except Exception as exc:
        restore_errors: list[str] = []
        try:
            _restore_tree_from_backup(backup_dir, plugin_dir)
        except Exception as tree_exc:  # noqa: BLE001 - report restore failures
            restore_errors.append(str(tree_exc))

        if restore_errors:
            detail = "; ".join(restore_errors)
            raise UpdateError(
                f"Update failed: {exc}. The plugin files could not be fully "
                f"restored; the backup is kept at {backup_dir}. Restore errors: {detail}",
                restored=False,
                backup_dir=backup_dir,
            ) from exc

        try:
            marker.unlink()
        except OSError:
            log.warning("Could not clear the interrupted-update marker %s", marker)

        if venv_started:
            raise UpdateError(
                "The plugin files were restored from the backup, but updating the "
                f"kcaa package failed: {exc}. Retry the update or rerun setup_plugin.sh.",
                restored=None,
                backup_dir=backup_dir,
            ) from exc
        if isinstance(exc, UpdateError):
            exc.restored = True
            exc.backup_dir = str(backup_dir)
            raise
        raise UpdateError(
            f"Update failed: {exc}",
            restored=True,
            backup_dir=backup_dir,
        ) from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    try:
        # Mark completion first: if unlinking fails, has_pending_recovery()
        # must not report a successful install as an interrupted one.
        marker.write_text(_COMPLETE_MARKER, encoding="utf-8")
        marker.unlink()
    except OSError:
        log.warning("Could not clear the interrupted-update marker %s", marker)
    _prune_backups(backup_dir.parent, keep=1)
    log.info("Plugin updated to %s (%s)", new_version, plugin_dir)
