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

Plugin users can manage skills through:
- ``add_skill(name, description, priority, content)`` — create a new skill.
- ``append_to_skill(name, content)`` — add content to an existing skill.
- ``delete_skill(name)`` — soft-delete (move to .deleted/ subdirectory).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shutil

from fastmcp import FastMCP

log = logging.getLogger(__name__)

# Plugin mode: server_manager._build_env() sets KCAA_SKILLS_DIR to
# kicad_plugin/skills/.  Standalone mode: main.py sets KCAA_SKILLS_DIR
# similarly.  If neither sets the env var, fall back to kicad_plugin/skills/
# relative to this package.
_DEFAULT_SKILLS = str(Path(__file__).parent.parent.parent / "kicad_plugin" / "skills")
_SKILLS_DIR = Path(os.environ.get("KCAA_SKILLS_DIR", _DEFAULT_SKILLS))

# Soft-delete directory — inside the skills dir so permissions are the same.
_DELETED_DIR = _SKILLS_DIR / ".deleted"

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


def _find_skill_file(name: str) -> Path | None:
    """Return the Path to the skill file whose front-matter ``name``
    matches *name*, or None if no match is found.
    """
    if not _SKILLS_DIR.exists():
        return None
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        try:
            meta, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        candidate = meta.get("name") or path.stem
        if candidate == name:
            return path
    return None


def _unique_deleted_path(filename: str) -> Path:
    """Return a path inside ``_DELETED_DIR`` that won't overwrite an
    existing file.  Appends a counter if the filename already exists.
    """
    _DELETED_DIR.mkdir(parents=True, exist_ok=True)
    target = _DELETED_DIR / filename
    if not target.exists():
        return target
    stem, ext = os.path.splitext(filename)
    counter = 1
    while True:
        target = _DELETED_DIR / f"{stem}-{counter}{ext}"
        if not target.exists():
            return target
        counter += 1


def _build_front_matter_blob(name: str, description: str, priority: int | str) -> str:
    """Build the YAML front-matter block for a skill file."""
    return f'---\nname: {name}\npriority: {priority}\ndescription: "{description}"\n---\n'


def register_skill_tools(mcp: FastMCP) -> None:
    """Register skill discovery, retrieval, and management tools on *mcp*."""

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
            raise ValueError(
                f"Invalid skill name '{name}'. "
                "Names must be lowercase letters, digits, and hyphens "
                "(e.g. 'schematic-placement'). "
                "Call list_skills() to see available options."
            )

        # Match by front-matter name rather than filename so that the
        # two can differ (e.g. hyphen in front matter, underscore on disk).
        if not _SKILLS_DIR.exists():
            log.warning("Skills directory %s does not exist", _SKILLS_DIR)
            raise ValueError(f"Skill '{name}' not found. No skills are currently available.")

        for path in sorted(_SKILLS_DIR.glob("*.md")):
            try:
                meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("Failed to read skill file %s", path)
                continue
            candidate_name = meta.get("name") or path.stem
            if candidate_name == name:
                return body

        log.warning(
            "Skill '%s' not found in %s (available files: %s)",
            name,
            _SKILLS_DIR,
            [p.name for p in sorted(_SKILLS_DIR.glob("*.md"))],
        )
        available = [s["name"] for s in _load_all_skills()]
        if available:
            raise ValueError(f"Skill '{name}' not found. Available skills: {', '.join(available)}")
        raise ValueError(f"Skill '{name}' not found. No skills are currently available.")

    # ------------------------------------------------------------------
    # Skill management tools (write operations for plugin users)
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_skill(name: str, description: str, content: str, priority: int = 50) -> str:
        """Create a new workflow skill.

        Args:
            name: Skill name — lowercase letters, digits, and hyphens
                  (e.g. ``"my-custom-workflow"``).
            description: One-line description shown in ``list_skills()``.
            content: Markdown body of the skill (workflow guidance).
            priority: Display priority (higher = shown first).  Default 50.
        """
        if not _VALID_NAME_RE.match(name):
            raise ValueError(
                f"Invalid skill name '{name}'. "
                "Names must use lowercase letters, digits, and hyphens "
                "(e.g. 'my-custom-workflow')."
            )

        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        # Reject duplicates
        if _find_skill_file(name) is not None:
            raise ValueError(
                f"A skill named '{name}' already exists. "
                "Use append_to_skill() to add content to it, or "
                "delete_skill() first to replace it."
            )

        blob = _build_front_matter_blob(name, description, priority)
        filepath = _SKILLS_DIR / f"{name}.md"
        filepath.write_text(blob + content + "\n", encoding="utf-8")
        log.info("Created skill file %s", filepath)
        return f"Skill '{name}' created at {filepath}"

    @mcp.tool()
    def append_to_skill(name: str, content: str) -> str:
        """Append content to an existing skill's body.

        Useful for extending a workflow with additional steps or notes
        without replacing the entire skill file.

        Args:
            name: Skill name as shown in ``list_skills()``.
            content: Markdown text to append to the skill body.
        """
        if not _VALID_NAME_RE.match(name):
            raise ValueError(
                f"Invalid skill name '{name}'. "
                "Names must use lowercase letters, digits, and hyphens."
            )

        path = _find_skill_file(name)
        if path is None:
            available = [s["name"] for s in _load_all_skills()]
            hint = f" Available skills: {', '.join(available)}" if available else ""
            raise ValueError(f"Skill '{name}' not found.{hint}")

        current = path.read_text(encoding="utf-8")
        # Ensure exactly one blank line between existing body and new content.
        new_body = current.rstrip() + "\n\n" + content.strip() + "\n"
        path.write_text(new_body, encoding="utf-8")
        log.info("Appended content to skill %s", path)
        return f"Content appended to skill '{name}'."

    @mcp.tool()
    def delete_skill(name: str) -> str:
        """Soft-delete a skill by moving its file to a .deleted/ subdirectory.

        The skill file is NOT permanently removed — it is moved to
        ``skills/.deleted/`` where you can manually recover it.  If a file
        with the same name already exists in the deleted directory, the
        moved file is automatically renamed (e.g. ``my-skill-1.md``).

        Args:
            name: Skill name to delete.
        """
        if not _VALID_NAME_RE.match(name):
            raise ValueError(
                f"Invalid skill name '{name}'. "
                "Names must use lowercase letters, digits, and hyphens."
            )

        path = _find_skill_file(name)
        if path is None:
            available = [s["name"] for s in _load_all_skills()]
            hint = f" Available skills: {', '.join(available)}" if available else ""
            raise ValueError(f"Skill '{name}' not found.{hint}")

        dest = _unique_deleted_path(path.name)
        shutil.move(str(path), str(dest))
        log.info("Moved skill %s → %s", path, dest)
        return (
            f"Skill '{name}' moved to deleted directory: {dest}\n"
            f"To restore, move the file back to {_SKILLS_DIR}/"
        )
