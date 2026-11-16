"""Tests for the canonical project resolver."""

import os

import pytest

from kcaa.utils import project_resolution


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    """Return a project-search root with no projects in its ancestors.

    The host's temp directory can contain unrelated KiCad projects, and the
    resolver legitimately walks to the filesystem root.  Scope candidate
    discovery to *tmp_path* so tests that expect ``None`` are deterministic.
    """
    root = tmp_path / "root"
    root.mkdir()
    root_str = str(root)
    real_candidates = project_resolution._project_candidates

    def scoped_candidates(directory):
        resolved = os.path.abspath(directory)
        if resolved == root_str or resolved.startswith(root_str + os.sep):
            return real_candidates(directory)
        return []

    monkeypatch.setattr(project_resolution, "_project_candidates", scoped_candidates)
    return root


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("(kicad)\n")
    return path


class TestFindProjectFile:
    def test_project_at_root_sheet_in_subdirectory(self, isolated_root):
        pro = _touch(isolated_root / "aria.kicad_pro")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(pro)

    def test_board_stem_differs_from_project_stem(self, isolated_root):
        pro = _touch(isolated_root / "aria.kicad_pro")
        board = _touch(isolated_root / "hardware" / "main_bus.kicad_pcb")

        assert project_resolution.find_project_file(str(board)) == str(pro)

    def test_project_two_levels_up(self, isolated_root):
        pro = _touch(isolated_root / "aria.kicad_pro")
        sheet = _touch(isolated_root / "boards" / "rev_a" / "power.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(pro)

    def test_path_is_existing_project_file(self, isolated_root):
        pro = _touch(isolated_root / "aria.kicad_pro")

        assert project_resolution.find_project_file(str(pro)) == str(pro)

    def test_path_is_missing_project_file(self, isolated_root):
        """A .kicad_pro argument is never resolved by searching upward."""
        _touch(isolated_root / "aria.kicad_pro")

        assert project_resolution.find_project_file(str(isolated_root / "gone.kicad_pro")) is None

    def test_missing_path_with_no_project_anywhere(self, isolated_root):
        target = isolated_root / "hardware" / "not_created.kicad_sch"

        assert project_resolution.find_project_file(str(target)) is None

    def test_directory_argument_searches_from_there(self, isolated_root):
        pro = _touch(isolated_root / "aria.kicad_pro")
        hardware = isolated_root / "hardware"
        hardware.mkdir()

        assert project_resolution.find_project_file(str(hardware)) == str(pro)


class TestSeveralProjectsInOneDirectory:
    def test_prefers_project_with_matching_frame(self, isolated_root):
        """A same-stem sch/pcb wins over the lexicographically smallest name."""
        _touch(isolated_root / "alpha.kicad_pro")
        zeta = _touch(isolated_root / "zeta.kicad_pro")
        _touch(isolated_root / "zeta.kicad_sch")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(zeta)

    def test_falls_back_to_lexicographically_smallest(self, isolated_root):
        alpha = _touch(isolated_root / "alpha.kicad_pro")
        _touch(isolated_root / "zeta.kicad_pro")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(alpha)

    def test_prefers_queried_stem_before_lexicographic_order(self, isolated_root):
        """With no matching frames, the queried file stem breaks the tie."""
        _touch(isolated_root / "alpha.kicad_pro")
        zeta = _touch(isolated_root / "zeta.kicad_pro")
        queried = _touch(isolated_root / "zeta.notes")

        assert project_resolution.find_project_file(str(queried)) == str(zeta)

    def test_project_suffix_is_case_insensitive(self, isolated_root):
        pro = _touch(isolated_root / "ARIA.KICAD_PRO")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(pro)


class TestProjectName:
    def test_subdirectory_sheet_resolves_root_project_name(self, isolated_root):
        _touch(isolated_root / "aria.kicad_pro")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.project_name(str(sheet)) == "aria"

    def test_project_file_argument_returns_its_stem(self, isolated_root):
        pro = _touch(isolated_root / "ARIA.KICAD_PRO")

        assert project_resolution.project_name(str(pro)) == "ARIA"

    def test_no_project_returns_none(self, isolated_root):
        sheet = _touch(isolated_root / "hardware" / "orphan.kicad_sch")

        assert project_resolution.project_name(str(sheet)) is None

    def test_matches_find_project_file_tie_break(self, isolated_root):
        _touch(isolated_root / "alpha.kicad_pro")
        zeta = _touch(isolated_root / "zeta.kicad_pro")
        _touch(isolated_root / "zeta.kicad_sch")
        sheet = _touch(isolated_root / "hardware" / "main_bus.kicad_sch")

        assert project_resolution.find_project_file(str(sheet)) == str(zeta)
        assert project_resolution.project_name(str(sheet)) == "zeta"
