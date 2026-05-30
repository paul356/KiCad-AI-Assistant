"""
Bill of Materials (BOM) resources for KiCad projects.
"""

import json
import logging
import os

from fastmcp import FastMCP
import pandas as pd

# Import the helper functions from bom_tools.py to avoid code duplication
from kcaa.tools.bom_tools import analyze_bom_data, parse_bom_file
from kcaa.utils.file_utils import get_project_files

log = logging.getLogger(__name__)


def register_bom_resources(mcp: FastMCP) -> None:
    """Register BOM-related resources with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.resource("kicad://bom/{project_path}")
    def get_bom_resource(project_path: str) -> str:
        """Get a formatted BOM report for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Markdown-formatted BOM report
        """
        print(f"Generating BOM report for project: {project_path}")

        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"

        # Get all project files
        files = get_project_files(project_path)

        # Look for BOM files
        bom_files = {}
        for file_type, file_path in files.items():
            if "bom" in file_type.lower() or file_path.lower().endswith(".csv"):
                bom_files[file_type] = file_path
                print(f"Found potential BOM file: {file_path}")

        if not bom_files:
            print("No BOM files found for project")
            return f"# BOM Report\n\nNo BOM files found for project: {os.path.basename(project_path)}.\n\nExport a BOM from KiCad first, or use the `export_bom_csv` tool to generate one."

        # Format as Markdown report
        project_name = os.path.basename(project_path)[:-10]  # Remove .kicad_pro

        report = f"# Bill of Materials for {project_name}\n\n"

        # Process each BOM file
        for file_type, file_path in bom_files.items():
            try:
                # Parse and analyze the BOM
                bom_data, format_info = parse_bom_file(file_path)

                if not bom_data:
                    report += f"## {file_type}\n\nFailed to parse BOM file: {os.path.basename(file_path)}\n\n"
                    continue

                analysis = analyze_bom_data(bom_data, format_info)

                # Add file section
                report += f"## {file_type.capitalize()}\n\n"
                report += f"**File**: {os.path.basename(file_path)}\n\n"
                report += f"**Format**: {format_info.get('detected_format', 'Unknown')}\n\n"

                # Add summary
                report += "### Summary\n\n"
                report += f"- **Total Components**: {analysis.get('total_component_count', 0)}\n"
                report += f"- **Unique Components**: {analysis.get('unique_component_count', 0)}\n"

                # Add cost if available
                if analysis.get("has_cost_data", False) and "total_cost" in analysis:
                    currency = analysis.get("currency", "USD")
                    currency_symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
                    symbol = currency_symbols.get(currency, "")

                    report += f"- **Estimated Cost**: {symbol}{analysis['total_cost']} {currency}\n"

                report += "\n"

                # Add categories breakdown
                if "categories" in analysis and analysis["categories"]:
                    report += "### Component Categories\n\n"

                    for category, count in analysis["categories"].items():
                        report += f"- **{category}**: {count}\n"

                    report += "\n"

                # Add most common components if available
                if "most_common_values" in analysis and analysis["most_common_values"]:
                    report += "### Most Common Components\n\n"

                    for value, count in analysis["most_common_values"].items():
                        report += f"- **{value}**: {count}\n"

                    report += "\n"

                # Add component table (first 20 items)
                if bom_data:
                    report += "### Component List\n\n"

                    # Try to identify key columns
                    columns = []
                    if format_info.get("header_fields"):
                        # Use a subset of columns for readability
                        preferred_cols = [
                            "Reference",
                            "Value",
                            "Footprint",
                            "Quantity",
                            "Description",
                        ]

                        # Find matching columns (case-insensitive)
                        header_lower = [h.lower() for h in format_info["header_fields"]]
                        for col in preferred_cols:
                            col_lower = col.lower()
                            if col_lower in header_lower:
                                idx = header_lower.index(col_lower)
                                columns.append(format_info["header_fields"][idx])

                        # If we didn't find any preferred columns, use the first 4
                        if not columns and len(format_info["header_fields"]) > 0:
                            columns = format_info["header_fields"][
                                : min(4, len(format_info["header_fields"]))
                            ]

                    # Generate the table header
                    if columns:
                        report += "| " + " | ".join(columns) + " |\n"
                        report += "| " + " | ".join(["---"] * len(columns)) + " |\n"

                        # Add rows (limit to first 20 for readability)
                        for i, component in enumerate(bom_data[:20]):
                            row = []
                            for col in columns:
                                value = component.get(col, "")
                                # Clean up cell content for Markdown table
                                value = str(value).replace("|", "\\|").replace("\n", " ")
                                row.append(value)

                            report += "| " + " | ".join(row) + " |\n"

                        # Add note if there are more components
                        if len(bom_data) > 20:
                            report += f"\n*...and {len(bom_data) - 20} more components*\n"
                    else:
                        report += "*Component table could not be generated - column headers not recognized*\n"

                report += "\n---\n\n"

            except Exception as e:
                print(f"Error processing BOM file {file_path}: {str(e)}")
                report += f"## {file_type}\n\nError processing BOM file: {str(e)}\n\n"

        # Add export instructions
        report += "## How to Export a BOM\n\n"
        report += "To generate a new BOM from your KiCad project:\n\n"
        report += "1. Open your schematic in KiCad\n"
        report += "2. Go to **Tools → Generate BOM**\n"
        report += "3. Choose a BOM plugin and click **Generate**\n"
        report += "4. Save the BOM file in your project directory\n\n"
        report += "Alternatively, use the `export_bom_csv` tool in this MCP server to generate a BOM file.\n"

        return report

    @mcp.resource("kicad://bom/{project_path}/csv")
    def get_bom_csv_resource(project_path: str) -> str:
        """Get a CSV representation of the BOM for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            CSV-formatted BOM data
        """
        print(f"Generating CSV BOM for project: {project_path}")

        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"

        # Get all project files
        files = get_project_files(project_path)

        # Look for BOM files
        bom_files = {}
        for file_type, file_path in files.items():
            if "bom" in file_type.lower() or file_path.lower().endswith(".csv"):
                bom_files[file_type] = file_path
                print(f"Found potential BOM file: {file_path}")

        if not bom_files:
            print("No BOM files found for project")
            return "No BOM files found for project. Export a BOM from KiCad first."

        # Use the first BOM file found
        file_type = next(iter(bom_files))
        file_path = bom_files[file_type]

        try:
            # If it's already a CSV, just return its contents
            if file_path.lower().endswith(".csv"):
                with open(file_path, encoding="utf-8-sig") as f:
                    return f.read()

            # Otherwise, try to parse and convert to CSV
            bom_data, format_info = parse_bom_file(file_path)

            if not bom_data:
                return f"Failed to parse BOM file: {file_path}"

            # Convert to DataFrame and then to CSV
            df = pd.DataFrame(bom_data)
            return df.to_csv(index=False)

        except Exception as e:
            print(f"Error generating CSV from BOM file: {str(e)}")
            return f"Error generating CSV from BOM file: {str(e)}"

    @mcp.resource("kicad://bom/{project_path}/json")
    def get_bom_json_resource(project_path: str) -> str:
        """Get a JSON representation of the BOM for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            JSON-formatted BOM data
        """
        print(f"Generating JSON BOM for project: {project_path}")

        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"

        # Get all project files
        files = get_project_files(project_path)

        # Look for BOM files
        bom_files = {}
        for file_type, file_path in files.items():
            if "bom" in file_type.lower() or file_path.lower().endswith((".csv", ".json")):
                bom_files[file_type] = file_path
                print(f"Found potential BOM file: {file_path}")

        if not bom_files:
            print("No BOM files found for project")
            return json.dumps({"error": "No BOM files found for project"}, indent=2)

        try:
            # Collect data from all BOM files
            result = {"project": os.path.basename(project_path)[:-10], "bom_files": {}}

            for file_type, file_path in bom_files.items():
                # If it's already JSON, parse it directly
                if file_path.lower().endswith(".json"):
                    with open(file_path) as f:
                        try:
                            result["bom_files"][file_type] = json.load(f)
                            continue
                        except Exception:
                            # If JSON parsing fails, fall back to regular parsing
                            log.debug("JSON parsing failed for %s, falling back to regular parsing", file_path)

                # Otherwise parse with our utility
                bom_data, format_info = parse_bom_file(file_path)

                if bom_data:
                    analysis = analyze_bom_data(bom_data, format_info)
                    result["bom_files"][file_type] = {
                        "file": os.path.basename(file_path),
                        "format": format_info,
                        "analysis": analysis,
                        "components": bom_data,
                    }

            return json.dumps(result, indent=2, default=str)

        except Exception as e:
            print(f"Error generating JSON from BOM file: {str(e)}")
            return json.dumps({"error": str(e)}, indent=2)
