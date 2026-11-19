"""
Analysis and validation tools for KiCad projects.
"""

import os
from typing import Any

from fastmcp import FastMCP

from kcaa.utils.atomic_io import read_text_utf8
from kcaa.utils.file_utils import get_project_files


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register analysis and validation tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def validate_project(project_path: str) -> dict[str, Any]:
        """Basic validation of a KiCad project."""
        if not os.path.exists(project_path):
            return {"valid": False, "error": f"Project not found: {project_path}"}

        issues = []
        files = get_project_files(project_path)

        # Check for essential files
        if "pcb" not in files:
            issues.append("Missing PCB layout file")

        if "schematic" not in files:
            issues.append("Missing schematic file")

        # Validate project file
        try:
            import json

            json.loads(read_text_utf8(project_path))
        except json.JSONDecodeError:
            issues.append("Invalid project file format (JSON parsing error)")
        except Exception as e:
            issues.append(f"Error reading project file: {str(e)}")

        return {
            "valid": len(issues) == 0,
            "path": project_path,
            "issues": issues if issues else None,
            "files_found": list(files.keys()),
        }
