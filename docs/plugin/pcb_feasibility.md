# PCB Editing Feasibility Study

## 1. Purpose

This document assesses what PCB editing operations are realistic using the `skip` library on `.kicad_pcb` files, identifies which operations should remain schematic-only in milestone 1, and states the prerequisites before any PCB editing code is written.

---

## 2. Milestone 1 Decision

**PCB editing is out of scope for milestone 1.**

The schematic-editing plugin must be stable and the feasibility spike below must be completed before any PCB tool is implemented.

---

## 3. The `skip` Library and PCB Files

The `skip` library (`kicad-skip` on PyPI) is a pure-Python S-expression parser and writer for KiCad file formats. Feasibility has been validated with a round-trip spike (see §3.2).

### 3.1 Spike Results (KiCad 7-format `.kicad_pcb`)

The following operations were tested and **confirmed working**:

| Operation | Result |
|-----------|--------|
| `skip.PCB(path)` — load a board file | ✅ Works |
| `list(pcb.footprint)` — enumerate footprints | ✅ Works; returns `FootprintWrapper` list |
| `fp.at.value = [x, y]` — move a footprint | ✅ Works; persists after `write()` + reload |
| `pcb.write(path)` — write modified board back | ✅ Works |
| `skip.PCB(path)` after write — reload verification | ✅ Move confirmed; other footprints unchanged |
| `pcb.net` — enumerate nets | ✅ Works; each net has `children[0]` (index) and `children[1]` (name) |
| `pcb.segment` — access copper segments | ✅ Collection accessible; individual segment iteration works |

**Important API note:** `fp.fp_text` items have `children[0]` typed as `Symbol`, not `str`. When filtering by type (`'reference'`, `'value'`), compare with `str(t.children[0]) == 'reference'`, not direct string equality.

### 3.2 What is still uncertain

- Whether `skip` correctly handles all PCB-specific constructs in real complex boards (net tie footprints, padstacks, complex zones, embedded files, hierarchy)
- Whether writing a `skip`-modified `.kicad_pcb` back to disk produces a file that KiCad 8/9/10 accepts without error on a real board
- Zone fill regeneration — `skip` does not invoke KiCad's zone-fill algorithm; zones must be manually refilled in KiCad after any edit

---

## 4. Feasibility Spike Required

Before any PCB tool is written, a feasibility spike must be completed:

### 4.1 Spike goals

1. Load a real `.kicad_pcb` file with `skip.Schematic` (or the appropriate class) and enumerate all footprints
2. Modify one footprint position and write the file
3. Open the modified file in KiCad and confirm it loads without errors and the footprint moved as expected
4. Repeat for a `gr_text` add/remove operation
5. Document which `.kicad_pcb` features `skip` handles, which it silently drops, and which cause parse errors

### 4.2 Spike acceptance criteria

The spike passes if:
- All tested operations round-trip without KiCad load errors
- UUIDs, net assignments, and pad connections are preserved after a footprint move
- Test file is a non-trivial board (at least 10 footprints, multiple layers, at least one zone)

---

## 5. Planned PCB Tool Surface (Post-Milestone-1)

The following tools are planned. None of these should be implemented until the feasibility spike passes.

| Tool | Operation | Risk |
|------|-----------|------|
| `list_pcb_footprints` | Read all footprints: ref, value, position, layer, side | Low — read-only |
| `get_footprint_position` | Read position and rotation of one footprint | Low — read-only |
| `move_footprint` | Update `at` coordinates of a footprint | Medium — must preserve UUID and net refs |
| `set_footprint_property` | Update `value`, `reference`, or `datasheet` field | Medium — embedded in footprint S-expr |
| `list_pcb_nets` | Enumerate board-level nets | Low — read-only |
| `list_pcb_tracks` | Enumerate copper segments: layer, start, end, width | Low — read-only |
| `add_pcb_text` | Add a `gr_text` item to a layer | Medium — requires correct syntax for the KiCad version |
| `delete_pcb_text` | Remove a `gr_text` item | Medium — match by content + position |
| `list_pcb_zones` | Enumerate copper pour zones: layer, net | Low — read-only |

### 5.1 Operations intentionally excluded from the PCB milestone

| Operation | Reason |
|-----------|--------|
| Route new copper tracks | Requires DRC-aware routing; `skip` cannot validate clearance or net connectivity |
| Add or remove copper pours | Zone fill is a complex DRC operation; not feasible via file editing alone |
| Change the board outline | Board outline edits require understanding keepout/copper interactions |
| Netlist-driven footprint assignment | This is a schematic-side operation (assign footprint → export netlist → import to PCB) |
| Move multiple footprints simultaneously | Requires understanding pairwise clearances; too risky without DRC |

---

## 6. Context Bridge Extensions Needed for PCB

When PCB editing is added, the context bridge (see `docs/plugin/context_bridge.md`) must be extended:

```json
{
  "active_pcb": "/path/to/board.kicad_pcb",
  "active_editor": "pcb",
  "selected_refs": ["U1"],
  "pcb_layers": ["F.Cu", "B.Cu", "F.Silks", "B.Silks"]
}
```

The plugin will need to query `pcbnew.GetBoard()` for the active PCB file path and expose selected footprint references.

---

## 7. Mutation Safety for PCB Editing

PCB files have additional mutation safety concerns compared to schematics:

- A footprint `at` value is tied to the physical PCB coordinate system; incorrect values can place components outside the board outline
- Track segments reference net indices by number; if net numbering changes, tracks become disconnected
- Zone fills must be regenerated after edits (this requires KiCad, not `skip`)

For PCB mutations, the same `.bak` pattern used for schematics must be applied, and the plugin should additionally prompt the engineer to run DRC after any PCB edit.

---

## 8. Prerequisites Before PCB Implementation Begins

The following must all be true before any PCB tool code is written:

1. ✅ Schematic-editing plugin integration is working end-to-end
2. ✅ Context bridge is stable and tested
3. ⬜ `skip` feasibility spike completed and acceptance criteria passed
4. ⬜ PCB context bridge extension implemented in the plugin
5. ⬜ Mutation safety contract reviewed for PCB-specific concerns
6. ⬜ Plugin UX reviewed for PCB viewport feedback (reload, selection, layer visibility)
7. ⬜ Test `.kicad_pcb` file added to the test suite for round-trip validation
