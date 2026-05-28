"""
Prompt templates for KiCad interactions.
"""
from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register prompt templates with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.prompt()
    def create_new_component() -> str:
        """Prompt for creating a new KiCad component."""
        prompt = """
        I want to create a new component in KiCad for my PCB design. I need help with:

        1. Deciding on the correct component package/footprint
        2. Creating the schematic symbol
        3. Connecting the schematic symbol to the footprint
        4. Adding the component to my design

        Please provide step-by-step instructions on how to create a new component in KiCad.
        """
        
        return prompt

    @mcp.prompt()
    def debug_pcb_issues() -> str:
        """Prompt for debugging common PCB issues."""
        prompt = """
        I'm having issues with my KiCad PCB design. Can you help me troubleshoot the following problems:

        1. Design rule check (DRC) errors
        2. Electrical rule check (ERC) errors
        3. Footprint mismatches
        4. Routing challenges

        Please provide a systematic approach to identifying and fixing these issues in KiCad.
        """
        
        return prompt

    @mcp.prompt()
    def pcb_manufacturing_checklist() -> str:
        """Prompt for PCB manufacturing preparation checklist."""
        prompt = """
        I'm preparing to send my KiCad PCB design for manufacturing. Please help me with a comprehensive checklist of things to verify before submitting my design, including:

        1. Design rule compliance
        2. Layer stack configuration
        3. Manufacturing notes and specifications
        4. Required output files (Gerber, drill, etc.)
        5. Component placement considerations

        Please provide a detailed checklist I can follow to ensure my design is ready for manufacturing.
        """
