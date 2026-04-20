"""
Settings dialog: lets the engineer configure the LLM provider and API key.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    import wx
    _WX_AVAILABLE = True
except ImportError:
    _WX_AVAILABLE = False


if _WX_AVAILABLE:
    class SettingsDialog(wx.Dialog):
        """Simple dialog for editing plugin settings."""

        _PROVIDERS = ["openai", "anthropic", "custom"]

        def __init__(self, parent, settings) -> None:
            super().__init__(parent, title="AI Assistant Settings", size=(420, 320))
            self._settings = settings
            self._build_ui()

        def _build_ui(self) -> None:
            vbox = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            grid.AddGrowableCol(1, 1)

            # Provider
            grid.Add(wx.StaticText(self, label="LLM Provider:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._provider = wx.Choice(self, choices=self._PROVIDERS)
            idx = self._PROVIDERS.index(self._settings.llm_provider) if self._settings.llm_provider in self._PROVIDERS else 0
            self._provider.SetSelection(idx)
            grid.Add(self._provider, 1, wx.EXPAND)

            # API Key
            grid.Add(wx.StaticText(self, label="API Key:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._api_key = wx.TextCtrl(self, value=self._settings.llm_api_key, style=wx.TE_PASSWORD)
            grid.Add(self._api_key, 1, wx.EXPAND)

            # Model
            grid.Add(wx.StaticText(self, label="Model:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._model = wx.TextCtrl(self, value=self._settings.llm_model)
            grid.Add(self._model, 1, wx.EXPAND)

            # Custom base URL
            grid.Add(wx.StaticText(self, label="Custom endpoint URL:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._base_url = wx.TextCtrl(self, value=self._settings.llm_base_url)
            grid.Add(self._base_url, 1, wx.EXPAND)

            # Server port
            grid.Add(wx.StaticText(self, label="Server port (0=auto):"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._port = wx.SpinCtrl(self, value=str(self._settings.server_port), min=0, max=65535)
            grid.Add(self._port, 1, wx.EXPAND)

            # Show tool log
            grid.Add(wx.StaticText(self, label="Show tool log by default:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._show_tool_log = wx.CheckBox(self)
            self._show_tool_log.SetValue(self._settings.show_tool_log)
            grid.Add(self._show_tool_log, 1)

            vbox.Add(grid, 1, wx.ALL | wx.EXPAND, 10)

            # Buttons
            btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            vbox.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, 8)

            self.SetSizer(vbox)
            self.Layout()

        def apply_to(self, settings) -> None:
            """Write dialog values back to settings object."""
            settings.llm_provider = self._PROVIDERS[self._provider.GetSelection()]
            settings.llm_api_key = self._api_key.GetValue().strip()
            settings.llm_model = self._model.GetValue().strip()
            settings.llm_base_url = self._base_url.GetValue().strip()
            settings.server_port = self._port.GetValue()
            settings.show_tool_log = self._show_tool_log.GetValue()

else:
    class SettingsDialog:  # type: ignore[no-redef]
        def __init__(self, parent, settings) -> None:
            pass

        def ShowModal(self):
            return 0

        def apply_to(self, settings) -> None:
            pass

        def Destroy(self) -> None:
            pass
