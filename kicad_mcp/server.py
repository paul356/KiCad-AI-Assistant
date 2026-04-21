"""
MCP server creation and configuration.

Two server profiles are supported, selected via the KICAD_MCP_PROFILE environment variable:

  full   (default) — registers all tools, resources, and prompts. Intended for
                     standalone MCP clients such as Claude Desktop.

  plugin            — registers only the skip-based schematic editing tools
                     (netlist, symbol, component, wire/junction). No resources,
                     no prompts, no kicad-cli-dependent tools. Intended for the
                     KiCad plugin integration where the plugin supplies project
                     context directly and the LLM drives editing via tool calls.
"""
import atexit
import os
import signal
import logging
import functools
from typing import Callable, Literal
from fastmcp import FastMCP

# Plugin profile tools — always imported (skip-based, no kicad-cli dependency)
from kicad_mcp.tools.netlist_tools import register_netlist_tools
from kicad_mcp.tools.symbol_tools import register_symbol_tools
from kicad_mcp.tools.component_edit_tools import register_component_edit_tools
from kicad_mcp.tools.wire_edit_tools import register_wire_edit_tools

# Full-profile imports are deferred inside _register_full_profile() to avoid
# loading kicad-cli-dependent modules when running in plugin mode.

from kicad_mcp.context import kicad_lifespan

ServerProfile = Literal["full", "plugin"]

# Track cleanup handlers
cleanup_handlers = []

# Flag to track whether we're already in shutdown process
_shutting_down = False

def add_cleanup_handler(handler: Callable) -> None:
    """Register a function to be called during cleanup.
    
    Args:
        handler: Function to call during cleanup
    """
    cleanup_handlers.append(handler)

def run_cleanup_handlers() -> None:
    """Run all registered cleanup handlers."""
    global _shutting_down

    # Prevent running cleanup handlers multiple times
    if _shutting_down:
        return

    _shutting_down = True
    logging.info("Running cleanup handlers...")

    for handler in cleanup_handlers:
        try:
            handler()
            logging.info("Cleanup handler %s completed successfully", handler.__name__)
        except Exception as e:
            logging.error("Error in cleanup handler %s: %s", handler.__name__, str(e), exc_info=True)


def register_signal_handlers(server: FastMCP) -> None:
    """Register handlers for system signals to ensure clean shutdown.

    Args:
        server: The FastMCP server instance
    """
    def handle_exit_signal(signum, frame):
        logging.info("Received signal %s, initiating shutdown...", signum)
        run_cleanup_handlers()
        # os._exit skips atexit handlers and buffered stdio — necessary here
        # because the stdio MCP transport may be blocking on reads.
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
            logging.info("Registered handler for signal %s", sig)
        except (ValueError, AttributeError) as e:
            logging.error("Could not register handler for signal %s: %s", sig, str(e))


def _register_plugin_profile(mcp: FastMCP) -> None:
    """Register only the skip-based schematic editing tools for the plugin profile."""
    logging.info("Registering plugin profile: netlist, symbol, component, wire tools only")
    register_netlist_tools(mcp)
    register_symbol_tools(mcp)
    register_component_edit_tools(mcp)
    register_wire_edit_tools(mcp)


def _register_full_profile(mcp: FastMCP) -> None:
    """Register all tools, resources, and prompts for the full profile.

    Full-profile imports are deferred here so that kicad-cli-dependent modules
    are never loaded when the server runs in plugin mode.
    """
    logging.info("Registering full profile: all tools, resources, and prompts")

    from kicad_mcp.resources.projects import register_project_resources
    from kicad_mcp.resources.files import register_file_resources
    from kicad_mcp.resources.drc_resources import register_drc_resources
    from kicad_mcp.resources.bom_resources import register_bom_resources
    from kicad_mcp.resources.netlist_resources import register_netlist_resources
    from kicad_mcp.resources.pattern_resources import register_pattern_resources
    from kicad_mcp.tools.project_tools import register_project_tools
    from kicad_mcp.tools.analysis_tools import register_analysis_tools
    from kicad_mcp.tools.export_tools import register_export_tools
    from kicad_mcp.tools.drc_tools import register_drc_tools
    from kicad_mcp.tools.bom_tools import register_bom_tools
    from kicad_mcp.tools.pattern_tools import register_pattern_tools
    from kicad_mcp.prompts.templates import register_prompts
    from kicad_mcp.prompts.drc_prompt import register_drc_prompts
    from kicad_mcp.prompts.bom_prompts import register_bom_prompts
    from kicad_mcp.prompts.pattern_prompts import register_pattern_prompts

    # Resources
    register_project_resources(mcp)
    register_file_resources(mcp)
    register_drc_resources(mcp)
    register_bom_resources(mcp)
    register_netlist_resources(mcp)
    register_pattern_resources(mcp)

    # Tools
    register_project_tools(mcp)
    register_analysis_tools(mcp)
    register_export_tools(mcp)
    register_drc_tools(mcp)
    register_bom_tools(mcp)
    register_netlist_tools(mcp)
    register_pattern_tools(mcp)
    register_symbol_tools(mcp)
    register_component_edit_tools(mcp)
    register_wire_edit_tools(mcp)

    # Prompts
    register_prompts(mcp)
    register_drc_prompts(mcp)
    register_bom_prompts(mcp)
    register_pattern_prompts(mcp)


def create_server(profile: ServerProfile = "full") -> FastMCP:
    """Create and configure the KiCad MCP server.

    Args:
        profile: Server capability profile.
            "full"   — all tools, resources, and prompts (default; for Claude Desktop etc.)
            "plugin" — plugin-facing profile: 26 skip-based schematic editing tools only,
                       no resources, no prompts, no kicad-cli-dependent tools.
                       Override via KICAD_MCP_PROFILE env var.
    """
    # Allow env var to override the profile argument; strip whitespace/case variants
    raw = os.environ.get("KICAD_MCP_PROFILE", profile).strip().lower()
    if raw not in ("full", "plugin"):
        logging.warning("Unknown KICAD_MCP_PROFILE '%s'; falling back to 'full'", raw)
        raw = "full"
    profile = "plugin" if raw == "plugin" else "full"

    logging.info(f"Initializing KiCad MCP server (profile={profile})")

    kicad_modules_available = False
    logging.info("KiCad Python module setup removed; relying on kicad-cli for external operations.")

    lifespan_factory = functools.partial(kicad_lifespan, kicad_modules_available=kicad_modules_available)

    mcp = FastMCP("KiCad", lifespan=lifespan_factory)
    logging.info("Created FastMCP server instance with lifespan management")

    if profile == "plugin":
        _register_plugin_profile(mcp)
    else:
        _register_full_profile(mcp)

    # Register signal handlers and cleanup
    register_signal_handlers(mcp)
    atexit.register(run_cleanup_handlers)
    
    # Add specific cleanup handlers
    add_cleanup_handler(lambda: logging.info(f"KiCad MCP server shutdown complete"))

    # Add temp directory cleanup
    def cleanup_temp_dirs():
        """Clean up any temporary directories created by the server."""
        import shutil
        from kicad_mcp.utils.temp_dir_manager import get_temp_dirs
        
        temp_dirs = get_temp_dirs()
        logging.info(f"Cleaning up {len(temp_dirs)} temporary directories")
        
        for temp_dir in temp_dirs:
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    logging.info(f"Removed temporary directory: {temp_dir}")
            except Exception as e:
                logging.error(f"Error cleaning up temporary directory {temp_dir}: {str(e)}")
    
    add_cleanup_handler(cleanup_temp_dirs)
    
    logging.info(f"Server initialization complete")
    return mcp


def setup_signal_handlers() -> None:
    """Setup signal handlers for graceful shutdown."""
    # Signal handlers are set up in register_signal_handlers
    pass


def cleanup_handler() -> None:
    """Handle cleanup during shutdown."""
    run_cleanup_handlers()


def setup_logging() -> None:
    """Configure logging for the server."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


def main() -> None:
    """Start the KiCad MCP server (blocking)."""
    setup_logging()
    logging.info("Starting KiCad MCP server...")
    
    server = create_server()

    # Read transport from environment variable; default to 'stdio'
    # Supported values: 'stdio', 'streamable-http', 'sse'
    transport = os.environ.get('MCP_TRANSPORT', 'stdio')
    logging.info(f"Using transport: {transport}")

    transport_kwargs: dict = {}
    if transport in ("streamable-http", "sse"):
        transport_kwargs["host"] = os.environ.get("MCP_HOST", "127.0.0.1")
        port_str = os.environ.get("MCP_PORT", "8000")
        try:
            transport_kwargs["port"] = int(port_str)
        except ValueError:
            logging.warning("Invalid MCP_PORT '%s'; defaulting to 8000", port_str)
            transport_kwargs["port"] = 8000

    try:
        server.run(transport=transport, **transport_kwargs)
    except KeyboardInterrupt:
        logging.info("Server interrupted by user")
    except Exception as e:
        logging.error(f"Server error: {e}")
    finally:
        logging.info("Server shutdown complete")


if __name__ == "__main__":
    main()
