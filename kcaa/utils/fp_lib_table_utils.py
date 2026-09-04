"""
Write-side helpers for KiCad fp-lib-table files.

The read side (parsing, env expansion, library listing) lives in
``kcaa.utils.pcb_library_utils``.  This module adds the write primitive
needed to register a newly created user footprint library: a surgical,
format-preserving append of a ``(lib ...)`` entry with a ``.bak`` backup.

The append is intentionally text-based instead of a full sexpdata
re-serialization so that untouched library entries keep their original
formatting and the diff stays minimal.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any

import sexpdata

from kcaa.utils import pcb_library_utils

log = logging.getLogger(__name__)

# Characters KiCad accepts in fp-lib-table nicknames.  Slashes, backslashes,
# colons and whitespace are excluded because they would break the table and
# file paths.
_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.+-]")


def sanitize_lib_nickname(nickname: str) -> str:
    """Return *nickname* with characters unsafe for fp-lib-table entries removed.

    :param nickname: Proposed library nickname.
    :returns: Sanitized nickname (may be empty when nothing usable remains).
    """
    return _SAFE_CHARS_RE.sub("_", nickname).strip("_")


def get_user_fp_lib_table_path() -> str:
    """Return the global user fp-lib-table path (may not exist yet).

    Uses the highest-priority KiCad config directory, mirroring the order in
    ``kpcaa.utils.pcb_library_utils._default_kicad_config_dirs()``.
    """
    dirs = pcb_library_utils._default_kicad_config_dirs()
    return os.path.join(dirs[0], "fp-lib-table") if dirs else ""


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

    Creates the table with a ``(fp_lib_table (version 7) ...)`` header when it
    does not exist.  Backs up an existing file to ``.bak`` before writing.
    If *nickname* is already registered the table is left untouched.

    :param table_path: Absolute path to the fp-lib-table file.
    :param nickname: Library nickname to register (sanitized internally).
    :param uri: Library URI, e.g. ``${KICAD10_3RD_PARTY}/footprints/X.pretty``.
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
            raise ValueError(f"Malformed fp-lib-table (no closing paren): {table_path}")
        if _last_line_is_closing_paren(original):
            # Pretty layout: insert a new entry line before the final ")".
            idx = original.rfind("\n)")
            new_text = original[: idx + 1] + "\t" + entry + "\n" + original[idx + 1 :]
        else:
            # Compact single-line layout: re-serialize with sexpdata.
            log.warning(
                "fp-lib-table %s is single-line; re-serializing to append entry",
                table_path,
            )
            data = sexpdata.loads(original)
            data.append(sexpdata.loads(entry))
            new_text = sexpdata.dumps(data, pretty_print=True, indent_as="\t") + "\n"
        backup_path = table_path + ".bak"
        shutil.copy2(table_path, backup_path)
    else:
        os.makedirs(os.path.dirname(table_path) or ".", exist_ok=True)
        new_text = "(fp_lib_table\n\t(version 7)\n\t" + entry + "\n)\n"

    tmp_path = table_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, table_path)
    return {
        "registered": True,
        "table_path": table_path,
        "backup_path": backup_path,
    }
