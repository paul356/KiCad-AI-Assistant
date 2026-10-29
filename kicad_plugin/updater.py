"""Self-update support for the KiCad AI Assistant plugin.

Pure logic only: this module NEVER imports wx.  The panel owns every dialog
and marshals results back onto the GUI thread with ``wx.CallAfter``.

The update payload is the release zip (``kicad_ai_assistant.zip``) published
by ``make dist-plugin`` together with a ``SHA256SUMS`` asset.  Installation
replaces the plugin tree, refreshes ``kcaa`` inside the plugin's ``.venv``,
and restores a pre-upgrade backup on any failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess  # nosec B404 - fixed argv, shell=False; runs only the plugin venv's uv/pip
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

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

ProgressCallback = Callable[[int, int | None], None]

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:")
_SAFE_VERSION_RE = re.compile(r"^\d+(\.\d+)*([.-][A-Za-z0-9]+)*$")

# urllib only receives GitHub URLs: the API, the release page host, and the
# asset CDN.  _checked_url() rejects any other scheme or host before a
# request is made.
_ALLOWED_UPDATE_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
    }
)


class UpdateError(RuntimeError):
    """A user-facing update failure (network, integrity, permissions)."""


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


def _parse_version(text: str) -> tuple[int, ...]:
    """Parse ``v1.2.3`` / ``1.2.3-rc1`` into ``(1, 2, 3)``.

    Anything after the leading numeric dotted run is ignored; trailing zero
    components are dropped so ``0.2.9`` and ``0.2.9.0`` compare equal.
    Unparseable input yields ``()``, which is older than any real version.
    """
    match = _VERSION_RE.match(str(text).strip())
    if match is None:
        return ()
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


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


def _checked_url(url: str) -> str:
    """Validate scheme and host before a URL reaches urllib."""
    parsed = urlsplit(str(url))
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing non-HTTPS update URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_UPDATE_HOSTS:
        raise UpdateError(f"Refusing update URL with untrusted host: {url!r}")
    return url


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
    version = str(data.get("tag_name") or "").strip().lstrip("vV")
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

    Returns ``None`` on any network or parse failure so a background check
    can stay silent.
    """
    try:
        request = Request(_checked_url(RELEASES_API), headers=_release_headers())
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - URL validated above
            payload = response.read()
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
    """Find ``filename`` in a sha256sum-style file (handles ``dist/...`` rows)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        name = parts[-1].lstrip("*").replace("\\", "/")
        if name == filename or PurePosixPath(name).name == filename:
            return digest
    return None


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
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - URL validated above
            return response.read().decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def _download_file(url: str, dest: Path, progress_cb: ProgressCallback | None) -> None:
    try:
        request = Request(_checked_url(url), headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # nosec B310 - URL validated above
            total = _response_total(response)
            received = 0
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
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
        if actual.lower() != expected:
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
# Install + rollback
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


def _backup_tree(plugin_dir: Path, old_version: str) -> Path:
    backup_root = plugin_dir / ".backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{old_version or 'unknown'}-{stamp}"
    backup_dir = backup_root / base
    counter = 1
    while backup_dir.exists():
        counter += 1
        backup_dir = backup_root / f"{base}-{counter}"
    shutil.copytree(plugin_dir, backup_dir, ignore=_backup_ignore)
    log.info("Backed up plugin %s to %s", plugin_dir, backup_dir)
    return backup_dir


def _clear_plugin_tree(plugin_dir: Path) -> None:
    for entry in list(plugin_dir.iterdir()):
        if entry.name in _UNTOUCHED_DIRS:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _restore_backup(backup_dir: Path, plugin_dir: Path) -> None:
    _clear_plugin_tree(plugin_dir)
    shutil.copytree(backup_dir, plugin_dir, dirs_exist_ok=True)
    log.warning("Restored plugin from backup %s", backup_dir)


def _prune_backups(backup_root: Path, keep: int = 1) -> None:
    try:
        backups = sorted(
            (path for path in backup_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in backups[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _safe_member(name: str) -> PurePosixPath | None:
    """Normalise an archive member, rejecting absolute/``..`` paths."""
    raw = name.replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        return None  # directory entry
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw == ".":
        return None
    if _WINDOWS_ABS_RE.match(raw) or raw.startswith("/"):
        raise UpdateError(f"Unsafe absolute path in update archive: {name!r}")
    rel = PurePosixPath(raw)
    if ".." in rel.parts:
        raise UpdateError(f"Unsafe path in update archive: {name!r}")
    if rel.parts[0] == "__MACOSX" or rel.name == ".DS_Store":
        return None
    return rel


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

        # Release zips may be rooted at ``kicad_ai_assistant/`` or flat.
        rooted = all(len(rel.parts) > 1 and rel.parts[0] == ZIP_ROOT for rel, _ in members)
        for rel, info in members:
            parts = rel.parts[1:] if rooted else rel.parts
            if not parts or parts[0] in (".venv", ".backup"):
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)


def _venv_python(plugin_dir: Path) -> Path | None:
    candidates = (
        plugin_dir / ".venv" / "Scripts" / "python.exe",  # Windows
        plugin_dir / ".venv" / "bin" / "python",  # POSIX
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _run_venv_install(plugin_dir: Path, new_version: str) -> None:
    _validate_version(new_version)
    python = _venv_python(plugin_dir)
    if python is None:
        raise UpdateError(
            f"Plugin virtual environment not found under {plugin_dir / '.venv'}. "
            "Reinstall the plugin zip or run setup_plugin to recreate it."
        )

    uv = shutil.which("uv")
    commands: list[list[str]] = []
    if uv:
        commands.append([uv, "pip", "install", "--python", str(python), f"kcaa=={new_version}"])
    commands.append([str(python), "-m", "pip", "install", "--upgrade", f"kcaa=={new_version}"])

    last_error = ""
    for command in commands:
        try:
            completed = subprocess.run(  # nosec B603 - argv list, shell=False, version validated
                command,
                capture_output=True,
                text=True,
                timeout=_VENV_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            last_error = f"{command[0]} not found"
            continue
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
            continue
        if completed.returncode == 0:
            return
        output = (completed.stderr or completed.stdout or "").strip()
        last_error = output[-800:] or f"exit code {completed.returncode}"
        log.warning("kcaa upgrade command failed (%s): %s", command[0], last_error)
    raise UpdateError(f"Could not update the kcaa package: {last_error}")


def install_update(
    zip_path: str | os.PathLike[str],
    plugin_dir: str | os.PathLike[str],
    new_version: str,
    progress_cb: ProgressCallback | None = None,
) -> None:
    """Replace the plugin tree with a verified release zip.

    Refuses git checkouts and unwritable directories.  A backup is taken
    first; any failure restores it and raises :class:`UpdateError`.
    """
    plugin_dir = Path(plugin_dir)
    zip_path = Path(zip_path)
    _validate_version(new_version)

    if (plugin_dir / ".git").exists():
        raise UpdateError(
            "This plugin directory is a git checkout; update it with 'git pull' "
            "instead of the in-panel updater."
        )
    _ensure_writable(plugin_dir)

    old_version = current_version(plugin_dir)
    try:
        backup_dir = _backup_tree(plugin_dir, old_version)
    except OSError as exc:
        raise UpdateError(f"Could not back up the current plugin: {exc}") from exc
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix="kcaa-install-"))
        _extract_zip(zip_path, staging)
        _notify(progress_cb, 1, 3)
        _run_venv_install(plugin_dir, new_version)
        _notify(progress_cb, 2, 3)
        _clear_plugin_tree(plugin_dir)
        shutil.copytree(
            staging,
            plugin_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*_UNTOUCHED_DIRS),
        )
        _notify(progress_cb, 3, 3)
    except Exception as exc:
        restore_error: Exception | None = None
        try:
            _restore_backup(backup_dir, plugin_dir)
        except Exception as restore_exc:  # noqa: BLE001 - report both failures
            restore_error = restore_exc
        if isinstance(exc, UpdateError) and restore_error is None:
            raise
        if restore_error is not None:
            raise UpdateError(
                f"Update failed: {exc}. The backup could not be restored "
                f"({restore_error}); it is kept at {backup_dir}."
            ) from exc
        raise UpdateError(f"Update failed: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    _prune_backups(backup_dir.parent, keep=1)
    log.info("Plugin updated to %s (%s)", new_version, plugin_dir)
