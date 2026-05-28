"""
KiCad-specific utility functions.
"""

import logging  # Import logging
import os
import subprocess
import sys  # Add sys import
from typing import Any

from kcaa.utils.config import config

# Get PID for logging - Removed, handled by logging config
# _PID = os.getpid()


def find_kicad_projects() -> list[dict[str, Any]]:
    """Find KiCad projects in the user's directory.

    Returns:
        List of dictionaries with project information
    """
    projects = []
    logging.info("Attempting to find KiCad projects...")  # Log start
    # Search directories to look for KiCad projects
    raw_search_dirs = [config.kicad_user_dir] + config.additional_search_paths
    logging.info(f"Raw KICAD_USER_DIR: '{config.kicad_user_dir}'")
    logging.info(f"Raw ADDITIONAL_SEARCH_PATHS: {config.additional_search_paths}")
    logging.info(f"Raw search list before expansion: {raw_search_dirs}")

    expanded_search_dirs = []
    for raw_dir in raw_search_dirs:
        expanded_dir = os.path.expanduser(raw_dir)  # Expand ~ and ~user
        if expanded_dir not in expanded_search_dirs:
            expanded_search_dirs.append(expanded_dir)
        else:
            logging.info(f"Skipping duplicate expanded path: {expanded_dir}")

    logging.info(f"Expanded search directories: {expanded_search_dirs}")

    for search_dir in expanded_search_dirs:
        if not os.path.exists(search_dir):
            logging.warning(
                f"Expanded search directory does not exist: {search_dir}"
            )  # Use warning level
            continue

        logging.info(f"Scanning expanded directory: {search_dir}")
        # Use followlinks=True to follow symlinks if needed
        for root, _, files in os.walk(search_dir, followlinks=True):
            for file in files:
                if file.endswith(config.kicad_extensions["project"]):
                    project_path = os.path.join(root, file)
                    # Check if it's a real file and not a broken symlink
                    if not os.path.isfile(project_path):
                        logging.info(f"Skipping non-file/broken symlink: {project_path}")
                        continue

                    try:
                        # Attempt to get modification time to ensure file is accessible
                        mod_time = os.path.getmtime(project_path)
                        rel_path = os.path.relpath(project_path, search_dir)
                        project_name = get_project_name_from_path(project_path)

                        logging.info(f"Found accessible KiCad project: {project_path}")
                        projects.append(
                            {
                                "name": project_name,
                                "path": project_path,
                                "relative_path": rel_path,
                                "modified": mod_time,
                            }
                        )
                    except OSError as e:
                        logging.error(
                            f"Error accessing project file {project_path}: {e}"
                        )  # Use error level
                        continue  # Skip if we can't access it

    logging.info(f"Found {len(projects)} KiCad projects after scanning.")
    return projects


def get_project_name_from_path(project_path: str) -> str:
    """Extract the project name from a .kicad_pro file path.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Project name without extension
    """
    basename = os.path.basename(project_path)
    return basename[: -len(config.kicad_extensions["project"])]


def open_kicad_project(project_path: str) -> dict[str, Any]:
    """Open a KiCad project using the KiCad application.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Dictionary with result information
    """
    if not os.path.exists(project_path):
        return {"success": False, "error": f"Project not found: {project_path}"}

    try:
        cmd = []
        if sys.platform == "darwin":  # macOS
            # On MacOS, use the 'open' command to open the project in KiCad
            cmd = ["open", "-a", config.kicad_app_path, project_path]
        elif sys.platform == "linux":  # Linux
            # On Linux, use 'xdg-open'
            cmd = ["xdg-open", project_path]
        else:
            # Fallback or error for unsupported OS
            return {"success": False, "error": f"Unsupported operating system: {sys.platform}"}

        result = subprocess.run(cmd, capture_output=True, text=True)

        return {
            "success": result.returncode == 0,
            "command": " ".join(cmd),
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
