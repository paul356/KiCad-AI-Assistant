"""Tests for the canonical plugin project-state module and its core twin."""

from __future__ import annotations

import json
import os

import pytest

from kcaa.utils import project_state as core_state
from kicad_plugin import project_state


def _project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    pro = root / "board.kicad_pro"
    pro.write_text("{}\n", encoding="utf-8")
    (root / "board.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    hardware = root / "hardware"
    hardware.mkdir()
    (hardware / "main.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    (hardware / "main_bus.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    return root, pro


def _record(root, pro, **overrides):
    record = {
        "version": 2,
        "written_at": "2026-11-17T09:12:33Z",
        "project_root": str(root),
        "profile_file": os.path.basename(pro),
        "pcb_files": ["hardware/main.kicad_pcb"],
        "schematic_files": ["board.kicad_sch", "hardware/main_bus.kicad_sch"],
    }
    record.update(overrides)
    return record


class TestSchema:
    def test_build_scan_and_round_trip(self, tmp_path):
        root, pro = _project(tmp_path)
        state_file = tmp_path / "cfg" / project_state.PROJECT_STATE_FILENAME

        record = project_state.build_record(str(pro), written_at="2026-11-17T09:12:33Z")
        assert record is not None
        assert record["project_root"] == str(root)
        assert record["profile_file"] == "board.kicad_pro"
        assert record["schematic_files"] == ["board.kicad_sch", "hardware/main_bus.kicad_sch"]
        assert record["pcb_files"] == ["hardware/main.kicad_pcb"]

        assert project_state.write_state(str(state_file), record) is True
        assert project_state.read_state(str(state_file)) == record

    def test_project_root_is_always_a_json_string(self, tmp_path):
        root, pro = _project(tmp_path)
        state_file = tmp_path / project_state.PROJECT_STATE_FILENAME
        record = project_state.build_record(str(pro))
        project_state.write_state(str(state_file), record)

        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert isinstance(payload["project_root"], str)
        assert payload["project_root"]

    def test_atomic_write_leaves_only_the_state_file(self, tmp_path):
        root, pro = _project(tmp_path)
        state_file = tmp_path / project_state.PROJECT_STATE_FILENAME
        record = project_state.build_record(str(pro))

        project_state.write_state(str(state_file), record)
        project_state.write_state(str(state_file), record)

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "proj",
            project_state.PROJECT_STATE_FILENAME,
        ]

    def test_invalid_record_never_writes_or_touches_the_file(self, tmp_path):
        root, pro = _project(tmp_path)
        state_file = tmp_path / project_state.PROJECT_STATE_FILENAME
        valid = project_state.build_record(str(pro))
        assert project_state.write_state(str(state_file), valid)
        before = state_file.read_bytes()

        assert project_state.write_state(str(state_file), {}) is False
        assert state_file.read_bytes() == before

        missing = tmp_path / "new" / project_state.PROJECT_STATE_FILENAME
        assert project_state.write_state(str(missing), {"version": 1}) is False
        assert not missing.exists()

    def test_vanished_profile_yields_no_record(self, tmp_path):
        root, pro = _project(tmp_path)
        pro.unlink()

        assert project_state.build_record(str(pro)) is None


class TestManifest:
    def test_invalid_entries_drop_individually(self, tmp_path):
        root, pro = _project(tmp_path)
        outside = tmp_path / "outside.kicad_pcb"
        outside.write_text("(kicad_pcb)\n", encoding="utf-8")
        payload = _record(
            root,
            pro,
            pcb_files=[
                "../outside.kicad_pcb",
                "missing.kicad_pcb",
                "hardware/main.kicad_pcb",
            ],
            schematic_files=["board.kicad_sch", "/abs/not/inside.kicad_sch"],
        )

        record = project_state.validate_state(payload)

        assert record is not None
        assert record["pcb_files"] == ["hardware/main.kicad_pcb"]
        assert record["schematic_files"] == ["board.kicad_sch"]

    def test_scan_skips_versions_backups_hidden_and_symlinked_dirs(self, tmp_path):
        root, pro = _project(tmp_path)

        versions = root / ".versions"
        versions.mkdir()
        (versions / "old.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        backups = root / "board-backups"
        backups.mkdir()
        (backups / "old.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
        hidden = root / ".hidden"
        hidden.mkdir()
        (hidden / "old.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "escaped.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        try:
            os.symlink(outside, root / "linked")
        except OSError:  # pragma: no cover - Windows without symlink privileges
            pytest.skip("symlinks unavailable")

        record = project_state.build_record(str(pro))

        assert record is not None
        assert record["pcb_files"] == ["hardware/main.kicad_pcb"]
        assert record["schematic_files"] == ["board.kicad_sch", "hardware/main_bus.kicad_sch"]

    def test_scan_is_case_insensitive_deduplicated_and_sorted(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        pro = root / "board.kicad_pro"
        pro.write_text("{}\n", encoding="utf-8")
        (root / "Zeta.KICAD_PCB").write_text("(kicad_pcb)\n", encoding="utf-8")
        (root / "alpha.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
        sub = root / "sub"
        sub.mkdir()
        (sub / "beta.Kicad_Sch").write_text("(kicad_sch)\n", encoding="utf-8")

        record = project_state.build_record(str(pro))

        assert record is not None
        assert record["pcb_files"] == ["Zeta.KICAD_PCB", "alpha.kicad_pcb"]
        assert record["schematic_files"] == ["sub/beta.Kicad_Sch"]


class TestValidity:
    def test_null_payload_is_invalid(self, tmp_path):
        assert project_state.validate_state(None) is None
        state_file = tmp_path / project_state.PROJECT_STATE_FILENAME
        state_file.write_text("null", encoding="utf-8")
        assert project_state.read_state(str(state_file)) is None
        assert core_state.read_state(str(state_file)) is None

    @pytest.mark.parametrize("version", [1, 3, "2", None])
    def test_wrong_version_is_invalid(self, tmp_path, version):
        root, pro = _project(tmp_path)
        payload = _record(root, pro, version=version)
        assert project_state.validate_state(payload) is None
        assert core_state.validate_state(payload) is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("written_at", None),
            ("project_root", None),
            ("project_root", "relative/dir"),
            ("profile_file", None),
            ("profile_file", ""),
            ("pcb_files", None),
            ("schematic_files", "board.kicad_sch"),
        ],
    )
    def test_type_errors_are_invalid(self, tmp_path, field, value):
        root, pro = _project(tmp_path)
        payload = _record(root, pro, **{field: value})
        assert project_state.validate_state(payload) is None
        assert core_state.validate_state(payload) is None

    def test_profile_outside_root_is_invalid(self, tmp_path):
        root, pro = _project(tmp_path)
        outside = tmp_path / "outside.kicad_pro"
        outside.write_text("{}\n", encoding="utf-8")

        for profile in ("../outside.kicad_pro", str(outside)):
            payload = _record(root, pro, profile_file=profile)
            assert project_state.validate_state(payload) is None
            assert core_state.validate_state(payload) is None

    def test_unknown_fields_are_ignored(self, tmp_path):
        root, pro = _project(tmp_path)
        payload = _record(root, pro, future_field={"anything": True})

        record = project_state.validate_state(payload)

        assert record is not None
        assert "future_field" not in record
        assert core_state.validate_state(payload) == record

    def test_missing_or_malformed_file_is_invalid(self, tmp_path):
        missing = tmp_path / "nope.json"
        assert project_state.read_state(str(missing)) is None
        assert core_state.read_state(str(missing)) is None

        malformed = tmp_path / "bad.json"
        malformed.write_text("{not json", encoding="utf-8")
        assert project_state.read_state(str(malformed)) is None
        assert core_state.read_state(str(malformed)) is None


class TestContract:
    """Both packages must read the same file identically."""

    def test_plugin_write_is_read_by_core(self, tmp_path):
        root, pro = _project(tmp_path)
        state_file = tmp_path / project_state.PROJECT_STATE_FILENAME
        record = project_state.build_record(str(pro), written_at="2026-11-17T09:12:33Z")
        assert project_state.write_state(str(state_file), record)

        plugin_view = project_state.read_state(str(state_file))
        core_view = core_state.read_state(str(state_file))

        assert core_view == plugin_view == record
        assert core_state.project_file_path(core_view) == str(pro)

    @pytest.mark.parametrize(
        "payload_factory",
        [
            lambda root, pro: None,
            lambda root, pro: {"version": 1, "project_root": str(root)},
            lambda root, pro: _record(root, pro, schematic_files="broken"),
            lambda root, pro: _record(root, pro, profile_file="../outside.kicad_pro"),
        ],
        ids=["null", "old-schema", "wrong-type", "profile-outside"],
    )
    def test_invalid_payloads_are_rejected_by_both(self, tmp_path, payload_factory):
        root, pro = _project(tmp_path)
        payload = payload_factory(root, pro)

        assert project_state.validate_state(payload) is None
        assert core_state.validate_state(payload) is None
