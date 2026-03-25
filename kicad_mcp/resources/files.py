"""
File content resources for KiCad files.
"""
import os
from fastmcp import FastMCP


def register_file_resources(mcp: FastMCP) -> None:
    """Register file-related resources with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.resource("kicad://schematic/{schematic_path}")
    def get_schematic_info(schematic_path: str) -> str:
        """Extract information from a KiCad schematic file."""
        if not os.path.exists(schematic_path):
            return f"Schematic file not found: {schematic_path}"
        
        # KiCad schematic files are in S-expression format (not JSON)
        # This is a basic extraction of text-based information
        try:
            with open(schematic_path, 'r') as f:
                content = f.read()
            
            # Basic extraction of components
            components = []
            for line in content.split('\n'):
                if '(symbol ' in line and 'lib_id' in line:
                    components.append(line.strip())
            
            result = f"# Schematic: {os.path.basename(schematic_path)}\n\n"
            result += f"## Components (Estimated Count: {len(components)})\n\n"
            
            # Extract a sample of components
            for i, comp in enumerate(components[:10]):
                result += f"{comp}\n"
            
            if len(components) > 10:
                result += f"\n... and {len(components) - 10} more components\n"
            
            return result
        
        except Exception as e:
            return f"Error reading schematic file: {str(e)}"
