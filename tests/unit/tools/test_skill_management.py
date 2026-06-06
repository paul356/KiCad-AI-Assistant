"""Unit tests for skill management tools: add_skill, append_to_skill, delete_skill."""

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockMCP:
    """Minimal mock for FastMCP that captures registered tools."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def skill_env(tmp_path):
    """Set up a clean temporary skills directory with two pre-existing skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Create a basic skill
    (skills_dir / "test-skill.md").write_text(
        "---\n"
        "name: test-skill\n"
        "priority: 50\n"
        'description: "A test skill"\n'
        "---\n"
        "# Test Skill\n"
        "Some content here.\n"
    )

    # Create another skill
    (skills_dir / "another-skill.md").write_text(
        "---\n"
        "name: another-skill\n"
        "priority: 30\n"
        'description: "Another skill"\n'
        "---\n"
        "# Another Skill\n"
        "More content.\n"
    )

    old_env = os.environ.get("KCAA_SKILLS_DIR")
    os.environ["KCAA_SKILLS_DIR"] = str(skills_dir)

    # Reload the module so _SKILLS_DIR uses the temp dir
    for key in list(sys.modules):
        if key.startswith("kcaa.tools.skill_tools"):
            del sys.modules[key]

    from kcaa.tools.skill_tools import register_skill_tools

    mcp = _MockMCP()
    register_skill_tools(mcp)

    yield mcp.tools, skills_dir

    if old_env is not None:
        os.environ["KCAA_SKILLS_DIR"] = old_env
    else:
        os.environ.pop("KCAA_SKILLS_DIR", None)

    for key in list(sys.modules):
        if key.startswith("kcaa.tools.skill_tools"):
            del sys.modules[key]


# ---------------------------------------------------------------------------
# add_skill
# ---------------------------------------------------------------------------


class TestAddSkill:
    """Tests for add_skill."""

    def test_creates_new_skill_file(self, skill_env):
        tools, skills_dir = skill_env
        result = tools["add_skill"](
            "my-workflow",
            "My custom workflow",
            "# Custom Workflow\nDo something useful.\n",
            priority=60,
        )
        assert "created" in result.lower()

        filepath = skills_dir / "my-workflow.md"
        assert filepath.exists()

        content = filepath.read_text()
        assert "name: my-workflow" in content
        assert 'description: "My custom workflow"' in content
        assert "priority: 60" in content
        assert "# Custom Workflow" in content
        assert "Do something useful." in content

    def test_default_priority(self, skill_env):
        tools, skills_dir = skill_env
        tools["add_skill"]("default-prio", "Desc", "# Body")
        content = (skills_dir / "default-prio.md").read_text()
        assert "priority: 50" in content

    def test_rejects_invalid_name(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="Invalid skill name"):
            tools["add_skill"]("Invalid Name!", "Desc", "# Body")

    def test_rejects_duplicate_name(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="already exists"):
            tools["add_skill"]("test-skill", "Desc", "# Body")

    def test_new_skill_appears_in_list(self, skill_env):
        tools, _ = skill_env
        tools["add_skill"]("new-listable", "Listable", "# Yep")
        result = tools["list_skills"]()
        assert "new-listable" in result

    def test_new_skill_content_retrievable(self, skill_env):
        tools, _ = skill_env
        tools["add_skill"]("retrievable", "Retrieve me", "# Retrievable\nBody text.")
        body = tools["get_skill"]("retrievable")
        assert "# Retrievable" in body
        assert "Body text." in body


# ---------------------------------------------------------------------------
# append_to_skill
# ---------------------------------------------------------------------------


class TestAppendToSkill:
    """Tests for append_to_skill."""

    def test_appends_content_to_existing_skill(self, skill_env):
        tools, skills_dir = skill_env
        result = tools["append_to_skill"]("test-skill", "## New Section\nAdded content.")
        assert "appended" in result.lower()

        content = (skills_dir / "test-skill.md").read_text()
        assert "Some content here." in content
        assert "## New Section" in content
        assert "Added content." in content

    def test_appended_content_is_separated_by_blank_line(self, skill_env):
        tools, skills_dir = skill_env
        tools["append_to_skill"]("test-skill", "Extra paragraph.")
        content = (skills_dir / "test-skill.md").read_text()
        assert "Some content here.\n\nExtra paragraph." in content

    def test_appended_content_appears_in_get_skill(self, skill_env):
        tools, _ = skill_env
        tools["append_to_skill"]("test-skill", "## Bonus\nExtra tip.")
        body = tools["get_skill"]("test-skill")
        assert "## Bonus" in body
        assert "Extra tip." in body

    def test_rejects_nonexistent_skill(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="not found"):
            tools["append_to_skill"]("nonexistent", "content")

    def test_rejects_invalid_name(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="Invalid skill name"):
            tools["append_to_skill"]("Bad Name!", "content")

    def test_multiple_appends_accumulate(self, skill_env):
        tools, skills_dir = skill_env
        tools["append_to_skill"]("test-skill", "First append.")
        tools["append_to_skill"]("test-skill", "Second append.")
        content = (skills_dir / "test-skill.md").read_text()
        assert "First append." in content
        assert "Second append." in content


# ---------------------------------------------------------------------------
# delete_skill
# ---------------------------------------------------------------------------


class TestDeleteSkill:
    """Tests for delete_skill (soft-delete)."""

    def test_moves_file_to_deleted_dir(self, skill_env):
        tools, skills_dir = skill_env
        result = tools["delete_skill"]("test-skill")
        assert "moved" in result.lower()

        assert not (skills_dir / "test-skill.md").exists()

        deleted_dir = skills_dir / ".deleted"
        assert deleted_dir.is_dir()
        assert (deleted_dir / "test-skill.md").exists()

    def test_deleted_skill_removed_from_list(self, skill_env):
        tools, _ = skill_env
        tools["delete_skill"]("test-skill")
        result = tools["list_skills"]()
        assert "test-skill" not in result
        assert "another-skill" in result

    def test_deleted_skill_not_gettable(self, skill_env):
        tools, _ = skill_env
        tools["delete_skill"]("test-skill")
        with pytest.raises(ValueError, match="not found"):
            tools["get_skill"]("test-skill")

    def test_rejects_nonexistent_skill(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="not found"):
            tools["delete_skill"]("nonexistent")

    def test_rejects_invalid_name(self, skill_env):
        tools, _ = skill_env
        with pytest.raises(ValueError, match="Invalid skill name"):
            tools["delete_skill"]("bad name")

    def test_rename_on_name_collision(self, skill_env):
        tools, skills_dir = skill_env
        tools["delete_skill"]("test-skill")

        tools["add_skill"]("test-skill", "New version", "# New content")

        tools["delete_skill"]("test-skill")
        deleted_dir = skills_dir / ".deleted"

        original = deleted_dir / "test-skill.md"
        renamed = deleted_dir / "test-skill-1.md"
        assert original.exists()
        assert renamed.exists()

        orig_content = original.read_text()
        renamed_content = renamed.read_text()
        assert orig_content != renamed_content


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestSkillManagementEdgeCases:
    """Edge case coverage for the new management tools."""

    @pytest.fixture
    def empty_skills_env(self, tmp_path):
        """Skills dir with no pre-existing .md files."""
        skills_dir = tmp_path / "skills_empty"
        skills_dir.mkdir()

        old_env = os.environ.get("KCAA_SKILLS_DIR")
        os.environ["KCAA_SKILLS_DIR"] = str(skills_dir)

        for key in list(sys.modules):
            if key.startswith("kcaa.tools.skill_tools"):
                del sys.modules[key]

        from kcaa.tools.skill_tools import register_skill_tools

        mcp = _MockMCP()
        register_skill_tools(mcp)

        yield mcp.tools, skills_dir

        if old_env is not None:
            os.environ["KCAA_SKILLS_DIR"] = old_env
        else:
            os.environ.pop("KCAA_SKILLS_DIR", None)

        for key in list(sys.modules):
            if key.startswith("kcaa.tools.skill_tools"):
                del sys.modules[key]

    def test_add_skill_to_empty_dir(self, empty_skills_env):
        tools, skills_dir = empty_skills_env
        tools["add_skill"]("first-ever", "First!", "# Hello")
        assert (skills_dir / "first-ever.md").exists()

    def test_list_empty_dir(self, empty_skills_env):
        tools, _ = empty_skills_env
        result = tools["list_skills"]()
        assert "No workflow skills" in result

    def test_append_and_get_after_add(self, empty_skills_env):
        tools, _ = empty_skills_env
        tools["add_skill"]("growable", "Will grow", "# Start")
        tools["append_to_skill"]("growable", "## Middle")
        tools["append_to_skill"]("growable", "## End")
        body = tools["get_skill"]("growable")
        assert "Start" in body
        assert "Middle" in body
        assert "End" in body

    def test_add_skill_name_with_digits(self, empty_skills_env):
        tools, skills_dir = empty_skills_env
        tools["add_skill"]("rf-v2", "RF workflow v2", "# V2 Content")
        assert (skills_dir / "rf-v2.md").exists()
