"""
Unit tests for kcaa/tools/netlist_tools.py.

Mocks extract_netlist and get_project_files so tests are self-contained.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# MockMCP — captures @mcp.tool()-decorated coroutines
# ---------------------------------------------------------------------------


class _MockMCP:
    """Minimal FastMCP stand-in that captures @mcp.tool()-decorated functions."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    """Register netlist tools against a mock MCP and return the captured dict."""
    from kcaa.tools.netlist_tools import register_netlist_tools

    mock = _MockMCP()
    register_netlist_tools(mock)
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tools():
    return _get_tools()


@pytest.fixture()
def mock_ctx():
    ctx = MagicMock()
    ctx.info = MagicMock()
    ctx.report_progress = MagicMock(return_value=asyncio.sleep(0))
    return ctx


# ---------------------------------------------------------------------------
# Sample netlist data
# ---------------------------------------------------------------------------

SAMPLE_NETLIST = {
    "component_count": 3,
    "net_count": 2,
    "components": {
        "R1": {
            "value": "10k",
            "position": {"x": 100, "y": 50},
            "pins": [
                {"num": "1", "x": 95, "y": 50, "direction": "left"},
                {"num": "2", "x": 105, "y": 50, "direction": "right"},
            ],
        },
        "R2": {
            "value": "4.7k",
            "position": {"x": 150, "y": 50},
            "pins": [
                {"num": "1", "x": 145, "y": 50, "direction": "left"},
                {"num": "2", "x": 155, "y": 50, "direction": "right"},
            ],
        },
        "C1": {
            "value": "100nF",
            "position": {"x": 200, "y": 100},
            "pins": [
                {"num": "1", "x": 195, "y": 100, "direction": "left"},
                {"num": "2", "x": 205, "y": 100, "direction": "right"},
            ],
        },
    },
    "nets": {
        "GND": [
            {"component": "R1", "pin": "2"},
            {"component": "C1", "pin": "2"},
        ],
        "Net-(R1-Pad1)": [
            {"component": "R1", "pin": "1"},
            {"component": "R2", "pin": "1"},
        ],
    },
    "wires": [
        {
            "start": {"x": 105, "y": 50},
            "end": {"x": 145, "y": 50},
        },
    ],
    "point_to_net": {
        (105.0, 50.0): "Net-(R1-Pad1)",
        (145.0, 50.0): "Net-(R1-Pad1)",
    },
    "dangling_points": [],
}

# Variant with one dangling wire stub (start at 95,50 is a pin, end at 75,50 is free)
NETLIST_WITH_DANGLING = {
    **SAMPLE_NETLIST,
    "wires": [
        *SAMPLE_NETLIST["wires"],
        {"start": {"x": 95, "y": 50}, "end": {"x": 75, "y": 50}},
    ],
    "point_to_net": {
        **SAMPLE_NETLIST["point_to_net"],
        (95.0, 50.0): "Net-(R1-Pad1)",
    },
    # (75,50) has only 1 wire touching it and no pin/label → dangling
    "dangling_points": [(75.0, 50.0)],
}

# Variant where both ends of a wire are free (completely floating wire)
NETLIST_WITH_FLOATING_WIRE = {
    **SAMPLE_NETLIST,
    "wires": [
        *SAMPLE_NETLIST["wires"],
        {"start": {"x": 10, "y": 10}, "end": {"x": 20, "y": 10}},
    ],
    "dangling_points": [(10.0, 10.0), (20.0, 10.0)],
}


# ---------------------------------------------------------------------------
# extract_project_netlist
# ---------------------------------------------------------------------------


class TestExtractProjectNetlist:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["extract_project_netlist"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=False)
    def test_project_not_found(self, mock_exists):
        result = _run(self.fn("/nonexistent/project.kicad_pro", ctx=None))
        assert result["success"] is False
        assert "Project not found" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.get_project_files", return_value={})
    def test_schematic_not_in_project(self, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", ctx=None))
        assert result["success"] is False
        assert "Schematic file not found" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_success_delegates_to_schematic_netlist(
        self, mock_extract, mock_exists2, mock_files, mock_exists
    ):
        result = _run(self.fn("/some/project.kicad_pro", ctx=None))
        assert result["success"] is True
        assert result["project_path"] == "/some/project.kicad_pro"
        assert "analysis" in result

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        side_effect=RuntimeError("corrupt project"),
    )
    def test_exception_in_get_project_files(self, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", ctx=None))
        assert result["success"] is False
        assert "corrupt project" in result["error"]


# ---------------------------------------------------------------------------
# extract_schematic_netlist
# ---------------------------------------------------------------------------


class TestExtractSchematicNetlist:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["extract_schematic_netlist"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=False)
    def test_schematic_file_not_found(self, mock_exists):
        result = _run(self.fn("/nonexistent/design.kicad_sch", ctx=None))
        assert result["success"] is False
        assert "Schematic file not found" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.extract_netlist",
        return_value={"error": "parse failed"},
    )
    def test_extract_netlist_returns_error(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        assert result["success"] is False
        assert "parse failed" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_basic_extraction(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        assert result["success"] is True
        assert result["schematic_path"] == "/some/design.kicad_sch"
        analysis = result["analysis"]
        assert analysis["component_count"] == 3
        assert analysis["net_count"] == 2
        assert "R1" in analysis["components"]
        assert "R2" in analysis["components"]
        assert "C1" in analysis["components"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_component_types_classification(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        types = result["analysis"]["component_types"]
        assert types["R"] == 2
        assert types["C"] == 1

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_power_net_classification(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        power_names = [n["name"] for n in result["analysis"]["power_nets"]]
        signal_names = [n["name"] for n in result["analysis"]["signal_nets"]]
        assert "GND" in power_names
        assert "Net-(R1-Pad1)" in signal_names
        assert "GND" not in signal_names

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist")
    def test_floating_net_detection(self, mock_extract, mock_exists):
        netlist = {
            **SAMPLE_NETLIST,
            "nets": {
                **SAMPLE_NETLIST["nets"],
                "Net-Floating": [{"component": "R1", "pin": "1"}],
            },
        }
        mock_extract.return_value = netlist
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        floating = result["analysis"]["floating_nets"]
        assert any(f["net"] == "Net-Floating" for f in floating)

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_pin_to_net_mapping(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        r1_pins = result["analysis"]["components"]["R1"]["pins"]
        pin_nets = {p["num"]: p["net"] for p in r1_pins}
        assert pin_nets["2"] == "GND"
        assert pin_nets["1"] == "Net-(R1-Pad1)"

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_wire_topology_included(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", include_wire_topology=True, ctx=None))
        assert "wires" in result["analysis"]
        wires = result["analysis"]["wires"]
        assert len(wires) == 1
        wire = wires["0"]
        assert "net" in wire
        assert "start" in wire
        assert "end" in wire
        assert "pins" in wire["start"]
        assert "pins" in wire["end"]
        # New fields
        assert "dangling_start" in wire
        assert "dangling_end" in wire
        assert "is_dangling" in wire
        assert "endpoints_share_net" in wire

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_wire_endpoints_share_net(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", include_wire_topology=True, ctx=None))
        wire = result["analysis"]["wires"]["0"]
        # Both endpoints are on "Net-(R1-Pad1)" per SAMPLE_NETLIST point_to_net
        assert wire["endpoints_share_net"] is True
        assert wire["is_dangling"] is False

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=NETLIST_WITH_DANGLING)
    def test_wire_dangling_one_end(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", include_wire_topology=True, ctx=None))
        wires = result["analysis"]["wires"]
        # Wire "1" is the dangling stub: start=(95,50) has net, end=(75,50) is dangling
        stub = wires["1"]
        assert stub["dangling_end"] is True
        assert stub["dangling_start"] is False
        assert stub["is_dangling"] is True

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=NETLIST_WITH_FLOATING_WIRE)
    def test_wire_both_ends_dangling(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", include_wire_topology=True, ctx=None))
        wires = result["analysis"]["wires"]
        # Wire "1" is the completely floating wire
        floating = wires["1"]
        assert floating["dangling_start"] is True
        assert floating["dangling_end"] is True
        assert floating["is_dangling"] is True
        assert floating["endpoints_share_net"] is False

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_wire_topology_excluded_by_default(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        assert "wires" not in result["analysis"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.extract_netlist",
        side_effect=RuntimeError("corrupt file"),
    )
    def test_extract_netlist_exception(self, mock_extract, mock_exists):
        result = _run(self.fn("/some/design.kicad_sch", ctx=None))
        assert result["success"] is False
        assert "corrupt file" in result["error"]


# ---------------------------------------------------------------------------
# find_component_connections
# ---------------------------------------------------------------------------


class TestFindComponentConnections:
    def setup_method(self):
        self.tools = _get_tools()
        self.fn = self.tools["find_component_connections"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=False)
    def test_project_not_found(self, mock_exists):
        result = _run(self.fn("/nonexistent/project.kicad_pro", "R1", ctx=None))
        assert result["success"] is False
        assert "Project not found" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch("kcaa.tools.netlist_tools.get_project_files", return_value={})
    def test_schematic_not_in_project(self, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", "R1", ctx=None))
        assert result["success"] is False
        assert "Schematic file not found" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch(
        "kcaa.tools.netlist_tools.extract_netlist",
        return_value={"error": "parse error"},
    )
    def test_extract_netlist_error(self, mock_extract, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", "R1", ctx=None))
        assert result["success"] is False
        assert "parse error" in result["error"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_component_not_found(self, mock_extract, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", "U99", ctx=None))
        assert result["success"] is False
        assert "U99" in result["error"]
        assert "available_components" in result

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch("kcaa.tools.netlist_tools.extract_netlist", return_value=SAMPLE_NETLIST)
    def test_component_found_with_connections(self, mock_extract, mock_files, mock_exists):
        result = _run(self.fn("/some/project.kicad_pro", "R1", ctx=None))
        assert result["success"] is True
        assert result["component"] == "R1"
        assert len(result["connections"]) > 0
        assert len(result["connected_nets"]) > 0
        assert "GND" in result["connected_nets"]

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch("kcaa.tools.netlist_tools.extract_netlist")
    def test_pin_function_classification(self, mock_extract, mock_files, mock_exists):
        netlist = {
            **SAMPLE_NETLIST,
            "components": {
                **SAMPLE_NETLIST["components"],
                "U1": {
                    "value": "ATmega328",
                    "position": {"x": 300, "y": 200},
                    "pins": [
                        {"num": "1", "x": 295, "y": 200, "direction": "left", "name": "VCC"},
                        {"num": "2", "x": 305, "y": 200, "direction": "right", "name": "GND"},
                        {"num": "3", "x": 295, "y": 210, "direction": "left", "name": "INPUT"},
                        {"num": "4", "x": 305, "y": 210, "direction": "right", "name": "OUTPUT"},
                        {"num": "5", "x": 295, "y": 220, "direction": "left", "name": "GPIO0"},
                    ],
                },
            },
            "nets": {
                **SAMPLE_NETLIST["nets"],
                "VCC": [{"component": "U1", "pin": "1"}],
                "GND2": [{"component": "U1", "pin": "2"}],
            },
        }
        mock_extract.return_value = netlist
        result = _run(self.fn("/some/project.kicad_pro", "U1", ctx=None))
        assert result["success"] is True
        pf = result["pin_functions"]
        assert pf["1"]["type"] == "power"
        assert pf["2"]["type"] == "power"
        assert pf["3"]["type"] == "input"
        assert pf["4"]["type"] == "output"
        assert pf["5"]["type"] == "io"

    @patch("kcaa.tools.netlist_tools.os.path.exists", return_value=True)
    @patch(
        "kcaa.tools.netlist_tools.get_project_files",
        return_value={"schematic": "/some/project.kicad_sch"},
    )
    @patch("kcaa.tools.netlist_tools.extract_netlist")
    def test_component_with_no_connections(self, mock_extract, mock_files, mock_exists):
        netlist = {
            "component_count": 1,
            "net_count": 0,
            "components": {
                "R1": {
                    "value": "10k",
                    "position": {"x": 100, "y": 50},
                    "pins": [{"num": "1", "x": 95, "y": 50, "direction": "left"}],
                },
            },
            "nets": {},
        }
        mock_extract.return_value = netlist
        result = _run(self.fn("/some/project.kicad_pro", "R1", ctx=None))
        assert result["success"] is True
        assert result["connections"] == []
        assert result["connected_nets"] == []
