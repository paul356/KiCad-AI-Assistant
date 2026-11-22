"""
Centralized KiCad CLI detection and management.

Provides a single source of truth for locating KiCad CLI across platforms
with caching and configuration support.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import glob
import importlib
import logging
import ntpath
import os
import platform
import shutil
import subprocess  # nosec B404 -- controlled command execution, no user input
import sys
from typing import Any, cast

from .config import config

logger = logging.getLogger(__name__)

_MAX_REPORTED_CANDIDATES = 10

# Registry locations that may contain a KiCad uninstall entry with
# ``DisplayName`` / ``InstallLocation`` values (Windows only).
_WINDOWS_UNINSTALL_KEYS: tuple[tuple[str, str], ...] = (
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
)


class KiCadCLIError(Exception):
    """Raised when KiCad CLI operations fail."""

    pass


@dataclass(frozen=True)
class CLICandidate:
    """
    A single location probed while looking for the KiCad CLI.

    Attributes:
        path: Candidate executable path
        source: Where the candidate came from ("environment", "path",
            "common", or "registry")
        exists: Whether the candidate file exists
        validated: Whether ``--version`` succeeded; None if not validated
            (missing file or validation not attempted)
    """

    path: str
    source: str
    exists: bool
    validated: bool | None = None


@dataclass(frozen=True)
class CLIDiagnosis:
    """
    Structured snapshot of a KiCad CLI detection pass.

    Attributes:
        platform: Platform name reported by :func:`platform.system`
        candidates: Candidates probed during the pass, in priority order
        selected: Path of the accepted candidate, or None if none worked
        path_lookup: Result of the ``PATH`` lookup for kicad-cli, if any
    """

    platform: str
    candidates: tuple[CLICandidate, ...]
    selected: str | None
    path_lookup: str | None = None


def _parse_version_directory(name: str) -> tuple[int, ...] | None:
    """
    Parse a dotted numeric directory name (``9.0.4``) into a tuple.

    ``str.isdecimal`` is used rather than ``str.isdigit``: characters such
    as ``²`` are digits but not valid ``int()`` input (and are not directory
    names KiCad produces).
    """
    parts = name.split(".")
    if not parts or not all(part.isdecimal() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _sort_version_directories(names: Iterable[str]) -> list[str]:
    """
    Sort installation directory names by version, newest first.

    Dotted numeric names (``10.0``, ``9.0.4``) sort numerically descending.
    Names that cannot be parsed (``nightly``) follow, sorted alphabetically.
    """
    numeric: list[tuple[tuple[int, ...], str]] = []
    other: list[str] = []
    for name in names:
        version = _parse_version_directory(name)
        if version is None:
            other.append(name)
        else:
            numeric.append((version, name))
    numeric.sort(key=lambda item: item[1])
    numeric.sort(key=lambda item: item[0], reverse=True)
    other.sort()
    return [name for _, name in numeric] + other


def _path_module_for(path: str) -> Any:
    """
    Return ``ntpath`` for Windows-style paths, ``os.path`` otherwise.

    Detection tests simulate Windows on POSIX, so path helpers must not rely
    on ``os.path`` always matching the simulated platform.
    """
    if ntpath.splitdrive(path)[0] or "\\" in path:
        return ntpath
    return os.path


def _join_path(path: str, *parts: str) -> str:
    """Join path components using the flavour implied by ``path``."""
    return cast(str, _path_module_for(path).join(path, *parts))


def _iter_ancestor_dirs(path: str, max_levels: int = 5) -> Iterator[str]:
    """
    Yield ``path`` and its parents, up to ``max_levels`` directories.

    Stops before the filesystem root; empty paths yield nothing.
    """
    if not path:
        return
    module = _path_module_for(path)
    current = module.normpath(path)
    for _ in range(max_levels):
        parent = module.dirname(current)
        if not parent or parent == current:
            break
        yield current
        current = parent


def _iter_appimage_mount_dirs(max_mounts: int = 8) -> Iterator[str]:
    """Yield recent AppImage mount directories (``/tmp/.mount_*``)."""
    try:
        mounts = [path for path in glob.glob("/tmp/.mount_*") if os.path.isdir(path)]
    except OSError:
        return

    def mount_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    mounts.sort(key=lambda path: (-mount_mtime(path), path))
    yield from mounts[:max_mounts]


def _iter_appimage_cli_paths() -> Iterator[str]:
    """Yield ``kicad-cli`` paths from the current AppImage mount and APPDIR."""
    app_dir = os.environ.get("APPDIR")
    if app_dir:
        yield os.path.join(app_dir, "usr", "bin", "kicad-cli")
    for mount in _iter_appimage_mount_dirs():
        yield os.path.join(mount, "usr", "bin", "kicad-cli")


def _list_windows_drives() -> list[str]:
    """Return Windows logical drive roots (``C:\\\\`` style)."""
    listdrives = getattr(os, "listdrives", None)
    if callable(listdrives):
        try:
            return list(listdrives())
        except OSError:
            return []

    try:
        import ctypes  # Windows-only lazy import
    except ImportError:
        return []

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return []
    try:
        mask = windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    return [f"{chr(ord('A') + index)}:\\" for index in range(26) if mask & (1 << index)]


_WINDOWS_DRIVE_KICAD_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("KiCad",),
    ("Program Files", "KiCad"),
    ("Program Files (x86)", "KiCad"),
    ("Programs", "KiCad"),
)


def _iter_windows_drive_roots() -> Iterator[str]:
    """Yield the four fixed KiCad roots on every Windows logical drive."""
    for drive in _list_windows_drives():
        if not drive:
            continue
        for parts in _WINDOWS_DRIVE_KICAD_SUBDIRS:
            yield ntpath.join(drive, *parts)


class KiCadCLIManager:
    """
    Manages KiCad CLI detection and validation across platforms.

    Provides caching and fallback mechanisms for reliable CLI access.
    """

    def __init__(self):
        """Initialize the CLI manager."""
        self._cached_cli_path: str | None = None
        self._cache_validated = False
        self._system = platform.system()
        self._last_candidates: list[CLICandidate] = []
        self._last_path_lookup: str | None = None

    def find_kicad_cli(self, force_refresh: bool = False) -> str | None:
        """
        Find the KiCad CLI executable path.

        Candidates are probed in priority order (KICAD_CLI_PATH, PATH, common
        installation directories). The first candidate that exists and passes
        ``--version`` validation wins; a candidate that exists but fails
        validation does not abort the search. On Windows, registry uninstall
        entries are probed as a last resort when nothing earlier validated.

        Args:
            force_refresh: Force re-detection even if cached

        Returns:
            Path to kicad-cli executable or None if not found
        """
        # Return cached path if available and valid
        if self._cached_cli_path and not force_refresh and self._cache_validated:
            return self._cached_cli_path

        self._last_candidates = []
        self._last_path_lookup = None
        probes: list[CLICandidate] = []
        selected: str | None = None

        for candidate in self._detect_cli_path():
            if not candidate.exists:
                probes.append(candidate)
                logger.debug("Skipping missing KiCad CLI candidate: %s", candidate.path)
                continue

            valid = self._validate_cli_path(candidate.path)
            probes.append(
                CLICandidate(
                    path=candidate.path,
                    source=candidate.source,
                    exists=True,
                    validated=valid,
                )
            )
            if valid:
                selected = candidate.path
                break

            logger.warning("KiCad CLI candidate %s failed validation; trying next", candidate.path)
            if candidate.source == "environment":
                logger.warning(
                    "KICAD_CLI_PATH=%s is not usable; falling back to other locations",
                    candidate.path,
                )

        self._last_candidates = probes

        if selected is not None:
            self._cached_cli_path = selected
            self._cache_validated = True
            logger.info("Found KiCad CLI at: %s", selected)
            return selected

        # Clear cache if detection failed
        self._cached_cli_path = None
        self._cache_validated = False
        logger.warning("KiCad CLI not found on this system")
        return None

    def get_cli_path(self, required: bool = True) -> str:
        """
        Get KiCad CLI path, raising exception if not found and required.

        Args:
            required: Whether to raise exception if CLI not found

        Returns:
            Path to kicad-cli executable

        Raises:
            KiCadCLIError: If CLI not found and required=True
        """
        cli_path = self.find_kicad_cli()

        if cli_path is None and required:
            raise KiCadCLIError(self._build_not_found_message())

        # ``required=False`` historically returns None despite the ``str``
        # annotation; keep that API while satisfying type checkers.
        return cast(str, cli_path)

    def is_available(self) -> bool:
        """Check if KiCad CLI is available."""
        return self.find_kicad_cli() is not None

    def get_version(self) -> str | None:
        """
        Get KiCad CLI version string.

        Returns:
            Version string or None if CLI not available
        """
        cli_path = self.find_kicad_cli()
        if not cli_path:
            return None

        try:
            result = subprocess.run(  # nosec B603 - CLI path is validated
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=config.timeout_constants["kicad_cli_version_check"],
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"Failed to get KiCad CLI version: {e}")

        return None

    def diagnose(self, force_refresh: bool = False) -> CLIDiagnosis:
        """
        Return a read-only snapshot of the most recent detection pass.

        Intended for UI/diagnostics consumers that need to explain why ERC or
        DRC commands are unavailable. This does not alter detection semantics;
        the returned ``selected`` value matches :meth:`find_kicad_cli`.

        Args:
            force_refresh: Re-probe instead of using the cached result

        Returns:
            Structured diagnosis with every probed candidate.
        """
        selected = self.find_kicad_cli(force_refresh=force_refresh)
        return CLIDiagnosis(
            platform=self._system,
            candidates=tuple(self._last_candidates),
            selected=selected,
            path_lookup=self._last_path_lookup,
        )

    def _candidate_key(self, path: str) -> str:
        """
        Return a platform-appropriate identity key for a candidate path.

        Windows paths are case-insensitive, so case variants (including
        environment variables that differ only in casing) must deduplicate.
        ``ntpath.normcase`` provides that behaviour even when tests simulate
        Windows on POSIX.
        """
        if self._system == "Windows":
            return ntpath.normcase(path)
        return os.path.normcase(path)

    def _iter_candidates(self) -> Iterator[tuple[str, str]]:
        """
        Yield ``(source, path)`` candidates in priority order.

        Lazily evaluated so an early acceptance avoids enumerating directories
        or reading the registry. Duplicate paths are yielded only once.
        """
        seen: set[str] = set()

        def fresh(source: str, path: str) -> tuple[str, str] | None:
            key = self._candidate_key(path)
            if key in seen:
                return None
            seen.add(key)
            return (source, path)

        env_path = os.environ.get("KICAD_CLI_PATH")
        if env_path:
            if not os.path.isfile(env_path):
                logger.warning(
                    "KICAD_CLI_PATH points to %s, which does not exist; ignoring it", env_path
                )
            item = fresh("environment", env_path)
            if item is not None:
                yield item

        for path in self._get_config_installation_paths():
            item = fresh("config", path)
            if item is not None:
                yield item

        cli_name = self._get_cli_executable_name()
        path_hit = shutil.which(cli_name)
        self._last_path_lookup = path_hit
        if path_hit:
            item = fresh("path", path_hit)
            if item is not None:
                yield item

        if self._system == "Linux":
            for path in _iter_appimage_cli_paths():
                item = fresh("appimage", path)
                if item is not None:
                    yield item

        for path in self._get_common_installation_paths():
            item = fresh("common", path)
            if item is not None:
                yield item

        if self._system == "Windows":
            for path in self._get_drive_installation_paths():
                item = fresh("drive", path)
                if item is not None:
                    yield item

        # Registry is the last resort. Being lazy, this is only reached when
        # the caller consumed every earlier candidate without accepting one,
        # i.e. when no candidate passed validation.
        if self._system == "Windows":
            for path in self._get_registry_installation_paths():
                item = fresh("registry", path)
                if item is not None:
                    yield item

    def _detect_cli_path(self) -> Iterator[CLICandidate]:
        """
        Yield KiCad CLI candidates in priority order, with existence flags.

        This is lazy: callers that stop early never enumerate installation
        directories or read the registry. Candidates are not validated here;
        :meth:`find_kicad_cli` validates each existing candidate in turn.

        Yields:
            Candidates in priority order (may include non-existent paths)
        """
        for source, path in self._iter_candidates():
            yield CLICandidate(path=path, source=source, exists=os.path.isfile(path))

    def _get_cli_executable_name(self) -> str:
        """Get the CLI executable name for current platform."""
        if self._system == "Windows":
            return "kicad-cli.exe"
        return "kicad-cli"

    def _get_common_installation_paths(self) -> list[str]:
        """Get list of common installation paths for current platform."""
        paths: list[str] = []

        if self._system == "Darwin":  # macOS
            paths.extend(
                [
                    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                    "/Applications/KiCad/kicad-cli",
                    "/usr/local/bin/kicad-cli",
                    "/opt/homebrew/bin/kicad-cli",
                ]
            )
        elif self._system == "Windows":
            # Versioned install dirs first (newest version first); the
            # unversioned locations below remain as a fallback.
            paths.extend(self._get_versioned_installation_paths())
            paths.extend(
                [
                    r"C:\Program Files\KiCad\bin\kicad-cli.exe",
                    r"C:\Program Files (x86)\KiCad\bin\kicad-cli.exe",
                    r"C:\KiCad\bin\kicad-cli.exe",
                ]
            )
        else:  # Linux and other Unix-like systems
            paths.extend(
                [
                    "/usr/bin/kicad-cli",
                    "/usr/local/bin/kicad-cli",
                    "/opt/kicad/bin/kicad-cli",
                    "/snap/kicad/current/usr/bin/kicad-cli",
                ]
            )

        deduped: dict[str, str] = {}
        for path in paths:
            deduped.setdefault(self._candidate_key(path), path)
        return list(deduped.values())

    def _get_config_installation_paths(self) -> list[str]:
        """
        Derive CLI candidates from the configured KiCad paths.

        Tries ``<kicad_app_path>/<version>/bin/kicad-cli`` first, then walks
        each configured symbol/footprint/template directory upwards looking
        for a sibling ``bin`` directory. Only existing executables are
        returned, so user-created library directories never add noise.
        """
        exe_name = self._get_cli_executable_name()

        def read_config_path(name: str) -> str | None:
            try:
                value = getattr(config, name, None)
            except OSError:
                return None
            return value if isinstance(value, str) and value else None

        paths: list[str] = []

        app_path = read_config_path("kicad_app_path")
        version = read_config_path("kicad_version")
        if app_path and version:
            candidate = _join_path(app_path, version, "bin", exe_name)
            if os.path.isfile(candidate):
                paths.append(candidate)

        for attr in ("kicad_symbol_dir", "kicad_footprint_dir", "kicad_template_dir"):
            base = read_config_path(attr)
            if not base:
                continue
            for ancestor in _iter_ancestor_dirs(base):
                candidate = _join_path(ancestor, "bin", exe_name)
                if os.path.isfile(candidate):
                    paths.append(candidate)
                if self._system == "Darwin":
                    module = _path_module_for(ancestor)
                    normalized = module.normpath(ancestor)
                    bundle_parent = module.basename(module.dirname(normalized))
                    if module.basename(normalized) == "Contents" and bundle_parent.lower().endswith(
                        ".app"
                    ):
                        macos_candidate = module.join(ancestor, "MacOS", "kicad-cli")
                        if os.path.isfile(macos_candidate):
                            paths.append(macos_candidate)

        deduped: dict[str, str] = {}
        for path in paths:
            deduped.setdefault(self._candidate_key(path), path)
        return list(deduped.values())

    def _get_drive_installation_paths(self) -> list[str]:
        """
        Enumerate versioned installs on every Windows logical drive.

        Only four fixed roots per drive are inspected; there is no recursive
        scan of the drive.
        """
        exe_name = self._get_cli_executable_name()
        paths: list[str] = []
        for root in _iter_windows_drive_roots():
            for directory in self._list_version_directories(root):
                paths.append(_join_path(root, directory, "bin", exe_name))
        return paths

    def _get_versioned_installation_paths(self) -> list[str]:
        """
        Enumerate ``<root>\\<version>\\bin\\kicad-cli.exe`` on Windows.

        Roots come from environment variables so tests can simulate Windows
        layouts on other platforms. Missing/unreadable roots are ignored.
        """
        exe_name = self._get_cli_executable_name()
        paths: list[str] = []
        for root in self._iter_windows_install_roots():
            for directory in self._list_version_directories(root):
                paths.append(_join_path(root, directory, "bin", exe_name))
        return paths

    def _iter_windows_install_roots(self) -> Iterator[str]:
        """Yield candidate ``...\\KiCad`` installation roots from the environment."""
        # Windows environment names are case-insensitive; keep canonical casing.
        program_files = os.environ.get("ProgramFiles")  # noqa: SIM112
        if program_files:
            yield os.path.join(program_files, "KiCad")

        program_files_x86 = os.environ.get("ProgramFiles(x86)")  # noqa: SIM112
        if program_files_x86:
            yield os.path.join(program_files_x86, "KiCad")

        # A 32-bit process sees ProgramFiles/WOW64 redirects; ProgramW6432 is
        # the real 64-bit Program Files directory, when present.
        program_w6432 = os.environ.get("ProgramW6432")  # noqa: SIM112
        if program_w6432:
            yield os.path.join(program_w6432, "KiCad")

        # KiCad also supports per-user installations.
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            yield os.path.join(local_appdata, "Programs", "KiCad")

    def _list_version_directories(self, root: str) -> list[str]:
        """List version-like child directories of ``root``, newest first."""
        names: list[str] = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir():
                            names.append(entry.name)
                    except OSError:
                        # Broken symlink or permission issue on this entry.
                        continue
        except OSError:
            # Root missing or unreadable: no versioned installs here.
            return []

        # Version-like names are always kept; other names (``share``,
        # ``docs``, ``nightly``) only when they contain a real CLI binary.
        exe_name = self._get_cli_executable_name()
        kept = [
            name
            for name in names
            if _parse_version_directory(name) is not None
            or os.path.isfile(_join_path(root, name, "bin", exe_name))
        ]
        return _sort_version_directories(kept)

    def _get_registry_installation_paths(self) -> list[str]:
        """
        Read KiCad install locations from the Windows uninstall registry.

        The ``winreg`` import is lazy and the whole function is a no-op
        outside Windows. Any registry error is swallowed: this is a fallback
        and must never mask the primary detection results.
        """
        # ``getattr`` keeps type checkers from constant-folding ``sys.platform``
        # and treating the rest of this Windows-only function as unreachable.
        if getattr(sys, "platform", "") != "win32":
            return []

        try:
            winreg: Any = importlib.import_module("winreg")
        except ImportError:
            return []

        hives: dict[str, Any] = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
        }
        exe_name = self._get_cli_executable_name()
        found: list[str] = []

        for hive_name, key_path in _WINDOWS_UNINSTALL_KEYS:
            hive = hives[hive_name]
            try:
                key = winreg.OpenKey(hive, key_path)
            except OSError:
                continue
            try:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    install_location = self._read_registry_install_location(
                        winreg, hive, key_path, subkey_name
                    )
                    if install_location:
                        found.append(self._join_windows_cli_path(install_location, exe_name))
            finally:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass

        return list(dict.fromkeys(found))

    @staticmethod
    def _read_registry_install_location(
        winreg_module: Any, hive: Any, key_path: str, subkey_name: str
    ) -> str | None:
        """Return ``InstallLocation`` for a KiCad uninstall entry, if present."""
        try:
            subkey = winreg_module.OpenKey(hive, ntpath.join(key_path, subkey_name))
        except OSError:
            return None
        try:
            try:
                display_name, _ = winreg_module.QueryValueEx(subkey, "DisplayName")
            except OSError:
                return None
            if not isinstance(display_name, str) or "kicad" not in display_name.lower():
                return None
            try:
                install_location, _ = winreg_module.QueryValueEx(subkey, "InstallLocation")
            except OSError:
                return None
            if not isinstance(install_location, str):
                return None
            stripped = install_location.strip().strip('"')
            return stripped or None
        finally:
            try:
                winreg_module.CloseKey(subkey)
            except OSError:
                pass

    @staticmethod
    def _join_windows_cli_path(install_location: str, exe_name: str) -> str:
        """Join a registry install location to its kicad-cli executable."""
        if ntpath.basename(install_location.rstrip("\\/")).lower() == "bin":
            return ntpath.join(install_location, exe_name)
        return ntpath.join(install_location, "bin", exe_name)

    def _build_not_found_message(self) -> str:
        """Build an actionable, bounded diagnostic message for detection failure."""
        cli_name = self._get_cli_executable_name()
        lines = ["KiCad CLI not found."]

        env_path = os.environ.get("KICAD_CLI_PATH")
        if env_path:
            if os.path.isfile(env_path):
                lines.append(
                    f"  KICAD_CLI_PATH={env_path!r} exists but is not a working {cli_name}."
                )
            else:
                lines.append(f"  KICAD_CLI_PATH={env_path!r} does not point to an existing file.")
        else:
            lines.append("  KICAD_CLI_PATH is not set.")

        if self._last_path_lookup:
            lines.append(
                f"  PATH lookup for {cli_name} found {self._last_path_lookup} (not usable)."
            )
        else:
            lines.append(f"  PATH lookup for {cli_name}: no executable found.")

        candidates = self._last_candidates
        if candidates:
            lines.append(f"  Tried {len(candidates)} candidate location(s):")
            for candidate in candidates[:_MAX_REPORTED_CANDIDATES]:
                # The message is only built when no candidate validated.
                status = "exists but failed --version" if candidate.exists else "missing"
                lines.append(f"    - [{candidate.source}] {candidate.path} ({status})")
            remaining = len(candidates) - _MAX_REPORTED_CANDIDATES
            if remaining > 0:
                lines.append(f"    ... and {remaining} more")
        else:
            lines.append("  No candidate locations were available to probe.")

        lines.append(
            "  Set KICAD_CLI_PATH to the full path of kicad-cli, "
            "or add KiCad's bin directory to PATH."
        )
        return "\n".join(lines)

    def _validate_cli_path(self, cli_path: str) -> bool:
        """
        Validate that a CLI path is working.

        Args:
            cli_path: Path to validate

        Returns:
            True if CLI is working
        """
        try:
            result = subprocess.run(  # nosec B603 - CLI path is validated
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=config.timeout_constants["kicad_cli_version_check"],
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError, ValueError):
            # ValueError covers UnicodeDecodeError from text=True output.
            return False


# Global CLI manager instance
_cli_manager = None


def get_cli_manager() -> KiCadCLIManager:
    """Get the global KiCad CLI manager instance."""
    global _cli_manager
    if _cli_manager is None:
        _cli_manager = KiCadCLIManager()
    return _cli_manager


def find_kicad_cli(force_refresh: bool = False) -> str | None:
    """Convenience function to find KiCad CLI path."""
    return get_cli_manager().find_kicad_cli(force_refresh)


def get_kicad_cli_path(required: bool = True) -> str:
    """Convenience function to get KiCad CLI path."""
    return get_cli_manager().get_cli_path(required)


def is_kicad_cli_available() -> bool:
    """Convenience function to check if KiCad CLI is available."""
    return get_cli_manager().is_available()


def get_kicad_version() -> str | None:
    """Convenience function to get KiCad CLI version."""
    return get_cli_manager().get_version()
