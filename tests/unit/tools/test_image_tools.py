"""Tests for kcaa.tools.image_tools — render_image tool.

Covers:
* Validation (required args per kind, kicad-cli presence)
* kicad-cli argument construction for every ``kind``
* SVG-only flow (no cairosvg conversion)
* PNG conversion flow (cairosvg invoked after kicad-cli)
* Region cropping math (mm bbox → pixel crop box)
* File output: SVG written by kicad-cli, PNG derived next to it
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _MockMCP:
    """Minimal MCP stand-in capturing ``@mcp.tool()`` decorated callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    from kcaa.tools.image_tools import register_image_tools

    mock = _MockMCP()
    register_image_tools(mock)
    return mock.tools


@pytest.fixture()
def tmp_output_dir():
    """Isolated directory for rendered files; cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="kcaa_imgtest_") as d:
        yield d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Minimal SVG that cairosvg can parse; 100×100 user units.
_MINIMAL_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
    b'viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
)


def _fake_proc() -> subprocess.CompletedProcess:
    """Synthesise the CompletedProcess that secure_subprocess would return."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="",
    )


def _fake_kicad_cli(svg_bytes: bytes = _MINIMAL_SVG):
    """Return an AsyncMock that mimics run_kicad_command_async.

    Writes ``svg_bytes`` to the ``--output`` path that kicad-cli is asked
    to produce, and returns a fake CompletedProcess with returncode 0.
    """

    async def _runner(command_args, output_files=None, timeout=None, **_):
        # kicad-cli is invoked with ``--output <path> <input>``; locate the
        # value that follows ``--output``.
        idx = command_args.index("--output")
        out_path = command_args[idx + 1]
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(svg_bytes)
        return _fake_proc()

    return AsyncMock(side_effect=_runner)


def _schematic_with_paper(paper: str = "A4") -> str:
    """Write a one-line .kicad_sch stub containing a ``(paper "X")`` entry."""
    fd, path = tempfile.mkstemp(suffix=".kicad_sch")
    with os.fdopen(fd, "w") as f:
        f.write(f'(kicad_sch (version 20240101) (paper "{paper}"))')
    return path


def _pcb_with_edge_cuts(coords: list[tuple[float, float]]) -> str:
    """Write a .kicad_pcb stub containing a (gr_line ...) on Edge.Cuts."""
    fd, path = tempfile.mkstemp(suffix=".kicad_pcb")
    segs = "".join(
        f'(gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts"))'
        for (x1, y1), (x2, y2) in zip(coords[::2], coords[1::2] or coords[::2])
    )
    with os.fdopen(fd, "w") as f:
        f.write(f'(kicad_pcb (version 20240101) {segs})')
    return path


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(kind="schematic"),
            dict(kind="pcb"),
        ],
    )
    def test_required_source_paths(self, tools, kwargs):
        with pytest.raises(ValueError):
            import asyncio

            asyncio.run(tools["render_image"](**kwargs))

    def test_region_requires_target(self, tools):
        with pytest.raises(ValueError, match="region_target"):
            import asyncio

            asyncio.run(tools["render_image"](kind="region", bbox_x=0))

    def test_region_requires_bbox(self, tools, tmp_output_dir):
        with pytest.raises(ValueError, match="bbox_x"):
            import asyncio

            asyncio.run(
                tools["render_image"](
                    kind="region",
                    region_target="schematic",
                    schematic_path=_schematic_with_paper(),
                )
            )

    def test_symbol_requires_lib_id(self, tools):
        with pytest.raises(ValueError, match="lib_id"):
            import asyncio

            asyncio.run(tools["render_image"](kind="symbol"))

    def test_footprint_requires_footprint_id(self, tools):
        with pytest.raises(ValueError, match="footprint_id"):
            import asyncio

            asyncio.run(tools["render_image"](kind="footprint"))


# ---------------------------------------------------------------------------
# kicad-cli argument construction
# ---------------------------------------------------------------------------


class TestCliArgs:
    @pytest.mark.asyncio
    async def test_schematic_args(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()
        sch = _schematic_with_paper("A4")
        try:
            with patch(
                "kcaa.tools.image_tools.run_kicad_command_async", runner
            ):
                await tools["render_image"](
                    kind="schematic",
                    schematic_path=sch,
                    output_path=os.path.join(tmp_output_dir, "sch.png"),
                )
        finally:
            os.unlink(sch)
        cmd = runner.call_args.kwargs["command_args"]
        assert cmd[:3] == ["sch", "export", "svg"]
        assert "--output" in cmd
        assert sch in cmd

    @pytest.mark.asyncio
    async def test_pcb_args_include_layers(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()
        pcb = _pcb_with_edge_cuts([(0, 0), (10, 0), (10, 10), (0, 10)])
        try:
            with patch(
                "kcaa.tools.image_tools.run_kicad_command_async", runner
            ):
                await tools["render_image"](
                    kind="pcb",
                    pcb_path=pcb,
                    output_path=os.path.join(tmp_output_dir, "pcb.png"),
                )
        finally:
            os.unlink(pcb)
        cmd = runner.call_args.kwargs["command_args"]
        assert cmd[:3] == ["pcb", "export", "svg"]
        assert "--layers" in cmd
        layers_value = cmd[cmd.index("--layers") + 1]
        assert "Edge.Cuts" in layers_value

    @pytest.mark.asyncio
    async def test_symbol_uses_lib_id(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()
        with patch("kcaa.tools.image_tools.run_kicad_command_async", runner):
            await tools["render_image"](
                kind="symbol",
                lib_id="Device:R_Small",
                output_path=os.path.join(tmp_output_dir, "sym.png"),
            )
        cmd = runner.call_args.kwargs["command_args"]
        assert cmd[:3] == ["sym", "export", "svg"]
        assert cmd[cmd.index("--symbol") + 1] == "Device:R_Small"

    @pytest.mark.asyncio
    async def test_footprint_uses_footprint_id(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()
        with patch("kcaa.tools.image_tools.run_kicad_command_async", runner):
            await tools["render_image"](
                kind="footprint",
                footprint_id="Resistor_SMD:R_0805_2012Metric",
                output_path=os.path.join(tmp_output_dir, "fp.png"),
            )
        cmd = runner.call_args.kwargs["command_args"]
        assert cmd[:3] == ["fp", "export", "svg"]
        assert (
            cmd[cmd.index("--footprint") + 1] == "Resistor_SMD:R_0805_2012Metric"
        )

    @pytest.mark.asyncio
    async def test_kicad_cli_failure_raises(self, tools, tmp_output_dir):
        runner = AsyncMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="oops"
            )
        )
        sch = _schematic_with_paper()
        try:
            with patch(
                "kcaa.tools.image_tools.run_kicad_command_async", runner
            ):
                with pytest.raises(RuntimeError, match="kicad-cli failed"):
                    await tools["render_image"](
                        kind="schematic",
                        schematic_path=sch,
                        output_path=os.path.join(tmp_output_dir, "sch.png"),
                    )
        finally:
            os.unlink(sch)


# ---------------------------------------------------------------------------
# SVG vs PNG delivery paths
# ---------------------------------------------------------------------------


# Minimal valid 1×1 PNG (transparent) — written by the mocked cairosvg
# so _load_image can find the file and Pillow can decode it.
import base64 as _base64
_PNG_1X1 = _base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    b"2mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class TestDelivery:
    @pytest.mark.asyncio
    async def test_svg_only_does_not_convert(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()
        with patch(
            "kcaa.tools.image_tools.run_kicad_command_async", runner
        ), patch("kcaa.tools.image_tools.cairosvg.svg2png") as convert:
            out_path = os.path.join(tmp_output_dir, "out.svg")
            result = await tools["render_image"](
                kind="symbol",
                lib_id="Device:R_Small",
                output_format="svg",
                output_path=out_path,
            )
            convert.assert_not_called()
            assert os.path.exists(out_path)
            # Image content carries SVG bytes; format string is "svg".
            assert result._format == "svg"
            assert result.data.startswith(b"<?xml")

    @pytest.mark.asyncio
    async def test_png_default_converts_svg(self, tools, tmp_output_dir):
        runner = _fake_kicad_cli()

        def _fake_svg_to_png(**kwargs):
            """Mimic cairosvg by writing a valid 1×1 PNG to write_to."""
            with open(kwargs["write_to"], "wb") as f:
                f.write(_PNG_1X1)

        with patch(
            "kcaa.tools.image_tools.run_kicad_command_async", runner
        ), patch(
            "kcaa.tools.image_tools.cairosvg.svg2png",
            side_effect=_fake_svg_to_png,
        ) as convert:
            out_path = os.path.join(tmp_output_dir, "out.png")
            result = await tools["render_image"](
                kind="symbol",
                lib_id="Device:R_Small",
                output_path=out_path,
            )
            convert.assert_called_once()
            kwargs = convert.call_args.kwargs
            assert kwargs["output_width"] == 1600  # default width
            assert result._format == "png"
            # PNG magic bytes
            assert result.data.startswith(b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------------------
# Region cropping math
# ---------------------------------------------------------------------------


class TestCropMath:
    def test_full_extent_crop_covers_image(self):
        from kcaa.tools.image_tools import _crop_bbox_to_pixels

        # bbox matches the extent exactly → crop = (0,0,W,H)
        crop = _crop_bbox_to_pixels(
            bbox_mm=(0.0, 0.0, 100.0, 50.0),
            extent_mm=(0.0, 0.0, 100.0, 50.0),
            image_size=(1000, 500),
        )
        assert crop == (0, 0, 1000, 500)

    def test_half_extent_crop_halves_pixels(self):
        from kcaa.tools.image_tools import _crop_bbox_to_pixels

        crop = _crop_bbox_to_pixels(
            bbox_mm=(0.0, 0.0, 50.0, 25.0),
            extent_mm=(0.0, 0.0, 100.0, 50.0),
            image_size=(1000, 500),
        )
        assert crop == (0, 0, 500, 250)

    def test_offset_crop(self):
        from kcaa.tools.image_tools import _crop_bbox_to_pixels

        # bbox top-left at (25,10) with size (50,20) inside a (0,0,100,50) extent
        # → pixel box: x: 250→750, y: 100→300
        crop = _crop_bbox_to_pixels(
            bbox_mm=(25.0, 10.0, 50.0, 20.0),
            extent_mm=(0.0, 0.0, 100.0, 50.0),
            image_size=(1000, 500),
        )
        assert crop == (250, 100, 750, 300)

    def test_crop_clamped_to_image(self):
        from kcaa.tools.image_tools import _crop_bbox_to_pixels

        crop = _crop_bbox_to_pixels(
            bbox_mm=(-50.0, -50.0, 200.0, 200.0),
            extent_mm=(0.0, 0.0, 100.0, 50.0),
            image_size=(1000, 500),
        )
        assert crop == (0, 0, 1000, 500)

    def test_non_zero_origin(self):
        from kcaa.tools.image_tools import _crop_bbox_to_pixels

        # PCB-style extent: board spans x∈[-100,100], y∈[-50,50].
        # bbox top-left at (-50, -25) with size (20, 10):
        #   bbox_x = -50 → 25% from left edge, bbox_x2 = -30 → 35% from left
        #   bbox_y = -25 → 25% from top, bbox_y2 = -15 → 35% from top
        # In a 1000×500 image: x0=250, x1=350, y0=125, y1=175.
        crop = _crop_bbox_to_pixels(
            bbox_mm=(-50.0, -25.0, 20.0, 10.0),
            extent_mm=(-100.0, -50.0, 100.0, 50.0),
            image_size=(1000, 500),
        )
        assert crop == (250, 125, 350, 175)


# ---------------------------------------------------------------------------
# Source-extent helpers
# ---------------------------------------------------------------------------


class TestSourceExtents:
    def test_schematic_paper_a4(self):
        from kcaa.tools.image_tools import _schematic_page_mm

        path = _schematic_with_paper("A4")
        try:
            w, h = _schematic_page_mm(path)
        finally:
            os.unlink(path)
        assert (w, h) == (297.0, 210.0)

    def test_schematic_paper_unknown_falls_back(self):
        from kcaa.tools.image_tools import _schematic_page_mm

        path = _schematic_with_paper("Banana")
        try:
            w, h = _schematic_page_mm(path)
        finally:
            os.unlink(path)
        assert (w, h) == (297.0, 210.0)  # A4 default

    def test_pcb_bbox_from_edge_cuts(self):
        from kcaa.tools.image_tools import _pcb_bbox_mm

        # Rectangle (0,0) → (100, 50) on Edge.Cuts.
        path = _pcb_with_edge_cuts(
            [(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]
        )
        try:
            bbox = _pcb_bbox_mm(path)
        finally:
            os.unlink(path)
        assert bbox == (0.0, 0.0, 100.0, 50.0)
