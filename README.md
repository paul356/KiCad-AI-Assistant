# KiCad AI Assistant

KiCad AI Assistant is a KiCad action plugin that embeds an LLM-powered chat panel directly inside KiCad. It runs a built-in [MCP](https://modelcontextprotocol.io/) server and exposes a rich set of tools so the LLM can read and edit your schematics and PCB layouts through natural-language conversation.

Tested on **KiCad 10.0 / Linux & Windows**.

[![CI](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml)

## Table of Contents

- [KiCad AI Assistant](#kicad-ai-assistant)
  - [Table of Contents](#table-of-contents)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
    - [1. Clone the repository](#1-clone-the-repository)
    - [2. Install the plugin](#2-install-the-plugin)
    - [3. Create the plugin virtual environment](#3-create-the-plugin-virtual-environment)
    - [4. Load the plugin in KiCad](#4-load-the-plugin-in-kicad)
  - [Configuration](#configuration)
  - [Standalone MCP Server](#standalone-mcp-server)
  - [Feature Highlights](#feature-highlights)
  - [Available Tools](#available-tools)
    - [Project Tools](#project-tools)
    - [Symbol Library](#symbol-library)
    - [Schematic Editing](#schematic-editing)
    - [Schematic Analysis](#schematic-analysis)
    - [Hierarchical Sheets](#hierarchical-sheets)
    - [PCB Library](#pcb-library)
    - [PCB Query](#pcb-query)
    - [PCB Editing](#pcb-editing)
    - [PCB Placement](#pcb-placement)
    - [PCB Groups](#pcb-groups)
    - [PCB Zones](#pcb-zones)
    - [DRC & Design Rules](#drc--design-rules)
    - [Versioning & Export](#versioning--export)
    - [Skill System](#skill-system)
    - [KiCad IPC](#kicad-ipc)
  - [Project Structure](#project-structure)
  - [Troubleshooting](#troubleshooting)
  - [Contributing](#contributing)
  - [License](#license)

## Prerequisites

- KiCad 10.0 or higher
- [`uv`](https://github.com/astral-sh/uv) — manages the Python virtual environment and installs the correct Python version automatically
  - `curl -Lsf https://astral.sh/uv/install.sh | sh`
- An API key for OpenAI, Anthropic, or a compatible LLM provider

## Installation

### 1. Clone the repository (optional)

Only needed if you want to build the plugin from source or contribute to the project. Skip this step if you're downloading the pre-built plugin from the Releases page.

```bash
git clone https://github.com/paul356/kcaa.git
cd kcaa
```

### 2. Install the plugin

Download `kicad-ai-assistant.zip` from the [Releases page](https://github.com/paul356/KiCad-AI-Assistant/releases) and unzip it into KiCad's plugin directory:

**Linux:**
```bash
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip kicad-ai-assistant.zip -d "$KICAD_PLUGIN_DIR"
```

**Windows (PowerShell):**
```powershell
$KICAD_PLUGIN_DIR = "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins"
New-Item -ItemType Directory -Force -Path $KICAD_PLUGIN_DIR
Expand-Archive -Path kicad-ai-assistant.zip -DestinationPath $KICAD_PLUGIN_DIR
```

Or build from source:

```bash
# In the kcaa repository root:
make dist-plugin          # produces dist/kicad_ai_assistant.zip

# Linux:
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

### 3. Create the plugin virtual environment

Run the setup script from inside the installed plugin directory to create a `.venv`, install `kcaa` from PyPI, and download the freerouting JAR. `uv` will automatically install the required Python version.

**Linux:**
```bash
cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
./setup_plugin.sh
```

**Windows (PowerShell):**
```powershell
cd "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins\kicad_ai_assistant"
.\setup_plugin.ps1
```

**Windows (Command Prompt):**
```cmd
cd /d "%USERPROFILE%\Documents\KiCad\10.0\scripting\plugins\kicad_ai_assistant"
setup_plugin.bat
```

The script will detect your KiCad version from the plugin directory path. KiCad already provides all necessary environment variables (`KICAD_VERSION`, `KICAD*_DIR`, etc.), so no `.env` file is needed.

### 4. Load the plugin in KiCad

1. Open KiCad and load your project.
2. Open the **Schematic Editor** or **PCB Editor**.
3. Go to **Tools → External Plugins → Refresh Plugins**.
4. Click **KiCad AI Assistant** in the plugin list to open the chat panel.
5. Go to **Options → Settings** and enter your LLM API key.

## Configuration

Plugin settings are stored in the KiCad user config directory:

Settings file: `~/.config/kicad/kicad_ai_assistant.json`

All settings can be changed through **Options → Settings** in the plugin panel:

| Setting | Description | Default |
|---------|-------------|---------|
| `llm_provider` | LLM provider: `openai`, `anthropic`, or `custom` | `openai` |
| `llm_api_key` | Your LLM API key (stored with owner-only permissions) | *(empty)* |
| `llm_model` | Model name | `gpt-4o` |
| `llm_base_url` | Custom endpoint URL (when `llm_provider` is `custom`) | *(provider default)* |
| `server_port` | Fixed port for the built-in MCP server (`0` = auto) | `0` |
| `show_tool_log` | Show tool-call log panel by default | `true` |
| `llm_context_tokens` | Total context window size in tokens | `128000` |
| `llm_compact_threshold` | Trigger context compaction at this usage fraction | `0.70` |

## Standalone MCP Server

You can also run `kcaa` as a standalone MCP server without the KiCad plugin. This is useful for integrating with other MCP clients (e.g., Claude Desktop, Cursor).

Create a `.env` file in your working directory:

```dotenv
KICAD_SEARCH_PATHS=/home/user/pcb
KICAD_APP_PATH=/usr/share/kicad
KICAD_VERSION=10.0
KICAD_CONFIG_DIR=~/.config/kicad/10.0
KICAD_3RD_PARTY=~/.local/share/kicad/10.0/3rdparty
MCP_TRANSPORT=streamable-http
```

Then start the server:

```bash
kcaa
```

## Feature Highlights

- **Schematic editing** — Add/remove symbols, set/rename properties, draw and delete wires, connect pins automatically
- **Hierarchical sheets** — Create, read, update, and delete hierarchical sheet symbols and sheet pins
- **PCB footprint library** — Search the system footprint library index by name, description, or tag; set footprints on schematic symbols
- **PCB synchronisation** — Trigger *Update PCB from Schematic* via KiCad's IPC API
- **PCB placement** — Query, move, rotate, flip, align, and distribute footprints; define or clear the board outline
- **Design rules** — View and modify board-level design rules, net classes, and custom DRC rules
- **DRC** — Run design-rule checks directly from the plugin
- **Context management** — Automatic compaction of the LLM context window when it approaches the limit
- **Session management** — Save, restore, and reset the current conversation; save design snapshots for rollback
- **Skill system** — On-demand workflow guidance for the LLM via user-definable Markdown skill files

## Available Tools

### Project Tools

| Tool | Description |
|------|-------------|
| `list_projects` | Find and list all KiCad projects |
| `get_project_structure` | Get the structure and files of a KiCad project |
| `open_project` | Open a KiCad project in KiCad |

### Symbol Library

| Tool | Description |
|------|-------------|
| `sync_symbol_index` | Build or refresh the symbol library index |
| `get_symbol_sync_status` | Query symbol index build progress |
| `get_symbol_index_stats` | Get statistics about the symbol index |
| `list_symbol_libraries` | List symbol libraries from the index |
| `search_symbols` | Full-text search across indexed symbols |
| `get_symbol` | Look up a symbol by library and name |
| `get_library_symbols` | Return symbols in a specific library |
| `get_symbol_pins` | Get pin definitions for a symbol |

### Schematic Editing

| Tool | Description |
|------|-------------|
| `add_symbol_to_schematic` | Place a symbol on the schematic |
| `place_symbol_relative` | Place a symbol relative to an existing component |
| `remove_symbol_from_schematic` | Remove placed symbol by reference |
| `move_component` | Move and/or rotate a placed component |
| `set_symbol_property` | Set a property field on a placed symbol |
| `rename_symbol` | Rename a placed symbol's reference designator |
| `list_symbol_properties` | List all properties of a placed symbol |
| `delete_symbol_property` | Delete a property from a placed symbol |
| `check_reference_conflicts` | Check for duplicate reference designators |
| `connect_points_with_wire` | Route a smart orthogonal wire between two points |
| `add_wire_to_schematic` | Add a single wire segment by endpoints |
| `connect_pins_with_wire` | Connect two symbol pins with a wire |
| `delete_wire_from_schematic` | Remove wire segments by endpoints |
| `add_label_to_schematic` | Add a local net label |
| `list_labels_in_schematic` | List all local net labels |
| `delete_label_from_schematic` | Delete net labels |
| `get_schematic_sheet_info` | Get drawing area, paper size, and grid |
| `find_free_area` | Find candidate areas for placing a block |

### Schematic Analysis

| Tool | Description |
|------|-------------|
| `extract_schematic_netlist` | Extract netlist from a schematic |
| `extract_project_netlist` | Extract netlist for a whole project |
| `find_component_connections` | Find all connections for a component |
| `identify_circuit_patterns` | Identify common circuit patterns |
| `analyze_project_circuit_patterns` | Analyze circuit patterns in a project |
| `validate_project` | Basic validation of a KiCad project |
| `validate_project_boundaries` | Validate component boundaries |
| `generate_validation_report` | Generate a comprehensive validation report |

### Hierarchical Sheets

| Tool | Description |
|------|-------------|
| `list_sheet_symbols` | List all hierarchical sheet symbols in a schematic |
| `get_sheet_hierarchy` | Get the full sheet hierarchy tree |
| `add_sheet_symbol` | Add a new hierarchical sheet symbol |
| `remove_sheet_symbol` | Remove a sheet symbol (with optional child file deletion) |
| `update_sheet_symbol` | Update a sheet symbol's properties and geometry |
| `add_sheet_pin` | Add a hierarchical pin to a sheet symbol |
| `remove_sheet_pin` | Remove a hierarchical pin from a sheet symbol |

### PCB Library

| Tool | Description |
|------|-------------|
| `sync_footprint_index` | Build or refresh the footprint library index |
| `get_footprint_sync_status` | Query footprint index build progress |
| `list_footprint_libraries` | List all available footprint libraries |
| `search_footprints` | Search footprints by name, description, or tag |
| `get_footprint_details` | Get footprint details (pads, bounding box, etc.) |

### PCB Query

| Tool | Description |
|------|-------------|
| `get_board_info` | Get basic PCB board information |
| `list_footprints` | List all footprints placed on the board |
| `get_footprint` | Get details of a single placed footprint |
| `get_footprint_bbox` | Get the courtyard bounding box of a footprint |
| `get_board_bounding_box` | Get the union bounding box of all footprints |
| `list_nets` | List all nets on the board |
| `get_ratsnest` | Get unrouted ratsnest connections |
| `score_placement` | Score the current PCB placement quality |
| `suggest_placement_order` | Get recommended footprint placement order |

### PCB Editing

| Tool | Description |
|------|-------------|
| `get_board_outline` | Read Edge.Cuts board outline elements |
| `clear_board_outline` | Clear the board outline |
| `add_board_outline_segment` | Add a line segment to the board outline |
| `add_board_outline_arc` | Add an arc to the board outline |
| `set_board_outline_rect` | Set a rectangular board outline (with optional rounded corners) |
| `set_footprint_property` | Set a property field on a footprint |
| `update_pcb_from_schematic` | Trigger *Update PCB from Schematic* via KiCad IPC |

### PCB Placement

| Tool | Description |
|------|-------------|
| `set_footprint_position` | Move and/or rotate a single footprint |
| `flip_footprint` | Flip a footprint between top and bottom layer |
| `align_footprints` | Align footprints to the same axis |
| `distribute_footprints` | Distribute footprints evenly along an axis |
| `move_footprints_by_delta` | Translate footprints by (dx, dy) |
| `find_free_pcb_area` | Find an area free of existing footprints |

### PCB Groups

| Tool | Description |
|------|-------------|
| `assign_footprints_to_group` | Assign footprints to a placement group |
| `list_footprint_groups` | List all placement groups on the board |
| `get_footprint_group` | Get details of a placement group |
| `score_footprint_group` | Score intra-group placement quality |
| `place_footprint_group` | Place all members of a group |
| `move_footprint_group` | Translate a placed group |
| `rotate_footprint_group` | Rotate a placed group around its anchor |

### PCB Zones

| Tool | Description |
|------|-------------|
| `list_zones` | List all copper-pour and keepout zones |
| `add_zone` | Add a copper-pour or keepout zone |
| `delete_zone` | Delete a zone by UUID |
| `refill_zones` | Refill all zones on the PCB |

### DRC & Design Rules

| Tool | Description |
|------|-------------|
| `get_effective_design_rules` | Get all design constraints (board rules, net classes, custom rules) |
| `set_design_rules` | Update board-level design rule minimums |
| `set_net_class_rules` | Create or update a net class's design parameters |
| `assign_nets_to_class` | Assign nets to a net class |
| `remove_nets_from_class` | Remove nets from a net class (revert to Default) |
| `delete_net_class` | Delete a net class |
| `add_custom_rule` | Add a custom DRC rule to the project |
| `del_custom_rule` | Delete a custom DRC rule by name |
| `run_drc_check` | Open the KiCad DRC dialog |
| `analyze_bom` | Analyze the bill of materials |
| `export_bom_csv` | Export the BOM to CSV |

### Versioning & Export

| Tool | Description |
|------|-------------|
| `save_file_version` | Save a version snapshot for rollback |
| `list_file_versions` | List saved version snapshots |
| `restore_file_version` | Restore to a previously saved version |
| `generate_pcb_thumbnail` | Render a PCB thumbnail image |
| `generate_project_thumbnail` | Render a project thumbnail |

### Skill System

| Tool | Description |
|------|-------------|
| `list_skills` | List all available skill documents |
| `get_skill` | Get the full content of a skill document |
| `add_skill` | Create a new skill document |
| `append_to_skill` | Append content to an existing skill |
| `delete_skill` | Soft-delete a skill document |

### KiCad IPC

| Tool | Description |
|------|-------------|
| `check_kicad_ipc_connection` | Check if the KiCad IPC socket is responsive |
| `save_document` | Save the active document in KiCad |
| `reload_kicad` | Reload documents in the running KiCad editor |

## Project Structure

```
kcaa/
├── main.py                  # MCP server entry point
├── pyproject.toml           # Package metadata and dependencies
├── run_tests.py             # Test runner script
├── kcaa/                    # MCP server package
│   ├── server.py            # Server setup and tool registration
│   ├── config.py            # Configuration and KiCad path detection
│   ├── context.py           # Request context management
│   ├── tools/               # All MCP tool implementations
│   ├── resources/           # MCP resource handlers
│   ├── prompts/             # MCP prompt templates
│   └── utils/               # Utility functions
├── kicad_plugin/            # KiCad action plugin
│   ├── __init__.py          # Plugin entry point (KiCadAIPlugin)
│   ├── server_manager.py    # Start/stop the kcaa subprocess
│   ├── llm_client.py        # Agentic tool-call loop (OpenAI / Anthropic)
│   ├── context_bridge.py    # Collect active project paths from KiCad
│   ├── settings.py          # Load/save plugin settings
│   ├── autorouter.py        # FreeRouting integration
│   ├── tool_registry.py     # Tool metadata and categorization
│   ├── setup_plugin.sh      # Linux/macOS setup script
│   ├── setup_plugin.ps1     # Windows PowerShell setup script
│   ├── setup_plugin.bat     # Windows batch setup script
│   └── ui/                  # wxPython chat panel and settings dialog
├── docs/                    # Feature documentation
└── tests/                   # Unit tests
```

## Troubleshooting

**Plugin does not appear in KiCad:**
- Confirm the plugin directory is named exactly `kicad_ai_assistant` (not `kicad_ai_plugin`).
- Run **Tools → External Plugins → Refresh Plugins** after installing.
- Linux: Check that `setup_plugin.sh` completed without errors and that `.venv/bin/python` exists inside the plugin directory.
- Windows: Check that `setup_plugin.bat` (or `setup_plugin.ps1`) completed without errors and that `.venv/Scripts/python.exe` exists inside the plugin directory.

**MCP server fails to start:**
- Linux: Check the plugin log in `~/.config/kicad/` for Python tracebacks.
- Windows: Check the plugin log in `%APPDATA%/kicad/` for Python tracebacks.

**Schematic editor does not refresh after edits:**
- This is a current KiCad IPC limitation. Use **File → Reload** or press **Ctrl+Z / Ctrl+Y** to trigger a refresh in the schematic editor.

**LLM API errors:**
- Confirm the API key is correct in **Options → Settings**.
- Check that `llm_model` is a valid model name for your chosen provider.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add your changes with tests
4. Submit a pull request

## License

This project is open source under the MIT license.
