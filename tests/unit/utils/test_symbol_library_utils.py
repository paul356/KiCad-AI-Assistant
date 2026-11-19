"""Unit tests for kcaa.utils.symbol_library_utils library-file writes."""

import pytest
import sexpdata

from kcaa.utils.atomic_io import detect_newline, normalize_newlines, read_text_utf8
from kcaa.utils.symbol_library_utils import (
    SymbolNameExistsError,
    SymbolNotFoundError,
    append_symbol_to_library,
    list_library_symbols,
    remove_symbol_from_library_file,
    set_symbol_pin,
)


class TestRemoveSymbolFromLibraryFile:
    def test_removes_quoted_name_standard_indent(self, tmp_path):
        """Quoted name with the pretty 2-space top-level indent (the layout
        the create/export tools write) still removes and preserves siblings
        byte-identically."""
        lib = tmp_path / "lib.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            '  (symbol "A"\n'
            "    (in_bom yes)\n"
            "  )\n"
            '  (symbol "B"\n'
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "A")
        assert lib.read_text() == (
            '(kicad_symbol_lib\n  (version 20220914)\n  (symbol "B"\n    (in_bom yes)\n  )\n)\n'
        )
        assert list_library_symbols(str(lib)) == ["B"]
        assert (tmp_path / "lib.kicad_sym.bak").is_file()

    def test_removes_bare_atom_name(self, tmp_path):
        """A bare-atom name (``(symbol BARE ...)``) is found by parse, not
        quote needle — the old text-hunt missed it."""
        lib = tmp_path / "bare.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol BARE\n"
            "    (in_bom yes)\n"
            "  )\n"
            '  (symbol "QUOTED"\n'
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "BARE")
        text = lib.read_text()
        assert "QUOTED" in text
        assert "BARE" not in text
        assert list_library_symbols(str(lib)) == ["QUOTED"]

    def test_removes_with_nonstandard_indentation(self, tmp_path):
        """Arbitrary top-level indentation (1-space, 4-space, zero) is
        matched by parse; everything outside the node stays exactly as-is."""
        lib = tmp_path / "odd.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            '(symbol "A" (in_bom yes))\n'
            '    (symbol "DEEP"\n'
            "      (in_bom yes)\n"
            "    )\n"
            '  (symbol "B" (in_bom yes))\n'
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "DEEP")
        assert lib.read_text() == (
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            '(symbol "A" (in_bom yes))\n'
            '  (symbol "B" (in_bom yes))\n'
            ")\n"
        )

    def test_symbol_not_found_still_raises(self, tmp_path):
        """An absent name raises SymbolNotFoundError without touching the
        file or writing any backup."""
        lib = tmp_path / "lib.kicad_sym"
        lib.write_text('(kicad_symbol_lib\n  (version 20220914)\n  (symbol "A" (in_bom yes))\n)\n')
        before = lib.read_text()
        with pytest.raises(SymbolNotFoundError):
            remove_symbol_from_library_file(str(lib), "Ghost")
        assert not (tmp_path / "lib.kicad_sym.bak").exists()
        assert lib.read_text() == before


# ---------------------------------------------------------------------------
# Line-ending preservation (issue #329)
# ---------------------------------------------------------------------------

_HEADER_ONLY_LIB = "(kicad_symbol_lib\n  (version 20220914)\n  (generator kicad_symbol_editor)\n)\n"

_TWO_SYMBOL_LIB = (
    "(kicad_symbol_lib\n"
    "  (version 20220914)\n"
    "  (generator kicad_symbol_editor)\n"
    '  (symbol "A"\n'
    "    (in_bom yes)\n"
    "  )\n"
    '  (symbol "B"\n'
    "    (in_bom yes)\n"
    "  )\n"
    ")\n"
)

_PIN_LIB = (
    "(kicad_symbol_lib\n"
    "  (version 20220914)\n"
    "  (generator kicad_symbol_editor)\n"
    '  (symbol "U"\n'
    "    (in_bom yes)\n"
    '    (symbol "U_1_1"\n'
    "      (pin input line (at -2.54 0 0) (length 2.54)\n"
    '        (name "OLD" (effects (font (size 1.27 1.27))))\n'
    '        (number "1" (effects (font (size 1.27 1.27))))\n'
    "      )\n"
    "    )\n"
    "  )\n"
    ")\n"
)


def _as_bytes(text: str, newline: str) -> bytes:
    return normalize_newlines(text, newline).encode("utf-8")


def _sym_raw(name: str = "NEW") -> list:
    return sexpdata.loads(f'(symbol "{name}" (in_bom yes))')


def _assert_crlf_only(data: bytes) -> None:
    assert data.count(b"\n") > 0
    assert data.replace(b"\r\n", b"").count(b"\n") == 0
    assert data.replace(b"\r\n", b"").count(b"\r") == 0


def _assert_lf_only(data: bytes) -> None:
    assert b"\r" not in data


def test_detect_newline_rule() -> None:
    """CRLF wins only on a strict majority; ties/empty resolve to LF."""
    assert detect_newline("") == "\n"
    assert detect_newline("single line") == "\n"
    assert detect_newline("a\nb\n") == "\n"
    assert detect_newline("a\r\nb\r\n") == "\r\n"
    assert detect_newline("a\r\nb\n") == "\n"  # tie
    assert detect_newline("a\r\nb\r\nc\n") == "\r\n"
    assert detect_newline("a\r\nb\nc\n") == "\n"


def test_read_text_utf8_default_still_normalizes(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"a\r\nb\r\n")
    assert read_text_utf8(str(path)) == "a\nb\n"
    assert read_text_utf8(str(path), preserve_newlines=True) == "a\r\nb\r\n"


class TestAppendSymbolToLibraryLineEndings:
    def test_crlf_input_stays_crlf(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        original = _as_bytes(_HEADER_ONLY_LIB, "\r\n")
        lib.write_bytes(original)

        append_symbol_to_library(str(lib), _sym_raw(), "NEW")

        data = lib.read_bytes()
        _assert_crlf_only(data)
        assert list_library_symbols(str(lib)) == ["NEW"]
        assert data.endswith(b")\r\n")
        assert b"\r\n\r\n" not in data
        # untouched lines stayed byte-identical
        original_lines = original.splitlines(keepends=True)
        output_lines = data.splitlines(keepends=True)
        assert output_lines[:3] == original_lines[:3]
        assert output_lines[-1] == original_lines[-1]
        assert b'  (symbol "NEW"' in data
        with pytest.raises(SymbolNameExistsError):
            append_symbol_to_library(str(lib), _sym_raw(), "NEW")

    def test_lf_input_stays_lf(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        original = _as_bytes(_HEADER_ONLY_LIB, "\n")
        lib.write_bytes(original)

        append_symbol_to_library(str(lib), _sym_raw(), "NEW")

        data = lib.read_bytes()
        _assert_lf_only(data)
        assert data.endswith(b")\n")
        assert b"\n\n" not in data
        assert list_library_symbols(str(lib)) == ["NEW"]

    def test_mixed_input_uses_dominant_terminator(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        lib.write_bytes(
            b"(kicad_symbol_lib\r\n  (version 20220914)\r\n  (generator kicad_symbol_editor)\n)\r\n"
        )

        append_symbol_to_library(str(lib), _sym_raw(), "NEW")

        data = lib.read_bytes()
        _assert_crlf_only(data)
        assert b'  (symbol "NEW"' in data
        assert list_library_symbols(str(lib)) == ["NEW"]

    def test_single_line_library_round_trips(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        lib.write_bytes(b"(kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor))")

        append_symbol_to_library(str(lib), _sym_raw(), "NEW")

        data = lib.read_bytes()
        _assert_lf_only(data)
        assert data.endswith(b")\n")
        assert list_library_symbols(str(lib)) == ["NEW"]

    def test_empty_library_raises_malformed_error(self, tmp_path):
        for name, content in (
            ("empty.kicad_sym", b""),
            ("blank.kicad_sym", b" \r\n\t\n"),
        ):
            lib = tmp_path / name
            lib.write_bytes(content)

            with pytest.raises(ValueError, match="Malformed symbol library file"):
                append_symbol_to_library(str(lib), _sym_raw(), "NEW")

            assert lib.read_bytes() == content
            assert not (tmp_path / (name + ".bak")).exists()


class TestRemoveSymbolFromLibraryFileLineEndings:
    def test_crlf_input_stays_crlf_without_blank_line(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        lib.write_bytes(_as_bytes(_TWO_SYMBOL_LIB, "\r\n"))

        remove_symbol_from_library_file(str(lib), "A")

        data = lib.read_bytes()
        _assert_crlf_only(data)
        assert data == _as_bytes(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (generator kicad_symbol_editor)\n"
            '  (symbol "B"\n'
            "    (in_bom yes)\n"
            "  )\n"
            ")\n",
            "\r\n",
        )
        assert list_library_symbols(str(lib)) == ["B"]

    def test_lf_input_stays_lf(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        lib.write_bytes(_as_bytes(_TWO_SYMBOL_LIB, "\n"))

        remove_symbol_from_library_file(str(lib), "A")

        data = lib.read_bytes()
        _assert_lf_only(data)
        assert b'"A"' not in data
        assert b'"B"' in data
        assert list_library_symbols(str(lib)) == ["B"]

    def test_mixed_input_unifies_on_dominant_terminator(self, tmp_path):
        lib = tmp_path / "lib.kicad_sym"
        lib.write_bytes(
            b"(kicad_symbol_lib\r\n"
            b"  (version 20220914)\r\n"
            b"  (generator kicad_symbol_editor)\n"
            b'  (symbol "A"\r\n'
            b"    (in_bom yes)\r\n"
            b"  )\r\n"
            b'  (symbol "B"\r\n'
            b"    (in_bom yes)\r\n"
            b"  )\r\n"
            b")\r\n"
        )

        remove_symbol_from_library_file(str(lib), "A")

        data = lib.read_bytes()
        _assert_crlf_only(data)
        assert list_library_symbols(str(lib)) == ["B"]

    def test_header_only_and_empty_raise_domain_error(self, tmp_path):
        header = tmp_path / "header.kicad_sym"
        header_bytes = _as_bytes(_HEADER_ONLY_LIB, "\r\n")
        header.write_bytes(header_bytes)
        empty = tmp_path / "empty.kicad_sym"
        empty.write_bytes(b"")

        with pytest.raises(SymbolNotFoundError):
            remove_symbol_from_library_file(str(header), "Ghost")
        with pytest.raises(SymbolNotFoundError):
            remove_symbol_from_library_file(str(empty), "Ghost")

        assert header.read_bytes() == header_bytes
        assert empty.read_bytes() == b""


def test_set_symbol_pin_keeps_crlf(tmp_path) -> None:
    lib = tmp_path / "lib.kicad_sym"
    lib.write_bytes(_as_bytes(_PIN_LIB, "\r\n"))

    report = set_symbol_pin(str(lib), "U", [{"pin_number": "1", "new_name": "NEW"}])

    data = lib.read_bytes()
    assert report["changed"] is True
    _assert_crlf_only(data)
    assert b'(name "NEW"' in data
    assert b'(name "OLD"' not in data
