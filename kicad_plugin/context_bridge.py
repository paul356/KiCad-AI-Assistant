"""
Context bridge: collect the active KiCad editor context and return it
as a structured dict that the plugin injects into every LLM request.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# KiCad window titles name project/board/schematic files, e.g.
# "PCB Editor: /path/to/board.kicad_pcb [*]" or a .kicad_pro path.
_TITLE_PROJECT_RE = re.compile(r"([^\s\[]+\.kicad_pro)\b")
_TITLE_FRAME_RE = re.compile(r"([^\s\[]+\.kicad_(?:pcb|sch))\b")

_PROJECT_SUFFIX = ".kicad_pro"


def _is_project_name(name: str) -> bool:
    """Return True when *name* has the project suffix, case-insensitively."""
    return name.lower().endswith(_PROJECT_SUFFIX)


def _project_candidates(directory: str) -> list[str]:
    """Return the existing ``.kicad_pro`` files directly inside *directory*."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name)
        for name in names
        if _is_project_name(name) and os.path.isfile(os.path.join(directory, name))
    )


def _pick_candidate(candidates: list[str], queried_stem: str) -> str:
    """Pick one project deterministically from several in one directory."""

    def sort_key(project_path: str) -> tuple[int, int, str]:
        stem = os.path.splitext(os.path.basename(project_path))[0]
        directory = os.path.dirname(project_path)
        has_matching_frame = os.path.isfile(
            os.path.join(directory, f"{stem}.kicad_sch")
        ) or os.path.isfile(os.path.join(directory, f"{stem}.kicad_pcb"))
        return (
            0 if has_matching_frame else 1,
            0 if stem == queried_stem else 1,
            os.path.basename(project_path),
        )

    return min(candidates, key=sort_key)


def find_project_for_path(path: str) -> str | None:
    """Return the ``.kicad_pro`` that owns *path* by searching upward.

    This mirrors :func:`kcaa.utils.project_resolution.find_project_file`;
    the plugin cannot import ``kcaa`` because it runs inside KiCad's
    Python, so the rule is duplicated here.  A ``.kicad_pro`` argument is
    returned when it exists.  Other paths walk from their directory up to
    the filesystem root; the first directory with a project wins.  Several
    projects in one directory are resolved deterministically: prefer the
    one whose stem has a matching ``.kicad_sch``/``.kicad_pcb``, then the
    queried stem, then the lexicographically smallest filename.
    """
    target = os.path.abspath(path)

    if _is_project_name(target):
        return target if os.path.isfile(target) else None

    directory = target if os.path.isdir(target) else os.path.dirname(target)
    queried_stem = os.path.splitext(os.path.basename(target))[0]

    while True:
        candidates = _project_candidates(directory)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            return _pick_candidate(candidates, queried_stem)

        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def project_path_from_title(title: str) -> str | None:
    """Extract a ``.kicad_pro`` path from a KiCad window title, or None.

    Editor windows title the active file (``.kicad_pcb`` / ``.kicad_sch``),
    the project manager window may title the ``.kicad_pro`` itself.  When
    only a board/schematic path is named, the owning project is found by
    searching that file's directory and its ancestors; the stem may differ
    and the sheet may live in a subdirectory.
    """
    for m in _TITLE_PROJECT_RE.finditer(title):
        return os.path.abspath(m.group(1))
    m = _TITLE_FRAME_RE.search(title)
    if not m:
        return None
    return find_project_for_path(m.group(1))


def _try_import_pcbnew():
    try:
        import pcbnew  # noqa: F401

        return pcbnew
    except ImportError:
        return None


def collect_context() -> dict[str, Any]:
    """
    Return the current KiCad editor context.

    Fields:
        active_project     Absolute path to .kicad_pro, or None
        active_schematic   Absolute path to .kicad_sch (root), or None
        active_pcb         Absolute path to .kicad_pcb, or None
        active_sheet       Sheet path in hierarchical schematics, or None
    """
    ctx: dict[str, Any] = {
        "active_project": None,
        "active_schematic": None,
        "active_pcb": None,
        "active_sheet": None,
    }

    pcbnew = _try_import_pcbnew()
    if pcbnew is None:
        log.debug("pcbnew not available — returning empty context")
        return ctx

    # ------------------------------------------------------------------ #
    # PCB file path (single GetBoard() call)
    # ------------------------------------------------------------------ #
    try:
        board = pcbnew.GetBoard()
        if board:
            pcb_path = board.GetFileName()
            if pcb_path:
                ctx["active_pcb"] = os.path.abspath(pcb_path)
                pro_path = find_project_for_path(pcb_path)
                if pro_path is not None:
                    ctx["active_project"] = pro_path
                    # The root schematic is always <project stem>.kicad_sch
                    # in the project directory; the sheet being edited may
                    # be a nested file with a different stem.
                    root_sch = os.path.join(
                        os.path.dirname(pro_path),
                        os.path.splitext(os.path.basename(pro_path))[0] + ".kicad_sch",
                    )
                    if os.path.isfile(root_sch):
                        ctx["active_schematic"] = os.path.abspath(root_sch)
    except Exception as e:
        log.debug("Could not collect PCB context: %s", e)

    return ctx


def context_to_system_prompt_block(ctx: dict[str, Any]) -> str:
    """
    Render the context dict as a plain-text block for insertion into
    the LLM system prompt.
    """
    lines = ["## Active KiCad Context"]

    if ctx.get("active_project"):
        lines.append(f"Active project: {ctx['active_project']}")
    else:
        lines.append("Active project: (none)")

    if ctx.get("active_schematic"):
        lines.append(f"Active schematic: {ctx['active_schematic']}")
    else:
        lines.append("Active schematic: (none)")

    if ctx.get("active_pcb"):
        lines.append(f"Active PCB: {ctx['active_pcb']}")
    else:
        lines.append("Active PCB: (none)")

    if ctx.get("active_sheet"):
        lines.append(f"Active sheet path: {ctx['active_sheet']}")

    if ctx.get("active_schematic"):
        lines.append(
            f"\nWhen editing the schematic, use {ctx['active_schematic']!r} "
            "as the schematic_path argument for all editing tools unless the "
            "engineer specifies a different file."
        )

    if ctx.get("active_pcb"):
        lines.append(
            f"\nWhen editing the PCB, use {ctx['active_pcb']!r} "
            "as the pcb_path argument for all PCB tools unless the "
            "engineer specifies a different file."
        )

    return "\n".join(lines)
