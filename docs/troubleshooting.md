# Troubleshooting Guide

This guide helps you troubleshoot common issues with the KiCad MCP Server.

## Server Setup Issues

### Server Not Starting

**Symptoms:**
- Error messages when running `python main.py`
- Server crashes immediately

**Possible Causes and Solutions:**

1. **Missing Dependencies**
   - **Error:** `ModuleNotFoundError: No module named 'mcp'`
   - **Solution:** Ensure you have installed the required packages
     ```bash
     pip install -r requirements.txt
     ```

2. **Python Version**
   - **Error:** Syntax errors or unsupported features
   - **Solution:** Verify you're using Python 3.10 or higher
     ```bash
     python --version
     ```

3. **Configuration Issues**
   - **Error:** `FileNotFoundError` or errors related to paths
   - **Solution:** Check your `.env` file and ensure all paths exist

4. **Permission Issues**
   - **Error:** `PermissionError: [Errno 13] Permission denied`
   - **Solution:** Ensure you have the necessary permissions for all directories and files

## MCP Client Integration Issues

### Server Not Appearing in Client

**Symptoms:**
- Server not showing in Claude Desktop tools dropdown
- Unable to access KiCad tools in the client

**Possible Causes and Solutions:**

1. **Configuration File Issues**
   - **Problem:** Incorrect or missing configuration
   - **Solution:** Check your client configuration file:
     
     For Claude Desktop (macOS):
     ```bash
     cat ~/Library/Application\ Support/Claude/claude_desktop_config.json
     ```
     
     For Claude Desktop (Windows):
     ```bash
     type %APPDATA%\Claude\claude_desktop_config.json
     ```
     
     Ensure it contains the correct server configuration:
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

2. **Working Directory Issues**
   - **Problem:** The server can't find files due to working directory issues
   - **Solution:** Always use absolute paths in your configuration
     
     Replace:
     ```json
     "args": ["main.py"]
     ```
     
     With:
     ```json
     "args": ["/absolute/path/to/main.py"]
     ```

3. **Client Needs Restart**
   - **Problem:** Changes to configuration not applied
   - **Solution:** Close and reopen your MCP client

4. **MCP Protocol Version Mismatch**
   - **Problem:** Client and server using incompatible protocol versions
   - **Solution:** Update both the client and server to compatible versions

## Working with KiCad

### KiCad Python Modules Not Found

**Symptoms:**
- Warning messages about missing KiCad Python modules
- Limited functionality for PCB analysis, DRC, etc.

**Possible Causes and Solutions:**

1. **KiCad Not Installed**
   - **Problem:** KiCad installation not found
   - **Solution:** Install KiCad from [kicad.org](https://www.kicad.org/download/)

2. **Non-standard KiCad Installation**
   - **Problem:** KiCad installed in a non-standard location
   - **Solution:** Set the `KICAD_APP_PATH` environment variable in your `.env` file:
     ```
     KICAD_APP_PATH=/path/to/KiCad/KiCad.app
     ```

3. **Python Path Issues**
   - **Problem:** KiCad Python modules not in Python path
   - **Solution:** Check server logs for Python path setup errors

### Unable to Open KiCad Projects

**Symptoms:**
- Error when trying to open a KiCad project
- KiCad doesn't launch when requested

**Possible Causes and Solutions:**

1. **KiCad Not Found**
   - **Problem:** KiCad executable not found
   - **Solution:** Set the correct `KICAD_APP_PATH` in your `.env` file

2. **Project Path Issues**
   - **Problem:** Project file not found
   - **Solution:** Double-check the path to your KiCad project file
   - Ensure the path is an absolute path
   - Ensure the file exists and has a `.kicad_pro` extension

3. **Permission Issues**
   - **Problem:** Insufficient permissions to launch KiCad
   - **Solution:** Check file and application permissions

## Project Discovery Issues

### KiCad Projects Not Found

**Symptoms:**
- No projects listed when asking for KiCad projects
- Missing specific projects that you know exist

**Possible Causes and Solutions:**

1. **Search Path Issues**
   - **Problem:** Server not looking in the right directories
   - **Solution:** Configure search paths in your `.env` file:
     ```
     KICAD_SEARCH_PATHS=~/pcb,~/Electronics,~/Projects/KiCad
     ```

2. **KiCad User Directory Override**
   - **Problem:** Custom KiCad user directory not being searched
   - **Solution:** Set `KICAD_USER_DIR` in your `.env` file:
     ```
     KICAD_USER_DIR=~/Documents/KiCadProjects
     ```

3. **File Extension Issues**
   - **Problem:** Projects not recognized due to file extensions
   - **Solution:** Ensure your KiCad projects have the `.kicad_pro` extension

## Debugging and Logging

### Checking Logs

To diagnose issues, check the server logs:

1. **Claude Desktop Logs (macOS)**
   - Server logs:
     ```bash
     tail -n 20 -F ~/Library/Logs/Claude/mcp-server-kicad.log
     ```
   - General MCP logs:
     ```bash
     tail -n 20 -F ~/Library/Logs/Claude/mcp.log
     ```

2. **Claude Desktop Logs (Windows)**
   - Check logs in:
     ```
     %APPDATA%\Claude\Logs\
     ```

## DRC and Export Feature Issues

### DRC Checks Failing

**Symptoms:**
- Error messages when running DRC checks
- Incomplete DRC results

**Possible Causes and Solutions:**

1. **KiCad CLI Tools Not Found**
   - **Problem:** Command-line tools not available
   - **Solution:** Ensure you have a complete KiCad installation with CLI tools

2. **PCB File Issues**
   - **Problem:** Invalid or corrupted PCB file
   - **Solution:** Verify the PCB file can be opened in KiCad

### PCB Thumbnail Generation Failing

**Symptoms:**
- Unable to generate PCB thumbnails
- Error messages about missing modules

**Possible Causes and Solutions:**

1. **Missing KiCad Python Modules**
   - **Problem:** Unable to import pcbnew module
   - **Solution:** The server will try alternative methods, but functionality may be limited

2. **File Path Issues**
   - **Problem:** PCB file not found
   - **Solution:** Ensure you're using absolute paths and the PCB file exists

## Environment-Specific Issues

### macOS Issues

1. **Permissions**
   - **Problem:** Permission denied when accessing files
   - **Solution:** Ensure Terminal has full disk access in System Preferences > Security & Privacy

2. **Python Environment**
   - **Problem:** System Python vs. Homebrew Python confusion
   - **Solution:** Specify the full path to the Python interpreter in your client configuration

### Windows Issues

1. **Path Separator Issues**
   - **Problem:** Backslashes in Windows paths causing issues
   - **Solution:** Use forward slashes (/) in all paths, or double backslashes (\\\\)

2. **Command Execution**
   - **Problem:** Issues launching KiCad from the server
   - **Solution:** Ensure the KICAD_APP_PATH is set correctly in your `.env` file

### Linux Issues

1. **KiCad Installation Location**
   - **Problem:** KiCad installed in non-standard location
   - **Solution:** Set KICAD_APP_PATH to your KiCad installation location

2. **Permission Issues**
   - **Problem:** Unable to access certain files
   - **Solution:** Check file permissions with `ls -la` and adjust with `chmod` if needed

## Still Having Issues?

If you're still experiencing problems:

1. Use the MCP Inspector for direct server testing:
   ```bash
   npx @modelcontextprotocol/inspector uv --directory . run main.py
   ```

2. Open an issue on GitHub with:
   - A clear description of the problem
   - Steps to reproduce
   - Error messages or logs
   - Your environment details (OS, Python version, KiCad version)
