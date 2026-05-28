"""
DRC prompt templates for KiCad PCB design.
"""

from fastmcp import FastMCP


def register_drc_prompts(mcp: FastMCP) -> None:
    """Register DRC prompt templates with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.prompt()
    def fix_drc_violations() -> str:
        """Prompt for assistance with fixing DRC violations."""
        return """
        I'm trying to fix DRC (Design Rule Check) violations in my KiCad PCB design. I need help with:

        1. Understanding what these DRC errors mean
        2. Knowing how to fix each type of violation
        3. Best practices for preventing DRC issues in future designs

        Here are the specific DRC errors I'm seeing (please list errors from your DRC report, or use the kicad://drc/path_to_project resource to see your full DRC report):

        [list your DRC errors here]

        Please help me understand these errors and provide step-by-step guidance on fixing them.
        """

    @mcp.prompt()
    def custom_design_rules() -> str:
        """Prompt for assistance with creating custom design rules."""
        return """
        I want to create custom design rules for my KiCad PCB. My project has the following requirements:

        1. [Describe your project's specific requirements]
        2. [List any special considerations like high voltage, high current, RF, etc.]
        3. [Mention any manufacturing constraints]

        Please help me set up appropriate design rules for my KiCad project, including:

        - Minimum trace width and clearance settings
        - Via size and drill constraints
        - Layer stack considerations
        - Other important design rules

        Explain how to configure these rules in KiCad and how to verify they're being applied correctly.
        """
