"""Skill catalog prompt — passive skill discovery for MCP clients (issue #203).

External MCP clients (Claude Desktop, Cursor, Hermes, …) connected to either
server profile discover the workflow-skill catalog through the prompt list
instead of having to call ``list_skills()`` — discovery responsibility moves
from an active tool call to passively visible context, matching the omp
skill design.

The catalog is rendered from the same source as the ``list_skills`` tool
(``kcaa.tools.skill_tools._load_all_skills``), so the prompt and the tool
never drift apart.
"""

from fastmcp import FastMCP

from kcaa.tools.skill_tools import _load_all_skills


def register_skill_prompts(mcp: FastMCP) -> None:
    """Register the workflow-skill catalog prompt on *mcp*.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.prompt()
    def skill_catalog() -> str:
        """List every workflow skill (name + description) for on-demand loading.

        When a task matches one of these skills, load it before executing
        task tools (with ``get_skill(name)`` where available) — the guides
        carry workflows and pitfalls that bare parameter schemas cannot
        convey.
        """
        skills = _load_all_skills()
        if not skills:
            return "No workflow skills are currently available."
        lines = [f"- {s['name']}: {s['description']}" for s in skills]
        return (
            "Available workflow skills (name + description). "
            "When a task matches one of these skills, load it before executing "
            "task tools:\n" + "\n".join(lines)
        )
