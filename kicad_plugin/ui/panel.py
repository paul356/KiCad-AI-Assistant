"""
AssistantPanel: the main wx.Frame for the KiCad AI Assistant plugin.

Layout:
  ┌──────────────────────────────────────────────────────────┐
  │  Conversation log (wx.TextCtrl, read-only, scrollable)   │
  ├──────────────────────────────────────────────────────────┤
  │  Tool log (collapsible wx.TextCtrl)                      │
  ├──────────────────────────────────────────────────────────┤
  │  [input field]                             [Send]        │
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
    import wx.html
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
            # Set to True when at least one tool call happens during a turn
            self._tool_calls_made: bool = False
            # Structured conversation history for HTML rendering
            self._conv_entries: list[dict] = []
            # Accumulates streamed AI text before it is finalised as an entry
            self._pending_ai_text: str = ""

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

        # Hex equivalents for HTML rendering
        _C_USER_HEX  = "#2255CC"
        _C_AI_HEX    = "#008250"
        _C_TOOL_HEX  = "#787878"
        _C_OK_HEX    = "#008C00"
        _C_WARN_HEX  = "#BE6400"
        _C_ERR_HEX   = "#BE1E1E"
        _BG_CONV_HEX = "#F5F7FC"

        def _build_ui(self) -> None:
            panel = wx.Panel(self)
            vbox = wx.BoxSizer(wx.VERTICAL)

            # ---- Splitter: conversation (top) / tool log (bottom) ----
            self._splitter = wx.SplitterWindow(
                panel, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH,
            )
            self._splitter.SetMinimumPaneSize(60)
            self._splitter.SetSashGravity(0.7)  # extra space goes to conversation

            # Conversation pane.
            self._conv_html = wx.html.HtmlWindow(
                self._splitter, style=wx.BORDER_SUNKEN,
            )
            self._conv_html.SetMinSize((-1, 120))
            self._conv_html.SetBackgroundColour(self._BG_CONV)
            self._conv_html.SetPage(f'<html><body bgcolor="{self._BG_CONV_HEX}"></body></html>')

            # Tool log pane: header row (label + show/hide toggle) + scrolled list.
            self._tool_log_panel = wx.Panel(self._splitter)
            tlp_vbox = wx.BoxSizer(wx.VERTICAL)

            tlp_header = wx.BoxSizer(wx.HORIZONTAL)
            tlp_header.Add(
                wx.StaticText(self._tool_log_panel, label="Tool Log"),
                1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 2,
            )
            self._tool_log_toggle_btn = wx.Button(
                self._tool_log_panel, label="Hide", style=wx.BU_EXACTFIT,
            )
            tlp_header.Add(self._tool_log_toggle_btn, 0, wx.ALIGN_CENTER_VERTICAL)
            tlp_vbox.Add(tlp_header, 0, wx.EXPAND | wx.BOTTOM, 2)

            # Scrolled window that holds one wx.CollapsiblePane per tool call.
            self._tool_log_scroll = wx.ScrolledWindow(
                self._tool_log_panel, style=wx.BORDER_SIMPLE | wx.VSCROLL,
            )
            self._tool_log_scroll.SetBackgroundColour(self._BG_TOOL)
            self._tool_log_scroll.SetScrollRate(0, 16)
            self._tool_log_entries_sizer = wx.BoxSizer(wx.VERTICAL)
            self._tool_log_scroll.SetSizer(self._tool_log_entries_sizer)
            tlp_vbox.Add(self._tool_log_scroll, 1, wx.EXPAND)

            self._tool_log_panel.SetSizer(tlp_vbox)

            # Cached monospace font used by per-call expanded bodies.
            self._tool_log_font = wx.Font(
                9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            )

            # Initial split: conversation gets ~70%, tool log ~30%.
            self._splitter.SplitHorizontally(
                self._conv_html, self._tool_log_panel, -200,
            )
            vbox.Add(self._splitter, 1, wx.ALL | wx.EXPAND, 4)

            if not self._settings.show_tool_log:
                # Start with tool log hidden but remember the sash for restore.
                self._tool_log_saved_sash = 200
                self._splitter.Unsplit(self._tool_log_panel)
                self._tool_log_toggle_btn.SetLabel("Show")
            else:
                self._tool_log_saved_sash = 200

            self._tool_log_toggle_btn.Bind(wx.EVT_BUTTON, self._on_toggle_tool_log)

            # ---- Input row ----
            hbox = wx.BoxSizer(wx.HORIZONTAL)

            self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
            self._input.SetHint("Ask the AI assistant…")
            hbox.Add(self._input, 1, wx.RIGHT, 6)

            self._send_btn = wx.Button(panel, label="Send")
            self._send_btn.Enable(False)
            hbox.Add(self._send_btn, 0)

            vbox.Add(hbox, 0, wx.ALL | wx.EXPAND, 4)

            # ---- Status bar (below input) ----
            status_hbox = wx.BoxSizer(wx.HORIZONTAL)
            self._status_label = wx.StaticText(panel, label="⏳ Starting backend…")
            status_hbox.Add(self._status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

            vbox.Add(status_hbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 4)

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
                self._status_label.SetLabel("❌ Backend failed to start — use Options → Restart Backend to retry")
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
            self._conv_entries.append({"type": "user", "text": text})
            self._render_conversation()
            self._busy = True
            self._send_btn.Enable(False)

            from ..context_bridge import collect_context, context_to_system_prompt_block
            ctx = collect_context()
            context_block = context_to_system_prompt_block(ctx)

            # Reset streaming state and start the flush timer
            self._stream_buffer.clear()
            self._pending_ai_text = ""
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
                self._conv_entries.append({"type": "ai", "text": reply})
                self._render_conversation()
            else:
                # Finalise the streamed text as a proper AI entry
                if self._pending_ai_text:
                    self._conv_entries.append({"type": "ai", "text": self._pending_ai_text})
                    self._pending_ai_text = ""
                    self._render_conversation()
            self._busy = False
            self._send_btn.Enable(True)
            # Auto-refresh after tool calls
            if self._tool_calls_made:
                self._auto_refresh(ctx)

        def _on_stream_flush(self, event) -> None:
            """Drain the streaming buffer into the pending AI text (main thread, timer-driven)."""
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
            self._pending_ai_text += "".join(parts)
            self._render_conversation()

        def _on_tool_call(self, name: str, args: dict, result: Any) -> None:
            ok = result.get("success", True) if isinstance(result, dict) else True
            icon = "✓" if ok else "✗"
            summary = result.get("message", "") if isinstance(result, dict) else str(result)
            self._conv_entries.append({"type": "tool", "text": f"↳ {name}  {icon} {summary}"})
            self._render_conversation()
            self._append_tool_log(name, args, result)
            self._tool_calls_made = True

        def _auto_refresh(self, ctx: dict) -> None:
            """Refresh the KiCad view automatically after tool calls."""
            editor = ctx.get("active_editor", "unknown")
            try:
                import pcbnew
                pcbnew.Refresh()
                self._conv_entries.append({"type": "status", "text": "⟳ Board view refreshed.", "color_hex": self._C_OK_HEX})
                self._render_conversation()
            except ImportError:
                pass  # outside KiCad — silently skip
            except Exception as e:
                self._conv_entries.append({"type": "status", "text": f"⚠ Auto-refresh failed: {e}", "color_hex": self._C_WARN_HEX})
                self._render_conversation()
                return
            if editor == "schematic":
                self._conv_entries.append({
                    "type": "status",
                    "text": "ℹ Schematic updated on disk — use File → Revert to see changes in the editor.",
                    "color_hex": self._C_WARN_HEX,
                })
                self._render_conversation()

        def _on_restart(self, event) -> None:
            if self._busy:
                wx.MessageBox(
                    "Please wait for the current request to finish.",
                    "Busy", wx.OK | wx.ICON_INFORMATION,
                )
                return
            self._send_btn.Enable(False)
            self._status_label.SetLabel("⏳ Restarting backend…")
            self._status_label.SetForegroundColour(wx.NullColour)
            self._conv_entries.append({"type": "status", "text": "↺ Restarting MCP backend…", "color_hex": self._C_WARN_HEX})
            self._render_conversation()

            def _do_restart():
                ok = self._server_mgr.restart()
                wx.CallAfter(self._on_restart_done, ok)

            threading.Thread(target=_do_restart, daemon=True).start()

        def _on_restart_done(self, ok: bool) -> None:
            if ok:
                self._conv_entries.append({"type": "status", "text": "✅ Backend restarted successfully.", "color_hex": self._C_OK_HEX})
                self._render_conversation()
                self._init_llm_client()
                self._on_server_started(True)
            else:
                self._conv_entries.append({"type": "status", "text": "❌ Backend failed to restart.", "color_hex": self._C_ERR_HEX})
                self._render_conversation()
                self._on_server_started(False)


        def _on_pane_changed(self, event) -> None:
            # Per-entry tool-log panes use this; just relayout, don't Fit().
            self.Layout()

        def _on_toggle_tool_log(self, _event) -> None:
            """Show/hide the tool-log pane in the splitter."""
            if self._splitter.IsSplit():
                # Remember current sash so we can restore it.
                total = self._splitter.GetClientSize().GetHeight()
                self._tool_log_saved_sash = max(
                    60, total - self._splitter.GetSashPosition()
                )
                self._splitter.Unsplit(self._tool_log_panel)
                self._tool_log_toggle_btn.SetLabel("Show")
                self._settings.show_tool_log = False
            else:
                sash = -max(60, getattr(self, "_tool_log_saved_sash", 200))
                self._splitter.SplitHorizontally(
                    self._conv_html, self._tool_log_panel, sash,
                )
                self._tool_log_toggle_btn.SetLabel("Hide")
                self._settings.show_tool_log = True
            try:
                self._settings.save()
            except Exception:
                pass

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
            self._conv_entries.clear()
            self._pending_ai_text = ""
            self._render_conversation()
            self._clear_tool_log()
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
        # Rendering helpers
        # ------------------------------------------------------------------ #

        @staticmethod
        def _md_to_html(text: str) -> str:
            """Convert markdown text to HTML.

            Uses the ``markdown`` package when available; otherwise falls back
            to a built-in converter that handles the most common syntax:
            ATX headings, bold/italic/code spans, pipe tables, and unordered lists.
            """
            try:
                import markdown
                return markdown.markdown(text, extensions=["tables", "fenced_code"])
            except ImportError:
                pass

            import html as _h
            import re

            def _inline(s: str) -> str:
                s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
                s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
                s = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", s)
                return s

            lines = text.split("\n")
            out: list[str] = []
            in_list = False
            i = 0

            while i < len(lines):
                line = lines[i]

                # Pipe table: header row followed by a separator row  |---|---|
                if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
                    if in_list:
                        out.append("</ul>")
                        in_list = False
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    out.append('<table border="1" cellpadding="4" cellspacing="0"><tr>')
                    for c in cells:
                        out.append(f"<th>{_inline(_h.escape(c))}</th>")
                    out.append("</tr>")
                    i += 2  # skip separator row
                    while i < len(lines) and "|" in lines[i] and lines[i].strip():
                        row_cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                        out.append("<tr>")
                        for c in row_cells:
                            out.append(f"<td>{_inline(_h.escape(c))}</td>")
                        out.append("</tr>")
                        i += 1
                    out.append("</table>")
                    continue

                # ATX headings  # … ######
                hm = re.match(r"^(#{1,6})\s+(.*)", line)
                if hm:
                    if in_list:
                        out.append("</ul>")
                        in_list = False
                    level = len(hm.group(1))
                    out.append(f"<h{level}>{_inline(_h.escape(hm.group(2)))}</h{level}>")
                    i += 1
                    continue

                # Unordered list items  - … or * …
                lm = re.match(r"^\s*[-*]\s+(.*)", line)
                if lm:
                    if not in_list:
                        out.append("<ul>")
                        in_list = True
                    out.append(f"<li>{_inline(_h.escape(lm.group(1)))}</li>")
                    i += 1
                    continue

                if in_list:
                    out.append("</ul>")
                    in_list = False

                if not line.strip():
                    out.append("<p>")
                else:
                    out.append(_inline(_h.escape(line)) + "<br>")
                i += 1

            if in_list:
                out.append("</ul>")

            return "".join(out)

        def _render_conversation(self) -> None:
            """Re-render the full conversation as HTML and update the HtmlWindow."""
            import html as _h

            parts = [
                f'<html><body style="font-family: Arial, sans-serif; font-size: 10pt;"'
                f' bgcolor="{self._BG_CONV_HEX}">'
            ]

            def _msg_block(sender: str, sender_color: str, bg_color: str, body_html: str) -> str:
                return (
                    f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{bg_color}"'
                    f' border="0"><tr><td>'
                    f'<b><font color="{sender_color}" size="3">{sender}</font></b>'
                    f'<br>{body_html}'
                    f'</td></tr></table>'
                    f'<br>'
                )

            for entry in self._conv_entries:
                typ = entry["type"]
                text = entry["text"]
                if typ == "user":
                    body = _h.escape(text).replace("\n", "<br>")
                    parts.append(_msg_block("You", self._C_USER_HEX, "#EBF0FF", body))
                elif typ == "ai":
                    body = self._md_to_html(text)
                    parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body))
                elif typ == "tool":
                    escaped = _h.escape(text)
                    parts.append(
                        f'<p style="margin: 2px 8px;"><font color="{self._C_TOOL_HEX}"><i>{escaped}</i></font></p>'
                    )
                elif typ == "status":
                    color = entry.get("color_hex", "#1E1E1E")
                    escaped = _h.escape(text)
                    parts.append(f'<p style="margin: 2px 8px;"><font color="{color}">{escaped}</font></p>')

            # Show pending streamed AI text
            if self._pending_ai_text:
                body = self._md_to_html(self._pending_ai_text)
                parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body))

            parts.append("</body></html>")
            self._conv_html.SetPage("".join(parts))
            self._conv_html.Scroll(0, self._conv_html.GetScrollRange(wx.VERTICAL))

        def _clear_tool_log(self) -> None:
            """Destroy all per-call collapsible entries in the tool log."""
            self._tool_log_entries_sizer.Clear(delete_windows=True)
            self._tool_log_scroll.FitInside()
            self._tool_log_scroll.Layout()

        @staticmethod
        def _json_dump_safe(value: Any, *, indent: int | None = None) -> str:
            import json as _json
            try:
                if indent is None:
                    return _json.dumps(value, separators=(',', ':'), default=str)
                return _json.dumps(value, indent=indent, default=str)
            except (TypeError, ValueError):
                return repr(value)

        def _append_tool_log(self, name: str, args: dict, result: Any) -> None:
            """Append a collapsible entry for one tool call.

            The header (always visible) shows a one-line truncated summary.
            The full one-line summary is also placed at the top of the
            expanded body and as the entry's tooltip, so users can read or
            copy it even if the native pane label is OS-truncated.
            """
            args_compact = self._json_dump_safe(args)
            result_compact = self._json_dump_safe(result)
            summary = f"{name}({args_compact}) → {result_compact}"

            # Native CollapsiblePane labels are single-line; cap to keep the
            # header tidy. The full text is available in body + tooltip.
            header = f"{name}({args_compact[:80]}) → {result_compact[:120]}"
            if len(header) > 200:
                header = header[:197] + "…"

            entry_pane = wx.CollapsiblePane(
                self._tool_log_scroll, label=header,
                style=wx.CP_DEFAULT_STYLE | wx.CP_NO_TLW_RESIZE,
            )
            try:
                entry_pane.SetToolTip(summary)
            except Exception:
                pass
            inner = entry_pane.GetPane()
            inner_sizer = wx.BoxSizer(wx.VERTICAL)

            body_text = (
                f"# {summary}\n\n"
                f"args:\n{self._json_dump_safe(args, indent=2)}\n\n"
                f"result:\n{self._json_dump_safe(result, indent=2)}"
            )
            body = wx.TextCtrl(
                inner, value=body_text,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.BORDER_SIMPLE | wx.HSCROLL,
            )
            body.SetFont(self._tool_log_font)
            body.SetBackgroundColour(self._BG_TOOL)
            # Size the body to fit its content so expanding the entry actually
            # reveals more of the result. Clamp between a small minimum and a
            # generous maximum so very long outputs still scroll within the
            # entry instead of dominating the panel.
            line_h = body.GetCharHeight()
            n_lines = body_text.count("\n") + 1
            # +2 for a little breathing room (top/bottom padding).
            desired_h = (n_lines + 2) * line_h
            min_h, max_h = 80, 600
            body.SetMinSize((-1, max(min_h, min(desired_h, max_h))))
            inner_sizer.Add(body, 1, wx.EXPAND | wx.ALL, 2)
            inner.SetSizer(inner_sizer)

            def _on_entry_toggle(_evt, _scroll=self._tool_log_scroll) -> None:
                _scroll.FitInside()
                _scroll.Layout()

            entry_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, _on_entry_toggle)

            self._tool_log_entries_sizer.Add(entry_pane, 0, wx.EXPAND | wx.ALL, 2)
            self._tool_log_scroll.FitInside()
            self._tool_log_scroll.Layout()
            # Auto-scroll to the newest entry.
            _, vunit = self._tool_log_scroll.GetScrollPixelsPerUnit()
            virt_h = self._tool_log_scroll.GetVirtualSize().GetHeight()
            if vunit:
                self._tool_log_scroll.Scroll(-1, virt_h // vunit)


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
