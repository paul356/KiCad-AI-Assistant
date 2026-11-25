"""Regression tests for issue #339: renders during a modal dialog.

``wxWebView::RunScript`` pumps the wx event loop.  While a KiCad modal or
quasi-modal (``ShowQuasiModal``) dialog owns a nested loop, that pump runs
inside the dialog's loop, WebKit creates a JS context and GTK aborts the
process.  The panel must therefore defer conversation renders while such a
dialog is open and replay them once it closes.

The panel cannot be instantiated without wx, so the pure blocker predicate is
tested directly and the render protocol is exercised on an ``object.__new__``
instance of the real class, imported with a permissive fake ``wx`` module
(same technique as ``test_panel_icons.py``).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import time
import types
from types import SimpleNamespace
from unittest import mock

import pytest

import kicad_plugin.ui.panel as _panel_module
from kicad_plugin.ui.panel import _render_blocking_window


class _Window:
    """Non-dialog top-level window (frame/panel)."""

    def __init__(self, *, shown: bool = True, enabled: bool = True) -> None:
        self._shown = shown
        self._enabled = enabled

    def IsShown(self) -> bool:
        return self._shown

    def IsEnabled(self) -> bool:
        return self._enabled


class _Dialog(_Window):
    """Dialog-like window: ``IsModal``/``ShowModal`` mark the wx type."""

    def __init__(
        self,
        *,
        shown: bool = True,
        enabled: bool = True,
        modal: bool = True,
        parent: object | None = None,
    ) -> None:
        super().__init__(shown=shown, enabled=enabled)
        self._modal = modal
        self._parent = parent

    def IsModal(self) -> bool:
        return self._modal

    def ShowModal(self) -> int:
        return 0

    def GetParent(self) -> object | None:
        return self._parent


def _not_plugin(_window: object) -> bool:
    return False


# --------------------------------------------------------------------------- #
# Pure blocking predicate
# --------------------------------------------------------------------------- #


def test_empty_window_list_does_not_block() -> None:
    assert _render_blocking_window([], _not_plugin) is None


def test_shown_modal_dialog_blocks() -> None:
    dialog = _Dialog(modal=True)
    assert _render_blocking_window([dialog], _not_plugin) is dialog


def test_hidden_dialog_does_not_block() -> None:
    assert _render_blocking_window([_Dialog(shown=False)], _not_plugin) is None


def test_non_dialog_top_level_window_does_not_block() -> None:
    assert _render_blocking_window([_Window()], _not_plugin) is None


def test_plugin_window_does_not_block_by_itself() -> None:
    panel = _Window(shown=True, enabled=True)
    assert _render_blocking_window([panel], lambda w: w is panel) is None


def test_quasi_modal_blocks_through_disabled_plugin_window() -> None:
    """A truly modal dialog disables other top-level windows; ours blocks."""
    quasi = _Dialog(modal=False)
    plugin = _Window(shown=True, enabled=False)
    assert _render_blocking_window([quasi, plugin], lambda w: w is plugin) is plugin


def test_quasi_modal_blocks_through_disabled_dialog_parent() -> None:
    """Issue #339: ShowQuasiModal disables only the dialog's parent."""
    editor = _Window(shown=True, enabled=False)
    quasi = _Dialog(modal=False, parent=editor)
    plugin = _Window(shown=True, enabled=True)
    assert _render_blocking_window([quasi, plugin], lambda w: w is plugin) is quasi


def test_non_modal_dialog_with_enabled_parent_does_not_block() -> None:
    dialog = _Dialog(modal=False, parent=_Window(shown=True, enabled=True))
    assert _render_blocking_window([dialog], _not_plugin) is None


def test_hidden_quasi_modal_dialog_does_not_block() -> None:
    dialog = _Dialog(shown=False, modal=False, parent=_Window(enabled=False))
    assert _render_blocking_window([dialog], _not_plugin) is None


def test_non_modal_dialog_without_parent_does_not_block() -> None:
    """No disabled parent means no observable nested loop; stay conservative."""
    assert _render_blocking_window([_Dialog(modal=False, parent=None)], _not_plugin) is None


def test_disabled_non_plugin_window_does_not_block() -> None:
    """The disabled-window signal is only meaningful for our own windows."""
    assert _render_blocking_window([_Window(enabled=False)], _not_plugin) is None


def test_window_that_vanishes_during_probe_is_ignored() -> None:
    """A destroyed wx window raises on probing; the scan must continue."""

    class _Vanishing(_Window):
        def IsShown(self) -> bool:
            raise RuntimeError("window destroyed")

    dialog = _Dialog(modal=True)
    assert _render_blocking_window([_Vanishing(), dialog], _not_plugin) is dialog


# --------------------------------------------------------------------------- #
# Render protocol with a fake wx
# --------------------------------------------------------------------------- #


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _FakeColour:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _FakeView:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.async_scripts: list[str] = []
        self.diag_scripts: list[str] = []

    def RunScript(self, js: str) -> tuple[bool, str]:
        self.scripts.append(js)
        return True, "ok"

    def RunScriptAsync(self, js: str) -> bool:
        if "kind: 'diag'" in js:
            self.diag_scripts.append(js)
        else:
            self.async_scripts.append(js)
        return True

    def SetFocus(self) -> None:
        pass


class _SyncOnlyView:
    """Old webkit1 backend: no RunScriptAsync."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def RunScript(self, js: str) -> tuple[bool, str]:
        self.scripts.append(js)
        return True, "ok"

    def SetFocus(self) -> None:
        pass


class _ScriptMessage:
    def __init__(self, payload) -> None:
        self._payload = payload if isinstance(payload, str) else json.dumps(payload)

    def GetString(self) -> str:
        return self._payload


class _FakeControl:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.shown = True

    def Show(self, flag: bool) -> None:
        self.shown = flag

    def SetFocus(self) -> None:
        pass

    def SetValue(self, value: str) -> None:
        self.value = value

    def GetValue(self) -> str:
        return self.value


class _FakeLabel:
    def __init__(self, label: str = "1/1") -> None:
        self.label = label
        self.shown = True

    def Show(self, flag: bool) -> None:
        self.shown = flag

    def SetLabel(self, label: str) -> None:
        self.label = label


class _FakeTimer:
    def __init__(self) -> None:
        self.running = False
        self.starts: list[int] = []
        self.stops = 0

    def Start(self, milliseconds: int) -> None:
        self.running = True
        self.starts.append(milliseconds)

    def Stop(self) -> None:
        self.running = False
        self.stops += 1

    def IsRunning(self) -> bool:
        return self.running


class _DestroyEvent:
    def __init__(self, source: object) -> None:
        self._source = source
        self.skipped = False

    def GetEventObject(self) -> object:
        return self._source

    def Skip(self) -> None:
        self.skipped = True


class _CloseEvent:
    def __init__(self, can_veto: bool = True) -> None:
        self._can_veto = can_veto
        self.vetoed = False

    def CanVeto(self) -> bool:
        return self._can_veto

    def Veto(self) -> None:
        self.vetoed = True


def _build_fake_wx(windows: list[object]) -> types.ModuleType:
    fake_wx = types.ModuleType("wx")
    fake_wx.Frame = _Dummy
    fake_wx.FileDropTarget = _Dummy
    fake_wx.Colour = _FakeColour
    fake_wx.GetTopLevelWindows = lambda: list(windows)
    # Replay runs synchronously so the test observes the render it triggers.
    fake_wx.CallAfter = lambda callback, *args, **kwargs: callback(*args, **kwargs)
    fake_wx.html = types.ModuleType("wx.html")
    fake_wx.html2 = types.ModuleType("wx.html2")
    fake_wx.__getattr__ = lambda name: _Dummy
    return fake_wx


@pytest.fixture
def panel_mod():
    """Import panel.py with a fake wx whose top-level-window list is mutable."""
    windows: list[object] = []
    fake_wx = _build_fake_wx(windows)
    module_names = ("wx", "wx.html", "wx.html2", "kicad_plugin.ui.panel")
    missing = object()
    saved_modules = {name: sys.modules.get(name, missing) for name in module_names}
    ui_pkg = sys.modules.get("kicad_plugin.ui")
    saved_panel_attr = getattr(ui_pkg, "panel", None) if ui_pkg is not None else None
    try:
        sys.modules["wx"] = fake_wx
        sys.modules["wx.html"] = fake_wx.html
        sys.modules["wx.html2"] = fake_wx.html2
        sys.modules.pop("kicad_plugin.ui.panel", None)
        panel = importlib.import_module("kicad_plugin.ui.panel")
        yield panel, windows
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


def _make_panel(panel, timer: _FakeTimer, view: _FakeView) -> object:
    obj = object.__new__(panel.AssistantPanel)
    obj._use_webview = True
    obj._js_running = False
    obj._render_pending = False
    obj._pending_force_scroll_to_bottom = False
    obj._pending_replace = False
    obj._pending_follow = False
    obj._shell_loaded = True
    obj._page_loading = False
    obj._conv_entries = []
    obj._stream_wrapper_visible = False
    obj._modal_render_timer = timer
    obj._search_timer = _FakeTimer()
    obj._search_query = ""
    obj._search_seq = 0
    obj._search_async_ok = True
    obj._search_script_running = False
    obj._last_diag_ts = 0.0
    obj._search_sent_at = {}
    obj._conv_view = view
    return obj


def _plugin_window(panel, *, shown: bool = True, enabled: bool = True) -> object:
    window = object.__new__(panel.AssistantPanel)
    window.IsShown = lambda: shown
    window.IsEnabled = lambda: enabled
    return window


def _make_search_panel(panel, timer: _FakeTimer, view: _FakeView) -> object:
    obj = _make_panel(panel, timer, view)
    obj._search_ctrl = _FakeControl("needle")
    obj._search_prev_btn = _FakeControl()
    obj._search_next_btn = _FakeControl()
    obj._search_close_btn = _FakeControl()
    obj._search_count_label = _FakeLabel("1/3")
    obj._ui_panel = SimpleNamespace(Layout=lambda: None)
    return obj


def test_modal_dialog_defers_render_and_replays_after_close(panel_mod) -> None:
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_panel(panel, timer, view)
    windows.append(_Dialog(modal=True))

    obj._update_conversation(force_scroll_to_bottom=True, replace=True, follow=True)

    assert view.scripts == [], "RunScript must not run inside the dialog's loop"
    assert obj._render_pending is True
    assert obj._pending_force_scroll_to_bottom is True
    assert obj._pending_replace is True
    assert obj._pending_follow is True
    assert timer.running is True
    assert timer.starts == [_panel_module._MODAL_RENDER_POLL_MS]

    # Another blocked render must not stack additional timer starts.
    obj._update_conversation(force_scroll_to_bottom=True, replace=True, follow=True)
    assert timer.starts == [_panel_module._MODAL_RENDER_POLL_MS]

    # Dialog still open: the tick leaves the render deferred.
    obj._on_modal_render_tick(None)
    assert view.scripts == []
    assert obj._render_pending is True
    assert timer.running is True

    # Dialog closed: exactly one replay through the normal render path.
    windows.clear()
    obj._on_modal_render_tick(None)

    assert len(view.scripts) == 1
    js = view.scripts[0]
    assert js.startswith("_updateConversation(")
    assert '"bottom"' in js
    assert js.endswith(", true)")
    assert obj._render_pending is False
    assert timer.running is False


def test_quasi_modal_defers_render_with_enabled_plugin_window(panel_mod) -> None:
    """Issue #339 scenario: only the PCB editor is disabled, panel stays enabled."""
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_panel(panel, timer, view)
    editor = _Window(shown=True, enabled=False)
    windows.extend(
        [
            _Dialog(modal=False, parent=editor),
            _plugin_window(panel, shown=True, enabled=True),
        ]
    )

    obj._update_conversation(force_scroll_to_bottom=True)

    assert view.scripts == [], "RunScript must not run inside the quasi-modal loop"
    assert obj._render_pending is True
    assert obj._pending_force_scroll_to_bottom is True
    assert timer.running is True


def test_probe_is_not_gated_by_modal_dialog(panel_mod) -> None:
    """Shell verification probes must run even while a dialog is open."""
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_panel(panel, timer, view)
    windows.append(_Dialog(modal=True))

    ok, result = obj._run_script("1 + 1", probe=True)

    assert ok is True
    assert result == "ok"
    assert view.scripts == ["1 + 1"]
    assert obj._render_pending is False
    assert timer.running is False


def test_replay_timer_stops_when_nothing_is_pending(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    obj = _make_panel(panel, timer, _FakeView())
    timer.Start(_panel_module._MODAL_RENDER_POLL_MS)

    obj._process_pending_render()

    assert timer.running is False


def test_destroy_stops_replay_timer(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    obj = _make_panel(panel, timer, _FakeView())
    obj._update_cancel_event = None
    obj._update_progress_timer = None
    obj._project_watch_timer = None
    obj._finalise_and_save_on_teardown = lambda: None
    obj._server_mgr = SimpleNamespace(stop=lambda: None)
    timer.Start(_panel_module._MODAL_RENDER_POLL_MS)
    event = _DestroyEvent(obj)

    obj._on_destroy(event)

    assert timer.running is False
    assert event.skipped is True


def test_close_stops_replay_timer(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    obj = _make_panel(panel, timer, _FakeView())
    obj.Hide = lambda: None
    timer.Start(_panel_module._MODAL_RENDER_POLL_MS)
    event = _CloseEvent(can_veto=True)

    obj._on_close(event)

    assert event.vetoed is True
    assert timer.running is False


# --------------------------------------------------------------------------- #
# Search: async RunScriptAsync + script messages (no sync RunScript, no gate)
# --------------------------------------------------------------------------- #

_ASYNC_SEARCH_CASES = [
    ("_on_find", "_clearFind(1)"),
    ("_on_search_close", "_clearFind(1)"),
    ("_on_search_next", "_findNext(1)"),
    ("_on_search_prev", "_findPrev(1)"),
]


@pytest.mark.parametrize(("handler", "expected_js"), _ASYNC_SEARCH_CASES)
def test_search_script_runs_async(panel_mod, handler, expected_js) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)

    getattr(obj, handler)(None)

    assert view.scripts == [], "search must not use synchronous RunScript"
    assert view.async_scripts == [expected_js]


@pytest.mark.parametrize(("handler", "expected_js"), _ASYNC_SEARCH_CASES)
def test_search_async_is_unaffected_by_modal_dialog(panel_mod, handler, expected_js) -> None:
    """RunScriptAsync does not pump the dialog loop, so search no longer defers."""
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    windows.append(_Dialog(modal=True))

    getattr(obj, handler)(None)

    assert view.scripts == []
    assert view.async_scripts == [expected_js]


def test_search_text_debounces_to_one_async_find(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)

    for text in ("h", "he", "hel", "hello"):
        obj._search_ctrl.SetValue(text)
        obj._on_search_text(None)
        assert view.scripts == []
        assert view.async_scripts == []

    assert obj._search_timer.starts == [_panel_module._SEARCH_DEBOUNCE_MS] * 4
    assert obj._search_query == "hello"
    assert obj._search_seq == 0

    obj._on_search_tick(None)

    assert view.scripts == []
    assert view.async_scripts == ['_findTextAndJump("hello", 1)']
    assert obj._search_seq == 1
    assert obj._search_timer.running is False


def test_search_tick_runs_async_while_modal_open(panel_mod) -> None:
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    windows.append(_Dialog(modal=True))

    obj._on_search_tick(None)

    assert view.scripts == []
    assert view.async_scripts == ['_findTextAndJump("hello", 1)']


def test_search_message_updates_count_label(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    obj._on_search_tick(None)

    obj._on_search_message(
        _ScriptMessage({"kind": "find", "seq": 1, "count": 3, "query": "hello", "index": 2})
    )

    assert obj._search_count_label.label == "2/3"


def test_stale_search_message_is_ignored(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    obj._on_search_tick(None)  # seq 1
    obj._on_search_text(None)
    obj._on_search_tick(None)  # seq 2

    obj._on_search_message(
        _ScriptMessage({"kind": "find", "seq": 1, "count": 9, "query": "hello", "index": 1})
    )
    assert obj._search_count_label.label == "1/3", "stale seq must not overwrite"

    obj._on_search_message(
        _ScriptMessage({"kind": "find", "seq": 2, "count": 4, "query": "hello", "index": 3})
    )
    assert obj._search_count_label.label == "3/4"


def test_malformed_search_message_is_ignored(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)

    obj._on_search_message(_ScriptMessage("not json"))
    obj._on_search_message(_ScriptMessage({"kind": "find", "seq": "bad"}))
    obj._on_search_message(_ScriptMessage({"kind": "other", "seq": 1}))

    assert obj._search_count_label.label == "1/3"


def test_empty_query_clears_asynchronously(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("")

    obj._on_search_text(None)
    obj._on_search_tick(None)

    assert view.scripts == []
    assert view.async_scripts == ["_clearFind(1)"]
    assert obj._search_count_label.label == "0/0"


def test_search_close_stops_debounce_timer(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    assert obj._search_timer.running is True

    obj._on_search_close(None)

    assert obj._search_timer.running is False
    assert view.scripts == []
    assert view.async_scripts == ["_clearFind(1)"]


def test_destroy_stops_search_debounce_timer(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._update_cancel_event = None
    obj._update_progress_timer = None
    obj._project_watch_timer = None
    obj._finalise_and_save_on_teardown = lambda: None
    obj._server_mgr = SimpleNamespace(stop=lambda: None)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    assert obj._search_timer.running is True

    obj._on_destroy(_DestroyEvent(obj))

    assert obj._search_timer.running is False


# ---- Fallback when script messages / RunScriptAsync are unavailable ----


def test_search_falls_back_to_sync_when_async_disabled(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_async_ok = False
    obj._search_ctrl.SetValue("hello")

    obj._on_search_text(None)
    obj._on_search_tick(None)

    assert view.async_scripts == []
    assert view.scripts == ['_findTextAndJump("hello", 1)']
    assert obj._search_count_label.label == "ok"


def test_search_falls_back_when_view_lacks_run_script_async(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    obj = _make_search_panel(panel, timer, _FakeView())
    view = _SyncOnlyView()
    obj._conv_view = view
    obj._search_ctrl.SetValue("hello")

    obj._on_search_text(None)
    obj._on_search_tick(None)

    assert view.scripts == ['_findTextAndJump("hello", 1)']


def test_search_fallback_respects_modal_dialog(panel_mod) -> None:
    panel, windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_async_ok = False
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    windows.append(_Dialog(modal=True))

    obj._on_search_tick(None)

    assert view.scripts == []
    assert obj._search_count_label.label == "1/3"


def test_sync_search_fallback_ignores_reentrant_call(panel_mod) -> None:
    """RunScript pumps the loop; an EVT_TEXT re-entry must not run a second JS."""
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    calls: list[str] = []

    def _reentrant_run_script(js: str) -> tuple[bool, str]:
        calls.append(js)
        # Simulates _on_search_text arriving from RunScript's wxYield mid-flight.
        assert obj._run_search_script("nested()") is None
        obj._on_search_text(None)
        return True, "1/2"

    view.RunScript = _reentrant_run_script
    obj._search_ctrl.SetValue("hello")

    result = obj._run_search_script("outer()")

    assert result == (True, "1/2")
    assert calls == ["outer()"], "the re-entrant call must not reach RunScript"
    assert obj._search_script_running is False, "the guard must reset in finally"


def test_async_zero_hits_writes_zero_and_never_uses_sync(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("zzznotfound")

    obj._on_search_text(None)
    obj._on_search_tick(None)

    assert view.scripts == [], "async find must not fall back without a failure"
    assert view.async_scripts == ['_findTextAndJump("zzznotfound", 1)']

    obj._on_search_message(
        _ScriptMessage({"kind": "find", "seq": 1, "count": 0, "query": "zzznotfound", "index": 0})
    )
    assert obj._search_count_label.label == "0/0"


def test_render_reapplies_active_search(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    obj._on_search_tick(None)  # seq 1
    view.async_scripts.clear()

    obj._update_conversation()

    assert view.scripts and view.scripts[0].startswith("_updateConversation(")
    assert view.async_scripts == ['_findTextAndJump("hello", 2)']


def test_append_reapplies_active_search(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_ctrl.SetValue("hello")
    obj._on_search_text(None)
    obj._on_search_tick(None)  # seq 1
    view.async_scripts.clear()

    obj._append_entry_js({"type": "status", "text": "hi", "color_hex": "#000000"})

    assert view.async_scripts == ['_findTextAndJump("hello", 2)']


def test_clear_find_returns_explicit_result() -> None:
    """An undefined result makes wx log a warning that KiCad shows modally."""
    source = (Path(_panel_module.__file__).parent / "shell.js").read_text(encoding="utf-8")
    start = source.index("window._clearFind = function")
    body = source[start : source.index("\n};", start)]
    assert "return '0/0';" in body


def test_find_text_mark_loop_always_advances() -> None:
    source = (Path(_panel_module.__file__).parent / "shell.js").read_text(encoding="utf-8")
    assert "Math.max(1, query.length)" in source
    assert "idx <= previousIdx" in source


# --------------------------------------------------------------------------- #
# Diagnostics must be observation-only
# --------------------------------------------------------------------------- #


def test_diag_probe_warns_without_async_and_never_falls_back(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)
    obj._search_async_ok = False

    with mock.patch.object(panel.log, "warning") as warning:
        obj._log_ui_state("test")

    warning.assert_called_once()
    assert "diag: async unavailable, cannot probe page" in warning.call_args.args[0]
    assert view.scripts == [], "diagnostics must never issue a synchronous RunScript"
    assert view.async_scripts == []
    assert view.diag_scripts == []


def test_diag_probe_is_throttled_and_async_only(panel_mod) -> None:
    panel, _windows = panel_mod
    timer = _FakeTimer()
    view = _FakeView()
    obj = _make_search_panel(panel, timer, view)

    obj._last_diag_ts = time.monotonic()
    obj._maybe_log_ui_state("search_tick")
    assert view.diag_scripts == [], "a probe within one second must be throttled"

    obj._last_diag_ts = 0.0
    obj._maybe_log_ui_state("search_tick")

    assert len(view.diag_scripts) == 1
    probe = view.diag_scripts[0]
    assert "kind: 'diag'" in probe
    assert "conversationChildren" in probe
    assert "window.__findMatches" in probe
    assert view.scripts == [], "diagnostics must never issue a synchronous RunScript"
