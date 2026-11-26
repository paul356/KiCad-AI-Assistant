"""
KiCad Plugin: AI Assistant powered by the kcaa MCP server.

This is the KiCad action plugin entry point. KiCad discovers this file via
the plugin directory and calls KiCadAIPlugin().register() at startup.

Usage:
  Manual install (unzip kicad_ai_assistant.zip):
    - Linux:   ~/.local/share/kicad/<ver>/scripting/plugins/kicad_ai_assistant/
    - macOS:   ~/Documents/KiCad/<ver>/scripting/plugins/kicad_ai_assistant/
    - Windows: %USERPROFILE%\\Documents\\KiCad\\<ver>\\scripting\\plugins\\kicad_ai_assistant\\
  PCM install (kicad_ai_assistant_pcm.zip):
    <documents>/kicad/<ver>/3rdparty/plugins/com_github_paul356_kicad-ai-assistant/
"""

import logging
import os

log = logging.getLogger(__name__)


def _setup_plugin_logging() -> None:
    """Configure logging for the plugin (writes to KiCad config directory).

    Sets the ``kicad_plugin`` logger hierarchy to DEBUG and writes to a file
    so diagnostic messages are available when debugging on Windows.
    """
    try:
        from .settings import _get_kcaa_data_dir

        log_dir = _get_kcaa_data_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "kicad_ai_plugin.log")

        # Use append mode so logs survive KiCad restarts (the typical
        # debugging workflow is: reproduce freeze → restart → examine logs).
        handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )

        # Attach to THIS package's logger (and all child loggers).
        # __name__ resolves to the actual package name (e.g. kicad_ai_assistant
        # in the installed plugin, kicad_plugin in the dev tree).
        plugin_logger = logging.getLogger(__name__)
        plugin_logger.setLevel(logging.DEBUG)
        plugin_logger.addHandler(handler)
        plugin_logger.propagate = False  # don't duplicate to root logger
        log.info("=" * 60)
        log.info("Plugin logging started: %s (logger=%s)", log_file, __name__)
    except Exception as exc:
        # Logging setup must never crash the plugin
        log.warning("Could not set up plugin logging: %s", exc)


_BUILD_IDENTITY_MAX_BYTES = 1024 * 1024


def _read_small_text(path: str, what: str) -> str | None:
    """Read a small UTF-8 file; ``None`` on failure or when it exceeds 1 MiB."""
    try:
        if os.path.getsize(path) > _BUILD_IDENTITY_MAX_BYTES:
            log.debug("build identity: %s too large: %s", what, path)
            return None
        with open(path, encoding="utf-8", errors="strict") as handle:
            content = handle.read(_BUILD_IDENTITY_MAX_BYTES + 1)
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("build identity: cannot read %s (%s): %s", what, exc, path)
        return None
    if len(content) > _BUILD_IDENTITY_MAX_BYTES:
        log.debug("build identity: %s too large: %s", what, path)
        return None
    return content


def _resolve_git_dir(start: str) -> str | None:
    """Locate the git directory for the checkout containing *start*.

    ``.git`` may be a directory, or a worktree/submodule file holding a
    ``gitdir: <path>`` line (relative paths resolve against that directory).
    """
    current = os.path.dirname(os.path.abspath(start))
    while True:
        dot_git = os.path.join(current, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            content = _read_small_text(dot_git, ".git file")
            if content is None:
                return None
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped.lower().startswith("gitdir:"):
                    continue
                target = stripped.split(":", 1)[1].strip()
                if not target:
                    log.debug("build identity: empty gitdir in %s", dot_git)
                    return None
                if not os.path.isabs(target):
                    target = os.path.join(current, target)
                return os.path.normpath(target)
            log.debug("build identity: no gitdir line in %s", dot_git)
            return None
        parent = os.path.dirname(current)
        if parent == current:
            log.debug("build identity: no .git above %s", start)
            return None
        current = parent


def _common_git_dir(git_dir: str) -> str:
    """Follow a linked worktree's ``commondir`` so refs resolve correctly."""
    content = _read_small_text(os.path.join(git_dir, "commondir"), "commondir")
    if content:
        target = content.strip()
        if target:
            if not os.path.isabs(target):
                target = os.path.join(git_dir, target)
            return os.path.normpath(target)
    return git_dir


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


def _packed_ref(git_dir: str, ref: str) -> str | None:
    content = _read_small_text(os.path.join(git_dir, "packed-refs"), "packed-refs")
    if content is None:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("^"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2 and parts[1] == ref and _looks_like_sha(parts[0]):
            return parts[0]
    return None


def _read_ref(git_dir: str, ref: str) -> str | None:
    """Read a ref's 40-char sha from its loose file, else ``packed-refs``."""
    if not ref.startswith("refs/") or ".." in ref.split("/"):
        log.debug("build identity: refusing odd ref %r", ref)
        return None
    root = os.path.normpath(git_dir)
    ref_path = os.path.normpath(os.path.join(root, ref))
    try:
        inside = os.path.commonpath([root, ref_path]) == root
    except ValueError:
        inside = False
    if not inside:
        log.debug("build identity: ref escapes git dir: %r", ref)
        return None
    loose = _read_small_text(ref_path, "ref")
    if loose is not None:
        sha = loose.strip()
        if _looks_like_sha(sha):
            return sha
        log.debug("build identity: invalid loose ref %r", ref)
    packed = _packed_ref(git_dir, ref)
    if packed is None:
        log.debug("build identity: ref not found: %r", ref)
    return packed


def _read_head(git_dir: str) -> tuple[str, str]:
    """Return (short_sha, branch) for HEAD; ``unknown`` on any failure."""
    content = _read_small_text(os.path.join(git_dir, "HEAD"), "HEAD")
    if content is None:
        return "unknown", "unknown"
    value = content.strip()
    if not value:
        return "unknown", "unknown"
    if value.startswith("ref:"):
        ref = value.split(":", 1)[1].strip()
        sha = _read_ref(_common_git_dir(git_dir), ref)
        if sha is None:
            return "unknown", "unknown"
        branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        return sha[:7], branch
    if _looks_like_sha(value):
        return value[:7], "detached"
    log.debug("build identity: unrecognised HEAD contents")
    return "unknown", "unknown"


def _log_build_identity() -> None:
    """Log the running checkout/wheel identity (diagnostics only; never raises)."""
    package_file = globals().get("__file__", "unknown")
    package_version = globals().get("__version__", "unknown")
    head = branch = "unknown"
    try:
        git_dir = _resolve_git_dir(package_file)
        if git_dir is not None:
            head, branch = _read_head(git_dir)
    except Exception as exc:
        # No repository (PCM/wheel), unreadable/oversize metadata, odd refs.
        log.debug("build identity: failed: %s", exc)
        head = branch = "unknown"
    log.info(
        "plugin: build file=%s version=%s head=%s branch=%s",
        package_file,
        package_version,
        head,
        branch,
    )


_setup_plugin_logging()
_log_build_identity()

# ---------------------------------------------------------------------------
# Graceful import guard — pcbnew is only available inside KiCad
# ---------------------------------------------------------------------------
try:
    import pcbnew as _pcbnew  # noqa: F401

    _IN_KICAD = True
    _ActionPluginBase = _pcbnew.ActionPlugin
except ImportError:
    _pcbnew = None  # type: ignore[assignment]
    _IN_KICAD = False
    _ActionPluginBase = object
    log.warning("pcbnew not available — plugin running outside KiCad (dev mode)")

try:
    import wx  # noqa: F401  (wxPython, bundled with KiCad)

    _WX_AVAILABLE = True
except ImportError:
    _WX_AVAILABLE = False
    log.warning("wx not available — plugin running outside KiCad (dev mode)")


def _plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


class KiCadAIPlugin(_ActionPluginBase):
    """KiCad action plugin that opens the AI assistant panel."""

    def __init__(self) -> None:
        if _IN_KICAD:
            super().__init__()
        self._panel = None  # wx.Frame, created on first run

    def defaults(self) -> None:
        """Called by KiCad to populate plugin metadata."""
        self.name = "KiCad AI Assistant"
        self.category = "AI"
        self.description = "LLM-powered schematic editing assistant via kcaa"
        # Path to the 24×24 PNG icon (optional)
        icon_path = os.path.join(_plugin_dir(), "resources", "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path

    def Run(self) -> None:  # noqa: N802  (KiCad convention: capital R)
        """Entry point called when the engineer clicks the plugin menu item."""
        if not _WX_AVAILABLE:
            log.error("wx not available — cannot open panel")
            return

        # Guard against destroyed wx C++ object (e.g. after KiCad restart)
        panel_alive = False
        if self._panel is not None:
            try:
                panel_alive = True
                if self._panel.IsShown():
                    self._panel.Raise()
                    return
                else:
                    # Re-use the existing panel (and its ServerManager) — don't
                    # spawn a new server process on every re-open.
                    self._panel.Show()
                    self._panel.Raise()
                    return
            except RuntimeError:
                # Underlying C++ object was destroyed
                self._panel = None
                panel_alive = False

        if not panel_alive:
            from .server_manager import ServerManager
            from .settings import PluginSettings
            from .ui.panel import AssistantPanel

            settings = PluginSettings.load()
            server_mgr = ServerManager(settings)

            self._panel = AssistantPanel(None, server_mgr=server_mgr, settings=settings)
            self._panel.Show()
            self._panel.Raise()

    def register(self) -> None:
        """Register the plugin with KiCad's action plugin system."""
        if not _IN_KICAD:
            return
        try:
            super().register()
            log.info("KiCad AI Assistant plugin registered")
        except Exception as e:
            log.error("Failed to register plugin: %s", e)


# ---------------------------------------------------------------------------
# KiCad auto-discovery: instantiate and register when the module is loaded
# ---------------------------------------------------------------------------
plugin = KiCadAIPlugin()
plugin.register()
