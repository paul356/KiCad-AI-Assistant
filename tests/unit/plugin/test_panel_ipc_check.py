"""Regression tests for the panel's KiCad IPC API status check (issue #230).

A failed MCP transport call returns ``{"success": False, "error": ...}`` —
it carries no ``connected`` key, because the request never reached
``check_kicad_ipc_connection``.  The panel used to read that as
``connected == False`` and tell the user to enable the KiCad API, which is
wrong guidance: the setting may already be enabled and the local MCP call
simply never arrived.

These tests drive the real panel methods against a stub wx module.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import logging
import sys
import types

import pytest

from kicad_plugin import llm_client


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
        yield panel, fake_wx
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


class _FakePanel:
    """Minimal receiver for the panel methods under test."""

    def __init__(self, panel, base_url: str = "http://127.0.0.1:8043") -> None:
        self._server_mgr = types.SimpleNamespace(base_url=base_url)
        self.entries: list[dict] = []
        self.renders = 0
        self._C_WARN_HEX = panel.AssistantPanel._C_WARN_HEX
        self._on_ipc_socket_checked = types.MethodType(
            panel.AssistantPanel._on_ipc_socket_checked, self
        )

    def _append_entry(self, entry: dict) -> None:
        self.entries.append(entry)

    def _render_conversation(self, *args, **kwargs) -> None:
        self.renders += 1


@pytest.fixture()
def panel_and_calls(monkeypatch, caplog):
    """Yield ``(panel, fake_wx, call_afters, panel_obj)`` with CallAfter recorded.

    The plugin configures the ``kicad_plugin`` logger with
    ``propagate = False``, so pytest's root-level caplog handler never sees
    panel records; attach its handler to the panel module logger instead.
    """
    with _panel_module() as (panel, fake_wx):
        call_afters: list[tuple[object, tuple]] = []

        def _fake_call_after(fn, *args):
            call_afters.append((fn, args))

        monkeypatch.setattr(fake_wx, "CallAfter", _fake_call_after)
        panel.log.addHandler(caplog.handler)
        try:
            panel_obj = _FakePanel(panel)
            yield panel, fake_wx, call_afters, panel_obj
        finally:
            panel.log.removeHandler(caplog.handler)


def _run_call_afters(call_afters) -> None:
    for fn, args in call_afters:
        fn(*args)


def test_transport_failure_does_not_blame_the_kicad_preference(
    panel_and_calls, monkeypatch, caplog
) -> None:
    panel, _fake_wx, call_afters, panel_obj = panel_and_calls
    monkeypatch.setattr(
        llm_client,
        "call_mcp_tool",
        lambda *args, **kwargs: {
            "success": False,
            "error": "MCP server unreachable: HTTP Error 502: No data received from server",
        },
    )

    with caplog.at_level(logging.WARNING, logger="kicad_plugin.ui.panel"):
        panel.AssistantPanel._check_ipc_socket(panel_obj)  # noqa: SLF001

    _run_call_afters(call_afters)
    assert panel_obj.entries == []
    assert call_afters == []
    assert "could not reach the MCP server" in caplog.text
    assert "HTTP Error 502" in caplog.text


def test_mcp_exception_does_not_blame_the_kicad_preference(
    panel_and_calls, monkeypatch, caplog
) -> None:
    panel, _fake_wx, call_afters, panel_obj = panel_and_calls

    def _boom(*args, **kwargs):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(llm_client, "call_mcp_tool", _boom)

    with caplog.at_level(logging.WARNING, logger="kicad_plugin.ui.panel"):
        panel.AssistantPanel._check_ipc_socket(panel_obj)  # noqa: SLF001

    _run_call_afters(call_afters)
    assert panel_obj.entries == []
    assert "Failed to check IPC socket via MCP tool" in caplog.text


def test_disconnected_tool_result_keeps_the_enable_api_hint(panel_and_calls, monkeypatch) -> None:
    panel, _fake_wx, call_afters, panel_obj = panel_and_calls
    monkeypatch.setattr(
        llm_client,
        "call_mcp_tool",
        lambda *args, **kwargs: {
            "success": True,
            "connected": False,
            "socket_path": "ipc:///tmp/kicad/api.sock",
            "error": "Connection refused",
        },
    )

    panel.AssistantPanel._check_ipc_socket(panel_obj)  # noqa: SLF001

    assert len(call_afters) == 1
    _run_call_afters(call_afters)
    assert len(panel_obj.entries) == 1
    text = panel_obj.entries[0]["text"]
    assert "Enable KiCad API" in text
    assert "ipc:///tmp/kicad/api.sock" in text
    assert panel_obj.renders == 1


def test_connected_result_shows_no_banner(panel_and_calls, monkeypatch) -> None:
    panel, _fake_wx, call_afters, panel_obj = panel_and_calls
    monkeypatch.setattr(
        llm_client,
        "call_mcp_tool",
        lambda *args, **kwargs: {
            "success": True,
            "connected": True,
            "socket_path": "ipc:///tmp/kicad/api.sock",
        },
    )

    panel.AssistantPanel._check_ipc_socket(panel_obj)  # noqa: SLF001

    assert len(call_afters) == 1
    _run_call_afters(call_afters)
    assert panel_obj.entries == []


def test_missing_backend_url_is_not_reported_as_disabled_api(panel_and_calls, caplog) -> None:
    panel, _fake_wx, call_afters, panel_obj = panel_and_calls
    panel_obj._server_mgr.base_url = ""

    with caplog.at_level(logging.WARNING, logger="kicad_plugin.ui.panel"):
        panel.AssistantPanel._check_ipc_socket(panel_obj)  # noqa: SLF001

    assert call_afters == []
    assert panel_obj.entries == []
    assert "not available" in caplog.text.lower()
