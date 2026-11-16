"""
File handling utilities for KiCad MCP Server.
"""

import json
import os
from typing import Any

from kcaa.utils.project_resolution import find_project_file, project_name


def get_project_files(project_path: str) -> dict[str, str]:
    """Get all files related to a KiCad project.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Dictionary mapping file types to file paths
    """
    from kcaa.utils.config import config

    pro = find_project_file(project_path)
    base = pro or project_path
    project_dir = os.path.dirname(base)
    project_stem = project_name(base)
    if not project_stem:
        return {"project": base}

    files = {}

    # Check for standard KiCad files
    for file_type, extension in config.kicad_extensions.items():
        if file_type == "project":
            # Report the resolved project file, not an arbitrary input file.
            files[file_type] = base
            continue

        file_path = os.path.join(project_dir, f"{project_stem}{extension}")
        if os.path.exists(file_path):
            files[file_type] = file_path

    # Check for data files
    try:
        for ext in config.data_extensions:
            for file in os.listdir(project_dir):
                if file.startswith(project_stem) and file.endswith(ext):
                    # Extract the type from filename (e.g., project_name-bom.csv -> bom)
                    file_type = file[len(project_stem) :].strip("-_")
                    file_type = file_type.split(".")[0]
                    if not file_type:
                        file_type = ext[1:]  # Use extension if no specific type

                    files[file_type] = os.path.join(project_dir, file)
    except (OSError, FileNotFoundError):
        # Directory doesn't exist or can't be accessed - return what we have
        pass

    return files


def load_project_json(project_path: str) -> dict[str, Any] | None:
    """Load and parse a KiCad project file.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Parsed JSON data or None if parsing failed
    """
    try:
        with open(project_path) as f:
            return json.load(f)
    except Exception:
        return None
