"""
KiCad MCP Server.

A Model Context Protocol (MCP) server for KiCad electronic design automation (EDA) files.
"""

from .utils.config import config, LibraryPathConfig
from .context import *
from .server import *

__version__ = "0.1.0"
__author__ = "Lama Al Rajih"
__description__ = "Model Context Protocol server for KiCad on Mac, Windows, and Linux"

__all__ = [
    # Package metadata
    "__version__",
    "__author__",
    "__description__",
    # Configuration
    "config",
    "LibraryPathConfig",
    # Server creation / shutdown helpers
    "create_server",
    "add_cleanup_handler",
    "run_cleanup_handlers",
    "shutdown_server",
    # Lifespan / context helpers
    "kicad_lifespan",
    "KiCadAppContext",
]
