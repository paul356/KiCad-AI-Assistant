"""
BOM-related prompt templates for KiCad.
"""

from fastmcp import FastMCP


def register_bom_prompts(mcp: FastMCP) -> None:
    """Register BOM-related prompt templates with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.prompt()
    def analyze_components() -> str:
        """Prompt for analyzing a KiCad project's components."""
        prompt = """
        I'd like to analyze the components used in my KiCad PCB design. Can you help me with:

        1. Identifying all the components in my design
        2. Analyzing the distribution of component types
        3. Checking for any potential issues or opportunities for optimization
        4. Suggesting any alternatives for hard-to-find or expensive components

        My KiCad project is located at:
        [Enter the full path to your .kicad_pro file here]

        Please use the BOM analysis tools to help me understand my component usage.
        """

        return prompt

    @mcp.prompt()
    def cost_estimation() -> str:
        """Prompt for estimating project costs based on BOM."""
        prompt = """
        I need to estimate the cost of my KiCad PCB project for:

        1. A prototype run (1-5 boards)
        2. A small production run (10-100 boards)
        3. Larger scale production (500+ boards)

        My KiCad project is located at:
        [Enter the full path to your .kicad_pro file here]

        Please analyze my BOM to help estimate component costs, and provide guidance on:

        - Which components contribute most to the overall cost
        - Where I might find cost savings
        - Potential volume discounts for larger runs
        - Suggestions for alternative components that could reduce costs
        - Estimated PCB fabrication costs based on board size and complexity

        If my BOM doesn't include cost data, please suggest how I might find pricing information for my components.
        """

        return prompt

    @mcp.prompt()
    def bom_export_help() -> str:
        """Prompt for assistance with exporting BOMs from KiCad."""
        prompt = """
        I need help exporting a Bill of Materials (BOM) from my KiCad project. I'm interested in:

        1. Understanding the different BOM export options in KiCad
        2. Exporting a BOM with specific fields (reference, value, footprint, etc.)
        3. Generating a BOM in a format compatible with my preferred supplier
        4. Adding custom fields to my components that will appear in the BOM

        My KiCad project is located at:
        [Enter the full path to your .kicad_pro file here]

        Please guide me through the process of creating a well-structured BOM for my project.
        """

        return prompt

    @mcp.prompt()
    def component_sourcing() -> str:
        """Prompt for help with component sourcing."""
        prompt = """
        I need help sourcing components for my KiCad PCB project. Specifically, I need assistance with:

        1. Identifying reliable suppliers for my components
        2. Finding alternatives for any hard-to-find or obsolete parts
        3. Understanding lead times and availability constraints
        4. Balancing cost versus quality considerations

        My KiCad project is located at:
        [Enter the full path to your .kicad_pro file here]

        Please analyze my BOM and provide guidance on sourcing these components efficiently.
        """

        return prompt

    @mcp.prompt()
    def bom_comparison() -> str:
        """Prompt for comparing BOMs between two design revisions."""
        prompt = """
        I have two versions of a KiCad project and I'd like to compare the changes between their Bills of Materials. I need to understand:

        1. Which components were added or removed
        2. Which component values or footprints changed
        3. The impact of these changes on the overall design
        4. Any potential issues introduced by these changes

        My original KiCad project is located at:
        [Enter the full path to your first .kicad_pro file here]

        My revised KiCad project is located at:
        [Enter the full path to your second .kicad_pro file here]

        Please analyze the BOMs from both projects and help me understand the differences between them.
        """

        return prompt
