# Development Backlog

Known issues discovered during review but not fixed in the originating PR.
Each entry records the evidence, the impact, and the proposed fix so the work
can be picked up independently.

## NPTH oval/slot drill shapes are not supported

**Status:** open (discovered while fixing the NPTH index bug)

### Symptom

`_npth_obstacle` only reads a single drill value:

```python
drill = float(drill_sub[1])
```

A KiCad oval drill node is `(drill oval <width> <length>)` — `drill_sub[1]`
is the `Symbol('oval')` tag, so `float()` raises `TypeError` and the
function returns `None`. Oval/slot NPTH holes are silently dropped from the
world model.

### Evidence

Real KiCad file `/home/user1/pcb/ninja-keyboard/.history/ninja-keyboard.kicad_pcb`:

```
(pad "MP" thru_hole rect (at 7.5 -3.1 180) (size 3 2.5)
    (drill oval 2.5 2) (layers "*.Cu" "*.Mask") ...)
```

Multiple `(drill oval w l)` nodes exist in the file history. The router
would generate no obstacle for any of them. (The current
`ninja-keyboard.kicad_pcb` uses 309 circular NPTH drills only.)

### Unresolved question

KiCad's oval-drill semantics for `(drill oval w l)` is not confirmed from
primary source at write time:
- Is `l` the total slot length (outer ends) or the center-to-center
  distance of the two end circles?
- Does the slot's major axis follow the pad's own `at` rotation, the
  footprint rotation, or neither (fixed along one axis)?

The pad examples show mixed data (`(size 3 2.5)` with `(drill oval 2.5 2)`;
`(size 1 1.6)` with `(drill oval 0.6 1.2)`) — size and drill axes do not
obviously align, so the axis rule needs verification before implementing.

### Fix (proposed)

Shape the obstacle as a stadium/capsule: `LineString` between the two end
circle centers, buffered by `width / 2` (shapely:
`LineString([(-d, 0), (d, 0)]).buffer(w / 2, cap_style="round")`), where
`d` depends on the resolved `l` semantics. Rotate by the pad's own `at`
rotation plus the footprint rotation (verify which KiCad actually applies).

### Validation

- Unit: NPTH oval pad node -> obstacle shape whose bounding box matches
  the resolved slot extent.
- Real board: ninja-keyboard history file with `(drill oval ...)` pads
  yields `drill`-kind obstacles.

---

## Wire routing does not avoid label / power-tip / junction anchors

**Status:** open (proposed; not tracked as a GitHub issue)

### Symptom

The schematic wire-routing tools (`kcaa/tools/wire_edit_tools.py`:
`connect_points_with_wire`, `add_wire_to_schematic`, `connect_pins_with_wire`)
build their obstacle set from only three kinds of geometry:

- `_collect_existing_wires` — existing wire segments,
- `_collect_all_pin_positions` — pin tips (incl. power-symbol pins),
- `_collect_pin_symbol_stubs` — pin stub lines.

**Labels never appear in the obstacle set.** `label` occurs in the file
only in `connect_points_with_wire`'s docstring, as an optional *endpoint*
input ("e.g. a net label position") — never as a path obstacle. The same
holds for power-symbol tips (they are covered only because they are also
pins) and for existing junction dots: none are anchor points the router
avoids.

### Evidence

- Grep of `wire_edit_tools.py` shows exactly three
  `_collect_*` obstacle builders, none of which read `label`,
  `global_label`, `hierarchical_label`, `junction`, or the `#PWR?`
  placement set beyond the generic pin walk.
- The router's rejection gates (`_try_angle_config`:
  pin-on-interior, stub overlap, wire overlap, pin-at-corner) have no
  label/junction gate.
- The KiCad connection semantics that make this harmful are now enforced
  in `netlist_parser._build_netlist` Step 2c (issue #100 fix): a point
  item (label, power tip, pin tip) anchored anywhere on a wire segment
  joins that wire's net.

### Impact

A candidate route that passes **through** a label anchor (not at an
endpoint) merges that label's net with the new wire's net in KiCad —
an unintended short or a silently renamed net. The same applies to
power-symbol tips and existing junction dots. `connect_points_with_wire`
using a label position as a *deliberate* endpoint should stay allowed,
but the tools have no way to distinguish (no anchor check at all).

### Fix (proposed)

1. New `_collect_anchor_points(sch)`: local / global / hierarchical
   label anchors + power-symbol (`#PWR?` or `power:` lib_id) tips +
   existing junction positions.
2. Fold the anchors into the existing `obstacles` list so the current
   `_PIN_COLLISION_TOL` (0.5 mm) on-segment circle check rejects routes
   crossing them, same channel as pin positions.
3. Endpoint exemption: a candidate endpoint that coincides with an
   anchor coordinate (user explicitly routed to a label) is allowed,
   mirroring the existing pin-endpoint / lead-stub exemption logic.

### Validation

- Unit: route between two pins whose straight path crosses a label
  anchor → routing rejects or detours; same label anchor as an explicit
  endpoint → route allowed.
- Real board: MotorCell, route a test wire through the `SL_B` mid-wire
  label anchor at (276.86, 197.9422) → rejected (today it would be
  accepted and would quietly merge the `SL_B` net).

---

## Tool-output collapse stops working in long sessions

**Status:** open (reported)

### Symptom

Clicking a tool row to collapse its output sometimes does nothing,
especially once the session has accumulated a lot of content.

### Current implementation

- WebView path (`kicad_plugin/ui/panel.py`, `_WEBVIEW_AVAILABLE`): tool
  rows are HTML5 `<details>/<summary>`, collapsed by clicking the tool
  body. The interaction is handled by `kicad_plugin/ui/shell.js`
  `_installToolCollapse`, a document-level `mousedown` + 250 ms
  `setTimeout` heuristic with drag/double-click/selection guards.
  It is not the native `<summary>` toggle for the body click — only the
  summary line is native.
- Fallback path (no WebView): `_tool_html_plain` in `panel.py` renders a
  compact inline summary with **no folding at all** ("no folding" in the
  docstring) — the collapse affordance silently vanishes.

### Suspected causes (to confirm before fixing)

- The 250 ms click-vs-drag heuristic depends on event timing and the
  pointer path; long DOMs slow `mousedown` handling and the jitter
  threshold can misfire.
- `_tool_html_plain` fallback gives no collapsing UI on systems without
  WebView.

### Fix (proposed)

1. Switch the collapse toggle to the summary element (single native
   `summary` click or its `toggle` event) instead of the body-click
   heuristic; keep the selection-guard only where needed.
2. Add `<details>/<summary>` folding (lightweight DOM, no JS dependency)
   to the `wx.html.HtmlWindow` fallback path so behavior matches.
3. Stress-test with a long session (100+ tool calls) in `test_shell_search.js`.

### Validation

- Unit/JS: `tests/unit/ui/test_shell_search.js` extended — collapse works
  on a rendered session with hundreds of tool rows; search still
  auto-expands collapsed details.
- Manual: long real session, click collapse on an early tool row.

---

## Unify schematic/PCB version management and archive history

**Status:** open (proposed)

### Current state

- `kcaa/tools/version_tools.py` snapshots a single file into the
  project's `.versions/` directory (`save_file_version` /
  `list_file_versions` / `restore_file_version`) — callers must invoke it
  per file, and the two file kinds are managed independently.
- Every edit tool (`symbol_edit_tools.py`, `wire_edit_tools.py`, …)
  writes a `.kicad_sch.bak` before saving — one-shot, single-path.
- KiCad itself maintains a per-file `.history/` folder; three separate
  mechanisms coexist with no shared retention policy.

### Aim

- One versioning scheme covering both `.kicad_sch` and `.kicad_pcb` (and,
  optionally, the whole project tree), so a restore can roll the project
  back as a unit.
- Compact storage: pack history files into one archive (`.tar.gz`/`.zip`)
  per project instead of loose timestamped copies, with a retention
  policy (keep-all, keep-last-N, daily/weekly) chosen by the user.

### Fix (proposed)

1. Extend `version_tools.py` with `save_project_version` /
   `list_project_versions` / `restore_project_version` that snapshot the
   schematic+PCB set (or whole project) as one archive entry.
2. Route the per-edit `.bak` writes through the same archive writer, or
   document explicitly that `.bak` stays a short-lived single-change
   safety net while `.versions/` is the durable archive.
3. Define how archived history interplays with KiCad's own `.history/`
   (which KiCad auto-prunes) to avoid duplication.

### Validation

- Unit: `tests/unit/tools/test_version_tools.py` extended — project
  archive contains consistent sch+pcb pairs; restore returns all files;
  retention policy evicts correctly.
- Manual: edit schematic + PCB, save versions, restore an older pair.

---

## Project-level symbol table with 3rdparty symbol export

**Status:** open (proposed)

### Current state

- Symbol lookup reads only the **global** `sym-lib-table`
  (`kcaa/utils/symbol_index_reader.py`, path from
  `config.ServerConfig.symbol_table_file` → `~/.config/kicad/<ver>/
  sym-lib-table`; `Table`-type entries are followed recursively). There
  is no project-level table next to the `.kicad_pro`.
- `KICAD_3RD_PARTY` (third-party library dir, `config.py`) is only a
  default path constant — nothing writes symbols into it.
- Placed symbols with inline `(lib_symbols ...)` bodies live only inside
  the `.kicad_sch`; they cannot be shared or versioned as libraries.

### Aim

- A project-scoped symbol table (KiCad supports a project-local
  `sym-lib-table`) layered over the global one, so a project pins the
  exact library versions it was designed with.
- A tool to export a schematic's used symbols into `.kicad_sym` files
  under the project's `3rdparty/` directory and register them in the
  project symbol table.

### Fix (proposed)

1. `symbol_index_reader.py`: try the project `sym-lib-table` first, fall
   back to the global table; surface which table each library came from.
2. New `export_symbols_to_3rdparty` tool: collect distinct `lib_id`+symbol
   bodies used by the schematic(s), write one `.kicad_sym` per library
   into `3rdparty/`, generate/update the project `sym-lib-table`, and
   keep the schematic's `lib_id` prefix resolvable.

### Validation

- Unit: `tests/unit/utils/test_symbol_index_reader.py` extended — project
  table takes precedence, indirection still works.
- Integration: export a schematic with inline symbols, reopen it with the
  project table only → symbols resolve from `3rdparty/`.

---

## Batch support for set_*/list_* tools

**Status:** open (proposed)

### Current state

- The set tools operate on one object per call:
  `set_symbol_property` (`symbol_edit_tools.py`), `set_footprint_position`
  (`pcb_placement_tools.py`), `set_footprint_property`
  (`pcb_edit_tools.py`), `set_design_rules`/`set_net_class_rules`
  (`drc_tools.py`), `set_board_outline_rect` — each takes a single target
  reference/path.
- The list tools return whole tables with no filtering: `list_footprints`,
  `list_nets`, `list_tracks`, `list_vias`, `list_symbol_properties`,
  `list_symbol_libraries` (the only one with `limit`/`offset`) — an LLM
  asking for "all nets of one net class" or "footprints near X" must
  fetch and filter the full JSON.

### Impact

Multi-object edits (e.g. set the same property on 10 symbols, move 5
footprints to a row) need N tool calls; large boards make list responses
huge, inflating token use in long sessions (same pressure as the
tool-collapse issue above).

### Fix (proposed)

1. Batch set: accept multiple targets (lists of references) in the set
   tools, returning per-target results; one parse+save per call, single
   `.bak`.
2. Batch/query list: add optional `filter` (net/ref prefix, bbox,
   property) and `fields` (subset projection) parameters to the big
   list tools; keep backwards-compatible defaults.

### Validation

- Unit: batch `set_symbol_property` over 10 refs → one save, all applied;
  `list_footprints` with bbox filter returns only the points inside.
- Manual: script a 20-object edit against a board, compare tool calls
  and response size vs. today.

---

## Project layout accessible without touching the system prompt

**Status:** resolved (2026-09-04) — implemented as a query tool, not a
prompt change.

### Decision

The original plan (append a project tree to the end of `build_system_prompt`)
was rejected: the system prompt must stay stable and token-budgeted.  The
project layout is instead exposed through the MCP query tool
``get_project_structure`` (``kcaa/tools/project_tools.py``), which the LLM
calls on demand when it needs to plan sheet edits, cross-file operations,
or exports.

### Implementation

- ``get_project_structure`` now returns (in addition to the flat ``files``
  set and metadata):
  * ``sheets`` — the root ``.kicad_sch`` plus every hierarchical sub-sheet
    reachable through ``(sheet ...)`` ``Sheetfile`` references, as a nested
    ``{"path", "children"}`` tree (absolute paths, cycle-free via real-path
    tracking, depth-bounded).
  * ``third_party`` — symbol (``.kicad_sym``) and footprint
    (``.kicad_mod``) libraries under the project's ``3rdparty/`` directory.
  * ``lib_tables`` — project-local ``sym-lib-table`` / ``fp-lib-table``
    paths, or None when absent.
- Registered in ``kicad_plugin/tool_registry.py`` as a query policy with
  ``path_arg="project_path"``.

### Validation

- Unit: `tests/unit/tools/test_project_tools.py` — sheet hierarchy follow
  Sheetfile refs, cycles are cut, 3rdparty kinds are reported, absent
  tables are None.
- System prompt tests (`tests/integration/test_skill_system.py`) are
  untouched and still pass.