# VLM Visual-Feedback-Driven Routing — Minimal Closed-Loop Experiment

## Status: implemented (plan A, issue #124)

- Plan A confirmed and shipped: `scripts/vlm_route_feedback.py` + unit tests, no existing code changed.
- Verified: dry-run smoke test connected 2/2 pairs (VCC single-layer, GND F.Cu→B.Cu→In1.Cu with via); `tests/unit/scripts/test_vlm_route_feedback.py` 18/18 passed.

## Background and goal

Verify that the loop "VLM sees image → semantic feedback → existing router executes → rendered feedback" is feasible — the **prerequisite minimal experiment** for route B (VLM planning + PNS precise execution).

Core question: can VLM feedback on a routing render make the existing A\* router's results better? The guidance dimensions are limited to the knobs `RouteRequest` currently exposes:

- Routing order (which pad pair to connect first)
- Layer selection (`layer_hint`)
- Strategy change after failure (`RouteFailure` reason fed back → retry with different parameters)

**Explicitly out of scope** (future issues): waypoints, candidate-comparison renders, PNS/sidecar engine.

## Current asset inventory (reusable)

| Component | Location | Status |
|---|---|---|
| Routing core | `kcaa/router/router.py::auto_route_pair` | ✅ Grid A\* + 45° postprocessing, no shove; blocks raise `RouteFailure` |
| Pipeline visualization | `_dump_viz` (`KCAA_DUMP_ROUTE_PIPELINE=1`) | ✅ Stage-by-stage JSON |
| JSON→PNG | `scripts/render_viz.py` (matplotlib+shapely) | ✅ |
| Multimodal LLM | `kicad_plugin/llm_client.py::_build_user_content` | ✅ base64 images |
| Whole-board render | `kcaa/tools/export_tools.py::generate_pcb_thumbnail` | ✅ kicad-cli SVG (no pad labels; not suited for the feedback loop) |
| Test board | `tests/integration/fixtures/test_routing_board.kicad_pcb` (3 nets) + `.kicad_pro` | ✅ |

**Gaps** (to fill):

1. Whole-board "current state" render: pad labels + pending-net highlight + routed segments, as the VLM input source
2. Driver script: render → ask VLM → parse semantic feedback → map to `RouteRequest` → route → save → render again
3. Evaluation metrics: connected/attempted pairs, rounds per pair, post-failure recovery success rate

## Loop design

```
┌──────────────┐ board-state render(pad labels)  ┌──────────────┐
│ render script │ ──────────────────────────────▶ │ VLM          │
│              │                                  │ (system hint │
└──────────────┘                                  │  + image)    │
                                                  └──────┬───────┘
                                                         │ semantic feedback: connect X.Y-A.B
                                                         │ layer Z / change strategy
                                                         ▼
                                                ┌────────────────┐
                                                │ parser → RouteRequest │
                                                └──────┬─────────┘
                                                       ▼
                                                auto_route_pair
                                                ├─ success → pcb_route_pad_to_pad save → render new state → next round
                                                └─ failure → failure viz + RouteFailure feedback → retry (cap N)
```

**VLM output protocol (experiment)**:

```
route: <ref_a>.<pad_a> -> <ref_b>.<pad_b>
layer: <F.Cu|B.Cu|auto>
reason: one-line rationale
```

On failure feedback, append:

```
last_error: <RouteFailure message>
advice: <change layer/order/abandon pair>
```

## Candidate plans

### Plan A: standalone experiment script (recommended)

Add `scripts/vlm_route_feedback.py`, **change no existing code**.

- Whole-board render: script draws with sexpdata + matplotlib (or reuses render_viz.py drawing helpers)
- Reuse: `auto_route_pair`, `load_pcb/save_pcb`, `llm_client._build_user_content`
- Tests: `tests/unit/router/` or a script-level smoke test (render one image, confirm manually) — experiment scripts are not forced to have unit tests

Pros: zero intrusion, fast loop closure, disposable; cons: some render logic overlaps render_viz.

### Plan B: MCP tool integration

Add `kcaa/tools/vlm_route_feedback_tools.py`, register as fastmcp tools, drive through an MCP session.

Pros: becomes a product capability usable from the plugin UI; cons: productizing an unvalidated experiment risks rework; session-state management is complex.

### Plan C: manual MCP driving (zero development)

Use existing tools directly: `generate_pcb_thumbnail` + `pcb_route_pad_to_pad` in a manual loop.

Pros: zero code, try it today; cons: thumbnail has no pad labels, no failure feedback, no automated evaluation — only for "getting a feel".

**Preference: Plan A.** Because the goal is to prove/disprove the feedback loop, not deliver a product; zero intrusion makes it fully disposable; evaluation is scriptable.

## Experiment design

- **Experiment 1 (this one)**: feedback dimensions limited to layer choice + routing order + post-failure strategy change. 3-net board, compare "VLM sees image and decides" vs "fixed order blind connect" connection rates.
- **Experiment 2 (future issue)**: if the VLM frequently wants to express "go around this area" but current parameters cannot — that empirically proves the need for waypoints, designed separately.

## Acceptance criteria (issue #124)

1. Script can render a whole-board state image (pad labels + pending nets + routed segments)
2. VLM sees the image and outputs semantic feedback; parser maps it to `RouteRequest` parameters
3. Success → save + render new state; failure → failure viz + reason fed back to VLM for strategy change
4. Output evaluation metrics: connected/attempted pairs, rounds per pair, post-failure recovery success rate

## Direction calibration (2026-09-13): the script validates; the product target is VLM-guided routing inside the plugin

`scripts/vlm_route_feedback.py` is a **validation stepping stone**: it proves the loop "VLM sees image → semantic feedback → existing router executes → rendered feedback" is feasible. The real goal is **inside kicad_plugin**: the user says "connect X and Y for me" → the VLM agentic loop completes perceive/decide/execute/retry-with-new-strategy, without depending on the experiment script.

### Product loop (plugin side, reuses the existing tool surface, zero new backend tools)

```
User: connect J2.3 and U4.52 for me
  │
  ▼
LLMClient.run() agentic loop (llm_client.py, 20 tool iterations, multimodal images supported)
  │
  ├─ get_ratsnest             # real unrouted pad pairs + world coords (data, not VLM guesswork)
  ├─ export_pcb_layer_image   # composite/single-layer render, white dashed ratsnest marks pairs
  ├─ pcb_route_pad_to_pad     # layer_hint / via_pairs / turn_penalty
  └─ failure → read error text → look at image → change layer/strategy, retry (no silent retry)
```

- **Perception/decision split**: candidate pairs always come from `get_ratsnest` (data source); the VLM only makes spatial decisions (which pair first, which layer, what strategy after failure). The white ratsnest dashed lines are image anchor indices, one-to-one with the data — the VLM is neither asked to discover candidates from the image nor to read pad text.
- **`_PROMPT_PCB` (llm_client.py:794, fixed constant) needs a "Routing workflow" section**: specify the execution order get_ratsnest → render and look → pcb_route_pad_to_pad → retry on failure. Otherwise the VLM has the tools but not the flow.
- **Consistent semantics**: the script's `routable_pairs()` is all same-net pad pairs (including already-connected ones), inconsistent with `get_ratsnest` (true unrouted pairs). If the script continues to be used, align it to the true unrouted-pair semantics.

### Workflow skill-ification + skill lookup improvements (considering)

- The connect workflow should be extracted into a standalone skill (e.g. `pcb-routing`), **leaving `_PROMPT_PCB` itself untouched**: keep the system prompt lean, lazy-load on demand via `get_skill` (reusing the skill-system-design.md Layer-2 mechanism; the skill catalog is auto-generated, adding a skill is just dropping a `.md` file).
- **Skill lookup improvement**: current `_find_skill_file` (kcaa/tools/skill_tools.py) is **exact-name matching** `candidate == name` — no aliases/near-synonyms/case normalization, so a slight LLM naming deviation yields not-found and falls back to the full list. Candidate improvements (by cost):
  1. **Normalization matching**: lowercase + strip hyphens/underscores before comparing;
  2. **Alias table**: add `aliases:` to front-matter (`pcb-routing` ← route/connect/ratsnest), read by both catalog and lookup;
  3. **Catalog trigger words**: attach typical trigger phrases to `list_skills` descriptions to reduce misnames;
  4. **Top-k suggestions on miss**: suggest candidates by substring/word overlap instead of failing outright.
- Role split: the script is for "tunable research"; the skill carries "product flow orchestration".

## Files touched

- New: `scripts/vlm_route_feedback.py`
- New: this planning doc (docs/plans/vlm-feedback-routing.md)
- Unchanged: router / llm_client / tools existing code