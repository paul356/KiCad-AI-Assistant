"""Server profile tool-set tests.

Pins which tools each server profile exposes: the plugin profile
(``KICAD_MCP_PROFILE=plugin``, used by the KiCad plugin) must include the
``get_project_structure`` query tool the LLM uses to plan cross-file
operations, while the full-profile management tools (``list_projects`` /
``open_project``) stay out of it.
"""

import asyncio

import pytest

from kcaa.server import ServerProfile, create_server


def _tool_names(profile: ServerProfile) -> set[str]:
    return {tool.name for tool in asyncio.run(create_server(profile=profile).list_tools())}


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch):
    """The suite must not inherit a KICAD_MCP_PROFILE override."""
    monkeypatch.delenv("KICAD_MCP_PROFILE", raising=False)


def test_plugin_profile_exposes_project_structure_only():
    names = _tool_names("plugin")
    assert "get_project_structure" in names
    assert "list_projects" not in names
    assert "open_project" not in names


def test_full_profile_registers_all_project_tools():
    names = _tool_names("full")
    for tool in ("get_project_structure", "list_projects", "open_project"):
        assert tool in names
