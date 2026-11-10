"""
Path validation utility for KiCad MCP.

Provides secure path validation to prevent path traversal attacks
and ensure file operations are restricted to safe directories.

Containment is decided on fully-resolved paths (``realpath``) with
``os.path.commonpath``, so ``..`` segments and symlinks cannot escape a
trusted root.

The trust roots are:

1. the open KiCad project, when one is known;
2. the KiCad user directory (``config.kicad_user_dir``);
3. KiCad's symbol, footprint and third-party directories;
4. every entry of ``config.additional_search_paths``;
5. the server's private temp directory (``<kcaa data dir>/tmp``), solely for
   server-generated output that the subprocess runner validates;
6. roots passed explicitly to the constructor (tests/embedding only).

The process working directory is deliberately NOT a root: it is unrelated to
the open project and to KiCad, and under the plugin it is the plugin data
directory. The rest of the kcaa data directory — settings, session records,
the server ``.env`` — is outside the trust boundary. The project root is
optional (it may be ``None``) and is re-read from a state file the plugin
publishes on every validation call (see ``PathValidator``).
"""

import json
import logging
import os
import tempfile
import threading

from kcaa.utils.config import config

log = logging.getLogger(__name__)

# Set by the plugin launcher; names the state file the panel writes whenever
# the open project changes. Missing/unreadable/malformed state is not fatal.
_PROJECT_STATE_ENV = "KCAA_PROJECT_STATE_FILE"

# Private server temp directory, created below the kcaa data directory.
_TEMP_DIR_NAME = "tmp"
_TEMP_DIR_MODE = 0o700


class PathValidationError(Exception):
    """Raised when path validation fails."""

    pass


def _normalize(path: str) -> str:
    """Return the absolute, symlink-resolved, user-expanded form of *path*."""
    return os.path.realpath(os.path.expanduser(path))


class PathValidator:
    """Validate paths against the project and KiCad directory trust roots.

    A path is accepted when its fully-resolved form is contained in one of
    the roots listed in the module docstring. Containment is checked with
    ``os.path.commonpath`` on realpaths, so neither ``..`` traversal nor an
    escaping symlink can smuggle a path out of a root.

    The project root is **optional**. When it is ``None``, matching is done
    by the KiCad roots alone, which is normal for a standalone server with no
    plugin publishing a project. In plugin mode the root is re-set in place
    whenever the active project changes: the panel atomically writes
    ``<kcaa data dir>/active_project.json`` containing
    ``{"project_root": "<abs path>"}`` (or ``null``), and each ``validate_*``
    call re-reads that file and applies a changed value in place. The state
    file is a trusted channel written by the plugin, never by the model;
    ``KCAA_PROJECT_STATE_FILE`` tells the server where it lives. A missing,
    unreadable or malformed file keeps the current root.
    """

    def __init__(self, trusted_roots: set[str] | None = None):
        """Initialize the validator.

        Args:
            trusted_roots: Extra root directories, for tests/embedding only.
                Production callers leave this unset; the trust roots are the
                project root, the configured KiCad directories and the private
                server temp directory. The process working directory is never
                added implicitly.
        """
        self._lock = threading.Lock()
        self._project_root: str | None = None
        self._state_file = os.environ.get(_PROJECT_STATE_ENV)
        self._data_dir = config.get_kcaa_data_dir()
        # The fixed roots are resolved once: realpath may touch a stalled
        # network mount, and it must never run while holding the lock.
        self._static_roots = self._compute_static_roots(
            {_normalize(root) for root in trusted_roots or ()}
        )
        self.trusted_roots: set[str] = set(self._static_roots)

    # ------------------------------------------------------------------ #
    # Project root
    # ------------------------------------------------------------------ #

    def set_project_root(self, path: str | None) -> None:
        """Set (or clear) the open project's root directory.

        The project root is a single entry: replacing it drops the previous
        one. ``None`` (or an empty string) clears it, leaving the KiCad
        directory roots in effect. A path that exists and is a regular file is
        refused — the published contract is a directory (the parent of the
        open ``.kicad_pro`` file).

        Args:
            path: Absolute path to the open project directory, or None.

        Raises:
            PathValidationError: If *path* is an existing regular file.
        """
        normalized = _normalize(path) if path else None
        self._install_root(normalized)

    def _install_root(self, normalized: str | None) -> None:
        """Install an already-normalized project root.

        The caller resolves the path (``realpath``) exactly once: resolving it
        again here would add a filesystem round-trip that can fail between the
        state-file read and the install, and there is nothing left to resolve.

        Raises:
            PathValidationError: If *normalized* is an existing regular file.
        """
        if normalized and os.path.isfile(normalized):
            raise PathValidationError(f"Project root must be a directory, not a file: {normalized}")
        with self._lock:
            if normalized == self._project_root:
                return
            self._project_root = normalized
            roots = set(self._static_roots)
            if normalized:
                roots.add(normalized)
            self.trusted_roots = roots

    def _compute_static_roots(self, extra_roots: set[str]) -> set[str]:
        """Resolve the roots that never change for this validator instance.

        Runs once in ``__init__`` (no lock held). The project root is the only
        moving part and is added in ``set_project_root``.
        """
        roots = set(extra_roots)
        temp_dir = os.path.join(self._data_dir, _TEMP_DIR_NAME)
        if os.path.islink(temp_dir):
            # Trusting its target would widen the boundary beyond the private
            # temp dir; create_safe_temp_path refuses it for writes too.
            log.warning("Ignoring symlinked server temp directory as a trust root: %s", temp_dir)
        else:
            # Only the private temp subdirectory is trusted — never the data
            # directory itself, which holds settings, sessions and the server
            # .env. This entry exists solely for server-generated output that
            # the subprocess runner validates (see create_safe_temp_path).
            roots.add(_normalize(temp_dir))
        for root in (
            config.kicad_user_dir,
            config.kicad_symbol_dir,
            config.kicad_footprint_dir,
            config.kicad_3rd_party,
            *config.additional_search_paths,
        ):
            if root:
                roots.add(_normalize(root))
        return roots

    def _refresh_project_root(self) -> None:
        """Reload the project root from the plugin-published state file.

        The file is tiny, so it is re-read and parsed on every validation call
        (no mtime tracking). Any failure — missing/unreadable file, malformed
        JSON, wrong type (including a transient Windows ``os.replace``
        collision with an open handle) — keeps the current root and never
        raises.
        """
        state_file = self._state_file
        if not state_file:
            return
        try:
            with open(state_file, encoding="utf-8") as f:
                payload = json.load(f)
            root = payload["project_root"]
            if root is not None and not isinstance(root, str):
                raise TypeError("project_root must be a string or null")
            normalized = _normalize(root) if root else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.debug(
                "Ignoring unreadable project state file %s (%s); keeping project root %r",
                state_file,
                exc,
                self._project_root,
            )
            return

        if normalized == self._project_root:
            return
        log.debug("Project state file %s: project_root=%r", state_file, root)
        try:
            self._install_root(normalized)
        except (PathValidationError, OSError, ValueError) as exc:
            log.debug(
                "Ignoring unusable project root from %s (%s); keeping %r",
                state_file,
                exc,
                self._project_root,
            )

    # ------------------------------------------------------------------ #
    # Public validation API
    # ------------------------------------------------------------------ #

    def validate_path(self, file_path: str, must_exist: bool = False) -> str:
        """
        Validate a file path for security and correctness.

        Args:
            file_path: Path to validate
            must_exist: Whether the file must exist

        Returns:
            Normalized absolute path

        Raises:
            PathValidationError: If path validation fails
        """
        if not file_path or not isinstance(file_path, str):
            raise PathValidationError("Path must be a non-empty string")

        # Pick up a project switch published by the plugin before matching.
        self._refresh_project_root()

        try:
            # Expand user home directory and resolve symbolic links
            normalized_path = os.path.realpath(os.path.expanduser(file_path))
        except (OSError, ValueError) as e:
            raise PathValidationError(f"Invalid path: {e}") from e

        # Check if path is within trusted roots
        if not self._is_within_trusted_roots(normalized_path):
            raise PathValidationError(f"Path '{file_path}' is outside trusted directories")

        # Check if file exists when required
        if must_exist and not os.path.exists(normalized_path):
            raise PathValidationError(f"Path does not exist: {file_path}")

        return normalized_path

    def validate_kicad_file(self, file_path: str, file_type: str, must_exist: bool = True) -> str:
        """
        Validate a KiCad file path with extension checking.

        Args:
            file_path: Path to validate
            file_type: Expected KiCad file type ('project', 'schematic', 'pcb', etc.)
            must_exist: Whether the file must exist

        Returns:
            Normalized absolute path

        Raises:
            PathValidationError: If path validation fails
        """
        # First validate the basic path
        normalized_path = self.validate_path(file_path, must_exist)

        # Check file extension
        if file_type not in config.kicad_extensions:
            raise PathValidationError(f"Unknown KiCad file type: {file_type}")

        expected_extension = config.kicad_extensions[file_type]
        if not normalized_path.endswith(expected_extension):
            raise PathValidationError(
                f"File must have {expected_extension} extension, got: {file_path}"
            )

        return normalized_path

    def validate_directory(self, dir_path: str, must_exist: bool = True) -> str:
        """
        Validate a directory path.

        Args:
            dir_path: Directory path to validate
            must_exist: Whether the directory must exist

        Returns:
            Normalized absolute directory path

        Raises:
            PathValidationError: If validation fails
        """
        normalized_path = self.validate_path(dir_path, must_exist)

        if must_exist and not os.path.isdir(normalized_path):
            raise PathValidationError(f"Path is not a directory: {dir_path}")

        return normalized_path

    def validate_project_directory(self, project_path: str) -> str:
        """
        Validate and return the directory containing a KiCad project file.

        Args:
            project_path: Path to .kicad_pro file

        Returns:
            Normalized absolute directory path

        Raises:
            PathValidationError: If validation fails
        """
        validated_project = self.validate_kicad_file(project_path, "project", must_exist=True)
        return os.path.dirname(validated_project)

    def create_safe_temp_path(self, base_name: str, extension: str = "") -> str:
        """
        Create a safe temporary file path for server-generated output.

        Temp files never depend on the project root: they live in a private
        directory below the server's data directory, created with mode
        ``0o700``. A symlinked temp directory is refused. The file itself is
        created with ``mkstemp`` (unique name, mode ``0o600``).

        Args:
            base_name: Base name for the temporary file
            extension: File extension (including dot)

        Returns:
            Safe temporary file path

        Raises:
            PathValidationError: If the temp directory is a symlink or cannot
                be created.
        """
        temp_dir = os.path.join(self._data_dir, _TEMP_DIR_NAME)
        if os.path.islink(temp_dir):
            raise PathValidationError(f"Refusing symlinked temp directory: {temp_dir}")
        try:
            os.makedirs(temp_dir, mode=_TEMP_DIR_MODE, exist_ok=True)
            os.chmod(temp_dir, _TEMP_DIR_MODE)
        except OSError as e:
            raise PathValidationError(f"Cannot prepare temp directory {temp_dir}: {e}") from e

        temp_fd, temp_path = tempfile.mkstemp(
            suffix=extension, prefix=f"{base_name}_", dir=temp_dir
        )
        os.close(temp_fd)  # Close the file descriptor, we just need the path

        return temp_path

    def _is_within_trusted_roots(self, path: str) -> bool:
        """
        Check if a path is within any trusted root directory.

        Both *path* and every root are already realpaths, so containment via
        ``commonpath`` rejects ``..`` traversal and escaping symlinks. A
        ``ValueError`` (different drives on Windows) means "not contained".

        Args:
            path: Normalized absolute path to check

        Returns:
            True if path is within trusted roots
        """
        for root in self.trusted_roots:
            try:
                if os.path.commonpath((root, path)) == root:
                    return True
            except ValueError:
                continue
        return False


# Global default validator instance
_default_validator = None


def get_default_validator() -> PathValidator:
    """Get the default global path validator instance."""
    global _default_validator
    if _default_validator is None:
        _default_validator = PathValidator()
    return _default_validator


def validate_path(file_path: str, must_exist: bool = False) -> str:
    """Convenience function using default validator."""
    return get_default_validator().validate_path(file_path, must_exist)


def validate_kicad_file(file_path: str, file_type: str, must_exist: bool = True) -> str:
    """Convenience function using default validator."""
    return get_default_validator().validate_kicad_file(file_path, file_type, must_exist)


def validate_directory(dir_path: str, must_exist: bool = True) -> str:
    """Convenience function using default validator."""
    return get_default_validator().validate_directory(dir_path, must_exist)
