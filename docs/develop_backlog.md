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

**Status:** implemented (2026-09-07) — set/get tools batched;
`list_footprints` gains only a `ref_prefix` row filter
(`set_symbol_property`, `set_footprint_position`, `set_footprint_property`),
partial-apply per-ref results; get reads (`get_footprint`,
`get_footprint_bbox`, `list_symbol_properties`) take
`references: list[str]`.  `list_nets`/`list_vias`/`list_tracks` unchanged
(bbox/fields projections dropped 2026-09-07 as too complex for the
payoff). `set_net_class_rules` stays single-class; `set_design_rules` /
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
2. List tools stay lean (decided 2026-09-07 — filters/fields projection
   judged too complex for the payoff): only `list_footprints` gains an
   optional `ref_prefix` row filter (default `None` = all footprints).
   `list_nets` (`classify`) and `list_vias` (`net`) are unchanged.
3. Per-object read tools batch too: `references: list[str]` replaces the
   single `reference` on `get_footprint`, `get_footprint_bbox`
   (`pcb_query_tools.py`) and `list_symbol_properties`
   (`symbol_edit_tools.py`).  One file load, per-reference `results[]`
   entries; missing references keep per-ref errors; duplicates/empty
   entries/unknown designators rejected up front.

### Validation

- Unit: batch `set_symbol_property` over refs incl. one missing → one
  save, successes applied, per-ref errors reported; duplicate refs
  rejected; `list_footprints` `ref_prefix` row filter.
- Manual: script a 20-object edit against a board, compare tool calls
  and response size vs. today.

---

## LLM vision-guided routing loop

**Status:** open (proposed)

### Current state

- Routing is one-shot/batch: the plugin auto-route exports the board to
  DSN, runs FreeRouting headless and reimports the SES
  (`kicad_plugin/autorouter.py`, `start_freerouting_thread`); the kcaa
  router routes one net at a time over a world model (obstacles →
  visibility graph → A*), but nothing inspects the routed result or
  steers the router mid-flight. A congested corridor, a via farm or a
  detour net is only caught afterwards by DRC (`run_drc_via_ipc`).
- `generate_pcb_thumbnail(project_path)` (`kcaa/tools/export_tools.py`)
  already renders the board to a PNG via kicad-cli, and `llm_client.py`
  already carries multimodal turns (`_build_user_content` emits
  OpenAI-style `image_url` blocks; converted for Anthropic/Ollama) — but
  nothing feeds a tool-rendered raster back to the model.

### Aim

- Close the loop with LLM vision: render the board state → the LLM
  inspects it (unrouted ratlines, congestion, DRC markers, crossing or
  cramped traces, via density) → returns a short corrective routing plan
  (nets/regions to rework and in what order) → the router/FreeRouting
  applies it → re-render; repeat until the LLM judges the board clean.

### Fix (proposed)

1. **Raster into the loop**: let the review step call
   `generate_pcb_thumbnail` (plus optional zoomed crop on a region from
   the track bbox) and surface the returned PNG as an `images` entry on
   the next turn, reusing the existing multimodal plumbing instead of
   adding a parallel image path.
2. **Vision review**: a schema-constrained prompt over the raster
   returning e.g. `{ok: bool, rework: [{net, area, reason}],
   congestion: [area]}`; parse strictly, reject free-form
3. **Steering mapping**: map `rework` back to executable routing — net
   order + tear-down-and-reroute list for `kcaa.router.router`
   (per-net `build_world_model` calls already exist), or reorder /
   `ignore_nets`-constrained FreeRouting passes (`autorouter.py` already
   supports per-run net ignores).
4. **Termination without silent fallback**: stop when the review returns
   `ok` or an explicit iteration budget is exhausted; on budget
   exhaustion report the last review (remaining issues) to the user
   instead of looping forever or inventing a degraded path.

### Open questions

- Which configured backends are actually vision-capable, and does the
  MCP tool-result → `images` wiring exist anywhere yet (thumbnail PNG
  today returns a path/text, not a base64 content block)?
- Visual review is a *hint*, not geometry truth — final acceptance must
  stay with `run_drc_via_ipc`; the summary is a heuristic gate, not a
  validation path.

### Validation

- Unit: review-output schema parsing and the review → net-order/reroute
  steering mapping over a fixture board with a known congested corridor.
- Integration: small 2-layer board with a deliberately congested corner;
  run the loop on the MCP server and assert it converges (`ok`) or
  reports unresolved nets within the iteration budget.

---

## Resolved

### Unify schematic/PCB version management and archive history

**Status:** resolved (2026-09-08) — PR #121 (commits `20c088a`, `0bcb0fe`,
`0e0e22f`); closes issue #120.

Schematic, PCB and `.kicad_pro` now share one version id and restore
together as a consistent unit.  The `keep-all / daily-weekly` retention
spectrum was trimmed to a parametrized keep-last-N (default 10), the same
default the removed per-file tools used.

#### Implementation

1. `kcaa/utils/version_manager.py` — `save_project_version` /
   `list_project_versions` / `restore_project_version` pack the same-stem
   `.kicad_sch` + `.kicad_pcb` + `.kicad_pro` into one `.tar.gz` under
   `<project_dir>/.versions/project/` (`<stem>.project.<ts>.tar.gz`) with a
   `.manifest.json` (per-file SHA-256) for dedup; restore archives the
   current state first (undoable) and extracts over the project files.
   Only files that currently exist are archived.
2. `kcaa/tools/version_tools.py` — three MCP tools wrapping the manager,
   registered in both profiles through the existing
   `register_version_tools`.
3. `.bak` stays the short-lived single-change safety net; KiCad's
   auto-pruned `.history/` stays independent.  `.gitignore` now excludes
   `**/.versions/`.
4. **Legacy removal + framework migration (same PR, final form):** the
   per-file tools (`save_file_version` / `list_file_versions` /
   `restore_file_version`) and their manager functions
   (`save_version_snapshot` / `list_versions` / `restore_version`) are
   removed — the three project tools are the only versioning surface.
   The plugin framework's auto-snapshot now calls `save_project_version`
   (derived pro path, deduped per project per turn), rollback-history
   pruning keys on `project_file` + `version_id` and prunes any turn that
   touched any file of the restored project, the auto-route pre-routing
   backup uses the project archive, and the tool-policy registry was
   updated accordingly.
5. Old `.versions/<basename>.<ts>` snapshots written by the removed tools
   are no longer readable by any tool; the files are left on disk.

#### Validation

- Unit `tests/unit/tools/test_project_version_tools.py` (14): archive
  bundles all three files, dedup reuse, distinct id on change, pro-only
  project, keep-pruning, missing-file/OSError paths, restore round-trip
  and undo via backup id.
- Integration `tests/integration/test_version_tools.py` (6 new): save /
  list / restore over the real MCP server.
- The legacy unit test file (`tests/unit/tools/test_version_tools.py`) was
  removed with the tools it tested; `test_llm_client.py` auto-snapshot and
  rollback-pruning tests were migrated to the project scheme.

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
had it (separate, still-open improvement).

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
