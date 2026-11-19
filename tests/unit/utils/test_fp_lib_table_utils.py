"""Unit tests for fp-lib-table read-modify-write line-ending preservation."""

import pytest

from kcaa.utils.fp_lib_table_utils import (
    register_library_in_table,
    unregister_library_in_table,
)

_A_ENTRY = '\t(lib (name "A") (type "KiCad") (uri "/a") (options "") (descr ""))'
_B_ENTRY = '\t(lib (name "B") (type "KiCad") (uri "/b") (options "") (descr ""))'
_NEW_ENTRY = '(lib (name "NEW") (type "KiCad") (uri "/new") (options "") (descr ""))'


def _table(*entries: str, newline: str = "\n") -> bytes:
    lines = ["(fp_lib_table", "\t(version 7)", *entries, ")"]
    return (newline.join(lines) + newline).encode("utf-8")


def _assert_crlf_only(data: bytes) -> None:
    assert data.count(b"\n") > 0
    assert data.replace(b"\r\n", b"").count(b"\n") == 0
    assert data.replace(b"\r\n", b"").count(b"\r") == 0


class TestRegisterLibraryInTableLineEndings:
    def test_crlf_table_stays_crlf(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        original = _table(_A_ENTRY, newline="\r\n")
        table.write_bytes(original)

        result = register_library_in_table(str(table), "NEW", "/new")

        data = table.read_bytes()
        assert result["registered"] is True
        assert result["backup_path"] == str(table) + ".bak"
        _assert_crlf_only(data)
        assert data == _table(_A_ENTRY, "\t" + _NEW_ENTRY, newline="\r\n")
        original_lines = original.splitlines(keepends=True)
        output_lines = data.splitlines(keepends=True)
        assert output_lines[:3] == original_lines[:3]
        assert output_lines[-1] == original_lines[-1]

    def test_lf_table_stays_lf(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        original = _table(_A_ENTRY)
        table.write_bytes(original)

        register_library_in_table(str(table), "NEW", "/new")

        data = table.read_bytes()
        assert b"\r" not in data
        assert data == _table(_A_ENTRY, "\t" + _NEW_ENTRY)
        assert original.splitlines()[2] in data.splitlines()

    def test_mixed_table_uses_dominant_terminator(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        table.write_bytes(
            b"(fp_lib_table\r\n"
            b"\t(version 7)\r\n"
            + _A_ENTRY.encode()
            + b"\r\n"
            + _B_ENTRY.encode()
            + b"\n"
            + b")\r\n"
        )

        register_library_in_table(str(table), "NEW", "/new")

        data = table.read_bytes()
        _assert_crlf_only(data)
        assert data == _table(_A_ENTRY, _B_ENTRY, "\t" + _NEW_ENTRY, newline="\r\n")

    def test_empty_table_raises_malformed_without_side_effects(self, tmp_path):
        header = tmp_path / "header-table"
        header.write_bytes(_table(newline="\r\n"))

        assert register_library_in_table(str(header), "NEW", "/new")["registered"] is True
        _assert_crlf_only(header.read_bytes())
        assert header.read_bytes() == _table("\t" + _NEW_ENTRY, newline="\r\n")
        assert (tmp_path / "header-table.bak").is_file()

        for name, content in (
            ("empty-table", b""),
            ("blank-table", b" \r\n\t\n"),
        ):
            table = tmp_path / name
            table.write_bytes(content)

            with pytest.raises(ValueError, match="Malformed fp-lib-table"):
                register_library_in_table(str(table), "NEW", "/new")

            assert table.read_bytes() == content
            assert not (tmp_path / (name + ".bak")).exists()


class TestUnregisterLibraryInTableLineEndings:
    def test_crlf_table_stays_crlf_without_blank_line(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        table.write_bytes(_table(_A_ENTRY, _B_ENTRY, newline="\r\n"))

        result = unregister_library_in_table(str(table), "A")

        data = table.read_bytes()
        assert result["unregistered"] is True
        _assert_crlf_only(data)
        assert data == _table(_B_ENTRY, newline="\r\n")
        assert b'name "A"' not in data

    def test_lf_table_stays_lf(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        table.write_bytes(_table(_A_ENTRY, _B_ENTRY))

        unregister_library_in_table(str(table), "A")

        data = table.read_bytes()
        assert b"\r" not in data
        assert data == _table(_B_ENTRY)

    def test_mixed_table_unifies_on_dominant_terminator(self, tmp_path):
        table = tmp_path / "fp-lib-table"
        table.write_bytes(
            b"(fp_lib_table\r\n"
            b"\t(version 7)\r\n"
            + _A_ENTRY.encode()
            + b"\r\n"
            + _B_ENTRY.encode()
            + b"\n"
            + b")\r\n"
        )

        unregister_library_in_table(str(table), "A")

        data = table.read_bytes()
        _assert_crlf_only(data)
        assert data == _table(_B_ENTRY, newline="\r\n")

    def test_empty_and_header_only_report_not_registered(self, tmp_path):
        header = tmp_path / "header-table"
        header_bytes = _table(newline="\r\n")
        header.write_bytes(header_bytes)
        empty = tmp_path / "empty-table"
        empty.write_bytes(b"")

        assert unregister_library_in_table(str(header), "Ghost")["unregistered"] is False
        assert unregister_library_in_table(str(empty), "Ghost")["unregistered"] is False
        assert header.read_bytes() == header_bytes
        assert empty.read_bytes() == b""


def test_compact_single_line_table_round_trips(tmp_path):
    table = tmp_path / "fp-lib-table"
    table.write_bytes(b'(fp_lib_table (version 7) (lib (name "A") (type "KiCad") (uri "/a")))')

    assert register_library_in_table(str(table), "NEW", "/new")["registered"] is True

    data = table.read_bytes()
    assert b'name "A"' in data
    assert b'name "NEW"' in data
    assert b"\r" not in data
