# KiCad AI Assistant

KiCad AI Assistant is a KiCad action plugin that embeds an LLM-powered chat panel directly inside KiCad. It runs a built-in [MCP](https://modelcontextprotocol.io/) server and exposes a rich set of tools so the LLM can read and edit your schematics and PCB layouts through natural-language conversation.

Tested on **KiCad 10.0 / Linux**.

## Table of Contents

- [KiCad AI Assistant](#kicad-ai-assistant)
  - [Table of Contents](#table-of-contents)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
    - [1. Clone the repository](#1-clone-the-repository)
    - [2. Configure the environment](#2-configure-the-environment)
    - [3. Build and install the plugin](#3-build-and-install-the-plugin)
    - [4. Create the plugin virtual environment](#4-create-the-plugin-virtual-environment)
    - [5. Load the plugin in KiCad](#5-load-the-plugin-in-kicad)
  - [Configuration](#configuration)
  - [Feature Highlights](#feature-highlights)
  - [Available Tools](#available-tools)
    - [Schematic Tools](#schematic-tools)
    - [PCB Tools](#pcb-tools)
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

### 1. Clone the repository

```bash
git clone https://github.com/paul356/kicad-mcp.git
cd kicad-mcp
```

### 2. Configure the environment

Create a `.env` file in the repository root to tell the server where KiCad is installed and where your projects live:

```bash
cp .env.example .env
vim .env
```

Key variables to set:

| Variable | Description | Example |
|----------|-------------|---------|
| `KICAD_SEARCH_PATHS` | Comma-separated directories to scan for KiCad projects | `/home/user/pcb` |
| `KICAD_APP_PATH` | Path to KiCad's shared data directory | `/usr/share/kicad` |
| `KICAD_VERSION` | KiCad major.minor version | `10.0` |
| `KICAD_CONFIG_DIR` | KiCad user config directory | `~/.config/kicad/10.0` |
| `KICAD_3RD_PARTY` | KiCad third-party plugins directory | `~/.local/share/kicad/10.0/3rdparty` |
| `MCP_TRANSPORT` | MCP transport protocol | `streamable-http` |

A typical Linux `.env` looks like:

```dotenv
KICAD_SEARCH_PATHS=/home/user/pcb
KICAD_APP_PATH=/usr/share/kicad
KICAD_VERSION=10.0
KICAD_CONFIG_DIR=~/.config/kicad/10.0
KICAD_3RD_PARTY=~/.local/share/kicad/10.0/3rdparty
MCP_TRANSPORT=streamable-http
```

### 3. Build and install the plugin

Build the plugin zip with `make`, then unzip it into KiCad's plugin directory:

```bash
# In the kicad-mcp repository root:
make dist-plugin          # produces dist/kicad_ai_assistant.zip

KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

### 4. Create the plugin virtual environment

Run `setup_plugin.sh` from inside the installed plugin directory to create a `.venv` and install `kicad_mcp` as an editable package. `uv` will automatically install the required Python version.

```bash
cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
./setup_plugin.sh /path/to/kicad-mcp
```

### 5. Load the plugin in KiCad

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

## Feature Highlights

- **Schematic editing** — Add/remove symbols, set properties, draw and delete wires, connect pins automatically
- **PCB footprint library** — Search the system footprint library index by name, description, or tag; set footprints on schematic symbols
- **PCB synchronisation** — Trigger *Update PCB from Schematic* via KiCad's IPC API
- **PCB placement** — Query, move, rotate, flip, align, and distribute footprints; define or clear the board outline
- **Context management** — Automatic compaction of the LLM context window when it approaches the limit
- **Session management** — Save, restore, and reset the current conversation; save design snapshots for rollback
- **DRC** — Run design-rule checks and track violations over time

## Available Tools

### Schematic Tools

| Tool | Description |
|------|-------------|
| `list_symbol_libraries` | List all available symbol libraries |
| `search_symbols` | Search symbols by name, description, or keyword |
| `get_symbol` | Get detailed information about a symbol |
| `get_symbol_pins` | Get pin definitions for a symbol |
| `sync_symbol_index` | Build or refresh the symbol library index |
| `get_symbol_sync_status` | Query symbol index build progress |
| `get_symbol_index_stats` | Get statistics about the symbol index |
| `get_symbol_index_libraries` | List libraries present in the symbol index |
| `add_symbol_to_schematic` | Place a symbol on the schematic |
| `remove_symbol_from_schematic` | Remove a placed symbol by reference |
| `move_component` | Move a component to new coordinates |
| `set_component_property` | Set a property field on a placed symbol |
| `get_component_properties` | Read all property fields of a placed symbol |
| `add_wire_to_schematic` | Draw a wire segment between two points |
| `connect_pins_with_wire` | Automatically route a wire between two pins |
| `delete_wire_from_schematic` | Remove a wire segment |
| `save_snapshot` | Save a snapshot of the current schematic for rollback |
| `list_projects` | List KiCad projects in the search paths |
| `get_project_info` | Get information about a KiCad project |
| `open_project` | Open a KiCad project |
| `extract_schematic_netlist` | Extract the netlist from a schematic |
| `extract_project_netlist` | Extract the netlist for a whole project |
| `find_component_connections` | Find all nets connected to a component |
| `analyze_schematic` | Analyse the schematic for design issues |
| `identify_circuit_patterns` | Identify common circuit patterns |
| `recognize_circuit_patterns` | Extended pattern recognition |
| `run_drc` | Run KiCad CLI design-rule check |
| `get_drc_history` | Retrieve historical DRC results |
| `generate_bom` | Generate a bill of materials |
| `export_bom` | Export the BOM to a file |
| `generate_thumbnail` | Render a PCB thumbnail image |

### PCB Tools

| # | Tool | Description |
|---|------|-------------|
| 1 | `sync_footprint_index` | Build or incrementally update the footprint library index |
| 2 | `get_footprint_sync_status` | Query footprint index build progress |
| 3 | `list_footprint_libraries` | List all available footprint libraries |
| 4 | `search_footprints` | Search footprints by name, description, or tag |
| 5 | `get_footprint_details` | Get footprint details (pads, bounding box, etc.) |
| 6 | `get_board_info` | Get basic PCB board information |
| 7 | `list_footprints` | List all footprints placed on the board |
| 8 | `get_footprint` | Get details of a single placed footprint |
| 9 | `list_nets` | List all nets on the board |
| 10 | `get_ratsnest` | Get unrouted ratsnest connections |
| 11 | `get_board_outline` | Read Edge.Cuts board outline elements |
| 12 | `clear_board_outline` | Clear the board outline |
| 13 | `add_board_outline_segment` | Add a line segment to the board outline |
| 14 | `add_board_outline_arc` | Add an arc to the board outline |
| 15 | `set_board_outline_rect` | Set a rectangular board outline (with optional rounded corners) |
| 16 | `get_footprint_bbox` | Get the courtyard bounding box of a footprint |
| 17 | `get_board_bounding_box` | Get the union bounding box of all footprints |
| 18 | `align_footprints` | Align a group of footprints to the same axis |
| 19 | `distribute_footprints` | Distribute footprints evenly along an axis |
| 20 | `move_footprints_by_delta` | Translate a group of footprints by (dx, dy) |
| 21 | `find_free_pcb_area` | Find an area on the board free of existing footprints |
| 22 | `set_footprint_position` | Move and/or rotate a single footprint |
| 23 | `flip_footprint` | Flip a footprint between top and bottom layer |
| 24 | `set_footprint_property` | Set a property field on a footprint |
| 25 | `update_pcb_from_schematic` | Trigger *Update PCB from Schematic* via KiCad IPC |
| 26 | `reload_kicad` | Reload the active file in the KiCad editor |

## Project Structure

```
kicad-mcp/
├── main.py                  # MCP server entry point
├── pyproject.toml           # Package metadata and dependencies
├── .env                     # Local environment configuration (not committed)
├── kicad_mcp/               # MCP server package
│   ├── server.py            # Server setup and tool registration
│   ├── config.py            # Configuration and KiCad path detection
│   ├── tools/               # All MCP tool implementations
│   ├── resources/           # MCP resource handlers
│   └── prompts/             # MCP prompt templates
├── kicad_plugin/            # KiCad action plugin
│   ├── __init__.py          # Plugin entry point (KiCadAIPlugin)
│   ├── server_manager.py    # Start/stop the kicad-mcp subprocess
│   ├── llm_client.py        # Agentic tool-call loop (OpenAI / Anthropic)
│   ├── context_bridge.py    # Collect active project paths from KiCad
│   ├── settings.py          # Load/save plugin settings
│   ├── setup_plugin.sh      # Helper script to create the plugin .venv
│   └── ui/                  # wxPython chat panel and settings dialog
├── docs/                    # Feature documentation
└── tests/                   # Unit tests
```

## Troubleshooting

**Plugin does not appear in KiCad:**
- Confirm the plugin directory is named exactly `kicad_ai_assistant` (not `kicad_ai_plugin`).
- Run **Tools → External Plugins → Refresh Plugins** after installing.
- Check that `setup_plugin.sh` completed without errors and that `.venv/bin/python` exists inside the plugin directory.

**MCP server fails to start:**
- Verify `KICAD_APP_PATH` and `KICAD_VERSION` in `.env` point to your actual KiCad installation.
- Check the plugin log in `~/.config/kicad/` (Linux) for Python tracebacks.

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
