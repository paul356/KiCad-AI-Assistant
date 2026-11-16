"""Tests for project file-set discovery."""

from types import SimpleNamespace

from kcaa.utils import kicad_utils
from kcaa.utils.file_utils import get_project_files


def _project(tmp_path):
    pro = tmp_path / "aria.kicad_pro"
    pro.write_text("{}\n")
    root_sch = tmp_path / "aria.kicad_sch"
    root_sch.write_text("(kicad_sch)\n")
    hardware = tmp_path / "hardware"
    hardware.mkdir()
    nested = hardware / "main_bus.kicad_sch"
    nested.write_text("(kicad_sch)\n")
    return pro, root_sch, nested


def test_subdirectory_sheet_reports_owning_project_and_root_schematic(tmp_path):
    pro, root_sch, nested = _project(tmp_path)

    files = get_project_files(str(nested))

    assert files["project"] == str(pro)
    assert files["schematic"] == str(root_sch)
    assert files["schematic"] != str(nested)


def test_no_project_returns_only_the_input_as_project(tmp_path, isolated_project_search):
    board = tmp_path / "orphan.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    (tmp_path / "unrelated.csv").write_text("a,b\n")
    (tmp_path / "unrelated.zip").write_bytes(b"PK")

    files = get_project_files(str(board))

    assert files == {"project": str(board)}


def test_find_kicad_projects_reports_project_stem(tmp_path, monkeypatch):
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    pro = project_dir / "aria.kicad_pro"
    pro.write_text("{}")
    fake_config = SimpleNamespace(
        kicad_user_dir=str(project_dir),
        additional_search_paths=[],
        kicad_extensions={"project": ".kicad_pro"},
    )
    monkeypatch.setattr(kicad_utils, "config", fake_config)

    projects = kicad_utils.find_kicad_projects()

    assert len(projects) == 1
    assert projects[0]["name"] == "aria"
    assert projects[0]["path"] == str(pro)
    assert projects[0]["relative_path"] == "aria.kicad_pro"
