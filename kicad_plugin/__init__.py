"""
KiCad Plugin: AI Assistant powered by the kcaa MCP server.

This is the KiCad action plugin entry point. KiCad discovers this file via
the plugin directory and calls KiCadAIPlugin().register() at startup.

Usage:
  Copy or symlink this directory into KiCad's plugin search path:
    - macOS:   ~/Library/Preferences/kicad/<ver>/scripting/plugins/kicad_ai_assistant/
    - Linux:   ~/.local/share/kicad/<ver>/scripting/plugins/kicad_ai_assistant/
    - Windows: %APPDATA%\\kicad\\<ver>\\scripting\\plugins\\kicad_ai_assistant\\
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


_setup_plugin_logging()

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
