"""
Prompt templates for circuit pattern analysis in KiCad.
"""

from fastmcp import FastMCP


def register_pattern_prompts(mcp: FastMCP) -> None:
    """Register pattern-related prompt templates with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.prompt()
    def analyze_circuit_patterns() -> str:
        """Prompt for circuit pattern analysis."""
        prompt = """
        I'd like to analyze the circuit patterns in my KiCad design. Can you help me identify:

        1. What common circuit blocks are present in my design
        2. Which components are part of each circuit block
        3. The function of each identified circuit block
        4. Any potential design issues in these circuits

        My KiCad project is located at:
        [Enter path to your .kicad_pro file]

        Please identify as many common patterns as possible (power supplies, amplifiers, filters, etc.)
        """

        return prompt

    @mcp.prompt()
    def analyze_power_supplies() -> str:
        """Prompt for power supply circuit analysis."""
        prompt = """
        I need help analyzing the power supply circuits in my KiCad design. Can you help me:

        1. Identify all the power supply circuits in my schematic
        2. Determine what voltage levels they provide
        3. Check if they're properly designed with appropriate components
        4. Suggest any improvements or optimizations

        My KiCad schematic is located at:
        [Enter path to your .kicad_sch file]

        Please focus on both linear regulators and switching power supplies.
        """

        return prompt

    @mcp.prompt()
    def analyze_sensor_interfaces() -> str:
        """Prompt for sensor interface analysis."""
        prompt = """
        I want to review all the sensor interfaces in my KiCad design. Can you help me:

        1. Identify all sensors in my schematic
        2. Determine what each sensor measures and how it interfaces with the system
        3. Check if the sensor connections follow best practices
        4. Suggest any improvements for sensor integration

        My KiCad project is located at:
        [Enter path to your .kicad_pro file]

        Please identify temperature, pressure, motion, light, and any other sensors in the design.
        """

        return prompt

    @mcp.prompt()
    def analyze_microcontroller_connections() -> str:
        """Prompt for microcontroller connection analysis."""
        prompt = """
        I want to review how my microcontroller is connected to other circuits in my KiCad design. Can you help me:

        1. Identify the microcontroller(s) in my schematic
        2. Map out what peripherals and circuits are connected to each pin
        3. Check if the connections follow good design practices
        4. Identify any potential issues or conflicts

        My KiCad schematic is located at:
        [Enter path to your .kicad_sch file]

        Please focus on interface circuits (SPI, I2C, UART), sensor connections, and power supply connections.
        """

        return prompt

    @mcp.prompt()
    def find_and_improve_circuits() -> str:
        """Prompt for finding and improving specific circuits."""
        prompt = """
        I'm looking to improve specific circuit patterns in my KiCad design. Can you help me:

        1. Find all instances of [CIRCUIT_TYPE] circuits in my schematic
        2. Evaluate if they are designed correctly
        3. Suggest modern alternatives or improvements
        4. Recommend specific component changes if needed

        My KiCad project is located at:
        [Enter path to your .kicad_pro file]

        Please replace [CIRCUIT_TYPE] with the type of circuit you're interested in (e.g., "filter", "amplifier", "power supply", etc.)
        """

        return prompt

    @mcp.prompt()
    def compare_circuit_patterns() -> str:
        """Prompt for comparing circuit patterns across designs."""
        prompt = """
        I want to compare circuit patterns across multiple KiCad designs. Can you help me:

        1. Identify common circuit patterns in these designs
        2. Compare how similar circuits are implemented across the designs
        3. Identify which implementation is most optimal
        4. Suggest best practices based on the comparison

        My KiCad projects are located at:
        [Enter paths to multiple .kicad_pro files]

        Please focus on identifying differences in approaches to the same functional circuit blocks.
        """

        return prompt

    @mcp.prompt()
    def explain_circuit_function() -> str:
        """Prompt for explaining the function of identified circuits."""
        prompt = """
        I'd like to understand the function of the circuits in my KiCad design. Can you help me:

        1. Identify the main circuit blocks in my schematic
        2. Explain how each circuit block works in detail
        3. Describe how they interact with each other
        4. Explain the overall signal flow through the system

        My KiCad schematic is located at:
        [Enter path to your .kicad_sch file]

        Please provide explanations that would help someone unfamiliar with the design understand it.
        """

        return prompt
