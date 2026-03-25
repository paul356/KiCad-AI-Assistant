"""
Netlist extraction and analysis tools for KiCad schematics.
"""
import os
from typing import Dict, Any
from fastmcp import FastMCP, Context

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.netlist_parser import extract_netlist, analyze_netlist

def register_netlist_tools(mcp: FastMCP) -> None:
    """Register netlist-related tools with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.tool()
    async def extract_schematic_netlist(schematic_path: str, ctx: Context | None) -> Dict[str, Any]:
        """Extract netlist information from a KiCad schematic.
        
        This tool parses a KiCad schematic file and extracts comprehensive
        netlist information including components, connections, and labels.
        
        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            ctx: MCP context for progress reporting
            
        Returns:
            Dictionary with netlist information
        """
        print(f"Extracting netlist from schematic: {schematic_path}")
        
        if not os.path.exists(schematic_path):
            print(f"Schematic file not found: {schematic_path}")
            if ctx:
                ctx.info(f"Schematic file not found: {schematic_path}")
            return {"success": False, "error": f"Schematic file not found: {schematic_path}"}
        
        # Report progress
        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(f"Loading schematic file: {os.path.basename(schematic_path)}")
        
        # Extract netlist information
        try:
            if ctx:
                await ctx.report_progress(20, 100)
                ctx.info("Parsing schematic structure...")
            
            netlist_data = extract_netlist(schematic_path)
            
            if "error" in netlist_data:
                print(f"Error extracting netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Error extracting netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data['error']}
            
            if ctx:
                await ctx.report_progress(60, 100)
                ctx.info(f"Extracted {netlist_data['component_count']} components and {netlist_data['net_count']} nets")
            
            # Analyze the netlist
            if ctx:
                await ctx.report_progress(70, 100)
                ctx.info("Analyzing netlist data...")
            
            analysis_results = analyze_netlist(netlist_data)
            
            if ctx:
                await ctx.report_progress(90, 100)
            
            # Build result
            result = {
                "success": True,
                "schematic_path": schematic_path,
                "component_count": netlist_data["component_count"],
                "net_count": netlist_data["net_count"],
                "components": netlist_data["components"],
                "nets": netlist_data["nets"],
                "analysis": analysis_results
            }
            
            # Complete progress
            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info("Netlist extraction complete")
            
            return result
            
        except Exception as e:
            print(f"Error extracting netlist: {str(e)}")
            if ctx:
                ctx.info(f"Error extracting netlist: {str(e)}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def extract_project_netlist(project_path: str, ctx: Context | None) -> Dict[str, Any]:
        """Extract netlist from a KiCad project's schematic.
        
        This tool finds the schematic associated with a KiCad project
        and extracts its netlist information.
        
        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting
            
        Returns:
            Dictionary with netlist information
        """
        print(f"Extracting netlist for project: {project_path}")
        
        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}
        
        # Report progress
        if ctx:
            await ctx.report_progress(10, 100)
        
        # Get the schematic file
        try:
            files = get_project_files(project_path)
            
            if "schematic" not in files:
                print("Schematic file not found in project")
                if ctx:
                    ctx.info("Schematic file not found in project")
                return {"success": False, "error": "Schematic file not found in project"}
            
            schematic_path = files["schematic"]
            print(f"Found schematic file: {schematic_path}")
            if ctx:
                ctx.info(f"Found schematic file: {os.path.basename(schematic_path)}")
            
            # Extract netlist
            if ctx:
                await ctx.report_progress(20, 100)
            
            # Call the schematic netlist extraction
            result = await extract_schematic_netlist(schematic_path, ctx)
            
            # Add project path to result
            if "success" in result and result["success"]:
                result["project_path"] = project_path
            
            return result
            
        except Exception as e:
            print(f"Error extracting project netlist: {str(e)}")
            if ctx:
                ctx.info(f"Error extracting project netlist: {str(e)}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def analyze_schematic_connections(
        schematic_path: str,
        include_wire_topology: bool = True,
        ctx: Context | None = None,
    ) -> Dict[str, Any]:
        """Summarize nets, component pins, and (optionally) wire geometry for a KiCad schematic.

        A net is a named group of pins that are electrically connected by wires.
        For example, if R1/pin2, C1/pin1, and a GND power symbol are all joined
        by wires, they form one net named "GND".

        This tool provides a statistical overview of the schematic: component
        type counts, net classification (power vs signal), pin coordinates, and
        simple issue detection.

        Set ``include_wire_topology=True`` to also receive, for every net, the
        exact wire segments that carry it (start/end mm coordinates, and which
        component pins touch each endpoint). Unconnected wire segments (no net)
        are returned separately under ``unconnected_wires``. This replaces the
        need to call get_net_topology or list_wires_in_schematic separately.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            include_wire_topology: When True, each net entry gains a ``wires``
                list and ``wire_count``, and a top-level ``unconnected_wires``
                list is added to the analysis. Default False.
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with the following structure on success:
            {
                "success": True,
                "schematic_path": "<path>",
                "analysis": {
                    "component_count": <int>,
                    "net_count": <int>,
                    "component_types": {"R": 3, "C": 2, ...},
                    "power_nets": [
                        {
                            "name": "GND",
                            "pin_count": <int>,
                            "pins": [{"component": "R1", "pin": "1", "x": 10.16, "y": 25.4}, ...],
                            # if include_wire_topology=True:
                            "wire_count": <int>,
                            "wires": [
                                {
                                    "start": {"x": ..., "y": ...},
                                    "end":   {"x": ..., "y": ...},
                                    "start_pins": [{"ref": "R1", "pin": "1"}],
                                    "end_pins":   []
                                }, ...
                            ]
                        },
                        ...
                    ],
                    "signal_nets": [ <same structure as power_nets> ],
                    "potential_issues": [
                        {
                            "type": "floating_net",
                            "net": "<net name>",
                            "description": "<explanation>"
                        }, ...
                    ],
                    # if include_wire_topology=True:
                    "unconnected_wires": [
                        {"start": {"x": ..., "y": ...}, "end": {"x": ..., "y": ...},
                         "start_pins": [], "end_pins": []}, ...
                    ],
                    "unconnected_wire_count": <int>
                }
            }
            On failure: {"success": False, "error": "<error message>"}
        """
        print(f"Analyzing connections in schematic: {schematic_path}")
        
        if not os.path.exists(schematic_path):
            print(f"Schematic file not found: {schematic_path}")
            if ctx:
                ctx.info(f"Schematic file not found: {schematic_path}")
            return {"success": False, "error": f"Schematic file not found: {schematic_path}"}
        
        # Report progress
        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(f"Extracting netlist from: {os.path.basename(schematic_path)}")
        
        # Extract netlist information
        try:
            netlist_data = extract_netlist(schematic_path)
            
            if "error" in netlist_data:
                print(f"Error extracting netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Error extracting netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data['error']}
            
            if ctx:
                await ctx.report_progress(40, 100)
            
            # Advanced connection analysis
            if ctx:
                ctx.info("Performing connection analysis...")
            
            analysis = {
                "component_count": netlist_data["component_count"],
                "net_count": netlist_data["net_count"],
                "component_types": {},
                "power_nets": [],
                "signal_nets": [],
                "potential_issues": []
            }
            
            # Analyze component types
            components = netlist_data.get("components", {})
            for ref, component in components.items():
                # Extract component type from reference (e.g., R1 -> R)
                import re
                comp_type_match = re.match(r'^([A-Za-z_]+)', ref)
                if comp_type_match:
                    comp_type = comp_type_match.group(1)
                    if comp_type not in analysis["component_types"]:
                        analysis["component_types"][comp_type] = 0
                    analysis["component_types"][comp_type] += 1
            
            if ctx:
                await ctx.report_progress(60, 100)
            
            # Identify power nets
            # Build a position lookup (ref → pin_num → {x, y}) from the enriched
            # pins list (world coords are now stored directly on each pin entry).
            components = netlist_data.get("components", {})
            pin_positions: Dict[str, Dict[str, Dict[str, float]]] = {}
            for cref, cdata in components.items():
                pin_positions[cref] = {}
                pos = cdata.get("position", {})
                comp_x = pos.get("x", 0.0)
                comp_y = pos.get("y", 0.0)
                for pinfo in cdata.get("pins", []):
                    pnum = str(pinfo.get("num", ""))
                    pin_positions[cref][pnum] = {
                        "x": float(pinfo.get("x", comp_x)),
                        "y": float(pinfo.get("y", comp_y)),
                    }

            def pins_with_coords(pins: list) -> list:
                enriched = []
                for p in pins:
                    ref = p.get("component", "")
                    pin_num = str(p.get("pin", ""))
                    entry = {"component": ref, "pin": pin_num}
                    coords = pin_positions.get(ref, {}).get(pin_num)
                    if coords:
                        entry["x"] = coords["x"]
                        entry["y"] = coords["y"]
                    enriched.append(entry)
                return enriched

            nets = netlist_data.get("nets", {})
            for net_name, pins in nets.items():
                if any(net_name.startswith(prefix) for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]):
                    analysis["power_nets"].append({
                        "name": net_name,
                        "pin_count": len(pins),
                        "pins": pins_with_coords(pins),
                    })
                else:
                    analysis["signal_nets"].append({
                        "name": net_name,
                        "pin_count": len(pins),
                        "pins": pins_with_coords(pins),
                    })
            
            if ctx:
                await ctx.report_progress(80, 100)
            
            # Check for potential issues
            # 1. Nets with only one connection (floating)
            for net_name, pins in nets.items():
                if len(pins) <= 1 and not any(net_name.startswith(prefix) for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]):
                    analysis["potential_issues"].append({
                        "type": "floating_net",
                        "net": net_name,
                        "description": f"Net '{net_name}' appears to be floating (only has {len(pins)} connection)"
                    })
            
            # 2. Power pins without connections
            # This would require more detailed parsing of the schematic

            if include_wire_topology:
                ROUND = 4

                def rpt(x, y):
                    return (round(float(x), ROUND), round(float(y), ROUND))

                # Union-find over all wire segments
                uf: Dict[Any, Any] = {}

                def uf_find(p):
                    uf.setdefault(p, p)
                    root = p
                    while uf[root] != root:
                        root = uf[root]
                    node = p
                    while uf[node] != root:
                        uf[node], node = root, uf[node]
                    return root

                def uf_union(a, b):
                    ra, rb = uf_find(a), uf_find(b)
                    if ra != rb:
                        uf[ra] = rb

                all_wires = netlist_data.get("wires", [])
                for wdata in all_wires:
                    uf_union(rpt(wdata["start"]["x"], wdata["start"]["y"]),
                             rpt(wdata["end"]["x"],   wdata["end"]["y"]))

                # Map each union-find root to its net name via pin world coords
                root_to_net: Dict[Any, str] = {}
                for net_name, pins in nets.items():
                    for p in pins:
                        coords = pin_positions.get(p.get("component", ""), {}).get(str(p.get("pin", "")))
                        if coords:
                            root = uf_find(rpt(coords["x"], coords["y"]))
                            if root not in root_to_net:
                                root_to_net[root] = net_name

                # Build point → pins lookup for endpoint annotation
                from collections import defaultdict as _dd
                pin_at: Dict[Any, list] = _dd(list)
                for net_name, pins in nets.items():
                    for p in pins:
                        coords = pin_positions.get(p.get("component", ""), {}).get(str(p.get("pin", "")))
                        if coords:
                            pin_at[rpt(coords["x"], coords["y"])].append(
                                {"ref": p.get("component", ""), "pin": str(p.get("pin", ""))}
                            )

                # Assign each wire to its net
                net_wires: Dict[str, list] = {}
                unconnected_wires = []
                for wdata in all_wires:
                    sp = rpt(wdata["start"]["x"], wdata["start"]["y"])
                    ep = rpt(wdata["end"]["x"],   wdata["end"]["y"])
                    wnet = root_to_net.get(uf_find(sp)) or root_to_net.get(uf_find(ep))
                    wire_entry = {
                        "start": wdata["start"],
                        "end":   wdata["end"],
                        "start_pins": list(pin_at.get(sp, [])),
                        "end_pins":   list(pin_at.get(ep, [])),
                    }
                    if wnet:
                        net_wires.setdefault(wnet, []).append(wire_entry)
                    else:
                        unconnected_wires.append(wire_entry)

                # Attach wire lists to each net entry
                for entry in analysis["power_nets"] + analysis["signal_nets"]:
                    wires = net_wires.get(entry["name"], [])
                    entry["wires"] = wires
                    entry["wire_count"] = len(wires)

                analysis["unconnected_wires"] = unconnected_wires
                analysis["unconnected_wire_count"] = len(unconnected_wires)

            if ctx:
                await ctx.report_progress(90, 100)

            # Build result
            result = {
                "success": True,
                "schematic_path": schematic_path,
                "analysis": analysis
            }
            
            # Complete progress
            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info("Connection analysis complete")
            
            return result
            
        except Exception as e:
            print(f"Error analyzing connections: {str(e)}")
            if ctx:
                ctx.info(f"Error analyzing connections: {str(e)}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def find_component_connections(project_path: str, component_ref: str, ctx: Context | None) -> Dict[str, Any]:
        """Find all connections for a specific component in a KiCad project.
        
        This tool extracts information about how a specific component
        is connected to other components in the schematic.
        
        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            component_ref: Component reference (e.g., "R1", "U3")
            ctx: MCP context for progress reporting
            
        Returns:
            Dictionary with component connection information
        """
        print(f"Finding connections for component {component_ref} in project: {project_path}")
        
        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}
        
        # Report progress
        if ctx:
            await ctx.report_progress(10, 100)
        
        # Get the schematic file
        try:
            files = get_project_files(project_path)
            
            if "schematic" not in files:
                print("Schematic file not found in project")
                if ctx:
                    ctx.info("Schematic file not found in project")
                return {"success": False, "error": "Schematic file not found in project"}
            
            schematic_path = files["schematic"]
            print(f"Found schematic file: {schematic_path}")
            if ctx:
                ctx.info(f"Found schematic file: {os.path.basename(schematic_path)}")
            
            # Extract netlist
            if ctx:
                await ctx.report_progress(30, 100)
                ctx.info(f"Extracting netlist to find connections for {component_ref}...")
            
            netlist_data = extract_netlist(schematic_path)
            
            if "error" in netlist_data:
                print(f"Failed to extract netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Failed to extract netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data['error']}
            
            # Check if component exists in the netlist
            components = netlist_data.get("components", {})
            if component_ref not in components:
                print(f"Component {component_ref} not found in schematic")
                if ctx:
                    ctx.info(f"Component {component_ref} not found in schematic")
                return {
                    "success": False, 
                    "error": f"Component {component_ref} not found in schematic",
                    "available_components": list(components.keys())
                }
            
            # Get component information
            component_info = components[component_ref]
            
            # Find connections
            if ctx:
                await ctx.report_progress(50, 100)
                ctx.info("Finding connections...")
            
            nets = netlist_data.get("nets", {})
            connections = []
            connected_nets = []
            
            for net_name, pins in nets.items():
                # Check if any pin belongs to our component
                component_pins = []
                for pin in pins:
                    if pin.get('component') == component_ref:
                        component_pins.append(pin)
                        
                if component_pins:
                    # This net has connections to our component
                    net_connections = []
                    
                    for pin in component_pins:
                        pin_num = pin.get('pin', 'Unknown')
                        # Find other components connected to this pin
                        connected_components = []
                        
                        for other_pin in pins:
                            other_comp = other_pin.get('component')
                            if other_comp and other_comp != component_ref:
                                connected_components.append({
                                    "component": other_comp,
                                    "pin": other_pin.get('pin', 'Unknown')
                                })
                        
                        net_connections.append({
                            "pin": pin_num,
                            "net": net_name,
                            "connected_to": connected_components
                        })
                    
                    connections.extend(net_connections)
                    connected_nets.append(net_name)
            
            # Analyze the connections
            if ctx:
                await ctx.report_progress(70, 100)
                ctx.info("Analyzing connections...")
            
            # Categorize connections by pin function (if possible)
            pin_functions = {}
            if "pins" in component_info:
                for pin in component_info["pins"]:
                    pin_num = pin.get('num')
                    pin_name = pin.get('name', '')
                    
                    # Try to categorize based on pin name
                    pin_type = "unknown"
                    
                    if any(power_term in pin_name.upper() for power_term in ["VCC", "VDD", "VEE", "VSS", "GND", "PWR", "POWER"]):
                        pin_type = "power"
                    elif any(io_term in pin_name.upper() for io_term in ["IO", "I/O", "GPIO"]):
                        pin_type = "io"
                    elif any(input_term in pin_name.upper() for input_term in ["IN", "INPUT"]):
                        pin_type = "input"
                    elif any(output_term in pin_name.upper() for output_term in ["OUT", "OUTPUT"]):
                        pin_type = "output"
                    
                    pin_functions[pin_num] = {
                        "name": pin_name,
                        "type": pin_type
                    }
            
            # Build result
            result = {
                "success": True,
                "project_path": project_path,
                "schematic_path": schematic_path,
                "component": component_ref,
                "component_info": component_info,
                "connections": connections,
                "connected_nets": connected_nets,
                "pin_functions": pin_functions,
                "total_connections": len(connections)
            }
            
            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info(f"Found {len(connections)} connections for component {component_ref}")
            
            return result
            
        except Exception as e:
            print(f"Error finding component connections: {str(e)}", exc_info=True)
            if ctx:
                ctx.info(f"Error finding component connections: {str(e)}")
            return {"success": False, "error": str(e)}
