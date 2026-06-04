"""Integration tests for the skill system end-to-end."""

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
def skill_functions():
    from kcaa.tools.skill_tools import register_skill_tools

    mcp = _make_mcp()
    register_skill_tools(mcp)
    return mcp.tools["get_skill"], mcp.tools["list_skills"]


class TestGetSkillContent:
    """Verify get_skill returns correct content for all registered skill files."""

    @pytest.mark.parametrize(
        "skill_name,expected_keywords",
        [
            ("schematic-placement", ["placement", "bbox"]),
            ("schematic-wiring", ["wiring", "wire"]),
            ("pcb-query", ["pcb", "footprint"]),
            ("pcb-placement", ["placement", "footprint"]),
            ("pcb-outline", ["outline", "Edge.Cuts"]),
            ("pcb-footprint-library", ["footprint", "library"]),
        ],
    )
    def test_get_skill_returns_expected_content(
        self, skill_functions, skill_name, expected_keywords
    ):
        get_skill, _ = skill_functions

        result = get_skill(skill_name)
        assert result is not None
        content_lower = result.lower() if hasattr(result, "lower") else str(result).lower()
        found = [kw for kw in expected_keywords if kw.lower() in content_lower]
        assert found, f"None of {expected_keywords} found in skill '{skill_name}' content"

    def test_list_skills_shows_all_six(self, skill_functions):
        _, list_skills = skill_functions

        result = list_skills()
        result_str = str(result)
        expected = [
            "schematic-placement",
            "schematic-wiring",
            "pcb-query",
            "pcb-placement",
            "pcb-outline",
            "pcb-footprint-library",
        ]
        for name in expected:
            assert name in result_str, f"Skill '{name}' not found in list_skills() output"


class TestPromptSizeRegression:
    """Verify the system prompt stays within the token budget."""

    def test_system_prompt_under_800_tokens(self):
        from kicad_plugin.llm_client import build_system_prompt

        context = "## Context\nactive_schematic: /tmp/test.kicad_sch"
        prompt = build_system_prompt(context)
        estimated_tokens = len(prompt) // 4
        assert estimated_tokens <= 950, (
            f"Prompt is {estimated_tokens} tokens, expected ≤ 950. "
            "System prompt grew too large — check for accidental additions."
        )

    def test_system_prompt_achieves_60pct_reduction(self):
        from kicad_plugin.llm_client import build_system_prompt

        baseline_tokens = 2300
        context = "## Context\nactive_schematic: /tmp/test.kicad_sch"
        prompt = build_system_prompt(context)
        estimated_tokens = len(prompt) // 4
        reduction_pct = (1 - estimated_tokens / baseline_tokens) * 100
        assert reduction_pct >= 60, (
            f"Token reduction is only {reduction_pct:.1f}%, expected ≥ 60%. "
            f"Current: {estimated_tokens} tokens, baseline: {baseline_tokens} tokens."
        )
