# KiCad Plugin UX Design

## 1. Overview

The KiCad plugin provides an engineer-facing chat and action interface for an LLM that edits KiCad schematics through MCP tools. The plugin is responsible for gathering context, displaying LLM activity, and giving the engineer control over what changes are applied.

---

## 2. Panel Layout

The plugin opens as a KiCad action panel (docked or floating) with three zones:

```
┌──────────────────────────────────────────────────────────────┐
│  🔧 KiCad AI Assistant                          [⚙] [✕]      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  CONVERSATION                                                │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄    │
│  [User] Add a 100nF bypass capacitor near U1 pin 4           │
│                                                              │
│  [AI] Reading schematic…                                     │
│    ↳ extract_schematic_netlist  ✓ 32 nets                    │
│    ↳ search_symbols  ✓ "C" → Device:C                        │
│    ↳ add_symbol_to_schematic  ✓ C3 placed at (120, 95)       │
│    ↳ connect_pins_with_wire  ✓ C3.1 → U1.4                   │
│    ↳ add_wire_to_schematic   ✓ C3.2 → GND net               │
│  I added C3 (100nF, Device:C) connected to U1 pin 4 and GND. │
│  Please review the change in the schematic editor.           │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  TOOL LOG  [collapse ▼]                                      │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄    │
│  14:02:31 extract_schematic_netlist → 32 nets, 8 components  │
│  14:02:32 add_symbol_to_schematic → C3 at (120, 95)          │
│  14:02:33 connect_pins_with_wire → C3.1–U1.4                 │
├──────────────────────────────────────────────────────────────┤
│  [Reload Schematic]         [Ask something…]  [Send ↩]       │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Conversation Flow

### 3.1 Engineer enters a request
- Free-text input field at the bottom of the panel
- Enter or Send button submits
- Previous conversation is preserved in the panel scroll

### 3.2 Context injection
Before sending to the LLM, the plugin silently prepends:
- Active project path
- Active schematic file path
- Active editor type (schematic / PCB)
- Currently selected component references (if any)

The engineer does not see this context payload; it is added automatically.

### 3.3 LLM response with tool calls
- Each tool call is shown in the conversation area as `↳ <tool_name>  ⏳ in progress…`
- When the tool returns, the indicator updates to `✓ <summary>` or `✗ <error>`
- Tool results are not shown in full to reduce clutter; the tool log (below) has full detail
- Multiple sequential tool calls are shown as a indented list under the initiating AI turn

### 3.4 Final LLM message
- Displayed as a regular chat message
- If one or more files were mutated, a notice appears: `⚠ Schematic file modified. [Reload Schematic]`

### 3.5 Reload notification
- After any successful mutation, the plugin shows a "Reload Schematic" button
- The engineer clicks it to trigger a KiCad file reload
- The plugin does **not** auto-reload without explicit confirmation because KiCad may have unsaved in-memory changes

---

## 4. Tool Log

- A collapsible section below the conversation area
- Shows: timestamp, tool name, key return fields, elapsed time
- On error: full error message in red
- "Clear log" button resets without affecting conversation history

---

## 5. Edit Approval Model

### Policy: immediate apply, manual reload

Tool mutations are **applied to disk immediately** when the LLM calls the tool. There is no staged approval gate before the file write. However:

- The file is written to disk but **not reloaded into KiCad** until the engineer clicks "Reload Schematic"
- A `.bak` backup is written before every mutation
- The engineer can discard the change by closing the panel and restoring from `.bak`

### Rationale
A pre-apply approval gate would require the plugin to preview a diff before writing, which is complex for KiCad's S-expression format and not required for the initial milestone. The `.bak` + manual reload pattern gives the engineer an implicit rejection path.

### Future: optional approval gate
A per-tool approve/reject mode can be added in a later milestone. In that mode, the plugin would hold mutations in memory and only write when explicitly confirmed.

---

## 6. Error Handling

| Situation | Plugin Behaviour |
|-----------|-----------------|
| Tool returns `{"success": false}` | Error shown in tool call indicator; LLM receives the error and can retry or report |
| MCP server not responding | Yellow warning banner: "Backend unavailable. [Restart]" |
| LLM API error | Red notice in chat: "LLM request failed: `<reason>`" |
| Schematic parse failure | Error shown in tool log; file is not written; backup is not needed |
| Network timeout (LLM) | Chat shows spinner timeout; engineer can retry |

---

## 7. Settings Panel

Accessed via the ⚙ icon. Exposes:
- LLM provider selector (OpenAI / Anthropic / Custom endpoint)
- API key input (stored securely in KiCad plugin settings)
- Model selector
- Show/hide tool log by default
- Server log path (read-only display)

---

## 8. First-Run Experience

On first open:
1. Panel shows a welcome message explaining the assistant
2. If no API key is configured, settings panel opens automatically
3. If MCP backend is not running, a one-time start prompt is shown
4. A brief example request is shown: "Try: 'List all components in the active schematic'"

---

## 9. Accessibility and Keyboard

- Chat input is focusable with Tab
- Enter sends; Shift+Enter inserts newline
- Conversation history is scrollable with arrow keys
- All interactive elements have labels for screen readers

---

## 10. Out of Scope for Milestone 1

- Diff/preview view of pending edits before they are written
- Undo/redo integration with KiCad's native undo stack
- Multi-turn context window management (conversation is not pruned)
- Streaming LLM responses (full response shown when complete)
- PCB editor interaction
