# Configuration Guide

This guide explains how to configure the KiCad MCP Server to suit your environment and requirements.

## Configuration Methods

The KiCad MCP Server can be configured in multiple ways:

1. **Environment Variables**: Set directly when running the server
2. **.env File**: Create a `.env` file in the project root (recommended)
3. **Code Modifications**: Edit configuration constants in `kcaa/config.py`

## Core Configuration Options

### Project Paths

These settings control where the server looks for KiCad projects:

| Environment Variable | Description | Default Value | Example |
|---------------------|-------------|---------------|---------|
| `KICAD_USER_DIR` | The main KiCad user directory | `~/Documents/KiCad` (macOS/Windows)<br>`~/kicad` (Linux) | `~/Documents/KiCadProjects` |
| `KICAD_SEARCH_PATHS` | Additional directories to search for KiCad projects (comma-separated) | None | `~/pcb,~/Electronics,~/Projects/KiCad` |

### Application Paths

These settings control how the server locates KiCad:

| Environment Variable | Description | Default Value | Example |
|---------------------|-------------|---------------|---------|
| `KICAD_APP_PATH` | Path to the KiCad application | `/Applications/KiCad/KiCad.app` (macOS)<br>`C:\Program Files\KiCad` (Windows)<br>`/usr/share/kicad` (Linux) | `/Applications/KiCad7/KiCad.app` |

## Using a .env File (Recommended)

The recommended way to configure the server is by creating a `.env` file in the project root:

1. Copy the example file:
   ```bash
   cp .env.example .env
   ```

2. Edit the `.env` file:
   ```bash
   vim .env
   ```

3. Add your configuration:
   ```
   # KiCad MCP Server Configuration
   
   # KiCad User Directory (where KiCad stores project files)
   KICAD_USER_DIR=~/Documents/KiCad
   
   # Additional directories to search for KiCad projects (comma-separated)
   KICAD_SEARCH_PATHS=~/pcb,~/Electronics,~/Projects/KiCad
   
   # KiCad application path (needed for opening projects and command-line tools)
   # macOS:
   KICAD_APP_PATH=/Applications/KiCad/KiCad.app
   # Windows:
   # KICAD_APP_PATH=C:\Program Files\KiCad
   # Linux:
   # KICAD_APP_PATH=/usr/share/kicad
   ```

## Directory Structure and Project Discovery

The server automatically searches for KiCad projects in:

1. The **KiCad user directory** (`KICAD_USER_DIR`)
2. Any **additional search paths** specified in `KICAD_SEARCH_PATHS`
3. **Common project locations** (auto-detected, including `~/Documents/PCB`, `~/Electronics`, etc.)

Projects are identified by the `.kicad_pro` file extension. The server recursively searches all configured directories to find KiCad projects.

## Client Configuration

### Claude Desktop Configuration

To configure Claude Desktop to use the KiCad MCP Server:

1. Create or edit the configuration file:

   **macOS**:
   ```bash
   mkdir -p ~/Library/Application\ Support/Claude
   vim ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```

   **Windows**:
   ```
   mkdir -p %APPDATA%\Claude
   notepad %APPDATA%\Claude\claude_desktop_config.json
   ```

2. Add the server configuration:
   ```json
   {
       "mcpServers": {
           "kicad": {
               "command": "/ABSOLUTE/PATH/TO/YOUR/PROJECT/kcaa/venv/bin/python",
               "args": [
                   "/ABSOLUTE/PATH/TO/YOUR/PROJECT/kcaa/main.py"
               ]
           }
       }
   }
   ```

3. For Windows, use the appropriate path format:
   ```json
   {
       "mcpServers": {
           "kicad": {
               "command": "C:\\Path\\To\\Your\\Project\\kcaa\\venv\\Scripts\\python.exe",
               "args": [
                   "C:\\Path\\To\\Your\\Project\\kcaa\\main.py"
               ]
           }
       }
   }
   ```

### Environment Variables in Client Configuration

You can also set environment variables directly in the client configuration:

```json
{
    "mcpServers": {
        "kicad": {
            "command": "/ABSOLUTE/PATH/TO/YOUR/PROJECT/kcaa/venv/bin/python",
            "args": [
                "/ABSOLUTE/PATH/TO/YOUR/PROJECT/kcaa/main.py"
            ],
            "env": {
                "KICAD_SEARCH_PATHS": "/custom/path1,/custom/path2",
                "KICAD_APP_PATH": "/custom/path"
            }
        }
    }
}
```

## Advanced Configuration

### Custom KiCad Extensions

If you need to modify the recognized KiCad file extensions, you can edit `kcaa/config.py`:

```python
# File extensions
KICAD_EXTENSIONS = {
    "project": ".kicad_pro",
    "pcb": ".kicad_pcb",
    "schematic": ".kicad_sch",
    # Add or modify extensions as needed
}
```

### DRC History Configuration

The server stores DRC history to track changes over time. By default, history is stored in:

- macOS/Linux: `~/.kcaa/drc_history/`
- Windows: `%APPDATA%\kcaa\drc_history\`

You can modify this in `kcaa/utils/drc_history.py` if needed.

### Python Path for KiCad Modules

The server attempts to locate and add KiCad's Python modules to the Python path automatically. If this fails, you can modify the search paths in `kcaa/utils/python_path.py`.

## Platform-Specific Configuration

### macOS

On macOS, KiCad is typically installed in `/Applications/KiCad/KiCad.app`. If you have multiple versions or a non-standard installation, set:

```
KICAD_APP_PATH=/path/to/your/KiCad.app
```

### Windows

On Windows, KiCad is typically installed in `C:\Program Files\KiCad`. If you have a different installation location, set:

```
KICAD_APP_PATH=D:\Software\KiCad
```

For paths in the `.env` file on Windows, you can use either:
- Forward slashes: `KICAD_SEARCH_PATHS=C:/Users/Username/Documents/KiCad`
- Escaped backslashes: `KICAD_SEARCH_PATHS=C:\\Users\\Username\\Documents\\KiCad`

### Linux

On Linux, KiCad's location varies by distribution. Common paths include:
- `/usr/share/kicad`
- `/usr/local/share/kicad`
- `/opt/kicad`

Set the appropriate path in your configuration:

```
KICAD_APP_PATH=/opt/kicad
```

## Debugging Configuration Issues

If you're having configuration problems:

1. Run the server:
   ```bash
   python main.py
   ```

2. Verify environment variables are being loaded:
   ```bash
   python -c "import os; print(os.environ.get('KICAD_SEARCH_PATHS', 'Not set'))"
   ```

3. Try absolute paths to eliminate path resolution issues
