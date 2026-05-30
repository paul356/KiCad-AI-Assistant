#!/usr/bin/env python3
"""
KiCad MCP Server - A Model Context Protocol server for KiCad on macOS.
This server allows Claude and other MCP clients to interact with KiCad projects.
"""
import os
import sys
import logging

# Import the config singleton — loads .env at import time if present
from kcaa.utils.config import config
from kcaa.server import main as server_main

# --- Setup Logging ---
# Log file goes to the versioned kcaa data directory (e.g. ~/.config/kicad/10.0/kcaa/)
log_file = os.path.join(config.get_kcaa_data_dir(), 'kcaa_server_standalone.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [PID:%(process)d] - %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='w'),
        logging.StreamHandler()
    ]
)
# ---------------------

logging.info("--- Server Starting --- ")
logging.info(f"Log file: {log_file}")
logging.info(f"KICAD_USER_DIR: {config.kicad_user_dir}")
logging.info(f"ADDITIONAL_SEARCH_PATHS: {config.additional_search_paths}")

if __name__ == "__main__":
    try:
        logging.info(f"Starting KiCad MCP server process") 

        # Print search paths from config
        logging.info(f"Using KiCad user directory: {config.kicad_user_dir}")
        if config.additional_search_paths:
            logging.info(f"Additional search paths: {', '.join(config.additional_search_paths)}")
        else:
            logging.info(f"No additional search paths configured")

        # Run server
        logging.info(f"Running server with stdio transport")
        import asyncio
        asyncio.run(server_main())
    except Exception as e:
        logging.exception(f"Unhandled exception in main")
        raise
