"""Shared fixtures for unit tests."""

import os

import pytest

from kcaa.utils import project_resolution
from kicad_plugin import project_resolution as plugin_project_resolution


@pytest.fixture()
def isolated_project_search(tmp_path, monkeypatch):
    """Scope project-file discovery to *tmp_path* and its descendants.

    The canonical resolver walks from a file's directory to the filesystem
    root, and a shared temp directory may legitimately contain unrelated
    KiCad projects.  Tests that assert "no project was found" use this
    fixture so ancestors outside the test's own tree are project-free.
    """
    root = os.path.abspath(str(tmp_path))

    for module in (project_resolution, plugin_project_resolution):
        real_candidates = module._project_candidates

        def scoped_candidates(directory, _real=real_candidates):
            resolved = os.path.abspath(directory)
            if resolved == root or resolved.startswith(root + os.sep):
                return _real(directory)
            return []

        monkeypatch.setattr(module, "_project_candidates", scoped_candidates)
    return tmp_path
