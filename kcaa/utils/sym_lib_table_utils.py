"""
Write-side helpers for KiCad sym-lib-table files.

Mirror of ``kcaa.utils.fp_lib_table_utils`` for the symbol library table:
a surgical, format-preserving append of a ``(lib ...)`` entry with a
``.bak`` backup.  sym-lib-table has no ``(version N)`` header (unlike
fp-lib-table's ``(version 7)``), so a fresh table is just ``(sym_lib_table
...(lib ...))``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any

import sexpdata

log = logging.getLogger(__name__)

# Characters KiCad accepts in sym-lib-table nicknames.  Slashes, backslashes,
# colons and whitespace are excluded because they would break the table and
# file paths.
_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.+-]")


def sanitize_lib_nickname(nickname: str) -> str:
    """Return *nickname* with characters unsafe for sym-lib-table entries removed.

    :param nickname: Proposed library nickname.
    :returns: Sanitized nickname (may be empty when nothing usable remains).
    """
    return _SAFE_CHARS_RE.sub("_", nickname).strip("_")


def get_user_sym_lib_table_path() -> str:
    """Return the global user sym-lib-table path (may not exist yet).

    Uses the highest-priority KiCad config directory, mirroring the order in
    ``kcaa.utils.pcb_library_utils._default_kicad_config_dirs()``.
    """
    from kcaa.utils import pcb_library_utils

    dirs = pcb_library_utils._default_kicad_config_dirs()
    return os.path.join(dirs[0], "sym-lib-table") if dirs else ""


def _entry_text(nickname: str, uri: str, description: str = "") -> str:
    """Render a ``(lib ...)`` entry as a single line, escaping quotes."""
    pieces = [
        "(lib",
        f'(name "{nickname}")',
        '(type "KiCad")',
        f'(uri "{uri}")',
        '(options "")',
        f'(descr "{description}")',
    ]
    return " ".join(pieces) + ")"


def _last_line_is_closing_paren(text: str) -> bool:
    """Return True when the last non-blank line is ``)`` (pretty layout)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    return lines[-1].strip() == ")"


def register_library_in_table(
    table_path: str,
    nickname: str,
    uri: str,
    description: str = "",
) -> dict[str, Any]:
    """Append a ``(lib ...)`` entry to *table_path* without disturbing the rest.

    Creates the table with a ``(sym_lib_table ...)`` header when it does not
    exist.  Backs up an existing file to ``.bak`` before writing.  If
    *nickname* is already registered the table is left untouched.

    :param table_path: Absolute path to the sym-lib-table file.
    :param nickname: Library nickname to register (sanitized internally).
    :param uri: Library URI, e.g. ``${KICAD10_3RD_PARTY}/symbols/X.kicad_sym``.
    :param description: Optional ``descr`` text for the entry.
    :returns: dict with ``registered`` (bool), ``table_path``, and optional
        ``backup_path`` / ``reason``.
    """
    table_path = os.path.abspath(table_path)
    nickname = sanitize_lib_nickname(nickname)
    if not nickname:
        return {"registered": False, "reason": "nickname empty after sanitization"}

    entry = _entry_text(nickname, uri, description)
    backup_path: str | None = None

    if os.path.isfile(table_path):
        with open(table_path, encoding="utf-8") as fh:
            original = fh.read()
        if f'name "{nickname}"' in original:
            log.info("Library %r already registered in %s", nickname, table_path)
            return {"registered": False, "table_path": table_path, "reason": "already_registered"}
        text = original.rstrip()
        if not text.endswith(")"):
            raise ValueError(f"Malformed sym-lib-table (no closing paren): {table_path}")
        if _last_line_is_closing_paren(original):
            # Pretty layout: insert a new entry line before the final ")".
            idx = original.rfind("\n)")
            new_text = original[: idx + 1] + "\t" + entry + "\n" + original[idx + 1 :]
        else:
            # Compact single-line layout: re-serialize with sexpdata.
            log.warning(
                "sym-lib-table %s is single-line; re-serializing to append entry",
                table_path,
            )
            data = sexpdata.loads(original)
            data.append(sexpdata.loads(entry))
            new_text = sexpdata.dumps(data, pretty_print=True, indent_as="\t") + "\n"
        backup_path = table_path + ".bak"
        shutil.copy2(table_path, backup_path)
    else:
        os.makedirs(os.path.dirname(table_path) or ".", exist_ok=True)
        new_text = "(sym_lib_table\n\t" + entry + "\n)\n"

    tmp_path = table_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, table_path)
    return {
        "registered": True,
        "table_path": table_path,
        "backup_path": backup_path,
    }


# Matches the ``(uri "…")`` token inside a single-line ``(lib ...)`` entry.
_URI_RE = re.compile(r'\(uri\s+"([^"]*)"\)')


def register_or_update_library_in_table(
    table_path: str,
    nickname: str,
    uri: str,
    description: str = "",
) -> dict[str, Any]:
    """Register *nickname* in *table_path*, updating an existing entry's URI.

    Explicit register-or-update for callers that must guarantee a nickname
    resolves to a specific URI (e.g. project-pinned export copies):

    * nickname not registered -> appended, same as
      ``register_library_in_table``;
    * nickname registered with the *same* URI -> table left untouched
      (``registered`` False, ``reason`` "already_registered");
    * nickname registered with a *different* URI -> the entry is rewritten
      in place (URI/descr replaced, other entries and formatting
      preserved), the table is backed up to ``.bak`` and ``updated`` is
      True.

    :param table_path: Absolute path to the sym-lib-table file.
    :param nickname: Library nickname to ensure is registered.
    :param uri: Library URI, e.g. ``${KIPRJMOD}/3rdparty/X.kicad_sym``.
    :param description: Optional ``descr`` text for the entry.
    :returns: dict with ``registered`` (bool), ``table_path``, optional
        ``backup_path`` / ``updated`` (bool), and optional ``reason``
        (``nickname empty after sanitization``, ``already_registered``).
    """
    table_path = os.path.abspath(table_path)
    nickname = sanitize_lib_nickname(nickname)
    if not nickname:
        return {"registered": False, "reason": "nickname empty after sanitization"}
    if not os.path.isfile(table_path):
        return register_library_in_table(table_path, nickname, uri, description)

    with open(table_path, encoding="utf-8") as fh:
        original = fh.read()
    needle = f'name "{nickname}"'
    if needle not in original:
        return register_library_in_table(table_path, nickname, uri, description)

    # Nickname already registered.  Keep the table untouched unless the
    # stored URI actually differs from the requested one.  Entries are
    # written single-line by register_library_in_table / this helper, so the
    # uri token lives on the same line as the name token.
    current_uri: str | None = None
    for ln in original.splitlines():
        if needle in ln:
            match = _URI_RE.search(ln)
            if match:
                current_uri = match.group(1)
            break
    if current_uri == uri:
        log.info("Library %r already registered in %s", nickname, table_path)
        return {"registered": False, "table_path": table_path, "reason": "already_registered"}

    entry = _entry_text(nickname, uri, description)
    new_text: str | None = None
    if _last_line_is_closing_paren(original):
        lines = original.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if ln.strip().startswith("(lib ") and needle in ln:
                lines[i] = "\t" + entry + "\n"
                new_text = "".join(lines)
                break
    if new_text is None:
        # Compact single-line or multi-line entry: re-serialize after
        # swapping in the replacement entry node.
        log.warning(
            "sym-lib-table %s is single-line; re-serializing to update entry",
            table_path,
        )
        data = sexpdata.loads(original)
        replacement = sexpdata.loads(entry)
        new_data = []
        for child in data:
            if (
                isinstance(child, list)
                and len(child) >= 2
                and child[0] == sexpdata.Symbol("lib")
                and any(
                    isinstance(e, list)
                    and len(e) >= 2
                    and e[0] == sexpdata.Symbol("name")
                    and str(e[1]) == nickname
                    for e in child
                )
            ):
                new_data.append(replacement)
            else:
                new_data.append(child)
        new_text = sexpdata.dumps(new_data, pretty_print=True, indent_as="\t") + "\n"

    backup_path = table_path + ".bak"
    shutil.copy2(table_path, backup_path)
    tmp_path = table_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, table_path)
    return {
        "registered": True,
        "table_path": table_path,
        "backup_path": backup_path,
        "updated": True,
    }


def unregister_library_in_table(
    table_path: str,
    nickname: str,
) -> dict[str, Any]:
    """Remove the ``(lib ...)`` entry for *nickname* from *table_path*.

    Only the matching entry is dropped; all other entries keep their
    original formatting (pretty layout is filtered line-by-line, the rare
    compact single-line layout is re-serialized).  Backs up the table to
    ``.bak`` before writing.

    :param table_path: Absolute path to the sym-lib-table file.
    :param nickname: Library nickname whose entry should be removed.
    :returns: dict with ``unregistered`` (bool), ``table_path``, and optional
        ``backup_path`` / ``reason`` (``table not found``,
        ``not_registered``).
    """
    table_path = os.path.abspath(table_path)
    nickname = sanitize_lib_nickname(nickname)
    if not nickname:
        return {"unregistered": False, "reason": "nickname empty after sanitization"}
    if not os.path.isfile(table_path):
        return {"unregistered": False, "reason": "table not found", "table_path": table_path}

    with open(table_path, encoding="utf-8") as fh:
        original = fh.read()
    needle = f'name "{nickname}"'
    if needle not in original:
        return {"unregistered": False, "table_path": table_path, "reason": "not_registered"}

    if _last_line_is_closing_paren(original):
        lines = original.splitlines(keepends=True)
        kept = [ln for ln in lines if not (ln.strip().startswith("(lib ") and needle in ln)]
        new_text = "".join(kept)
    else:
        log.warning(
            "sym-lib-table %s is single-line; re-serializing to drop entry",
            table_path,
        )
        data = sexpdata.loads(original)
        kept_data = [
            child
            for child in data
            if not (
                isinstance(child, list)
                and len(child) >= 2
                and child[0] == sexpdata.Symbol("lib")
                and any(
                    isinstance(entry, list)
                    and len(entry) >= 2
                    and entry[0] == sexpdata.Symbol("name")
                    and str(entry[1]) == nickname
                    for entry in child
                )
            )
        ]
        new_text = sexpdata.dumps(kept_data, pretty_print=True, indent_as="\t") + "\n"

    backup_path = table_path + ".bak"
    shutil.copy2(table_path, backup_path)
    tmp_path = table_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, table_path)
    return {
        "unregistered": True,
        "table_path": table_path,
        "backup_path": backup_path,
    }
