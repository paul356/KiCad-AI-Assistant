"""Contracts for the native jump-to-start/latest controls.

The shell no longer owns a floating button.  Two wx buttons sit in the
right-hand column around Send/Stop and call plain JS hooks in the WebView
(or scroll the HtmlWindow fallback).

The panel cannot be instantiated without wx, so the template and handlers are
extracted from the module source and evaluated directly.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
import textwrap
from types import SimpleNamespace

import kicad_plugin.ui.panel as _panel_module

_MODULE_SOURCE = Path(_panel_module.__file__).read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(_MODULE_SOURCE, filename=str(_panel_module.__file__))

_WX_VERTICAL = 1


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_MODULE_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"panel.py has no function named {name}")


def _source(node: ast.AST) -> str:
    return ast.get_source_segment(_MODULE_SOURCE, node) or ""


def _self_attr(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _build_shell_html() -> str:
    """Evaluate the template function without importing a wx-enabled panel."""
    node = _function("_build_shell_html")
    namespace = {
        "os": os,
        "log": logging.getLogger(__name__),
        "__file__": _panel_module.__file__,
    }
    exec(textwrap.dedent(_source(node)), namespace)
    return namespace["_build_shell_html"]()


_SHELL_HTML = _build_shell_html()


# --------------------------------------------------------------------------- #
# Shell HTML
# --------------------------------------------------------------------------- #


def test_shell_html_has_no_floating_jump_button() -> None:
    """The control moved to wx; no button markup, CSS, or install hook remains."""
    assert "<button" not in _SHELL_HTML
    assert "jump-latest" not in _SHELL_HTML
    assert "_updateJumpLatest" not in _SHELL_HTML
    assert "_installJumpLatest" not in _SHELL_HTML


def test_shell_html_inlines_both_jump_hooks() -> None:
    """The native buttons call these hooks through RunScript."""
    head, _ = _SHELL_HTML.split("<body>", 1)
    assert "window._jumpToLatest = function()" in head
    assert "window._jumpToStart = function()" in head
    assert "function _shouldScrollBottom()" in head  # follow logic still uses it


# --------------------------------------------------------------------------- #
# _build_ui structure
# --------------------------------------------------------------------------- #


def _side_vbox_order(build_ui: ast.FunctionDef) -> list[str]:
    adds: list[tuple[int, str]] = []
    for node in ast.walk(build_ui):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Add"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "side_vbox"
            and node.args
        ):
            continue
        name = _self_attr(node.args[0])
        if name is not None:
            adds.append((node.lineno, name))
    return [name for _, name in sorted(adds)]


def test_jump_buttons_surround_send_in_the_side_column() -> None:
    """Top control above Send/Stop, latest below it."""
    assert _side_vbox_order(_function("_build_ui")) == [
        "_status_dot",
        "_jump_top_btn",
        "_send_btn",
        "_jump_latest_btn",
    ]


def test_jump_buttons_have_tooltips_and_accessible_names() -> None:
    source = _source(_function("_build_ui"))
    for label in ("Jump to start", "Jump to latest"):
        assert f'.SetToolTip("{label}")' in source
        assert f'.SetName("{label}")' in source


def test_jump_buttons_bind_their_handlers() -> None:
    source = _source(_function("_build_ui"))
    assert "self._jump_top_btn.Bind(wx.EVT_BUTTON, self._on_jump_start)" in source
    assert "self._jump_latest_btn.Bind(wx.EVT_BUTTON, self._on_jump_latest)" in source


def test_jump_buttons_are_always_visible() -> None:
    """Visibility is not tied to scroll position; no Show/Enable toggling."""
    for name in ("_jump_top_btn", "_jump_latest_btn"):
        assert f"{name}.Show(" not in _MODULE_SOURCE
        assert f"{name}.Enable(" not in _MODULE_SOURCE


def test_jump_bitmap_helper_draws_both_directions() -> None:
    source = _source(_function("_make_jump_bitmap"))
    assert "GraphicsContext.Create" in source
    assert 'direction == "up"' in source
    assert "size: int = 28" in source


# --------------------------------------------------------------------------- #
# Handler behaviour
# --------------------------------------------------------------------------- #


class _FakeConvView:
    def __init__(self, bottom: int) -> None:
        self.bottom = bottom
        self.calls: list[tuple[int, int]] = []

    def Scroll(self, orientation: int, position: int) -> None:
        self.calls.append((orientation, position))

    def GetScrollRange(self, orientation: int) -> int:
        return self.bottom


class _FakePanel:
    def __init__(
        self,
        use_webview: bool,
        shell_loaded: bool,
        *,
        bottom: int = 700,
        script_error: bool = False,
    ) -> None:
        self._use_webview = use_webview
        self._shell_loaded = shell_loaded
        self._conv_view = _FakeConvView(bottom)
        self.scripts: list[str] = []
        self._script_error = script_error

    def _run_script(self, script: str) -> tuple[bool, None]:
        self.scripts.append(script)
        if self._script_error:
            raise RuntimeError("backend down")
        return True, None


def _handler(name: str):
    namespace = {
        "wx": SimpleNamespace(VERTICAL=_WX_VERTICAL),
        "log": logging.getLogger(__name__),
    }
    exec(textwrap.dedent(_source(_function(name))), namespace)
    return namespace[name]


def test_jump_start_uses_js_when_the_webview_is_ready() -> None:
    panel = _FakePanel(use_webview=True, shell_loaded=True)
    _handler("_on_jump_start")(panel, None)
    assert panel.scripts == ["_jumpToStart()"]
    assert panel._conv_view.calls == []


def test_jump_latest_uses_js_when_the_webview_is_ready() -> None:
    panel = _FakePanel(use_webview=True, shell_loaded=True)
    _handler("_on_jump_latest")(panel, None)
    assert panel.scripts == ["_jumpToLatest()"]
    assert panel._conv_view.calls == []


def test_jump_handlers_wait_for_the_shell() -> None:
    """A WebView that has not loaded the shell must not receive the call."""
    for name in ("_on_jump_start", "_on_jump_latest"):
        panel = _FakePanel(use_webview=True, shell_loaded=False)
        _handler(name)(panel, None)
        assert panel.scripts == []
        assert panel._conv_view.calls == []


def test_jump_handlers_fall_back_to_htmlwindow() -> None:
    start = _FakePanel(use_webview=False, shell_loaded=False)
    _handler("_on_jump_start")(start, None)
    assert start.scripts == []
    assert start._conv_view.calls == [(0, 0)]

    latest = _FakePanel(use_webview=False, shell_loaded=False, bottom=700)
    _handler("_on_jump_latest")(latest, None)
    assert latest.scripts == []
    assert latest._conv_view.calls == [(0, 700)]


def test_jump_latest_fallback_handles_an_empty_view() -> None:
    panel = _FakePanel(use_webview=False, shell_loaded=False, bottom=0)
    _handler("_on_jump_latest")(panel, None)
    assert panel._conv_view.calls == [(0, 0)]


def test_jump_handlers_swallow_backend_errors() -> None:
    for name in ("_on_jump_start", "_on_jump_latest"):
        panel = _FakePanel(use_webview=True, shell_loaded=True, script_error=True)
        _handler(name)(panel, None)  # must not raise
        assert len(panel.scripts) == 1
