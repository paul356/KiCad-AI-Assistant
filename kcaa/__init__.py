"""
KiCad MCP Server.

A Model Context Protocol (MCP) server for KiCad electronic design automation (EDA) files.
"""

from importlib.metadata import PackageNotFoundError, version

from .context import *
from .server import *
from .utils.config import LibraryPathConfig, config

try:
    __version__ = version("kcaa")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
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
