"""Panel integration with the canonical project-state file.

Covers the read path (no probe fallback), session restore/load gating, the
write path and the tick timing.  The panel module is loaded against a stub
``wx`` module because wx is not installed in the test environment.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import os
import sys
import types

import pytest

from kicad_plugin import llm_client
from kicad_plugin import project_state as pstate
from kicad_plugin import session_store as sstore


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def ShowModal(self):
        return object()

    def Destroy(self) -> None:
        pass

    def GetSelection(self) -> int:
        return -1


@contextmanager
def _panel_module():
    """Import ``kicad_plugin.ui.panel`` against a stub ``wx`` module."""
    fake_wx = types.ModuleType("wx")
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


@pytest.fixture()
def panel():
    with _panel_module() as module:
        yield module


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir()
    pro = root / f"{name}.kicad_pro"
    pro.write_text("{}\n", encoding="utf-8")
    (root / f"{name}.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    return root, pro


def _fake_panel(module, config_dir, probe=None):
    obj = object.__new__(module.AssistantPanel)
    for name in (
        "_state_path",
        "_current_project_path",
        "_session_project_path",
        "_probe_project",
        "_write_project_state",
        "_start_project_watch",
        "_on_project_watch_tick",
        "_autoload_session",
        "_on_load_session",
        "_init_llm_client",
    ):
        setattr(obj, name, types.MethodType(getattr(module.AssistantPanel, name), obj))
    obj._settings = types.SimpleNamespace(config_dir=str(config_dir))
    obj._published = None
    obj._ticks_until_scan = 0
    obj._busy = False
    obj._shown = True
    obj.IsShown = lambda: obj._shown
    obj._probe_value = probe
    obj._probe_project = types.MethodType(lambda self: self._probe_value, obj)
    obj.autoload_calls = []
    obj.sync_calls = []
    obj.started = []
    obj._project_watch_timer = types.SimpleNamespace(
        Start=lambda ms: obj.started.append(ms), Stop=lambda: None
    )
    obj._server_mgr = types.SimpleNamespace(base_url="http://127.0.0.1:1")
    obj._llm_client = None
    obj._conv_entries = []
    obj._conv_version = 0
    obj._saved_conv_version = 0
    obj._current_session_file = None
    obj._C_TOOL_HEX = "#000000"
    obj._request_footprint_sync = lambda project: obj.sync_calls.append(project)
    obj._render_conversation = lambda *args, **kwargs: None
    return obj


def _publish(obj, pro):
    record = obj._write_project_state(str(pro))
    assert record is not None
    return record


class TestCurrentProjectPath:
    def test_missing_state_returns_none_without_probing(self, panel, tmp_path):
        obj = _fake_panel(panel, tmp_path / "cfg")
        obj._probe_project = types.MethodType(
            lambda self: (_ for _ in ()).throw(AssertionError("probe must not run")), obj
        )

        assert obj._current_project_path() is None

    @pytest.mark.parametrize("content", ["{not json", "null"])
    def test_corrupt_or_null_state_returns_none(self, panel, tmp_path, content):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / pstate.PROJECT_STATE_FILENAME).write_text(content, encoding="utf-8")
        obj = _fake_panel(panel, cfg)

        assert obj._current_project_path() is None

    def test_valid_state_matches_session_ownership(self, panel, tmp_path):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg)
        _publish(obj, pro)

        data = sstore.make_payload([], [], str(pro), [])

        assert obj._current_project_path() == str(pro)
        assert sstore.is_project_session(data, obj._current_project_path())


class TestSessionGating:
    def test_autoload_ignores_project_session_when_state_invalid(self, panel, tmp_path):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        _publish(_fake_panel(panel, cfg), pro)  # then remove the record
        (cfg / pstate.PROJECT_STATE_FILENAME).unlink()

        session = cfg / "kicad_ai_sessions" / "session_owned.json"
        payload = sstore.make_payload([{"type": "user", "text": "hello"}], [], str(pro), [])
        assert sstore.save_session(str(cfg), session.name, payload) is None
        sstore.update_current_link(str(cfg), session.name)

        obj = _fake_panel(panel, cfg)
        obj._llm_client = types.SimpleNamespace(
            set_history=lambda h: None, set_enabled_tools=lambda t: None
        )
        obj._autoload_session(prompt_if_missing=True)

        assert obj._conv_entries == []

    def test_load_dialog_passes_none_when_state_invalid(self, panel, tmp_path, monkeypatch):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg)
        captured = {}

        def fake_list(config_dir, project_path):
            captured["project"] = project_path
            return []

        monkeypatch.setattr(sstore, "list_loadable_sessions", fake_list)
        obj._on_load_session(None)

        assert captured["project"] is None

    def test_load_dialog_includes_owned_sessions_when_state_valid(
        self, panel, tmp_path, monkeypatch
    ):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg)
        _publish(obj, pro)
        owned = cfg / "kicad_ai_sessions" / "session_owned.json"
        payload = sstore.make_payload([{"type": "user", "text": "hello"}], [], str(pro), [])
        assert sstore.save_session(str(cfg), owned.name, payload) is None
        real_list = sstore.list_loadable_sessions
        captured = {}

        def fake_list(config_dir, project_path):
            captured["project"] = project_path
            return real_list(config_dir, project_path)

        monkeypatch.setattr(sstore, "list_loadable_sessions", fake_list)
        obj._on_load_session(None)

        assert captured["project"] == str(pro)
        assert str(owned) in sstore.list_loadable_sessions(str(cfg), str(pro))


class TestStartupOrder:
    def test_state_is_written_before_autoload(self, panel, tmp_path, monkeypatch):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        seen = []
        obj._autoload_session = lambda prompt_if_missing=False: seen.append(
            obj._current_project_path()
        )

        class _Client:
            def __init__(self, settings, base_url):
                pass

        monkeypatch.setattr(llm_client, "LLMClient", _Client)

        obj._init_llm_client()

        assert seen == [str(pro)]
        assert obj._published is not None
        assert obj.started == [panel._PROJECT_TICK_MS]


class TestWritePath:
    def test_probe_failure_writes_nothing(self, panel, tmp_path):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        _publish(obj, pro)
        state_file = cfg / pstate.PROJECT_STATE_FILENAME
        before = state_file.read_bytes()

        obj._probe_value = None
        obj._write_project_state()

        assert state_file.read_bytes() == before


class TestTickTiming:
    def test_tick_interval_is_one_second(self, panel, tmp_path):
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg)

        obj._start_project_watch()

        assert obj.started == [1000]

    def test_scan_runs_only_on_the_fifth_tick(self, panel, tmp_path, monkeypatch):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        _publish(obj, pro)
        obj._start_project_watch()

        calls = {"n": 0}
        real_build = pstate.build_record

        def counting_build(project_file, written_at=None):
            calls["n"] += 1
            return real_build(project_file, written_at)

        monkeypatch.setattr(pstate, "build_record", counting_build)
        baseline = calls["n"]

        for _ in range(4):
            obj._on_project_watch_tick(None)
        assert calls["n"] == baseline

        obj._on_project_watch_tick(None)
        assert calls["n"] == baseline + 1

    def test_idle_ticks_do_not_touch_the_file(self, panel, tmp_path):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        _publish(obj, pro)
        obj._start_project_watch()
        state_file = cfg / pstate.PROJECT_STATE_FILENAME
        before = state_file.read_bytes()
        before_mtime = os.stat(state_file).st_mtime_ns

        for _ in range(5):
            obj._on_project_watch_tick(None)

        assert state_file.read_bytes() == before
        assert os.stat(state_file).st_mtime_ns == before_mtime

    def test_project_switch_writes_and_autoloads_once(self, panel, tmp_path):
        root_a, pro_a = _project(tmp_path, "a")
        root_b, pro_b = _project(tmp_path, "b")
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro_a))
        _publish(obj, pro_a)
        obj._start_project_watch()

        obj._probe_value = str(pro_b)
        obj._autoload_session = lambda prompt_if_missing=False: obj.autoload_calls.append(
            prompt_if_missing
        )
        obj._on_project_watch_tick(None)

        record = pstate.read_state(str(cfg / pstate.PROJECT_STATE_FILENAME))
        assert record is not None
        assert record["project_root"] == str(root_b)
        assert obj.autoload_calls == [True]
        assert obj.sync_calls == [str(pro_b)]

    def test_new_subsheet_appears_within_five_ticks(self, panel, tmp_path):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        _publish(obj, pro)
        obj._start_project_watch()

        (root / "late.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")

        for _ in range(5):
            obj._on_project_watch_tick(None)

        record = pstate.read_state(str(cfg / pstate.PROJECT_STATE_FILENAME))
        assert record is not None
        assert "late.kicad_sch" in record["schematic_files"]

    def test_probe_failure_does_not_scan_or_write(self, panel, tmp_path, monkeypatch):
        root, pro = _project(tmp_path)
        cfg = tmp_path / "cfg"
        obj = _fake_panel(panel, cfg, probe=str(pro))
        _publish(obj, pro)
        obj._start_project_watch()
        state_file = cfg / pstate.PROJECT_STATE_FILENAME
        before = state_file.read_bytes()
        before_ticks = obj._ticks_until_scan

        calls = {"n": 0}
        real_build = pstate.build_record

        def counting_build(project_file, written_at=None):
            calls["n"] += 1
            return real_build(project_file, written_at)

        monkeypatch.setattr(pstate, "build_record", counting_build)
        obj._probe_value = None

        for _ in range(5):
            obj._on_project_watch_tick(None)

        assert calls["n"] == 0
        assert state_file.read_bytes() == before
        assert obj._ticks_until_scan == before_ticks
