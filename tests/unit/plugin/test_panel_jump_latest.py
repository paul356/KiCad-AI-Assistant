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
import re
import textwrap
from types import SimpleNamespace

import pytest

import kicad_plugin.ui.panel as _panel_module
from kicad_plugin.ui.panel import _jump_chevron_geometry

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


def _sizer_order(build_ui: ast.FunctionDef, sizer: str) -> list[str]:
    adds: list[tuple[int, str]] = []
    for node in ast.walk(build_ui):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Add"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == sizer
            and node.args
        ):
            continue
        name = _self_attr(node.args[0])
        if name is None and isinstance(node.args[0], ast.Name):
            name = node.args[0].id
        if name is not None:
            adds.append((node.lineno, name))
    return [name for _, name in sorted(adds)]


def _send_row_layout(build_ui: ast.FunctionDef) -> list[str]:
    """Return send-row children/spacers in source order."""
    items: list[tuple[int, str]] = []
    for node in ast.walk(build_ui):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "send_row"
        ):
            continue
        if node.func.attr == "Add" and node.args:
            name = _self_attr(node.args[0])
            if name is not None:
                items.append((node.lineno, name))
        elif node.func.attr == "AddSpacer" and node.args:
            value = node.args[0]
            if isinstance(value, ast.Constant):
                items.append((node.lineno, f"spacer:{value.value}"))
    return [item for _, item in sorted(items)]


def test_jump_arrows_stack_above_and_below_send() -> None:
    """Top arrow, send row, bottom arrow, left-aligned at 2 px spacing."""
    build_ui = _function("_build_ui")
    assert _sizer_order(build_ui, "side_vbox") == [
        "_jump_top_btn",
        "send_row",
        "_jump_latest_btn",
    ]
    source = _source(build_ui)
    assert "side_vbox.Add(self._jump_top_btn, 0, wx.ALIGN_LEFT | wx.BOTTOM, 2)" in source
    assert "side_vbox.Add(send_row, 0, wx.ALIGN_LEFT | wx.BOTTOM, 2)" in source
    assert "side_vbox.Add(self._jump_latest_btn, 0, wx.ALIGN_LEFT)" in source
    side_children = _sizer_order(build_ui, "side_vbox")
    assert side_children.count("_jump_top_btn") == 1
    assert side_children.count("send_row") == 1
    assert side_children.count("_jump_latest_btn") == 1
    assert "_status_dot" not in side_children


def test_send_row_puts_the_status_dot_after_send() -> None:
    """The dot is 16 px from the chip and followed by a 12 px pad."""
    build_ui = _function("_build_ui")
    assert _send_row_layout(build_ui) == ["_send_btn", "_status_dot", "spacer:12"]
    source = _source(build_ui)
    assert "send_row = wx.BoxSizer(wx.HORIZONTAL)" in source
    assert "send_row.Add(self._status_dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16)" in source
    assert "send_row.AddSpacer(12)" in source
    assert "_status_dot" not in _sizer_order(build_ui, "side_vbox")
    assert "_jump_top_btn" not in _sizer_order(build_ui, "send_row")
    assert "_jump_latest_btn" not in _sizer_order(build_ui, "send_row")


def test_status_dot_is_centred_in_the_trailing_space() -> None:
    """16 px left equals 12 px spacer + the surrounding 4 px panel margin."""
    build_ui = _function("_build_ui")
    source = _source(build_ui)
    dot_add = re.search(
        r"send_row\.Add\(self\._status_dot, 0, wx\.ALIGN_CENTER_VERTICAL \| "
        r"wx\.LEFT, (\d+)\)",
        source,
    )
    spacer = re.search(r"send_row\.AddSpacer\((\d+)\)", source)
    margin = re.search(r"vbox\.Add\(hbox, 0, wx\.ALL \| wx\.EXPAND, (\d+)\)", source)
    assert dot_add is not None and spacer is not None and margin is not None
    leading = int(dot_add.group(1))
    trailing = int(spacer.group(1))
    panel_margin = int(margin.group(1))
    dot_source = _source(_function("_make_status_dot_bitmap"))
    dot_size = int(re.search(r"size: int = (\d+)", dot_source).group(1))
    assert (leading, trailing, panel_margin) == (16, 12, 4)
    assert leading + dot_size / 2 == 21.0  # chip right → dot centre
    assert trailing + panel_margin == leading


def test_jump_buttons_have_tooltips_and_accessible_names() -> None:
    source = _source(_function("_build_ui"))
    for widget, label in (
        ("_jump_top_btn", "Jump to start"),
        ("_jump_latest_btn", "Jump to latest"),
    ):
        assert f'self.{widget}.SetToolTip("{label}")' in source
        assert f'self.{widget}.SetName("{label}")' in source


def test_jump_buttons_bind_their_handlers() -> None:
    source = _source(_function("_build_ui"))
    assert "self._jump_top_btn.Bind(wx.EVT_BUTTON, self._on_jump_start)" in source
    assert "self._jump_latest_btn.Bind(wx.EVT_BUTTON, self._on_jump_latest)" in source


def test_jump_buttons_are_always_visible() -> None:
    """Both buttons exist and no visibility call touches them."""
    source = _source(_function("_build_ui"))
    for name in ("_jump_top_btn", "_jump_latest_btn"):
        assert f"self.{name} = wx.BitmapButton(" in source
        for method in ("Show(", "Hide(", "Enable(", "Disable("):
            assert f"self.{name}.{method}" not in _MODULE_SOURCE


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("up", ((18.0, 5.5), (12.0, 12.5), (24.0, 12.5))),
        ("down", ((18.0, 12.5), (12.0, 5.5), (24.0, 5.5))),
    ],
)
def test_jump_chevron_geometry(direction: str, expected: tuple[tuple[float, float], ...]) -> None:
    assert _jump_chevron_geometry(36, 18, direction) == expected


@pytest.mark.parametrize("direction", ["Up", "top", ""])
def test_jump_chevron_geometry_rejects_unknown_directions(direction: str) -> None:
    with pytest.raises(ValueError):
        _jump_chevron_geometry(36, 18, direction)


def test_jump_bitmap_draws_distinct_chevrons() -> None:
    source = _source(_function("_make_jump_bitmap"))
    assert "GraphicsContext.Create" in source
    assert "wx.Pen(colour, 3)" in source
    assert "gc.StrokePath(path)" in source
    assert "width: int = 36" in source
    assert "height: int = 18" in source
    assert "_new_icon_bitmap(width, bg, height)" in source
    up = _jump_chevron_geometry(36, 18, "up")
    down = _jump_chevron_geometry(36, 18, "down")
    assert up != down
    assert up[0][0] == down[0][0] == 18.0  # same centre column
    assert up[1][0] == down[1][0] == 12.0
    assert up[2][0] == down[2][0] == 24.0
    assert up[1][1] == up[2][1] == 12.5
    assert down[1][1] == down[2][1] == 5.5
    assert up[0][1] < up[1][1] and down[0][1] > down[1][1]  # opposite points


def test_send_and_stop_use_the_same_36px_chip() -> None:
    send = _source(_function("_make_send_bitmap"))
    stop = _source(_function("_make_stop_bitmap"))
    chip = "AddRoundedRectangle(0.5, 0.5, size - 1, size - 1, 8)"
    for source in (send, stop):
        assert "size: int = 36" in source
        assert chip in source
    assert "gc.SetPen(wx.Pen(wx.Colour(28, 76, 184), 1))" in send
    assert "gc.SetPen(wx.Pen(wx.Colour(185, 56, 56), 1))" in stop
    assert "wx.Colour(34, 85, 204)" in send
    assert "wx.Colour(220, 70, 70)" in stop


def test_status_dot_is_ten_pixels_with_a_ring() -> None:
    source = _source(_function("_make_status_dot_bitmap"))
    assert "size: int = 10" in source
    assert "wx.Colour(214, 218, 224)" in source
    assert "DrawEllipse(0.5, 0.5, size - 1, size - 1)" in source


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
