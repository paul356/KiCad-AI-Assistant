# Image Tools

`render_image` lives in `kcaa/tools/image_tools.py`. It is registered
automatically in both the full and plugin profiles.  Returns MCP `Image`
content (so the caller can pipe the bytes directly to a vision-capable
LLM) and also writes the image to disk for audit and reuse.

## `render_image`

Single tool with a `kind` discriminator that covers five render targets:

| `kind`           | Source                                | Output                |
|------------------|---------------------------------------|-----------------------|
| `"schematic"`    | `.kicad_sch` page                     | full page             |
| `"pcb"`          | `.kicad_pcb` board                    | full board            |
| `"symbol"`       | library symbol by `lib_id`            | symbol preview        |
| `"footprint"`    | library footprint by `footprint_id`   | footprint preview     |
| `"region"`       | bbox of a schematic or PCB            | rasterised crop       |

### Pipeline

`kicad-cli export` only emits SVG (no PNG path). `render_image` runs:

1. **`kicad-cli … export svg`** — produces a vector SVG on disk.
2. **SVG → PNG conversion** via `cairosvg` (when `output_format="png"` or
   `kind="region"`). Default raster width is 1600 px; configurable via
   the `width` parameter.
3. **Pillow crop** (only for `kind="region"`), then re-encode as PNG.
4. Return MCP `Image(data=..., format=...)` and write the final file
   to `output_path` (defaults to `<kcaa_data_dir>/render/<name>.<ext>`).

Vision APIs (OpenAI / Anthropic / Ollama) accept PNG/JPEG/WebP/GIF but
not SVG — keep the PNG default unless you specifically want vector
output for another consumer.

### Coordinate convention

For `kind="region"`, the bbox is in mm with KiCad's `+Y down` convention,
matching the bbox convention used by `save_selection_as_snippet`:

* `bbox_x`, `bbox_y` — top-left of the region in mm.
* `bbox_width`, `bbox_height` — extent in mm.

The mm→pixel mapping comes from:

* **Schematic**: `(paper "A4")` (or A3, Letter, …) read from the
  `.kicad_sch` file. Falls back to A4 landscape (297 × 210 mm) if the
  paper entry is missing or unrecognised.
* **PCB**: the `Edge.Cuts` layer bbox of the `.kicad_pcb` file.
  Falls back to `(0, 0, 100, 100)` mm if Edge.Cuts is empty.

### Symbol / footprint reference format

* `lib_id` — `"Library:SymbolName"`, e.g. `"Device:R_Small"`,
  `"power:GND"`.
* `footprint_id` — `"Library:FootprintName"`, e.g.
  `"Resistor_SMD:R_0805_2012Metric"`.

The library must already be on the user's KiCad library table — the
tool does not look up symbols in the `kcaa` symbol index; it forwards
the id straight to `kicad-cli sym export`.

### Layer default (PCB)

Same as the existing `generate_pcb_thumbnail`:

```
F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts
```

Override with the `layers` parameter using kicad-cli's `--layers` syntax
(comma-separated layer names).

### Output paths

| Parameter        | Effect                                                   |
|------------------|----------------------------------------------------------|
| `output_path`    | Where to write the final file. Parent dir auto-created.  |
| (unset)          | `<kcaa_data_dir>/render/<stem>.<ext>`                    |

`<stem>` is derived from the source — schematic/PCB basename, or
`sym_<lib>` / `fp_<footprint>` for symbol/footprint, or `region` for
bbox crops.

### Common parameters

* `width: int = 1600` — rasterised PNG width in pixels (forwarded to
  cairosvg's `output_width`). Height auto-derived from aspect.
* `background_color: str` — CSS hex like `"#FFFFFF"` or named color.
  Optional.
* `output_format: "png" | "svg" | "pdf"` — default `"png"`. Forced to
  `"png"` for `kind="region"` because cropping requires raster.

### Example calls

```python
# Render a schematic page to PNG for the vision model
render_image(
    kind="schematic",
    schematic_path="/path/to/sheet.kicad_sch",
)

# Crop a specific area of the schematic for the vision model
render_image(
    kind="region",
    region_target="schematic",
    schematic_path="/path/to/sheet.kicad_sch",
    bbox_x=50.0, bbox_y=50.0, bbox_width=100.0, bbox_height=50.0,
)

# Render a single library symbol
render_image(
    kind="symbol",
    lib_id="Device:R_Small",
    output_format="svg",   # vector for non-vision use
)

# Render the PCB with a custom layer set
render_image(
    kind="pcb",
    pcb_path="/path/to/board.kicad_pcb",
    layers=["F.Cu", "F.SilkS", "Edge.Cuts"],
)
```

### Failure modes

* **`KiCadCLIError`** — `kicad-cli` not found on `PATH`, no
  `KICAD_CLI_PATH` env var, and no platform-specific install path.
  Install KiCad or set `KICAD_CLI_PATH=/path/to/kicad-cli`.
* **`RuntimeError("kicad-cli failed (exit=N) …")`** — the CLI exited
  non-zero. Stderr/stdout are included in the message.
* **`RuntimeError("SVG→PNG conversion failed: …")`** — `cairosvg`
  could not rasterise the SVG (malformed output from kicad-cli,
  missing cairo system library, etc.).
* **`FileNotFoundError`** / **`PathValidationError`** — `output_path`
  points outside the trusted directories. Use a path under the
  kcaa data dir or pass an explicit trusted location.

## What this PR is NOT

This tool produces **static images** of the current state of the
schematic / PCB / library.  It is not:

* A live-view screenshot of the KiCad editor window (cross-process
  capture is out of scope — see `plans/kicad-plugin-enhancement.md`).
* A 3D render of the PCB (use `kicad-cli pcb render` directly if you
  need raytraced 3D imagery).
* A diff tool — there is no before/after visualisation in this PR.

## Relationship to other tools

* **`generate_pcb_thumbnail`** (`kcaa/tools/export_tools.py`) —
  also calls `kicad-cli pcb export svg`, but returns the SVG content
  directly without cairosvg conversion. Kept as-is per the PR review;
  `render_image(kind="pcb")` is the vision-model-friendly counterpart.
* **`save_selection_as_snippet`** (`kcaa/tools/snippet_tools.py`) —
  extracts a bbox region of a schematic as a portable
  `.kicad_snippet`. `render_image(kind="region")` extracts the same
  bbox but as a PNG image for vision models. The two tools share the
  mm `+Y down` bbox convention.
