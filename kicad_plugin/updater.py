"""Self-update support for the KiCad AI Assistant plugin.

Pure logic only: this module NEVER imports wx.  The panel owns every dialog
and marshals results back onto the GUI thread with ``wx.CallAfter``.

Update detection is tag inequality: GitHub's ``/releases/latest`` returns the
newest non-draft, non-prerelease release by publish time, so any different
release tag is offered.  A dev tree ahead of the latest release will therefore
be offered a "downgrade".  ``0.2.9`` and ``0.2.9.0`` are different tags; an
update loop is possible only when a hand-uploaded ``VERSION`` diverges from the
release tag, because workflow-built releases are guarded by the CI
tag-vs-VERSION check.

The update payload is the release zip (``kicad_ai_assistant.zip``) published
by ``make dist-plugin``.  Releases may also publish a ``SHA256SUMS`` asset;
when it is absent the download still installs, with only the integrity
comparison skipped.  Installation replaces the plugin tree (backed up
beforehand and restored on failure) and, when the venv does not already
contain the target ``kcaa`` version, refreshes
``kcaa`` inside the plugin's ``.venv`` with ``uv``, which stages and renames
its install atomically.  A matching ``kcaa`` version is left untouched, so a
plugin-only upgrade works offline.  An editable ``kcaa`` install blocks the
upgrade entirely: swapping the plugin tree would leave release plugin code
next to a source ``kcaa``, the mixed state editable detection exists to
avoid, so ``install_update`` refuses before touching anything and the panel
does not offer the upgrade.  Editable detection follows PEP 610 only:
``direct_url.json`` with ``dir_info.editable`` set to true.  Legacy
``setup.py develop`` installs (``.egg-link``, no ``direct_url.json``) and
missing metadata are reported as non-editable.  The venv state is read
straight from ``site-packages``, never through a process spawn, so
verification cannot be defeated by the host process failing to fork.
Unknown therefore defaults to installing, which replaces a legacy editable
install with a wheel; such users must manage ``kcaa`` themselves.  When
several ``kcaa`` metadata directories match, the newest numeric version
wins and a warning names every distinct version found: a stale *newer*
duplicate is exactly the case where the reader no longer agrees with what
the venv interpreter would import, so it is logged rather than silent.
Otherwise a pre-flight dry-run catches dependency/index errors before the
plugin tree is touched, and the installed version is re-read from the venv
afterwards.  An ``.incomplete`` marker keeps a crashed install recoverable.
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
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

log = logging.getLogger(__name__)

_REPO = "paul356/KiCad-AI-Assistant"
RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{_REPO}/releases/latest"
ZIP_ASSET = "kicad_ai_assistant.zip"
CHECKSUM_ASSET = "SHA256SUMS"
ZIP_ROOT = "kicad_ai_assistant"

USER_AGENT = "KiCad-AI-Assistant-Updater"
# The check runs on a daemon thread, so a generous timeout never blocks the UI.
CHECK_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0
# One retry on a transient transport failure; metadata errors are not retried.
_CHECK_ATTEMPTS = 2

# check_latest() reason codes; callers pick a user-facing message from these.
CHECK_REASON_OK = "ok"  # a usable release payload was found
CHECK_REASON_NETWORK = "network"  # OSError/URLError, redirect/host rejection
CHECK_REASON_TIMEOUT = "timeout"  # TimeoutError after the final attempt
CHECK_REASON_METADATA = "metadata"  # payload unusable: bad JSON, no zip, bad tag, oversized
_VENV_TIMEOUT = 300.0
_CHUNK_SIZE = 64 * 1024

# Installed ``kcaa`` metadata is read straight off the plugin venv's
# ``site-packages``, the same files ``importlib.metadata`` reads.  Spawning
# the venv interpreter instead made verification depend on fork/exec from a
# large multi-threaded host process, so a single spawn hiccup reported a
# correct install as "unknown version".
_SITE_PACKAGE_GLOBS = (
    "Lib/site-packages",
    "lib/python*/site-packages",
    "lib64/python*/site-packages",
)
_DIST_SUFFIXES = (".dist-info", ".egg-info")

# Directories/patterns never copied into a backup, never deleted on restore,
# and never taken from the update archive.
_UNTOUCHED_DIRS = frozenset({".venv", ".backup", "__pycache__"})
_BACKUP_IGNORES = frozenset({"*.log"})
# Root artifacts written by setup_plugin.* or runtime logs that the release
# zip never ships.  They must survive both a successful update and a rollback.
_PRESERVED_ROOT_GLOBS = ("freerouting*.jar", "*.log")
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

# Allow-list for version strings that reach uv's argv and error messages.
# The first branch keeps every legacy tag shape accepted before (``1-rc1``);
# the second accepts the PEP 440 canonical spellings ``_normalize_version()``
# can emit (attached pre-release with optional ``.postN``/``.devN``, e.g.
# ``1rc1``, ``1rc1.post1.dev2``).  ``+local`` and epochs stay rejected.
_SAFE_VERSION_RE = re.compile(
    r"^(?:"
    r"[0-9]+(?:\.[0-9]+)*(?:[.-][A-Za-z0-9]+)*"
    r"|"
    r"[0-9]+(?:\.[0-9]+)*(?:a|b|rc)[0-9]+"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r")$"
)
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


class UpdateError(RuntimeError):
    """A user-facing update failure.

    ``restored`` is ``True`` when the plugin tree is intact — restored after
    a failure, or never touched because a pre-flight check refused — and
    ``False`` when the plugin tree is inconsistent.  It is ``None`` when no
    tree swap happened or when the venv update failed after the tree was
    restored (the venv state is deliberately not claimed).
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
    checksum_url: str | None
    page_url: str


@dataclass(frozen=True)
class CheckResult:
    """Outcome of :func:`check_latest`.

    ``reason`` is one of the ``CHECK_REASON_*`` constants; ``info`` is set
    only when ``reason`` is :data:`CHECK_REASON_OK`.
    """

    info: UpdateInfo | None
    reason: str


@dataclass(frozen=True)
class InstalledKcaa:
    """The venv's ``kcaa`` state; unknown fields fall back to safe defaults."""

    version: str | None
    editable: bool = False


@dataclass(frozen=True)
class InstallOutcome:
    """What ``install_update`` did to the venv's ``kcaa`` package."""

    kcaa_updated: bool
    kcaa_skipped_reason: str | None


@dataclass(frozen=True)
class VerifiedDownload:
    """A downloaded release archive and its SHA-256 digest.

    The digest is compared against ``SHA256SUMS`` when the release publishes
    one; releases without that asset come back unverified.
    """

    path: Path
    sha256: str


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


# Public subset of PEP 440 used for canonical comparison:
# ``N(.N)*[{a|b|rc}N][.postN][.devN]``.  The local segment is stripped before
# matching, and epochs (``1!2.0``) are deliberately unsupported because
# ``_validate_version`` rejects ``!`` in release tags.
_PEP440_PUBLIC_RE = re.compile(
    r"""
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:
        [-_.]?
        (?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)
        [-_.]?
        (?P<pre_n>[0-9]+)?
    )?
    (?:
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:
            [-_.]?
            (?P<post_l>post|rev|r)
            [-_.]?
            (?P<post_n2>[0-9]+)?
        )
    )?
    (?:
        [-_.]?
        (?P<dev_l>dev)
        [-_.]?
        (?P<dev_n>[0-9]+)?
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# PEP 440 canonical pre-release spellings: alpha/beta/rc, with ``c``, ``pre``,
# and ``preview`` as historical aliases for ``rc``.
_PRE_RELEASE_SHORT = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


def _canonical_number(digits: str | None) -> str:
    """Drop leading zeros from one PEP 440 numeric component."""
    return (digits or "0").lstrip("0") or "0"


def _normalize_version(text: object) -> str:
    """Return the PEP 440 canonical spelling used for version comparisons.

    Surrounding whitespace, one leading ``v``, and a ``+local`` segment are
    stripped as before.  The remaining public version is then canonicalised
    per PEP 440: leading zeros in every numeric component are dropped
    (``1.02`` -> ``1.2``), pre-release aliases collapse to ``a``/``b``/``rc``
    (``0.2.9-RC1``/``0.2.9_rc1``/``0.2.9.rc1`` -> ``0.2.9rc1``), omitted
    numbers become ``0`` (``1.0-rc`` -> ``1.0rc0``), ``post``/``rev``/``r``
    and ``-N`` become ``.postN``, and ``dev`` becomes ``.devN``.

    Deliberately not covered: epochs (left untouched), local-segment content
    (stripped), and comparison semantics such as ordering or trailing-zero
    equality.  Unparseable input is returned best-effort after the same
    stripping and never raises.
    """
    raw = "" if text is None else str(text).strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    raw = raw.split("+", 1)[0].strip()
    match = _PEP440_PUBLIC_RE.fullmatch(raw)
    if match is None:
        return raw

    canonical = ".".join(_canonical_number(part) for part in match["release"].split("."))
    pre_label = match["pre_l"]
    if pre_label:
        canonical += _PRE_RELEASE_SHORT[pre_label.lower()]
        canonical += _canonical_number(match["pre_n"])
    post_number = match["post_n1"] if match["post_n1"] is not None else match["post_n2"]
    if post_number is not None or match["post_l"] is not None:
        canonical += ".post" + _canonical_number(post_number)
    if match["dev_l"] is not None:
        canonical += ".dev" + _canonical_number(match["dev_n"])
    return canonical


def is_update_available(latest: str, current: str) -> bool:
    """True when the latest release tag differs from the installed version.

    GitHub's ``/releases/latest`` is the newest non-draft, non-prerelease
    release by publish time, so tag inequality is the update rule.  Known
    limitation: a local build newer than the latest published release (for
    example a development build) is offered a "downgrade"; that trade-off
    keeps the check simple and avoids version-ordering edge cases.
    """
    latest_norm = _normalize_version(latest).lower()
    current_norm = _normalize_version(current).lower()
    if not latest_norm or not current_norm:
        return False
    return latest_norm != current_norm


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
    # segment (``v0.2.9+build`` -> ``0.2.9``) and canonicalise the remaining
    # spelling (``0.2.9-RC1`` -> ``0.2.9rc1``).  install_update() validates
    # strictly and rejects ``+``, so the release path must never offer a
    # version it cannot install.  Non-local suffixes such as ``rc1`` stay.
    version = _normalize_version(data.get("tag_name"))
    if not version:
        return None
    try:
        _validate_version(version)
    except UpdateError:
        # A tag install_update() would refuse must never be advertised.
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
    checksum_url = urls.get(CHECKSUM_ASSET) or None
    if not zip_url:
        # A release without the zip has nothing installable to offer.
        return None
    page_url = str(data.get("html_url") or RELEASE_PAGE)
    return UpdateInfo(
        version=version,
        zip_url=zip_url,
        checksum_url=checksum_url,
        page_url=page_url,
    )


def _fetch_release_payload(timeout: float) -> bytes:
    """Fetch the raw ``releases/latest`` body through the checked opener."""
    request = Request(_checked_url(RELEASES_API), headers=_release_headers())
    with _open_url(request, timeout) as response:
        return response.read(_MAX_TEXT_BYTES + 1)


def check_latest(timeout: float = CHECK_TIMEOUT) -> CheckResult:
    """Ask GitHub for the latest release; NEVER raises.

    Returns a :class:`CheckResult` whose ``reason`` explains the outcome.

    ``timeout`` bounds each attempt.  ``TimeoutError``/``URLError`` are retried
    once, so a manual check can take up to ``2 * timeout`` (20 s with the
    default :data:`CHECK_TIMEOUT`) plus metadata parsing.  urllib applies its
    timeout per socket operation, not as a hard monotonic deadline, so the
    wall-clock worst case is not strictly bounded by that figure.  A metadata
    failure is final because the payload will not change.
    """
    payload: bytes | None = None
    reason = CHECK_REASON_NETWORK
    for attempt in range(1, _CHECK_ATTEMPTS + 1):
        try:
            payload = _fetch_release_payload(timeout)
            break
        except TimeoutError:
            reason = CHECK_REASON_TIMEOUT
        except URLError:
            reason = CHECK_REASON_NETWORK
        except UpdateError:
            # _checked_url() / _CheckedRedirectHandler rejected the request.
            log.debug("Update check rejected the release URL", exc_info=True)
            reason = CHECK_REASON_NETWORK
            break
        except OSError:
            # Other transport failures are not worth a second attempt.
            log.debug("Update check failed", exc_info=True)
            reason = CHECK_REASON_NETWORK
            break
        except Exception:  # noqa: BLE001 - never surface a background-check failure
            log.warning(
                "Update check failed with an unexpected error; reporting it as a network failure",
                exc_info=True,
            )
            return CheckResult(info=None, reason=CHECK_REASON_NETWORK)
        if attempt < _CHECK_ATTEMPTS:
            log.debug("Update check failed (%s); retrying once", reason)

    if payload is None:
        log.debug("Update check gave up: reason=%s", reason)
        return CheckResult(info=None, reason=reason)

    try:
        if len(payload) > _MAX_TEXT_BYTES:
            raise ValueError("release metadata is unexpectedly large")
        data = json.loads(payload.decode("utf-8"))
        # Deeply nested JSON can exhaust the stack in either the decoder or
        # the payload walk; both mean the payload is unusable.
        info = _update_from_release(data)
    except (ValueError, RecursionError):
        log.debug("Update check received unusable metadata", exc_info=True)
        return CheckResult(info=None, reason=CHECK_REASON_METADATA)

    if info is None:
        log.debug("Update check received an unusable release payload")
        return CheckResult(info=None, reason=CHECK_REASON_METADATA)

    log.debug("Update check found release %s", info.version)
    return CheckResult(info=info, reason=CHECK_REASON_OK)


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
) -> VerifiedDownload:
    """Download the release zip, verifying it against ``SHA256SUMS`` when present.

    Returns the verified path and digest.  When the release has no
    ``SHA256SUMS`` asset the download still happens, the size cap and
    deadline still apply, the digest is still returned, and only the
    comparison is skipped.  On a checksum mismatch no zip (partial or
    otherwise) is left behind.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    expected = None
    if update.checksum_url is None:
        log.info(
            "Release %s has no %s asset; skipping checksum verification",
            update.version,
            CHECKSUM_ASSET,
        )
    else:
        sums_text = _fetch_text(update.checksum_url, DOWNLOAD_TIMEOUT)
        expected = _lookup_checksum(sums_text, ZIP_ASSET)
        if expected is None:
            raise UpdateError(f"{CHECKSUM_ASSET} has no entry for {ZIP_ASSET}")

    final_path = dest / ZIP_ASSET
    partial_path = dest / f"{ZIP_ASSET}.part"
    try:
        _download_file(update.zip_url, partial_path, progress_cb)
        actual = _sha256_file(partial_path)
        if expected is not None and not hmac.compare_digest(actual.lower(), expected.lower()):
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
    return VerifiedDownload(path=final_path, sha256=actual.lower())


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


def _site_packages_dirs(plugin_dir: Path) -> list[Path]:
    """Return the plugin venv's ``site-packages`` directories, if any."""
    venv = plugin_dir / ".venv"
    found: list[Path] = []
    for pattern in _SITE_PACKAGE_GLOBS:
        try:
            candidates = sorted(venv.glob(pattern))
        except OSError:
            continue
        found.extend(path for path in candidates if path.is_dir())
    return found


def _kcaa_dist_dirs(site_dir: Path) -> list[Path]:
    """Return ``kcaa`` metadata directories inside one ``site-packages``.

    The project is the first ``-``-separated segment of the stem, so
    ``kcaa_tools-1.0.dist-info`` and ``kcaa-extra-1.0.dist-info`` do not
    match whatever their METADATA says.  The second segment must be
    version-shaped (start with a digit), which covers the easy_install
    layout ``kcaa-0.2.9-py3.13.egg-info``.  A bare ``kcaa.egg-info`` never
    matches: ``setup.py develop`` writes it into the project tree, not
    ``site-packages``, so a legacy editable install stays "unknown".
    """
    try:
        entries = sorted(site_dir.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for entry in entries:
        lowered = entry.name.lower()
        suffix = next((item for item in _DIST_SUFFIXES if lowered.endswith(item)), None)
        if suffix is None or not entry.is_dir():
            continue
        segments = entry.name[: -len(suffix)].split("-")
        if len(segments) < 2:
            continue
        if segments[0].lower() != "kcaa" or not segments[1][:1].isdigit():
            continue
        found.append(entry)
    return found


def _dist_version(dist_dir: Path) -> tuple[str | None, str | None]:
    """Read the ``Version:`` header line from ``METADATA`` or ``PKG-INFO``.

    Only the header line itself is used: RFC 822 folding (a continuation
    line) is not joined, because real metadata writers never fold this short
    header.  Returns ``(version, problem)``; ``problem`` names every file
    that was tried and why it yielded nothing (the ``OSError`` type name and
    message, or a missing header) so callers can log the cause rather than
    the outcome.
    """
    problems: list[str] = []
    for filename in ("METADATA", "PKG-INFO"):
        try:
            text = (dist_dir / filename).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{filename}: {type(exc).__name__}: {exc}")
            continue
        for line in text.splitlines():
            if not line.strip():
                break  # end of headers; never match the free-text body
            header, _, value = line.partition(":")
            if header.strip().lower() == "version" and value.strip():
                return value.strip(), None
        problems.append(f"no Version: header in {filename}")
    return None, "; ".join(problems)


def _dist_is_editable(dist_dir: Path) -> bool:
    """True for a PEP 610 editable (``direct_url.json``) install."""
    try:
        raw = (dist_dir / "direct_url.json").read_text(encoding="utf-8")
        direct = json.loads(raw)
    except (OSError, ValueError):
        return False
    info = direct.get("dir_info") if isinstance(direct, dict) else None
    return isinstance(info, dict) and info.get("editable") is True


def _version_sort_key(text: str) -> tuple[int, ...]:
    """Numeric sort key so ``0.2.10`` ranks above ``0.2.9``."""
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.\-+_]", text))


def installed_kcaa_info(plugin_dir: str | os.PathLike[str]) -> InstalledKcaa:
    """Return the venv's ``kcaa`` version and editable flag, or unknown.

    Metadata is read directly from the plugin venv's ``site-packages``, so no
    interpreter subprocess is spawned: verification cannot fail merely because
    the host process could not fork or the probe timed out.  A missing venv,
    missing/unreadable metadata, or a missing ``Version`` header means
    "unknown" rather than an error; callers fall back to installing.

    Several matching metadata directories resolve to the newest numeric
    version.  Every distinct version found is logged at warning level, since
    a stale *newer* duplicate makes the answer disagree with what the venv
    interpreter would import.
    """
    plugin_dir = Path(plugin_dir)
    site_dirs = _site_packages_dirs(plugin_dir)
    best_version: str | None = None
    best_editable = False
    best_key: tuple[int, ...] | None = None
    versions: list[str] = []
    dist_count = 0
    for site_dir in site_dirs:
        dist_dirs = _kcaa_dist_dirs(site_dir)
        if not dist_dirs:
            log.debug("no kcaa metadata directory in %s", site_dir)
            continue
        dist_count += len(dist_dirs)
        for dist_dir in dist_dirs:
            version, problem = _dist_version(dist_dir)
            if version is None:
                log.debug("kcaa metadata at %s has no usable version: %s", dist_dir, problem)
                continue
            versions.append(version)
            key = _version_sort_key(version)
            if best_key is None or key > best_key:
                best_version, best_key = version, key
                best_editable = _dist_is_editable(dist_dir)
    if best_version is None:
        if not site_dirs:
            log.debug("no site-packages directory under the plugin venv %s", plugin_dir)
        elif dist_count == 0:
            log.debug("kcaa metadata not found in the plugin venv under %s", plugin_dir)
        else:
            log.debug("no usable kcaa version in the plugin venv under %s", plugin_dir)
        return InstalledKcaa(None)

    distinct = sorted(set(versions), key=_version_sort_key)
    if len(distinct) > 1:
        log.warning(
            "multiple kcaa metadata versions in the plugin venv (%s); using %s",
            ", ".join(distinct),
            best_version,
        )
    log.debug("kcaa in plugin venv: version=%s editable=%s", best_version, best_editable)
    return InstalledKcaa(version=best_version, editable=best_editable)


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
    installed_norm = _normalize_version(installed).lower()
    expected_norm = _normalize_version(expected).lower()
    if not installed_norm or installed_norm != expected_norm:
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
) -> InstallOutcome:
    """Install a release zip over the plugin directory.

    Refuses git checkouts, unwritable directories, and leftovers from an
    interrupted install.  Before any filesystem change it resolves the
    plugin venv and reads the installed ``kcaa`` state.  An editable ``kcaa``
    install is refused outright: the plugin tree is never swapped, so a
    source checkout cannot end up with release plugin code next to a source
    package.  Otherwise, if the version differs from ``new_version``,
    it requires uv and runs a dry-run install; if it already matches, both are
    skipped so a plugin-only upgrade works offline.  Skipping the venv command
    also means dependency resolution is not re-run, so a damaged dependency
    set is not repaired by an update; rerun ``setup_plugin.sh`` for a full
    reinstall.  It then backs up the plugin tree, replaces it, verifies
    VERSION, and updates ``kcaa`` only when needed, re-reading the venv
    metadata afterwards.  The tree is restored on failure; if the venv step
    fails after the swap, the error says the plugin files were restored but
    the package update did not complete.

    Returns:
        An :class:`InstallOutcome`.  ``kcaa_updated`` is true only after a
        successful venv package refresh.  ``kcaa_skipped_reason`` is
        ``"already-current"`` when the installed version already matches and
        ``None`` after a successful refresh.

    Pass ``expected_sha256`` to re-verify a zip obtained outside
    :func:`download_and_verify`.
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

    # Pre-flight: fail before backup/clear/copy when the venv is missing, when
    # kcaa is a source install, or, only when the package actually needs
    # replacing, when uv or dependency resolution cannot succeed.
    python = _venv_python(plugin_dir)
    if python is None:
        raise UpdateError(
            f"Plugin virtual environment not found under {plugin_dir / '.venv'}. "
            "Rerun setup_plugin.sh to recreate it."
        )
    installed = installed_kcaa_info(plugin_dir)
    if installed.editable:
        # Swapping the tree would leave release plugin code next to the
        # user's source kcaa: refuse before anything is backed up or written.
        raise UpdateError(
            "kcaa is installed in editable mode; the plugin will not be upgraded "
            "in-panel. Update the checkout and rerun setup_plugin.sh.",
            restored=True,
        )
    # Textual comparison, not PEP 440: pre-release tags are not canonicalised
    # (``0.2.9-RC1`` vs ``0.2.9rc1``); see issue #226.
    needs_install = _normalize_version(installed.version or "").lower() != (
        _normalize_version(new_version).lower()
    )
    skipped_reason: str | None = None if needs_install else "already-current"
    if needs_install:
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

        if needs_install:
            venv_started = True
            _run_venv_install(plugin_dir, new_version)
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

    installed_after = installed_kcaa_info(plugin_dir) if needs_install else None
    installed_after_version = installed_after.version if installed_after else None
    try:
        # Mark completion first: if unlinking fails, has_pending_recovery()
        # must not report a successful install as an interrupted one.
        marker.write_text(_COMPLETE_MARKER, encoding="utf-8")
        marker.unlink()
    except OSError:
        log.warning("Could not clear the interrupted-update marker %s", marker)
    if needs_install and _normalize_version(installed_after_version or "").lower() != (
        # Same textual comparison as the pre-flight; pre-release canonical
        # form is issue #226.
        _normalize_version(new_version).lower()
    ):
        raise UpdateError(
            f"The plugin files were updated to {new_version}, but kcaa is still at "
            f"{installed_after_version or 'an unknown version'}. The plugin tree was not "
            "rolled back. Retry the update or rerun setup_plugin.sh.",
            restored=None,
            backup_dir=backup_dir,
        )
    _notify(progress_cb, 3, 3)
    _prune_backups(backup_dir.parent, keep=1)
    log.info("Plugin updated to %s (%s)", new_version, plugin_dir)
    return InstallOutcome(
        kcaa_updated=needs_install,
        kcaa_skipped_reason=skipped_reason,
    )
