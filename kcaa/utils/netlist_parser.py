"""
KiCad schematic netlist extraction utilities.
"""

from collections import defaultdict
import contextlib
import os
import re
from typing import Any

from kcaa.utils.skip_compat import safe_schematic
from kcaa.utils.skip_helpers import sym_pin_world_coords
from kcaa.utils.symbol_geometry import (
    BBox,
    compute_unit_bboxes,
    lib_bbox_to_world,
    union_bboxes,
)


def _angle_to_direction_screen(angle_deg: float) -> str:
    """Convert a pin wire-exit angle to a human-readable direction string.

    Angles use the KiCad file-angle convention (CCW on screen):
      0   → "right"  (+X)
      90  → "up"     (-Y screen)
      180 → "left"   (-X)
      270 → "down"   (+Y screen)
    """
    a = int(round(float(angle_deg))) % 360
    return {0: "right", 90: "up", 180: "left", 270: "down"}.get(a, f"{a}deg")


def _normalize_iterable(value: Any) -> list[Any]:
    """Return skip collections and single wrappers as a regular list."""
    if value is None:
        return []
    elements = getattr(value, "_elements", None)
    if elements is not None:
        return list(elements)
    if isinstance(value, list):
        return value
    if hasattr(value, "entity_type"):
        return [value]
    try:
        length = len(value)
    except TypeError:
        return [value]
    return [value[i] for i in range(length)]


class SchematicParser:
    """Parser for KiCad schematic files to extract netlist information."""

    def __init__(self, schematic_path: str):
        """Initialize the schematic parser.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
        """
        self.schematic_path = schematic_path
        self._sch = None
        self.components: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.wires: list[dict[str, Any]] = []
        self.junctions: list[dict[str, Any]] = []
        self.no_connects: list[dict[str, Any]] = []
        self.power_symbols: list[dict[str, Any]] = []
        self.hierarchical_labels: list[dict[str, Any]] = []
        self.global_labels: list[dict[str, Any]] = []

        # Netlist information
        self.nets: dict[str, list] = defaultdict(list)
        self.component_pins: dict[tuple, str] = {}
        self.point_to_net: dict[tuple, str] = {}

        # Component information
        self.component_info: dict[str, dict[str, Any]] = {}

        self._load_schematic()

    def _load_schematic(self) -> None:
        """Load the schematic using the skip library."""
        if not os.path.exists(self.schematic_path):
            print(f"Schematic file not found: {self.schematic_path}")
            raise FileNotFoundError(f"Schematic file not found: {self.schematic_path}")
        try:
            self._sch = safe_schematic(self.schematic_path)
            print(f"Successfully loaded schematic: {self.schematic_path}")
        except Exception as e:
            print(f"Error reading schematic file: {str(e)}")
            raise

    def parse(self) -> dict[str, Any]:
        """Parse the schematic to extract netlist information.

        Returns:
            Dictionary with parsed netlist information
        """
        print("Starting schematic parsing")

        self._extract_components()
        self._extract_sheet_components()
        self._extract_wires()
        self._extract_junctions()
        self._extract_labels()
        self._extract_power_symbols()
        self._extract_no_connects()
        self._build_netlist()

        result = {
            "components": self.component_info,
            "nets": dict(self.nets),
            "labels": self.labels,
            "wires": self.wires,
            "junctions": self.junctions,
            "power_symbols": self.power_symbols,
            "component_count": len(self.component_info),
            "net_count": len(self.nets),
            "point_to_net": self.point_to_net,
            "dangling_points": list(self._compute_dangling_points()),
        }

        print(
            f"Schematic parsing complete: found {len(self.component_info)} components and {len(self.nets)} nets"
        )
        return result

    def _extract_components(self) -> None:
        """Extract component information from schematic."""
        print("Extracting components")
        try:
            symbols = self._sch.symbol
        except AttributeError:
            print("No symbols found in schematic")
            return

        # Cache per-lib_id unit bboxes (lib Y-up coords).
        lib_bbox_cache: dict[str, dict[int, BBox]] = {}

        def _lib_unit_bboxes(lib_id: str) -> dict[int, BBox]:
            if lib_id in lib_bbox_cache:
                return lib_bbox_cache[lib_id]
            try:
                wrapper = self._sch.lib_symbols._libsyms_by_id.get(lib_id)
            except AttributeError:
                wrapper = None
            # _libsyms_by_id values are skip LibSymbol wrappers (or raw lists
            # if injected via _add_lib_symbol before write+reload). Reach
            # through to the underlying sexpdata tree either way.
            raw = None
            if wrapper is not None:
                if isinstance(wrapper, list):
                    raw = wrapper
                else:
                    pv = getattr(wrapper, "_pv", None)
                    raw = getattr(pv, "_tree", None) if pv is not None else None
            bboxes: dict[int, BBox] = {}
            if raw is not None:
                try:
                    bboxes = compute_unit_bboxes(raw)
                except Exception:
                    bboxes = {}
            lib_bbox_cache[lib_id] = bboxes
            return bboxes

        # World bboxes accumulated per reference so multi-unit symbols
        # report the union of every placed unit's footprint.
        world_bbox_per_ref: dict[str, list[BBox]] = defaultdict(list)

        for sym in symbols:
            comp: dict[str, Any] = {}

            # Reference is required; skip entries that don't have one
            try:
                comp["reference"] = sym.property.Reference.value
            except AttributeError:
                continue
            ref = comp["reference"]
            if not ref:
                continue

            with contextlib.suppress(AttributeError):
                comp["lib_id"] = sym.lib_id.value

            with contextlib.suppress(AttributeError):
                comp["value"] = sym.property.Value.value

            with contextlib.suppress(AttributeError):
                comp["footprint"] = sym.property.Footprint.value

            # sym.at.value -> [x, y, angle]
            sym_x = sym_y = sym_rot = None
            try:
                at_val = sym.at.value
                sym_x = float(at_val[0])
                sym_y = float(at_val[1])
                sym_rot = float(at_val[2]) if len(at_val) > 2 else 0.0
                comp["position"] = {
                    "x": sym_x,
                    "y": sym_y,
                    "rotation": sym_rot,
                }
            except (AttributeError, IndexError, TypeError):
                pass

            # Mirror flag (rare; "x" or "y") so the world bbox is correct
            # even when the user has flipped a placed instance in KiCad.
            mirror_val: str | None = None
            try:
                mv = sym.mirror.value
                mirror_val = mv.value() if hasattr(mv, "value") else mv
            except AttributeError:
                pass

            # Per-unit world bbox: look up this unit's lib bbox, transform.
            if comp.get("lib_id") and sym_x is not None and sym_y is not None:
                unit_no = 1
                with contextlib.suppress(AttributeError, ValueError, TypeError):
                    unit_no = int(sym.unit.value)
                bboxes = _lib_unit_bboxes(comp["lib_id"])
                lib_bb = bboxes.get(unit_no) or bboxes.get(1)
                if lib_bb is not None:
                    rot_int = int(round(sym_rot or 0.0))
                    world_bb = lib_bbox_to_world(
                        lib_bb,
                        sym_x,
                        sym_y,
                        rot_int,
                        mirror_val,
                    )
                    world_bbox_per_ref[ref].append(world_bb)

            # Collect pin positions via shared helper (handles the skip bug
            # for single-pin symbols: power nets, PWR_FLAG, TestPoint, etc.)
            pins_summary: list[dict[str, str]] = []
            for pin in sym_pin_world_coords(sym):
                pins_summary.append(
                    {
                        "num": pin.number,
                        "name": pin.name,
                        "electrical": pin.electrical_type,
                        "x": str(pin.x),
                        "y": str(pin.y),
                        "direction": _angle_to_direction_screen(pin.angle),
                    }
                )

            if pins_summary:
                comp["pins"] = pins_summary

            prev = self.component_info.get(ref)
            if prev is not None:
                # Multi-unit symbol: keep the first unit's entry and merge
                # every later unit's pins into it.  skip/schematic yields one
                # Symbol per unit, so without merging only the last unit's
                # pins would survive (U2 lost its 9 power pins).
                prev_pins = prev.setdefault("pins", [])
                seen = {(p["num"], p["x"], p["y"]) for p in prev_pins}
                for pin in comp.get("pins", []):
                    key = (pin["num"], pin["x"], pin["y"])
                    if key not in seen:
                        seen.add(key)
                        prev_pins.append(pin)
            else:
                self.components.append(comp)
                self.component_info[ref] = comp

        # Attach the union world bbox to each component info entry.
        for ref, bbs in world_bbox_per_ref.items():
            merged = union_bboxes(bbs)
            if merged is not None and ref in self.component_info:
                self.component_info[ref]["body_bbox"] = merged.to_dict()

        print(f"Extracted {len(self.components)} components")

    def _extract_sheet_components(self) -> None:
        """Extract hierarchical sheet symbols as opaque components."""
        print("Extracting sheet symbols")
        try:
            raw_sheets = self._sch.sheet
        except AttributeError:
            print("No sheet symbols found in schematic")
            return

        sheet_count = 0
        for sheet_wrapper in _normalize_iterable(raw_sheets):
            prop_map: dict[str, Any] = {}
            try:
                raw_props = sheet_wrapper.property
            except AttributeError:
                raw_props = None
            for prop in _normalize_iterable(raw_props):
                raw_tree = getattr(getattr(prop, "_pv", None), "_tree", None)
                if not isinstance(raw_tree, list) or len(raw_tree) < 3:
                    continue
                prop_name = raw_tree[1]
                if not isinstance(prop_name, str):
                    continue
                prop_map[prop_name.replace(" ", "").replace("_", "").lower()] = raw_tree[2]

            sheet_name = prop_map.get("sheetname")
            sheet_file = prop_map.get("sheetfile")
            try:
                sheet_uuid = sheet_wrapper.uuid.value
            except AttributeError:
                sheet_uuid = None

            try:
                at_vals = list(sheet_wrapper.at)
                sheet_x = float(at_vals[0])
                sheet_y = float(at_vals[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                sheet_x = sheet_y = None

            try:
                size_vals = list(sheet_wrapper.size)
                sheet_width = float(size_vals[0])
                sheet_height = float(size_vals[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                sheet_width = sheet_height = None

            reference = str(sheet_name or sheet_uuid or f"sheet-{sheet_count + 1}")
            unique_reference = reference
            duplicate_index = 2
            while unique_reference in self.component_info:
                unique_reference = f"{reference}#{duplicate_index}"
                duplicate_index += 1

            pins: list[dict[str, Any]] = []
            try:
                raw_pins = sheet_wrapper.pin
            except AttributeError:
                raw_pins = None
            for pin_wrapper in _normalize_iterable(raw_pins):
                pin_value = getattr(pin_wrapper, "value", None)
                if isinstance(pin_value, list) and pin_value:
                    pin_name = str(pin_value[0])
                elif pin_value is not None:
                    pin_name = str(pin_value)
                else:
                    continue
                try:
                    pin_at = list(pin_wrapper.at)
                    pin_x = float(pin_at[0])
                    pin_y = float(pin_at[1])
                except (AttributeError, IndexError, TypeError, ValueError):
                    continue
                pins.append(
                    {
                        "num": pin_name,
                        "number": pin_name,
                        "name": pin_name,
                        "x": pin_x,
                        "y": pin_y,
                    }
                )

            comp: dict[str, Any] = {
                "reference": unique_reference,
                "value": str(sheet_file or ""),
                "type": "sheet",
                "pins": pins,
            }
            if sheet_x is not None and sheet_y is not None:
                comp["position"] = {"x": sheet_x, "y": sheet_y}
            if None not in (sheet_x, sheet_y, sheet_width, sheet_height):
                comp["body_bbox"] = {
                    "min_x": sheet_x,
                    "min_y": sheet_y,
                    "max_x": sheet_x + sheet_width,
                    "max_y": sheet_y + sheet_height,
                }

            self.components.append(comp)
            self.component_info[unique_reference] = comp
            sheet_count += 1

        print(f"Extracted {sheet_count} sheet symbols")

    def _extract_wires(self) -> None:
        """Extract wire information from schematic."""
        print("Extracting wires")
        try:
            for wire in self._sch.wire:
                try:
                    xys = wire.pts.xy
                    s, e = xys[0].value, xys[1].value
                    self.wires.append(
                        {
                            "start": {"x": float(s[0]), "y": float(s[1])},
                            "end": {"x": float(e[0]), "y": float(e[1])},
                        }
                    )
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
                    self.junctions.append(
                        {
                            "x": float(at_val[0]),
                            "y": float(at_val[1]),
                        }
                    )
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
                    self.labels.append(
                        {
                            "type": "local",
                            "text": str(label.value),
                            "position": {
                                "x": float(at_val[0]),
                                "y": float(at_val[1]),
                                "angle": float(at_val[2]) if len(at_val) > 2 else 0.0,
                            },
                        }
                    )
                except (AttributeError, IndexError, TypeError):
                    continue
        except AttributeError:
            pass

        # Global labels
        try:
            for label in self._sch.global_label._elements:
                try:
                    at_val = label.at.value
                    self.global_labels.append(
                        {
                            "type": "global",
                            "text": str(label.value),
                            "position": {
                                "x": float(at_val[0]),
                                "y": float(at_val[1]),
                                "angle": float(at_val[2]) if len(at_val) > 2 else 0.0,
                            },
                        }
                    )
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
                        self.hierarchical_labels.append(
                            {
                                "type": "hierarchical",
                                "text": str(label.value),
                                "position": {
                                    "x": float(at_val[0]),
                                    "y": float(at_val[1]),
                                    "angle": float(at_val[2]) if len(at_val) > 2 else 0.0,
                                },
                            }
                        )
                    except (AttributeError, IndexError, TypeError):
                        continue
        except AttributeError:
            pass

        print(
            f"Extracted {len(self.labels)} local labels, "
            f"{len(self.global_labels)} global labels, "
            f"and {len(self.hierarchical_labels)} hierarchical labels"
        )

    def _extract_power_symbols(self) -> None:
        """Extract power symbol information from schematic."""
        print("Extracting power symbols")
        for comp in self.components:
            if comp.get("lib_id", "").startswith("power:"):
                self.power_symbols.append(
                    {
                        "type": comp["lib_id"].split(":", 1)[1],
                        "position": comp.get("position", {}),
                    }
                )
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
                        self.no_connects.append(
                            {
                                "x": float(at_val[0]),
                                "y": float(at_val[1]),
                            }
                        )
                    except (AttributeError, IndexError, TypeError):
                        continue
        except AttributeError:
            pass
        print(f"Extracted {len(self.no_connects)} no-connects")

    def _build_netlist(self) -> None:
        """Build the netlist by tracing wire connectivity between component pins.

        Uses the world pin coordinates computed by
        :func:`~kcaa.utils.skip_helpers.sym_pin_world_coords` (already
        rotation-corrected; skip's own ``SymbolPin.location`` is wrong for
        90°/270° placements, see docs/skip_library_notes.md §6) together
        with a union-find over wire endpoints to group connected pins into
        nets.
        """
        print("Building netlist from schematic data")

        ROUND = 4

        def pt(x: float, y: float) -> tuple[float, float]:
            return (round(float(x), ROUND), round(float(y), ROUND))

        # --- Union-Find ---
        uf: dict[tuple, tuple] = {}

        def find(p: tuple) -> tuple:
            uf.setdefault(p, p)
            root = p
            while uf[root] != root:
                root = uf[root]
            node = p
            while uf[node] != root:
                uf[node], node = root, uf[node]
            return root

        def union(p1: tuple, p2: tuple) -> None:
            r1, r2 = find(p1), find(p2)
            if r1 != r2:
                uf[r1] = r2

        # Step 1: Wire connectivity
        for wire in self.wires:
            union(
                pt(wire["start"]["x"], wire["start"]["y"]),
                pt(wire["end"]["x"], wire["end"]["y"]),
            )

        # Step 2: Register pin world positions (already rotation-corrected by skip)
        placed_pin_world: dict[tuple[str, str], tuple] = {}
        for ref, comp in self.component_info.items():
            if ref.startswith("#"):
                continue
            for pin_data in comp.get("pins", []):
                pin_number = str(pin_data.get("num", pin_data.get("number", "")))
                if not pin_number:
                    continue
                world_pt = pt(pin_data["x"], pin_data["y"])
                find(world_pt)  # register in uf
                placed_pin_world[(ref, pin_number)] = world_pt

        # Step 3: Assign net names from labels
        point_net: dict[tuple, str] = {}

        def name_point(p: tuple, name: str) -> None:
            root = find(p)
            if root not in point_net or name.upper() in ("GND", "VCC", "VDD", "VSS", "VEE"):
                point_net[root] = name

        for label in self.labels:
            name_point(pt(label["position"]["x"], label["position"]["y"]), label["text"])

        for label in self.global_labels:
            name_point(pt(label["position"]["x"], label["position"]["y"]), label["text"])

        for label in self.hierarchical_labels:
            name_point(pt(label["position"]["x"], label["position"]["y"]), label["text"])

        for comp in self.component_info.values():
            if comp.get("type") != "sheet":
                continue
            for pin_data in comp.get("pins", []):
                pin_name = str(
                    pin_data.get("name") or pin_data.get("number") or pin_data.get("num")
                )
                if pin_name:
                    name_point(pt(pin_data["x"], pin_data["y"]), pin_name)

        # Power symbol pins provide net names at their world positions.
        # When skip cannot resolve pin.location for a power symbol (e.g. power:GND),
        # fall back to the symbol's placement position, which in KiCad is always
        # the connection point for single-pin power symbols.
        for ref, comp in self.component_info.items():
            if comp.get("lib_id", "").startswith("power:"):
                power_name = comp["lib_id"].split(":", 1)[1]
                pin_coords = comp.get("pins", [])
                if pin_coords:
                    for pin_data in pin_coords:
                        name_point(pt(pin_data["x"], pin_data["y"]), power_name)
                else:
                    pos = comp.get("position", {})
                    if pos:
                        name_point(pt(pos.get("x", 0), pos.get("y", 0)), power_name)

        # Step 4: Group component pins by union-find group -> net
        group_pins: dict[tuple, list] = defaultdict(list)
        for (ref, pin_num), world_pt in placed_pin_world.items():
            group_pins[find(world_pt)].append({"component": ref, "pin": pin_num})

        net_counter = [1]

        def auto_net_name(root: tuple, pins: list) -> str:
            if root in point_net:
                return point_net[root]
            if len(pins) == 1:
                return f"Net-({pins[0]['component']}-Pin{pins[0]['pin']})"
            name = f"Net-{net_counter[0]}"
            net_counter[0] += 1
            return name

        # Build root → net name for all groups (named and auto-generated).
        root_to_name: dict[tuple, str] = {}
        for root, pins in group_pins.items():
            name = auto_net_name(root, pins)
            root_to_name[root] = name
            self.nets[name].extend(pins)

        # Register named nets that carry no component pins
        for root, net_name in point_net.items():
            if net_name not in self.nets:
                self.nets[net_name] = []
            if root not in root_to_name:
                root_to_name[root] = net_name

        # Expose a flat point → net-name mapping for ALL connected points,
        # including auto-named nets (previously only named/labeled nets were covered).
        for p in list(uf.keys()):
            root = find(p)
            if root in root_to_name:
                self.point_to_net[p] = root_to_name[root]

        print(
            f"Built netlist: {len(self.nets)} nets, "
            f"{sum(len(v) for v in self.nets.values())} pin connections"
        )

    def _compute_dangling_points(self) -> set[tuple[float, float]]:
        """Return wire endpoints that have no other connections.

        A point is dangling when exactly one wire touches it AND no component
        pin, net label, or junction sits at that coordinate.  A no-connect
        marker at a wire endpoint is intentionally omitted from the anchored
        set: a wire landing on a no-connect is a schematic contradiction and
        should still be flagged.
        """
        ROUND = 4

        def rpt(x: float, y: float) -> tuple[float, float]:
            return (round(float(x), ROUND), round(float(y), ROUND))

        endpoint_count: dict[tuple, int] = {}
        for wire in self.wires:
            sp = rpt(wire["start"]["x"], wire["start"]["y"])
            ep = rpt(wire["end"]["x"], wire["end"]["y"])
            endpoint_count[sp] = endpoint_count.get(sp, 0) + 1
            endpoint_count[ep] = endpoint_count.get(ep, 0) + 1

        anchored: set[tuple] = set()
        for cdata in self.component_info.values():
            for pin in cdata.get("pins", []):
                anchored.add(rpt(pin["x"], pin["y"]))
        for label in self.labels + self.global_labels + self.hierarchical_labels:
            pos = label["position"]
            anchored.add(rpt(pos["x"], pos["y"]))
        for junc in self.junctions:
            anchored.add(rpt(junc["x"], junc["y"]))

        return {pt for pt, count in endpoint_count.items() if count == 1 and pt not in anchored}


def extract_netlist(schematic_path: str) -> dict[str, Any]:
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
        return {"error": str(e), "components": {}, "nets": {}, "component_count": 0, "net_count": 0}


def analyze_netlist(netlist_data: dict[str, Any]) -> dict[str, Any]:
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
        "power_nets": [],
    }

    # Analyze component types
    for ref, component in netlist_data.get("components", {}).items():
        # Extract component type from reference (e.g., R1 -> R)
        comp_type = re.match(r"^([A-Za-z_]+)", ref)
        if comp_type:
            results["component_types"][comp_type.group(1)] += 1

    # Identify power nets
    for net_name in netlist_data.get("nets", {}):
        if any(
            net_name.startswith(prefix) for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]
        ):
            results["power_nets"].append(net_name)

    # Count pin connections
    total_pins = sum(len(pins) for pins in netlist_data.get("nets", {}).values())
    results["total_pin_connections"] = total_pins

    return results
