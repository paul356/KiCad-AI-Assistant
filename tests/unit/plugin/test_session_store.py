"""Tests for session_store: schema v2 project scoping and v1 legacy support.

Defends the contract that drives session auto-restore:

- v1 files (no ``project_path``) are legacy and never project-matched, so
  they can never be auto-restored into a project;
- v2 files match only the project they were stamped with;
- ``current.json`` is followed only when it points to a session of the open
  project, and project-scoped lookups never leak other projects' sessions;
- existing files (v1 and v2) remain readable/writable — no data loss upgrade.
"""

import json
import os

from kicad_plugin import session_store as sstore

PROJECT_A = "/home/user/projects/alpha/alpha.kicad_pro"
PROJECT_B = "/home/user/projects/beta/beta.kicad_pro"


def _write_v1_session(config_dir: str, filename: str) -> str:
    """Write a legacy v1 session file exactly as the old plugin did."""
    sessions = sstore.sessions_dir(config_dir)
    os.makedirs(sessions, exist_ok=True)
    data = {
        "version": 1,
        "title": "legacy",
        "timestamp": "2026-08-01T10:00:00",
        "conv_entries": [{"type": "user", "text": "legacy question"}],
        "llm_history": [],
    }
    path = os.path.join(sessions, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _write_v2_session(config_dir: str, filename: str, project_path: str, title: str = "v2") -> str:
    """Write a v2 session file stamped with *project_path*."""
    payload = sstore.make_payload([{"type": "user", "text": title}], [], project_path)
    sessions = sstore.sessions_dir(config_dir)
    os.makedirs(sessions, exist_ok=True)
    path = os.path.join(sessions, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


class TestSchemaV2:
    def test_make_payload_stamps_project(self):
        payload = sstore.make_payload(
            [{"type": "user", "text": "hello"}], [{"role": "user"}], PROJECT_A
        )
        assert payload["version"] == 2
        assert payload["project_path"] == PROJECT_A
        assert payload["title"] == "hello"
        assert "timestamp" in payload
        assert payload["conv_entries"] == [{"type": "user", "text": "hello"}]
        assert payload["llm_history"] == [{"role": "user"}]

    def test_make_payload_defaults(self):
        payload = sstore.make_payload([{"type": "ai", "text": "hi"}], [], None)
        assert payload["version"] == 2
        assert payload["project_path"] is None
        assert payload["title"] == "session"

    def test_title_uses_first_user_message(self):
        payload = sstore.make_payload(
            [
                {"type": "status", "text": "notice"},
                {"type": "user", "text": "  " * 30 + "real question"},
            ],
            [],
            PROJECT_A,
        )
        assert payload["title"] == ("  " * 30 + "real question")[:60]


class TestProjectMatching:
    def test_v2_session_matches_own_project(self):
        assert sstore.is_project_session({"project_path": PROJECT_A}, PROJECT_A)

    def test_v2_session_rejects_other_project(self):
        assert not sstore.is_project_session({"project_path": PROJECT_A}, PROJECT_B)

    def test_v1_legacy_session_never_matches(self):
        # v1 payloads have no project_path key at all.
        assert not sstore.is_project_session({"version": 1, "conv_entries": []}, PROJECT_A)

    def test_session_with_none_project_never_matches(self):
        assert not sstore.is_project_session({"project_path": None}, PROJECT_A)

    def test_no_open_project_never_matches(self):
        assert not sstore.is_project_session({"project_path": PROJECT_A}, None)

    def test_none_data_never_matches(self):
        assert not sstore.is_project_session(None, PROJECT_A)


class TestCurrentLink:
    def test_resolve_plain_text_pointer(self, tmp_path):
        _write_v1_session(str(tmp_path), "session_old.json")
        link = sstore.current_link_path(str(tmp_path))
        with open(link, "w", encoding="utf-8") as f:
            f.write("session_old.json")
        resolved = sstore.resolve_current_session(str(tmp_path))
        assert resolved == os.path.join(str(tmp_path), "kicad_ai_sessions", "session_old.json")

    def test_resolve_symlink_form(self, tmp_path):
        path = _write_v2_session(str(tmp_path), "session_a.json", PROJECT_A)
        sstore.update_current_link(str(tmp_path), "session_a.json")
        assert sstore.resolve_current_session(str(tmp_path)) == path

    def test_resolve_missing_returns_none(self, tmp_path):
        assert sstore.resolve_current_session(str(tmp_path)) is None

    def test_resolve_dangling_symlink_returns_none(self, tmp_path):
        sessions = sstore.sessions_dir(str(tmp_path))
        os.makedirs(sessions, exist_ok=True)
        os.symlink("session_ghost.json", os.path.join(sessions, "current.json"))
        assert sstore.resolve_current_session(str(tmp_path)) is None

    def test_remove_current_link(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_a.json", PROJECT_A)
        sstore.update_current_link(str(tmp_path), "session_a.json")
        assert sstore.resolve_current_session(str(tmp_path)) is not None
        sstore.remove_current_link(str(tmp_path))
        assert sstore.resolve_current_session(str(tmp_path)) is None

    def test_remove_current_link_missing_is_noop(self, tmp_path):
        sstore.remove_current_link(str(tmp_path))  # must not raise


class TestProjectSessions:
    def test_lists_only_matching_project(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_1.json", PROJECT_A, title="a1")
        _write_v2_session(str(tmp_path), "session_2.json", PROJECT_B, title="b1")
        _write_v1_session(str(tmp_path), "session_3.json")
        found = sstore.list_project_sessions(str(tmp_path), PROJECT_A)
        assert [os.path.basename(f) for f in found] == ["session_1.json"]

    def test_newest_first_ordering(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_1.json", PROJECT_A)
        _write_v2_session(str(tmp_path), "session_2.json", PROJECT_A)
        found = sstore.list_project_sessions(str(tmp_path), PROJECT_A)
        assert [os.path.basename(f) for f in found] == [
            "session_2.json",
            "session_1.json",
        ]

    def test_legacy_sessions_excluded(self, tmp_path):
        _write_v1_session(str(tmp_path), "session_1.json")
        assert sstore.list_project_sessions(str(tmp_path), PROJECT_A) == []

    def test_none_project_returns_empty(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_1.json", PROJECT_A)
        assert sstore.list_project_sessions(str(tmp_path), None) == []

    def test_corrupt_file_is_skipped_not_crash(self, tmp_path):
        sessions = sstore.sessions_dir(str(tmp_path))
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "session_bad.json"), "w") as f:
            f.write("{not json")
        _write_v2_session(str(tmp_path), "session_1.json", PROJECT_A)
        found = sstore.list_project_sessions(str(tmp_path), PROJECT_A)
        assert [os.path.basename(f) for f in found] == ["session_1.json"]


class TestLoadableSessions:
    def test_own_project_sessions_plus_unowned(self, tmp_path):
        own = _write_v2_session(str(tmp_path), "session_1.json", PROJECT_A, title="own1")
        _write_v2_session(str(tmp_path), "session_2.json", PROJECT_B, title="other")
        legacy = _write_v1_session(str(tmp_path), "session_3.json")
        found = sstore.list_loadable_sessions(str(tmp_path), PROJECT_A)
        assert found == [own, legacy]

    def test_unowned_v2_without_project_included(self, tmp_path):
        unowned = sstore.make_payload([{"type": "user", "text": "no proj"}], [], None)
        sessions = sstore.sessions_dir(str(tmp_path))
        os.makedirs(sessions, exist_ok=True)
        unowned_path = os.path.join(sessions, "session_x.json")
        with open(unowned_path, "w", encoding="utf-8") as f:
            json.dump(unowned, f)
        _write_v2_session(str(tmp_path), "session_own.json", PROJECT_A)
        found = sstore.list_loadable_sessions(str(tmp_path), PROJECT_A)
        assert found == [
            os.path.join(sessions, "session_own.json"),
            unowned_path,
        ]

    def test_none_project_returns_only_unowned(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_a.json", PROJECT_A)
        _write_v1_session(str(tmp_path), "session_legacy.json")
        found = sstore.list_loadable_sessions(str(tmp_path), None)
        assert [os.path.basename(f) for f in found] == ["session_legacy.json"]

    def test_empty_dir(self, tmp_path):
        assert sstore.list_loadable_sessions(str(tmp_path), PROJECT_A) == []

    def test_other_projects_excluded(self, tmp_path):
        _write_v2_session(str(tmp_path), "session_a.json", PROJECT_A)
        _write_v2_session(str(tmp_path), "session_b.json", PROJECT_B)
        found = sstore.list_loadable_sessions(str(tmp_path), PROJECT_A)
        assert [os.path.basename(f) for f in found] == ["session_a.json"]


class TestRoundTrip:
    def test_save_then_load_v2(self, tmp_path):
        payload = sstore.make_payload(
            [{"type": "user", "text": "q"}], [{"role": "user"}], PROJECT_A
        )
        err = sstore.save_session(str(tmp_path), "session_x.json", payload)
        assert err is None

        path = os.path.join(sstore.sessions_dir(str(tmp_path)), "session_x.json")
        assert os.path.isfile(path)
        if os.name != "nt":
            assert os.stat(path).st_mode & 0o777 == 0o600

        data, err2 = sstore.load_session(path)
        assert err2 is None
        assert data is not None
        assert data["version"] == 2
        assert data["project_path"] == PROJECT_A
        assert data["conv_entries"] == [{"type": "user", "text": "q"}]

    def test_load_missing_file(self, tmp_path):
        data, err = sstore.load_session(
            os.path.join(sstore.sessions_dir(str(tmp_path)), "session_none.json")
        )
        assert data is None
        assert err is not None

    def test_load_corrupt_file(self, tmp_path):
        sessions = sstore.sessions_dir(str(tmp_path))
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "session_bad.json"), "w") as f:
            f.write("{not json")
        data, err = sstore.load_session(os.path.join(sessions, "session_bad.json"))
        assert data is None
        assert err is not None
