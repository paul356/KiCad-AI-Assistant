"""Skill lookup tools — serve on-demand workflow guidance to the LLM.

Skills are Markdown files with YAML front matter::

    ---
    name: schematic-placement
    priority: 50
    description: "Symbol placement workflow, find_free_area, bbox geometry"
    ---
    # Recommended Placement Workflow
    ...

The LLM discovers available skills via ``list_skills()`` and loads the full
content of a single skill via ``get_skill(name)``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from fastmcp import FastMCP

from kcaa.utils.config import config

log = logging.getLogger(__name__)

# Plugin mode: server_manager._build_env() sets KCAA_SKILLS_DIR to
# kicad_plugin/skills/.  Standalone mode: default to the kcaa data
# directory under KiCad's config dir (e.g. ~/.config/kicad/10.0/kcaa/skills/).
_DEFAULT_SKILLS = os.path.join(config.get_kcaa_data_dir(), "skills")
_SKILLS_DIR = Path(os.environ.get("KCAA_SKILLS_DIR", _DEFAULT_SKILLS))

# Skill names must be lowercase slugs: letters, digits, hyphens.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Return ``(meta, body)`` from a Markdown document with YAML front matter.

    Only flat ``key: value`` pairs are parsed — no nested structures.
    If the document has no front-matter fence the meta dict is empty and the
    full text is returned as the body.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    front_matter_str = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in front_matter_str.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _load_all_skills() -> list[dict[str, str]]:
    """Return metadata (without body) for every valid skill file, sorted by
    priority descending then name ascending.
    """
    if not _SKILLS_DIR.exists():
        return []
    skills = []
    for path in _SKILLS_DIR.glob("*.md"):
        try:
            meta, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
            name = meta.get("name") or path.stem
            skills.append(
                {
                    "name": name,
                    "description": meta.get("description", ""),
                    "priority": meta.get("priority", "50"),
                }
            )
        except Exception:
            log.warning("Failed to read skill file %s", path)
    return sorted(skills, key=lambda s: (-int(s["priority"]), s["name"]))


def register_skill_tools(mcp: FastMCP) -> None:
    """Register skill discovery and retrieval tools on *mcp*."""

    @mcp.tool()
    def list_skills() -> str:
        """List all available on-demand workflow skills.

        Returns a catalog of skill names and one-line descriptions.  Call
        ``get_skill(name)`` to load the full guidance for a specific skill
        into your context.
        """
        skills = _load_all_skills()
        if not skills:
            return "No workflow skills are currently available."
        lines = [f"- {s['name']}: {s['description']}" for s in skills]
        return "Available workflow skills:\n" + "\n".join(lines)

    @mcp.tool()
    def get_skill(name: str) -> str:
        """Load detailed workflow guidance for the named skill.

        The returned content is injected into your context for the remainder
        of this session — you do not need to call this tool again for the
        same skill.

        Args:
            name: Skill name as shown in ``list_skills()`` output
                  (e.g. ``"schematic-placement"``).
        """
        if not _VALID_NAME_RE.match(name):
            return (
                f"Invalid skill name '{name}'. "
                "Names must be lowercase letters, digits, and hyphens "
                "(e.g. 'schematic-placement'). "
                "Call list_skills() to see available options."
            )

        # Resolve and validate path stays inside _SKILLS_DIR.
        candidate = (_SKILLS_DIR / f"{name}.md").resolve()
        try:
            candidate.relative_to(_SKILLS_DIR.resolve())
        except ValueError:
            return f"Invalid skill name '{name}'."

        if not candidate.exists():
            available = [s["name"] for s in _load_all_skills()]
            if available:
                return f"Skill '{name}' not found. Available skills: {', '.join(available)}"
            return f"Skill '{name}' not found. No skills are currently available."

        _, body = _parse_front_matter(candidate.read_text(encoding="utf-8"))
        return body
