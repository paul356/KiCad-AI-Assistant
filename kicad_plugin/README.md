# KiCad AI Assistant Plugin

This directory contains the KiCad action plugin that connects to the `kcaa` MCP server, allowing an LLM to edit KiCad schematics through natural-language chat.

## Architecture

```
KiCad (GUI)
  └─ kicad_plugin/          ← this directory (action plugin)
       ├─ __init__.py        ← KiCad plugin entry point
       ├─ settings.py        ← Load/save LLM API key and preferences
       ├─ server_manager.py  ← Start/stop the kcaa subprocess
       ├─ context_bridge.py  ← Collect active project/schematic paths from KiCad
       ├─ llm_client.py      ← Agentic tool-call loop (OpenAI / Anthropic)
       └─ ui/
            ├─ panel.py         ← Main chat panel (wx.Frame)
            └─ settings_dialog.py ← Settings dialog

kcaa MCP server (subprocess, streamable-http on localhost)
  └─ Profile: "plugin" — 26 skip-based schematic editing tools only
```

## Installation

### macOS (recommended)

1. Install [KiCad](https://www.kicad.org/download/macos/) and [uv](https://github.com/astral-sh/uv) if you haven't already.
2. Copy or extract this plugin folder so it lives at:
   ```text
   ~/Library/Preferences/kicad/10.0/scripting/plugins/kicad_ai_assistant
   ```
3. Double-click `setup_plugin_macos.command` inside that folder, **or** run from Terminal:
   ```bash
   cd ~/Library/Preferences/kicad/10.0/scripting/plugins/kicad_ai_assistant
   ./setup_plugin_macos.command
   ```

For a fully automated install, run:
```bash
./install_macos.sh
```
This detects your KiCad version, copies the plugin to the correct directory, and runs the setup script.

### Linux

```bash
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
cp -r kicad_plugin "$KICAD_PLUGIN_DIR/kicad_ai_assistant"
cd "$KICAD_PLUGIN_DIR/kicad_ai_assistant"
./setup_plugin.sh
```

### Windows (PowerShell)

```powershell
$dir = "$env:APPDATA\kicad\10.0\scripting\plugins\kicad_ai_assistant"
New-Item -ItemType Directory -Force -Path $dir
Copy-Item -Recurse kicad_plugin\* $dir
cd "$env:APPDATA\kicad\10.0\scripting\plugins\kicad_ai_assistant"
.\setup_plugin.bat
```

### Open KiCad

1. Close KiCad if it is already open so the plugin and API changes take effect.
2. Reopen KiCad and load your project.
3. In the PCB editor, the **KiCad AI Assistant** toolbar button should appear automatically. If it does not, choose **Tools → External Plugins → Refresh Plugins**.
4. Click **KiCad AI Assistant** to open the chat panel.
5. Enter your LLM API key in **Options → Settings**

## Configuration

Settings are stored in the KiCad user config directory. Replace `<ver>` with your KiCad version (e.g. `10.0`):
- Linux: `~/.config/kicad/<ver>/kcaa/kicad_ai_assistant.json`
- macOS: `~/Library/Preferences/kicad/<ver>/kcaa/kicad_ai_assistant.json`
- Windows: `%APPDATA%\kicad\<ver>\kcaa\kicad_ai_assistant.json`

| Setting | Description | Default |
|---------|-------------|---------|
| `llm_provider` | `openai`, `anthropic`, or `ollama` | `openai` |
| `llm_api_key` | Your LLM API key | (empty) |
| `llm_model` | Model name | `gpt-4o` |
| `llm_base_url` | Ollama endpoint URL (e.g. http://localhost:11434) | (uses provider default) |
| `server_port` | Fixed port for MCP server (0 = auto) | `0` |
| `show_tool_log` | Show tool-call log by default | `true` |

## Available Tools (Milestone 1)

The plugin exposes 26 schematic editing tools to the LLM:

- **Netlist inspection:** `extract_schematic_netlist`, `extract_project_netlist`, `find_component_connections`
- **Symbol search:** `search_symbols`, `get_symbol`, `list_symbol_libraries`, `get_symbol_pins` + 4 more
- **Component editing:** `add_symbol_to_schematic`, `remove_symbol_from_schematic`, `move_component`, `set_symbol_property` + 5 more
- **Wire editing:** `add_wire_to_schematic`, `connect_pins_with_wire`, `delete_wire_from_schematic` + 3 more

See `docs/plugin/tool_contract.md` for full documentation.

## Safety

- Every schematic mutation writes a `.kicad_sch.bak` backup before saving
- The MCP server only listens on `127.0.0.1` (never exposed to the network)
- The LLM API key is stored in the KiCad config directory, not in environment variables or source code
- File mutations are visible to you before you click **Reload Schematic**

## Development

To test the plugin outside KiCad:

```bash
cd /path/to/kcaa
.venv/bin/python3 -c "
from kicad_plugin.settings import PluginSettings
from kicad_plugin.server_manager import ServerManager
s = PluginSettings.load()
mgr = ServerManager(s)
print('Settings:', s)
"
```
