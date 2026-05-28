# KiCad MCP Plugin — Architecture Design

## 1. Overview

**KiCad Plugin** is a Python panel bundled into KiCad that provides the engineer-facing chat UI.
It owns the LLM client lifecycle, collects live project context via KiCad's scripting API, and
spawns the MCP server subprocess. All user interaction flows through it.

**LLM Client** runs embedded in the plugin process and speaks to an external LLM API (OpenAI,
Anthropic, or a compatible endpoint). It drives the agentic loop: it sends tool definitions along
with conversation history, receives tool-call requests, forwards them to the MCP server, and feeds
results back until the model produces a final natural-language reply.

**MCP Server** is a separate Python subprocess running the existing FastMCP server
(`kcaa/server.py`) in `streamable-http` mode. It exposes the schematic-editing, netlist, and
symbol tools exclusively; there is no `kicad-cli` dependency inside the server. Every mutation is
backed up to a `.bak` file before being written. The server has no direct knowledge of KiCad's
GUI.

**KiCad Files** are the artifacts the server reads and writes: `.kicad_sch` schematics,
`.kicad_pro` project descriptors, and `.bak` backup snapshots. They live on the local filesystem
inside the active project directory.

---

## 2. Component Roles

### KiCad Plugin (Python, runs inside KiCad)
- Renders the chat panel, tool-call log, and approve/reject controls inside a `wx`-based panel.
- Reads context from KiCad's scripting API on each request: active project path, active schematic
  path, active editor type (`SCH_EDIT_FRAME` vs `PCB_EDIT_FRAME`), and current selection.
- Spawns the MCP server subprocess and monitors it via a background thread.
- After each LLM turn, calls `SCH_EDIT_FRAME.LoadSchematic()` (or equivalent) to reload any files
  the server modified.
- Stores configuration in the KiCad plugin settings file (not `.env`).

### LLM Client (embedded in plugin process)
- Authenticates to the configured LLM provider using `KICAD_MCP_LLM_API_KEY`.
- Builds a request containing: system prompt, serialised KiCad context, full conversation history,
  and MCP tool definitions (fetched once at startup from the server's `/tools` endpoint).
- Executes the agentic loop (see §4); reports each tool invocation to the plugin UI before
  executing it.
- Handles provider-specific response formats; exposes a single internal `run_turn()` interface.

### MCP Server (separate Python subprocess)
- Launched with `MCP_TRANSPORT=streamable-http` and `MCP_PORT=<port>`.
- Listens on `127.0.0.1` only; exposes milestone-1 tools: skip-based schematic editing, netlist
  generation, and symbol lookup.
- Uses the `skip` library for all file mutations; never shells out to `kicad-cli`.
- Writes `<file>.bak` before every mutation.
- Exposes `GET /health` for startup polling.

### KiCad Files
- `.kicad_sch` — read and written by the schematic editing tools.
- `.kicad_pro` — read for project discovery; never mutated.
- `.bak` — created by the server before every write; not managed by the plugin.

---

## 3. Transport and Process Lifecycle

**Transport: streamable-http on localhost**

`stdio` transport is avoided because KiCad's bundled Python has restrictions around
subprocess stdin/stdout piping that make reliable bidirectional communication fragile. SSE is
asymmetric and more complex to drive from a client. Streamable-HTTP over localhost gives clean
request/response semantics with no piping issues.

**Port selection**

The plugin selects a free port at startup:

```python
import socket
with socket.socket() as s:
    s.bind(('', 0))
    port = s.getsockname()[1]
```

`KICAD_MCP_SERVER_PORT` overrides this if set.

**Startup sequence**

```
plugin start
  → pick free port
  → subprocess.Popen(
        [sys.executable, '-m', 'kcaa.server'],
        env={..., 'MCP_TRANSPORT': 'streamable-http', 'MCP_PORT': str(port)}
    )
  → poll GET http://127.0.0.1:<port>/health  (100 ms interval, 10 s timeout)
  → fetch tool definitions from /tools
  → mark server ready
```

**Shutdown:** plugin sends `SIGTERM` to the subprocess when the panel is unloaded or KiCad exits.

**Crash recovery:** if the health-check thread detects the process has exited unexpectedly, the
plugin surfaces an error banner with a **Restart** button that re-runs the startup sequence.

---

## 4. Agentic Loop

1. Engineer submits a request in the chat panel.
2. Plugin reads current KiCad context (project path, open schematic, selection).
3. Plugin calls `LLMClient.run_turn(context, history, tools)`.
4. Client sends the full message to the LLM API.
5. If the response contains tool calls:
   - Plugin logs `"calling <tool_name>…"` in the tool-call log.
   - Client POSTs to `http://127.0.0.1:<port>/call/<tool_name>` with the tool arguments.
   - Result is appended to the conversation as a `tool` message.
   - Go to step 4.
6. When the LLM returns a message with no tool calls, the loop ends.
7. Plugin renders the final message in the chat panel.
8. Plugin inspects tool-call results for `modified_files`; calls KiCad's reload API for each.

---

## 5. Configuration

Stored in the KiCad plugin settings file (`metadata.json` or plugin-specific JSON in the KiCad
user config directory). **Not** in `.env`.

| Variable | Description |
|---|---|
| `KICAD_MCP_LLM_PROVIDER` | `"openai"` \| `"anthropic"` \| `"custom"` |
| `KICAD_MCP_LLM_API_KEY` | API key for the chosen provider |
| `KICAD_MCP_LLM_MODEL` | Model name, e.g. `"gpt-4o"` or `"claude-opus-4-5"` |
| `KICAD_MCP_SERVER_PORT` | Fixed port (default: auto-select) |
| `KICAD_MCP_LOG_DIR` | Log destination (default: KiCad user config dir) |

---

## 6. Directory Layout

### macOS
```
~/Library/Preferences/kicad/<ver>/scripting/plugins/kcaa_plugin/
    __init__.py          # wx panel entry point
    client/              # LLM client code
    logs/                # server logs (KICAD_MCP_LOG_DIR default)

~/Library/Preferences/kicad/<ver>/scripting/plugins/kcaa_plugin/
    settings.json        # API key, model, port override

# MCP server (installed as a Python package, separate venv or system):
<venv>/lib/python*/site-packages/kcaa/
```

### Linux
```
~/.config/kicad/<ver>/scripting/plugins/kcaa_plugin/
    __init__.py
    client/
    logs/
    settings.json
```

### Windows
```
%APPDATA%\kicad\<ver>\scripting\plugins\kcaa_plugin\
    __init__.py
    client\
    logs\
    settings.json
```

---

## 7. Security Considerations

- The MCP server **always** binds to `127.0.0.1`, never `0.0.0.0`.
- The port is ephemeral and chosen at runtime; it is not advertised outside the plugin process.
- `KICAD_MCP_LLM_API_KEY` is stored only in the plugin settings file, never in source code or
  environment variables passed to the server subprocess.
- File mutations performed by the server are validated to be within the active project directory;
  paths outside the project root are rejected.
- `.bak` files provide a single-level undo for every mutation.

---

## 8. Key Constraints

- **Python version mismatch:** KiCad ships its own bundled Python (typically 3.x but not
  guaranteed to match system Python). The plugin runs inside KiCad's interpreter; the MCP server
  runs in whatever `sys.executable` the plugin discovers at startup. They must not share an
  interpreter.
- **Server start failure:** if the server process fails to pass the health check within 10 seconds,
  the plugin disables all tool-call functionality and shows a degraded-mode banner. Chat-only
  interaction with the LLM (no tools) remains available.
- **No `kicad-cli` dependency:** the server milestone-1 scope uses only the `skip` library for
  file I/O, eliminating the need for `kicad-cli` to be on `PATH` inside the server subprocess.
