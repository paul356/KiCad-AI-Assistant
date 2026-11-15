"""Shared fixtures for unit tests."""

import os

import pytest

from kcaa.utils import project_resolution


@pytest.fixture()
def isolated_project_search(tmp_path, monkeypatch):
    """Scope project-file discovery to *tmp_path* and its descendants.

    The canonical resolver walks from a file's directory to the filesystem
    root, and a shared temp directory may legitimately contain unrelated
    KiCad projects.  Tests that assert "no project was found" use this
    fixture so ancestors outside the test's own tree are project-free.
    """
    root = os.path.abspath(str(tmp_path))
    real_candidates = project_resolution._project_candidates

    def scoped_candidates(directory):
        resolved = os.path.abspath(directory)
        if resolved == root or resolved.startswith(root + os.sep):
            return real_candidates(directory)
        return []

    monkeypatch.setattr(project_resolution, "_project_candidates", scoped_candidates)
    return tmp_path
