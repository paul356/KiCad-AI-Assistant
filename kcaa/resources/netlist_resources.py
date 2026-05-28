"""
Netlist resources for KiCad schematics.
"""

import os

from fastmcp import FastMCP

from kcaa.utils.file_utils import get_project_files
from kcaa.utils.netlist_parser import analyze_netlist, extract_netlist


def register_netlist_resources(mcp: FastMCP) -> None:
    """Register netlist-related resources with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.resource("kicad://netlist/{schematic_path}")
    def get_netlist_resource(schematic_path: str) -> str:
        """Get a formatted netlist report for a KiCad schematic.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)

        Returns:
            Markdown-formatted netlist report
        """
        print(f"Generating netlist report for schematic: {schematic_path}")

        if not os.path.exists(schematic_path):
            return f"Schematic file not found: {schematic_path}"

        try:
            # Extract netlist information
            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                return f"# Netlist Extraction Error\n\nError: {netlist_data['error']}"

            # Analyze the netlist
            analysis_results = analyze_netlist(netlist_data)

            # Format as Markdown report
            schematic_name = os.path.basename(schematic_path)

            report = f"# Netlist Analysis for {schematic_name}\n\n"

            # Overview section
            report += "## Overview\n\n"
            report += f"- **Components**: {netlist_data['component_count']}\n"
            report += f"- **Nets**: {netlist_data['net_count']}\n"

            if "total_pin_connections" in analysis_results:
                report += f"- **Pin Connections**: {analysis_results['total_pin_connections']}\n"

            report += "\n"

            # Component Types section
            if "component_types" in analysis_results and analysis_results["component_types"]:
                report += "## Component Types\n\n"

                for comp_type, count in analysis_results["component_types"].items():
                    report += f"- **{comp_type}**: {count}\n"

                report += "\n"

            # Power Nets section
            if "power_nets" in analysis_results and analysis_results["power_nets"]:
                report += "## Power Nets\n\n"

                for net_name in analysis_results["power_nets"]:
                    report += f"- **{net_name}**\n"

                report += "\n"

            # Components section
            components = netlist_data.get("components", {})
            if components:
                report += "## Component List\n\n"
                report += "| Reference | Type | Value | Footprint |\n"
                report += "|-----------|------|-------|----------|\n"

                # Sort components by reference
                for ref in sorted(components.keys()):
                    component = components[ref]
                    lib_id = component.get("lib_id", "Unknown")
                    value = component.get("value", "")
                    footprint = component.get("footprint", "")

                    report += f"| {ref} | {lib_id} | {value} | {footprint} |\n"

                report += "\n"

            # Nets section (limit to showing first 20 for readability)
            nets = netlist_data.get("nets", {})
            if nets:
                report += "## Net List\n\n"

                # Filter to show only the first 20 nets
                net_items = list(nets.items())[:20]

                for net_name, pins in net_items:
                    report += f"### Net: {net_name}\n\n"

                    if pins:
                        report += "**Connected Pins:**\n\n"
                        for pin in pins:
                            component = pin.get("component", "Unknown")
                            pin_num = pin.get("pin", "Unknown")
                            report += f"- {component}.{pin_num}\n"
                    else:
                        report += "*No connections found*\n"

                    report += "\n"

                if len(nets) > 20:
                    report += f"*...and {len(nets) - 20} more nets*\n\n"

            return report

        except Exception as e:
            return f"# Netlist Extraction Error\n\nError: {str(e)}"

    @mcp.resource("kicad://project_netlist/{project_path}")
    def get_project_netlist_resource(project_path: str) -> str:
        """Get a formatted netlist report for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Markdown-formatted netlist report
        """
        print(f"Generating netlist report for project: {project_path}")

        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"

        # Get the schematic file
        try:
            files = get_project_files(project_path)

            if "schematic" not in files:
                return "Schematic file not found in project"

            schematic_path = files["schematic"]
            print(f"Found schematic file: {schematic_path}")

            # Get the netlist resource for this schematic
            return get_netlist_resource(schematic_path)

        except Exception as e:
            return f"# Netlist Extraction Error\n\nError: {str(e)}"

    @mcp.resource("kicad://component/{schematic_path}/{component_ref}")
    def get_component_resource(schematic_path: str, component_ref: str) -> str:
        """Get detailed information about a specific component and its connections.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            component_ref: Component reference designator (e.g., R1)

        Returns:
            Markdown-formatted component report
        """
        print(f"Generating component report for {component_ref} in schematic: {schematic_path}")

        if not os.path.exists(schematic_path):
            return f"Schematic file not found: {schematic_path}"

        try:
            # Extract netlist information
            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                return f"# Component Analysis Error\n\nError: {netlist_data['error']}"

            # Check if the component exists
            components = netlist_data.get("components", {})
            if component_ref not in components:
                return (
                    f"# Component Not Found\n\nComponent {component_ref} was not found in the schematic.\n\n**Available Components**:\n\n"
                    + "\n".join([f"- {ref}" for ref in sorted(components.keys())])
                )

            component_info = components[component_ref]

            # Format as Markdown report
            report = f"# Component Analysis: {component_ref}\n\n"

            # Component Details section
            report += "## Component Details\n\n"
            report += f"- **Reference**: {component_ref}\n"

            if "lib_id" in component_info:
                report += f"- **Type**: {component_info['lib_id']}\n"

            if "value" in component_info:
                report += f"- **Value**: {component_info['value']}\n"

            if "footprint" in component_info:
                report += f"- **Footprint**: {component_info['footprint']}\n"

            # Add other properties
            if "properties" in component_info:
                for prop_name, prop_value in component_info["properties"].items():
                    report += f"- **{prop_name}**: {prop_value}\n"

            report += "\n"

            # Pins section
            if "pins" in component_info:
                report += "## Pins\n\n"

                for pin in component_info["pins"]:
                    report += f"- **Pin {pin['num']}**: {pin['name']}\n"

                report += "\n"

            # Connections section
            report += "## Connections\n\n"

            nets = netlist_data.get("nets", {})
            connected_nets = []

            for net_name, pins in nets.items():
                # Check if any pin belongs to our component
                for pin in pins:
                    if pin.get("component") == component_ref:
                        connected_nets.append(
                            {
                                "net_name": net_name,
                                "pin": pin.get("pin", "Unknown"),
                                "connections": [
                                    p for p in pins if p.get("component") != component_ref
                                ],
                            }
                        )

            if connected_nets:
                for net in connected_nets:
                    report += f"### Pin {net['pin']} - Net: {net['net_name']}\n\n"

                    if net["connections"]:
                        report += "**Connected To:**\n\n"
                        for conn in net["connections"]:
                            comp = conn.get("component", "Unknown")
                            pin = conn.get("pin", "Unknown")
                            report += f"- {comp}.{pin}\n"
                    else:
                        report += "*No connections*\n"

                    report += "\n"
            else:
                report += "*No connections found for this component*\n\n"

            return report

        except Exception as e:
            return f"# Component Analysis Error\n\nError: {str(e)}"
