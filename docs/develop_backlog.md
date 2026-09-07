# Development Backlog

Known issues discovered during review but not fixed in the originating PR.
Each entry records the evidence, the impact, and the proposed fix so the work
can be picked up independently.  Resolved items are moved to the
``## Resolved`` section at the bottom.

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

### Semantics (resolved 2026-09-07, primary source KiCad master `pcbnew/pad.cpp`)

- **`l` is the total outer slot length** (including both end caps), not the
  center-to-center distance. `PAD::GetEffectiveDrillShape` builds the hole as
  `SHAPE_SEGMENT` with half-width `min(w, l)/2` and endpoint offset
  `(|w−l|/2, |w−l|/2)` on the longer drill axis, i.e. endpoints at
  `±(l−w)/2`: outer extent = `l`, center-to-center = `l − w`.
- **The slot's major axis follows the longer drill dimension** in the pad's
  local frame (X when `w > l`, Y when `l > w`; `w == l` degenerates to a
  circle of radius `w/2`).
- **Rotation:** the endpoint offset is rotated by the pad's effective
  orientation (`GetOrientation()`, master: `m_libOrientation +
  parentFootprint->GetOrientation()`).  Convention confirmed on the repo
  side: `pcb_footprint_utils.py` documents that KiCad 10 files store pad
  rotation as **absolute board-space degrees**, matching
  `PAD::SetOrientation`/`GetOrientation` (which convert between the
  relative lib frame and board space by adding/subtracting the footprint
  orientation).  Sanity note: a slot with pad angle 0 inside a rotated
  footprint renders *with* the footprint rotation in KiCad; pin this down
  with one real file where both footprint and pad carry nonzero rotation
  before finalizing the implementation.

  Caveat on the current router: `_pad_obstacle` deliberately rotates by
  `fp_rot` **only** and ignores the pad's own `at` rotation
  (`world_model.py:404-407`), while `pcb_query_tools.list_footprints`
  treats `pad_rot` as absolute (`:319`). Any oval-slot implementation must
  first unify this rotation pipeline — a rotated pad's copper obstacle
  itself is currently drawn at the wrong angle.

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

**Status:** implemented (2026-09-07) — set/list/get tools batched
(`set_symbol_property`, `set_footprint_position`, `set_footprint_property`),
partial-apply per-ref results; list filters on `list_footprints` /
`list_nets` / `list_vias` with unknown-field rejection; `list_tracks`
keeps `net`+`layer` filters only (projection skipped; grouping makes it
ambiguous). `set_net_class_rules` stays single-class; `set_design_rules` /
`set_board_outline_rect` non-per-object, out of scope.

### Current state

- The set tools operate on one object per call:
  `set_symbol_property` (`symbol_edit_tools.py`), `set_footprint_position`
  (`pcb_placement_tools.py`), `set_footprint_property`
  (`pcb_edit_tools.py`), `set_net_class_rules` (`drc_tools.py`) — each
  takes a single target reference/class.  (`set_design_rules` takes a
  project-level dict, `set_board_outline_rect` a single rectangle — both
  non-per-object, out of scope.)
- The list tools have limited filtering: `list_footprints`, `list_nets`
  (only `classify`) return whole tables; `list_tracks`/`list_vias`
  (`net`, tracks also `layer`) and `list_symbol_libraries`
  (`limit`/`offset`) are partial — "all nets of one net class" or
  "footprints near X" still requires fetching the full JSON.  Batch
  precedent already exists: `assign_nets_to_class`, `align_footprints`
  take `references: list[str]`.

### Impact

Multi-object edits (e.g. set the same property on 10 symbols, move 5
footprints to a row) need N tool calls; large boards make list responses
huge, inflating token use in long sessions (same pressure as the
tool-collapse issue above).

### Fix (design agreed 2026-09-07 — batch-first, no backward-compat
retained per maintainer decision)

1. Set tools switch to `items: list[dict]`: each self-contained entry
   `{"reference", ...spec}` replaces the single `reference` + shared
   property/value params on `set_symbol_property` (`property_name` +
   `property_value`), `set_footprint_position` (`x`/`y`/`rotation`, each
   optional but at least one required per entry), `set_footprint_property`
   (`property_name` + `value`).  Targets may carry different values in one
   call.  One parse + one save per call, single `.bak`.  Per-target results
   in `results[]`; partial-apply semantics (successful targets saved,
   failures keep their per-ref error — per `docs/plugin/mutation_safety.md`).
   Duplicate references or unknown item fields are rejected.
   `set_net_class_rules` stays single-class.
2. List tools get named query params, default `None` = current output:
   - `list_footprints(ref_prefix, bbox=[xmin,ymin,xmax,ymax], fields)`
   - `list_nets(name_prefix, netclass, fields)` — `netclass` filter
     implies classify resolution
   - `list_vias(fields)`
   - `list_tracks` already filters by `net`+`layer`; fields projection
     skipped there (trace/segment grouping makes projection ambiguous).
   Unknown field names are rejected, not silently dropped.
3. Per-object read tools batch too: `references: list[str]` replaces the
   single `reference` on `get_footprint`, `get_footprint_bbox`
   (`pcb_query_tools.py`) and `list_symbol_properties`
   (`symbol_edit_tools.py`).  One file load, per-reference `results[]`
   entries; missing references keep per-ref errors; duplicates/empty
   entries/unknown designators rejected up front.

### Validation

- Unit: batch `set_symbol_property` over refs incl. one missing → one
  save, successes applied, per-ref errors reported; duplicate refs
  rejected; `list_footprints` bbox/ref_prefix/fields; `list_nets`
  netclass filter implies classification; `list_vias` fields.
- Manual: script a 20-object edit against a board, compare tool calls
  and response size vs. today.

---

## Resolved

### Tool-output collapse stops working in long sessions

**Status:** resolved (2026-09-07) — fixed by PR #118 (commit `87e1a24`); closes issue #117.

#### Root cause (confirmed)

The collapse interaction depended on globally unique `details` ids. Tool
rows get `id="tool_<seq>"` from a per-panel counter (`_seq`, assigned in
`stream_events.apply_stream_event`). Session files persist each
`tool_call` entry's `_seq` verbatim (`session_store.make_payload`), and
the restore paths (`_autoload_session`, `_restore_session_file`) load
those entries into a panel whose `self._tool_seq` was never recomputed —
a fresh panel restarts the counter at 0, so new tool calls re-issued ids
`tool_1..tool_K` that collided with restored rows. `shell.js` collapsed
the row via `document.getElementById(data-details)`, which returns the
first match in document order: after a restore, clicking a colliding row
collapsed the older row (or no-opped when that row was already closed).
Real session files showed up to 147 duplicate ids and 23 seq-restart
generations in a single conversation. WebView is the only path with
folding; the `wx.html.HtmlWindow` fallback (`_tool_html_plain`) has never
had it.

#### Fix

1. `shell.js` collapse now resolves the target via
   `toolBody.closest('details.tools')` (DOM position) instead of
   `getElementById`, and the whole `tool_<seq>` details-id mechanism is
   removed (`_seq`/`_tool_seq` counter, session persistence, JS `uid`
   generation and the `id`/`data-details` attributes): nothing addresses
   tool rows by id anymore, so the id-collision failure class is
   structurally gone and no restore-time counter bookkeeping is needed.

#### Validation

- Unit: `test_shell_search.js` gains body-click collapse and
  duplicate-id regression (25/25) — the old `getElementById` path was
  verified to fail the new assertions; `test_stream_events.py` updated
  for the removed seq field.
- Full unit suite: 1276 passed, 17 skipped; ruff clean.

---
### Wire routing does not avoid label / power-tip / junction anchors

**Status:** resolved (2026-09-04) — fixed by PR #115 (commit `afa504d`); closes issue #114.

#### Symptom

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

#### Evidence

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

#### Impact

A candidate route that passes **through** a label anchor (not at an
endpoint) merges that label's net with the new wire's net in KiCad —
an unintended short or a silently renamed net. The same applies to
power-symbol tips and existing junction dots. `connect_points_with_wire`
using a label position as a *deliberate* endpoint should stay allowed,
but the tools have no way to distinguish (no anchor check at all).

#### Fix

1. New `_collect_anchor_points(sch)`: local / global / hierarchical
   label anchors + power-symbol (`#PWR?` or `power:` lib_id) tips +
   existing junction positions.
2. Fold the anchors into the existing `obstacles` list so the current
   `_PIN_COLLISION_TOL` (0.5 mm) on-segment circle check rejects routes
   crossing them, same channel as pin positions.
3. Endpoint exemption: a candidate endpoint that coincides with an
   anchor coordinate (user explicitly routed to a label) is allowed,
   mirroring the existing pin-endpoint / lead-stub exemption logic.

#### Validation

- Unit: route between two pins whose straight path crosses a label
  anchor → routing rejects or detours; same label anchor as an explicit
  endpoint → route allowed.
- Real board: MotorCell, route a test wire through the `SL_B` mid-wire
  label anchor at (276.86, 197.9422) → rejected (today it would be
  accepted and would quietly merge the `SL_B` net).

---
### Project layout accessible without touching the system prompt

**Status:** resolved (2026-09-04) — implemented as a query tool, not a
prompt change (PR #116).

#### Decision

The original plan (append a project tree to the end of `build_system_prompt`)
was rejected: the system prompt must stay stable and token-budgeted.  The
project layout is instead exposed through the MCP query tool
``get_project_structure`` (``kcaa/tools/project_tools.py``), which the LLM
calls on demand when it needs to plan sheet edits, cross-file operations,
or exports.

#### Implementation

- ``get_project_structure`` now returns (in addition to the flat ``files``
  set and metadata):
  * ``sheets`` — the root ``.kicad_sch`` plus every hierarchical sub-sheet
    reachable through ``(sheet ...)`` ``Sheetfile`` references, as a nested
    ``{"path", "children"}`` tree (absolute paths, cycle-free via real-path
    tracking, depth-bounded).  The ``children`` key is omitted for sheets
    with no sub-sheets.
  * ``lib_tables`` — project-local ``sym-lib-table`` / ``fp-lib-table``
    paths, or None when absent.
- Registered in ``kicad_plugin/tool_registry.py`` as a query policy with
  ``path_arg="project_path"``.
- Registered in the plugin server profile (``KICAD_MCP_PROFILE=plugin``)
  via ``register_project_tools(mcp, tools=("get_project_structure",))``,
  so the KiCad plugin's LLM can call it; the full-profile management
  tools (``list_projects`` / ``open_project``) stay out of that profile.

#### Validation

- Unit: `tests/unit/tools/test_project_tools.py` — sheet hierarchy follow
  Sheetfile refs, cycles are cut, leaf sheets omit ``children``, absent
  tables are None.
- Server profiles: `tests/unit/test_server_profiles.py` — plugin profile
  exposes ``get_project_structure`` but not ``list_projects``/``open_project``.
- System prompt tests (`tests/integration/test_skill_system.py`) are
  untouched and still pass.
