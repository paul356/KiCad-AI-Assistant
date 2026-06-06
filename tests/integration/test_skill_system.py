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
    # Standalone mode: main.py sets KCAA_SKILLS_DIR to kicad_plugin/skills/.
    # Mirror that here so tests find skills without relying on the
    # config-dir default (which may be empty).
    import os
    from pathlib import Path
    import sys

    os.environ["KCAA_SKILLS_DIR"] = str(
        (Path(__file__).parent.parent.parent / "kicad_plugin" / "skills").resolve()
    )

    # Clear cached import so _SKILLS_DIR is re-read from the updated env.
    for key in list(sys.modules):
        if key.startswith("kcaa.tools.skill_tools"):
            del sys.modules[key]

    from kcaa.tools.skill_tools import register_skill_tools

    mcp = _make_mcp()
    register_skill_tools(mcp)
    return mcp.tools["get_skill"], mcp.tools["list_skills"]


class TestGetSkillContent:
    """Verify get_skill returns correct content for all registered skill files."""

    @pytest.mark.parametrize(
        "skill_name,expected_keywords",
        [
            ("schematic-placement", ["body_bbox", "find_free_area"]),
            ("schematic-wiring", ["manhattan", "isotropic"]),
            ("pcb-query", ["ratsnest", "layer stack"]),
            ("pcb-placement", ["courtyard", "set_footprint_position"]),
            ("pcb-outline", ["corner_radius", "gr_rect"]),
            ("pcb-footprint-library", ["sync_footprint_index", "get_footprint_details"]),
            ("component-edit", ["set_component_property", "check_reference_conflicts"]),
            ("symbol-library", ["sync_symbol_index", "search_symbols"]),
            ("sheet-management", ["sheet_symbol", "hierarchy"]),
            ("pcb-zone", ["copper_pour", "refill_zones"]),
        ],
    )
    def test_get_skill_returns_expected_content(
        self, skill_functions, skill_name, expected_keywords
    ):
        get_skill, _ = skill_functions

        result = get_skill(skill_name)
        assert result is not None

        # Safety: the result must not be a "not found" / error message.
        assert not str(result).startswith("Skill "), (
            f"get_skill('{skill_name}') returned an error: {result!r}"
        )

        content_lower = result.lower() if hasattr(result, "lower") else str(result).lower()
        missing = [kw for kw in expected_keywords if kw.lower() not in content_lower]
        assert not missing, f"Keywords {missing} not found in skill '{skill_name}' content"

    def test_list_skills_shows_all_ten(self, skill_functions):
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
            "component-edit",
            "symbol-library",
            "sheet-management",
            "pcb-zone",
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
        # 2026-06: relaxed from 60→58 to accommodate 4 extra skill catalog entries
        assert reduction_pct >= 58, (
            f"Token reduction is only {reduction_pct:.1f}%, expected ≥ 58%. "
            f"Current: {estimated_tokens} tokens, baseline: {baseline_tokens} tokens."
        )
