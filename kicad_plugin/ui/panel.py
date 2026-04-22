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

import collections
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
            # Thread-safe buffer for streamed text chunks; drained by _stream_timer
            self._stream_buffer: collections.deque = collections.deque()
            self._stream_header_shown: bool = False
            # Set to True when at least one tool call happens during a turn
            self._tool_calls_made: bool = False

            self._build_ui()
            self._start_server()
            self.Centre()

        # ------------------------------------------------------------------ #
        # UI Construction
        # ------------------------------------------------------------------ #

        # Colour palette (RGB tuples) — centralised for easy theming
        _C_USER   = (34,  85, 204)   # Blue    – "You:" prefix
        _C_AI     = (0,  130,  80)   # Green   – "AI:" prefix
        _C_TOOL   = (120, 120, 120)  # Grey    – tool-call lines
        _C_OK     = (0,  140,  0)    # Green   – success notices
        _C_WARN   = (190, 100,  0)   # Amber   – warnings
        _C_ERR    = (190,  30,  30)  # Red     – errors
        _BG_CONV  = wx.Colour(245, 247, 252)  # Very light blue-grey conversation bg
        _BG_TOOL  = wx.Colour(250, 248, 240)  # Warm off-white tool-log bg

        def _build_ui(self) -> None:
            panel = wx.Panel(self)
            vbox = wx.BoxSizer(wx.VERTICAL)

            # ---- Status bar (label + Reload + Restart on same row) ----
            status_hbox = wx.BoxSizer(wx.HORIZONTAL)
            self._status_label = wx.StaticText(panel, label="⏳ Starting backend…")
            status_hbox.Add(self._status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

            self._reload_btn = wx.Button(panel, label="⟳ Reload", style=wx.BU_EXACTFIT)
            self._reload_btn.SetToolTip("Reload the schematic view")
            self._reload_btn.Enable(False)
            status_hbox.Add(self._reload_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)

            self._restart_btn = wx.Button(panel, label="↺ Restart", style=wx.BU_EXACTFIT)
            self._restart_btn.SetToolTip("Restart the MCP backend server")
            status_hbox.Add(self._restart_btn, 0, wx.ALIGN_CENTER_VERTICAL)

            vbox.Add(status_hbox, 0, wx.ALL | wx.EXPAND, 4)

            # ---- Conversation log ----
            conv_label = wx.StaticText(panel, label="Conversation")
            conv_font = conv_label.GetFont()
            conv_font.SetWeight(wx.FONTWEIGHT_BOLD)
            conv_label.SetFont(conv_font)
            vbox.Add(conv_label, 0, wx.LEFT | wx.TOP, 6)

            self._conv_log = wx.TextCtrl(
                panel,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.BORDER_SUNKEN,
            )
            self._conv_log.SetMinSize((-1, 300))
            self._conv_log.SetBackgroundColour(self._BG_CONV)
            # Use a slightly larger, more readable font
            chat_font = wx.Font(
                10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            )
            self._conv_log.SetFont(chat_font)
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
            self._tool_log.SetBackgroundColour(self._BG_TOOL)
            tool_log_font = wx.Font(
                9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            )
            self._tool_log.SetFont(tool_log_font)
            tool_sizer.Add(self._tool_log, 1, wx.EXPAND)
            tool_pane_win.SetSizer(tool_sizer)
            if self._settings.show_tool_log:
                self._tool_log_pane.Expand()
            vbox.Add(self._tool_log_pane, 0, wx.ALL | wx.EXPAND, 4)
            self._tool_log_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self._on_pane_changed)

            # ---- Bottom bar ----
            hbox = wx.BoxSizer(wx.HORIZONTAL)

            self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
            self._input.SetHint("Ask the AI assistant…")
            hbox.Add(self._input, 1, wx.RIGHT, 6)

            self._send_btn = wx.Button(panel, label="Send")
            self._send_btn.Enable(False)
            hbox.Add(self._send_btn, 0)

            vbox.Add(hbox, 0, wx.ALL | wx.EXPAND, 4)

            panel.SetSizer(vbox)

            # ---- Menu bar ----
            menu_bar = wx.MenuBar()
            m = wx.Menu()
            m.Append(wx.ID_PREFERENCES, "&Settings\tCtrl+,")
            m.Append(wx.ID_CLEAR, "Clear Conversation")
            self._menu_restart_id = wx.NewIdRef()
            m.Append(self._menu_restart_id, "Restart Backend")
            menu_bar.Append(m, "&Options")
            self.SetMenuBar(menu_bar)

            # ---- Events ----
            self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
            self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
            self._reload_btn.Bind(wx.EVT_BUTTON, self._on_reload)
            self._restart_btn.Bind(wx.EVT_BUTTON, self._on_restart)
            self.Bind(wx.EVT_MENU, self._on_settings, id=wx.ID_PREFERENCES)
            self.Bind(wx.EVT_MENU, self._on_clear, id=wx.ID_CLEAR)
            self.Bind(wx.EVT_MENU, self._on_restart, id=self._menu_restart_id)
            self.Bind(wx.EVT_CLOSE, self._on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

            # Timer that drains the streaming text buffer at ~20 fps
            self._stream_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_stream_flush, self._stream_timer)

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
                self._status_label.SetForegroundColour(wx.Colour(*self._C_OK))
                self._send_btn.Enable(True)
                self._init_llm_client()
            else:
                self._status_label.SetLabel("❌ Backend failed to start — use ↺ Restart to retry")
                self._status_label.SetForegroundColour(wx.Colour(*self._C_ERR))
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
            self._append_conv("You: ", bold=True, color=self._C_USER)
            self._append_conv(f"{text}\n")
            self._busy = True
            self._send_btn.Enable(False)

            from ..context_bridge import collect_context, context_to_system_prompt_block
            ctx = collect_context()
            context_block = context_to_system_prompt_block(ctx)

            # Reset streaming state and start the flush timer
            self._stream_buffer.clear()
            self._stream_header_shown = False
            self._tool_calls_made = False
            self._stream_timer.Start(50)  # flush every 50 ms → ~20 fps

            state = {"ai_turn_started": False}

            def _on_delta(chunk: str) -> None:
                # Called from background thread — just push to buffer; timer handles UI
                state["ai_turn_started"] = True
                self._stream_buffer.append(chunk)

            def _run():
                try:
                    reply = self._llm_client.run(
                        text,
                        context_block,
                        on_tool_call=lambda name, args, result: wx.CallAfter(
                            self._on_tool_call, name, args, result
                        ),
                        on_text_delta=_on_delta,
                    )
                except Exception as e:
                    log.exception("LLM request failed")
                    reply = f"[Error] {e}"
                wx.CallAfter(self._on_reply, reply, ctx, was_streamed=state["ai_turn_started"])

            threading.Thread(target=_run, daemon=True).start()

        def _on_reply(self, reply: str, ctx: dict, was_streamed: bool = False) -> None:
            # Stop the flush timer and drain any remaining chunks
            self._stream_timer.Stop()
            self._on_stream_flush(None)

            if not was_streamed:
                self._append_conv("AI: ", color=self._C_AI, bold=True)
                self._append_conv(f"{reply}\n\n")
            else:
                self._append_conv("\n\n")  # close the streamed text
            self._busy = False
            self._send_btn.Enable(True)
            # Enable reload button if schematic was mentioned in context
            if ctx.get("active_schematic"):
                self._reload_btn.Enable(True)
            # Auto-refresh after tool calls
            if self._tool_calls_made:
                self._auto_refresh(ctx)

        def _on_stream_flush(self, event) -> None:
            """Drain the streaming buffer into the conversation log (main thread, timer-driven)."""
            if not self._stream_buffer:
                return
            parts = []
            while self._stream_buffer:
                try:
                    parts.append(self._stream_buffer.popleft())
                except IndexError:
                    break
            if not parts:
                return
            if not self._stream_header_shown:
                self._stream_header_shown = True
                self._append_conv("AI: ", color=self._C_AI, bold=True)
            self._append_conv("".join(parts))

        def _on_tool_call(self, name: str, args: dict, result: Any) -> None:
            ok = result.get("success", True) if isinstance(result, dict) else True
            icon = "✓" if ok else "✗"
            summary = result.get("message", "") if isinstance(result, dict) else str(result)
            self._append_conv(f"  ↳ {name}  {icon} {summary}\n", color=self._C_TOOL, italic=True)
            self._append_tool_log(name, args, result)
            self._tool_calls_made = True

        def _auto_refresh(self, ctx: dict) -> None:
            """Refresh the KiCad view automatically after tool calls."""
            editor = ctx.get("active_editor", "unknown")
            try:
                import pcbnew
                pcbnew.Refresh()
                self._append_conv("⟳ Board view refreshed.\n", color=self._C_OK)
            except ImportError:
                pass  # outside KiCad — silently skip
            except Exception as e:
                self._append_conv(f"⚠ Auto-refresh failed: {e}\n", color=self._C_WARN)
                return
            if editor == "schematic":
                self._append_conv(
                    "ℹ Schematic updated on disk — use File → Revert to see changes in the editor.\n",
                    color=self._C_WARN,
                )

        def _on_reload(self, event) -> None:
            ctx = {"active_editor": "pcb"}  # manual reload always attempts pcbnew.Refresh
            try:
                import pcbnew
                pcbnew.Refresh()
                self._append_conv("⟳ Board view refreshed.\n", color=self._C_OK)
            except ImportError:
                self._append_conv(
                    "⚠ Reload not available in schematic editor. Press F5 to refresh.\n",
                    color=self._C_WARN,
                )
            except Exception as e:
                self._append_conv(f"⚠ Reload failed: {e}\n", color=self._C_ERR)

        def _on_restart(self, event) -> None:
            if self._busy:
                wx.MessageBox(
                    "Please wait for the current request to finish.",
                    "Busy", wx.OK | wx.ICON_INFORMATION,
                )
                return
            self._restart_btn.Enable(False)
            self._send_btn.Enable(False)
            self._status_label.SetLabel("⏳ Restarting backend…")
            self._status_label.SetForegroundColour(wx.NullColour)
            self._append_conv("↺ Restarting MCP backend…\n", color=self._C_WARN)

            def _do_restart():
                ok = self._server_mgr.restart()
                wx.CallAfter(self._on_restart_done, ok)

            threading.Thread(target=_do_restart, daemon=True).start()

        def _on_restart_done(self, ok: bool) -> None:
            self._restart_btn.Enable(True)
            if ok:
                self._append_conv("✅ Backend restarted successfully.\n\n", color=self._C_OK)
                self._init_llm_client()
                self._on_server_started(True)
            else:
                self._append_conv("❌ Backend failed to restart.\n\n", color=self._C_ERR)
                self._on_server_started(False)


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
            self._stream_timer.Stop()
            self._stream_buffer.clear()
            self._stream_header_shown = False
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
            italic: bool = False,
            color: tuple = (30, 30, 30),
        ) -> None:
            attr = wx.TextAttr(wx.Colour(*color))
            if bold:
                attr.SetFontWeight(wx.FONTWEIGHT_BOLD)
            if italic:
                attr.SetFontStyle(wx.FONTSTYLE_ITALIC)
            self._conv_log.SetDefaultStyle(attr)
            self._conv_log.AppendText(text)
            self._conv_log.SetDefaultStyle(wx.TextAttr(wx.Colour(30, 30, 30)))

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
