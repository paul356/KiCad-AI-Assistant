"""
Utility functions for detecting and selecting available KiCad API approaches.
"""

import os
import shutil
import subprocess

from kcaa.utils.config import config


def check_for_cli_api() -> bool:
    """Check if KiCad CLI API is available.

    Returns:
        True if KiCad CLI is available, False otherwise
    """
    try:
        # Check if kicad-cli is in PATH
        if config.system == "Windows":
            # On Windows, check for kicad-cli.exe
            kicad_cli = shutil.which("kicad-cli.exe")
        else:
            # On Unix-like systems
            kicad_cli = shutil.which("kicad-cli")

        if kicad_cli:
            # Verify it's a working kicad-cli
            cmd = [kicad_cli, "--version"] if config.system == "Windows" else [kicad_cli, "--version"]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Found working kicad-cli: {kicad_cli}")
                return True

        # Check common installation locations if not found in PATH
        if config.system == "Windows":
            # Common Windows installation paths
            potential_paths = [
                r"C:\Program Files\KiCad\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\bin\kicad-cli.exe",
            ]
        elif config.system == "Darwin":  # macOS
            # Common macOS installation paths
            potential_paths = [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/Applications/KiCad/kicad-cli",
            ]
        else:  # Linux
            # Common Linux installation paths
            potential_paths = [
                "/usr/bin/kicad-cli",
                "/usr/local/bin/kicad-cli",
                "/opt/kicad/bin/kicad-cli",
            ]

        # Check each potential path
        for path in potential_paths:
            if os.path.exists(path) and os.access(path, os.X_OK):
                print(f"Found kicad-cli at common location: {path}")
                return True

        print("KiCad CLI API is not available")
        return False

    except Exception as e:
        print(f"Error checking for KiCad CLI API: {str(e)}")
        return False
