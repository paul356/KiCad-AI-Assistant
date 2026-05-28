"""
Netlist extraction and analysis tools for KiCad schematics.
"""

import os
import re
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.file_utils import get_project_files
from kcaa.utils.netlist_parser import extract_netlist


def register_netlist_tools(mcp: FastMCP) -> None:
    """Register netlist-related tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    async def extract_project_netlist(project_path: str, ctx: Context | None) -> dict[str, Any]:
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
            result = await extract_schematic_netlist(schematic_path, ctx=ctx)

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
    async def extract_schematic_netlist(
        schematic_path: str,
        include_wire_topology: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Extract component inventory, net analysis, and wire geometry for a KiCad schematic.

        A net is a named group of pins that are electrically connected by wires.
        For example, if R1/pin2, C1/pin1, and a GND power symbol are all joined
        by wires, they form one net named "GND".

        This is the primary tool for spatial and connectivity reasoning. Each
        entry in ``components`` (keyed by reference) contains:

        - ``position``: ``{x, y}`` placement anchor in mm (KiCad screen coords,
          **+Y is down**).
        - ``rotation``, ``mirror``, ``lib_id``, ``value``, ``footprint``.
        - ``body_bbox``: ``{min_x, min_y, max_x, max_y}`` world-space bounding
          box (union of every placed unit). Use this to check overlaps before
          calling ``move_component`` / ``add_symbol_to_schematic``. May be
          absent for graphics-less symbols (e.g. PWR_FLAG).
        - ``pins``: list of ``{num, x, y, direction}``. ``x``/``y`` are world
          coordinates already accounting for placement and rotation;
          ``direction`` is the screen-space exit direction
          (``"right"|"up"|"left"|"down"``). When choosing which two pins to
          wire, the **electrically correct pin** comes first — only fall
          back to "closest pin pair" geometry when both candidates are
          electrically interchangeable (e.g. the two leads of a non-polar
          R/C/L, or any two pins already on the same net). Never pick by
          distance for polarised parts (diodes, electrolytic caps, LEDs,
          transistors), ICs, connectors, or named pins.

        Net entries contain only ``component``/``pin`` references (pin
        coordinates live on the component side to avoid duplication).

        Set ``include_wire_topology=True`` to also receive a top-level ``wires``
        dict keyed by wire ID. Each wire entry includes its net name (``null``
        if unconnected), start/end mm coordinates, and which component pins touch
        each endpoint. Required input for ``delete_wire_from_schematic``.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            include_wire_topology: When True, a top-level ``wires`` dict is
                added to the analysis. Each wire carries its own ``net`` field
                (the net name, or null if the wire is unconnected). Default True.
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
                    "components": {
                        "R1": {"value": "10k",
                               "position": {"x": ..., "y": ..., "rotation": ...},
                               "pins": [{"num": "1", "x": ..., "y": ..., "direction": ..., "net": "GND"}, ...]},
                               # direction: wire-exit direction in screen coords: "right", "down", "left", "up"
                        ...
                    },
                    "power_nets": [
                        {
                            "name": "GND",
                            "pin_count": <int>
                        },
                        ...
                    ],
                    "signal_nets": [ <same structure as power_nets> ],
                    "floating_nets": [
                        {
                            "net": "<net name>",
                            "description": "<explanation>"
                        }, ...
                    ],
                    # if include_wire_topology=True:
                    "wires": {
                        "0": {"net": "GND",
                              "start": {"x": ..., "y": ..., "pins": [{"ref": "R1", "pin": "1"}]},
                              "end":   {"x": ..., "y": ..., "pins": []}},
                        "5": {"net": null,
                              "start": {"x": ..., "y": ..., "pins": []},
                              "end":   {"x": ..., "y": ..., "pins": []}},
                        ...
                    }
                }
            }
            On failure: {"success": False, "error": "<error message>"}
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
            ctx.info(f"Extracting netlist from: {os.path.basename(schematic_path)}")

        # Extract netlist information
        try:
            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                print(f"Error extracting netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Error extracting netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data["error"]}

            if ctx:
                await ctx.report_progress(40, 100)

            # Advanced connection analysis
            if ctx:
                ctx.info("Performing connection analysis...")

            raw_components = netlist_data.get("components", {})

            # Build pin→net lookup so each pin entry can carry its net name
            _raw_nets = netlist_data.get("nets", {})
            pin_to_net: dict[tuple, str] = {}
            for _net_name, _net_pins in _raw_nets.items():
                for _p in _net_pins:
                    pin_to_net[(_p.get("component", ""), str(_p.get("pin", "")))] = _net_name

            components = {
                ref: {
                    "value": cdata.get("value", ""),
                    "position": cdata.get("position", {}),
                    "pins": [
                        {**pinfo, "net": pin_to_net.get((ref, str(pinfo.get("num", ""))))}
                        for pinfo in cdata.get("pins", [])
                    ],
                    **({"body_bbox": cdata["body_bbox"]} if "body_bbox" in cdata else {}),
                }
                for ref, cdata in raw_components.items()
            }
            analysis = {
                "component_count": netlist_data["component_count"],
                "net_count": netlist_data["net_count"],
                "component_types": {},
                "components": components,
                "power_nets": [],
                "signal_nets": [],
                "floating_nets": [],
            }

            # Analyze component types
            for ref in components:
                comp_type_match = re.match(r"^([A-Za-z_]+)", ref)
                if comp_type_match:
                    comp_type = comp_type_match.group(1)
                    analysis["component_types"][comp_type] = (
                        analysis["component_types"].get(comp_type, 0) + 1
                    )

            if ctx:
                await ctx.report_progress(60, 100)

            # Classify nets and detect floating ones in a single pass
            _POWER_PREFIXES = ("VCC", "VDD", "GND", "+5V", "+3V3", "+12V")
            nets = netlist_data.get("nets", {})
            for net_name, pins in nets.items():
                is_power = any(net_name.startswith(pfx) for pfx in _POWER_PREFIXES)
                net_entry = {
                    "name": net_name,
                    "pin_count": len(pins),
                }
                if is_power:
                    analysis["power_nets"].append(net_entry)
                else:
                    analysis["signal_nets"].append(net_entry)
                    if len(pins) <= 1:
                        analysis["floating_nets"].append(
                            {
                                "net": net_name,
                                "description": f"Net '{net_name}' appears to be floating (only has {len(pins)} connection)",
                            }
                        )

            if ctx:
                await ctx.report_progress(80, 100)

            if include_wire_topology:
                ROUND = 4

                def rpt(x, y):
                    return (round(float(x), ROUND), round(float(y), ROUND))

                # Reuse the wire→net mapping already resolved by the parser
                point_to_net = netlist_data.get("point_to_net", {})

                # Build point → component-pins lookup from component pin world coords
                from collections import defaultdict as _dd

                pin_at: dict[Any, list] = _dd(list)
                for ref, cdata in components.items():
                    for pinfo in cdata.get("pins", []):
                        pin_at[rpt(pinfo["x"], pinfo["y"])].append(
                            {"ref": ref, "pin": str(pinfo.get("num", ""))}
                        )

                # Build global wire registry
                all_wires = netlist_data.get("wires", [])
                wires: dict[str, Any] = {}
                for wire_id, wdata in enumerate(all_wires):
                    sp = rpt(wdata["start"]["x"], wdata["start"]["y"])
                    ep = rpt(wdata["end"]["x"], wdata["end"]["y"])
                    wnet = point_to_net.get(sp) or point_to_net.get(ep)
                    wires[str(wire_id)] = {
                        "net": wnet,
                        "start": {**wdata["start"], "pins": list(pin_at.get(sp, []))},
                        "end": {**wdata["end"], "pins": list(pin_at.get(ep, []))},
                    }

                analysis["wires"] = wires

            if ctx:
                await ctx.report_progress(90, 100)

            # Build result
            result = {"success": True, "schematic_path": schematic_path, "analysis": analysis}

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
    async def find_component_connections(
        project_path: str, component_ref: str, ctx: Context | None
    ) -> dict[str, Any]:
        """Find all connections for a specific component in a KiCad project.

        This tool extracts information about how a specific component
        is connected to other components in the schematic.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            component_ref: Component reference (e.g., "R1", "U3")
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with component connection information. In component_info,
            each pin's ``direction`` is the wire-exit direction in screen coordinates:
            "right", "down", "left", or "up".
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
                return {"success": False, "error": netlist_data["error"]}

            # Check if component exists in the netlist
            components = netlist_data.get("components", {})
            if component_ref not in components:
                print(f"Component {component_ref} not found in schematic")
                if ctx:
                    ctx.info(f"Component {component_ref} not found in schematic")
                return {
                    "success": False,
                    "error": f"Component {component_ref} not found in schematic",
                    "available_components": list(components.keys()),
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
                    if pin.get("component") == component_ref:
                        component_pins.append(pin)

                if component_pins:
                    # This net has connections to our component
                    net_connections = []

                    for pin in component_pins:
                        pin_num = pin.get("pin", "Unknown")
                        # Find other components connected to this pin
                        connected_components = []

                        for other_pin in pins:
                            other_comp = other_pin.get("component")
                            if other_comp and other_comp != component_ref:
                                connected_components.append(
                                    {
                                        "component": other_comp,
                                        "pin": other_pin.get("pin", "Unknown"),
                                    }
                                )

                        net_connections.append(
                            {"pin": pin_num, "net": net_name, "connected_to": connected_components}
                        )

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
                    pin_num = pin.get("num")
                    pin_name = pin.get("name", "")

                    # Try to categorize based on pin name
                    pin_type = "unknown"

                    if any(
                        power_term in pin_name.upper()
                        for power_term in ["VCC", "VDD", "VEE", "VSS", "GND", "PWR", "POWER"]
                    ):
                        pin_type = "power"
                    elif any(io_term in pin_name.upper() for io_term in ["IO", "I/O", "GPIO"]):
                        pin_type = "io"
                    elif any(input_term in pin_name.upper() for input_term in ["IN", "INPUT"]):
                        pin_type = "input"
                    elif any(output_term in pin_name.upper() for output_term in ["OUT", "OUTPUT"]):
                        pin_type = "output"

                    pin_functions[pin_num] = {"name": pin_name, "type": pin_type}

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
                "total_connections": len(connections),
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
