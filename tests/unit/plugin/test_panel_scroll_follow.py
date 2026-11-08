"""Unit tests for the wx-free scroll-mode wiring of the conversation panel.

The panel has no checkbox: the WebView follows new output only when the
reader is already near the bottom.  A wx UI cannot be instantiated in this
environment, so the call-site contract is pinned with AST assertions over
the production module:

- no Auto-scroll checkbox remains;
- a send / session load/restore/clear force ``bottom``;
- streaming renders and ``_updateStream`` pass ``follow``;
- session replacements pass ``replace=True``;
- ``follow`` and ``replace`` survive the deferred-render replay.
"""

import ast
from pathlib import Path

import kicad_plugin.ui.panel as _panel_module

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


def _calls(fn: ast.FunctionDef, attr: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _is_self_attr(node.func, attr)
    ]


def _keyword_true(call: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def _keyword_name(call: ast.Call, name: str, value: str) -> bool:
    return any(
        keyword.arg == name and isinstance(keyword.value, ast.Name) and keyword.value.id == value
        for keyword in call.keywords
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


def _has_replace_true(call: ast.Call) -> bool:
    return _keyword_true(call, "replace")


def test_auto_scroll_checkbox_is_gone() -> None:
    """Option B removes the checkbox entirely; scroll following is positional."""
    assert "Auto-scroll" not in _MODULE_SOURCE
    assert "auto_scroll" not in _MODULE_SOURCE
    assert "wx.CheckBox" not in _MODULE_SOURCE


def test_update_conversation_maps_the_three_scroll_modes() -> None:
    """Force -> bottom, follow -> follow, otherwise none (no implicit fallback)."""
    fn = _function("_update_conversation")
    assigned: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "scroll" for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                assigned.add(node.value.value)
    assert {'"bottom"', '"follow"', '"none"'} <= assigned


def test_update_conversation_forwards_replace_and_follow_to_run_script() -> None:
    calls = _calls(_function("_update_conversation"), "_run_script")
    assert calls, "_update_conversation should call _run_script"
    assert any(
        _keyword_name(call, "replace", "replace") and _keyword_name(call, "follow", "follow")
        for call in calls
    ), "_update_conversation must forward replace=replace and follow=follow"


def test_send_forces_bottom() -> None:
    """An explicit send reveals the message the user just sent."""
    calls = _calls(_function("_send_request"), "_render_conversation")
    assert any(_keyword_true(call, "force_scroll_to_bottom") for call in calls)


def test_pdf_rerender_follows() -> None:
    assert "wx.CallAfter(self._render_conversation, follow=True)" in _MODULE_SOURCE


def test_streaming_renders_follow() -> None:
    """Every render path in the flush/refresh helpers must use follow mode.

    Dropping ``follow=True`` from any one call (e.g. the entries-changed
    path) would silently disable positional following there.
    """
    for method in ("_on_stream_flush", "_auto_refresh"):
        calls = _calls(_function(method), "_render_conversation")
        assert calls, f"{method} should call _render_conversation"
        for index, call in enumerate(calls):
            assert not call.args, f"{method} render #{index} must use keyword arguments"
            assert _keyword_true(call, "follow"), f"{method} render #{index} must pass follow=True"
            assert not any(keyword.arg == "force_scroll_to_bottom" for keyword in call.keywords), (
                f"{method} render #{index} must not force the bottom"
            )


def test_incremental_stream_update_passes_follow_mode() -> None:
    fn = _function("_incremental_stream_update")
    source = _source(fn)
    assert "_updateStream(" in source
    assert """scroll_arg = '"follow"' if follow else '"none"'""" in source

    calls = _calls(fn, "_run_script")
    assert calls, "_incremental_stream_update should call _run_script"
    assert any(_keyword_name(call, "follow", "follow") for call in calls)


def test_stop_forces_bottom() -> None:
    fn = _function("_on_stop")
    append_calls = _calls(fn, "_append_entry_js")
    render_calls = _calls(fn, "_render_conversation")
    assert any(_keyword_true(call, "force_scroll_to_bottom") for call in append_calls)
    assert any(_keyword_true(call, "force_scroll_to_bottom") for call in render_calls)


def test_conversation_replacement_renders_with_replace() -> None:
    """Every wholesale swap must tell the shell it is a replacement."""
    for method in (
        "_start_blank_session",
        "_restore_session_file",
        "_on_clear",
        "_autoload_session",
    ):
        fn = _function(method)
        assert _replaces_conversation(fn), f"{method} should replace the conversation"
        calls = _calls(fn, "_render_conversation")
        assert calls, f"{method} should re-render the conversation"
        assert any(_has_replace_true(call) for call in calls), (
            f"{method} must call _render_conversation(replace=True)"
        )


def test_run_script_stores_pending_flags_on_deferral() -> None:
    """A deferred render keeps its own force/replace/follow flags for replay."""
    fn = _function("_run_script")
    deferral_branches = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If) and "_try_acquire_render_lock" in _source(node.test)
    ]
    assert deferral_branches, "_run_script should gate on _try_acquire_render_lock"

    expected = {
        "_pending_force_scroll_to_bottom",
        "_pending_replace",
        "_pending_follow",
    }
    for branch in deferral_branches:
        assigned = {
            target.attr
            for node in ast.walk(branch)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert expected <= assigned, "deferral must store all pending render flags"


def test_pending_render_replays_stored_flags() -> None:
    """The releasing call must not donate its flags to the deferred request."""
    fn = _function("_process_pending_render")
    params = {arg.arg for arg in fn.args.args + fn.args.kwonlyargs}
    assert "force_scroll_to_bottom" not in params
    assert "replace" not in params
    assert "follow" not in params

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
    assert len(replay_args) == 1, "expected exactly one replay call"
    arg_names: list[str] = []
    for arg in replay_args[0]:
        assert isinstance(arg, ast.Attribute), "replay arguments should be pending attributes"
        arg_names.append(arg.attr)

    # The replayed call is _update_conversation(force, replace, follow).
    assert arg_names == [
        "_pending_force_scroll_to_bottom",
        "_pending_replace",
        "_pending_follow",
    ], "replay must pass force, replace, follow in that order"

    # Evaluate the recorded deferrals in the call's actual argument order.
    # Swapping replace/follow would turn the second tuple into (True, True, False).
    def replayed(force: bool, replace: bool, follow: bool) -> tuple[bool, ...]:
        stored = {
            "_pending_force_scroll_to_bottom": force,
            "_pending_replace": replace,
            "_pending_follow": follow,
        }
        return tuple(stored[name] for name in arg_names)

    assert replayed(False, True, True) == (False, True, True)
    assert replayed(True, False, True) == (True, False, True)
