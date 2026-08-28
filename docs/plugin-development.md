# KiCad AI Assistant Plugin — Development Guide

This document describes how the **KiCad AI Assistant** plugin was designed, how its pieces fit together, and how to modify or extend it. It covers the plugin code in `kicad_plugin/`, the MCP server in `kcaa/`, the installation plumbing, the macOS port, and the test strategy.

---

## 1. What the plugin does

The plugin embeds a chat panel inside KiCad (PCB Editor / Schematic Editor). From that panel an engineer can:

- Ask natural-language questions about the open project.
- Invoke MCP tools that read or edit schematics, netlists, symbols, and PCBs.
- Review every tool call before it runs (or enable YOLO/AFK mode for hands-off automation).
- Reload files that were modified by the agent.

The plugin does not implement the AI logic itself. It is a thin client that:

1. Renders the UI.
2. Reads live context from KiCad.
3. Sends context + conversation history to an external LLM API (OpenAI, Anthropic, or any OpenAI-compatible endpoint such as Moonshot).
4. Executes tool calls requested by the LLM through a local MCP server.

---

## 2. High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        KiCad PCB Editor                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              KiCad AI Assistant panel                   │   │
│  │  (wxPython UI in kicad_plugin/ui/panel.py)              │   │
│  └─────────────────────────────────────────────────────────┘   │
│                         │                                       │
│         reads context   │   spawns                              │
│         from pcbnew     │   subprocess                          │
│                         ▼                                       │
│  ┌─────────────────────────────┐    ┌────────────────────────┐ │
│  │   LLMClient                 │◄──►│   MCP server (kcaa)    │ │
│  │   kicad_plugin/llm_client.py│    │   .venv Python process │ │
│  └─────────────────────────────┘    └────────────────────────┘ │
│              │                                 │                │
│              │ HTTPS                           │ streamable-http│
│              ▼                                 │                │
│       OpenAI / Anthropic / Moonshot            │                │
│              API endpoint                      │                │
└────────────────────────────────────────────────┴────────────────┘
```

Three independent runtimes are involved:

1. **KiCad’s embedded Python** — runs the plugin UI and `LLMClient`.
2. **The plugin virtual-environment Python** — runs the MCP server (`kcaa`) and, on macOS, proxies HTTPS requests.
3. **The LLM provider API** — external cloud service.

This separation is intentional: KiCad’s bundled interpreter is constrained (no pip, no network in some builds, different Python version), so heavy work is delegated to a venv we control.

---

## 3. Repository layout

```
KiCad-AI-Assistant/
├── kicad_plugin/                  # KiCad plugin source
│   ├── __init__.py                # KiCad entry point, registers the panel
│   ├── settings.py                # PluginSettings + KiCad path detection
│   ├── server_manager.py          # Spawns/monitors the kcaa MCP server
│   ├── llm_client.py              # LLM API client + HTTPS subprocess shim
│   ├── context_bridge.py          # Collects context from pcbnew
│   ├── pdf_extractor.py           # PDF parsing helpers
│   ├── autorouter.py              # FreeRouting integration
│   ├── ui/                        # wx UI
│   │   ├── panel.py               # Main chat panel
│   │   ├── settings_dialog.py     # Options → Settings UI
│   │   └── ...
│   ├── skills/                    # Markdown skill catalog
│   ├── setup_plugin.sh            # Linux setup
│   ├── setup_plugin.bat           # Windows setup (cmd)
│   ├── setup_plugin.ps1           # Windows setup (PowerShell)
│   ├── setup_plugin_macos.command # Double-clickable macOS setup
│   ├── install_macos.sh           # One-step macOS installer
│   └── README.md
├── kcaa/                          # MCP server + tools package
│   ├── server.py                  # FastMCP server factory
│   ├── context.py                 # Lifespan context
│   ├── config.py                  # Platform/path configuration
│   ├── tools/                     # ~30 tool modules
│   ├── resources/                 # MCP resources
│   ├── prompts/                   # Prompt templates
│   └── utils/                     # KiCad/file helpers
├── tests/
│   └── unit/plugin/               # Plugin unit tests (macOS focused)
├── Makefile                       # Builds dist/kicad_ai_assistant.zip
├── pyproject.toml                 # kcaa package metadata
└── README.md
```

---

## 4. The KiCad plugin entry point

`kicad_plugin/__init__.py` is what KiCad imports when it loads the plugin. It must:

- Detect whether it is running inside KiCad (`pcbnew` is importable).
- Register a wx panel class with KiCad’s action plugin framework.
- Expose metadata such as name, category, and icon.

When the user clicks the toolbar button, KiCad instantiates the panel class defined in `ui/panel.py`.

### Why `__init__.py` is tiny

All real logic lives in submodules so that:

- The plugin can be imported for unit tests without KiCad present.
- Each subsystem (settings, LLM, server, UI) can be tested independently.
- Failures in one area do not prevent KiCad from loading the plugin.

---

## 5. Settings and KiCad path detection

`kicad_plugin/settings.py` defines `PluginSettings`, a dataclass that loads from JSON in the KiCad user config directory.

### Settings file location

| Platform | Path |
|---|---|
| macOS | `~/Library/Preferences/kicad/<ver>/kcaa/kicad_ai_assistant.json` |
| Linux | `~/.config/kicad/<ver>/kcaa/kicad_ai_assistant.json` |
| Windows | `%APPDATA%\kicad\<ver>\kcaa\kicad_ai_assistant.json` |

The `<ver>` directory is detected by `_detect_kicad_version()`, which tries:

1. `KICAD_VERSION` environment variable.
2. `pcbnew.GetMajorMinorVersion()` when running inside KiCad.
3. Regex on the plugin install path (`.../kicad/10.0/scripting/plugins/...`).
4. `KICAD{N}_*` environment variables.
5. Fallback to `"10.0"` so the plugin still loads in non-standard installs.

### Fields

```python
@dataclass
class PluginSettings:
    llm_provider: str = "openai"        # openai | anthropic | ollama
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_base_url: str = ""              # custom endpoint, e.g. Moonshot
    llm_supports_vision: bool = False
    server_port: int = 0                # 0 = auto-select
    show_tool_log: bool = True
    ...
```

The file is written with `0o600` permissions so the API key is not world-readable.

---

## 6. Spawning the MCP server

`kicad_plugin/server_manager.py` owns the lifecycle of the `kcaa` subprocess.

### Why a subprocess?

KiCad’s bundled Python is not a normal interpreter:

- No `pip` and often no working `_ssl`.
- Different Python version than the rest of the project.
- Environment variables such as `PYTHONHOME`/`LD_LIBRARY_PATH` that break external packages.

Running the MCP server in a separate venv Python avoids all of these problems.

### Startup sequence

1. Resolve the venv Python (`kicad_ai_assistant/.venv/bin/python`).
2. Pick a free TCP port (`server_port` or auto-select).
3. `subprocess.Popen` the server with:
   - `MCP_TRANSPORT=streamable-http`
   - `MCP_PORT=<port>`
   - `KICAD_MCP_PROFILE=plugin`
   - A cleaned environment that strips KiCad AppImage variables.
4. Poll `GET http://127.0.0.1:<port>/mcp` until it responds.
5. Fetch tool definitions from the server.

### Crash recovery

A background thread watches the subprocess. If it exits unexpectedly, the panel shows a **Restart** button that re-runs the startup sequence.

---

## 7. LLM client and the HTTPS shim

`kicad_plugin/llm_client.py` implements the agentic loop and talks to the LLM provider.

### Provider support

- **OpenAI-compatible** — used for OpenAI, Moonshot, DeepSeek, Ollama (with `base_url`), etc.
- **Anthropic** — native Anthropic messages API.
- **Ollama** — local models via `/api/chat`.

### The macOS HTTPS problem

KiCad’s embedded Python on macOS often has a working `_ssl` module but fails certificate verification with an empty or misleading `URLError`. The original code tried in-process HTTPS first and fell back only when it saw the marker string `"unknown url type: https"`.

The fix forces macOS to use the venv Python for every HTTPS request from the start:

```python
if platform.system() == "Darwin" and _resolve_plugin_python():
    _in_process_ssl = False
```

This delegates the request to a one-shot subprocess that runs `urllib.request` inside the plugin venv, where certificates and SSL work correctly.

### Agentic loop

1. Build system prompt + KiCad context + conversation history + tool definitions.
2. POST to the LLM API.
3. If the model requests tool calls:
   - Execute each tool through the local MCP server.
   - Append results to history.
   - Repeat from step 2.
4. When the model returns plain text, render it in the chat panel.

---

## 8. MCP server (`kcaa`)

`kcaa/server.py` creates a FastMCP server. Two profiles exist:

| Profile | Use case | Tools |
|---|---|---|
| `full` (default) | Standalone clients (Claude Desktop, Cursor, Kimi Code CLI) | All tools, resources, prompts |
| `plugin` | Inside KiCad plugin | Skip-based schematic/netlist/symbol tools only |

### Why two profiles?

The plugin supplies project context itself and drives the LLM, so the server inside KiCad only needs the editing tools. The standalone profile includes DRC, export, thumbnail, and `kicad-cli`-dependent tools for external clients.

### Transports

`kcaa` supports `stdio`, `streamable-http`, and `sse`. The plugin uses `streamable-http` with `stateless_http=True` because KiCad’s bundled Python has trouble with `stdio` pipes and SSE is asymmetric.

### Tool categories

The server exposes roughly 30 tool modules:

- **Schematic:** add/remove symbols, wires, labels, sheets.
- **Netlist:** extract and search netlists.
- **Symbol:** search libraries, inspect pins/properties.
- **PCB (full profile):** placement, routing, zones, groups.
- **Validation (full profile):** DRC, BOM, design analysis.

Every mutation creates a `.bak` backup before writing.

---

## 9. Installation plumbing

The plugin is distributed as a zip file (`dist/kicad_ai_assistant.zip`) built by `make dist-plugin`. The zip contains the entire `kicad_plugin/` directory.

### Build flow

```bash
make dist-plugin
```

1. Copies `kicad_plugin/` into `dist/_stage/kicad_ai_assistant/`.
2. Generates a `VERSION` file from `pyproject.toml`.
3. Zips the staged directory.

### Platform setup scripts

After extracting the zip into KiCad’s `scripting/plugins/` directory, the user runs a platform-specific setup script that:

1. Creates a Python virtual environment inside the plugin directory.
2. Installs `kcaa` from PyPI (which pulls all tool dependencies).
3. Installs `pymupdf` for PDF extraction.
4. Downloads the FreeRouting JAR for autorouting.

### macOS one-step installer

`kicad_plugin/install_macos.sh` is the user-facing installer. It:

1. Detects KiCad version from `/Applications/KiCad/KiCad.app/Contents/Info.plist`.
2. Falls back to the newest versioned directory under `~/Library/Preferences/kicad/`.
3. Copies the plugin to `~/Library/Preferences/kicad/<ver>/scripting/plugins/kicad_ai_assistant`.
4. Generates a `VERSION` file from `pyproject.toml` if one is missing.
5. Runs `setup_plugin_macos.command`.
6. **Auto-enables the KiCad API server** in `kicad_common.json`.
7. Warns if KiCad is running (restart required).

This is what makes the macOS install seamless: the user runs one script and the API is configured automatically.

---

## 10. macOS-specific adaptations

The macOS port required several resilience changes because KiCad on macOS behaves differently than on Linux:

### Version detection

The plugin may be run from the repo tree, a temp directory, or the final install path. `_detect_kicad_version()` now falls back through multiple sources and finally defaults to `"10.0"` so the plugin never fails to load.

### VERSION file resilience

The setup script reads the kcaa version from `VERSION`. In the repo this file does not exist, so `install_macos.sh` generates it from `pyproject.toml` after copying the plugin.

### KiCad API auto-enable

KiCad 10 on macOS stores the API setting at:

```text
~/Library/Preferences/kicad/<ver>/kicad_common.json
```

under `api.enable_server`. The installer edits this JSON safely (with a `.kcaa-backup`) so the user never has to touch it manually.

### SSL fallback

As described in §7, macOS KiCad SSL is unreliable, so HTTPS is always proxied through the plugin venv Python.

### IPC socket

The kipy-based tools look for the KiCad IPC socket in the system temp directory. On macOS this resolves to `/var/folders/.../T/kicad/api*.sock`.

---

## 11. Testing strategy

Plugin tests live in `tests/unit/plugin/`. They focus on:

- KiCad version detection (`settings.py`).
- macOS config and plugin paths.
- Venv Python resolution.
- SSL fallback initialization.
- Server manager environment building.
- Installer behavior (dry-run, version detection, API auto-enable, VERSION generation).
- Dist zip contents.

Run the plugin tests:

```bash
KICAD_VERSION=10 uv run pytest tests/unit/plugin/ -q
```

### Developing `kcaa` itself

The plugin venv installs `kcaa` from PyPI by default. When you modify code under `kcaa/`, reinstall the local package into the plugin venv so KiCad sees the changes:

```bash
uv pip install --python ~/Library/Preferences/kicad/<ver>/scripting/plugins/kicad_ai_assistant/.venv/bin/python /path/to/KiCad-AI-Assistant
```

Run macOS-specific tests:

```bash
KICAD_VERSION=10 uv run pytest tests/unit/plugin/test_macos.py -v
```

### CI

`.github/workflows/ci-macos.yml` runs the macOS tests and builds the plugin zip on every PR/push to `main` or `develop`.

### Why mostly unit tests?

Integration-testing inside a real KiCad process is hard to automate. The plugin is therefore structured so that every decision (path resolution, environment construction, JSON editing) can be tested in isolation without KiCad running.

---

## 12. Extending the plugin

### Add a new tool

1. Add the tool function in `kcaa/tools/<category>_tools.py`.
2. Register it in that module’s `register_*_tools(mcp)` function.
3. Import and call the register function in `kcaa/server.py` for the appropriate profile.
4. Add a unit test in `tests/unit/tools/` or `tests/unit/plugin/`.

### Add a new setting

1. Add a field to `PluginSettings` in `kicad_plugin/settings.py`.
2. Add the corresponding control in `kicad_plugin/ui/settings_dialog.py`.
3. The `save()` / `load()` methods handle persistence automatically.

### Add a new LLM provider

If the provider is OpenAI-compatible, no code change is needed — just set `llm_provider="openai"` and the right `llm_base_url`. For a provider with a completely different protocol, add a new `_call_*` method in `llm_client.py` and branch in `_call_llm()`.

---

## 13. Common issues and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `KiCad IPC API socket not available` | KiCad API server disabled | Run `install_macos.sh`, or manually set `api.enable_server=true` in `kicad_common.json` |
| `HTTPS request failed:` (empty) | KiCad embedded Python SSL broken on macOS | Reinstall plugin; the fix forces venv Python for HTTPS |
| `HTTP 401` | Missing or invalid API key | Verify key with `curl`; place config at `~/Library/Preferences/kicad/<ver>/kcaa/kicad_ai_assistant.json` |
| Plugin does not appear in KiCad | Wrong install path or KiCad cached old plugins | Quit KiCad, reinstall, reopen |
| `No module named pip` | venv created by `uv` | Use `uv pip install --python <venv-python> <pkg>` |

---

## 14. Key design decisions

1. **Separate venv for the MCP server** — avoids KiCad interpreter limitations.
2. **Plugin profile for the server** — keeps the in-KiCad server lightweight.
3. **Streamable-HTTP transport** — works around KiCad stdin/stdout restrictions.
4. **Settings in KiCad user config** — follows KiCad conventions and persists per version.
5. **Backup before mutation** — every file write creates a `.bak` for safety.
6. **macOS SSL subprocess fallback** — isolates HTTPS from KiCad’s fragile SSL.
7. **One-step macOS installer** — hides all setup complexity from end users.

---

## 15. Further reading

- `docs/plugin/architecture.md` — original architecture notes.
- `docs/configuration.md` — standalone MCP server configuration.
- `docs/plugin/tool_contract.md` — tool interface contract.
- `kicad_plugin/README.md` — end-user install guide.
- `README.md` — project overview.
