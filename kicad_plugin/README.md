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
  └─ Profile: "plugin" — 124 tools; 23 are the schematic/symbol core below
```

## Installation

The plugin is a folder inside KiCad's user plugin directory. Release archives
already use `kicad_ai_assistant` for manual installs; keep that name so the
documented `cd`/setup commands and the manual-install convention match.
Renaming the installed folder is unsupported. `10.0` below is your KiCad
`<major>.<minor>` version.

| OS | Manual install (unzip `kicad_ai_assistant.zip` here) | PCM install (`Install from File...`) |
|----|-----------------------------------------------------|--------------------------------------|
| Linux | `~/.local/share/kicad/10.0/scripting/plugins` | `~/.local/share/kicad/10.0/3rdparty/plugins` |
| macOS | `~/Documents/KiCad/10.0/scripting/plugins` | `~/Documents/KiCad/10.0/3rdparty/plugins` |
| Windows | `%USERPROFILE%\Documents\KiCad\10.0\scripting\plugins` | `%USERPROFILE%\Documents\KiCad\10.0\3rdparty\plugins` |

`KICAD_DOCUMENTS_HOME` overrides the documents directory on every OS.

### 1. Install the plugin

**Manual install:** unzip `kicad_ai_assistant.zip` into the manual directory for
your OS. It creates `<manual>/kicad_ai_assistant/`; keep that name so the
documented `cd`/setup commands match. Do not hand-unzip into `3rdparty/plugins`
— that directory belongs to the Plugin and Content Manager, and a manually
copied folder there is not a PCM install.

**PCM install:** use `kicad_ai_assistant_pcm.zip`, not the manual archive. Open
**Preferences → Plugin and Content Manager → Install from File…** and select it.
KiCad extracts the package to
`<3rdparty>/plugins/com_github_paul356_kicad-ai-assistant/`; that package
directory is the plugin directory, with `__init__.py` directly inside it. PCM
updates keep `.venv` and the freerouting JAR (`keep_on_update`); rerun the
setup script after an update so the venv gets the matching `kcaa` version.

### 2. Run the setup script

The script creates `.venv`, installs `kcaa` and **PyMuPDF** (imported as
`fitz`, used for PDF import), and downloads the freerouting JAR. `uv` is
required; if it is missing the script prints an error and exits non-zero.
Install it from <https://docs.astral.sh/uv/getting-started/installation/>.

Run it from the installed plugin directory (the folder containing this README):

**Linux/macOS:**
```bash
cd "<installed plugin directory>"
./setup_plugin.sh
```

**Windows:**
```powershell
cd "<installed plugin directory>"
.\setup_plugin.bat
```

For a manual install the directory is
`...\scripting\plugins\kicad_ai_assistant`; for a PCM install it is
`...\3rdparty\plugins\com_github_paul356_kicad-ai-assistant`.

#### Installing an unreleased build

A PR branch or a CI `dist` artifact usually carries the same version as the last release, so a plain
install would silently keep the PyPI release and the new tools would be missing. Pass the build instead:

**Linux/macOS:**
```bash
./setup_plugin.sh /path/to/kcaa-build          # artifact file or source directory
```

**Windows:**
```powershell
.\setup_plugin.ps1 C:\path\to\kcaa-build       # artifact file or source directory
```

The local build is reinstalled over any equal version, and no `VERSION` file is required. Re-running
`./setup_plugin.sh` (or `.\setup_plugin.ps1`) **without** an argument targets the released `kcaa` from
PyPI again; if that release has the same version as the local build, delete `.venv` first to force the
PyPI copy back.

### 3. Enable the KiCad API

**Preferences → Plugins → Enable KiCad API.** The panel uses this IPC socket for
**Reload PCB** and the `update_pcb_from_schematic` tool; without it those two
fail, while file-based editing keeps working. This package does not declare
the IPC runtime, so KiCad does not prompt for the toggle — enable it manually.

### 4. Open KiCad

1. Open KiCad and load your project
2. In the PCB editor or schematic editor, go to **Tools → External Plugins → Refresh Plugins**
3. The **KiCad AI Assistant** plugin will appear in the plugin list
4. Click it to open the chat panel
5. Enter your LLM API key in **Options → Settings** if your endpoint requires one

## Configuration

Settings are stored under `<config>/kicad/<ver>/kcaa/kicad_ai_assistant.json`:
- Linux: `~/.config/kicad/10.0/kcaa/kicad_ai_assistant.json`
- macOS: `~/Library/Preferences/kicad/10.0/kcaa/kicad_ai_assistant.json`
- Windows: `%APPDATA%\kicad\10.0\kcaa\kicad_ai_assistant.json`

| Setting | Description | Default |
|---------|-------------|---------|
| `llm_provider` | `openai`, `anthropic`, or `ollama` | `openai` |
| `llm_api_key` | API key; optional for unauthenticated compatible endpoints | (empty) |
| `llm_model` | Model name | `gpt-4o` |
| `llm_base_url` | Custom OpenAI, Anthropic, or Ollama-compatible endpoint URL | (uses provider default) |
| `server_port` | Fixed port for MCP server (0 = auto) | `0` |
| `show_tool_log` | Show tool-call log by default | `true` |

## Available Tools (Milestone 1)

The plugin registers 124 tools in total. The 23 tools below are the milestone-1 schematic/symbol core:

- **Netlist inspection:** `extract_schematic_netlist`, `find_component_connections`
- **Symbol search:** `search_library_symbols`, `get_library_symbol`, `list_symbol_libraries`, `get_library_symbol_pins` + 4 more
- **Component editing:** `add_symbol_to_schematic`, `remove_symbol_from_schematic`, `move_component`, `set_symbol_property` + 6 more
- **Wire editing:** `connect_points_with_wire`, `connect_pins_with_wire`, `delete_wire_from_schematic`

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
