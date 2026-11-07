"""Unit tests for the wx-free auto-scroll decision helper and its wiring.

The panel only pins the conversation to the newest entry when a turn is
producing output AND the user's "Auto-scroll" checkbox is checked.

The wx UI cannot be instantiated in this environment, so the plumbing between
the session-replacement call sites and the WebView render protocol is pinned
with AST assertions over the production module.
"""

import ast
from pathlib import Path

import pytest

import kicad_plugin.ui.panel as _panel_module
from kicad_plugin.ui.panel import _should_follow_output

_MODULE_SOURCE = Path(_panel_module.__file__).read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(_MODULE_SOURCE, filename=str(_panel_module.__file__))


def _source(node: ast.AST) -> str:
    return ast.get_source_segment(_MODULE_SOURCE, node) or ""


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_MODULE_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"panel.py has no function named {name}")


def _is_self_attr(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == name
    )


def _clears_conv_entries(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "clear"
        and _is_self_attr(call.func.value, "_conv_entries")
    )


def _replaces_conversation(fn: ast.FunctionDef) -> bool:
    """True when the function swaps out the whole conversation."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            _is_self_attr(target, "_conv_entries") for target in node.targets
        ):
            return True
        if isinstance(node, ast.Call) and _clears_conv_entries(node):
            return True
    return False


def _render_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _is_self_attr(node.func, "_render_conversation")
    ]


def _has_replace_true(call: ast.Call) -> bool:
    return any(
        keyword.arg == "replace"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


@pytest.mark.parametrize(
    ("auto_scroll", "follow", "expected"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
        (False, False, False),
    ],
)
def test_should_follow_output(auto_scroll: bool, follow: bool, expected: bool) -> None:
    assert _should_follow_output(auto_scroll, follow) is expected


@pytest.mark.parametrize(
    "method",
    [
        "_start_blank_session",
        "_restore_session_file",
        "_on_clear",
        "_autoload_session",
    ],
)
def test_conversation_replacement_renders_with_replace(method: str) -> None:
    """Every wholesale conversation swap must tell the shell it is a replacement.

    Without ``replace=True`` the shell restores the previous session's scroll
    offset and expanded tool rows (the round-1 inheritance defect).
    """
    fn = _function(method)
    assert _replaces_conversation(fn), f"{method} should replace the conversation"
    calls = _render_calls(fn)
    assert calls, f"{method} should re-render the conversation"
    assert any(_has_replace_true(call) for call in calls), (
        f"{method} must call _render_conversation(replace=True)"
    )


def test_update_conversation_forwards_replace_to_run_script() -> None:
    """The replacement flag must reach the render protocol, not just the JS call."""
    calls = [
        node
        for node in ast.walk(_function("_update_conversation"))
        if isinstance(node, ast.Call) and _is_self_attr(node.func, "_run_script")
    ]
    assert calls, "_update_conversation should call _run_script"
    assert any(
        any(
            keyword.arg == "replace"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "replace"
            for keyword in call.keywords
        )
        for call in calls
    ), "_update_conversation must forward replace=replace to _run_script"


def test_run_script_stores_pending_flags_on_deferral() -> None:
    """A deferred render keeps its own flags for replay."""
    fn = _function("_run_script")
    deferral_branches = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If) and "_try_acquire_render_lock" in _source(node.test)
    ]
    assert deferral_branches, "_run_script should gate on _try_acquire_render_lock"

    for branch in deferral_branches:
        assigned = {
            target.attr
            for node in ast.walk(branch)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert {
            "_pending_force_scroll_to_bottom",
            "_pending_replace",
        } <= assigned, "deferral must store both pending render flags"


def test_pending_render_replays_stored_flags() -> None:
    """The releasing call must not donate its flags to the deferred request."""
    fn = _function("_process_pending_render")
    params = {arg.arg for arg in fn.args.args + fn.args.kwonlyargs}
    assert "force_scroll_to_bottom" not in params
    assert "replace" not in params

    replay_args: list[list[ast.AST]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _is_self_attr(node.func, "_update_conversation"):
            replay_args.append(list(node.args))
            continue
        for index, arg in enumerate(node.args):
            if _is_self_attr(arg, "_update_conversation"):
                replay_args.append(list(node.args[index + 1 :]))
                break

    assert replay_args, "_process_pending_render should replay via _update_conversation"
    assert any(
        any(_is_self_attr(arg, "_pending_force_scroll_to_bottom") for arg in args)
        and any(_is_self_attr(arg, "_pending_replace") for arg in args)
        for args in replay_args
    ), "replay must consume the stored pending flags"
