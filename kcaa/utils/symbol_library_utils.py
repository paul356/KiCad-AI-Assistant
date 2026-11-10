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
import stat
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


def _outer_root_span(text: str) -> tuple[int, int] | None:
    """Return the span of the outermost balanced s-expression, or None.

    ``None`` means the text has no root node or its parentheses do not
    balance, which callers report as a syntax error rather than as a
    missing symbol.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == ";":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "(":
            close = _balanced_close(text, i)
            return (i, close) if close != -1 else None
        i += 1
    return None


def _root_children_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) text spans of the root node's direct children.

    The outermost root s-expr is expanded; every nested node is consumed
    whole.  Comments (``;`` to end of line) are skipped so a ``(`` inside a
    comment is never mistaken for a node start.  Returns [] when the text
    has no root, or the layout cannot be scanned reliably.
    """
    root = _outer_root_span(text)
    if root is None:
        return []
    root_open, root_close = root
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


# ---------------------------------------------------------------------------
# Pin number / pin name editing
# ---------------------------------------------------------------------------

_MAX_PIN_NUMBER_LENGTH = 32
_MAX_PINS_PER_REQUEST = 256
_MAX_AVAILABLE_PIN_NUMBERS = 10
_STRING_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}


def _decode_sexp_string(raw: str) -> str:
    """Decode the escape sequences KiCad uses inside a quoted string."""
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            out.append(_STRING_ESCAPES.get(raw[index + 1], raw[index + 1]))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _skip_whitespace(text: str, index: int, end: int) -> int:
    """Advance *index* past whitespace, never past *end*."""
    while index < end and text[index] in " \t\r\n":
        index += 1
    return index


def _read_token(text: str, index: int, end: int) -> tuple[str | None, int, int, int]:
    """Read one s-expression token (quoted string or bare atom).

    :returns: ``(value, token_start, token_end, next_index)``.  *value* is
        None when no token starts at *index*; the spans cover the token
        including its quotes so callers can splice a replacement in place.
    """
    index = _skip_whitespace(text, index, end)
    if index >= end:
        return None, index, index, index
    if text[index] == '"':
        start = index
        index += 1
        content_start = index
        while index < end:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                return _decode_sexp_string(text[content_start:index]), start, index + 1, index + 1
            index += 1
        return None, start, end, end
    start = index
    while index < end and text[index] not in ' \t\r\n()"':
        index += 1
    if index == start:
        # A paren: consume it so callers cannot loop forever on it.
        return None, start, start, index + 1
    return text[start:index], start, index, index


def _node_head(text: str, start: int, end: int) -> tuple[str | None, str | None]:
    """Return ``(keyword, second_token)`` for the node whose ``(`` is at *start*."""
    keyword, _, _, after_keyword = _read_token(text, start + 1, end)
    name, _, _, _ = _read_token(text, after_keyword, end)
    return keyword, name


def _child_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return direct child node spans of the node whose ``(`` is at *start*."""
    spans: list[tuple[int, int]] = []
    index = start + 1
    limit = end - 1
    while index < limit:
        char = text[index]
        if char == ";":
            newline = text.find("\n", index)
            index = limit if newline == -1 or newline >= limit else newline + 1
        elif char == '"':
            _, _, _, index = _read_token(text, index, limit)
        elif char == "(":
            close = _balanced_close(text, index)
            if close == -1 or close > end:
                return []
            spans.append((index, close))
            index = close
        else:
            index += 1
    return spans


def _find_top_symbol_span(text: str, symbol_name: str) -> tuple[int, int] | None:
    """Return the span of the top-level ``(symbol "symbol_name" ...)`` node."""
    for start, end in _root_children_spans(text):
        keyword, name = _node_head(text, start, end)
        if keyword == "symbol" and name == symbol_name:
            return start, end
    return None


def _child_token(
    text: str, start: int, end: int, keyword: str
) -> tuple[str | None, tuple[int, int] | None]:
    """Return the first token of a direct *keyword* child node."""
    for child_start, child_end in _child_spans(text, start, end):
        child_keyword, _ = _node_head(text, child_start, child_end)
        if child_keyword != keyword:
            continue
        _, _, _, after_keyword = _read_token(text, child_start + 1, child_end)
        value, token_start, token_end, _ = _read_token(text, after_keyword, child_end)
        if value is None:
            return None, None
        return value, (token_start, token_end)
    return None, None


def _split_sub_symbol_name(name: str, symbol_name: str) -> tuple[int, int] | None:
    """Parse ``<symbol>_<unit>_<style>`` into ``(unit, style)``.

    Symbol names may themselves contain underscores, so the trailing two
    all-digit segments are the only ones that define the unit/style.  Returns
    None for unrelated sub-symbols and for non-numeric suffixes.
    """
    prefix = symbol_name + "_"
    if not name.startswith(prefix):
        return None
    parts = name[len(prefix) :].split("_")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _has_control_character(value: str) -> bool:
    """Return True when *value* contains a C0 control character or DEL."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_pin_entry(entry: Any) -> dict[str, Any]:
    """Validate one ``pins`` entry and return its normalized fields."""
    if not isinstance(entry, dict):
        raise ValueError(
            "each pins entry must be an object with pin_number and at least one of "
            "new_number/new_name"
        )
    unknown = set(entry) - {"pin_number", "new_number", "new_name"}
    if unknown:
        raise ValueError(f"unknown pins entry field: {sorted(unknown)[0]!r}")
    pin_number = entry.get("pin_number")
    new_number = entry.get("new_number")
    new_name = entry.get("new_name")
    if not isinstance(pin_number, str) or not pin_number:
        raise ValueError("pin_number must be a non-empty string")
    if new_number is None and new_name is None:
        raise ValueError("both new_number and new_name are None; nothing to change")
    if new_number is not None:
        invalid_number = (
            not isinstance(new_number, str)
            or not new_number
            or len(new_number) > _MAX_PIN_NUMBER_LENGTH
            or any(char.isspace() for char in new_number)
            or any(char in new_number for char in '"()\\')
            or _has_control_character(new_number)
        )
        if invalid_number:
            raise ValueError(
                "new_number must be a non-empty token without whitespace, quotes, "
                "parentheses, backslashes or control characters (max 32 characters)"
            )
    if new_name is not None and (
        not isinstance(new_name, str)
        or any(char in new_name for char in '"()\\')
        or _has_control_character(new_name)
    ):
        raise ValueError(
            "new_name must not contain quotes, parentheses, backslashes or control characters"
        )
    return {"pin_number": pin_number, "new_number": new_number, "new_name": new_name}


def _validate_pin_edit_requests(pins: Any) -> list[dict[str, Any]]:
    """Validate the shared batch contract and reject duplicate selectors."""
    if not isinstance(pins, list) or not pins:
        raise ValueError("pins must be a non-empty list")
    if len(pins) > _MAX_PINS_PER_REQUEST:
        raise ValueError(f"pins must contain at most {_MAX_PINS_PER_REQUEST} entries")
    requests = [_validate_pin_entry(entry) for entry in pins]
    seen: set[str] = set()
    for request in requests:
        selector = request["pin_number"]
        if selector in seen:
            raise ValueError(f"duplicate pin_number '{selector}' in pins list")
        seen.add(selector)
    return requests


def _symbol_unit_style_universe(
    pins: list[dict[str, Any]],
) -> tuple[set[int], set[int]]:
    """Return the concrete units and body styles of a symbol.

    Unit ``0`` and style ``0`` are wildcards, so only non-zero values define
    the universe those wildcards expand to.
    """
    units = {pin["unit"] for pin in pins if pin["unit"] != 0}
    styles = {pin["style"] for pin in pins if pin["style"] != 0}
    return units or {0}, styles or {0}


def effective_unit_styles(
    pin: dict[str, Any],
    all_units: set[int],
    all_styles: set[int],
) -> tuple[set[int], set[int]]:
    """Expand a pin's unit/style into the sets it effectively belongs to.

    Unit ``0`` belongs to every unit of the symbol; style ``0`` belongs to
    every body style.  Styles ``1`` and ``2`` are independent alternatives
    (normal / De Morgan), not the same representation.
    """
    units = set(all_units) if pin["unit"] == 0 else {pin["unit"]}
    styles = set(all_styles) if pin["style"] == 0 else {pin["style"]}
    return units, styles


def _pin_pair_key(first: dict[str, Any], second: dict[str, Any]) -> tuple[int, int]:
    """Return an order-independent id pair for two pin dicts."""
    first_id, second_id = id(first), id(second)
    return (first_id, second_id) if first_id < second_id else (second_id, first_id)


def _pre_existing_duplicate_pairs(pins: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """Return the id pairs of pins that already share a number in one scope.

    KiCad allows duplicate pin numbers (``duplicate_pin_numbers_are_jumpers``),
    so duplicates that exist *before* a batch are legal and must not be
    reported as collisions.  Scope is the intersection of the effective unit
    and body-style sets, matching :func:`find_pin_collisions`.
    """
    all_units, all_styles = _symbol_unit_style_universe(pins)
    pairs: set[tuple[int, int]] = set()
    for index, pin in enumerate(pins):
        pin_units, pin_styles = effective_unit_styles(pin, all_units, all_styles)
        for other in pins[index + 1 :]:
            if other["number"] != pin["number"]:
                continue
            other_units, other_styles = effective_unit_styles(other, all_units, all_styles)
            if pin_units & other_units and pin_styles & other_styles:
                pairs.add(_pin_pair_key(pin, other))
    return pairs


def find_pin_collisions(
    pins: list[dict[str, Any]],
    final_numbers: dict[int, str],
    pre_existing: set[tuple[int, int]] | None = None,
) -> None:
    """Reject number collisions introduced by a batch, ignoring pre-existing ones.

    Two pins collide when their effective unit sets intersect and their
    effective style sets intersect.  Pairs that already shared a number
    before the batch (KiCad stacked pins / jumpers) stay legal, so renaming
    a stacked pin keeps working; only a *new* duplicate is rejected.  Only
    pins whose number actually changes are checked, so no-op batches never
    block an edit.

    Raises:
        ValueError: When an edited pin lands on a number used by another pin
            in an intersecting effective unit/style.
    """
    already_duplicated = pre_existing or set()
    all_units, all_styles = _symbol_unit_style_universe(pins)
    changed = [pin for pin in pins if final_numbers[id(pin)] != pin["number"]]
    for pin in changed:
        pin_units, pin_styles = effective_unit_styles(pin, all_units, all_styles)
        for other in pins:
            if other is pin:
                continue
            if final_numbers[id(other)] != final_numbers[id(pin)]:
                continue
            if _pin_pair_key(pin, other) in already_duplicated:
                continue
            other_units, other_styles = effective_unit_styles(other, all_units, all_styles)
            if not (pin_units & other_units and pin_styles & other_styles):
                continue
            number = final_numbers[id(pin)]
            if final_numbers[id(other)] != other["number"]:
                raise ValueError(
                    f"pin number '{number}' is requested for more than one pin in "
                    "this batch; give each pin a distinct number"
                )
            raise ValueError(f"pin number '{number}' is already used by another pin in the symbol")


def _plan_pin_edit_batch(
    pins: list[dict[str, Any]],
    symbol_name: str,
    requests: list[dict[str, Any]],
    unit: int | None,
) -> dict[str, Any]:
    """Resolve and validate a whole batch against its *final* state.

    Each entry of *pins* carries at least ``unit``, ``style``, ``number`` and
    ``name``.  All selectors are resolved against the pre-edit snapshot, then
    every requested value is applied simultaneously before collisions are
    checked, so chains and cycles that form a valid permutation are accepted.
    Only pin *numbers* are constrained to stay unique where they already were;
    names may repeat or be empty (KiCad allows both).
    """
    selected = [pin for pin in pins if unit is None or pin["unit"] == unit]
    resolved: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for request in requests:
        matches = [pin for pin in selected if pin["number"] == request["pin_number"]]
        if not matches:
            available = list(dict.fromkeys(pin["number"] for pin in selected if pin["number"]))[
                :_MAX_AVAILABLE_PIN_NUMBERS
            ]
            detail = ", ".join(available) if available else "none"
            raise ValueError(
                f"pin '{request['pin_number']}' not found in symbol '{symbol_name}' "
                f"(available pin numbers: {detail})"
            )
        resolved.append((request, matches))

    final_numbers = {id(pin): pin["number"] for pin in pins}
    final_names = {id(pin): pin["name"] for pin in pins}
    for request, matches in resolved:
        for pin in matches:
            if request["new_number"] is not None:
                final_numbers[id(pin)] = request["new_number"]
            if request["new_name"] is not None:
                final_names[id(pin)] = request["new_name"]

    all_units, all_styles = _symbol_unit_style_universe(pins)
    duplicates = _pre_existing_duplicate_pairs(pins)
    warnings: list[str] = []
    for request, matches in resolved:
        if request["new_number"] is None:
            continue
        # A selector can match several pins (stacked pins, shared numbers).
        # Those pins are edited together by the caller's request, so they are
        # not "another pin" to warn about, and one warning per selector is
        # enough.
        match_ids = {id(pin) for pin in matches}
        warned = False
        for pin in matches:
            if warned:
                break
            final_number = final_numbers[id(pin)]
            if final_number == pin["number"]:
                continue
            pin_units, pin_styles = effective_unit_styles(pin, all_units, all_styles)
            for other in pins:
                if other is pin or id(other) in match_ids:
                    continue
                if final_numbers[id(other)] != final_number:
                    continue
                if _pin_pair_key(pin, other) in duplicates:
                    continue
                other_units, other_styles = effective_unit_styles(other, all_units, all_styles)
                if pin_units & other_units and pin_styles & other_styles:
                    # A real collision; find_pin_collisions rejects the batch.
                    continue
                warnings.append(
                    f"pin number '{final_number}' is also used by another pin of this symbol"
                )
                warned = True
                break

    changes: list[dict[str, Any]] = []
    changed_pins: list[dict[str, Any]] = []
    pins_changed = 0
    for request, matches in resolved:
        for pin in matches:
            final_number = final_numbers[id(pin)]
            final_name = final_names[id(pin)]
            changed = final_number != pin["number"] or final_name != pin["name"]
            log.debug(
                "pin edit in '%s': unit=%s style=%s selector=%s old=(%s,%s) new=(%s,%s)",
                symbol_name,
                pin["unit"],
                pin["style"],
                request["pin_number"],
                pin["number"],
                pin["name"],
                final_number,
                final_name,
            )
            changes.append(
                {
                    "pin_number": request["pin_number"],
                    "old_number": pin["number"],
                    "old_name": pin["name"],
                    "new_number": final_number,
                    "new_name": final_name,
                    "changed": changed,
                }
            )
            changed_pins.append(pin)
            if changed:
                pins_changed += 1

    return {
        "changed_pins": changed_pins,
        "changes": changes,
        "final_numbers": final_numbers,
        "pre_existing_pairs": duplicates,
        "pins_changed": pins_changed,
        "changed": pins_changed > 0,
        "occurrences": len(changed_pins),
        "warnings": warnings,
    }


def set_symbol_pin(
    file_path: str,
    symbol_name: str,
    pins: list[dict[str, Any]],
    unit: int | None = None,
) -> dict:
    """Rename pin numbers and/or pin names inside one library symbol.

    *pins* is a non-empty batch (max 256 entries); each entry carries
    ``pin_number`` plus at least one of ``new_number`` / ``new_name``.  The
    whole batch is validated against its final state before anything is
    written: chains and cycles that form a valid permutation are accepted,
    and only a *newly introduced* duplicate number is rejected (duplicate
    numbers that already existed, e.g. KiCad stacked pins, stay legal).  Pin
    names are not required to be unique and may be empty.

    Only the quoted literals of the targeted ``(number ...)`` / ``(name ...)``
    nodes are replaced: every other byte of the ``.kicad_sym`` file — header,
    other symbols, formatting, line endings — stays identical.  Exactly one
    ``.bak`` is written and exactly one atomic replace is performed; on any
    validation error nothing is written.  The file's permission bits are
    preserved, and a read-only file is refused.

    Pins live in sub-symbols named ``<symbol>_<unit>_<style>``.  ``unit=None``
    edits every matching occurrence in all units and both body styles;
    ``unit=N`` restricts the edit to that unit (``0`` = common unit), still
    covering both styles.

    :param file_path: Absolute path to the ``.kicad_sym`` library file.
    :param symbol_name: Plain (unqualified) top-level symbol name.
    :param pins: Batch of pin edits.
    :param unit: Optional unit to restrict the edit to.
    :returns: Report dict with ``pins_requested``, ``pins_changed``,
        ``occurrences``, ``changes``, ``changed``, ``backup_path`` and
        ``warnings``.
    :raises FileNotFoundError: When *file_path* does not exist.
    :raises SymbolNotFoundError: When *symbol_name* is not a top-level symbol.
    :raises ValueError: On invalid arguments, a missing pin, or a collision.
    """
    requests = _validate_pin_edit_requests(pins)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Library file not found: {file_path}")

    with open(file_path, encoding="utf-8", newline="") as fh:
        text = fh.read()

    if _outer_root_span(text) is None:
        raise ValueError(
            f"Library {os.path.basename(file_path)} is not valid s-expression data "
            "(no root node or unbalanced parentheses); refusing to edit it"
        )

    span = _find_top_symbol_span(text, symbol_name)
    if span is None:
        raise SymbolNotFoundError(
            f"Symbol '{symbol_name}' not found in library {os.path.basename(file_path)}"
        )
    symbol_start, symbol_end = span

    extends_base: str | None = None
    sub_symbols: list[tuple[int, int, int, int]] = []
    for child_start, child_end in _child_spans(text, symbol_start, symbol_end):
        keyword, name = _node_head(text, child_start, child_end)
        if keyword == "extends" and name is not None:
            extends_base = name
        elif keyword == "symbol" and name is not None:
            parsed = _split_sub_symbol_name(name, symbol_name)
            if parsed is not None:
                sub_symbols.append((parsed[0], parsed[1], child_start, child_end))

    pin_nodes: list[dict[str, Any]] = []
    for pin_unit, pin_style, sub_start, sub_end in sub_symbols:
        for pin_start, pin_end in _child_spans(text, sub_start, sub_end):
            keyword, _ = _node_head(text, pin_start, pin_end)
            if keyword != "pin":
                continue
            number, number_span = _child_token(text, pin_start, pin_end, "number")
            name, name_span = _child_token(text, pin_start, pin_end, "name")
            pin_nodes.append(
                {
                    "unit": pin_unit,
                    "style": pin_style,
                    "number": number or "",
                    "name": name or "",
                    "number_span": number_span,
                    "name_span": name_span,
                }
            )

    if extends_base is not None and not pin_nodes:
        raise ValueError(
            f"symbol '{symbol_name}' extends '{extends_base}' and defines no pins; "
            f"update the base symbol '{extends_base}' instead"
        )

    plan = _plan_pin_edit_batch(pin_nodes, symbol_name, requests, unit)
    find_pin_collisions(pin_nodes, plan["final_numbers"], plan["pre_existing_pairs"])

    replacements: list[tuple[tuple[int, int], str]] = []
    for pin, change in zip(plan["changed_pins"], plan["changes"], strict=True):
        if change["new_number"] != pin["number"]:
            if pin["number_span"] is None:
                raise ValueError(f"pin '{change['pin_number']}' has no (number ...) node to edit")
            replacements.append((pin["number_span"], f'"{change["new_number"]}"'))
        if change["new_name"] != pin["name"]:
            if pin["name_span"] is None:
                raise ValueError(f"pin '{change['pin_number']}' has no (name ...) node to edit")
            replacements.append((pin["name_span"], f'"{change["new_name"]}"'))

    backup_path: str | None = None
    if plan["changed"]:
        if not os.access(file_path, os.W_OK):
            raise PermissionError(
                f"Library file is not writable: {file_path} "
                "(fix the file permissions and retry; nothing was changed)"
            )
        mode = stat.S_IMODE(os.stat(file_path).st_mode)
        new_text = text
        for (replace_start, replace_end), replacement in sorted(
            replacements, key=lambda item: item[0][0], reverse=True
        ):
            new_text = new_text[:replace_start] + replacement + new_text[replace_end:]
        file_path = os.path.abspath(file_path)
        backup_path = file_path + ".bak"
        shutil.copy2(file_path, backup_path)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        # Keep the library's own mode (e.g. 0640); the temp file is created
        # with the process default otherwise.
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)

    return {
        "symbol_name": symbol_name,
        "file_path": os.path.abspath(file_path),
        "unit": unit,
        "pins_requested": len(requests),
        "pins_changed": plan["pins_changed"],
        "occurrences": plan["occurrences"],
        "changes": plan["changes"],
        "changed": plan["changed"],
        "backup_path": backup_path,
        "warnings": plan["warnings"],
    }
