"""
Centralized KiCad CLI detection and management.

Provides a single source of truth for locating KiCad CLI across platforms
with caching and configuration support.

``config.kicad_app_path`` points at the directory containing ``bin/kicad-cli``.
The KiCad version is known up front, so platform default candidates are built
directly from it; no directory scanning or version-less fallbacks are used.
"""

from collections.abc import Iterator
from dataclasses import dataclass
import logging
import os
import platform
import shutil
import subprocess  # nosec B404 -- controlled command execution, no user input
from typing import cast

from .config import config

logger = logging.getLogger(__name__)

_MAX_REPORTED_CANDIDATES = 10


class KiCadCLIError(Exception):
    """Raised when KiCad CLI operations fail."""

    pass


@dataclass(frozen=True)
class CLICandidate:
    """
    A single location probed while looking for the KiCad CLI.

    Attributes:
        path: Candidate executable path
        source: Where the candidate came from ("config", "path" or "common")
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

        Candidates are probed in priority order: the configured install root,
        ``PATH``, then the platform default roots for the known version. The
        first candidate that exists and passes ``--version`` validation wins;
        a candidate that exists but fails validation does not abort the search.

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
        """Return a platform-appropriate identity key for a candidate path."""
        if self._system == "Windows":
            return os.path.normcase(path).lower()
        return os.path.normcase(path)

    def _iter_candidates(self) -> Iterator[tuple[str, str]]:
        """
        Yield ``(source, path)`` candidates in priority order.

        Lazily evaluated so an early acceptance avoids the ``PATH`` lookup and
        platform defaults. Duplicate paths are yielded only once.
        """
        seen: set[str] = set()

        def fresh(source: str, path: str) -> tuple[str, str] | None:
            key = self._candidate_key(path)
            if key in seen:
                return None
            seen.add(key)
            return (source, path)

        for path in self._get_config_installation_paths():
            item = fresh("config", path)
            if item is not None:
                yield item

        for name in self._which_names():
            path_hit = shutil.which(name)
            if path_hit:
                if self._last_path_lookup is None:
                    self._last_path_lookup = path_hit
                item = fresh("path", path_hit)
                if item is not None:
                    yield item

        for path in self._get_common_installation_paths():
            item = fresh("common", path)
            if item is not None:
                yield item

    def _detect_cli_path(self) -> Iterator[CLICandidate]:
        """
        Yield KiCad CLI candidates in priority order, with existence flags.

        Candidates are not validated here; :meth:`find_kicad_cli` validates
        each existing candidate in turn.

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

    def _which_names(self) -> tuple[str, ...]:
        """Return PATH lookup names in platform-appropriate order."""
        if self._system == "Windows":
            return ("kicad-cli.exe", "kicad-cli")
        return ("kicad-cli", "kicad-cli.exe")

    def _known_version(self) -> str | None:
        """Return the configured KiCad version, or None if unavailable."""
        try:
            version = config.kicad_version
        except Exception:  # noqa: BLE001 -- config access must never break detection
            return None
        return version if isinstance(version, str) and version else None

    def _get_config_installation_paths(self) -> list[str]:
        """Return the canonical CLI path under the configured install root."""
        try:
            app_path = config.kicad_app_path
        except Exception:  # noqa: BLE001 -- config access must never break detection
            return []
        if not app_path:
            return []

        if self._system == "Darwin":
            candidate = os.path.join(app_path, "Contents", "MacOS", "kicad-cli")
        else:
            candidate = os.path.join(app_path, "bin", self._get_cli_executable_name())

        return [candidate] if os.path.isfile(candidate) else []

    def _get_common_installation_paths(self) -> list[str]:
        """Return deterministic CLI paths for platform default install roots."""
        paths: list[str] = []

        if self._system == "Darwin":
            paths.append("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
        elif self._system == "Windows":
            version = self._known_version()
            if version:
                exe_name = self._get_cli_executable_name()
                for root in self._iter_windows_install_roots():
                    paths.append(os.path.join(root, version, "bin", exe_name))
        # Linux: no hardcoded paths -- config derivation and ``which`` cover it.

        deduped: dict[str, str] = {}
        for path in paths:
            deduped.setdefault(self._candidate_key(path), path)
        return list(deduped.values())

    def _iter_windows_install_roots(self) -> Iterator[str]:
        """Yield KiCad roots from the Windows environment, when present."""
        # Windows environment names are case-insensitive; keep canonical casing.
        program_files = os.environ.get("ProgramFiles")  # noqa: SIM112
        if program_files:
            yield os.path.join(program_files, "KiCad")

        program_files_x86 = os.environ.get("ProgramFiles(x86)")  # noqa: SIM112
        if program_files_x86:
            yield os.path.join(program_files_x86, "KiCad")

        program_w6432 = os.environ.get("ProgramW6432")  # noqa: SIM112
        if program_w6432:
            yield os.path.join(program_w6432, "KiCad")

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            yield os.path.join(local_appdata, "Programs", "KiCad")

    def _build_not_found_message(self) -> str:
        """Build an actionable, bounded diagnostic message for detection failure."""
        cli_name = self._get_cli_executable_name()
        lines = ["KiCad CLI not found."]

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
            "  Set KICAD_APP_PATH to the directory containing bin/kicad-cli "
            "(e.g. C:\\Program Files\\KiCad\\10.0), or add KiCad's bin directory to PATH."
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
    """Check if KiCad CLI is available."""
    return get_cli_manager().is_available()


def get_kicad_version() -> str | None:
    """Convenience function to get KiCad CLI version."""
    return get_cli_manager().get_version()
