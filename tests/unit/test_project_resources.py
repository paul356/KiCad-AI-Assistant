"""Tests for project resources."""

import json


class _MockMCP:
    def __init__(self):
        self.resources: dict = {}

    def resource(self, uri):
        def decorator(fn):
            self.resources[uri] = fn
            return fn

        return decorator


def _project_resource():
    from kcaa.resources.projects import register_project_resources

    mock = _MockMCP()
    register_project_resources(mock)
    return mock.resources["kicad://project/{project_path}"]


def test_non_project_json_uses_basename_as_display_name(tmp_path, isolated_project_search):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"meta": {}, "sheets": []}))

    text = _project_resource()(str(path))

    assert text.splitlines()[0] == "# Project: notes.json"
