"""Tests for snippet_tools — save_selection_as_snippet / read_snippet."""

import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest
import skip

from kcaa.utils.skip_compat import safe_schematic


SCHEMATIC_PATH = str(
    Path(__file__).parent / "fixtures" / "tools_test.kicad_sch"
)


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.snippet_tools import register_snippet_tools

    mock = _MockMCP()
    register_snippet_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    """Fresh copy of the fixture schematic."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir()
    )
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    yield tmp.name
    for p in [tmp.name, tmp.name + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture()
def tmp_snip():
    """Fresh target path for a snippet file."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".kicad_snippet", delete=False, dir=tempfile.gettempdir()
    )
    tmp.close()
    os.unlink(tmp.name)
    yield tmp.name
    for p in [tmp.name, tmp.name + ".bak", tmp.name + ".tmp"]:
        if os.path.exists(p):
            os.unlink(p)


def _wires(sch_path: str) -> list[tuple[float, float, float, float]]:
    sch = skip.Schematic(sch_path)
    out = []
    for w in sch.wire:
        out.append(
            (
                float(w.start.value[0]),
                float(w.start.value[1]),
                float(w.end.value[0]),
                float(w.end.value[1]),
            )
        )
    return out


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    def test_non_kicad_sch_source_rejected(self, tools, tmp_snip):
        bad = tmp_sch = SCHEMATIC_PATH.replace(".kicad_sch", ".txt")
        try:
            result = asyncio_run(
                tools["save_selection_as_snippet"](
                    schematic_path=bad,
                    output_path=tmp_snip,
                    bbox_x=0,
                    bbox_y=0,
                    bbox_width=10,
                    bbox_height=10,
                    snippet_name="test",
                )
            )
            assert "error" in result
            assert ".kicad_sch" in result["error"]
        finally:
            if os.path.exists(bad):
                os.unlink(bad)

    def test_nonexistent_source_rejected(self, tools, tmp_snip):
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path="/nonexistent.kicad_sch",
                output_path=tmp_snip,
                bbox_x=0,
                bbox_y=0,
                bbox_width=10,
                bbox_height=10,
                snippet_name="test",
            )
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_non_positive_bbox_rejected(self, tools, tmp_sch, tmp_snip):
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=0,
                bbox_y=0,
                bbox_width=0,
                bbox_height=10,
                snippet_name="test",
            )
        )
        assert "error" in result
        assert "bbox_width" in result["error"] or "must be > 0" in result["error"]

    def test_nan_bbox_rejected(self, tools, tmp_sch, tmp_snip):
        for bad_value in (float("nan"), float("inf")):
            result = asyncio_run(
                tools["save_selection_as_snippet"](
                    schematic_path=tmp_sch,
                    output_path=tmp_snip,
                    bbox_x=bad_value,
                    bbox_y=0,
                    bbox_width=10,
                    bbox_height=10,
                    snippet_name="test",
                )
            )
            assert "error" in result


# ===========================================================================
# Empty selection
# ===========================================================================


class TestEmptySelection:
    def test_bbox_with_no_components_writes_minimal_snippet(
        self, tools, tmp_sch, tmp_snip
    ):
        """An empty bbox region still produces a valid (if minimal) snippet."""
        # Bbox in upper-left corner of the sheet, well clear of R2..R5.
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=10.0,
                bbox_y=10.0,
                bbox_width=20.0,
                bbox_height=20.0,
                snippet_name="empty",
            )
        )
        assert result.get("success") is True, result
        assert os.path.exists(tmp_snip)

        with open(tmp_snip) as f:
            content = f.read()
        assert content.startswith("(kicad_snippet")
        assert '(name "empty")' in content

    def test_empty_selection_writes_no_symbols(self, tools, tmp_sch, tmp_snip):
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=10.0,
                bbox_y=10.0,
                bbox_width=20.0,
                bbox_height=20.0,
                snippet_name="empty",
            )
        )
        assert result["counts"]["symbols"] == 0
        assert result["counts"]["wires"] == 0
        assert result["counts"]["labels"] == 0
        assert result["counts"]["junctions"] == 0
        assert result["counts"]["lib_symbols"] == 0


# ===========================================================================
# Bbox containment
# ===========================================================================


class TestBboxContainment:
    def test_symbol_inside_bbox_is_exported(self, tools, tmp_sch, tmp_snip):
        """R2 is at (100, 100) — a bbox covering (95..105, 95..105) should pick it up."""
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2_block",
            )
        )
        assert result.get("success") is True, result
        assert result["counts"]["symbols"] >= 1, result
        # lib_symbols entry for R_Small must be bundled.
        assert result["counts"]["lib_symbols"] >= 1

    def test_symbol_outside_bbox_is_not_exported(self, tools, tmp_sch, tmp_snip):
        """R5 is at (160, 100) — a bbox at (95..105) should NOT pick it up."""
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2_only",
            )
        )
        assert result.get("success") is True, result
        # The selected symbols should all be inside the bbox; no R5 in the output.
        with open(tmp_snip) as f:
            content = f.read()
        # R5 has reference "R5" — its Reference property should NOT appear in the snippet.
        assert '"R5"' not in content

    def test_bbox_normalises_coordinates_to_origin(
        self, tools, tmp_sch, tmp_snip
    ):
        """After saving, the snippet's local origin is (0, 0) — bbox top-left.

        We pick a bbox that starts at (95, 95) and save R2 (at 100, 100).  The
        snippet symbol should be at (5, 5) in local coords.
        """
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2_normalised",
            )
        )
        assert result.get("success") is True, result

        with open(tmp_snip) as f:
            content = f.read()

        # The R_Small symbol placement in the snippet is at the bbox-local
        # position of the original anchor (100, 100) minus the bbox top-left
        # (95, 95) = (5, 5).  Tolerance for skip's float formatting.
        assert re.search(r'\(at 5\.0+ 5\.0+', content), (
            f"Expected (at 5.0 5.0 ...) somewhere in snippet; got:\n{content[:500]}"
        )

    def test_extension_appended_if_missing(self, tools, tmp_sch):
        """output_path without .kicad_snippet should still produce a valid file."""
        base = tempfile.NamedTemporaryFile(
            delete=False, dir=tempfile.gettempdir(), suffix=".txt"
        )
        base.close()
        os.unlink(base.name)
        no_ext = base.name  # no extension

        try:
            result = asyncio_run(
                tools["save_selection_as_snippet"](
                    schematic_path=tmp_sch,
                    output_path=no_ext,
                    bbox_x=95.0,
                    bbox_y=95.0,
                    bbox_width=10.0,
                    bbox_height=10.0,
                    snippet_name="test",
                )
            )
            assert result.get("success") is True, result
            assert result["output_path"].endswith(".kicad_snippet")
            assert os.path.exists(result["output_path"])
        finally:
            for p in [no_ext, no_ext + ".kicad_snippet"]:
                if os.path.exists(p):
                    os.unlink(p)


# ===========================================================================
# Lib symbol bundling
# ===========================================================================


class TestLibSymbolBundling:
    def test_lib_id_is_symdir_form(self, tools, tmp_sch, tmp_snip):
        """Exported lib_id should be 'Device:R_Small' form (or symdir form)."""
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2",
            )
        )
        assert result.get("success") is True, result

        with open(tmp_snip) as f:
            content = f.read()

        # The lib_id inside the (symbol ...) entry should be symdir form.
        # The R_Small symbol in the fixture is referenced as 'Device:R_Small'
        # — normalising strips the 'Device:' prefix.
        sym_matches = re.findall(r'\(symbol \(lib_id "([^"]+)"\)', content)
        for lib_id in sym_matches:
            assert ":" not in lib_id, (
                f"lib_id {lib_id!r} should be symdir form, not resolved form"
            )

    def test_lib_symbol_block_is_carried(self, tools, tmp_sch, tmp_snip):
        """The bundled lib_symbols section contains the symbol definition."""
        result = asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2",
            )
        )
        assert result.get("success") is True, result

        with open(tmp_snip) as f:
            content = f.read()

        # The R_Small symbol definition must appear inside (lib_symbols ...).
        lib_section = re.search(
            r"\(lib_symbols(.*?)\n\s*\)",
            content,
            flags=re.DOTALL,
        )
        assert lib_section is not None, "lib_symbols section not found"
        assert "(symbol \"R_Small\"" in lib_section.group(1), (
            f"R_Small definition not bundled; lib_section:\n{lib_section.group(1)[:400]}"
        )


# ===========================================================================
# Atomic write and idempotence
# ===========================================================================


class TestAtomicWrite:
    def test_existing_snippet_backed_up(self, tools, tmp_sch, tmp_snip):
        """Re-saving over an existing snippet must keep a .bak of the old one."""
        # First save
        asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=10.0,
                bbox_y=10.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="first",
            )
        )
        first = Path(tmp_snip).read_text()
        # Second save over the same path
        asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=10.0,
                bbox_y=10.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="second",
            )
        )
        second = Path(tmp_snip).read_text()
        bak = Path(tmp_snip + ".bak")

        assert bak.exists(), "Expected .bak backup file"
        # The .bak should contain the FIRST snippet (preserved verbatim).
        assert '(name "first")' in bak.read_text()
        # The new file should have the SECOND name.
        assert '(name "second")' in second

    def test_no_tmp_file_left_behind_on_success(self, tools, tmp_sch, tmp_snip):
        """The atomic-write tmp file must not linger after success."""
        asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=10.0,
                bbox_y=10.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="test",
            )
        )
        assert not os.path.exists(tmp_snip + ".tmp")


# ===========================================================================
# Read
# ===========================================================================


class TestReadSnippet:
    def test_read_returns_counts(self, tools, tmp_sch, tmp_snip):
        asyncio_run(
            tools["save_selection_as_snippet"](
                schematic_path=tmp_sch,
                output_path=tmp_snip,
                bbox_x=95.0,
                bbox_y=95.0,
                bbox_width=10.0,
                bbox_height=10.0,
                snippet_name="r2",
            )
        )
        result = asyncio_run(tools["read_snippet"](snippet_path=tmp_snip))
        assert result.get("success") is True, result
        assert result["name"] == "r2"
        assert "uuid" in result and result["uuid"]
        assert result["counts"]["symbols"] >= 1
        assert result["counts"]["lib_symbols"] >= 1
        assert "raw_size_bytes" in result

    def test_read_rejects_non_snippet(self, tools, tmp_sch):
        result = asyncio_run(
            tools["read_snippet"](snippet_path=tmp_sch)
        )
        assert "error" in result
        assert ".kicad_snippet" in result["error"]


# ---------------------------------------------------------------------------
# Helper: asyncio.run shim — lets us call the async tools from sync tests
# without pulling in pytest-asyncio per-call.
# ---------------------------------------------------------------------------


import asyncio


def asyncio_run(coro):
    """Run an awaitable to completion and return its result."""
    return asyncio.run(coro)