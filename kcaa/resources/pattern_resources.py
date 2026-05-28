"""
Circuit pattern recognition resources for KiCad schematics.
"""
import os
from fastmcp import FastMCP

from kcaa.utils.file_utils import get_project_files
from kcaa.utils.netlist_parser import extract_netlist
from kcaa.utils.pattern_recognition import (
    identify_power_supplies,
    identify_amplifiers,
    identify_filters,
    identify_oscillators,
    identify_digital_interfaces,
    identify_microcontrollers,
    identify_sensor_interfaces
)


def register_pattern_resources(mcp: FastMCP) -> None:
    """Register circuit pattern recognition resources with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.resource("kicad://patterns/{schematic_path}")
    def get_circuit_patterns_resource(schematic_path: str) -> str:
        """Get a formatted report of identified circuit patterns in a KiCad schematic.
        
        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            
        Returns:
            Markdown-formatted circuit pattern report
        """
        if not os.path.exists(schematic_path):
            return f"Schematic file not found: {schematic_path}"
        
        try:
            # Extract netlist information
            netlist_data = extract_netlist(schematic_path)
            
            if "error" in netlist_data:
                return f"# Circuit Pattern Analysis Error\n\nError: {netlist_data['error']}"
            
            components = netlist_data.get("components", {})
            nets = netlist_data.get("nets", {})
            
            # Identify circuit patterns
            power_supplies = identify_power_supplies(components, nets)
            amplifiers = identify_amplifiers(components, nets)
            filters = identify_filters(components, nets)
            oscillators = identify_oscillators(components, nets)
            digital_interfaces = identify_digital_interfaces(components, nets)
            microcontrollers = identify_microcontrollers(components)
            sensor_interfaces = identify_sensor_interfaces(components, nets)
            
            # Format as Markdown report
            schematic_name = os.path.basename(schematic_path)
            
            report = f"# Circuit Pattern Analysis for {schematic_name}\n\n"
            
            # Add summary
            total_patterns = (
                len(power_supplies) + 
                len(amplifiers) + 
                len(filters) + 
                len(oscillators) + 
                len(digital_interfaces) + 
                len(microcontrollers) + 
                len(sensor_interfaces)
            )
            
            report += f"## Summary\n\n"
            report += f"- **Total Components**: {netlist_data['component_count']}\n"
            report += f"- **Total Circuit Patterns Identified**: {total_patterns}\n\n"
            
            report += "### Pattern Types\n\n"
            report += f"- **Power Supply Circuits**: {len(power_supplies)}\n"
            report += f"- **Amplifier Circuits**: {len(amplifiers)}\n"
            report += f"- **Filter Circuits**: {len(filters)}\n"
            report += f"- **Oscillator Circuits**: {len(oscillators)}\n"
            report += f"- **Digital Interface Circuits**: {len(digital_interfaces)}\n"
            report += f"- **Microcontroller Circuits**: {len(microcontrollers)}\n"
            report += f"- **Sensor Interface Circuits**: {len(sensor_interfaces)}\n\n"
            
            # Add detailed sections
            if power_supplies:
                report += "## Power Supply Circuits\n\n"
                for i, ps in enumerate(power_supplies, 1):
                    ps_type = ps.get("type", "Unknown")
                    ps_subtype = ps.get("subtype", "")
                    
                    report += f"### Power Supply {i}: {ps_subtype.upper() if ps_subtype else ps_type.title()}\n\n"
                    
                    if ps_type == "linear_regulator":
                        report += f"- **Type**: Linear Voltage Regulator\n"
                        report += f"- **Subtype**: {ps_subtype}\n"
                        report += f"- **Main Component**: {ps.get('main_component', 'Unknown')}\n"
                        report += f"- **Value**: {ps.get('value', 'Unknown')}\n"
                        report += f"- **Output Voltage**: {ps.get('output_voltage', 'Unknown')}\n"
                    elif ps_type == "switching_regulator":
                        report += f"- **Type**: Switching Voltage Regulator\n"
                        report += f"- **Topology**: {ps_subtype.title() if ps_subtype else 'Unknown'}\n"
                        report += f"- **Main Component**: {ps.get('main_component', 'Unknown')}\n"
                        report += f"- **Inductor**: {ps.get('inductor', 'Unknown')}\n"
                        report += f"- **Value**: {ps.get('value', 'Unknown')}\n"
                    
                    report += "\n"
            
            if amplifiers:
                report += "## Amplifier Circuits\n\n"
                for i, amp in enumerate(amplifiers, 1):
                    amp_type = amp.get("type", "Unknown")
                    amp_subtype = amp.get("subtype", "")
                    
                    report += f"### Amplifier {i}: {amp_subtype.upper() if amp_subtype else amp_type.title()}\n\n"
                    
                    if amp_type == "operational_amplifier":
                        report += f"- **Type**: Operational Amplifier\n"
                        report += f"- **Subtype**: {amp_subtype.replace('_', ' ').title() if amp_subtype else 'General Purpose'}\n"
                        report += f"- **Component**: {amp.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {amp.get('value', 'Unknown')}\n"
                    elif amp_type == "transistor_amplifier":
                        report += f"- **Type**: Transistor Amplifier\n"
                        report += f"- **Transistor Type**: {amp_subtype}\n"
                        report += f"- **Component**: {amp.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {amp.get('value', 'Unknown')}\n"
                    elif amp_type == "audio_amplifier_ic":
                        report += f"- **Type**: Audio Amplifier IC\n"
                        report += f"- **Component**: {amp.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {amp.get('value', 'Unknown')}\n"
                    
                    report += "\n"
            
            if filters:
                report += "## Filter Circuits\n\n"
                for i, filt in enumerate(filters, 1):
                    filt_type = filt.get("type", "Unknown")
                    filt_subtype = filt.get("subtype", "")
                    
                    report += f"### Filter {i}: {filt_subtype.upper() if filt_subtype else filt_type.title()}\n\n"
                    
                    if filt_type == "passive_filter":
                        report += f"- **Type**: Passive Filter\n"
                        report += f"- **Topology**: {filt_subtype.replace('_', ' ').upper() if filt_subtype else 'Unknown'}\n"
                        report += f"- **Components**: {', '.join(filt.get('components', []))}\n"
                    elif filt_type == "active_filter":
                        report += f"- **Type**: Active Filter\n"
                        report += f"- **Main Component**: {filt.get('main_component', 'Unknown')}\n"
                        report += f"- **Value**: {filt.get('value', 'Unknown')}\n"
                    elif filt_type == "crystal_filter":
                        report += f"- **Type**: Crystal Filter\n"
                        report += f"- **Component**: {filt.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {filt.get('value', 'Unknown')}\n"
                    elif filt_type == "ceramic_filter":
                        report += f"- **Type**: Ceramic Filter\n"
                        report += f"- **Component**: {filt.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {filt.get('value', 'Unknown')}\n"
                    
                    report += "\n"
            
            if oscillators:
                report += "## Oscillator Circuits\n\n"
                for i, osc in enumerate(oscillators, 1):
                    osc_type = osc.get("type", "Unknown")
                    osc_subtype = osc.get("subtype", "")
                    
                    report += f"### Oscillator {i}: {osc_subtype.upper() if osc_subtype else osc_type.title()}\n\n"
                    
                    if osc_type == "crystal_oscillator":
                        report += f"- **Type**: Crystal Oscillator\n"
                        report += f"- **Component**: {osc.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {osc.get('value', 'Unknown')}\n"
                        report += f"- **Frequency**: {osc.get('frequency', 'Unknown')}\n"
                        report += f"- **Has Load Capacitors**: {'Yes' if osc.get('has_load_capacitors', False) else 'No'}\n"
                    elif osc_type == "oscillator_ic":
                        report += f"- **Type**: Oscillator IC\n"
                        report += f"- **Component**: {osc.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {osc.get('value', 'Unknown')}\n"
                        report += f"- **Frequency**: {osc.get('frequency', 'Unknown')}\n"
                    elif osc_type == "rc_oscillator":
                        report += f"- **Type**: RC Oscillator\n"
                        report += f"- **Subtype**: {osc_subtype.replace('_', ' ').title() if osc_subtype else 'Unknown'}\n"
                        report += f"- **Component**: {osc.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {osc.get('value', 'Unknown')}\n"
                    
                    report += "\n"
            
            if digital_interfaces:
                report += "## Digital Interface Circuits\n\n"
                for i, iface in enumerate(digital_interfaces, 1):
                    iface_type = iface.get("type", "Unknown")
                    
                    report += f"### Interface {i}: {iface_type.replace('_', ' ').upper()}\n\n"
                    report += f"- **Type**: {iface_type.replace('_', ' ').title()}\n"
                    
                    signals = iface.get("signals_found", [])
                    if signals:
                        report += f"- **Signals Found**: {', '.join(signals)}\n"
                    
                    report += "\n"
            
            if microcontrollers:
                report += "## Microcontroller Circuits\n\n"
                for i, mcu in enumerate(microcontrollers, 1):
                    mcu_type = mcu.get("type", "Unknown")
                    
                    if mcu_type == "microcontroller":
                        report += f"### Microcontroller {i}: {mcu.get('model', mcu.get('family', 'Unknown'))}\n\n"
                        report += f"- **Type**: Microcontroller\n"
                        report += f"- **Family**: {mcu.get('family', 'Unknown')}\n"
                        if "model" in mcu:
                            report += f"- **Model**: {mcu['model']}\n"
                        report += f"- **Component**: {mcu.get('component', 'Unknown')}\n"
                        if "common_usage" in mcu:
                            report += f"- **Common Usage**: {mcu['common_usage']}\n"
                        if "features" in mcu:
                            report += f"- **Features**: {mcu['features']}\n"
                    elif mcu_type == "development_board":
                        report += f"### Development Board {i}: {mcu.get('board_type', 'Unknown')}\n\n"
                        report += f"- **Type**: Development Board\n"
                        report += f"- **Board Type**: {mcu.get('board_type', 'Unknown')}\n"
                        report += f"- **Component**: {mcu.get('component', 'Unknown')}\n"
                        report += f"- **Value**: {mcu.get('value', 'Unknown')}\n"
                    
                    report += "\n"
            
            if sensor_interfaces:
                report += "## Sensor Interface Circuits\n\n"
                for i, sensor in enumerate(sensor_interfaces, 1):
                    sensor_type = sensor.get("type", "Unknown")
                    sensor_subtype = sensor.get("subtype", "")
                    
                    report += f"### Sensor {i}: {sensor_subtype.title() + ' ' if sensor_subtype else ''}{sensor_type.replace('_', ' ').title()}\n\n"
                    report += f"- **Type**: {sensor_type.replace('_', ' ').title()}\n"
                    
                    if sensor_subtype:
                        report += f"- **Subtype**: {sensor_subtype}\n"
                    
                    report += f"- **Component**: {sensor.get('component', 'Unknown')}\n"
                    
                    if "model" in sensor:
                        report += f"- **Model**: {sensor['model']}\n"
                    
                    report += f"- **Value**: {sensor.get('value', 'Unknown')}\n"
                    
                    if "interface" in sensor:
                        report += f"- **Interface**: {sensor['interface']}\n"
                    
                    if "measures" in sensor:
                        if isinstance(sensor["measures"], list):
                            report += f"- **Measures**: {', '.join(sensor['measures'])}\n"
                        else:
                            report += f"- **Measures**: {sensor['measures']}\n"
                    
                    if "range" in sensor:
                        report += f"- **Range**: {sensor['range']}\n"
                    
                    report += "\n"
            
            return report
            
        except Exception as e:
            return f"# Circuit Pattern Analysis Error\n\nError: {str(e)}"
    
    @mcp.resource("kicad://patterns/project/{project_path}")
    def get_project_patterns_resource(project_path: str) -> str:
        """Get a formatted report of identified circuit patterns in a KiCad project.
        
        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            
        Returns:
            Markdown-formatted circuit pattern report
        """
        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"
        
        try:
            # Get the schematic file from the project
            files = get_project_files(project_path)
            
            if "schematic" not in files:
                return "Schematic file not found in project"
            
            schematic_path = files["schematic"]
            
            # Use the existing resource handler to generate the report
            return get_circuit_patterns_resource(schematic_path)
            
        except Exception as e:
            return f"# Circuit Pattern Analysis Error\n\nError: {str(e)}"
