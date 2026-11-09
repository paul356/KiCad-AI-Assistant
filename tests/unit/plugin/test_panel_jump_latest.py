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
    """One button with a down arrow and an accessible name."""
    _, body = _SHELL_HTML.split("<body>", 1)
    assert body.count("<button") == 1
    button = body.split("<button", 1)[1].split("</button>", 1)[0]
    assert "id='jump-latest'" in button
    assert "type='button'" in button
    assert "aria-label='Jump to latest message'" in button
    assert "title='Jump to latest message'" in button
    assert "↓" in button


def test_jump_latest_button_sits_inside_the_body() -> None:
    """The button must be a body child, never markup injected into the head."""
    head, body = _SHELL_HTML.split("<body>", 1)
    assert "<button" in body
    assert "<button" not in head


def test_jump_latest_css_rule_is_in_the_style_block() -> None:
    """The stylesheet hides and floats the button; exact styling is free."""
    style = _SHELL_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    rule = style.split("#jump-latest{", 1)[1].split("}", 1)[0]
    assert "display:none" in rule
    assert "position:fixed" in rule


def test_shell_html_inlines_the_jump_latest_hooks() -> None:
    """shell.js is inlined into the head, hooks included."""
    head, _ = _SHELL_HTML.split("<body>", 1)
    assert "function _updateJumpLatest()" in head
    assert "function _jumpToLatest()" in head
    assert "btn.addEventListener('click', _jumpToLatest)" in head
