# Snippet Tools

`save_selection_as_snippet` and `read_snippet` live in `kcaa/tools/snippet_tools.py`.
Both are registered automatically (full + plugin profiles).

## `save_selection_as_snippet`

Export a rectangular region of a `.kicad_sch` file to a portable
`.kicad_snippet` file. Bbox-local coordinates are normalised to `(0, 0)`,
and every `lib_symbol` referenced by an exported placed instance is
bundled inside the snippet's `(lib_symbols ...)` section — so the file
travels with its own dependencies and does not need the host project to
have a particular library alias.

### Coordinate convention

* `bbox_x`, `bbox_y`: selection top-left in mm (parent coordinates).
* `bbox_width`, `bbox_height`: selection extent in mm.
* The bbox is in KiCad's `+Y down` screen convention.
* A 1.27 mm border-tolerance matches one KiCad grid step — elements
  whose geometry crosses the boundary by less than this are kept.

### Symbol reference format

`lib_id` is normalised to symdir form before being written inside the
snippet's `(symbol (lib_id "...") ...)` entries. So `Device:R_Small`
becomes `R_Small`. The host project must still have the symbol available,
but the snippet does not depend on the host's library-alias table.

### Multi-unit symbols

Each placed `R_Small` references the *parent* symbol. KiCad looks up
the unit definitions (`R_Small_0_1`, `R_Small_1_1`) as nested children
of that parent. `save_selection_as_snippet` serialises the full parent
S-expression (parent + all children) via `sexpdata.dumps(sym.raw)`, so
unit information travels with the snippet.

### Write semantics

* Atomic write: the file is written to `<output>.tmp` first, then renamed
  over the destination. A crash never leaves a partial file.
* Overwrite safety: if `<output>` already exists, the previous version
  is copied to `<output>.bak>` before the rename.

## `read_snippet`

Lightweight regex scan of a `.kicad_snippet` file. Returns:

* `name`, `uuid`
* `counts` (wires, junctions, labels, symbols, lib_symbols)
* `raw_size_bytes`
* a `note` warning that counts include matches inside `(lib_symbols ...)`.

`read_snippet` does not model the snippet via `skip` (skip does not
have a Snippet class). It is a sanity check, not a strict inventory.

## What this PR is NOT

This PR is scoped to *saving* reusable blocks. The symmetric *place* /
*insert* operation (the inverse direction — drop a snippet into another
schematic) is not in scope; that would require either a skip
snippet-placement path or a sexpdata-based injection. Out of scope for
this commit.

## Relationship to other tools

* **`add_sheet_symbol`** (`kcaa/tools/sheet_tools.py`) is the
  hierarchical-subcircuit path: it places a sheet symbol on a parent
  schematic pointing at a separate `.kicad_sch` file.
* **`save_selection_as_snippet`** is the snippet path: it exports a
  selection from any schematic into a self-contained `.kicad_snippet`
  file that can be pasted into other schematics via KiCad's *Place →
  Reusable Design Blocks* menu.

Both exist because they serve different reuse models (per-project
subcircuit vs cross-project snippet palette).