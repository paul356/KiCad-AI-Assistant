"""
AssistantPanel: the main wx.Frame for the KiCad AI Assistant plugin.

Layout:
  ┌──────────────────────────────────────────────────────────┐
  │  Conversation log (scrollable, tool calls folded inline) │
  ├──────────────────────────────────────────────────────────┤
  │  [input field]                             [Send]        │
  └──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import collections
import datetime
import logging
import os
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    import wx
    import wx.html
    _WX_AVAILABLE = True
except ImportError:
    _WX_AVAILABLE = False

# Try to use WebView (HTML5 + <details>/<summary> for folding).
# Falls back to wx.html.HtmlWindow when unavailable.
_WEBVIEW_AVAILABLE = False
if _WX_AVAILABLE:
    try:
        import wx.html2 as _wx_html2
        _WEBVIEW_AVAILABLE = True
    except ImportError:
        pass


if _WX_AVAILABLE:
    class AssistantPanel(wx.Frame):
        """Main floating panel for the KiCad AI Assistant."""

        def __init__(self, parent, server_mgr, settings) -> None:
            super().__init__(
                parent,
                title="KiCad AI Assistant",
                size=(520, 680),
                style=wx.DEFAULT_FRAME_STYLE,
            )
            self._server_mgr = server_mgr
            self._settings = settings
            self._llm_client: Optional[Any] = None
            self._busy = False
            # Thread-safe buffer for streamed text chunks; drained by _stream_timer
            self._stream_buffer: collections.deque = collections.deque()
            # Set to True when at least one tool call happens during a turn
            self._tool_calls_made: bool = False
            # Set to True when any tool modifies a .kicad_sch file this turn
            self._schematic_edited: bool = False
            # Set to True when any tool modifies a .kicad_pcb file this turn
            self._pcb_edited: bool = False
            # Structured conversation history for HTML rendering.
            # Each entry is one of:
            #   {"type": "user",   "text": str}
            #   {"type": "ai",     "text": str,
            #                      "tools": [{"name":str,"args":dict,"result":dict},...]}
            #   {"type": "status", "text": str, "color_hex": str}
            self._conv_entries: list[dict] = []
            # Accumulates streamed AI text before it is finalised as an entry
            self._pending_ai_text: str = ""
            # Basename of the current session file; None means no file yet.
            # _save_session_to_disk overwrites this file when set, or creates
            # a new timestamped one otherwise.
            self._current_session_file: Optional[str] = None
            # Keep the conversation pinned to the newest output while one AI
            # turn is actively streaming / appending tool results.
            self._follow_output_to_bottom: bool = False
            # True while SetPage(shell) is in-flight.  Only relevant during
            # initial shell load — subsequent updates use RunScript, not SetPage.
            self._page_loading: bool = False
            # True once the shell HTML (CSS + JS framework + empty containers)
            # has been successfully loaded into the WebView.
            self._shell_loaded: bool = False
            # True while any RunScript call is in-flight.  On Windows WebView2,
            # RunScript pumps the message loop; we guard against re-entrant
            # RunScript calls with this flag.
            self._js_running: bool = False
            # Set when a render is skipped (due to shell not loaded or
            # _js_running).  The next opportunity triggers a deferred render.
            self._render_pending: bool = False
            # True when the stream-wrapper table is visible in the shell.
            self._stream_wrapper_visible: bool = False
            # Shell load retry counter (prevents infinite retry on WebView2 failure).
            self._shell_retry_count: int = 0

            # Page-load watchdog: if _on_webview_loaded never fires (WebView2
            # glitch on Windows), reset _page_loading so renders are not
            # permanently blocked.
            self._page_watchdog = wx.Timer(self)

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

        @staticmethod
        def _build_shell_html() -> str:
            """Build the static shell HTML (CSS + JS framework + empty containers).

            This is loaded once via SetPage at startup.  All subsequent UI
            updates happen through RunScript calls to the JS functions defined
            here — SetPage is never called again (unless the shell needs to be
            reloaded after a WebView error).
            """
            import os

            # Get the path to shell.js (in the same directory as this file)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            shell_js_path = os.path.join(script_dir, "shell.js")

            # Read the JS file
            js_code = "// shell.js not found"
            try:
                with open(shell_js_path, "r", encoding="utf-8") as f:
                    js_code = f.read()
                log.info("Loaded shell.js: %d bytes", len(js_code))
                # Check first few chars
                log.info("shell.js starts with: %s", js_code[:100])
            except Exception as e:
                log.error("Failed to load shell.js: %s", e)

            return (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>"
                "body{font-family:Microsoft YaHei,Ubuntu,Noto Sans CJK SC,Noto Sans CJK,DejaVu Sans,Arial,sans-serif;font-size:11pt;font-weight:400;background:#F5F7FC;margin:4px;color:#1E1E1E}"
                "table.msg{width:100%;border-collapse:collapse;margin-bottom:8px}"
                "table.msg td{padding:8px 10px;border-radius:4px}"
                ".tool-list{margin-top:6px;font-size:9pt}"
                "details.tools{margin:2px 0}"
                "details.tools summary{cursor:pointer;color:#333;font-weight:600;user-select:none;"
                "padding:2px 4px;border-radius:3px;list-style:disclosure-closed}"
                "details.tools summary:hover{background:#e8e8e8}"
                "details.tools[open] summary{list-style:disclosure-open}"
                ".tool-entry{margin:2px 0;padding:4px 8px;background:#f5f5f0;"
                "border-left:3px solid #999;border-radius:2px;"
                "font-family:Microsoft YaHei UI,monospace;font-size:9pt;font-weight:600;white-space:pre-wrap;"
                "word-break:break-all;color:#222}"
                ".tool-ok{border-left-color:#2e7d32}"
                ".tool-err{border-left-color:#c62828}"
                "pre{font-family:Microsoft YaHei UI,monospace;white-space:pre;background:#e8e8e8;"
                "padding:8px;border-radius:4px;overflow-x:auto;font-size:9pt;font-weight:500;"
                "line-height:1.4;color:#222}"
                "pre code{background:none;padding:0;border-radius:0;font-size:inherit}"
                "code{font-family:Microsoft YaHei UI,monospace;background:#e0e0e0;"
                "padding:1px 3px;border-radius:2px;font-weight:600}"
                "</style>"
                "<script>"
                + js_code +
                "</script>"
                "</head>"
                "<body>"
                "<div id='conversation'></div>"
                "<table class='msg' id='stream-wrapper' style='display:none'>"
                "<tr><td style='background:#EBF7F2'>"
                "<b><span style='color:#008250'>AI</span></b><br>"
                "<div id='pending-ai-text'></div>"
                "</td></tr></table>"
                "</body></html>"
            )

        def _load_shell(self) -> None:
            """Load the static shell HTML into the WebView.

            Called once at startup and on WebView error recovery.  After this,
            all UI updates go through RunScript — SetPage is never called again
            (unless the shell needs to be reloaded).
            """
            shell_html = self._build_shell_html()
            log.debug("Loading shell HTML: %d bytes", len(shell_html))
            self._page_loading = True
            self._shell_loaded = False
            self._stream_wrapper_visible = False
            self._page_watchdog.Start(5000, oneShot=True)
            self._conv_view.SetPage(shell_html, "")

        def _build_ui(self) -> None:
            panel = wx.Panel(self)
            vbox = wx.BoxSizer(wx.VERTICAL)

            # ---- Conversation view (WebView when available, HtmlWindow fallback) ----
            self._use_webview = False
            if _WEBVIEW_AVAILABLE:
                try:
                    self._conv_view = _wx_html2.WebView.New(
                        panel, style=wx.BORDER_SUNKEN,
                    )
                    self._use_webview = True
                    self.Bind(
                        _wx_html2.EVT_WEBVIEW_LOADED,
                        self._on_webview_loaded,
                        self._conv_view,
                    )
                    self.Bind(
                        _wx_html2.EVT_WEBVIEW_ERROR,
                        self._on_webview_error,
                        self._conv_view,
                    )
                    # Load the static shell (CSS + JS + empty containers).
                    # After this, all UI updates go through RunScript.
                    self._load_shell()
                except Exception as e:
                    log.warning("WebView init failed, falling back to HtmlWindow: %s", e)
                    self._use_webview = False

            if not self._use_webview:
                self._conv_view = wx.html.HtmlWindow(
                    panel, style=wx.BORDER_SUNKEN,
                )
                self._conv_view.SetBackgroundColour(self._BG_CONV)
                self._conv_view.SetPage(
                    f'<html><body bgcolor="{self._BG_CONV_HEX}"></body></html>'
                )

            self._conv_view.SetMinSize((-1, 120))
            vbox.Add(self._conv_view, 1, wx.ALL | wx.EXPAND, 4)

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

            # Options menu
            m = wx.Menu()
            m.Append(wx.ID_PREFERENCES, "&Settings\tCtrl+,")
            menu_bar.Append(m, "&Options")

            # Session menu
            self._menu_new_session_id = wx.NewIdRef()
            self._menu_load_session_id = wx.NewIdRef()
            session_menu = wx.Menu()
            session_menu.Append(self._menu_new_session_id, "New Session")
            session_menu.Append(self._menu_load_session_id, "Load Session\u2026")
            menu_bar.Append(session_menu, "&Session")

            # Backend menu
            self._menu_restart_id = wx.NewIdRef()
            backend_menu = wx.Menu()
            backend_menu.Append(self._menu_restart_id, "Restart Backend")
            menu_bar.Append(backend_menu, "&Backend")

            # Tools menu
            self._menu_autoroute_id = wx.NewIdRef()
            tools_menu = wx.Menu()
            tools_menu.Append(self._menu_autoroute_id, "Auto Route\u2026")
            tools_menu.Enable(self._menu_autoroute_id, False)
            menu_bar.Append(tools_menu, "&Tools")

            self.SetMenuBar(menu_bar)

            # ---- Events ----
            self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
            self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
            self.Bind(wx.EVT_MENU, self._on_settings, id=wx.ID_PREFERENCES)
            self.Bind(wx.EVT_MENU, self._on_new_session, id=self._menu_new_session_id)
            self.Bind(wx.EVT_MENU, self._on_load_session, id=self._menu_load_session_id)
            self.Bind(wx.EVT_MENU, self._on_restart, id=self._menu_restart_id)
            self.Bind(wx.EVT_MENU, self._on_autoroute, id=self._menu_autoroute_id)
            self.Bind(wx.EVT_CLOSE, self._on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

            # Timer that drains the streaming text buffer at ~20 fps
            self._stream_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_stream_flush, self._stream_timer)

            # Suicide watchdog: when KiCad is closed, our top-level wx.Frame
            # would otherwise keep the wx event loop alive. KiCad's shutdown
            # does not reliably propagate EVT_CLOSE / EVT_WINDOW_DESTROY to
            # plugin frames, so we instead poll wx.GetTopLevelWindows(): once
            # we are the only visible top-level window left, KiCad's main
            # window is gone and we close ourselves.
            self._suicide_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_suicide_check, self._suicide_timer)
            self._suicide_timer.Start(500)  # 500 ms poll

            # Page-load watchdog: if _on_webview_loaded never fires (WebView2
            # glitch on Windows), reset _page_loading so renders are not
            # permanently blocked.
            self.Bind(wx.EVT_TIMER, self._on_page_watchdog, self._page_watchdog)

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
            try:
                if ok:
                    self._status_label.SetLabel("✅ Backend ready")
                    self._status_label.SetForegroundColour(wx.Colour(*self._C_OK))
                    self._send_btn.Enable(True)
                    self.GetMenuBar().Enable(self._menu_autoroute_id, True)
                    self._init_llm_client()
                    self._check_kicad_ipc_environment()
                    self._auto_save_pcb_on_open()
                else:
                    self._status_label.SetLabel("❌ Backend failed to start — use Options → Restart Backend to retry")
                    self._status_label.SetForegroundColour(wx.Colour(*self._C_ERR))
                self.Layout()
            except Exception as e:
                import traceback
                log.error("_on_server_started failed: %s\n%s", e, traceback.format_exc())

        def _init_llm_client(self) -> None:
            try:
                from ..llm_client import LLMClient
                self._llm_client = LLMClient(self._settings, self._server_mgr.base_url)
                self._autoload_session()
            except Exception as e:
                import traceback
                log.error("_init_llm_client failed: %s\n%s", e, traceback.format_exc())

        # ------------------------------------------------------------------ #
        # KiCad IPC API status check
        # ------------------------------------------------------------------ #

        def _check_kicad_ipc_environment(self) -> None:
            """Check that kipy is in the MCP venv and the KiCad IPC socket exists.

            The "Reload PCB" feature and the ``update_pcb_from_schematic``
            tool both depend on the kicad-python (kipy) module (installed in
            the plugin's .venv, *not* in KiCad's Python) and a live KiCad IPC
            socket (default ``/tmp/kicad/api.sock`` on Linux/macOS).
            If either is missing, surface a friendly warning in the
            conversation panel so the user knows what to enable.
            """
            import glob
            import os

            # 1) kipy presence check in the plugin's .venv -----------------
            # kipy runs inside the MCP server venv, not KiCad's Python, so
            # we must NOT do `import kipy` here.  Instead probe the venv's
            # site-packages on disk.
            plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            venv_site = os.path.join(plugin_dir, ".venv", "lib")
            # On Windows the venv layout is .venv/Lib/site-packages/ (capital
            # Lib, no python* subdirectory).  Check both layouts.
            kipy_found = bool(glob.glob(os.path.join(venv_site, "python*", "site-packages", "kipy")))
            if not kipy_found:
                win_site = os.path.join(plugin_dir, ".venv", "Lib", "site-packages", "kipy")
                kipy_found = os.path.isdir(win_site)
            kipy_ok = kipy_found
            if not kipy_ok:
                self._conv_entries.append({
                    "type": "status",
                    "text": (
                        "⚠ kicad-python (kipy) is not installed in the MCP "
                        "server's virtual environment. The 'Reload PCB' "
                        "feature and the 'update_pcb_from_schematic' tool "
                        "will be disabled. Run: "
                        f".venv/bin/pip install kicad-python  "
                        f"(looked in {venv_site})"
                    ),
                    "color_hex": self._C_WARN_HEX,
                })
                self._render_conversation()

            # 2) IPC socket check ------------------------------------------
            # This MUST run on a background thread: it connects to the KiCad
            # IPC socket and sends a ping with a 10-second timeout.  Doing
            # this on the KiCad main (UI) thread would block KiCad, which in
            # turn prevents KiCad from accepting the IPC connection — a
            # deadlock that freezes the entire application on startup.
            def _check_socket_async():
                socket_exists = False
                checked_path = "unknown"
                base_url = self._server_mgr.base_url
                if base_url:
                    try:
                        from ..llm_client import call_mcp_tool
                        result = call_mcp_tool(
                            base_url,
                            "check_kicad_ipc_connection",
                            {},
                        )
                        socket_exists = result.get("connected", False)
                        checked_path = result.get("socket_path", "unknown")
                        if not socket_exists:
                            log.debug("IPC socket check failed: %s", result.get("error", "unknown"))
                    except Exception as exc:
                        log.warning("Failed to check IPC socket via MCP tool: %s", exc)
                wx.CallAfter(self._on_ipc_socket_checked, socket_exists, checked_path)

            t = threading.Thread(target=_check_socket_async, daemon=True)
            t.start()

        def _on_ipc_socket_checked(self, socket_exists: bool, checked_path: str) -> None:
            """Callback invoked on the UI thread after the async IPC socket check."""
            if not socket_exists:
                self._conv_entries.append({
                    "type": "status",
                    "text": (
                        f"⚠ KiCad IPC API socket not available at {checked_path}. "
                        "The 'Reload PCB' feature and 'update_pcb_from_schematic' "
                        "tool require it. Enable it in KiCad: "
                        "Preferences → Preferences → Plugins → "
                        "'Enable KiCad API' (KiCad 9+)."
                    ),
                    "color_hex": self._C_WARN_HEX,
                })
                self._render_conversation()

        def _auto_save_pcb_on_open(self) -> None:
            """Auto-save the PCB when the plugin opens.

            KiCad marks the PCB editor as dirty when the plugin is opened,
            so we save it immediately to clear the dirty state.
            Uses the save_board MCP tool via IPC for proper state management.
            
            This MUST run on a background thread to avoid blocking the KiCad
            main thread, which would prevent KiCad from responding to IPC
            connections and cause a deadlock.
            """
            def _do_auto_save():
                try:
                    from ..llm_client import call_mcp_tool
                    result = call_mcp_tool(
                        self._server_mgr.base_url,
                        "save_board",
                        {},
                    )
                    if result.get("success"):
                        log.info("Auto-saved PCB on plugin open via save_board tool")
                    else:
                        log.debug("Auto-save PCB failed: %s", result.get("error", "unknown"))
                except Exception as exc:
                    log.debug("Auto-save PCB skipped or failed: %s", exc)

            t = threading.Thread(target=_do_auto_save, daemon=True)
            t.start()

        # ------------------------------------------------------------------ #
        # Event handlers
        # ------------------------------------------------------------------ #

        def _on_send(self, event) -> None:
            if self._busy or not self._llm_client:
                log.debug("_on_send: skipped (busy=%s, client=%s)", self._busy, self._llm_client is not None)
                return
            text = self._input.GetValue().strip()
            if not text:
                return
            log.info("_on_send: user message (%d chars)", len(text))
            self._input.Clear()
            self._conv_entries.append({"type": "user", "text": text, "timestamp": datetime.datetime.now().strftime("%H:%M:%S")})
            self._follow_output_to_bottom = True
            # Create the session file on the very first message so current.json
            # is established before the AI responds.
            if self._current_session_file is None:
                err = self._save_session_to_disk()
                if err:
                    log.warning("Could not create session file: %s", err)
            self._render_conversation(force_scroll_to_bottom=True)
            self._busy = True
            self._send_btn.Enable(False)

            from ..context_bridge import collect_context, context_to_system_prompt_block
            ctx = collect_context()
            context_block = context_to_system_prompt_block(ctx)

            # Reset streaming state and start the flush timer
            self._stream_buffer.clear()
            self._pending_ai_text = ""
            self._tool_calls_made = False
            self._schematic_edited = False
            self._pcb_edited = False
            self._stream_timer.Start(50)  # flush every 50 ms → ~20 fps

            state = {"ai_turn_started": False}

            def _on_delta(chunk: str) -> None:
                # Called from background thread — just push to buffer; timer handles UI
                state["ai_turn_started"] = True
                self._stream_buffer.append(chunk)

            def _run():
                log.info("Background _run: started")
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
                log.info("Background _run: finished (reply_len=%d, streamed=%s)",
                         len(reply), state["ai_turn_started"])
                wx.CallAfter(self._on_reply, reply, ctx, was_streamed=state["ai_turn_started"])

            threading.Thread(target=_run, daemon=True).start()

        def _on_reply(self, reply: str, ctx: dict, was_streamed: bool = False) -> None:
            # Stop the flush timer and drain any remaining chunks
            self._stream_timer.Stop()
            self._on_stream_flush(None)

            # Hide streaming preview before appending the final entry
            if self._use_webview:
                self._hide_stream_wrapper()

            if not was_streamed:
                entry = {"type": "ai", "text": reply, "timestamp": datetime.datetime.now().strftime("%H:%M:%S")}
                self._conv_entries.append(entry)
                if self._use_webview and self._shell_loaded:
                    self._append_entry_js(entry, force_scroll_to_bottom=True)
                else:
                    self._render_conversation(force_scroll_to_bottom=True)
            else:
                # Finalise any remaining streamed text as a proper AI entry
                if self._pending_ai_text:
                    entry = {
                        "type": "ai",
                        "text": self._pending_ai_text,
                        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    self._conv_entries.append(entry)
                    self._pending_ai_text = ""
                    if self._use_webview and self._shell_loaded:
                        self._append_entry_js(entry, force_scroll_to_bottom=True)
                    else:
                        self._render_conversation(force_scroll_to_bottom=True)
            self._busy = False
            self._send_btn.Enable(True)
            # Auto-refresh after tool calls
            if self._tool_calls_made:
                self._auto_refresh(ctx)
            self._follow_output_to_bottom = False

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

            if self._use_webview:
                # Show stream wrapper on first chunk
                if self._shell_loaded and not self._stream_wrapper_visible:
                    self._show_stream_wrapper()
                # Incremental DOM update — should always succeed with shell loaded
                if self._incremental_stream_update():
                    return
                # Shell not loaded yet — defer
                self._render_pending = True
            else:
                self._render_conversation(force_scroll_to_bottom=self._follow_output_to_bottom)

        def _incremental_stream_update(self) -> bool:
            """Update streaming AI text via _updateStream JS call.

            The #pending-ai-text div always exists in the shell HTML, so this
            should always succeed when the shell is loaded.
            """
            if not self._use_webview or not self._shell_loaded or self._js_running:
                return False

            import json as _json
            body_html = self._md_to_html(self._pending_ai_text)
            scroll_arg = "true" if self._follow_output_to_bottom else "false"
            js = f'_updateStream({_json.dumps(body_html)}, {scroll_arg})'

            self._js_running = True
            try:
                ok, result = self._conv_view.RunScript(js)
                if not ok:
                    log.warning("_incremental_stream_update: RunScript failed")
                return ok
            except Exception as e:
                log.warning("_incremental_stream_update: exception: %s", e)
                return False
            finally:
                self._js_running = False

        def _on_page_watchdog(self, event) -> None:
            """Safety net: detect shell load timeout.

            The shell is the only thing loaded via SetPage.  If it times out,
            we reset state and retry (up to 3 times).  Once the shell is
            loaded, there is no further SetPage activity, so the watchdog
            is not needed.
            """
            if not self._page_loading:
                return
            self._shell_retry_count += 1
            self._page_loading = False
            self._js_running = False
            self._shell_loaded = False
            self._stream_wrapper_visible = False
            self._render_pending = False

            if self._shell_retry_count > 3:
                log.error("WebView shell failed to load after %d retries — giving up",
                          self._shell_retry_count)
                self._conv_entries.append({
                    "type": "status",
                    "text": (
                        "⚠ WebView failed to initialize after multiple retries. "
                        "The chat panel may not work. Try restarting KiCad."
                    ),
                    "color_hex": self._C_ERR_HEX,
                })
                return

            log.warning("WebView watchdog: shell load timed out (>5s), retry %d/3",
                        self._shell_retry_count)
            self._conv_entries.append({
                "type": "status",
                "text": (
                    f"⚠ WebView initialization timed out (>5s). "
                    f"Retrying ({self._shell_retry_count}/3)…"
                ),
                "color_hex": self._C_WARN_HEX,
            })
            wx.CallAfter(self._load_shell)

        def _on_tool_call(self, name: str, args: dict, result: Any) -> None:
            log.info("_on_tool_call: %s (shell_loaded=%s, entries=%d)",
                     name, self._shell_loaded, len(self._conv_entries))
            # If there is pending streamed text that preceded this tool call,
            # finalise it as an AI entry now so the timeline order is correct.
            if self._pending_ai_text:
                self._conv_entries.append({"type": "ai", "text": self._pending_ai_text})
                self._pending_ai_text = ""
            # Append as a permanent timeline entry so tool calls appear in
            # chronological order alongside user and AI messages.
            # Store full data — truncation for UI display happens at render time
            # to preserve session data integrity.
            self._conv_entries.append({
                "type": "tool_call",
                "name": name,
                "args": args,
                "result": result,
            })
            # Full update needed to clear pending-ai-text and show new entries
            self._render_conversation(force_scroll_to_bottom=self._follow_output_to_bottom)
            self._tool_calls_made = True
            # Use tool_registry to determine if tool modified PCB/schematic files
            try:
                from ..tool_registry import get_tool_policy
                policy = get_tool_policy(name)
            except Exception as e:
                log.error("Failed to get tool policy for %s: %s", name, e)
                policy = None

            if policy and policy.path_arg == "pcb_path" and policy.mark_dirty:
                self._pcb_edited = True
            elif policy and policy.path_arg == "schematic_path" and policy.mark_dirty:
                self._schematic_edited = True

        def _auto_refresh(self, ctx: dict) -> None:
            """Refresh the KiCad view automatically after tool calls."""
            editor = ctx.get("active_editor", "unknown")
            if self._pcb_edited:
                try:
                    import pcbnew
                    pcbnew.Refresh()
                    self._conv_entries.append({"type": "status", "text": "⟳ Board view refreshed.", "color_hex": self._C_OK_HEX})
                    self._render_conversation(force_scroll_to_bottom=self._follow_output_to_bottom)
                except ImportError:
                    pass  # outside KiCad — silently skip
                except Exception as e:
                    self._conv_entries.append({"type": "status", "text": f"⚠ Auto-refresh failed: {e}", "color_hex": self._C_WARN_HEX})
                    self._render_conversation(force_scroll_to_bottom=self._follow_output_to_bottom)
                    return
            if self._schematic_edited:
                self._conv_entries.append({
                    "type": "status",
                    "text": "ℹ Schematic updated on disk — use File → Revert in the Schematic Editor to see the changes.",
                    "color_hex": self._C_WARN_HEX,
                })
                self._render_conversation(force_scroll_to_bottom=self._follow_output_to_bottom)

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


        def _on_autoroute(self, event) -> None:
            """Menu handler: Tools → Auto Route…

            All pcbnew calls (ExportSpecctraDSN, ImportSpecctraSES, Refresh)
            must happen on the wx main thread to avoid corruption of the board
            view.  Only the FreeRouting subprocess runs in a background thread.
            """
            if self._busy:
                wx.MessageBox(
                    "Please wait for the current AI request to finish.",
                    "Busy", wx.OK | wx.ICON_INFORMATION,
                )
                return

            # ---- 1. Get board (main thread) ----
            try:
                import pcbnew as _pcbnew
                board = _pcbnew.GetBoard()
            except Exception:
                board = None

            if board is None:
                self._conv_entries.append({
                    "type": "status",
                    "text": "⚠ No board is currently open. Open a .kicad_pcb file before running Auto Route.",
                    "color_hex": self._C_WARN_HEX,
                })
                self._render_conversation()
                return

            # ---- 2. Enumerate net names from board, show checklist ----
            # Net code 0 is the unconnected pseudo-net — skip it.
            all_nets = []
            try:
                for net_code in range(1, board.GetNetCount()):
                    net = board.FindNet(net_code)
                    if net is not None:
                        name = net.GetNetname()
                        if name:
                            all_nets.append(name)
                all_nets.sort()
            except Exception:
                all_nets = []

            # Fallback: plain text entry if we couldn't enumerate nets
            if not all_nets:
                dlg = wx.TextEntryDialog(
                    self,
                    "Net names to skip (comma-separated, e.g. GND,+3.3V).\n"
                    "Leave blank to route all nets.",
                    "Auto Route — Skip Nets",
                    value="",
                )
                if dlg.ShowModal() != wx.ID_OK:
                    dlg.Destroy()
                    return
                ignore_input = dlg.GetValue().strip()
                dlg.Destroy()
                ignore_nets = [n.strip() for n in ignore_input.split(",") if n.strip()] or None
            else:
                dlg = wx.MultiChoiceDialog(
                    self,
                    "Select nets FreeRouting should NOT route.\n"
                    "(These stay as ratsnest — route them manually, e.g. via power planes.)",
                    "Auto Route — Skip Nets",
                    all_nets,
                )
                dlg.SetSelections([])
                if dlg.ShowModal() != wx.ID_OK:
                    dlg.Destroy()
                    return
                selections = dlg.GetSelections()
                dlg.Destroy()
                ignore_nets = [all_nets[i] for i in selections] or None

            # ---- 3. Export DSN (main thread — pcbnew call) ----
            import shutil
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="kicad_autoroute_")
            dsn_path = os.path.join(tmp_dir, "board.dsn")
            ses_path = os.path.join(tmp_dir, "board.ses")

            # ---- Take a version snapshot via MCP tool before routing ----
            board_path = board.GetFileName()
            backup_path = ""
            if board_path and os.path.isfile(board_path):
                try:
                    from ..llm_client import call_mcp_tool
                    result = call_mcp_tool(
                        self._server_mgr.base_url,
                        "save_file_version",
                        {"file_path": board_path},
                    )
                    backup_path = result.get("snapshot_path", "")
                    log.info("autoroute: version snapshot saved to %s", backup_path)
                except Exception as exc:
                    log.warning("autoroute: version snapshot failed: %s", exc)
                    backup_path = ""

            try:
                _pcbnew.ExportSpecctraDSN(board, dsn_path)
            except Exception as exc:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                self._conv_entries.append({
                    "type": "autoroute_log",
                    "success": False,
                    "message": f"DSN export failed: {exc}",
                    "stdout": "", "stderr": "",
                })
                self._render_conversation()
                return

            if not os.path.isfile(dsn_path):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                self._conv_entries.append({
                    "type": "autoroute_log",
                    "success": False,
                    "message": "DSN export produced no output file.",
                    "stdout": "", "stderr": "",
                })
                self._render_conversation()
                return

            # ---- 3. Update UI, then start background thread ----
            self.GetMenuBar().Enable(self._menu_autoroute_id, False)
            self._status_label.SetLabel("⏳ Auto-routing in progress…")
            self._status_label.SetForegroundColour(wx.Colour(*self._C_WARN))
            self.Layout()

            self._conv_entries.append({
                "type": "status",
                "text": "⏳ Auto Route started — running FreeRouting in background…",
                "color_hex": self._C_WARN_HEX,
            })
            self._render_conversation()

            from ..autorouter import start_freerouting_thread

            def _progress(msg: str) -> None:
                wx.CallAfter(self._status_label.SetLabel, f"⏳ {msg}")

            def _routing_done(success: bool, message: str, stdout: str, stderr: str) -> None:
                # Marshal back to main thread for pcbnew import + UI update.
                wx.CallAfter(
                    self._on_autoroute_done,
                    success, message, stdout, stderr, ses_path, tmp_dir, backup_path,
                )

            start_freerouting_thread(
                dsn_path, ses_path,
                on_done=_routing_done,
                on_progress=_progress,
                ignore_nets=ignore_nets,
            )

        def _on_autoroute_done(
            self,
            success: bool,
            message: str,
            stdout: str,
            stderr: str,
            ses_path: str,
            tmp_dir: str,
            backup_path: str = "",
        ) -> None:
            """Called on the wx main thread after FreeRouting finishes.

            Performs ImportSpecctraSES + Refresh here so that all pcbnew UI
            operations stay on the main thread, preventing the blank-view bug.
            """
            import shutil

            try:
                if success and os.path.isfile(ses_path):
                    # ---- Import SES (main thread — pcbnew call) ----
                    try:
                        import pcbnew as _pcbnew
                        board = _pcbnew.GetBoard()
                        _pcbnew.ImportSpecctraSES(board, ses_path)
                        board.SetModified()
                        file_name = board.GetFileName()
                        if file_name:
                            board.Save(file_name)
                        _pcbnew.Refresh()
                    except Exception as exc:
                        success = False
                        message = f"SES import failed: {exc}"
                        log.error("autoroute: SES import error: %s", exc)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            # ---- Restore menu item and status bar ----
            self.GetMenuBar().Enable(self._menu_autoroute_id, True)
            if success:
                self._status_label.SetLabel("✅ Backend ready")
                self._status_label.SetForegroundColour(wx.Colour(*self._C_OK))
            else:
                self._status_label.SetLabel("❌ Auto-routing failed")
                self._status_label.SetForegroundColour(wx.Colour(*self._C_ERR))
            self.Layout()

            # FreeRouting SMD padstack note
            if success:
                message += (
                    " Note: if traces look sparse on a pure-SMD board, "
                    "FreeRouting may have skipped pads with '(attach off)' padstacks."
                )
                if backup_path:
                    message += f"\nBackup saved to: {backup_path}"

            # ---- Append collapsible log entry ----
            self._conv_entries.append({
                "type": "autoroute_log",
                "success": success,
                "message": message,
                "stdout": stdout,
                "stderr": stderr,
            })
            self._render_conversation()

        def _on_settings(self, event) -> None:
            from .settings_dialog import SettingsDialog
            dlg = SettingsDialog(self, self._settings)
            if dlg.ShowModal() == wx.ID_OK:
                if dlg.apply_to(self._settings):
                    self._settings.save()
            dlg.Destroy()

        # ------------------------------------------------------------------ #
        # Session persistence
        # ------------------------------------------------------------------ #

        def _sessions_dir(self) -> str:
            return os.path.join(self._settings.config_dir, "kicad_ai_sessions")

        def _on_new_session(self, event) -> None:
            """Save the current session (if non-empty) then clear to start a new one."""
            if self._busy:
                wx.MessageBox(
                    "Please wait for the current request to finish.",
                    "Busy", wx.OK | wx.ICON_INFORMATION,
                )
                return

            has_content = any(e["type"] in ("user", "ai") for e in self._conv_entries)
            if has_content:
                err = self._save_session_to_disk()
                if err:
                    wx.MessageBox(f"Could not save session:\n{err}", "Error", wx.OK | wx.ICON_ERROR)
                    return

            # Clear conversation and LLM history for the new session.
            self._stream_timer.Stop()
            self._stream_buffer.clear()
            self._conv_entries.clear()
            self._pending_ai_text = ""
            self._current_session_file = None
            self._render_conversation()
            if self._llm_client:
                self._llm_client.reset()

            # Remove current.json so a blank close won't restore the old session.
            self._remove_current_link()

            self._status_label.SetLabel("✅ New session started" + (
                " (previous session saved)" if has_content else ""
            ))
            self._status_label.SetForegroundColour(wx.Colour(*self._C_OK))
            self.Layout()

        def _remove_current_link(self) -> None:
            """Remove current.json (both symlink and plain-text variants)."""
            link = os.path.join(self._sessions_dir(), "current.json")
            try:
                if os.path.lexists(link):  # lexists catches dangling symlinks too
                    os.remove(link)
            except OSError as e:
                log.warning("Could not remove current.json: %s", e)

        def _save_session_to_disk(self) -> Optional[str]:
            """Write current conv_entries + history to disk and update current.json.

            If ``_current_session_file`` is already set the existing file is
            overwritten; otherwise a new timestamped file is created and
            ``_current_session_file`` is updated to track it.

            Returns an error string on failure, None on success.
            """
            import datetime
            import json as _json

            sessions_dir = self._sessions_dir()
            try:
                os.makedirs(sessions_dir, exist_ok=True)
            except OSError as e:
                return str(e)

            title = next(
                (e["text"][:60] for e in self._conv_entries if e["type"] == "user"),
                "session",
            )
            if self._current_session_file:
                filename = self._current_session_file
            else:
                ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                filename = f"session_{ts}.json"
                self._current_session_file = filename
            path = os.path.join(sessions_dir, filename)
            data = {
                "version": 1,
                "title": title,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "conv_entries": self._conv_entries,
                "llm_history": self._llm_client.get_history() if self._llm_client else [],
            }
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(data, f, indent=2, default=str)
            except OSError as e:
                return str(e)

            self._update_current_link(filename)
            return None

        def _update_current_link(self, filename: str) -> None:
            """Atomically update current.json to point at *filename* (basename)."""
            sessions_dir = self._sessions_dir()
            link = os.path.join(sessions_dir, "current.json")
            tmp_link = link + ".tmp"
            try:
                os.symlink(filename, tmp_link)
                os.replace(tmp_link, link)
            except Exception:
                try:
                    with open(link, "w", encoding="utf-8") as lf:
                        lf.write(filename)
                except Exception:
                    pass

        def _on_load_session(self, event) -> None:
            import json as _json
            import glob

            if self._busy:
                wx.MessageBox("Please wait for the current request to finish.",
                              "Busy", wx.OK | wx.ICON_INFORMATION)
                return

            sessions_dir = self._sessions_dir()
            files = sorted(
                glob.glob(os.path.join(sessions_dir, "session_*.json")),
                reverse=True,
            )
            if not files:
                wx.MessageBox("No saved sessions found.", "Load Session",
                              wx.OK | wx.ICON_INFORMATION)
                return

            # Build display labels
            labels = []
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        d = _json.load(fh)
                    ts = d.get("timestamp", "")[:19].replace("T", " ")
                    title = d.get("title", "")[:50]
                    labels.append(f"{ts}  —  {title}")
                except Exception:
                    labels.append(os.path.basename(f))

            dlg = wx.SingleChoiceDialog(
                self, "Select a session to restore:", "Load Session", labels,
            )
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return
            idx = dlg.GetSelection()
            dlg.Destroy()

            chosen = files[idx]
            try:
                with open(chosen, "r", encoding="utf-8") as fh:
                    data = _json.load(fh)
            except (OSError, _json.JSONDecodeError) as e:
                wx.MessageBox(f"Could not load session:\n{e}", "Error", wx.OK | wx.ICON_ERROR)
                return

            # Restore state
            self._stream_timer.Stop()
            self._stream_buffer.clear()
            self._pending_ai_text = ""
            self._conv_entries = data.get("conv_entries", [])
            if self._llm_client:
                self._llm_client.set_history(data.get("llm_history", []))
            # Track which file is now active; point current.json at it.
            self._current_session_file = os.path.basename(chosen)
            self._update_current_link(self._current_session_file)
            self._render_conversation(force_scroll_to_bottom=True)
            self._status_label.SetLabel("✅ Session restored")
            self._status_label.SetForegroundColour(wx.Colour(*self._C_OK))
            self.Layout()

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
            if self._llm_client:
                self._llm_client.reset()

        def _on_close(self, event) -> None:
            if event.CanVeto():
                # User closed the plugin panel – hide it so the backend stays
                # warm and KiCad is unaffected. The suicide watchdog timer
                # (_on_suicide_check) will force-close us when KiCad itself
                # exits, triggering the real teardown path below.
                event.Veto()
                self.Hide()
                return
            # Force-close (e.g. from _on_suicide_check when KiCad has exited)
            # – tear down completely.
            self._autosave_session()
            self._server_mgr.stop()
            self.Destroy()

        def _on_suicide_check(self, event) -> None:
            """Periodically check if KiCad is gone. If we are the only
            visible top-level wx window left, KiCad's main window must
            have been closed without sending us EVT_CLOSE — so close
            ourselves now."""
            others = [
                w for w in wx.GetTopLevelWindows()
                if w is not self and w.IsShown()
            ]
            if not others:
                self.Close(force=True)

        def _on_destroy(self, event) -> None:
            """Called when the wx window is actually destroyed (e.g. KiCad shutdown)."""
            if event.GetEventObject() is self:
                self._autosave_session()
                self._server_mgr.stop()
            event.Skip()

        # ------------------------------------------------------------------ #
        # Auto-save / auto-load
        # ------------------------------------------------------------------ #

        def _autosave_session(self) -> None:
            """Save a timestamped session file on every close.

            Also atomically updates the ``current.json`` symlink in the sessions
            directory so the next startup can load it directly without globbing.
            """
            # Only save if there is real conversational content — skip sessions
            # that only contain status/warning notices.
            if not any(e["type"] in ("user", "ai") for e in self._conv_entries):
                return
            err = self._save_session_to_disk()
            if err:
                log.warning("Auto-save failed: %s", err)

        def _autoload_session(self) -> None:
            """Restore the session pointed to by ``current.json`` on startup.

            Only follows current.json — no glob fallback. This ensures "New
            Session" (which removes current.json) always starts blank.
            """
            import json as _json

            sessions_dir = self._sessions_dir()
            link = os.path.join(sessions_dir, "current.json")
            path: Optional[str] = None

            if os.path.exists(link):
                # Resolve: may be a real symlink or the plain-text fallback.
                if os.path.islink(link):
                    target = os.readlink(link)
                    # readlink may return a relative path — resolve against dir.
                    if not os.path.isabs(target):
                        target = os.path.join(sessions_dir, target)
                    if os.path.isfile(target):
                        path = target
                else:
                    # Plain-text pointer written by the symlink fallback.
                    try:
                        with open(link, "r", encoding="utf-8") as lf:
                            fname = lf.read().strip()
                        candidate = os.path.join(sessions_dir, fname)
                        if os.path.isfile(candidate):
                            path = candidate
                    except OSError:
                        pass

            if path is None:
                return  # No current.json and no sessions dir — blank start.

            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
            except (OSError, _json.JSONDecodeError) as e:
                log.warning("Auto-load failed: %s", e)
                return

            conv = data.get("conv_entries", [])
            history = data.get("llm_history", [])
            if not conv:
                return

            self._conv_entries = conv
            self._current_session_file = os.path.basename(path)
            if self._llm_client:
                self._llm_client.set_history(history)
            self._conv_entries.append({
                "type": "status",
                "text": "↺ Previous session restored automatically.",
                "color_hex": self._C_TOOL_HEX,
            })
            # Render once with all content and force scroll to bottom
            self._render_conversation(force_scroll_to_bottom=True)

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

                # Fenced code block  ``` … ```
                if re.match(r"^```", line):
                    if in_list:
                        out.append("</ul>")
                        in_list = False
                    i += 1
                    code_lines: list[str] = []
                    while i < len(lines) and not re.match(r"^```", lines[i]):
                        code_lines.append(_h.escape(lines[i]))
                        i += 1
                    i += 1  # skip closing ```
                    code_content = "\n".join(code_lines)
                    out.append(
                        '<pre style="font-family:monospace;white-space:pre;'
                        'background:#f4f4f4;padding:8px;border-radius:4px;'
                        'overflow-x:auto;font-size:9pt">'
                        + code_content
                        + "</pre>"
                    )
                    continue

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

        def _on_webview_loaded(self, event) -> None:
            """Called when the shell HTML has finished loading.

            This fires exactly once at startup (and once after each shell
            reload on WebView error recovery).  It transitions the shell
            state and triggers the first conversation render.
            """
            log.debug("_on_webview_loaded: shell ready (render_pending=%s, entries=%d)",
                      self._render_pending, len(self._conv_entries))
            self._page_loading = False
            self._shell_retry_count = 0
            self._page_watchdog.Stop()

            # The page is loaded but JS may not have executed yet.
            # Schedule verification for later.
            wx.CallLater(1000, self._verify_shell_and_render)

        def _on_webview_error(self, event) -> None:
            """Handle WebView errors by reloading the shell.

            Since the shell is the only thing loaded via SetPage, an error
            means the shell failed to load.  We reset state and retry.
            """
            description = ""
            try:
                description = event.GetString()
            except Exception:
                pass
            log.warning("WebView error: %s — reloading shell", description)
            self._page_loading = False
            self._shell_loaded = False
            self._js_running = False
            self._stream_wrapper_visible = False
            self._page_watchdog.Stop()
            self._conv_entries.append({
                "type": "status",
                "text": f"⚠ WebView error: {description}. Retrying…",
                "color_hex": self._C_WARN_HEX,
            })
            self._render_pending = False
            wx.CallAfter(self._load_shell)

        def _verify_shell_and_render(self) -> None:
            """Verify JS is working and trigger render if needed.

            Called 1 second after EVT_WEBVIEW_LOADED to give scripts time to execute.
            """
            log.debug("_verify_shell_and_render: starting verification")
            self._js_running = True
            js_works = False

            try:
                # Check font
                ok, result = self._conv_view.RunScript(
                    "getComputedStyle(document.body).fontFamily"
                )
                log.info("_verify_shell_and_render: font=%r", result)

                # Check all window functions (what's actually defined in window)
                ok, result = self._conv_view.RunScript(
                    "Object.keys(window).filter(k => typeof window[k] === 'function' && k.startsWith('_')).join(',')"
                )
                log.info("_verify_shell_and_render: window functions starting with _ =%r", result)

                # Check scripts in document
                ok, result = self._conv_view.RunScript(
                    "document.scripts.length + ',' + (document.scripts[0] ? document.scripts[0].src : 'inline') + ',' + (document.scripts[0] ? document.scripts[0].innerHTML.length : 0)"
                )
                log.info("_verify_shell_and_render: scripts info=%r", result)

                # Check for JS errors in the console
                ok, result = self._conv_view.RunScript(
                    "try { eval('1+1'); 'no_error'; } catch(e) { e.message; }"
                )
                log.info("_verify_shell_and_render: eval test=%r", result)

                # Try to check for syntax errors in the script
                ok, result = self._conv_view.RunScript(
                    "try { new Function(document.scripts[0].innerHTML); 'syntax_ok'; } catch(e) { 'error:' + e.message; }"
                )
                log.info("_verify_shell_and_render: script syntax check=%r", result)

                # Try to call JS function
                ok, result = self._conv_view.RunScript(
                    "try { _updateConversation('[]', 'preserve'); 'ok'; } catch(e) { e.message; }"
                )
                log.info("_verify_shell_and_render: JS call result=%r", result)
                if ok and result and ("ok" in str(result) or "error" in str(result).lower()):
                    js_works = True
            except Exception as e:
                log.error("_verify_shell_and_render: exception: %s", e)
            finally:
                self._js_running = False

            if js_works:
                self._shell_loaded = True
                log.info("_verify_shell_and_render: JS verified, _shell_loaded=True")
            else:
                log.warning("_verify_shell_and_render: JS not working yet")

            # Trigger render if there is content or pending
            if self._render_pending or self._conv_entries:
                self._render_pending = False
                self._update_conversation(True)

        # ------------------------------------------------------------------ #
        # WebView rendering: SetPage-once architecture
        # ------------------------------------------------------------------ #
        # The shell HTML (CSS + JS framework + empty containers) is loaded
        # exactly once via SetPage.  All subsequent UI updates go through
        # RunScript calls to JS functions defined in the shell.  This
        # eliminates the Windows WebView2 deadlock caused by repeated SetPage
        # calls triggering nested message-pump re-entrancy.
        # ------------------------------------------------------------------ #

        def _prepare_entries_for_js(self) -> list[dict]:
            """Serialize _conv_entries for JavaScript consumption.

            - AI text: render markdown to HTML
            - Tool calls: truncate args/result for display
            - User text / status: kept as-is (JS will escape)
            """
            import json as _json
            result = []
            for entry in self._conv_entries:
                e = dict(entry)  # shallow copy — don't mutate originals
                if e["type"] == "ai":
                    e["text"] = self._md_to_html(e.get("text", ""))
                elif e["type"] == "tool_call":
                    e["args"] = self._truncate_for_ui(e.get("args", {}), self._MAX_UI_JSON)
                    e["result"] = self._truncate_for_ui(e.get("result", {}), self._MAX_UI_JSON)
                elif e["type"] == "autoroute_log":
                    raw_out = (e.get("stdout") or "").strip()
                    raw_err = (e.get("stderr") or "").strip()
                    combined = "\n".join(filter(None, [
                        raw_out,
                        ("--- stderr ---\n" + raw_err) if raw_err else "",
                    ]))
                    e["output"] = combined or "(no output)"
                result.append(e)
            return result

        def _prepare_single_entry(self, entry: dict) -> dict:
            """Prepare a single entry for JavaScript consumption."""
            e = dict(entry)
            if e["type"] == "ai":
                e["text"] = self._md_to_html(e.get("text", ""))
            elif e["type"] == "tool_call":
                e["args"] = self._truncate_for_ui(e.get("args", {}), self._MAX_UI_JSON)
                e["result"] = self._truncate_for_ui(e.get("result", {}), self._MAX_UI_JSON)
            elif e["type"] == "autoroute_log":
                raw_out = (e.get("stdout") or "").strip()
                raw_err = (e.get("stderr") or "").strip()
                combined = "\n".join(filter(None, [
                    raw_out,
                    ("--- stderr ---\n" + raw_err) if raw_err else "",
                ]))
                e["output"] = combined or "(no output)"
            return e

        def _update_conversation(self, force_scroll_to_bottom: bool = False) -> None:
            """Full conversation update via RunScript (WebView path).

            Serializes all entries as JSON and calls _updateConversation()
            in the shell.
            """
            if not self._try_acquire_render_lock():
                return  # _try_acquire_render_lock already set _render_pending

            import json as _json
            js_entries = self._prepare_entries_for_js()
            scroll = '"bottom"' if force_scroll_to_bottom else '"preserve"'
            # Double json.dumps: inner produces JSON string, outer escapes it
            # for safe embedding inside a JS function call argument.
            js = f'_updateConversation({_json.dumps(_json.dumps(js_entries))}, {scroll})'
            log.debug("_updateConversation: %d entries, scroll=%s, js_size=%d",
                      len(js_entries), scroll, len(js))

            try:
                ok, result = self._conv_view.RunScript(js)
                if ok:
                    self._stream_wrapper_visible = False
                    result_str = str(result) if result else ""
                    if result_str.startswith("error:"):
                        log.error("_updateConversation JS error: %s (js_size=%d, entries=%d)",
                                  result_str, len(js), len(js_entries))
                    else:
                        log.debug("_updateConversation JS result: %s", result_str)
                else:
                    log.error("_updateConversation RunScript FAILED: js_size=%d, result=%r, entries=%d",
                              len(js), result, len(js_entries))
            except Exception as e:
                log.error("_updateConversation exception: %s", e)
            finally:
                self._release_render_lock(force_scroll_to_bottom)

        def _process_pending_render(self, force_scroll_to_bottom: bool) -> None:
            """Process any pending render after _js_running is reset."""
            if self._render_pending:
                self._render_pending = False
                wx.CallAfter(self._update_conversation, force_scroll_to_bottom)

        # ------------------------------------------------------------------ #
        # Render lock helpers: encapsulate sync state management
        # ------------------------------------------------------------------ #
        def _try_acquire_render_lock(self) -> bool:
            """Try to acquire the render lock. Returns True if acquired.

            If acquisition fails (shell not loaded or another render in progress),
            sets _render_pending so the render will be retried later.
            """
            self._render_pending = False  # Reset first to allow recursion

            if self._js_running:
                self._render_pending = True
                return False

            # If shell_loaded is False, try to detect if it actually is loaded
            # (EVT_WEBVIEW_LOADED may not fire on all Windows WebView2 versions)
            if not self._shell_loaded:
                # Try to detect shell state by checking if the conversation div exists
                # AND if JS functions are defined. Try multiple times in case scripts
                # haven't executed yet.
                shell_loaded_detected = False
                for attempt in range(3):
                    try:
                        ok, result = self._conv_view.RunScript(
                            "document.getElementById('conversation') ? 'loaded' : 'not_loaded'"
                        )
                        if ok and result and "loaded" in str(result):
                            # Also verify JS functions exist by trying to call one
                            ok2, result2 = self._conv_view.RunScript(
                                "try { _updateConversation('[]', 'preserve'); 'ok'; } catch(e) { e.message; }"
                            )
                            log.info("JS test attempt %d: %r", attempt + 1, result2)
                            if ok2 and result2 and ("ok" in str(result2) or "error" in str(result2).lower()):
                                log.info("Detected shell and JS working (attempt %d)", attempt + 1)
                                self._shell_loaded = True
                                self._page_loading = False
                                shell_loaded_detected = True
                                break
                            else:
                                log.warning("Shell loaded but JS not working (attempt %d)", attempt + 1)
                        else:
                            break
                    except Exception as e:
                        log.warning("Failed to detect shell state (attempt %d): %s", attempt + 1, e)
                        break

                if not shell_loaded_detected:
                    log.warning("Shell loaded but JS not working after 3 attempts, allowing retry")
                    self._render_pending = True
                    return False

            self._js_running = True
            return True

        def _release_render_lock(self, force_scroll_to_bottom: bool) -> None:
            """Release the render lock and process pending if needed."""
            self._js_running = False
            self._process_pending_render(force_scroll_to_bottom)

        def _append_entry_js(self, entry: dict,
                              force_scroll_to_bottom: bool = False) -> None:
            """Append a single entry via JS _appendEntry (WebView path)."""
            if not self._try_acquire_render_lock():
                return  # _try_acquire_render_lock already set _render_pending

            import json as _json
            prepared = self._prepare_single_entry(entry)
            scroll = '"bottom"' if force_scroll_to_bottom else '"preserve"'
            js = f'_appendEntry({_json.dumps(_json.dumps(prepared))}, {scroll})'
            log.debug("_appendEntry: type=%s, scroll=%s", entry.get("type"), scroll)

            try:
                ok, result = self._conv_view.RunScript(js)
                if ok:
                    self._stream_wrapper_visible = False
                    result_str = str(result) if result else ""
                    if result_str.startswith("error:"):
                        log.error("_appendEntry JS error: %s (type=%s, js_size=%d)",
                                  result_str, entry.get("type"), len(js))
                    else:
                        log.debug("_appendEntry JS result: %s", result_str)
                else:
                    log.error("_appendEntry RunScript FAILED: result=%r, type=%s, js_size=%d",
                              result, entry.get("type"), len(js))
                    self._render_pending = True
            except Exception:
                self._render_pending = True
            finally:
                self._release_render_lock(force_scroll_to_bottom)

        def _show_stream_wrapper(self) -> None:
            """Make the stream-wrapper table visible (first streaming chunk)."""
            if not self._shell_loaded or self._stream_wrapper_visible:
                return  # No-op

            # Use the lock but handle the case where we don't need full update
            if not self._try_acquire_render_lock():
                return

            try:
                ok, _ = self._conv_view.RunScript(
                    "document.getElementById('stream-wrapper').style.display=''"
                )
                if ok:
                    self._stream_wrapper_visible = True
            finally:
                self._release_render_lock(False)

        def _hide_stream_wrapper(self) -> None:
            """Hide the stream-wrapper table and clear pending text."""
            if not self._shell_loaded or not self._stream_wrapper_visible:
                return  # No-op

            # Use the lock but handle the case where we don't need full update
            if not self._try_acquire_render_lock():
                return

            try:
                ok, _ = self._conv_view.RunScript(
                    "var w=document.getElementById('stream-wrapper');"
                    "if(w)w.style.display='none';"
                    "document.getElementById('pending-ai-text').innerHTML='';"
                )
                if ok:
                    self._stream_wrapper_visible = False
            finally:
                self._release_render_lock(False)

        def _render_conversation(self, force_scroll_to_bottom: bool = False) -> None:
            """Re-render the conversation.  Routes to WebView or HtmlWindow path."""
            if self._use_webview:
                # Note: _page_loading is handled by the shell load completion events
                # which will trigger pending renders when done
                if self._page_loading:
                    self._render_pending = True
                    return  # Let the page load complete and handle it

                # Delegate to _update_conversation which handles locking
                self._update_conversation(force_scroll_to_bottom)
            else:
                self._render_conversation_htmlwindow(force_scroll_to_bottom)

        def _render_conversation_htmlwindow(self, force_scroll_to_bottom: bool = False) -> None:
            """HtmlWindow fallback: build HTML and call SetPage.

            Used only when wx.html2.WebView is unavailable.  The WebView path
            uses _update_conversation (RunScript-based) instead.
            """
            import html as _h

            parts = [
                f'<html><body style="font-family: Arial, sans-serif; font-size: 10pt;"'
                f' bgcolor="{self._BG_CONV_HEX}">'
            ]

            def _tool_html_plain(tools: list[dict]) -> str:
                """Compact inline tool summary for wx.html.HtmlWindow (no folding)."""
                if not tools:
                    return ""
                rows = []
                for t in tools:
                    ok = t["result"].get("success", True) if isinstance(t["result"], dict) else True
                    icon = "&#x2713;" if ok else "&#x2717;"
                    name = _h.escape(t["name"])
                    summary = _h.escape(
                        t["result"].get("message", "") if isinstance(t["result"], dict)
                        else str(t["result"])
                    )[:120]
                    color = self._C_OK_HEX if ok else self._C_ERR_HEX
                    rows.append(
                        f'<font color="{color}"><tt>{icon} {name}</tt></font>'
                        f' <font color="{self._C_TOOL_HEX}"><i>{summary}</i></font>'
                    )
                inner = "<br>".join(rows)
                return f'<blockquote style="margin:4px 0">{inner}</blockquote>'

            def _msg_block(sender: str, sender_color: str, bg_color: str,
                           body_html: str, tools_html: str = "",
                           timestamp: str = "") -> str:
                ts_html = (
                    f' <font size="2" color="#999999"><i>{timestamp}</i></font>'
                    if timestamp else ""
                )
                return (
                    f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{bg_color}"'
                    f' border="0"><tr><td>'
                    f'<b><font color="{sender_color}" size="3">{sender}</font></b>'
                    f'{ts_html}'
                    f'<br>{body_html}{tools_html}'
                    f'</td></tr></table>'
                    f'<br>'
                )

            for entry in self._conv_entries:
                typ = entry["type"]
                text = entry.get("text", "")
                if typ == "user":
                    body = _h.escape(text).replace("\n", "<br>")
                    ts = entry.get("timestamp", "")
                    parts.append(_msg_block("You", self._C_USER_HEX, "#EBF0FF", body, timestamp=ts))
                elif typ == "ai":
                    body = self._md_to_html(text)
                    tools = entry.get("tools") or []
                    ts = entry.get("timestamp", "")
                    tools_html = _tool_html_plain(tools)
                    parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body, tools_html, timestamp=ts))
                elif typ == "tool_call":
                    ok = entry["result"].get("success", True) if isinstance(entry["result"], dict) else True
                    icon = "&#x2713;" if ok else "&#x2717;"
                    tname = _h.escape(entry["name"])
                    color = self._C_OK_HEX if ok else self._C_ERR_HEX
                    parts.append(
                        f'<p style="margin: 2px 8px;"><font color="{color}"><tt>{icon} ↳ {tname}</tt></font></p>'
                    )
                elif typ == "status":
                    color = entry.get("color_hex", "#1E1E1E")
                    escaped = _h.escape(text)
                    parts.append(f'<p style="margin: 2px 8px;"><font color="{color}">{escaped}</font></p>')
                elif typ == "autoroute_log":
                    ok = entry.get("success", False)
                    msg = _h.escape(entry.get("message", ""))
                    raw_out = (entry.get("stdout") or "").strip()
                    raw_err = (entry.get("stderr") or "").strip()
                    combined = "\n".join(filter(None, [raw_out, ("--- stderr ---\n" + raw_err) if raw_err else ""]))
                    icon = "&#x2713;" if ok else "&#x2717;"
                    color = self._C_OK_HEX if ok else self._C_ERR_HEX
                    snippet = _h.escape(combined[:500] + ("…" if len(combined) > 500 else ""))
                    parts.append(
                        f'<p style="margin: 2px 8px;">'
                        f'<font color="{color}"><tt>{icon} Auto Route</tt></font> {msg}</p>'
                        f'<blockquote style="margin:2px 8px"><tt><font size="2">{snippet}</font></tt></blockquote>'
                    )

            # Show pending streamed AI text (HtmlWindow path only).
            if self._pending_ai_text:
                body = self._md_to_html(self._pending_ai_text)
                parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body))

            # Capture scroll position before replacing the page
            _max = self._conv_view.GetScrollRange(wx.VERTICAL)
            _pos = self._conv_view.GetScrollPos(wx.VERTICAL)
            _at_bottom = force_scroll_to_bottom or _max == 0 or (_max - _pos) < 3

            parts.append("</body></html>")
            self._conv_view.SetPage("".join(parts))
            # Use wx.CallLater so the scroll fires after wx has finished
            # its layout pass for the new page.
            if _at_bottom:
                wx.CallLater(
                    50,
                    lambda: self._conv_view.Scroll(
                        0, self._conv_view.GetScrollRange(wx.VERTICAL)
                    ),
                )
            else:
                _frac = _pos / _max if _max > 0 else 0.0
                wx.CallLater(
                    50,
                    lambda f=_frac: self._conv_view.Scroll(
                        0, int(f * self._conv_view.GetScrollRange(wx.VERTICAL))
                    ),
                )

        # Maximum character length for tool args/result JSON in UI display.
        # Keeps HTML size manageable for WebView2 (large NavigateToString payloads
        # cause the control to freeze on Windows).  LLM history is unaffected.
        _MAX_UI_JSON = 2000

        @staticmethod
        def _truncate_for_ui(obj: Any, max_chars: int = 2000) -> Any:
            """Truncate large objects for UI display in tool_call entries.

            Returns the original object if its JSON representation is within
            *max_chars* characters; otherwise returns a truncated string.
            The LLM conversation history always stores the full object.
            """
            import json as _json
            try:
                s = _json.dumps(obj, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                return repr(obj)
            if len(s) <= max_chars:
                return obj
            return s[:max_chars] + f"… [{len(s)} chars total]"

        @staticmethod
        def _json_dump_safe(value: Any, *, indent: int | None = None) -> str:
            import json as _json
            try:
                if indent is None:
                    return _json.dumps(value, separators=(',', ':'), default=str)
                return _json.dumps(value, indent=indent, default=str)
            except (TypeError, ValueError):
                return repr(value)

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
