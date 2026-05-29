"""Streaming symbol extractor for KiCad .kicad_sym library files.

Fast path: character-scan to the recorded file_index, guarded by
mtime+size validation against the DB record.

Fallback path: full skip.sexp.sourcefile.SourceFile parse with linear
search by name, used when the file has been modified since indexing.
"""

import copy
import logging
import os

import sexpdata
import skip.collection

from kcaa.utils.skip_compat import safe_source_file

log = logging.getLogger(__name__)


def extract_lib_symbol_raw(
    file_path: str,
    file_index: int,
    symbol_name: str,
    db_mtime: float,
    db_size: int,
) -> list:
    """
    Extract the raw S-expression list for a single symbol from a .kicad_sym file.

    Uses a fast streaming scan when the file's mtime and size match the
    database record.  Falls back to a full parse when they differ.

    Parameters
    ----------
    file_path   : absolute path to the .kicad_sym library file
    file_index  : 0-based index among all (symbol ...) entries in the file
    symbol_name : expected symbol name (e.g. "R"), used for verification
    db_mtime    : mtime stored in the DB when the file was indexed
    db_size     : file size stored in the DB when the file was indexed

    Returns
    -------
    A deep-copied raw sexpdata list ready for injection / ParsedValue wrapping.

    Raises
    ------
    FileNotFoundError  if file_path cannot be stat'd
    ValueError         if the symbol cannot be found or the name doesn't match
    """
    try:
        stat = os.stat(file_path)
    except OSError as exc:
        raise FileNotFoundError(f"Cannot stat library file {file_path!r}: {exc}") from exc

    if stat.st_mtime == db_mtime and stat.st_size == db_size:
        log.debug("Fast-path extract: file_index=%d from %r", file_index, file_path)
        raw = _fast_path(file_path, file_index)
    else:
        log.debug(
            "Fallback extract (mtime/size mismatch) for %r in %r",
            symbol_name,
            file_path,
        )
        raw = _fallback_path(file_path, symbol_name)

    if len(raw) < 2 or not isinstance(raw[1], str):
        raise ValueError(f"Extracted symbol has no string name at index 1 (got {raw[:3]!r})")
    extracted_name = raw[1]
    if extracted_name != symbol_name:
        raise ValueError(
            f"Symbol name mismatch: expected {symbol_name!r} "
            f"but extracted {extracted_name!r} from {file_path!r}"
        )

    return raw


def _fast_path(file_path: str, file_index: int) -> list:
    """
    Streaming character scan: extract the file_index-th (symbol ...) block
    from a .kicad_sym file and return it as a parsed sexpdata list.

    Correctly handles:
    - quoted strings (skipped entirely, including escaped quotes \\")
    - non-symbol depth-2 entries ((version ...), (generator ...), etc.)

    Depth convention:
      0  outside everything
      1  inside (kicad_symbol_lib ...)
      2  inside a direct child of (kicad_symbol_lib ...)
    """
    with open(file_path, encoding="utf-8") as fh:
        text = fh.read()

    depth = 0
    in_string = False
    escape_next = False
    sym_count = 0  # (symbol ...) blocks seen so far (0-based)
    capture_start = None

    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # ---- inside a quoted string ----------------------------------------
        if escape_next:
            escape_next = False
            i += 1
            continue

        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        # ---- structural parens outside strings -----------------------------
        if ch == "(":
            if depth == 1:
                # Entering a direct child of (kicad_symbol_lib ...).
                # Peek ahead to read its entity-type token.
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                tok_start = j
                while j < n and text[j] not in ' \t\r\n()"':
                    j += 1
                token = text[tok_start:j]

                if token == "symbol":
                    if sym_count == file_index:
                        capture_start = i
                    sym_count += 1

            depth += 1
            i += 1
            continue

        if ch == ")":
            # depth == 2 before decrement means we're closing a direct child
            # of (kicad_symbol_lib ...).
            if depth == 2 and capture_start is not None:
                block_str = text[capture_start : i + 1]
                return sexpdata.loads(block_str)
            depth -= 1
            i += 1
            continue

        i += 1

    raise ValueError(
        f"Symbol at file_index={file_index} not found in {file_path!r} "
        f"(found {sym_count} symbol block(s))"
    )


def _fallback_path(file_path: str, symbol_name: str) -> list:
    """
    Full-parse fallback: parse the entire file with skip's SourceFile,
    then find the symbol by name and return a deep copy of its raw list.
    """
    library_file = safe_source_file(file_path)

    if not hasattr(library_file, "symbol"):
        raise ValueError(f"No symbols found in {file_path!r}")

    elements = library_file.symbol
    if isinstance(elements, (list, skip.collection.ElementCollection)):
        sym_iter = elements
    else:
        # Library contains exactly one symbol — SourceFile returns it directly.
        sym_iter = [elements]

    for sym_el in sym_iter:
        try:
            if sym_el.value == symbol_name:
                return copy.deepcopy(sym_el.raw)
        except Exception:
            continue

    raise ValueError(f"Symbol {symbol_name!r} not found in {file_path!r}")
