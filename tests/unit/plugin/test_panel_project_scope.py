"""Contract tests: the panel threads its active project into every LLM turn.

Issue #255: symbol-library tools default an omitted ``project_path`` to the
active project inside ``LLMClient.run``.  That default only exists if both
panel call sites — chat and framework ``tool_direct`` — pass the panel's
``_active_project`` through, so cover the real ``_send_request`` code path
against a stub wx module (no display).
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import importlib
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass


@contextmanager
def _panel_module():
    """Import ``kicad_plugin.ui.panel`` against a stub ``wx`` module."""
    fake_wx = types.ModuleType("wx")
    fake_wx.__kcaa_fake_wx__ = True  # type: ignore[attr-defined]
    fake_wx.__getattr__ = lambda name: _Dummy  # type: ignore[attr-defined]
    fake_html = types.ModuleType("wx.html")
    fake_html.HtmlWindow = _Dummy  # type: ignore[attr-defined]
    fake_html2 = types.ModuleType("wx.html2")
    fake_html2.WebView = _Dummy  # type: ignore[attr-defined]
    fake_wx.html = fake_html  # type: ignore[attr-defined]
    fake_wx.html2 = fake_html2  # type: ignore[attr-defined]

    module_names = ("wx", "wx.html", "wx.html2", "kicad_plugin.ui.panel")
    missing = object()
    saved_modules = {name: sys.modules.get(name, missing) for name in module_names}
    ui_pkg = sys.modules.get("kicad_plugin.ui")
    saved_panel_attr = getattr(ui_pkg, "panel", None) if ui_pkg is not None else None
    try:
        sys.modules["wx"] = fake_wx
        sys.modules["wx.html"] = fake_html
        sys.modules["wx.html2"] = fake_html2
        sys.modules.pop("kicad_plugin.ui.panel", None)
        panel = importlib.import_module("kicad_plugin.ui.panel")
        assert panel._WX_AVAILABLE  # noqa: SLF001 - exercising the real branch
        yield panel
    finally:
        for name, previous in saved_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous  # type: ignore[assignment]
        current_ui_pkg = sys.modules.get("kicad_plugin.ui")
        if current_ui_pkg is not None:
            if saved_panel_attr is None:
                current_ui_pkg.__dict__.pop("panel", None)
            else:
                current_ui_pkg.panel = saved_panel_attr  # type: ignore[attr-defined]


class _InlineThread:
    """Run the turn body synchronously so assertions need no join/race."""

    def __init__(self, target=None, daemon=None, *args, **kwargs) -> None:
        self._target = target

    def start(self) -> None:
        assert self._target is not None
        self._target()


class _FakeInput:
    def __init__(self, text: str) -> None:
        self._text = text

    def GetValue(self) -> str:
        return self._text

    def Clear(self) -> None:
        self._text = ""


class _FakeTimer:
    def Start(self, interval: int) -> None:
        pass


class _FakePanel:
    """Minimal receiver for ``AssistantPanel._send_request``."""

    def __init__(self, client: MagicMock, active_project: str | None) -> None:
        self._busy = False
        self._llm_client = client
        self._active_project = active_project
        self._server_ready = True
        self._settings = types.SimpleNamespace(llm_supports_vision=True)
        self._input = _FakeInput("hello")
        self._attached_files: list = []
        self._conv_entries: list = []
        self._conv_version = 0
        self._stream_events: deque = deque()
        self._stream_timer = _FakeTimer()
        self._pending_ai_text = ""
        self._stream_delta_chars = 0
        self._tool_calls_made = False
        self._schematic_edited = False
        self._pcb_edited = False
        self._turn_had_text = False
        self._event_gen = 0
        self._cancel_event = None
        self._follow_output_to_bottom = False

    def _toggle_send_stop(self, busy: bool) -> None:
        pass

    def _encode_attached_images(self) -> list:
        return []

    def _refresh_attachments_bar(self) -> None:
        pass

    def _render_conversation(self, *args, **kwargs) -> None:
        pass


@pytest.mark.parametrize("kind", ["chat", "tool_direct"])
def test_send_request_passes_active_project_to_run(kind: str) -> None:
    with _panel_module() as panel:
        client = MagicMock()
        client.run.return_value = "ok" if kind == "chat" else ""
        receiver = _FakePanel(client, active_project="/p/proj")
        payload = {} if kind == "chat" else {"name": "sync_symbol_index", "arguments": {}}

        with (
            patch.object(threading, "Thread", _InlineThread),
            patch("kicad_plugin.context_bridge.collect_context", return_value={}),
            patch(
                "kicad_plugin.context_bridge.context_to_system_prompt_block",
                return_value="CTX",
            ),
        ):
            panel.AssistantPanel._send_request(receiver, kind, payload)

    assert client.run.call_args.kwargs["active_project"] == "/p/proj"
    if kind == "tool_direct":
        assert client.run.call_args.args[0] == {
            "kind": "tool_direct",
            "name": "sync_symbol_index",
            "arguments": {},
        }
