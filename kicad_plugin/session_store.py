"""Session persistence for the KiCad AI Assistant (schema v2).

Schema history
--------------
v1 (original)::

    {"version": 1, "title", "timestamp", "conv_entries", "llm_history"}

v2 (project-scoped): adds ``project_path`` — the absolute path of the
``.kicad_pro`` the session was created in, or ``None`` when no project was
open at save time::

    {"version": 2, "project_path", "title", "timestamp",
     "conv_entries", "llm_history"}

Project scoping rules
---------------------
- A v2 session is owned by exactly one project (``project_path``).
- A v1 file (missing ``project_path``) is *legacy*: it must never be
  auto-restored into a project, and is not offered in project-scoped lookups.
  It stays loadable via the manual Load Session dialog in the UI.
- ``current.json`` (symlink or plain-text fallback) points at the most
  recently active session file regardless of project; the caller decides
  whether the pointed session belongs to the open project.
"""

from __future__ import annotations

import datetime
import glob
import json
import logging
import os

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SESSIONS_DIRNAME = "kicad_ai_sessions"
CURRENT_LINK_NAME = "current.json"
SESSION_PREFIX = "session_"
SESSION_SUFFIX = ".json"
SESSION_GLOB = SESSION_PREFIX + "*" + SESSION_SUFFIX


def sessions_dir(config_dir: str) -> str:
    """Directory holding session files under the kcaa config dir."""
    return os.path.join(config_dir, SESSIONS_DIRNAME)


def current_link_path(config_dir: str) -> str:
    """Path of the current.json pointer file."""
    return os.path.join(sessions_dir(config_dir), CURRENT_LINK_NAME)


def resolve_current_session(config_dir: str) -> str | None:
    """Resolve current.json to a session file path, or None.

    Handles both the symlink form and the plain-text fallback.  A dangling
    symlink or a pointer to a missing file resolves to None.
    """
    link = current_link_path(config_dir)
    if not os.path.exists(link):
        return None
    if os.path.islink(link):
        target = os.readlink(link)
        # readlink may return a relative path — resolve against the dir.
        if not os.path.isabs(target):
            target = os.path.join(sessions_dir(config_dir), target)
        if os.path.isfile(target):
            return target
        return None
    try:
        with open(link, encoding="utf-8") as lf:
            fname = lf.read().strip()
        candidate = os.path.join(sessions_dir(config_dir), fname)
        if os.path.isfile(candidate):
            return candidate
    except OSError:
        pass
    return None


def load_session(path: str) -> tuple[dict | None, str | None]:
    """Load a session file.

    Returns (data, None) on success; (None, error) when the file is missing
    or contains invalid JSON.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return None, str(e)


def session_project_path(session: dict | None) -> str | None:
    """Project stamp of a session; None for legacy v1 files."""
    if not session:
        return None
    return session.get("project_path")


def is_project_session(session: dict | None, project_path: str | None) -> bool:
    """True when *session* is v2-owned by exactly *project_path*.

    Legacy v1 files (no project stamp) and a missing current project never
    match, so they can never be auto-restored into a project.
    """
    return bool(project_path) and session_project_path(session) == project_path


def list_session_files(config_dir: str) -> list[str]:
    """All session files under the sessions dir, newest first."""
    return sorted(
        glob.glob(os.path.join(sessions_dir(config_dir), SESSION_GLOB)),
        reverse=True,
    )


def list_project_sessions(config_dir: str, project_path: str | None) -> list[str]:
    """Session files owned by *project_path*, newest first.

    Legacy v1 sessions are never included.
    """
    if not project_path:
        return []
    return [
        f
        for f in list_session_files(config_dir)
        if is_project_session(load_session(f)[0], project_path)
    ]


def list_loadable_sessions(config_dir: str, project_path: str | None) -> list[str]:
    """Sessions offered in the Load dialog: current project's plus unowned.

    Unowned = legacy v1 files (no project stamp) and v2 files saved with no
    project open.  Other projects' sessions are hidden so a session can never
    be picked into the wrong project.  Current project's sessions come first,
    then unowned — each group newest first.
    """
    own = list_project_sessions(config_dir, project_path) if project_path else []
    unowned = [
        f for f in list_session_files(config_dir) if not session_project_path(load_session(f)[0])
    ]
    return own + unowned


def session_title(conv_entries: list[dict]) -> str:
    """First user message text (truncated), or the default title."""
    return next((e["text"][:60] for e in conv_entries if e["type"] == "user"), "session")


def make_payload(
    conv_entries: list[dict],
    llm_history: list,
    project_path: str | None,
) -> dict:
    """Build the on-disk schema v2 payload for a session."""
    return {
        "version": SCHEMA_VERSION,
        "project_path": project_path,
        "title": session_title(conv_entries),
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "conv_entries": conv_entries,
        "llm_history": llm_history,
    }


def save_session(config_dir: str, filename: str, payload: dict) -> str | None:
    """Write *payload* to sessions_dir/filename with owner-only perms.

    Returns an error string on failure, None on success.
    """
    sessions = sessions_dir(config_dir)
    try:
        os.makedirs(sessions, exist_ok=True)
    except OSError as e:
        return str(e)
    path = os.path.join(sessions, filename)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except OSError as e:
        return str(e)
    return None


def update_current_link(config_dir: str, filename: str) -> None:
    """Atomically point current.json at *filename* (basename).

    Uses a symlink when possible (POSIX); falls back to a plain-text pointer
    file when symlinks are unavailable (e.g. Windows without privilege).
    """
    link = current_link_path(config_dir)
    tmp_link = link + ".tmp"
    try:
        os.symlink(filename, tmp_link)
        os.replace(tmp_link, link)
    except Exception:
        try:
            with open(link, "w", encoding="utf-8") as lf:
                lf.write(filename)
        except Exception as e:
            log.debug("Could not write session file link: %s", e)


def remove_current_link(config_dir: str) -> None:
    """Remove current.json (both symlink and plain-text variants)."""
    link = current_link_path(config_dir)
    try:
        if os.path.lexists(link):  # lexists catches dangling symlinks too
            os.remove(link)
    except OSError:
        pass
