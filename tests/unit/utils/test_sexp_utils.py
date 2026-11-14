"""Tests for the shared ``sexpdata`` parse-tree helpers."""

import sexpdata

from kcaa.utils.sexp_utils import sym_str


def test_sym_str_returns_plain_string_for_symbol():
    """A Symbol must become a str that compares equal to the same characters."""
    assert sym_str(sexpdata.Symbol("edge")) == "edge"


def test_sym_str_result_is_not_a_symbol():
    """The result must not carry Symbol equality semantics."""
    result = sym_str(sexpdata.Symbol("edge"))

    assert type(result) is str


def test_sym_str_converts_non_symbol_values():
    """Non-Symbol input still converts through str()."""
    assert sym_str(3) == "3"
