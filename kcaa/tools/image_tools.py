"""Image rendering tools for KiCad schematics, PCBs, symbols, and footprints.

Provides a single ``render_image`` tool that produces raster / vector images
suitable for passing to a vision-capable LLM for sanity-checking.  Supports
five render targets via a ``kind`` discriminator:

* ``schematic`` — render a ``.kicad_sch`` page
* ``pcb`` — render a ``.kicad_pcb`` board
* ``symbol`` — render a library symbol
* ``footprint`` — render a library footprint
* ``region`` — render a bbox-cropped region of a schematic or PCB

The tool always writes its output to disk (under ``kcaa_data_dir/render/``
by default) and returns the image bytes as MCP ``Image`` content so the
caller can pipe them directly to a vision model.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Literal

import cairosvg
from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image
from PIL import Image as PILImage

from kcaa.utils.config import config
from kcaa.utils.kicad_cli import get_kicad_cli_path, KiCadCLIError
from kcaa.utils.pcb_board_utils import get_edge_cuts_items
from kcaa.utils.pcb_sexp_utils import load_pcb
from kcaa.utils.schematic_sexp_utils import load_schematic
from kcaa.utils.secure_subprocess import run_kicad_command_async

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)


# Standard KiCad paper sizes (mm, landscape orientation).
_PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
    "A5": (210.0, 148.0),
    "Letter": (279.4, 215.9),
    "Legal": (355.6, 215.9),
    "Tabloid": (431.8, 279.4),
}
_DEFAULT_PAPER = "A4"

# Layers shown by the existing ``generate_pcb_thumbnail`` tool — kept in sync.
_DEFAULT_PCB_LAYERS = (
    "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"
)


# ---------------------------------------------------------------------------
# Source-extent helpers (mm bbox of the source document)
# ---------------------------------------------------------------------------


def _schematic_page_mm(schematic_path: str) -> tuple[float, float]:
    """Return ``(width_mm, height_mm)`` for a ``.kicad_sch`` page.

    Reads the ``(paper "...")`` entry from the schematic S-expression and
    maps it through ``_PAPER_SIZES``.  Falls back to A4 landscape if the
    paper entry is missing or unrecognised.
    """
    try:
        tree = load_schematic(schematic_path)
        for item in _walk(tree):
            if (
                isinstance(item, list)
                and len(item) >= 2
                and item[0] == "paper"
                and isinstance(item[1], str)
                and item[1] in _PAPER_SIZES
            ):
                return _PAPER_SIZES[item[1]]
    except (OSError, ValueError) as e:
        log.warning("could not parse paper size from %s: %s", schematic_path, e)
    return _PAPER_SIZES[_DEFAULT_PAPER]


def _pcb_bbox_mm(pcb_path: str) -> tuple[float, float, float, float]:
    """Return ``(x_min, y_min, x_max, y_max)`` mm bbox of Edge.Cuts on a ``.kicad_pcb``.

    Edge.Cuts contains the board outline as ``gr_line`` / ``gr_arc`` /
    ``gr_rect`` / ``gr_circle`` items.  Falls back to ``(0, 0, 100, 100)`` if
    the file has no Edge.Cuts geometry (or cannot be parsed).
    """
    try:
        data = load_pcb(pcb_path)
        items = get_edge_cuts_items(data)
        xs: list[float] = []
        ys: list[float] = []
        for it in items:
            kind = it.get("type")
            if kind in ("gr_line", "gr_rect"):
                xs += [it["x1"], it["x2"]]
                ys += [it["y1"], it["y2"]]
            elif kind == "gr_arc":
                xs += [it["start_x"], it["mid_x"], it["end_x"]]
                ys += [it["start_y"], it["mid_y"], it["end_y"]]
            elif kind == "gr_circle":
                xs += [it["cx"], it["ex"]]
                ys += [it["cy"], it["ey"]]
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))
    except (OSError, ValueError, KeyError) as e:
        log.warning("could not parse Edge.Cuts bbox from %s: %s", pcb_path, e)
    return (0.0, 0.0, 100.0, 100.0)


def _walk(node):
    """Depth-first iteration over a sexpdata-style nested list."""
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk(child)


# ---------------------------------------------------------------------------
# kicad-cli invocation helpers
# ---------------------------------------------------------------------------


async def _run_cli(
    args: list[str],
    output_path: str,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ``kicad-cli <args> --output <output_path>`` and return the result.

    Uses the secure subprocess wrapper so input paths, output directories,
    and the executable are validated.
    """
    full_args = list(args) + ["--output", output_path]
    return await run_kicad_command_async(
        command_args=full_args,
        output_files=[output_path],
        timeout=timeout,
    )


def _load_image(
    path: str,
    fmt: Literal["png", "svg", "pdf"],
) -> tuple[bytes, PILImage.Image | None]:
    """Read ``path`` and return ``(raw_bytes, PIL_image_or_None)``.

    PIL can decode PNG natively.  SVG is returned as raw bytes only
    (Pillow cannot decode SVG without external renderers).  PDF is
    returned as raw bytes only.
    """
    with open(path, "rb") as f:
        data = f.read()
    pil: PILImage.Image | None = None
    if fmt == "png":
        try:
            opened = PILImage.open(path)
            opened.load()  # force decode so the file handle can be released
            pil = opened
        except Exception as e:  # pragma: no cover — defensive
            log.warning("Pillow could not decode %s: %s", path, e)
            pil = None
    return data, pil


def _svg_to_png(
    svg_path: str,
    png_path: str,
    output_width: int | None = None,
    background_color: str | None = None,
) -> None:
    """Convert an SVG file to PNG using cairosvg.

    ``output_width`` scales the SVG so its rendered width matches the
    given pixel count (height auto from aspect).  ``background_color``
    accepts CSS-style values (``"#FFFFFF"``, ``"white"``); ``None`` uses
    cairosvg's default (transparent).
    """
    kwargs: dict[str, object] = {"url": svg_path, "write_to": png_path}
    if output_width is not None:
        kwargs["output_width"] = output_width
    if background_color is not None:
        kwargs["background_color"] = background_color
    cairosvg.svg2png(**kwargs)  # type: ignore[arg-type]


def _render_root_path() -> str:
    """Return the default output directory for rendered images."""
    return os.path.join(config.get_kcaa_data_dir(), "render")


def _crop_bbox_to_pixels(
    bbox_mm: tuple[float, float, float, float],
    extent_mm: tuple[float, float, float, float],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Convert an mm bbox within ``extent_mm`` to a pixel crop box.

    ``extent_mm`` is ``(x_min, y_min, x_max, y_max)``.  ``bbox_mm`` is
    ``(x, y, w, h)`` — top-left + size in mm, KiCad ``+Y down``.
    ``image_size`` is ``(width_px, height_px)``.

    The crop is clamped to the image bounds and rounded to integers.
    """
    ix_min, iy_min, ix_max, iy_max = extent_mm
    bx, by, bw, bh = bbox_mm
    src_w = max(ix_max - ix_min, 1e-9)
    src_h = max(iy_max - iy_min, 1e-9)
    px_w, px_h = image_size
    # x grows right, y grows down → both axes map monotonically.
    x0 = int(round((bx - ix_min) / src_w * px_w))
    y0 = int(round((by - iy_min) / src_h * px_h))
    x1 = int(round((bx + bw - ix_min) / src_w * px_w))
    y1 = int(round((by + bh - iy_min) / src_h * px_h))
    # Clamp to image bounds
    x0 = max(0, min(px_w, x0))
    x1 = max(0, min(px_w, x1))
    y0 = max(0, min(px_h, y0))
    y1 = max(0, min(px_h, y1))
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def register_image_tools(mcp: FastMCP) -> None:
    """Register ``render_image`` with the MCP server."""

    @mcp.tool()
    async def render_image(
        kind: Literal["schematic", "pcb", "symbol", "footprint", "region"],
        schematic_path: str | None = None,
        pcb_path: str | None = None,
        lib_id: str | None = None,
        footprint_id: str | None = None,
        region_target: Literal["schematic", "pcb"] | None = None,
        bbox_x: float | None = None,
        bbox_y: float | None = None,
        bbox_width: float | None = None,
        bbox_height: float | None = None,
        output_format: Literal["png", "svg", "pdf"] = "png",
        output_path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        layers: list[str] | None = None,
        background_color: str | None = None,
        ctx: Context | None = None,
    ) -> Image:
        """Render a KiCad schematic, PCB, symbol, footprint, or bbox region to an image.

        Returns MCP ``Image`` content (so the caller can pass the bytes
        directly to a vision-capable LLM) and also writes the image to
        ``output_path`` if provided, or to ``<kcaa_data_dir>/render/``
        otherwise.

        Args:
            kind: Which render target to use.

              - ``"schematic"`` — render the full page of ``schematic_path``.
              - ``"pcb"`` — render the full board of ``pcb_path``.
              - ``"symbol"`` — render the library symbol ``lib_id``
                (e.g. ``"Device:R_Small"``).
              - ``"footprint"`` — render the library footprint
                ``footprint_id`` (e.g. ``"Resistor_SMD:R_0805_2012Metric"``).
              - ``"region"`` — render a bbox-cropped region of a schematic
                or PCB.  Requires ``region_target`` plus ``bbox_x``,
                ``bbox_y``, ``bbox_width``, ``bbox_height``.  Output is
                always PNG (raster) regardless of ``output_format``.

            schematic_path: Required for ``kind="schematic"`` and as the
                source when ``kind="region"`` and ``region_target="schematic"``.
            pcb_path: Required for ``kind="pcb"`` and as the source when
                ``kind="region"`` and ``region_target="pcb"``.
            lib_id: Required for ``kind="symbol"``.
            footprint_id: Required for ``kind="footprint"``.
            region_target: ``"schematic"`` or ``"pcb"``, required for ``kind="region"``.
            bbox_x, bbox_y: Top-left of the region in mm (KiCad ``+Y down``).
            bbox_width, bbox_height: Region extent in mm.
            output_format: ``"png"`` (default, vision-friendly), ``"svg"``,
                or ``"pdf"``.  Forced to ``"png"`` for ``kind="region"``.
            output_path: Where to write the image.  Defaults to
                ``<kcaa_data_dir>/render/<name>.<ext>``.
            width, height: Render size hints in pixels (forwarded to
                ``kicad-cli``).  ``height`` is auto-computed from aspect
                if omitted.  Default: ``1600``.
            layers: PCB layers to include (kicad-cli ``--layers`` syntax,
                comma-separated).  Defaults to
                ``F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts``.
                Only meaningful for ``kind="pcb"`` and PCB regions.
            background_color: Optional hex color (e.g. ``"#FFFFFF"``) for
                the page background.  Forwarded to ``kicad-cli``.
            ctx: MCP context (used for progress + logging).
        """

        async def _info(msg: str) -> None:
            if ctx:
                await ctx.info(msg)
            log.info(msg)

        # --- validate kind/source ---
        if kind in ("schematic", "region") and (
            kind == "schematic"
            and not schematic_path
            or kind == "region"
            and region_target == "schematic"
            and not schematic_path
        ):
            raise ValueError(f"kind={kind!r} requires schematic_path")
        if kind == "pcb" and not pcb_path:
            raise ValueError("kind='pcb' requires pcb_path")
        if kind == "region" and not region_target:
            raise ValueError("kind='region' requires region_target")
        if kind == "region":
            if region_target == "pcb" and not pcb_path:
                raise ValueError("region_target='pcb' requires pcb_path")
            if (
                bbox_x is None
                or bbox_y is None
                or bbox_width is None
                or bbox_height is None
            ):
                raise ValueError(
                    "kind='region' requires bbox_x, bbox_y, bbox_width, bbox_height"
                )
        if kind == "symbol" and not lib_id:
            raise ValueError("kind='symbol' requires lib_id")
        if kind == "footprint" and not footprint_id:
            raise ValueError("kind='footprint' requires footprint_id")

        # kicad-cli must be on PATH / configured for any of these modes.
        try:
            get_kicad_cli_path(required=True)
        except KiCadCLIError as e:
            raise RuntimeError(str(e)) from e

        # --- region mode forces raster ---
        effective_format: Literal["png", "svg", "pdf"] = "png" if kind == "region" else output_format

        # --- determine render width (used for SVG→PNG conversion only) ---
        render_width = width if width is not None else 1600

        # --- build kicad-cli args (always SVG — kicad-cli export doesn't do PNG) ---
        cli_args: list[str]
        extent_mm: tuple[float, float, float, float] | None = None  # for region
        layers_str = ",".join(layers) if layers else _DEFAULT_PCB_LAYERS

        if kind == "schematic":
            assert schematic_path is not None
            cli_args = ["sch", "export", "svg"]
            if background_color:
                cli_args += ["--background-color", background_color]
            cli_args += [schematic_path]

        elif kind == "pcb":
            assert pcb_path is not None
            cli_args = [
                "pcb",
                "export",
                "svg",
                "--layers",
                layers_str,
            ]
            if background_color:
                cli_args += ["--background-color", background_color]
            cli_args += [pcb_path]

        elif kind == "symbol":
            assert lib_id is not None
            cli_args = [
                "sym",
                "export",
                "svg",
                "--symbol",
                lib_id,
            ]
            if background_color:
                cli_args += ["--background-color", background_color]

        elif kind == "footprint":
            assert footprint_id is not None
            cli_args = [
                "fp",
                "export",
                "svg",
                "--footprint",
                footprint_id,
            ]
            if background_color:
                cli_args += ["--background-color", background_color]

        elif kind == "region":
            assert region_target is not None
            await _info(f"render_image: region mode, target={region_target}")
            if region_target == "schematic":
                assert schematic_path is not None
                extent_mm = (0.0, 0.0, *_schematic_page_mm(schematic_path))
                cli_args = ["sch", "export", "svg"]
                if background_color:
                    cli_args += ["--background-color", background_color]
                cli_args += [schematic_path]
            else:  # region_target == "pcb"
                assert pcb_path is not None
                extent_mm = _pcb_bbox_mm(pcb_path)
                cli_args = [
                    "pcb",
                    "export",
                    "svg",
                    "--layers",
                    layers_str,
                ]
                if background_color:
                    cli_args += ["--background-color", background_color]
                cli_args += [pcb_path]

        else:  # pragma: no cover — defensive
            raise ValueError(f"unknown kind: {kind!r}")

        # --- choose output path (always .svg for the kicad-cli output) ---
        if output_path is None:
            os.makedirs(_render_root_path(), exist_ok=True)
            stem = _default_stem(kind, schematic_path, pcb_path, lib_id, footprint_id)
            svg_path = os.path.join(_render_root_path(), stem + ".svg")
        else:
            out_dir = os.path.dirname(output_path) or "."
            os.makedirs(out_dir, exist_ok=True)
            # kicad-cli writes whatever extension matches the format; pin to .svg
            base, _ = os.path.splitext(output_path)
            svg_path = base + ".svg"

        await _info(f"render_image: kind={kind} → SVG via kicad-cli at {svg_path}")

        # --- run kicad-cli to produce SVG ---
        try:
            proc = await _run_cli(cli_args, svg_path)
        except Exception as e:
            raise RuntimeError(f"kicad-cli invocation failed for {kind}: {e}") from e

        if proc.returncode != 0:
            raise RuntimeError(
                f"kicad-cli failed (exit={proc.returncode}) for kind={kind}: "
                f"{(proc.stderr or proc.stdout or '').strip()}"
            )

        # --- load SVG bytes (always present) ---
        with open(svg_path, "rb") as f:
            svg_bytes = f.read()

        # --- convert SVG → PNG if needed (default + region) ---
        if effective_format == "png":
            png_path = os.path.splitext(svg_path)[0] + ".png"
            try:
                _svg_to_png(
                    svg_path,
                    png_path,
                    output_width=render_width,
                    background_color=background_color,
                )
            except Exception as e:
                raise RuntimeError(f"SVG→PNG conversion failed: {e}") from e
            data, pil = _load_image(png_path, "png")

            # Region mode: Pillow-crop the rasterised PNG.
            if kind == "region" and pil is not None and extent_mm is not None:
                assert (
                    bbox_x is not None
                    and bbox_y is not None
                    and bbox_width is not None
                    and bbox_height is not None
                )
                crop_box = _crop_bbox_to_pixels(
                    (bbox_x, bbox_y, bbox_width, bbox_height),
                    extent_mm,
                    pil.size,
                )
                cropped = pil.crop(crop_box)
                cropped.save(png_path, format="PNG")
                data, pil = _load_image(png_path, "png")
            final_path = png_path
        else:
            # SVG or PDF — return raw bytes from the kicad-cli output.
            data, pil = svg_bytes, None
            final_path = svg_path

        await _info(
            f"render_image: wrote {final_path} ({len(data)} bytes"
            + (f", {pil.size[0]}x{pil.size[1]} px" if pil else "")
            + ")"
        )

        return Image(data=data, format=effective_format)


def _default_stem(
    kind: str,
    schematic_path: str | None,
    pcb_path: str | None,
    lib_id: str | None,
    footprint_id: str | None,
) -> str:
    """Pick a sensible default filename stem for a render."""
    base: str
    if kind == "schematic" and schematic_path:
        base = os.path.splitext(os.path.basename(schematic_path))[0]
    elif kind == "pcb" and pcb_path:
        base = os.path.splitext(os.path.basename(pcb_path))[0]
    elif kind == "symbol" and lib_id:
        base = "sym_" + lib_id.replace(":", "_")
    elif kind == "footprint" and footprint_id:
        base = "fp_" + footprint_id.replace(":", "_")
    elif kind == "region":
        base = "region"
    else:
        base = "render"
    return base
