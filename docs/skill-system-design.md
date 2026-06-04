# Skill System Design

## Background

The current system prompt (`build_system_prompt`) is fully static: ~2000 tokens are sent with
every request regardless of what the user is actually asking. As KiCad tools and workflows grow,
this approach is not scalable.

The skill system introduces **on-demand workflow guidance**: the system prompt retains only
universal rules, while specialised workflow sections live as independently-loadable **skills**.

---

## Design Goals

1. Reduce per-request token cost without sacrificing LLM accuracy.
2. Make workflow guidance independently maintainable (one file per skill).
3. Keep the injection mechanism simple — no extra infrastructure (no vector DB, no embedding
   model) in the first phase.
4. Leave the interface open for a future RAG-based retrieval backend.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  System Prompt (Layer 1 — always injected, ~600 tokens)  │
│                                                          │
│  _PROMPT_HEADER       — framework safety rules           │
│  Schematic section    — coordinate system + hard rules   │
│  PCB section          — coordinate system + hard rules   │
│  context_block        — active files, selected refs      │
│  Skill catalog        — names + one-line descriptions    │
└──────────────────────────────────────────────────────────┘
                           │
          LLM reads user message, decides if a skill is needed
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│  get_skill(name) — MCP tool (Layer 2 — lazy loaded)      │
│                                                          │
│  Returns detailed workflow prompt block for one skill.   │
│  Result lives in message history → stays in context for  │
│  the remainder of the session without re-fetching.       │
└──────────────────────────────────────────────────────────┘
                           │
              kicad_plugin/skills/<name>.md
```

---

## Dynamic Context Techniques Considered

| Technique | Description | Status |
|---|---|---|
| **Tool-based lazy loading** | `get_skill(name)` tool; LLM requests what it needs | **Selected (Phase 1)** |
| Semantic retrieval (RAG) | Embed queries, topK skill retrieval via cosine similarity | Future — when skills > ~20 |
| Dependency graph injection | Skill declares `requires`; loading A auto-fetches B | Future — complex workflows |
| History-aware re-injection | Re-inject skills referenced before history compaction | Future — compaction fix |
| MemGPT paged memory | Explicit page-in/out, external store for large knowledge | Long-term |

The `get_skill` tool interface is designed to be backend-agnostic: the lookup logic can be
replaced with RAG retrieval later without changing the LLM-facing API.

---

## Why Not Context-Based or Keyword-Based Triggering?

Context state (e.g. `active_schematic`) is unreliable — the user may be editing a schematic
through the plugin even when no file is reported as open. Keyword matching is brittle for
natural-language requests. Both approaches require pre-request heuristics that add maintenance
burden.

The **LLM-as-router** approach (Layer 2) delegates intent detection to the main LLM itself,
which already reads the full user message. This is more accurate, requires no separate model,
and adds only one tool-call round trip at the start of a new task type — acceptable because
skills are fetched once and persist in context.

---

## System Prompt Restructuring

### Before (static, ~2000 tokens)

```
_PROMPT_HEADER          ~200 tokens
_PROMPT_SCHEMATIC       ~1100 tokens  (coord + geometry APIs + placement workflow +
                                       wiring strategy + spacing rules + hard rules)
_PROMPT_PCB             ~900 tokens   (coord + query workflow + placement workflow +
                                       group ops + outline workflow + lib workflow + hard rules)
context_block           ~100 tokens
─────────────────────────────────────
Total                  ~2300 tokens
```

### After (Layer 1 only, ~600 tokens)

```
_PROMPT_HEADER          ~200 tokens   (unchanged)
_PROMPT_SCHEMATIC       ~250 tokens   (coord system + hard rules only)
_PROMPT_PCB             ~200 tokens   (coord system + hard rules only)
context_block           ~100 tokens   (unchanged)
skill_catalog           ~60 tokens    (NEW: names + one-line descriptions)
─────────────────────────────────────
Total                   ~810 tokens   (−65%)
```

Skills are fetched on demand; a typical session requesting one or two skills adds 200–400
tokens to message history, keeping total context well below the budget.

---

## Skill Catalog

Six initial skills extracted from the existing system prompt (no new content invented):

| Skill name | Extracted from | Content |
|---|---|---|
| `schematic-placement` | `_PROMPT_SCHEMATIC` | Geometry APIs, `find_free_area` workflow, spacing rules |
| `schematic-wiring` | `_PROMPT_SCHEMATIC` | Wire routing strategy, pin selection, junction handling |
| `pcb-query` | `_PROMPT_PCB` | `get_board_info` / `list_footprints` / `get_ratsnest` workflow |
| `pcb-placement` | `_PROMPT_PCB` | Footprint positioning, overlap checks, group align/distribute |
| `pcb-outline` | `_PROMPT_PCB` | `Edge.Cuts` creation and editing workflow |
| `pcb-footprint-library` | `_PROMPT_PCB` | Footprint index, search, and inspection workflow |

Skill files live in `kicad_plugin/skills/<name>.md`.

---

## `get_skill` Tool Specification

```
Tool name:   get_skill
Kind:        readonly
path_arg:    (none)
auto_snapshot: false
mark_dirty:  false

Arguments:
  name (str, required) — skill name from the catalog

Returns:
  Markdown string with the full skill prompt block, or an error message
  listing available skills if the name is not recognised.
```

The tool is registered in `tool_registry.py` with the same policy structure as other
read-only tools. It is implemented in a new file `kicad_plugin/skill_tools.py` (or
inline in `llm_client.py` — TBD).

---

## Prompt Composition Technique

The current implementation uses plain string concatenation, which is hard to compose,
test, or extend. The skill system introduces a **component-based builder pattern** inspired
by JSX/TSX: each prompt section is a self-contained unit, and a `PromptBuilder` assembles
them declaratively.

### Skill Files — Markdown with YAML Front Matter

Each skill lives in `kicad_plugin/skills/<name>.md`:

```markdown
---
name: schematic-placement
priority: 50
description: "Symbol placement workflow, find_free_area, bbox geometry, spacing rules"
---
# Recommended Placement Workflow
...full prompt content...
```

The front matter carries metadata (name, priority, description for the catalog).
The Markdown body is the raw text injected into the prompt.

### PromptBuilder

```python
class PromptBuilder:
    def layer1(self, *sections: str) -> "PromptBuilder": ...
    def skills(self, fetched: list[Skill]) -> "PromptBuilder": ...
    def build(self) -> str: ...
```

Usage in `build_system_prompt`:

```python
def build_system_prompt(context_block: str, fetched_skills: list[Skill] = ()) -> str:
    return (
        PromptBuilder()
        .layer1(_PROMPT_HEADER, _PROMPT_SCHEMATIC_CORE, _PROMPT_PCB_CORE, context_block)
        .skills(fetched_skills)          # zero or more, ordered by priority
        .build()
    )
```

### Benefits over string concatenation

| Concern | Before | After |
|---|---|---|
| Adding a new section | Edit the concatenation expression | Add a `.md` file |
| Conditional inclusion | Manual `if` around string append | `builder.add_if(cond, section)` |
| Section ordering | Implicit in code order | `priority` field in front matter |
| Unit testing | Compare giant strings | Test each section independently |
| Skill catalog | Hard-coded separately | Auto-generated from front matter |

### Skill loading

```python
# skill_loader.py
def load_skill(name: str) -> Skill:
    path = SKILLS_DIR / f"{name}.md"
    front_matter, body = parse_markdown_with_front_matter(path)
    return Skill(name=front_matter["name"],
                 description=front_matter["description"],
                 priority=front_matter.get("priority", 50),
                 content=body)

def skill_catalog() -> str:
    """Return the skill catalog block for Layer 1 system prompt."""
    skills = [load_skill(p.stem) for p in sorted(SKILLS_DIR.glob("*.md"))]
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return "# Available Skills\nCall get_skill(name) for detailed workflow guidance:\n" + "\n".join(lines)
```

The catalog block is **auto-generated** from the front matter of all `.md` files — adding a
new skill only requires dropping a new file into `kicad_plugin/skills/`.

---

## Future: Semantic Retrieval Backend

When the skill library grows beyond ~20 entries, the `get_skill` lookup can be replaced with:

```python
def get_skill(query: str) -> str:
    """Return the most relevant skill(s) for the given query."""
    hits = skill_index.search(query, top_k=2)
    return "\n\n".join(s.content for s in hits)
```

The LLM-facing tool signature stays identical; only the backend changes.

---

## Implementation Tasks

Legend: `[ ]` = pending · `[~]` = in progress · `[x]` = done

### Phase 1 — Infrastructure (`skill-infra`)

- [x] Create `kcaa/skills/` directory
- [x] ~Skill dataclass in plugin~ — skills served by MCP server only
  - fields: `name`, `description`, `priority`, `content`
- [x] Implement `_parse_front_matter` helper in `kcaa/tools/skill_tools.py`
- [x] ~`load_skill` in plugin~ — skill loading happens server-side
- [x] Implement `list_skills()` MCP tool in `kcaa/tools/skill_tools.py`
- [x] Skill catalog returned by `list_skills()` MCP tool (auto-generated from front matter)
- [ ] ~PromptBuilder~ — deferred to Phase 3 (needed when prompt refactor happens)



- [x] Implement `get_skill(name)` MCP tool in `kcaa/tools/skill_tools.py`
  - Returns skill body on success
  - Returns helpful error + catalog listing on unknown name
- [x] Register `list_skills` + `get_skill` in `kicad_plugin/tool_registry.py`
  - `kind=query`, no `path_arg`

### Phase 2 — Skill Extraction (depends on Phase 1)

Each task: create the `.md` file with YAML front matter, copy the relevant prompt
section as the body, verify content matches source exactly.

- [ ] `kicad_plugin/skills/schematic_placement.md`
  - Source: `_PROMPT_SCHEMATIC` — "Geometry you get for free" + "Recommended placement
    workflow" + "Spacing & layout rules"
- [ ] `kicad_plugin/skills/schematic_wiring.md`
  - Source: `_PROMPT_SCHEMATIC` — "Wiring strategy" section
- [ ] `kicad_plugin/skills/pcb_query.md`
  - Source: `_PROMPT_PCB` — "PCB query workflow" section
- [ ] `kicad_plugin/skills/pcb_placement.md`
  - Source: `_PROMPT_PCB` — "PCB placement workflow" + "PCB group operations"
- [ ] `kicad_plugin/skills/pcb_outline.md`
  - Source: `_PROMPT_PCB` — "Board outline workflow" section
- [ ] `kicad_plugin/skills/pcb_footprint_library.md`
  - Source: `_PROMPT_PCB` — "Footprint library workflow" section

### Phase 3 — Prompt Refactor (depends on Phase 2)

- [ ] Trim `_PROMPT_SCHEMATIC` in `llm_client.py` — keep coordinate system + hard rules only
- [ ] Trim `_PROMPT_PCB` in `llm_client.py` — keep coordinate system + hard rules only
- [ ] Replace string concatenation in `build_system_prompt()` with `PromptBuilder`
- [ ] Add `skill_catalog()` block at end of Layer 1 prompt
- [ ] Verify Layer 1 token count ≤ 800 tokens

### Phase 4 — Tests (depends on Phase 1 & Phase 3)

- [ ] Unit tests for `PromptBuilder` (section ordering, empty skills list, priority sort)
- [ ] Unit tests for `skill_loader` (load by name, unknown name error, catalog generation)
- [ ] Integration test: `get_skill` returns correct content for each registered skill
- [ ] Integration test: `get_skill` with unknown name returns error + available skill list
- [ ] Prompt size regression test: confirm ≥ 60% reduction vs baseline

### Dependency Order

```
Phase 1 (infra)
  └─ Phase 2 (skill extraction) ──┐
                                  ├─ Phase 3 (prompt refactor)
                                  │       └─ Phase 4 tests (prompt size)
  └─ Phase 4 tests (get_skill)────┘
```
