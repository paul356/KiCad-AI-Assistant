"""Static contract for the jump-to-latest button in ``_build_shell_html()``.

The panel cannot be instantiated without wx, so the template function is
extracted from the module source and evaluated with its two globals
(``os`` and ``log``).  The resulting HTML is exactly what ``SetPage`` loads.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
import textwrap

import kicad_plugin.ui.panel as _panel_module

_MODULE_SOURCE = Path(_panel_module.__file__).read_text(encoding="utf-8")

_BUTTON = (
    "<button id='jump-latest' type='button' "
    "aria-label='Jump to latest message' title='Jump to latest message'>↓</button>"
)


def _build_shell_html() -> str:
    """Evaluate the template function without importing a wx-enabled panel."""
    tree = ast.parse(_MODULE_SOURCE, filename=str(_panel_module.__file__))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_shell_html":
            source = ast.get_source_segment(_MODULE_SOURCE, node)
            assert source is not None
            namespace = {
                "os": os,
                "log": logging.getLogger(__name__),
                "__file__": _panel_module.__file__,
            }
            exec(textwrap.dedent(source), namespace)
            return namespace["_build_shell_html"]()
    raise AssertionError("panel.py has no function named _build_shell_html")


_SHELL_HTML = _build_shell_html()


def test_shell_html_contains_the_jump_latest_button() -> None:
    """One button, a down arrow, no emoji or icon font."""
    assert _SHELL_HTML.count("<button id='jump-latest'") == 1
    assert _BUTTON in _SHELL_HTML


def test_jump_latest_button_sits_inside_the_body() -> None:
    """The button must be a body child, never markup injected into the head."""
    head, body = _SHELL_HTML.split("<body>", 1)
    assert _BUTTON in body
    assert "<button id='jump-latest'" not in head


def test_jump_latest_css_rule_is_in_the_style_block() -> None:
    """The floating placement and resting shadow live in the shell stylesheet."""
    style, rest = _SHELL_HTML.split("<style>", 1)
    style, _ = rest.split("</style>", 1)
    assert "#jump-latest{display:none;position:fixed;right:14px;bottom:14px" in style
    assert "border:1px solid #B8BEC9" in style
    assert "box-shadow:" in style


def test_shell_html_inlines_the_jump_latest_hooks() -> None:
    """shell.js is inlined into the head, hooks included."""
    head, _ = _SHELL_HTML.split("<body>", 1)
    assert "function _updateJumpLatest()" in head
    assert "function _jumpToLatest()" in head
    assert "btn.addEventListener('click', _jumpToLatest)" in head
