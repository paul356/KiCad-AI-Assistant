"""Issue #208: every frontmatter ``tools:`` entry in kicad_plugin/skills/*.md
must name a tool that exists in the MCP plugin tool registry.

The auto-inject mapping is author-declared and may drift; this test pins the
declared names to the actual registry so a misspelled or renamed tool is
caught at authoring time (fix the frontmatter, never the test).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kcaa.server import create_server
from kicad_plugin import llm_client

_SKILLS_DIR = Path(llm_client.__file__).resolve().parent / "skills"


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch):
    """The suite must not inherit a KICAD_MCP_PROFILE override."""
    monkeypatch.delenv("KICAD_MCP_PROFILE", raising=False)


def _plugin_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(create_server(profile="plugin").list_tools())}


def test_every_declared_tool_exists_in_registry():
    registry = _plugin_tool_names()
    missing: list[str] = []
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        meta = llm_client._parse_skill_front_matter(path.read_text(encoding="utf-8"))
        for tool in meta.get("tools") or []:
            if tool not in registry:
                missing.append(f"{path.name}: {tool}")
    assert not missing, "frontmatter tools: entries missing from the MCP registry: " + ", ".join(
        missing
    )


def test_author_declared_length_for_known_skills():
    """All twelve shipped skill files carry a non-empty ``tools:`` field."""
    names = []
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        meta = llm_client._parse_skill_front_matter(path.read_text(encoding="utf-8"))
        if not meta.get("tools"):
            names.append(path.name)
    assert names == [], f"skills missing tools: declarations: {names}"
