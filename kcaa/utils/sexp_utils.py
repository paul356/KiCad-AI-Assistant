"""Helpers for reading ``sexpdata`` parse trees."""

from typing import Any

__all__ = ["sym_str"]


def sym_str(value: Any) -> str:
    """Return *value* as a plain ``str``.

    ``sexpdata.Symbol`` (unquoted atoms) and ``sexpdata.String`` (quoted atoms)
    both subclass ``str`` but their ``__eq__`` also requires the two sides to be
    the same class, so an atom taken out of a parse tree never compares equal to
    the plain ``str`` holding the same characters. Every value read out of a
    parse tree must therefore go through ``str()`` before it is compared, used
    as a dict key, or returned to callers.

    Write-back paths must keep the original atom and must not use this helper.
    """
    return str(value)
