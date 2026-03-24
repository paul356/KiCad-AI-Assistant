"""
KiCad schematic netlist extraction utilities.
"""
import os
import re
from typing import Any, Dict, List, Tuple
from collections import defaultdict

import skip


class SchematicParser:
    """Parser for KiCad schematic files to extract netlist information."""

    def __init__(self, schematic_path: str):
        """Initialize the schematic parser.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
        """
        self.schematic_path = schematic_path
        self._sch = None
        self.components: List[Dict[str, Any]] = []
        self.labels: List[Dict[str, Any]] = []
        self.wires: List[Dict[str, Any]] = []
        self.junctions: List[Dict[str, Any]] = []
        self.no_connects: List[Dict[str, Any]] = []
        self.power_symbols: List[Dict[str, Any]] = []
        self.hierarchical_labels: List[Dict[str, Any]] = []
        self.global_labels: List[Dict[str, Any]] = []

        # Netlist information
        self.nets: Dict[str, List] = defaultdict(list)
        self.component_pins: Dict[Tuple, str] = {}

        # Component information
        self.component_info: Dict[str, Dict[str, Any]] = {}

        self._load_schematic()

    def _load_schematic(self) -> None:
        """Load the schematic using the skip library."""
        if not os.path.exists(self.schematic_path):
            print(f"Schematic file not found: {self.schematic_path}")
            raise FileNotFoundError(f"Schematic file not found: {self.schematic_path}")
        try:
            self._sch = skip.Schematic(self.schematic_path)
            print(f"Successfully loaded schematic: {self.schematic_path}")
        except Exception as e:
            print(f"Error reading schematic file: {str(e)}")
            raise

    def parse(self) -> Dict[str, Any]:
        """Parse the schematic to extract netlist information.
        
        Returns:
            Dictionary with parsed netlist information
        """
        print("Starting schematic parsing")

        self._extract_components()
        self._extract_wires()
        self._extract_junctions()
        self._extract_labels()
        self._extract_power_symbols()
        self._extract_no_connects()
        self._build_netlist()

        # Strip internal-only field before returning
        for comp in self.component_info.values():
            comp.pop('_pin_world_coords', None)

        result = {
            "components": self.component_info,
            "nets": dict(self.nets),
            "labels": self.labels,
            "wires": self.wires,
            "junctions": self.junctions,
            "power_symbols": self.power_symbols,
            "component_count": len(self.component_info),
            "net_count": len(self.nets),
        }

        print(f"Schematic parsing complete: found {len(self.component_info)} components and {len(self.nets)} nets")
        return result

    def _extract_components(self) -> None:
        """Extract component information from schematic."""
        print("Extracting components")
        try:
            symbols = self._sch.symbol
        except AttributeError:
            print("No symbols found in schematic")
            return

        for sym in symbols:
            comp: Dict[str, Any] = {}

            # Reference is required; skip entries that don't have one
            try:
                comp['reference'] = sym.property.Reference.value
            except AttributeError:
                continue
            ref = comp['reference']
            if not ref:
                continue

            try:
                comp['lib_id'] = sym.lib_id.value
            except AttributeError:
                pass

            try:
                comp['value'] = sym.property.Value.value
            except AttributeError:
                pass

            try:
                comp['footprint'] = sym.property.Footprint.value
            except AttributeError:
                pass

            # sym.at.value -> [x, y, angle]
            try:
                at_val = sym.at.value
                comp['position'] = {
                    'x': float(at_val[0]),
                    'y': float(at_val[1]),
                    'angle': float(at_val[2]) if len(at_val) > 2 else 0.0,
                }
            except (AttributeError, IndexError, TypeError):
                pass

            # Pins: pin.location gives world coords (rotation-corrected by skip)
            pin_world_coords: List[Dict[str, Any]] = []
            pins_summary: List[Dict[str, str]] = []
            try:
                for pin in sym.pin:
                    try:
                        num = str(pin.number)
                        loc = pin.location
                        pin_world_coords.append({
                            'num': num,
                            'world_x': float(loc.x),
                            'world_y': float(loc.y),
                        })
                        pins_summary.append({'num': num})
                    except AttributeError:
                        continue
            except (AttributeError, TypeError):
                pass

            if pins_summary:
                comp['pins'] = pins_summary
            # Internal field consumed by _build_netlist; stripped before parse() returns
            comp['_pin_world_coords'] = pin_world_coords

            self.components.append(comp)
            self.component_info[ref] = comp

        print(f"Extracted {len(self.components)} components")

    def _extract_wires(self) -> None:
        """Extract wire information from schematic."""
        print("Extracting wires")
        try:
            for wire in self._sch.wire:
                try:
                    xys = wire.pts.xy
                    s, e = xys[0].value, xys[1].value
                    self.wires.append({
                        'start': {'x': float(s[0]), 'y': float(s[1])},
                        'end':   {'x': float(e[0]), 'y': float(e[1])},
                    })
                except (AttributeError, IndexError, TypeError):
                    continue
        except AttributeError:
            pass
        print(f"Extracted {len(self.wires)} wires")

    def _extract_junctions(self) -> None:
        """Extract junction information from schematic."""
        print("Extracting junctions")
        try:
            for junc in self._sch.junction._elements:
                try:
                    at_val = junc.at.value
                    self.junctions.append({
                        'x': float(at_val[0]),
                        'y': float(at_val[1]),
                    })
                except (AttributeError, IndexError, TypeError):
                    continue
        except AttributeError:
            pass
        print(f"Extracted {len(self.junctions)} junctions")

    def _extract_labels(self) -> None:
        """Extract label information from schematic."""
        print("Extracting labels")

        # Local labels: (label "NAME" (at x y angle) ...)
        try:
            for label in self._sch.label._elements:
                try:
                    at_val = label.at.value
                    self.labels.append({
                        'type': 'local',
                        'text': str(label.value),
                        'position': {
                            'x': float(at_val[0]),
                            'y': float(at_val[1]),
                            'angle': float(at_val[2]) if len(at_val) > 2 else 0.0,
                        },
                    })
                except (AttributeError, IndexError, TypeError):
                    continue
        except AttributeError:
            pass

        # Global labels
        try:
            for label in self._sch.global_label._elements:
                try:
                    at_val = label.at.value
                    self.global_labels.append({
                        'type': 'global',
                        'text': str(label.value),
                        'position': {
                            'x': float(at_val[0]),
                            'y': float(at_val[1]),
                            'angle': float(at_val[2]) if len(at_val) > 2 else 0.0,
                        },
                    })
                except (AttributeError, IndexError, TypeError):
                    continue
        except AttributeError:
            pass

        # Hierarchical labels
        try:
            hl_coll = self._sch.hierarchical_label
            if hl_coll is not None:
                for label in hl_coll._elements:
                    try:
                        at_val = label.at.value
                        self.hierarchical_labels.append({
                            'type': 'hierarchical',
                            'text': str(label.value),
                            'position': {
                                'x': float(at_val[0]),
                                'y': float(at_val[1]),
                                'angle': float(at_val[2]) if len(at_val) > 2 else 0.0,
                            },
                        })
                    except (AttributeError, IndexError, TypeError):
                        continue
        except AttributeError:
            pass

        print(f"Extracted {len(self.labels)} local labels, "
              f"{len(self.global_labels)} global labels, "
              f"and {len(self.hierarchical_labels)} hierarchical labels")

    def _extract_power_symbols(self) -> None:
        """Extract power symbol information from schematic."""
        print("Extracting power symbols")
        for comp in self.components:
            if comp.get('lib_id', '').startswith('power:'):
                self.power_symbols.append({
                    'type': comp['lib_id'].split(':', 1)[1],
                    'position': comp.get('position', {}),
                })
        print(f"Extracted {len(self.power_symbols)} power symbols")

    def _extract_no_connects(self) -> None:
        """Extract no-connect information from schematic."""
        print("Extracting no-connects")
        try:
            nc_coll = self._sch.no_connect
            if nc_coll is not None:
                for nc in nc_coll._elements:
                    try:
                        at_val = nc.at.value
                        self.no_connects.append({
                            'x': float(at_val[0]),
                            'y': float(at_val[1]),
                        })
                    except (AttributeError, IndexError, TypeError):
                        continue
        except AttributeError:
            pass
        print(f"Extracted {len(self.no_connects)} no-connects")

    def _build_netlist(self) -> None:
        """Build the netlist by tracing wire connectivity between component pins.

        Uses skip's pin.location (world coordinates, rotation already applied)
        together with a union-find over wire endpoints to group connected pins
        into nets.
        """
        print("Building netlist from schematic data")

        ROUND = 4

        def pt(x: float, y: float) -> Tuple[float, float]:
            return (round(float(x), ROUND), round(float(y), ROUND))

        # --- Union-Find ---
        uf: Dict[Tuple, Tuple] = {}

        def find(p: Tuple) -> Tuple:
            uf.setdefault(p, p)
            root = p
            while uf[root] != root:
                root = uf[root]
            node = p
            while uf[node] != root:
                uf[node], node = root, uf[node]
            return root

        def union(p1: Tuple, p2: Tuple) -> None:
            r1, r2 = find(p1), find(p2)
            if r1 != r2:
                uf[r1] = r2

        # Step 1: Wire connectivity
        for wire in self.wires:
            union(
                pt(wire['start']['x'], wire['start']['y']),
                pt(wire['end']['x'],   wire['end']['y']),
            )

        # Step 2: Register pin world positions (already rotation-corrected by skip)
        placed_pin_world: Dict[Tuple[str, str], Tuple] = {}
        for ref, comp in self.component_info.items():
            if ref.startswith('#'):
                continue
            for pin_data in comp.get('_pin_world_coords', []):
                world_pt = pt(pin_data['world_x'], pin_data['world_y'])
                find(world_pt)  # register in uf
                placed_pin_world[(ref, pin_data['num'])] = world_pt

        # Step 3: Assign net names from labels
        point_net: Dict[Tuple, str] = {}

        def name_point(p: Tuple, name: str) -> None:
            root = find(p)
            if root not in point_net or name.upper() in ('GND', 'VCC', 'VDD', 'VSS', 'VEE'):
                point_net[root] = name

        for label in self.labels:
            name_point(pt(label['position']['x'], label['position']['y']), label['text'])

        for label in self.global_labels:
            name_point(pt(label['position']['x'], label['position']['y']), label['text'])

        for label in self.hierarchical_labels:
            name_point(pt(label['position']['x'], label['position']['y']), label['text'])

        # Power symbol pins provide net names at their world positions.
        # When skip cannot resolve pin.location for a power symbol (e.g. power:GND),
        # fall back to the symbol's placement position, which in KiCad is always
        # the connection point for single-pin power symbols.
        for ref, comp in self.component_info.items():
            if comp.get('lib_id', '').startswith('power:'):
                power_name = comp['lib_id'].split(':', 1)[1]
                pin_coords = comp.get('_pin_world_coords', [])
                if pin_coords:
                    for pin_data in pin_coords:
                        name_point(pt(pin_data['world_x'], pin_data['world_y']), power_name)
                else:
                    pos = comp.get('position', {})
                    if pos:
                        name_point(pt(pos.get('x', 0), pos.get('y', 0)), power_name)

        # Step 4: Group component pins by union-find group -> net
        group_pins: Dict[Tuple, List] = defaultdict(list)
        for (ref, pin_num), world_pt in placed_pin_world.items():
            group_pins[find(world_pt)].append({'component': ref, 'pin': pin_num})

        net_counter = [1]

        def auto_net_name(root: Tuple, pins: List) -> str:
            if root in point_net:
                return point_net[root]
            if len(pins) == 1:
                return f"Net-({pins[0]['component']}-Pin{pins[0]['pin']})"
            name = f"Net-{net_counter[0]}"
            net_counter[0] += 1
            return name

        for root, pins in group_pins.items():
            self.nets[auto_net_name(root, pins)].extend(pins)

        # Register named nets that carry no component pins
        for root, net_name in point_net.items():
            if net_name not in self.nets:
                self.nets[net_name] = []

        print(f"Built netlist: {len(self.nets)} nets, "
              f"{sum(len(v) for v in self.nets.values())} pin connections")


def extract_netlist(schematic_path: str) -> Dict[str, Any]:
    """Extract netlist information from a KiCad schematic file.

    Args:
        schematic_path: Path to the KiCad schematic file (.kicad_sch)

    Returns:
        Dictionary with netlist information
    """
    try:
        parser = SchematicParser(schematic_path)
        return parser.parse()
    except Exception as e:
        print(f"Error extracting netlist: {str(e)}")
        return {
            "error": str(e),
            "components": {},
            "nets": {},
            "component_count": 0,
            "net_count": 0
        }


def analyze_netlist(netlist_data: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze netlist data to provide insights.
    
    Args:
        netlist_data: Dictionary with netlist information
        
    Returns:
        Dictionary with analysis results
    """
    results = {
        "component_count": netlist_data.get("component_count", 0),
        "net_count": netlist_data.get("net_count", 0),
        "component_types": defaultdict(int),
        "power_nets": []
    }
    
    # Analyze component types
    for ref, component in netlist_data.get("components", {}).items():
        # Extract component type from reference (e.g., R1 -> R)
        comp_type = re.match(r'^([A-Za-z_]+)', ref)
        if comp_type:
            results["component_types"][comp_type.group(1)] += 1
    
    # Identify power nets
    for net_name in netlist_data.get("nets", {}):
        if any(net_name.startswith(prefix) for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]):
            results["power_nets"].append(net_name)
    
    # Count pin connections
    total_pins = sum(len(pins) for pins in netlist_data.get("nets", {}).values())
    results["total_pin_connections"] = total_pins
    
    return results
