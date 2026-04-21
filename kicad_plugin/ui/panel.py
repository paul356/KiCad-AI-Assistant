"""
AssistantPanel: the main wx.Frame for the KiCad AI Assistant plugin.

Layout:
  ┌──────────────────────────────────────────────────────────┐
  │  Conversation log (wx.TextCtrl, read-only, scrollable)   │
  ├──────────────────────────────────────────────────────────┤
  │  Tool log (collapsible wx.TextCtrl)                      │
  ├──────────────────────────────────────────────────────────┤
  │  [Reload Schematic]   [input field]        [Send]        │
  └──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    import wx
    _WX_AVAILABLE = True
except ImportError:
    _WX_AVAILABLE = False


if _WX_AVAILABLE:
    class AssistantPanel(wx.Frame):
        """Main floating panel for the KiCad AI Assistant."""

        def __init__(self, parent, server_mgr, settings) -> None:
            super().__init__(
                parent,
                title="KiCad AI Assistant",
                size=(520, 680),
                style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP,
            )
            self._server_mgr = server_mgr
            self._settings = settings
            self._llm_client: Optional[Any] = None
            self._busy = False

            self._build_ui()
            self._start_server()
            self.Centre()

        # ------------------------------------------------------------------ #
        # UI Construction
        # ------------------------------------------------------------------ #

        def _build_ui(self) -> None:
            panel = wx.Panel(self)
            vbox = wx.BoxSizer(wx.VERTICAL)

            # ---- Status bar ----
            self._status_label = wx.StaticText(panel, label="⏳ Starting backend…")
            vbox.Add(self._status_label, 0, wx.ALL | wx.EXPAND, 4)

            # ---- Conversation log ----
            vbox.Add(wx.StaticText(panel, label="Conversation"), 0, wx.LEFT | wx.TOP, 4)
            self._conv_log = wx.TextCtrl(
                panel,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.BORDER_SIMPLE,
            )
            self._conv_log.SetMinSize((-1, 300))
            vbox.Add(self._conv_log, 1, wx.ALL | wx.EXPAND, 4)

            # ---- Tool log (collapsible) ----
            self._tool_log_pane = wx.CollapsiblePane(panel, label="Tool Log")
            tool_pane_win = self._tool_log_pane.GetPane()
            tool_sizer = wx.BoxSizer(wx.VERTICAL)
            self._tool_log = wx.TextCtrl(
                tool_pane_win,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.BORDER_SIMPLE,
            )
            self._tool_log.SetMinSize((-1, 120))
            tool_sizer.Add(self._tool_log, 1, wx.EXPAND)
            tool_pane_win.SetSizer(tool_sizer)
            if self._settings.show_tool_log:
                self._tool_log_pane.Expand()
            vbox.Add(self._tool_log_pane, 0, wx.ALL | wx.EXPAND, 4)
            self._tool_log_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self._on_pane_changed)

            # ---- Bottom bar ----
            hbox = wx.BoxSizer(wx.HORIZONTAL)
            self._reload_btn = wx.Button(panel, label="Reload Schematic")
            self._reload_btn.Enable(False)
            hbox.Add(self._reload_btn, 0, wx.RIGHT, 6)

            self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
            hbox.Add(self._input, 1, wx.RIGHT, 6)

            self._send_btn = wx.Button(panel, label="Send")
            self._send_btn.Enable(False)
            hbox.Add(self._send_btn, 0)

            vbox.Add(hbox, 0, wx.ALL | wx.EXPAND, 4)

            panel.SetSizer(vbox)

            # ---- Settings button in title bar area ----
            menu_bar = wx.MenuBar()
            m = wx.Menu()
            m.Append(wx.ID_PREFERENCES, "&Settings\tCtrl+,")
            m.Append(wx.ID_CLEAR, "Clear Conversation")
            menu_bar.Append(m, "&Options")
            self.SetMenuBar(menu_bar)

            # ---- Events ----
            self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
            self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
            self._reload_btn.Bind(wx.EVT_BUTTON, self._on_reload)
            self.Bind(wx.EVT_MENU, self._on_settings, id=wx.ID_PREFERENCES)
            self.Bind(wx.EVT_MENU, self._on_clear, id=wx.ID_CLEAR)
            self.Bind(wx.EVT_CLOSE, self._on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        # ------------------------------------------------------------------ #
        # Server lifecycle
        # ------------------------------------------------------------------ #

        def _start_server(self) -> None:
            def _do_start():
                ok = self._server_mgr.start()
                wx.CallAfter(self._on_server_started, ok)

            t = threading.Thread(target=_do_start, daemon=True)
            t.start()

        def _on_server_started(self, ok: bool) -> None:
            if ok:
                self._status_label.SetLabel("✅ Backend ready")
                self._send_btn.Enable(True)
                self._init_llm_client()
            else:
                self._status_label.SetLabel("❌ Backend failed to start. [Options → Restart]")
            self.Layout()

        def _init_llm_client(self) -> None:
            from ..llm_client import LLMClient
            self._llm_client = LLMClient(self._settings, self._server_mgr.base_url)

        # ------------------------------------------------------------------ #
        # Event handlers
        # ------------------------------------------------------------------ #

        def _on_send(self, event) -> None:
            if self._busy or not self._llm_client:
                return
            text = self._input.GetValue().strip()
            if not text:
                return
            self._input.Clear()
            self._append_conv(f"You: {text}\n", bold=True)
            self._busy = True
            self._send_btn.Enable(False)

            from ..context_bridge import collect_context, context_to_system_prompt_block
            ctx = collect_context()
            context_block = context_to_system_prompt_block(ctx)

            def _run():
                try:
                    reply = self._llm_client.run(
                        text,
                        context_block,
                        on_tool_call=lambda name, args, result: wx.CallAfter(
                            self._on_tool_call, name, args, result
                        ),
                    )
                except Exception as e:
                    log.exception("LLM request failed")
                    reply = f"[Error] {e}"
                wx.CallAfter(self._on_reply, reply, ctx)

            threading.Thread(target=_run, daemon=True).start()

        def _on_reply(self, reply: str, ctx: dict) -> None:
            self._append_conv(f"AI: {reply}\n\n")
            self._busy = False
            self._send_btn.Enable(True)
            # Enable reload button if schematic was mentioned in context
            if ctx.get("active_schematic"):
                self._reload_btn.Enable(True)

        def _on_tool_call(self, name: str, args: dict, result: Any) -> None:
            ok = result.get("success", True) if isinstance(result, dict) else True
            icon = "✓" if ok else "✗"
            summary = result.get("message", "") if isinstance(result, dict) else str(result)
            self._append_conv(f"  ↳ {name}  {icon} {summary}\n", color=(100, 100, 100))
            self._append_tool_log(name, args, result)

        def _on_reload(self, event) -> None:
            try:
                import pcbnew
                pcbnew.Refresh()
                self._append_conv("⟳ Board view refreshed.\n", color=(0, 128, 0))
            except ImportError:
                self._append_conv(
                    "⚠ Reload not available in schematic editor. Press F5 to refresh.\n",
                    color=(200, 128, 0),
                )
            except Exception as e:
                self._append_conv(f"⚠ Reload failed: {e}\n", color=(200, 0, 0))

        def _on_pane_changed(self, event) -> None:
            self.Layout()
            self.Fit()

        def _on_settings(self, event) -> None:
            from .settings_dialog import SettingsDialog
            dlg = SettingsDialog(self, self._settings)
            if dlg.ShowModal() == wx.ID_OK:
                dlg.apply_to(self._settings)
                self._settings.save()
            dlg.Destroy()

        def _on_clear(self, event) -> None:
            if self._busy:
                wx.MessageBox(
                    "Please wait for the current request to finish.",
                    "Busy", wx.OK | wx.ICON_INFORMATION,
                )
                return
            self._conv_log.Clear()
            self._tool_log.Clear()
            if self._llm_client:
                self._llm_client.reset()

        def _on_close(self, event) -> None:
            # Check if the parent (KiCad main window) is being destroyed.
            # If so, stop the server so KiCad can exit cleanly.
            parent = self.GetParent()
            parent_closing = parent is None or not parent.IsShown()
            if parent_closing:
                self._server_mgr.stop()
                self.Destroy()
            else:
                self.Hide()  # keep server alive so re-opening is fast

        def _on_destroy(self, event) -> None:
            """Called when the wx window is actually destroyed (e.g. KiCad shutdown)."""
            if event.GetEventObject() is self:
                self._server_mgr.stop()
            event.Skip()

        # ------------------------------------------------------------------ #
        # Logging helpers
        # ------------------------------------------------------------------ #

        def _append_conv(
            self,
            text: str,
            bold: bool = False,
            color: tuple = (0, 0, 0),
        ) -> None:
            attr = wx.TextAttr(wx.Colour(*color))
            if bold:
                attr.SetFontWeight(wx.FONTWEIGHT_BOLD)
            self._conv_log.SetDefaultStyle(attr)
            self._conv_log.AppendText(text)
            self._conv_log.SetDefaultStyle(wx.TextAttr(wx.BLACK))

        def _append_tool_log(self, name: str, args: dict, result: Any) -> None:
            import json as _json
            line = f"{name}({_json.dumps(args, separators=(',', ':'))[:80]}) → {_json.dumps(result, separators=(',', ':'))[:120]}\n"
            self._tool_log.AppendText(line)


else:
    # Fallback stub when wx is not available (e.g., during unit tests or dev)
    class AssistantPanel:  # type: ignore[no-redef]
        def __init__(self, parent, server_mgr, settings) -> None:
            log.warning("AssistantPanel created without wx — UI unavailable")

        def Show(self) -> None:
            pass

        def Raise(self) -> None:
            pass

        def IsShown(self) -> bool:
            return False
