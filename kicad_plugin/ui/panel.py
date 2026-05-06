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
            # Set to True when any tool modifies a .kicad_sch file this turn
            self._schematic_edited: bool = False
            # Set to True when any tool modifies a .kicad_pcb file this turn
            self._pcb_edited: bool = False
            # Tool calls buffered during the current AI turn; flushed into the
            # AI conv_entry when the turn finishes.
            self._pending_tool_calls: list[dict] = []
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

            # ---- Conversation view (WebView when available, HtmlWindow fallback) ----
            self._use_webview = False
            if _WEBVIEW_AVAILABLE:
                try:
                    self._conv_view = _wx_html2.WebView.New(
                        panel, style=wx.BORDER_SUNKEN,
                    )
                    self._use_webview = True
                except Exception:
                    pass

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
            m = wx.Menu()
            m.Append(wx.ID_PREFERENCES, "&Settings\tCtrl+,")
            self._menu_new_session_id = wx.NewIdRef()
            self._menu_load_session_id = wx.NewIdRef()
            m.AppendSeparator()
            m.Append(self._menu_new_session_id, "New Session")
            m.Append(self._menu_load_session_id, "Load Session…")
            m.AppendSeparator()
            self._menu_restart_id = wx.NewIdRef()
            m.Append(self._menu_restart_id, "Restart Backend")
            menu_bar.Append(m, "&Options")
            self.SetMenuBar(menu_bar)

            # ---- Events ----
            self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
            self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
            self.Bind(wx.EVT_MENU, self._on_settings, id=wx.ID_PREFERENCES)
            self.Bind(wx.EVT_MENU, self._on_new_session, id=self._menu_new_session_id)
            self.Bind(wx.EVT_MENU, self._on_load_session, id=self._menu_load_session_id)
            self.Bind(wx.EVT_MENU, self._on_restart, id=self._menu_restart_id)
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
                self._check_kicad_ipc_environment()
            else:
                self._status_label.SetLabel("❌ Backend failed to start — use Options → Restart Backend to retry")
                self._status_label.SetForegroundColour(wx.Colour(*self._C_ERR))
            self.Layout()

        def _init_llm_client(self) -> None:
            from ..llm_client import LLMClient
            self._llm_client = LLMClient(self._settings, self._server_mgr.base_url)
            self._autoload_session()

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
            import platform
            from tempfile import gettempdir

            # 1) kipy presence check in the plugin's .venv -----------------
            # kipy runs inside the MCP server venv, not KiCad's Python, so
            # we must NOT do `import kipy` here.  Instead probe the venv's
            # site-packages on disk.
            plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            venv_site = os.path.join(plugin_dir, ".venv", "lib")
            kipy_found = bool(glob.glob(os.path.join(venv_site, "python*", "site-packages", "kipy")))
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

            # 2) IPC socket check ------------------------------------------
            socket_url = os.environ.get("KICAD_API_SOCKET")
            if socket_url:
                # Strip ipc:// prefix if present, check filesystem path
                socket_path = socket_url[len("ipc://"):] if socket_url.startswith("ipc://") else socket_url
                socket_exists = os.path.exists(socket_path) or platform.system() == "Windows"
                checked_path = socket_url
            elif platform.system() == "Windows":
                # Windows uses a named pipe — can't easily stat it
                socket_exists = True
                checked_path = f"{gettempdir()}\\kicad\\api.sock"
            else:
                # Glob for api*.sock (covers api.sock and api-<pid>.sock).
                # Try flatpak directory first, then standard /tmp/kicad.
                home = os.environ.get("HOME", "")
                candidate_dirs = []
                if home:
                    candidate_dirs.append(f"{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad")
                candidate_dirs.append("/tmp/kicad")

                socket_exists = False
                checked_path = "/tmp/kicad/api.sock"  # fallback for error message
                for sock_dir in candidate_dirs:
                    matches = glob.glob(os.path.join(sock_dir, "api*.sock"))
                    if matches:
                        # Report the newest socket found
                        checked_path = max(matches, key=os.path.getmtime)
                        socket_exists = True
                        break
                if not socket_exists:
                    checked_path = "/tmp/kicad/api.sock"

            if not socket_exists:
                self._conv_entries.append({
                    "type": "status",
                    "text": (
                        f"⚠ KiCad IPC API socket not found at {checked_path}. "
                        "The 'Reload PCB' feature and 'update_pcb_from_schematic' "
                        "tool require it. Enable it in KiCad: "
                        "Preferences → Preferences → Plugins → "
                        "'Enable KiCad API' (KiCad 9+)."
                    ),
                    "color_hex": self._C_WARN_HEX,
                })

            if not kipy_ok or not socket_exists:
                self._render_conversation()

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
            # Create the session file on the very first message so current.json
            # is established before the AI responds.
            if self._current_session_file is None:
                err = self._save_session_to_disk()
                if err:
                    log.warning("Could not create session file: %s", err)
            self._render_conversation()
            self._busy = True
            self._send_btn.Enable(False)

            from ..context_bridge import collect_context, context_to_system_prompt_block
            ctx = collect_context()
            context_block = context_to_system_prompt_block(ctx)

            # Reset streaming state and start the flush timer
            self._stream_buffer.clear()
            self._pending_ai_text = ""
            self._pending_tool_calls = []
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

            tools_snapshot = list(self._pending_tool_calls)
            self._pending_tool_calls = []

            # Remove the transient in-progress tool lines; they will be
            # replaced by the folded tool details inside the AI entry.
            self._conv_entries = [e for e in self._conv_entries if e["type"] != "tool"]

            if not was_streamed:
                self._conv_entries.append({"type": "ai", "text": reply, "tools": tools_snapshot})
                self._render_conversation()
            else:
                # Finalise the streamed text as a proper AI entry
                if self._pending_ai_text:
                    self._conv_entries.append({
                        "type": "ai",
                        "text": self._pending_ai_text,
                        "tools": tools_snapshot,
                    })
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
            # Buffer for embedding into the AI entry when the turn finishes.
            self._pending_tool_calls.append({"name": name, "args": args, "result": result})
            # Show a lightweight in-progress line while the turn is still running
            # so the user sees activity.  This entry is not persisted — it will
            # be replaced by the final AI entry in _on_reply.
            ok = result.get("success", True) if isinstance(result, dict) else True
            icon = "✓" if ok else "✗"
            summary = result.get("message", "") if isinstance(result, dict) else str(result)
            self._conv_entries.append({"type": "tool", "text": f"↳ {name}  {icon} {summary}"})
            self._render_conversation()
            self._tool_calls_made = True
            if isinstance(result, dict) and str(result.get("file_modified", "")).endswith(".kicad_sch"):
                self._schematic_edited = True
            if isinstance(result, dict) and str(result.get("file_modified", "")).endswith(".kicad_pcb"):
                self._pcb_edited = True

        def _auto_refresh(self, ctx: dict) -> None:
            """Refresh the KiCad view automatically after tool calls."""
            editor = ctx.get("active_editor", "unknown")
            if self._pcb_edited:
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
            if self._schematic_edited:
                self._conv_entries.append({
                    "type": "status",
                    "text": "ℹ Schematic updated on disk — use File → Revert in the Schematic Editor to see the changes.",
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
            self._pending_tool_calls = []
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
            self._pending_tool_calls = []
            self._conv_entries = data.get("conv_entries", [])
            if self._llm_client:
                self._llm_client.set_history(data.get("llm_history", []))
            # Track which file is now active; point current.json at it.
            self._current_session_file = os.path.basename(chosen)
            self._update_current_link(self._current_session_file)
            self._render_conversation()
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
            self._pending_tool_calls = []
            self._pending_ai_text = ""
            self._render_conversation()
            if self._llm_client:
                self._llm_client.reset()

        def _on_close(self, event) -> None:
            # Always tear down. Hiding instead (to keep the server warm)
            # leaves a top-level wx.Frame alive that prevents KiCad from
            # exiting cleanly when the user closes KiCad first.
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
            self._render_conversation()
            self._conv_entries.append({
                "type": "status",
                "text": "↺ Previous session restored automatically.",
                "color_hex": self._C_TOOL_HEX,
            })
            self._render_conversation()

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

        def _render_conversation(self) -> None:
            """Re-render the full conversation as HTML and update the view."""
            import html as _h
            import json as _json

            if self._use_webview:
                bg = self._BG_CONV_HEX
                parts = [
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<style>"
                    f"body{{font-family:Arial,sans-serif;font-size:10pt;background:{bg};margin:4px}}"
                    "table.msg{width:100%;border-collapse:collapse;margin-bottom:8px}"
                    "table.msg td{padding:8px 10px;border-radius:4px}"
                    "details.tools{margin-top:6px;font-size:9pt}"
                    "details.tools summary{cursor:pointer;color:#787878;user-select:none;"
                    "padding:2px 4px;border-radius:3px;list-style:disclosure-closed}"
                    "details.tools summary:hover{background:#e8e8e8}"
                    "details.tools[open] summary{list-style:disclosure-open}"
                    ".tool-entry{margin:4px 0;padding:4px 8px;background:#f5f5f0;"
                    "border-left:3px solid #ccc;border-radius:2px;"
                    "font-family:monospace;font-size:8.5pt;white-space:pre-wrap;"
                    "word-break:break-all}"
                    ".tool-ok{border-left-color:#4caf50}"
                    ".tool-err{border-left-color:#f44336}"
                    "pre{font-family:monospace;white-space:pre;background:#f4f4f4;"
                    "padding:8px;border-radius:4px;overflow-x:auto;font-size:9pt;"
                    "line-height:1.4}"
                    "pre code{background:none;padding:0;border-radius:0;font-size:inherit}"
                    "code{font-family:monospace;background:#f0f0f0;"
                    "padding:1px 3px;border-radius:2px}"
                    "</style></head>"
                    f"<body>"
                ]
            else:
                parts = [
                    f'<html><body style="font-family: Arial, sans-serif; font-size: 10pt;"'
                    f' bgcolor="{self._BG_CONV_HEX}">'
                ]

            def _tool_html_webview(tools: list[dict]) -> str:
                if not tools:
                    return ""
                count = len(tools)
                label = f"{count} tool call{'s' if count > 1 else ''}"
                rows = []
                for t in tools:
                    ok = t["result"].get("success", True) if isinstance(t["result"], dict) else True
                    icon = "✓" if ok else "✗"
                    css = "tool-entry tool-ok" if ok else "tool-entry tool-err"
                    args_txt = _h.escape(_json.dumps(t["args"], separators=(",", ":"), default=str))
                    result_txt = _h.escape(_json.dumps(t["result"], separators=(",", ":"), default=str))
                    name = _h.escape(t["name"])
                    rows.append(
                        f'<div class="{css}">'
                        f'<b>{icon} {name}</b><br>'
                        f'<span style="color:#555">args:</span> {args_txt}<br>'
                        f'<span style="color:#555">result:</span> {result_txt}'
                        f'</div>'
                    )
                return (
                    f'<details class="tools"><summary>{label}</summary>'
                    + "".join(rows)
                    + "</details>"
                )

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

            if self._use_webview:
                def _msg_block(sender: str, sender_color: str, bg_color: str,
                               body_html: str, tools_html: str = "") -> str:
                    return (
                        f'<table class="msg"><tr><td style="background:{bg_color}">'
                        f'<b><span style="color:{sender_color}">{sender}</span></b>'
                        f'<br>{body_html}{tools_html}'
                        f'</td></tr></table>'
                    )
            else:
                def _msg_block(sender: str, sender_color: str, bg_color: str,
                               body_html: str, tools_html: str = "") -> str:
                    return (
                        f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{bg_color}"'
                        f' border="0"><tr><td>'
                        f'<b><font color="{sender_color}" size="3">{sender}</font></b>'
                        f'<br>{body_html}{tools_html}'
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
                    tools = entry.get("tools") or []
                    if self._use_webview:
                        tools_html = _tool_html_webview(tools)
                    else:
                        tools_html = _tool_html_plain(tools)
                    parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body, tools_html))
                elif typ == "tool":
                    # Transient in-progress line shown while the turn is running
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
                # Pending tool calls shown while streaming
                if self._use_webview:
                    tools_html = _tool_html_webview(self._pending_tool_calls)
                else:
                    tools_html = _tool_html_plain(self._pending_tool_calls)
                parts.append(_msg_block("AI", self._C_AI_HEX, "#EBF7F2", body, tools_html))

            if self._use_webview:
                # Read the current scroll position BEFORE SetPage (old page still loaded).
                # RunScript is synchronous on the current page so values are reliable.
                _ok_y, _sy = self._conv_view.RunScript("String(window.scrollY)")
                _ok_h, _sh = self._conv_view.RunScript(
                    "String(document.body.scrollHeight - window.innerHeight)"
                )
                try:
                    _sy_val = int(float(_sy)) if _ok_y else 0
                    _sh_val = int(float(_sh)) if _ok_h else 0
                    # Consider "at bottom" when within 50 px or page not yet scrollable
                    _at_bottom = _sh_val < 50 or (_sh_val - _sy_val) < 50
                except (ValueError, TypeError):
                    _at_bottom = True
                    _sy_val = 0

                # Embed the scroll command as an inline <script> at the end of <body>
                # so it executes synchronously as part of the page load, avoiding the
                # async race condition that makes a post-SetPage RunScript ineffective.
                if _at_bottom:
                    _scroll_js = "window.scrollTo(0, document.body.scrollHeight);"
                else:
                    _scroll_js = f"window.scrollTo(0, {_sy_val});"
                parts.append(f"<script>{_scroll_js}</script>")
                parts.append("</body></html>")
                self._conv_view.SetPage("".join(parts), "")
            else:
                # Capture scroll position before replacing the page
                _max = self._conv_view.GetScrollRange(wx.VERTICAL)
                _pos = self._conv_view.GetScrollPos(wx.VERTICAL)
                _at_bottom = _max == 0 or (_max - _pos) < 3

                parts.append("</body></html>")
                self._conv_view.SetPage("".join(parts))
                # Defer scroll until after layout is complete
                if _at_bottom:
                    wx.CallAfter(
                        self._conv_view.Scroll, 0,
                        self._conv_view.GetScrollRange(wx.VERTICAL)
                    )
                else:
                    _new_max = self._conv_view.GetScrollRange(wx.VERTICAL)
                    _new_pos = int(_pos * _new_max / _max) if _max > 0 else 0
                    wx.CallAfter(self._conv_view.Scroll, 0, _new_pos)

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
