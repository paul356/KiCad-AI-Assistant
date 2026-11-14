"""Helpers for reading ``sexpdata`` parse trees."""

from typing import Any

__all__ = ["sym_str"]


def sym_str(value: Any) -> str:
    """Return *value* as a plain ``str``.

    ``sexpdata.Symbol`` (the default representation of unquoted atoms) and
    ``sexpdata.String`` (produced only when a caller parses with
    ``string_to=sexpdata.String``) both subclass ``str`` but their ``__eq__``
    also requires the two sides to be the same class, so such an atom never
    compares equal to the plain ``str`` holding the same characters. Every atom
    read out of a parse tree must therefore go through ``str()`` before it is
    compared, used as a dict key, or returned to callers.

    Only equality fails: both classes hash exactly like ``str``, so
    hashes still match and a dict or set lookup still misses, because equality
    is checked after the hash.

    Write-back paths must keep the original atom and must not use this helper.
    """
    return str(value)
