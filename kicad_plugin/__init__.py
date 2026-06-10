"""
KiCad Plugin: AI Assistant powered by the kcaa MCP server.

This is the KiCad action plugin entry point. KiCad discovers this file via
the plugin directory and calls KiCadAIPlugin().register() at startup.

Usage:
  Copy or symlink this directory into KiCad's plugin search path:
    - Linux:   ~/.local/share/kicad/<ver>/scripting/plugins/kicad_ai_plugin/
    - macOS:   ~/Library/Preferences/kicad/<ver>/scripting/plugins/kicad_ai_plugin/
    - Windows: %APPDATA%\\kicad\\<ver>\\scripting\\plugins\\kicad_ai_plugin\\
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


def _export_design_defaults() -> None:
    """Extract KiCad's default board design rules and persist them.

    Writes ``design-defaults.json`` into the kcaa data directory so the
    MCP server can read KiCad's real default values without importing pcbnew.

    Reads the bare board-level ``m_`` attributes from a fresh ``BOARD()``.
    Net-class values are not merged — they are returned separately by
    ``get_effective_design_rules`` from the ``.kicad_pro`` file.
    """
    if not _IN_KICAD:
        return
    try:
        import json as _json

        board = _pcbnew.BOARD()
        settings = board.GetDesignSettings()

        # Hardcoded mapping: sexp tag → pcbnew attribute name.
        # Each attr is validated; warnings are logged for missing attributes.
        _FIELD_MAP = {
            "min_clearance": "m_MinClearance",
            "min_groove_width": "m_MinGrooveWidth",
            "min_connection_width": "m_MinConn",
            "min_track_width": "m_TrackMinWidth",
            "min_via_annular_width": "m_ViasMinAnnularWidth",
            "min_via_size": "m_ViasMinSize",
            "min_through_drill": "m_MinThroughDrill",
            "min_microvia_size": "m_MicroViasMinSize",
            "min_microvia_drill": "m_MicroViasMinDrill",
            "copper_edge_clearance": "m_CopperEdgeClearance",
            "hole_clearance": "m_HoleClearance",
            "hole_to_hole_min": "m_HoleToHoleMin",
            "silk_clearance": "m_SilkClearance",
            "min_silk_text_height": "m_MinSilkTextHeight",
            "min_silk_text_thickness": "m_MinSilkTextThickness",
        }

        defaults = {}
        log.info("Exporting KiCad design defaults (board m_):")

        for key, attr in _FIELD_MAP.items():
            if not hasattr(settings, attr):
                log.warning(
                    "  %s: pcbnew attribute %s NOT FOUND on BOARD_DESIGN_SETTINGS — skipped",
                    key,
                    attr,
                )
                continue
            raw = getattr(settings, attr)
            mm_value = _pcbnew.ToMM(raw)
            defaults[key] = mm_value
            log.info("  %s: %s mm (board %s, raw=%s)", key, mm_value, attr, raw)

        # Resolved spokes — board-level only
        raw_spokes = settings.m_MinResolvedSpokes
        defaults["min_resolved_spokes"] = int(raw_spokes)
        log.info(
            "  min_resolved_spokes: %s (board m_MinResolvedSpokes, raw=%s)",
            int(raw_spokes),
            raw_spokes,
        )

        from .settings import _get_kcaa_data_dir

        defaults_dir = _get_kcaa_data_dir()
        defaults_path = os.path.join(defaults_dir, "design-defaults.json")
        os.makedirs(defaults_dir, exist_ok=True)
        with open(defaults_path, "w", encoding="utf-8") as f:
            _json.dump(defaults, f, indent=2)
        log.info("Exported KiCad design defaults to %s", defaults_path)
    except Exception as exc:
        log.warning("Could not export KiCad design defaults: %s", exc)


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

            parent = None
            if _IN_KICAD:
                try:
                    parent = _pcbnew.GetCurrentFrame()
                except Exception as e:
                    log.debug("Could not get KiCad parent frame: %s", e)

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
_export_design_defaults()

plugin = KiCadAIPlugin()
plugin.register()
