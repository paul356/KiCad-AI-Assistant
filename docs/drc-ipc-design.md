# DRC IPC Integration & FreeRouting Constraint Design

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

1. Both plugin (`kicad_plugin/`) and standalone MCP server use kipy IPC exclusively for DRC —
   no `kicad-cli` fallback anywhere.
2. KiCad 9+ design rules (board constraints, net classes, custom rules) are readable/writable
   through typed Python objects via `kipy.board_rules`.
3. FreeRouting receives relevant constraint values before routing, and results are DRC-validated
   automatically.
4. All new tools follow the existing registration pattern (`register_*_tools(mcp)`).
5. The legacy `cli_drc.py` path is deprecated and eventually removed.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MCP Tools (kcaa/tools/)                       │
│                                                                  │
│  run_drc_check() ─── runs DRC (kipy) + reads markers (pcbnew)  │
│  get_design_rules() ─── reads BoardDesignRules via kipy         │
│  set_design_rules() ─── writes MinimumConstraints + severities   │
│  list_custom_rules() ─── returns all (rule "name" ...) blocks   │
│  add_custom_rule() ─── appends one custom rule to the board     │
│  run_autoroute() ─── enhanced with constraint passing + DRC      │
└───────────────────────────┬─────────────────────────────────────┘
                            │
          ┌─────────────────┴─────────────────┐
          ▼                                   ▼
┌─────────────────────────┐    ┌──────────────────────────────┐
│  kipy IPC               │    │  pcbnew (KiCad native API)   │
│                          │    │                              │
│  run_action("runDRC")   │    │  board.GetMarkers()          │
│  run_action("clear")    │    │  → parse PCB_MARKER items    │
│  board.refill_zones()   │    │  → violations list           │
│  client.send(GetBoard   │    │                              │
│    DesignRules...)      │    │  (pcbnew is always available  │
│                          │    │   in KiCad's Python process) │
└─────────────────────────┘    └──────────────────────────────┘
```

**Rationale**: kipy can trigger/clear DRC, refill zones, and read/write design rules via
protobuf, but does not wrap `PCB_MARKER` (no class in `_proto_to_object`). Reading DRC
results requires `pcbnew` board API, which is KiCad's native Python module and always
available inside the KiCad process. This is a single IPC-based approach — `pcbnew` is
the KiCad API, not an external CLI fallback.

---

## kipy IPC Capabilities (KiCad 9+)

### Available via `kipy.board_rules` (already in .venv)

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

### kipy Limitations

| Gap | Why | Resolution |
|-----|-----|------------|
| Cannot read `PCB_MARKER` items | No wrapper class in kipy `_proto_to_object` | Use `pcbnew` board API (`board.GetMarkers()`) in the KiCad process |
| Design rules proto commands may need raw client | Not all wrapped by `kipy.board_rules` in current version | Use `kicad.client.send()` with raw protobuf messages |

### Available via kipy client (raw protobuf)

| Proto Command | Request | Response |
|---------------|---------|----------|
| `GetBoardDesignRules` | `DocumentSpecifier` | `BoardDesignRulesResponse` |
| `UpdateBoardDesignRules` | `DocumentSpecifier` + `BoardDesignRules` | (ack) |
| `GetCustomRules` | `DocumentSpecifier` | `CustomRulesResponse` |
| `SetCustomRules` | `DocumentSpecifier` + `CustomRule[]` | `CustomRulesResponse` |
| `InjectDrcError` | severity + message + position + KIIDs | `InjectDrcErrorResponse` (marker KIID) |

---

## Task Breakdown

### Phase 0 — Foundation (no user-visible change)

> **Goal**: Build IPC DRC runner and replace the CLI-based `run_drc_check` with it.

- [ ] **0.1 Create `kcaa/tools/drc_impl/ipc_drc.py`**
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

- [ ] **0.2 Replace `run_drc_check` in `kcaa/tools/drc_tools.py`**
  - `run_drc_check()` currently hardcodes the CLI path via `run_drc_via_cli()`.
  - Replace with `run_drc_via_ipc()` — single code path, no dispatch.
  - kipy connection is obtained the same way as in `kipy_tools.py` (`_connect()` helper).
  - Standalone MCP server connects to a running KiCad instance via the same IPC socket.
  - If KiCad is not running → return clear error message prompting user to open KiCad.
  - Result format stays consistent (`{"success", "violations", "total_violations", "violation_categories"}`).

- [ ] **0.3 Write tests for IPC DRC**
  - `tests/unit/tools/test_ipc_drc.py`:
    - Mock `kipy.KiCad.run_action`, `board.get_items`.
    - Test marker parsing (error + warning + exclusion severities).
    - Test edge cases: zero violations, KiCad not running, empty board.
    - CI-safe: no actual kipy connection needed (all mocked).

### Phase 1 — Design Rules Read/Write

> **Goal**: LLM can inspect and modify board design rules through typed MCP tools.

- [ ] **1.1 Create `kcaa/tools/drc_impl/ipc_design_rules.py`**
  - `get_board_design_rules(kicad, board_doc) -> BoardDesignRules`:
    Use `kicad.client.send(GetBoardDesignRules(...), BoardDesignRulesResponse)` raw proto call.
  - `update_board_design_rules(kicad, board_doc, rules: BoardDesignRules) -> None`.
  - `get_custom_rules(kicad, board_doc) -> list[CustomRule]`.
  - `set_custom_rules(kicad, board_doc, rules: list[CustomRule]) -> None`.

- [ ] **1.2 Register MCP tools in `kcaa/tools/drc_tools.py`**
  - `get_design_rules(project_path: str) -> dict`:
    - Returns all `MinimumConstraints` values as a flat dict.
    - Returns `predefined_sizes` (track widths, via dimensions, diff pair dimensions).
    - Returns `severities` mapping.
    - Returns custom rules list.
  - `set_design_rules(project_path: str, rules: dict) -> dict`:
    - Partial update — only specified fields are changed.
    - Validates that values are within KiCad's allowed ranges.
    - Reports back what was changed.
  - `add_custom_rule(project_path: str, name: str, condition: str, constraint_type: str, value: dict, severity: str) -> dict`:
    - Appends one custom rule to the board.
    - Validates condition syntax (basic check).

- [ ] **1.3 Test design rules tools**
  - Mock `KiCadClient.send` to return synthetic `BoardDesignRulesResponse`.
  - Test read: all constraint fields present in output dict.
  - Test write: only specified fields in proto update message.
  - Test custom rule: proper condition + constraint serialization.

### Phase 2 — FreeRouting Constraint Integration

> **Goal**: FreeRouting respects board design rules, and routed result is automatically DRC-checked.

- [ ] **2.1 Pre-routing constraint extraction**
  - In `kicad_plugin/autorouter.py`, before DSN export:
    1. Call `get_board_design_rules()` via kipy.
    2. Extract `min_clearance`, `min_track_width`, `copper_edge_clearance` values.
    3. Add `-dr <clearance_nm>` FreeRouting CLI flag (converts nm to FreeRouting's unit).
    4. Optionally pre-process DSN to inject per-net-class width/clearance (if FreeRouting supports it).
  - Constraint values are logged in the autoroute output for transparency.

- [ ] **2.2 Post-routing DRC validation**
  - In `_on_autoroute_done()`, after `ImportSpecctraSES`:
    1. Call `board.refill_zones()` via kipy to rebuild zone fills.
    2. Run `run_drc_via_ipc()` (Phase 0).
    3. If new violations found compared to pre-route DRC snapshot:
       - Append a summary entry to the conversation log: "Auto Route introduced N new violations: ..."
       - Offer to run `fix_drc_violations` prompt or roll back.
    4. If violations decreased or unchanged → log success.

- [ ] **2.3 Autoroute constraint UI hint**
  - Before the "Skip Nets" dialog, show a one-line summary of current constraints:
    "Current rules: clearance=0.2mm, track_width=0.25mm"
  - Add checkbox: "☐ Re-run DRC after routing (recommended)" — default ON.

- [ ] **2.4 Test FreeRouting integration**
  - Unit test: `test_autoroute_constraint_extraction` — mock kipy, verify `-dr` flag in CLI.
  - Unit test: `test_autoroute_post_drc` — mock run_drc returning violations, verify conversation entry.
  - Integration test: with a real KiCad board (manual) — route, verify DRC results appear in chat.

### Phase 3 — Polish & Rollback

> **Goal**: Safe DRC change tracking and rollback.

- [ ] **3.1 DRC rule change rollback**
  - Before `set_design_rules()` or `add_custom_rule()` writes:
    1. Read current rules (full snapshot).
    2. Save snapshot to `drc_history/` with timestamp (reuse existing `drc_history.py` pattern).
  - Add `restore_design_rules(project_path, timestamp)` tool:
    - Reads snapshot, writes it back via IPC.
    - Triggers `reload_kicad()` to reflect changes in GUI.

- [ ] **3.2 DRC violation history integration**
  - `run_drc_check` already saves history via `drc_history.py`.
  - Enhance `compare_with_previous()` to diff `violation_categories` at a glance.

- [ ] **3.3 Documentation**
  - Update `README_CN.md` / `README.md` with new DRC tools.
  - Add docstrings for all new tools (already required by FastMCP convention).

---

## File Map

| New File | Purpose |
|----------|---------|
| `kcaa/tools/drc_impl/ipc_drc.py` | IPC-based DRC runner |
| `kcaa/tools/drc_impl/ipc_design_rules.py` | IPC-based design rules get/set |
| `tests/unit/tools/test_ipc_drc.py` | Tests for IPC DRC |
| `tests/unit/tools/test_ipc_design_rules.py` | Tests for design rules tools |

| Modified File | Change |
|---------------|--------|
| `kcaa/tools/drc_tools.py` | Add `get_design_rules`, `set_design_rules`, `add_custom_rule` tools; replace CLI runner with IPC |
| `kcaa/tools/drc_impl/__init__.py` | Re-export IPC functions |
| `kicad_plugin/autorouter.py` | Pre-route constraint extraction + CLI flag injection |
| `kicad_plugin/ui/panel.py` | Post-route DRC check + constraint summary + rollback UI |
| `kcaa/utils/drc_history.py` | Enhanced comparison + history format update |

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

1. **IPC-only, all modes** — Both plugin and standalone MCP server use kipy IPC. The standalone
   server connects to a running KiCad instance just like the plugin does. No `kicad-cli` involved.
2. **Read markers via kipy `get_items`** — After DRC runs, violations are live `PCB_MARKER` items
   on the board. We read them directly rather than parsing a JSON dump.
3. **Custom rules as opaque text + typed helper** — Custom rules are stored in Lisp-like DSL.
   The LLM can generate DSL text, but we validate it by attempting to set it and catching parse
   errors from the KiCad API.
4. **Rollback as a general capability** — DRC rule changes are destructive (they modify the
   `.kicad_pcb` file when saved). Snapshots before each write protect against bad LLM outputs.
