"""Helpers for reading ``sexpdata`` parse trees."""

from typing import Any

__all__ = ["sym_str"]


def sym_str(value: Any) -> str:
    """Return *value* as a plain ``str``.

    ``sexpdata.Symbol`` subclasses ``str`` but overrides ``__eq__`` so that a
    ``Symbol`` never compares equal to the ``str`` holding the same characters.
    Any value taken out of a parse tree and then compared, hashed or returned to
    callers therefore has to go through ``str()`` first.

    Write-back paths must keep the original atom and must not use this helper.
    """
    return str(value)
