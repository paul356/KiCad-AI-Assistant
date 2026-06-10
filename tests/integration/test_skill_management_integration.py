"""Integration tests for skill management tools (add, append, delete)."""

import os

import pytest


def _make_mcp():
    class _MockMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    return _MockMCP()


@pytest.fixture
def skill_tools(tmp_path):
    """Register all 5 skill tools in an isolated skills directory."""
    import sys

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    os.environ["KCAA_SKILLS_DIR"] = str(skills_dir)

    # Clear cached imports so _SKILLS_DIR is re-read.
    for key in list(sys.modules):
        if key.startswith("kcaa.tools.skill_tools"):
            del sys.modules[key]

    from kcaa.tools.skill_tools import register_skill_tools

    mcp = _make_mcp()
    register_skill_tools(mcp)
    return mcp.tools


# ---- full lifecycle ---------------------------------------------------------


class TestSkillLifecycle:
    """End-to-end: add → list → get → append → get → delete → list."""

    def test_full_lifecycle(self, skill_tools):
        # 1. Start empty
        result = skill_tools["list_skills"]()
        assert "No workflow skills are currently available" in result

        # 2. Add a skill
        result = skill_tools["add_skill"](
            "my-workflow", "A test workflow", "# My Workflow\nStep 1."
        )
        assert "Skill 'my-workflow' created" in result

        # 3. list_skills shows it
        result = skill_tools["list_skills"]()
        assert "my-workflow" in result

        # 4. get_skill returns body content (without front matter)
        result = skill_tools["get_skill"]("my-workflow")
        assert "Step 1" in result

        # 5. append_to_skill
        result = skill_tools["append_to_skill"]("my-workflow", "## Step 2\nMore content.")
        assert "Content appended to skill" in result

        # 6. get_skill shows appended content
        result = skill_tools["get_skill"]("my-workflow")
        assert "Step 1" in result
        assert "Step 2" in result
        assert "More content" in result

        # 7. delete_skill (soft-delete)
        result = skill_tools["delete_skill"]("my-workflow")
        assert "moved to deleted directory" in result.lower()

        # 8. list_skills no longer shows it
        result = skill_tools["list_skills"]()
        assert "my-workflow" not in result

        # 9. get_skill raises
        with pytest.raises(ValueError, match="my-workflow"):
            skill_tools["get_skill"]("my-workflow")

    def test_deleted_file_present(self, skill_tools, tmp_path):
        """Verify the skill file is moved to .deleted/ subdirectory."""
        skill_tools["add_skill"]("to-delete", "Will be deleted", "# Gone soon")
        skill_tools["delete_skill"]("to-delete")

        deleted_dir = tmp_path / "skills" / ".deleted"
        assert deleted_dir.is_dir(), ".deleted/ directory should exist"

        deleted_files = list(deleted_dir.glob("*.md"))
        assert len(deleted_files) == 1, (
            f"Expected 1 .md file in .deleted/, found {len(deleted_files)}"
        )
        content = deleted_files[0].read_text()
        assert "Gone soon" in content

    def test_rename_on_collision(self, skill_tools, tmp_path):
        """Deleting two skills with the same front-matter name renames the second."""
        # Add and delete first instance
        skill_tools["add_skill"]("dup-skill", "First", "# First instance")
        skill_tools["delete_skill"]("dup-skill")

        # Add and delete second instance with same name
        skill_tools["add_skill"]("dup-skill", "Second", "# Second instance")
        skill_tools["delete_skill"]("dup-skill")

        deleted_dir = tmp_path / "skills" / ".deleted"
        deleted_files = sorted(deleted_dir.glob("*.md"))
        assert len(deleted_files) == 2, f"Expected 2 files in .deleted/, found {len(deleted_files)}"

        # One should be the original name, the other renamed
        names = {f.name for f in deleted_files}
        assert "dup-skill.md" in names, f"Expected dup-skill.md in {names}"
        renamed = names - {"dup-skill.md"}
        assert len(renamed) == 1
        assert renamed.pop().startswith("dup-skill-"), f"Expected dup-skill-*.md in {names}"

    def test_add_after_delete(self, skill_tools, tmp_path):
        """Adding a skill with the same name after deletion works."""
        skill_tools["add_skill"]("recycled", "Original", "# Original")
        skill_tools["delete_skill"]("recycled")
        skill_tools["add_skill"]("recycled", "New version", "# New version", 60)

        result = skill_tools["get_skill"]("recycled")
        assert "New version" in result
        assert "Original" not in result

    def test_append_preserves_front_matter(self, skill_tools):
        """Appending content should not modify YAML front matter."""
        skill_tools["add_skill"]("intact", "Keep front matter", "# Body start", 30)
        skill_tools["append_to_skill"]("intact", "## Extra section\nMore text.")

        # Read the raw file to verify front matter is intact
        import os

        skills_dir = os.environ["KCAA_SKILLS_DIR"]
        filepath = os.path.join(skills_dir, "intact.md")
        raw = open(filepath).read()

        # Front matter should be at the top, not repeated
        assert raw.startswith("---\n")
        assert raw.count("---") == 2, "Front matter delimiters should appear exactly twice"
        assert "name: intact" in raw
        assert "priority: 30" in raw


# ---- error handling ----------------------------------------------------------


class TestSkillErrors:
    """Verify validation and error reporting."""

    def test_add_duplicate_name(self, skill_tools):
        skill_tools["add_skill"]("unique", "First", "50", "# First")
        with pytest.raises(ValueError, match="already exists"):
            skill_tools["add_skill"]("unique", "Second", "50", "# Second")

    def test_append_nonexistent(self, skill_tools):
        with pytest.raises(ValueError, match="not found"):
            skill_tools["append_to_skill"]("no-such-skill", "# Content")

    def test_delete_nonexistent(self, skill_tools):
        with pytest.raises(ValueError, match="not found"):
            skill_tools["delete_skill"]("no-such-skill")

    def test_invalid_skill_name(self, skill_tools):
        with pytest.raises(ValueError, match="Invalid"):
            skill_tools["add_skill"]("Bad Name!", "Desc", "50", "# Content")

    def test_default_priority(self, skill_tools):
        """If no priority is given, it defaults to 50."""
        skill_tools["add_skill"]("defaulted", "Default prio", "# Content")

        # Read the raw file to verify default priority
        import os

        skills_dir = os.environ["KCAA_SKILLS_DIR"]
        filepath = os.path.join(skills_dir, "defaulted.md")
        raw = open(filepath).read()
        assert "priority: 50" in raw


# ---- concurrent operations --------------------------------------------------


class TestConcurrentOperations:
    """Verify multiple skills coexist without interference."""

    def test_multiple_skills_independent(self, skill_tools):
        skill_tools["add_skill"]("skill-a", "First skill", "# A content")
        skill_tools["add_skill"]("skill-b", "Second skill", "# B content")
        skill_tools["add_skill"]("skill-c", "Third skill", "# C content")

        result = skill_tools["list_skills"]()
        assert "skill-a" in result
        assert "skill-b" in result
        assert "skill-c" in result

        # Append to one does not affect others
        skill_tools["append_to_skill"]("skill-b", "## B extra")
        a_content = skill_tools["get_skill"]("skill-a")
        c_content = skill_tools["get_skill"]("skill-c")
        assert "B extra" not in a_content
        assert "B extra" not in c_content

        # Delete one does not affect others
        skill_tools["delete_skill"]("skill-a")
        result = skill_tools["list_skills"]()
        assert "skill-a" not in result
        assert "skill-b" in result
        assert "skill-c" in result

    def test_list_skills_ordering(self, skill_tools):
        """Higher priority skills should appear first in list_skills."""
        skill_tools["add_skill"]("low", "Low priority", "# Low", 10)
        skill_tools["add_skill"]("high", "High priority", "# High", 90)
        skill_tools["add_skill"]("mid", "Mid priority", "# Mid", 50)

        result = skill_tools["list_skills"]()
        # Parse skill names from the "- name: description" format
        import re

        names_in_order = re.findall(r"^- (\S+):", result, re.MULTILINE)
        assert names_in_order == ["high", "mid", "low"], (
            f"Expected [high, mid, low], got {names_in_order}"
        )
