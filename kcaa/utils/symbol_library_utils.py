"""Write/read helpers for KiCad .kicad_sym symbol library files.

Provides the symbol-side counterpart of the footprint library write
helpers (``pcb_footprint_utils.write_footprint_mod``):

* ``create_empty_library_file`` — create a ``(kicad_symbol_lib ...)`` file.
* ``append_symbol_to_library`` — add one ``(symbol ...)`` definition to an
  existing library file, never overwriting a same-named symbol.
* ``list_library_symbols`` — live-scan the top-level ``(symbol ...)`` names
  of a ``.kicad_sym`` file (no index database involved).
* ``is_safe_symbol_name`` — path/table-safe symbol-name guard.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
from typing import Any

import sexpdata

from kcaa.utils.config import ServerConfig

log = logging.getLogger(__name__)

# Characters KiCad accepts in sym-lib-table nicknames (same safe set as
# fp-lib-table nicknames: no slashes, colons, or whitespace).
_SAFE_NICKNAME_RE = re.compile(r"[^A-Za-z0-9_.+-]")


def sanitize_lib_nickname(nickname: str) -> str:
    """Return *nickname* with characters unsafe for sym-lib-table entries removed."""
    return _SAFE_NICKNAME_RE.sub("_", nickname).strip("_")


def _default_kicad_config_dirs() -> list[str]:
    """Return KiCad config dirs, most-specific first (mirror of the
    pcb_library_utils helper)."""
    from kcaa.utils import pcb_library_utils

    return pcb_library_utils._default_kicad_config_dirs()


def find_sym_lib_tables(project_path: str | None = None) -> list[str]:
    """Return sym-lib-table paths that exist on this system, most-specific first.

    :param project_path: Optional path to a project file or project directory;
        its directory is searched for a project-local sym-lib-table first.
    """
    tables: list[str] = []
    if project_path:
        proj_dir = project_path if os.path.isdir(project_path) else os.path.dirname(project_path)
        proj_table = os.path.join(proj_dir, "sym-lib-table")
        if os.path.isfile(proj_table):
            tables.append(proj_table)

    for config_dir in _default_kicad_config_dirs():
        candidate = os.path.join(config_dir, "sym-lib-table")
        if os.path.isfile(candidate) and candidate not in tables:
            tables.append(candidate)

    return tables


def _build_env_map(project_dir: str | None = None) -> dict[str, str]:
    """Build a dict of KiCad ``${VAR}`` substitutions for sym-lib-table URIs."""
    env: dict[str, str] = ServerConfig().get_env_vars()
    for key, val in os.environ.items():
        if key.startswith("KICAD"):
            env[key] = val
    if project_dir:
        env["KIPRJMOD"] = project_dir
    return env


def _resolve_uri(uri: str, env: dict[str, str]) -> str:
    """Expand ``${VAR}`` placeholders in a library URI (leaves unresolvable
    placeholders in place, mirroring the fp-lib-table behaviour)."""

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        value = env.get(var)
        return value if value is not None else match.group(0)

    return os.path.normpath(re.sub(r"\$\{([^}]+)\}", _replace, uri))


def _parse_sym_lib_table_raw(table_path: str, env: dict[str, str]) -> list[dict[str, str]]:
    """Parse a single sym-lib-table file without recursion.

    :returns: List of dicts with keys ``nickname``, ``type``, ``uri``
        (resolved), ``raw_uri`` (unexpanded), ``description``,
        ``table_path``.
    """
    libraries: list[dict[str, str]] = []
    try:
        with open(table_path, encoding="utf-8") as fh:
            raw = fh.read()
        data = sexpdata.loads(raw)
    except Exception:
        return libraries

    def _sym(v: Any) -> str:
        return str(v) if isinstance(v, sexpdata.Symbol) else str(v)

    for item in data:
        if not (isinstance(item, list) and len(item) > 0 and _sym(item[0]) == "lib"):
            continue
        entry: dict[str, str] = {
            "nickname": "",
            "type": "",
            "uri": "",
            "raw_uri": "",
            "description": "",
        }
        for sub in item[1:]:
            if not (isinstance(sub, list) and len(sub) >= 2):
                continue
            key = _sym(sub[0])
            val = sub[1] if isinstance(sub[1], str) else _sym(sub[1])
            if key == "name":
                entry["nickname"] = val
            elif key == "type":
                entry["type"] = val
            elif key == "uri":
                entry["raw_uri"] = val
                entry["uri"] = _resolve_uri(val, env)
            elif key == "descr":
                entry["description"] = val
        if entry["nickname"]:
            entry["table_path"] = os.path.realpath(table_path)
            libraries.append(entry)

    return libraries


def _parse_sym_lib_table_recursive(
    table_path: str,
    env: dict[str, str],
    visited: set,
) -> list[dict[str, str]]:
    """Recursive helper handling ``type=\"Table\"`` indirection."""
    real_path = os.path.realpath(table_path)
    if real_path in visited:
        log.warning("Circular sym-lib-table indirection, skipping: %s", table_path)
        return []
    visited.add(real_path)

    result: list[dict[str, str]] = []
    for entry in _parse_sym_lib_table_raw(table_path, env):
        if entry["type"].lower() == "table":
            sub_table = entry["uri"]
            if os.path.isfile(sub_table):
                result.extend(_parse_sym_lib_table_recursive(sub_table, env, visited))
            else:
                log.warning(
                    "sym-lib-table indirection target not found: %s (entry '%s' in %s)",
                    sub_table,
                    entry.get("nickname", ""),
                    table_path,
                )
        else:
            result.append(entry)
    return result


def parse_sym_lib_table(table_path: str, project_dir: str | None = None) -> list[dict[str, str]]:
    """Parse a sym-lib-table file and return its library entries.

    :param table_path: Absolute path to a sym-lib-table file.
    :param project_dir: Optional project directory used to resolve
        ``${KIPRJMOD}`` in library URIs.
    :returns: List of dicts with keys ``nickname``, ``type``, ``uri``
        (resolved), ``raw_uri``, ``description``.
    """
    env = _build_env_map(project_dir)
    return _parse_sym_lib_table_recursive(table_path, env, visited=set())


def build_effective_symbol_library_list(
    project_path: str | None = None,
) -> list[dict[str, str]]:
    """Return a deduplicated, precedence-ordered list of symbol libraries.

    Reads all sym-lib-table files (project first, then global), resolves
    ``type=\"Table\"`` indirections, and deduplicates by nickname — the first
    occurrence wins (project libraries override global ones).

    :param project_path: Optional path to a project file or directory; its
        directory is checked for a project-local sym-lib-table and used to
        resolve ``${KIPRJMOD}`` in library URIs.
    :returns: List of dicts: ``nickname``, ``type``, ``uri`` (resolved),
        ``raw_uri`` (unexpanded), ``description``.
    """
    project_dir = None
    if project_path:
        project_dir = project_path if os.path.isdir(project_path) else os.path.dirname(project_path)
    table_paths = find_sym_lib_tables(project_path)
    seen_nicknames: set = set()
    result: list[dict[str, str]] = []
    for tpath in table_paths:
        for lib in parse_sym_lib_table(tpath, project_dir=project_dir):
            if lib["nickname"] not in seen_nicknames:
                seen_nicknames.add(lib["nickname"])
                result.append(lib)
    return result


def resolve_symbol_library(
    library: str,
    project_path: str | None = None,
) -> dict[str, str]:
    """Resolve a registered library nickname to its ``.kicad_sym`` file.

    :param library: Library nickname as registered in sym-lib-table.
    :param project_path: Optional project path for project-local table lookup
        (``${KIPRJMOD}`` resolution).
    :returns: dict with ``path`` (absolute .kicad_sym file), ``uri``,
        ``table_path``, ``nickname``.
    :raises ValueError: When the nickname is not in sym-lib-table, resolves
        to a missing file, or is not a file.
    """
    libs = build_effective_symbol_library_list(project_path)
    entry = next((lib for lib in libs if lib["nickname"] == library), None)
    if entry is None:
        raise ValueError(
            f"Library '{library}' not found in sym-lib-table. "
            "Create it first with create_symbol_library."
        )
    file_path = entry.get("uri", "")
    if not file_path or not os.path.isfile(file_path):
        raise ValueError(f"Library '{library}' resolves to a missing file: {file_path}")
    return {
        "path": file_path,
        "uri": file_path,
        "table_path": entry.get("table_path", ""),
        "nickname": library,
    }


# KiCad 10 .kicad_sym header tokens (see test_device.kicad_sym fixture).
_LIB_HEADER = "(kicad_symbol_lib\n  (version 20220914)\n  (generator kicad_symbol_editor)"

# Symbol names embedded in .kicad_sym files must be filesystem- and
# sym-lib-table-safe: no separators, no whitespace, no leading digit.
_SYMBOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def is_safe_symbol_name(name: str) -> bool:
    """Return True when *name* is a safe library symbol name.

    Mirrors the constraint enforced by ``create_symbol`` (no leading digit,
    alphanumerics/underscore only) so names taken from an untrusted
    schematic can never escape the library path.
    """
    return bool(_SYMBOL_NAME_RE.match(name))


def create_empty_library_file(file_path: str) -> str:
    """Create a brand-new empty ``.kicad_sym`` library file.

    Refuses to overwrite an existing file.  The generator name matches what
    KiCad's own symbol editor writes.

    :param file_path: Absolute path of the library to create.
    :returns: The path written.
    :raises FileExistsError: When *file_path* already exists.
    """
    if os.path.exists(file_path):
        raise FileExistsError(f"Library file already exists, refusing to overwrite: {file_path}")
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(_LIB_HEADER + "\n)\n")
    return file_path


def list_library_symbols(file_path: str) -> list[str]:
    """Return the top-level ``(symbol ...)`` names of a .kicad_sym file.

    Only direct children of the ``kicad_symbol_lib`` root are considered, so
    sub-symbols (``NAME_0_1`` units) are not returned.  A missing or
    unparsable file yields ``[]`` (callers decide how to report).

    :param file_path: Absolute path to the .kicad_sym file.
    """
    try:
        with open(file_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    try:
        data = sexpdata.loads(text)
    except Exception:
        log.warning("Failed to parse symbol library %s", file_path)
        return []
    if not isinstance(data, list) or len(data) < 1:
        return []
    # Root children: everything after the kicad_symbol_lib keyword and its
    # (version ...) header.  A top-level symbol node is [Symbol('symbol'),
    # 'NAME', ...]; sub-symbols live nested inside those nodes, never here.
    names: list[str] = []
    for child in data[1:]:
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol | str)
            and str(child[0]) == "symbol"
            and isinstance(child[1], sexpdata.Symbol | str)
        ):
            # Symbol subclasses str but Symbol('X') == 'X' is False, so the
            # node MUST be normalized to a plain str — otherwise a bare-atom
            # name slips past the delete non-empty guard / remove lookup even
            # though it was counted.  str() on a quoted str is identity; on a
            # Symbol it yields the atom text.
            names.append(str(child[1]))
    return names


def _symbol_exists(file_path: str, symbol_name: str) -> bool:
    """Return True when a top-level symbol named *symbol_name* is present."""
    return symbol_name in list_library_symbols(file_path)


def append_symbol_to_library(file_path: str, lib_sym_raw: list, symbol_name: str) -> str:
    """Append one ``(symbol ...)`` definition to a .kicad_sym library file.

    The top-level name inside *lib_sym_raw* is normalised to *symbol_name*
    (the caller passes the plain library form, e.g. ``"MYOP"``, never a
    qualified ``lib_id`` like ``"custom:MYOP"``).  An existing symbol with
    the same name is **never** overwritten — ``SymbolNameExistsError`` is
    raised instead.

    :param file_path: Absolute path to the library file (must exist).
    :param lib_sym_raw: Raw sexpdata list for one ``(symbol ...)`` node.
    :param symbol_name: Plain (unqualified) symbol name to store.
    :returns: Absolute path written.
    :raises FileNotFoundError: When *file_path* does not exist.
    :raises SymbolNameExistsError: When *symbol_name* already exists.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Library file not found: {file_path}")
    if not is_safe_symbol_name(symbol_name):
        raise ValueError(f"Unsafe symbol name {symbol_name!r} (refusing to write)")
    if _symbol_exists(file_path, symbol_name):
        raise SymbolNameExistsError(
            f"Symbol '{symbol_name}' already exists in library {os.path.basename(file_path)} "
            "(refusing to overwrite)"
        )

    with open(file_path, encoding="utf-8") as fh:
        original = fh.read()
    stripped = original.rstrip()
    if not stripped.endswith(")"):
        raise ValueError(f"Malformed symbol library file (no closing paren): {file_path}")

    raw_copy = copy.deepcopy(lib_sym_raw)
    raw_copy[1] = symbol_name
    node_text = sexpdata.dumps(raw_copy)

    shutil.copy2(file_path, file_path + ".bak")
    # Insert the new (symbol ...) node before the final closing paren of the
    # kicad_symbol_lib root, keeping the pretty layout.
    idx = stripped.rfind("\n)")
    if idx == -1:
        idx = len(stripped) - 1
    new_text = stripped[:idx] + "\n  " + node_text + stripped[idx:] + "\n"
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, file_path)
    return os.path.abspath(file_path)


class SymbolNameExistsError(ValueError):
    """Raised when appending a symbol whose name already exists in a library."""


class SymbolNotFoundError(ValueError):
    """Raised when removing a symbol that is not a top-level symbol in a library."""


def _balanced_close(text: str, start: int) -> int:
    """Return the index just past the closing paren of the s-expr starting at *start*.

    Quote-aware: parentheses inside quoted strings do not count.  Returns -1
    when the text is unbalanced.
    """
    depth = 0
    in_str = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _root_children_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) text spans of the root node's direct children.

    The outermost root s-expr is expanded; every nested node is consumed
    whole.  Comments (``;`` to end of line) are skipped so a ``(`` inside a
    comment is never mistaken for a node start.  Returns [] when the text
    has no root, or the layout cannot be scanned reliably.
    """
    i = 0
    n = len(text)
    root_open = -1
    while i < n:
        ch = text[i]
        if ch == ";":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "(":
            root_open = i
            break
        i += 1
    if root_open == -1:
        return []
    root_close = _balanced_close(text, root_open)
    if root_close == -1:
        return []
    spans: list[tuple[int, int]] = []
    i = root_open + 1
    while i < root_close:
        ch = text[i]
        if ch == ";":
            nl = text.find("\n", i)
            i = root_close if nl == -1 or nl >= root_close else nl + 1
            continue
        if ch == "(":
            close = _balanced_close(text, i)
            if close == -1 or close > root_close:
                return []
            spans.append((i, close))
            i = close
            continue
        i += 1
    return spans


def remove_symbol_from_library_file(file_path: str, symbol_name: str) -> str:
    """Remove one top-level ``(symbol ...)`` node from a .kicad_sym library.

    Only the named top-level definition is removed; sub-symbols (units) of
    other symbols and the library header are untouched.  A backup (``.bak``)
    is written before saving.  Refuses to remove a symbol that does not
    exist — ``SymbolNotFoundError`` is raised instead.

    The node is located by parsing the file (same sexpdata walk as
    :func:`list_library_symbols`) and matching its top-level name, so quoted
    (``(symbol "A" ...)``) and bare-atom (``(symbol A ...)``) names work
    regardless of indentation.  Only the matched node's text is spliced out
    — the rest of the file stays byte-identical.

    :param file_path: Absolute path to the library file (must exist).
    :param symbol_name: Plain (unqualified) symbol name to remove.
    :returns: Absolute path written.
    :raises FileNotFoundError: When *file_path* does not exist.
    :raises ValueError: When *symbol_name* is not a safe library symbol name.
    :raises SymbolNotFoundError: When *symbol_name* is not a top-level
        symbol of the library.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Library file not found: {file_path}")
    if not is_safe_symbol_name(symbol_name):
        raise ValueError(f"Unsafe symbol name {symbol_name!r} (refusing to edit)")

    with open(file_path, encoding="utf-8") as fh:
        text = fh.read()

    try:
        data = sexpdata.loads(text)
    except Exception:
        data = None
    if not (isinstance(data, list) and len(data) >= 1):
        raise SymbolNotFoundError(
            f"Symbol '{symbol_name}' not found in library {os.path.basename(file_path)}"
        )

    match_index: int | None = None
    for index, child in enumerate(data[1:]):
        if (
            isinstance(child, list)
            and len(child) >= 2
            and isinstance(child[0], sexpdata.Symbol | str)
            and str(child[0]) == "symbol"
            and isinstance(child[1], sexpdata.Symbol | str)
            and str(child[1]) == symbol_name
        ):
            match_index = index
            break
    if match_index is None:
        raise SymbolNotFoundError(
            f"Symbol '{symbol_name}' not found in library {os.path.basename(file_path)}"
        )

    spans = _root_children_spans(text)
    if len(spans) != len(data) - 1:
        raise SymbolNotFoundError(
            f"Symbol '{symbol_name}' not found in library {os.path.basename(file_path)}"
        )
    node_start, node_close = spans[match_index]

    # Splice the node's whole line out: from the start of its line through
    # the close paren plus one trailing newline, so no blank line is left.
    # Everything outside that region stays byte-identical.
    line_start = text.rfind("\n", 0, node_start) + 1
    remove_end = node_close
    j = remove_end
    while j < len(text) and text[j] in " \t":
        j += 1
    if j < len(text) and text[j] == "\n":
        remove_end = j + 1

    new_text = text[:line_start] + text[remove_end:]
    shutil.copy2(file_path, file_path + ".bak")
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp_path, file_path)
    return os.path.abspath(file_path)
