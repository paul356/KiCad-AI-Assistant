"""Unit tests for kcaa.utils.symbol_library_utils library-file writes."""

import pytest

from kcaa.utils.symbol_library_utils import (
    SymbolNotFoundError,
    list_library_symbols,
    remove_symbol_from_library_file,
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
            "  (symbol \"A\"\n"
            "    (in_bom yes)\n"
            "  )\n"
            "  (symbol \"B\"\n"
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "A")
        assert lib.read_text() == (
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol \"B\"\n"
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
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
            "  (symbol \"QUOTED\"\n"
            "    (in_bom yes)\n"
            "  )\n"
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "BARE")
        text = lib.read_text()
        assert 'QUOTED' in text
        assert "BARE" not in text
        assert list_library_symbols(str(lib)) == ["QUOTED"]

    def test_removes_with_nonstandard_indentation(self, tmp_path):
        """Arbitrary top-level indentation (1-space, 4-space, zero) is
        matched by parse; everything outside the node stays exactly as-is."""
        lib = tmp_path / "odd.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "(symbol \"A\" (in_bom yes))\n"
            "    (symbol \"DEEP\"\n"
            "      (in_bom yes)\n"
            "    )\n"
            "  (symbol \"B\" (in_bom yes))\n"
            ")\n"
        )
        remove_symbol_from_library_file(str(lib), "DEEP")
        assert lib.read_text() == (
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "(symbol \"A\" (in_bom yes))\n"
            "  (symbol \"B\" (in_bom yes))\n"
            ")\n"
        )

    def test_symbol_not_found_still_raises(self, tmp_path):
        """An absent name raises SymbolNotFoundError without touching the
        file or writing any backup."""
        lib = tmp_path / "lib.kicad_sym"
        lib.write_text(
            "(kicad_symbol_lib\n"
            "  (version 20220914)\n"
            "  (symbol \"A\" (in_bom yes))\n"
            ")\n"
        )
        before = lib.read_text()
        with pytest.raises(SymbolNotFoundError):
            remove_symbol_from_library_file(str(lib), "Ghost")
        assert not (tmp_path / "lib.kicad_sym.bak").exists()
        assert lib.read_text() == before
