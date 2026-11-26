"""The async dispatch wrapper, executed by node (issue #342 review).

``_wrap_script`` is JavaScript generated in Python, so a Python-level string
check cannot prove that an expression's value reaches the exec reply.  These
tests run the generated wrapper under node with a stubbed ``kcaa``/``document``
and assert the posted messages.

Statement mode discards the block's completion value, so its reply must carry
``result: null``; expression mode must carry the value.  Both must survive a
``js`` that ends in a ``//`` line comment, which would otherwise comment out
the wrapper tail.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from kicad_plugin.ui.panel import _wrap_script

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not installed")

_HARNESS = """
var posted = [];
var kcaa = {{postMessage: function (s) {{ posted.push(JSON.parse(s)); }}}};
var document = {{
    getElementById: function (id) {{
        return id === 'conversation' ? {{childElementCount: 3}} : null;
    }}
}};
var _updateConversation = function () {{ return 'ok:2'; }};
{scripts}
console.log(JSON.stringify(posted));
"""


def _exec(*snippets: str) -> list[dict]:
    """Run wrapped scripts in node and return the messages they posted."""
    program = _HARNESS.format(scripts="\n".join(snippets))
    proc = subprocess.run(
        [_NODE, "-e", program],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_PROBE = (
    "JSON.stringify({conv: !!document.getElementById('conversation'), "
    "js: typeof _updateConversation === 'function', font: 'Arial'})"
)


def test_expression_scripts_report_their_value() -> None:
    """The shell and diag probes are expressions: their values must survive."""
    replies = _exec(
        _wrap_script(1, _PROBE, expression=True) + ";",
        _wrap_script(2, "_updateConversation()", expression=True) + ";",
    )

    assert [reply["seq"] for reply in replies] == [1, 2]
    assert all(reply["kind"] == "exec" for reply in replies)
    assert all(reply["ok"] is True for reply in replies)
    assert json.loads(replies[0]["result"]) == {
        "conv": True,
        "js": True,
        "font": "Arial",
    }, "the probe reply must carry its JSON string"
    assert replies[1]["result"] == "ok:2", "an expression's value is reported"


def test_statement_scripts_report_a_null_result() -> None:
    replies = _exec(
        _wrap_script(3, "_updateConversation()") + ";",
        _wrap_script(4, "var w = 1; if (w) { w = 2; }") + ";",
    )

    assert [reply["seq"] for reply in replies] == [3, 4]
    assert all(reply["ok"] is True for reply in replies)
    assert [reply["result"] for reply in replies] == [None, None], (
        "a statement block has no value; the reply must say null, not drop the key"
    )
    assert all("result" in reply for reply in replies)


def test_trailing_line_comment_does_not_swallow_the_reply() -> None:
    """A `js` ending in `//` must not comment out the wrapper tail."""
    replies = _exec(
        _wrap_script(5, "_updateConversation() // trailing comment") + ";",
        _wrap_script(6, "var w = 1; // trailing comment") + ";",
        _wrap_script(7, "_updateConversation() // trailing", expression=True) + ";",
    )

    assert [reply["seq"] for reply in replies] == [5, 6, 7]
    assert [reply["ok"] for reply in replies] == [True, True, True]
    assert [reply["result"] for reply in replies] == [None, None, "ok:2"]


def test_throwing_script_reports_ok_false_with_the_message() -> None:
    replies = _exec(_wrap_script(8, "(()=>{throw new Error('boom');})()", expression=True) + ";")

    assert replies[0]["ok"] is False
    assert replies[0]["error"] == "boom"
    assert replies[0]["result"] is None
