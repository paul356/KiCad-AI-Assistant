"""
KiCad Plugin: AI Assistant powered by the kicad-mcp MCP server.

This is the KiCad action plugin entry point. KiCad discovers this file via
the plugin directory and calls KiCadAIPlugin().register() at startup.

Usage:
  Copy or symlink this directory into KiCad's plugin search path:
    - Linux:   ~/.local/share/kicad/<ver>/scripting/plugins/kicad_ai_plugin/
    - macOS:   ~/Library/Preferences/kicad/<ver>/scripting/plugins/kicad_ai_plugin/
    - Windows: %APPDATA%\\kicad\\<ver>\\scripting\\plugins\\kicad_ai_plugin\\
"""
import os
import sys
import logging

log = logging.getLogger(__name__)

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
        self.description = "LLM-powered schematic editing assistant via kicad-mcp"
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
            from .ui.panel import AssistantPanel
            from .server_manager import ServerManager
            from .settings import PluginSettings

            settings = PluginSettings.load()
            server_mgr = ServerManager(settings)

            parent = None
            if _IN_KICAD:
                try:
                    parent = _pcbnew.GetCurrentFrame()
                except Exception:
                    pass

            self._panel = AssistantPanel(parent, server_mgr=server_mgr, settings=settings)
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
