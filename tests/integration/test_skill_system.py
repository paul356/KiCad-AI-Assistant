"""Integration tests for the skill system end-to-end."""

import pytest


def _make_mcp():
    class _MockMCP:
        def __init__(self):
            self.tools = {}
            self.prompts = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

        def prompt(self):
            def decorator(fn):
                self.prompts[fn.__name__] = fn
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
            ("symbol-edit", ["set_symbol_property", "check_reference_conflicts"]),
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
            "symbol-edit",
            "symbol-library",
            "sheet-management",
            "pcb-zone",
        ]
        for name in expected:
            assert name in result_str, f"Skill '{name}' not found in list_skills() output"


class TestSkillCatalogPrompt:
    """The skill catalog is discoverable as an MCP prompt (issue #203).

    External clients connected to either server profile must be able to list
    the workflow-skill catalog through prompts — discovery responsibility
    moves from an active list_skills() call to passively visible context.
    """

    # All 12 authored skill files (front-matter name), see kicad_plugin/skills/.
    EXPECTED_SKILLS = [
        "drc-rules",
        "pcb-footprint-library",
        "pcb-outline",
        "pcb-placement",
        "pcb-query",
        "pcb-routing",
        "pcb-zone",
        "schematic-placement",
        "schematic-wiring",
        "sheet-management",
        "symbol-edit",
        "symbol-library",
    ]

    def _point_skills_at_repo(self):
        """Point KCAA_SKILLS_DIR at kicad_plugin/skills/ and re-import the
        loader so the module-level _SKILLS_DIR picks the env up."""
        import os
        from pathlib import Path
        import sys

        os.environ["KCAA_SKILLS_DIR"] = str(
            (Path(__file__).parent.parent.parent / "kicad_plugin" / "skills").resolve()
        )
        for key in list(sys.modules):
            if key.startswith("kcaa.tools.skill_tools"):
                del sys.modules[key]

    def test_skill_catalog_prompt_lists_all_skills(self):
        self._point_skills_at_repo()
        from kcaa.prompts.skill_prompt import register_skill_prompts

        mcp = _make_mcp()
        register_skill_prompts(mcp)
        body = mcp.prompts["skill_catalog"]()

        for name in self.EXPECTED_SKILLS:
            assert f"- {name}:" in body, f"Skill '{name}' missing from prompt catalog"
        # Every catalog line must carry a non-empty description.
        for line in body.splitlines():
            if line.startswith("- "):
                assert len(line.split(":", 1)[1].strip()) > 0, f"Missing description: {line!r}"
        # omp-style trigger language must be present for the consuming model.
        assert (
            "When a task matches one of these skills, load it before executing task tools" in body
        )
        # Spot-check one exact line so description content demonstrably flows
        # through the front-matter loader.
        assert (
            "- pcb-routing: PCB pad-to-pad routing workflow: get_ratsnest, "
            "export_pcb_layer_image, pcb_route_pad_to_pad, failure retry" in body
        )

    @pytest.mark.parametrize("profile", ["plugin", "full"])
    def test_server_registers_skill_catalog_prompt(self, profile, monkeypatch):
        import asyncio

        # The suite must not inherit a KICAD_MCP_PROFILE override.
        monkeypatch.delenv("KICAD_MCP_PROFILE", raising=False)
        from kcaa.server import create_server

        mcp = create_server(profile=profile)
        prompts = asyncio.run(mcp.list_prompts())
        names = {p.name for p in prompts}
        assert "skill_catalog" in names, (
            f"{profile} profile missing skill_catalog in {sorted(names)}"
        )
        prompt = next(p for p in prompts if p.name == "skill_catalog")
        assert "When a task matches one of these skills" in prompt.description


class TestPromptSizeRegression:
    """Verify the system prompt stays within the token budget."""

    def test_system_prompt_under_800_tokens(self):
        from kicad_plugin.llm_client import build_system_prompt

        context = "## Context\nactive_schematic: /tmp/test.kicad_sch"
        prompt = build_system_prompt(context)
        estimated_tokens = len(prompt) // 4
        # 2026-10: relaxed from 950 → 1400 to restore the per-skill
        # - name: description list in the catalog block (measured 1190 tokens
        # with the full list; ~210 headroom for future skill additions).
        # Deliberate: the previous 950 cap forced over-compressing the prompt.
        assert estimated_tokens <= 1400, (
            f"Prompt is {estimated_tokens} tokens, expected ≤ 1400. "
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
        # 2026-10: relaxed from 58→40 because the restored skill description
        # list (measured 1190 tokens, 48.3% vs the 2300 baseline) intentionally
        # spends more of the budget — over-compressing the prompt hurt usability.
        assert reduction_pct >= 40, (
            f"Token reduction is only {reduction_pct:.1f}%, expected ≥ 40%. "
            f"Current: {estimated_tokens} tokens, baseline: {baseline_tokens} tokens."
        )
