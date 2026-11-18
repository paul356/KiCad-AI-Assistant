"""Read-side retry for the core project-state twin."""

import builtins
import json
import os

from kcaa.utils import project_state


def _write_state(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n", encoding="utf-8")
    state_file = tmp_path / "project_state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 2,
                "written_at": "2026-11-17T09:12:33Z",
                "project_root": str(tmp_path),
                "profile_file": pro.name,
                "pcb_files": [],
                "schematic_files": [],
            }
        ),
        encoding="utf-8",
    )
    return state_file


def _flaky_open(state_file, failures):
    """Return an open() that raises *failures* times for *state_file*."""
    real_open = builtins.open
    calls = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        if os.fspath(path) == str(state_file):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(13, "sharing violation")
        return real_open(path, *args, **kwargs)

    return flaky_open, calls


def test_transient_permission_error_is_retried_once(tmp_path, monkeypatch):
    state_file = _write_state(tmp_path)
    flaky_open, calls = _flaky_open(state_file, failures=1)
    monkeypatch.setattr(builtins, "open", flaky_open)

    record = project_state.read_state(str(state_file))

    assert record is not None
    assert record["project_root"] == str(tmp_path)
    assert calls["n"] == 2


def test_persistent_permission_error_returns_none(tmp_path, monkeypatch):
    state_file = _write_state(tmp_path)
    flaky_open, calls = _flaky_open(state_file, failures=99)
    monkeypatch.setattr(builtins, "open", flaky_open)

    assert project_state.read_state(str(state_file)) is None
    assert calls["n"] == 2


def test_non_transient_error_is_not_retried(tmp_path, monkeypatch):
    state_file = tmp_path / "project_state.json"
    real_open = builtins.open
    calls = {"n": 0}

    def missing_open(path, *args, **kwargs):
        if os.fspath(path) == str(state_file):
            calls["n"] += 1
            raise FileNotFoundError(2, "no such file")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", missing_open)

    assert project_state.read_state(str(state_file)) is None
    assert calls["n"] == 1
