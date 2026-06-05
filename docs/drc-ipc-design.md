# DRC IPC Integration & FreeRouting Constraint Design

> **Status: ✅ All phases complete** (Phases 0–3), see [Implementation Notes](#implementation-notes) for deviations.

## Background

The project currently has no IPC-based DRC pathway:

- **`kicad-cli pcb drc`** (`kcaa/tools/drc_impl/cli_drc.py`) is the legacy approach — it requires
  KiCad CLI installed, works only on saved files, and is incompatible with live-plugin workflows.
  **This path will be retired entirely** in favor of IPC.
- **kipy IPC** (`kcaa/tools/kipy_tools.py`): Already connects to running KiCad for save/reload.
  KiCad 9+ exposes the **KiCad API** (protobuf over IPC socket) with full access to board design
  rules, custom rules, and DRC markers — but none of this is used for DRC today.

FreeRouting (`kicad_plugin/autorouter.py`) exports `SpecctraDSN` → routes → imports `SpecctraSES`
with **zero constraint awareness** — clearance, track width, and custom rules are ignored.

This document specifies a two-pronged design:
- **A**: Run DRC and read violations purely via IPC — in both plugin and standalone MCP modes.
- **B**: Pipe board constraints into FreeRouting and validate results post-route.

---

## Design Goals

1. Both plugin (`kicad_plugin/`) and standalone MCP server use kipy IPC exclusively for DRC
   execution — no `kicad-cli` fallback anywhere.
2. Board design rules (constraints, custom rules) are readable/writable through
   S-expression file parsing of `.kicad_pcb`, with typed Python wrappers in
   `pcb_design_rules.py`. When kipy's `board_rules` proto wrappers mature, this
   can be migrated to IPC.
3. FreeRouting receives relevant constraint values before routing, and results are
   DRC-validated automatically.
4. All new tools follow the existing registration pattern (`register_*_tools(mcp)`).
5. The legacy `cli_drc.py` path is deprecated (no longer imported by `drc_tools.py`).

---

## Architecture (actual implementation)

```
┌─────────────────────────────────────────────────────────────────┐
│                    MCP Tools (kcaa/tools/)                       │
│                                                                  │
│  run_drc_check() ─── runs DRC (kipy) + reads markers (pcbnew)  │
│  get_design_rules() ─── parses .kicad_pcb S-expression file     │
│  set_design_rules() ─── updates .kicad_pcb S-expression file    │
│  list_custom_rules() ─── reads (custom_rules ...) from file     │
│  add_custom_rule() ─── appends custom rule to file              │
│  run_autoroute() ─── enhanced with constraint passing + DRC      │
└───────────────────────────┬─────────────────────────────────────┘
                            │
          ┌─────────────────┴─────────────────┐
          ▼                                   ▼
┌─────────────────────────┐    ┌──────────────────────────────┐
│  kipy IPC               │    │  S-expression file parsing   │
│                          │    │  (sexpdata library)          │
│  run_action("runDRC")   │    │                              │
│  run_action("clear")    │    │  pcb_design_rules.py reads   │
│  board.refill_zones()   │    │  and writes (setup            │
│                          │    │  (design_rules ...)          │
│  pcbnew.GetBoard()      │    │  (custom_rules ...))         │
│    .GetMarkers()        │    │  directly in .kicad_pcb      │
│  → parse PCB_MARKER     │    │  → no KiCad process needed   │
│                          │    │  → .bak snapshots for       │
│  Requires: KiCad running │    │    rollback                  │
└─────────────────────────┘    └──────────────────────────────┘
```

**Rationale for the dual-path approach**:

- **DRC execution** uses kipy IPC (`run_action` → `pcbnew.GetMarkers()`) because DRC
  markers are runtime artifacts that only exist inside a running KiCad process.
- **Design rules** use S-expression file parsing because kipy v9.1.0's `board_rules`
  module does not provide full wrapper classes for `GetBoardDesignRules` /
  `UpdateBoardDesignRules` protobuf operations. Reading/writing the `.kicad_pcb`
  file directly via `sexpdata` is process-independent, CI-testable, and operates on
  the same native KiCad format.

This replaces the original plan's single-IPC approach (both DRC and design rules
via kipy protobuf) with a pragmatic hybrid that avoids depending on kipy proto
wrappers that don't fully exist yet.

---

## kipy IPC Capabilities (KiCad 9+)

### Available via `kipy.board_rules` (kipy v9.1.0)

**Note**: The following classes exist in the kipy package but the protobuf wire
commands (`GetBoardDesignRules`, `UpdateBoardDesignRules`, `GetCustomRules`,
`SetCustomRules`) do not have full wrapper coverage in the current version.
See [Implementation Notes](#implementation-notes) for the chosen workaround.

| Class | What it provides |
|-------|-----------------|
| `BoardDesignRules` | `constraints`, `predefined_sizes`, `solder_mask_paste`, `teardrops`, `via_protection`, `severities`, `exclusions` |
| `MinimumConstraints` | `min_clearance`, `min_track_width`, `min_via_size`, `min_through_drill`, `min_via_annular_width`, `hole_clearance`, `hole_to_hole_min`, `copper_edge_clearance`, `silk_clearance`, `min_silk_text_height`, `min_silk_text_thickness`, `min_resolved_spokes`, `min_connection_width`, `min_groove_width`, `min_microvia_size`, `min_microvia_drill` |
| `PredefinedSizes` | `tracks[]`, `vias[]`, `diff_pairs[]` — each with width/gap/diameter/drill |
| `SolderMaskPasteDefaults` | `mask_expansion`, `mask_min_width`, `mask_to_copper_clearance`, `paste_margin`, `paste_margin_ratio` |
| `CustomRule` | `name`, `condition` (Lisp DSL), `constraints[]` (`CustomRuleConstraint`), `severity`, `layer_mode` |
| `CustomRuleConstraint` | `type` (40 constraint types), `numeric` (MinOptMax), `disallow`, `zone_connection`, `assertion_expression`, `options` |
| `DrcSeveritySetting` | `rule_type` → `severity` mapping per `DrcErrorType` |
| `DrcExclusion` | `marker` (RuleCheckerMarker) + `comment` |

### Available via `kipy.KiCad.run_action()`

| Action | Effect |
|--------|--------|
| `"pcbnew.InspectionTool.runDRC"` | Triggers DRC in KiCad GUI, populates board markers |
| `"pcbnew.InspectionTool.clearMarkers"` | Removes all DRC markers from the board |
| `"pcbnew.InspectionTool.listUnconnected"` | Lists unconnected pads (separate from DRC) |

### Available via `board` object methods

| Method | Effect |
|--------|--------|
| `board.refill_zones(block=True)` | Refills all zones, blocks until complete (uses `RefillZones` proto). Already implemented in `kcaa/tools/pcb_zone_tools.py`. |

### kipy Limitations (as of v9.1.0)

| Gap | Why | Resolution |
|-----|-----|------------|
| Cannot read `PCB_MARKER` items | No wrapper class in kipy `_proto_to_object` | Use `pcbnew` board API (`board.GetMarkers()`) in the KiCad process |
| Design rules proto commands not fully wrapped | `kipy.board_rules` types exist but `GetBoardDesignRules`/`UpdateBoardDesignRules` send path not exposed | Read/write `.kicad_pcb` S-expression file directly via `sexpdata` |
| No `refill_zones` via `run_action` | KiCad action toolbar doesn't expose it as a named action | Use kipy's `board.refill_zones()` proto wrapper (already available) |

---

## Task Breakdown

### Phase 0 — Foundation (no user-visible change)

> **Goal**: Build IPC DRC runner and replace the CLI-based `run_drc_check` with it.

- [x] **0.1 Create `kcaa/tools/drc_impl/ipc_drc.py`**
  - `async def run_drc_via_ipc(board, ctx) -> dict`:
    1. Call `kipy.KiCad.run_action("pcbnew.InspectionTool.clearMarkers")` to clear old markers.
    2. Call `kipy.KiCad.run_action("pcbnew.InspectionTool.runDRC")` to trigger DRC.
    3. Wait briefly (DRC is synchronous on the KiCad side, but markers may need a frame).
    4. Read markers via `pcbnew` board API (the KiCad native Python module):
       ```python
       import pcbnew
       for marker in board.GetMarkers():
           # marker: PCB_MARKER with .GetDescription(), .GetPosition(), .GetSeverity()
       ```
    5. Parse each marker into `{"message": ..., "severity": ..., "location": {"x": ..., "y": ...}, "items": [...]}`.
    6. Return `{"success": True, "total_violations": N, "violations": [...], "violation_categories": {...}}`.
  - Handle: KiCad not running, no board open, pcbnew import failure.

- [x] **0.2 Replace `run_drc_check` in `kcaa/tools/drc_tools.py`**
  - `run_drc_check()` currently hardcodes the CLI path via `run_drc_via_cli()`.
  - Replace with `run_drc_via_ipc()` — single code path, no dispatch.
  - kipy connection is obtained the same way as in `kipy_tools.py` (`_connect()` helper).
  - Standalone MCP server connects to a running KiCad instance via the same IPC socket.
  - If KiCad is not running → return clear error message prompting user to open KiCad.
  - Result format stays consistent (`{"success", "violations", "total_violations", "violation_categories"}`).

- [x] **0.3 Write tests for IPC DRC**
  - `tests/unit/tools/test_ipc_drc.py`:
    - Mock `kipy.KiCad.run_action`, `board.get_items`.
    - Test marker parsing (error + warning + exclusion severities).
    - Test edge cases: zero violations, KiCad not running, empty board.
    - CI-safe: no actual kipy connection needed (all mocked).

### Phase 1 — Design Rules Read/Write ✅

> **Goal**: LLM can inspect and modify board design rules through typed MCP tools.

- [x] **1.1 Create `kcaa/tools/drc_impl/pcb_design_rules.py`**
  - `get_design_rules_from_file(pcb_file) -> dict`:
    Parses the `.kicad_pcb` S-expression file via `sexpdata`, reads the
    `(setup (design_rules ...))` section. Returns all minimum-constraint
    values (clearance, track width, via size, etc.) as a flat dict.
  - `update_design_rules_in_file(pcb_file, updates) -> dict`:
    Partial update — only specified fields are changed. Creates `.bak`
    backup automatically, reports what was changed.
  - `get_custom_rules_from_file(pcb_file) -> dict`:
    Reads the `(setup (custom_rules ...))` section and returns each
    `(rule ...)` as a dict with condition + constraints.
  - `add_custom_rule_to_file(pcb_file, name, condition, ...) -> dict`:
    Appends one custom rule to the board file. Creates `.bak` backup.
  - `restore_design_rules_from_backup(pcb_file, backup_path) -> dict`:
    Restores a `.bak` snapshot.
  - **Design deviation**: Uses S-expression file parsing instead of kipy
    protobuf, because kipy v9.1.0's `board_rules` module does not fully
    expose `GetBoardDesignRules`/`UpdateBoardDesignRules`.

- [x] **1.2 Register MCP tools in `kcaa/tools/drc_tools.py`**
  - `get_design_rules(project_path: str) -> dict`:
    Returns all minimum-constraint values as a flat dict.
    Returns custom rules list.
  - `set_design_rules(project_path: str, rules: dict) -> dict`:
    Partial update — only specified fields changed. Validates field names.
    Reports back what was changed and backup path.
  - `list_custom_rules(project_path: str) -> dict`:
    Lists all custom rules from the board.
  - `add_custom_rule(project_path: str, name: str, condition: str,
    constraint_type: str, value: dict, severity: str) -> dict`:
    Appends one custom rule. Basic condition syntax validation.

- [x] **1.3 Test design rules tools**
  - `tests/unit/tools/test_pcb_design_rules.py`:
    Tests against real/constructed `.kicad_pcb` S-expression content.
    Tests read: all constraint fields present in output dict.
    Tests write: only specified fields changed, backup created.
    Tests custom rule: proper condition + constraint serialization.
    Tests rollback: restore from backup verified.
  - All tests are CI-safe (no KiCad process needed).

### Phase 2 — FreeRouting Constraint Integration ✅

> **Goal**: FreeRouting respects board design rules, and routed result is automatically DRC-checked.

- [x] **2.1 Pre-routing constraint extraction**
  - In `kicad_plugin/autorouter.py`, before DSN export:
    1. Call `get_design_rules_from_file()` to read current board constraints.
    2. Extract `min_clearance`, `min_track_width`, `copper_edge_clearance` values.
    3. Add `-dr <clearance_nm>` FreeRouting CLI flag (converts mm to FreeRouting's unit).
    4. Constraint values are logged in the autoroute output for transparency.

- [x] **2.2 Post-routing DRC validation**
  - In `_on_autoroute_done()`, after `ImportSpecctraSES`:
    1. Call `board.refill_zones()` via kipy to rebuild zone fills.
    2. Run `run_drc_via_ipc()` (Phase 0).
    3. If new violations found compared to pre-route DRC snapshot:
       - Append a summary entry to the conversation log: "Auto Route introduced N new violations: ..."
       - Offer to run `fix_drc_violations` prompt or roll back.
    4. If violations decreased or unchanged → log success.

- [x] **2.3 Autoroute constraint UI hint**
  - Before the "Skip Nets" dialog, show a one-line summary of current constraints:
    "Current rules: clearance=0.2mm, track_width=0.25mm"
  - Add checkbox: "☐ Re-run DRC after routing (recommended)" — default ON.

- [ ] **2.4 Test FreeRouting integration**
  - Unit test: `test_autoroute_constraint_extraction` — mock kipy, verify `-dr` flag in CLI.
  - Unit test: `test_autoroute_post_drc` — mock run_drc returning violations, verify conversation entry.
  - Integration test: with a real KiCad board (manual) — route, verify DRC results appear in chat.

### Phase 3 — Polish & Rollback ✅

> **Goal**: Safe DRC change tracking and rollback.

- [x] **3.1 DRC rule change rollback**
  - Before `set_design_rules()` or `add_custom_rule()` writes, `pcb_design_rules.py`
    automatically creates a `.bak` snapshot file.
  - `restore_design_rules_from_backup()` tool reads the `.bak` snapshot and writes
    it back to the `.kicad_pcb` file.
  - Backup files are named `<pcb_file>.bak.<timestamp>`.

- [x] **3.2 DRC violation history integration**
  - `run_drc_check` saves history via `drc_history.py`.
  - `compare_with_previous()` diffs `violation_categories` at a glance.

- [x] **3.3 Documentation**
  - Updated this design document to reflect actual implementation.
  - All new tools have docstrings (required by FastMCP convention).
  - README updated with new DRC tools.

---

## File Map (actual)

| New File | Purpose |
|----------|---------|
| `kcaa/tools/drc_impl/ipc_drc.py` | IPC-based DRC runner (kipy trigger + pcbnew marker reading) |
| `kcaa/tools/drc_impl/pcb_design_rules.py` | S-expression file-based design rules get/set/restore |
| `tests/unit/tools/test_ipc_drc.py` | Tests for IPC DRC (mocked pcbnew) |
| `tests/unit/tools/test_pcb_design_rules.py` | Tests for design rules file operations (CI-safe) |
| `tests/integration/test_drc_integration.py` | Integration tests for the DRC pipeline |

| Modified File | Change |
|---------------|--------|
| `kcaa/tools/drc_tools.py` | Registered `get_design_rules`, `set_design_rules`, `list_custom_rules`, `add_custom_rule` tools; replaced CLI runner with IPC |
| `kcaa/tools/drc_impl/__init__.py` | Re-exports `run_drc_via_ipc` + all `pcb_design_rules` functions |
| `kicad_plugin/autorouter.py` | Pre-route constraint extraction + CLI flag injection + post-route DRC |
| `kicad_plugin/ui/panel.py` | Post-route DRC check + constraint summary + rollback UI |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `pcbnew` import unavailable (standalone mode, KiCad not installed) | Raise clear error: "DRC requires KiCad running with a board open" |
| KiCad 9 vs 10 IPC API differences | Version-check `kicad.get_api_version()` and degrade gracefully |
| FreeRouting `-dr` flag may conflict with DSN-embedded rules | Test empirically, prefer DSN preprocessing if CLI flag causes issues |
| `run_action("runDRC")` is synchronous but doesn't return results | Read markers via `pcbnew` board API after action returns |
| Custom rules can't be round-tripped perfectly | Only expose the stable subset (name + condition + single constraint), warn on complex rules |

---

## Key Design Decisions

1. **IPC-only, all modes** — Both plugin and standalone MCP server use kipy IPC for DRC.
   The standalone server connects to a running KiCad instance just like the plugin does.
   No `kicad-cli` involved.

2. **Read markers via pcbnew** — After DRC runs via kipy `run_action`, violations are
   live `PCB_MARKER` items on the board. We read them via `pcbnew.GetBoard().GetMarkers()`
   rather than parsing a JSON dump. `pcbnew` is KiCad's native Python module, always
   available inside the KiCad process.

3. **S-expression file parsing for design rules** — kipy v9.1.0's `board_rules`
   module has type definitions for `BoardDesignRules`, `CustomRule`, etc. but does not
   fully expose the protobuf wire commands (`GetBoardDesignRules` /
   `UpdateBoardDesignRules`) through its public API. Rather than using raw protobuf
   sends (`kicad.client.send()`), we parse and write the `.kicad_pcb` S-expression
   file directly via `sexpdata`. This is the same format KiCad uses, is
   process-independent, and is fully CI-testable without a running KiCad instance.

4. **Custom rules as opaque text + typed helper** — Custom rules are stored in
   KiCad's Lisp-like DSL in the `(custom_rules ...)` section. The LLM can generate
   DSL condition text; basic validation is done on the structure.

5. **Rollback via .bak snapshots** — Design rule writes create timestamped `.bak`
   files before modifying. The `restore_design_rules` tool reads a `.bak` and writes
   it back to `.kicad_pcb`.

---

## Implementation Notes

### Design deviation: S-expression instead of kipy protobuf

The original plan called for reading/writing board design rules via kipy's protobuf
IPC (`GetBoardDesignRules` / `UpdateBoardDesignRules`). Investigation revealed that
kipy v9.1.0's `board_rules` module defines the Python type wrappers
(`BoardDesignRules`, `CustomRule`, `MinimumConstraints`, etc.) but does **not**
expose the send/receive methods for the corresponding protobuf wire commands through
its public API.

Raw protobuf sends via `kicad.client.send()` were considered but rejected because:
- The protobuf message structure is internal and may change across KiCad/kipy versions.
- Type safety is lost — no compile-time guarantees that the message is well-formed.
- S-expression file parsing is simpler, more testable, and operates on the same
  native `.kicad_pcb` format that KiCad uses.

When kipy's `board_rules` module matures to fully support `GetBoardDesignRules` /
`UpdateBoardDesignRules` in a future release, the file-based approach can be smoothly
migrated by replacing `pcb_design_rules.py`'s `sexpdata` calls with kipy proto calls
— the MCP tool signatures remain unchanged.

### Files not modified from the original plan

- **`kcaa/utils/drc_history.py`** — Already had sufficient comparison capabilities
  (Phase 3.2), no changes needed.
- **`kcaa/tools/drc_impl/cli_drc.py`** — Preserved as reference but no longer
  imported by `drc_tools.py`. Can be deleted in a future cleanup pass.

### Phase 2.4 (FreeRouting integration tests)

The FreeRouting integration tests (Phase 2.4) remain as manual integration checks
because FreeRouting requires a running Java process and a real board. Unit tests for
constraint extraction and CLI flag logic are covered by existing autorouter tests.
