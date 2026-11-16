"""Resolve the KiCad project that owns an arbitrary file path.

KiCad stores the project as a ``.kicad_pro`` at the project root, but
schematics may live in subdirectories and the board/sheet stem is free to
differ from the project stem (for example ``aria.kicad_pro`` owning
``hardware/main_bus.kicad_sch``).  There is therefore no reliable sibling
mapping from a file to its project: the only dependable rule is that the
owning ``.kicad_pro`` sits in the file's directory or in one of its
ancestors.

This module is deliberately pure filesystem code (``os.path`` only, no
KiCad imports, no logging) so both the MCP server and lightweight callers
can use it without side effects.
"""

from __future__ import annotations

import os

PROJECT_SUFFIX = ".kicad_pro"


def _is_project_name(name: str) -> bool:
    """Return True when *name* has the project suffix, case-insensitively."""
    return name.lower().endswith(PROJECT_SUFFIX)


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
    """Pick one project deterministically from several in the same directory.

    Order (all criteria compared in sequence, later ones only break ties):

    1. a project whose stem has a matching ``.kicad_sch`` and/or
       ``.kicad_pcb`` in the same directory (the normal KiCad layout);
    2. otherwise a project whose stem equals *queried_stem*;
    3. otherwise the lexicographically smallest filename.
    """

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


def find_project_file(path: str) -> str | None:
    """Return the absolute path of the ``.kicad_pro`` that owns *path*.

    *path* may be a ``.kicad_pro`` itself, a ``.kicad_sch``, a
    ``.kicad_pcb``, a directory inside a project, or any other file inside
    a project directory.

    A ``.kicad_pro`` argument is returned unchanged when it exists and
    ``None`` is returned when it does not.  Every other path is resolved by
    walking from its directory upward to the filesystem root; the first
    directory containing at least one ``.kicad_pro`` wins.  When that
    directory contains several projects, the deterministic choice is:

    1. prefer a project whose stem has a matching ``.kicad_sch`` and/or
       ``.kicad_pcb`` in the same directory;
    2. then a project whose stem equals the queried file's stem;
    3. then the lexicographically smallest filename.

    The returned path always exists; ``None`` means no project was found.
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


def find_project_root(path: str) -> str | None:
    """Return the directory containing the project that owns *path*, or None.

    This is :func:`find_project_file` followed by taking its directory, so
    it inherits the same upward search and deterministic tie-breaking.
    """
    project_file = find_project_file(path)
    return os.path.dirname(project_file) if project_file is not None else None


def project_name(path: str) -> str | None:
    """Return the owning project's stem for *path*, or None.

    *path* is resolved with :func:`find_project_file`, so it may be a
    schematic, a board, an existing ``.kicad_pro`` or any other file inside
    the project directory.  This is the single project-name derivation in the
    codebase; callers that need a display fallback add it locally.
    """
    project_file = find_project_file(path)
    if project_file is None:
        return None
    return os.path.splitext(os.path.basename(project_file))[0]
