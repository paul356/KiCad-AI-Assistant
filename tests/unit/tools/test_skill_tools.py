"""Unit tests for kcaa/tools/skill_tools.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(
    directory: Path, stem: str, name: str, description: str, priority: int, body: str
) -> None:
    content = f'---\nname: {name}\ndescription: "{description}"\npriority: {priority}\n---\n' + body
    (directory / f"{stem}.md").write_text(content, encoding="utf-8")


def _make_mcp():
    """Return a minimal mock MCP object that captures registered tools."""

    class _MockMCP:
        def __init__(self):
            self.tools: dict = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    return _MockMCP()


# ---------------------------------------------------------------------------
# Tests for _parse_front_matter (internal helper via public behaviour)
# ---------------------------------------------------------------------------


class TestParseFrontMatter:
    def test_no_front_matter_returns_full_text(self, tmp_path):
        from kcaa.tools.skill_tools import _parse_front_matter

        meta, body = _parse_front_matter("just content\nno fence")
        assert meta == {}
        assert body == "just content\nno fence"

    def test_parses_key_value_pairs(self, tmp_path):
        from kcaa.tools.skill_tools import _parse_front_matter

        text = '---\nname: foo\ndescription: "bar baz"\npriority: 80\n---\nbody text'
        meta, body = _parse_front_matter(text)
        assert meta["name"] == "foo"
        assert meta["description"] == "bar baz"
        assert meta["priority"] == "80"
        assert body == "body text"

    def test_missing_closing_fence_returns_empty_meta(self):
        from kcaa.tools.skill_tools import _parse_front_matter

        text = "---\nname: foo\nbody text"
        meta, body = _parse_front_matter(text)
        assert meta == {}
        assert body == text

    def test_body_leading_newline_stripped(self):
        from kcaa.tools.skill_tools import _parse_front_matter

        text = "---\nname: x\n---\n\n\ncontent here"
        _, body = _parse_front_matter(text)
        assert body == "content here"


# ---------------------------------------------------------------------------
# Tests for list_skills
# ---------------------------------------------------------------------------


class TestListSkills:
    def test_empty_directory_returns_no_skills_message(self, tmp_path):
        from kcaa.tools import skill_tools

        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["list_skills"]()
        assert "No workflow skills" in result

    def test_missing_directory_returns_no_skills_message(self, tmp_path):
        from kcaa.tools import skill_tools

        mcp = _make_mcp()
        missing = tmp_path / "nonexistent"
        with patch.object(skill_tools, "_SKILLS_DIR", missing):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["list_skills"]()
        assert "No workflow skills" in result

    def test_lists_skills_sorted_by_priority_desc(self, tmp_path):
        from kcaa.tools import skill_tools

        _write_skill(tmp_path, "low", "low-priority", "Low desc", 10, "content")
        _write_skill(tmp_path, "high", "high-priority", "High desc", 90, "content")
        _write_skill(tmp_path, "mid", "mid-priority", "Mid desc", 50, "content")
        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["list_skills"]()
        lines = [l for l in result.splitlines() if l.startswith("-")]
        names = [l.split(":")[0].strip("- ") for l in lines]
        assert names == ["high-priority", "mid-priority", "low-priority"]

    def test_lists_name_and_description(self, tmp_path):
        from kcaa.tools import skill_tools

        _write_skill(tmp_path, "sch", "schematic-placement", "Symbol placement", 50, "body")
        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["list_skills"]()
        assert "schematic-placement" in result
        assert "Symbol placement" in result


# ---------------------------------------------------------------------------
# Tests for get_skill
# ---------------------------------------------------------------------------


class TestGetSkill:
    def test_returns_body_for_valid_skill(self, tmp_path):
        from kcaa.tools import skill_tools

        _write_skill(
            tmp_path,
            "schematic-placement",
            "schematic-placement",
            "Placement workflow",
            50,
            "# Placement\nDetailed content here.",
        )
        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["get_skill"]("schematic-placement")
        assert "# Placement" in result
        assert "Detailed content here." in result

    def test_unknown_skill_returns_error_with_available_list(self, tmp_path):
        from kcaa.tools import skill_tools

        _write_skill(
            tmp_path, "schematic-placement", "schematic-placement", "Placement workflow", 50, "body"
        )
        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["get_skill"]("nonexistent")
        assert "not found" in result.lower()
        assert "schematic-placement" in result

    def test_unknown_skill_empty_dir_returns_error(self, tmp_path):
        from kcaa.tools import skill_tools

        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["get_skill"]("anything")
        assert "not found" in result.lower()

    def test_invalid_name_format_rejected(self, tmp_path):
        from kcaa.tools import skill_tools

        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            for bad_name in ["../etc/passwd", "Uppercase", "has space", "", "."]:
                if not bad_name:
                    continue
                result = mcp.tools["get_skill"](bad_name)
                assert "Invalid" in result or "not found" in result.lower(), (
                    f"Expected rejection for name {bad_name!r}, got: {result}"
                )

    def test_path_traversal_rejected(self, tmp_path):
        from kcaa.tools import skill_tools

        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["get_skill"]("../secret")
        assert "Invalid" in result

    def test_body_does_not_include_front_matter(self, tmp_path):
        from kcaa.tools import skill_tools

        _write_skill(tmp_path, "my-skill", "my-skill", "My desc", 50, "This is the body only.")
        mcp = _make_mcp()
        with patch.object(skill_tools, "_SKILLS_DIR", tmp_path):
            skill_tools.register_skill_tools(mcp)
            result = mcp.tools["get_skill"]("my-skill")
        assert "name:" not in result
        assert "priority:" not in result
        assert "This is the body only." in result
