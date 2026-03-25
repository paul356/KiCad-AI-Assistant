"""
Design Rule Check (DRC) resources for KiCad PCB files.
"""
import os

from fastmcp import FastMCP

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.drc_history import get_drc_history
from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli

def register_drc_resources(mcp: FastMCP) -> None:
    """Register DRC resources with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.resource("kicad://drc/history/{project_path}")
    def get_drc_history_report(project_path: str) -> str:
        """Get a formatted DRC history report for a KiCad project.
        
        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            
        Returns:
            Markdown-formatted DRC history report
        """
        print(f"Generating DRC history report for project: {project_path}")
        
        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"
        
        # Get history entries
        history_entries = get_drc_history(project_path)
        
        if not history_entries:
            return "# DRC History\n\nNo DRC history available for this project. Run a DRC check first."
        
        # Format results as Markdown
        project_name = os.path.basename(project_path)[:-10]  # Remove .kicad_pro
        report = f"# DRC History for {project_name}\n\n"
        
        # Add trend visualization
        if len(history_entries) >= 2:
            report += "## Trend\n\n"
            
            # Create a simple ASCII chart of violations over time
            report += "```\n"
            report += "Violations\n"
            
            # Find min/max for scaling
            max_violations = max(entry.get("total_violations", 0) for entry in history_entries)
            if max_violations < 10:
                max_violations = 10  # Minimum scale
            
            # Generate chart (10 rows high)
            for i in range(10, 0, -1):
                threshold = (i / 10) * max_violations
                report += f"{int(threshold):4d} |"
                
                for entry in reversed(history_entries):  # Oldest to newest
                    violations = entry.get("total_violations", 0)
                    if violations >= threshold:
                        report += "*"
                    else:
                        report += " "
                
                report += "\n"
            
            # Add x-axis
            report += "     " + "-" * len(history_entries) + "\n"
            report += "     "
            
            # Add dates (shortened)
            for entry in reversed(history_entries):
                date = entry.get("datetime", "")
                if date:
                    # Just show month/day
                    shortened = date.split(" ")[0].split("-")[-2:]
                    report += shortened[-2][0]  # First letter of month
                
            report += "\n```\n"
        
        # Add history table
        report += "## History Entries\n\n"
        report += "| Date | Time | Violations | Categories |\n"
        report += "| ---- | ---- | ---------- | ---------- |\n"
        
        for entry in history_entries:
            date_time = entry.get("datetime", "Unknown")
            if " " in date_time:
                date, time = date_time.split(" ")
            else:
                date, time = date_time, ""
                
            violations = entry.get("total_violations", 0)
            categories = entry.get("violation_categories", {})
            category_count = len(categories)
            
            report += f"| {date} | {time} | {violations} | {category_count} |\n"
        
        # Add detailed information about the most recent run
        if history_entries:
            most_recent = history_entries[0]
            report += "\n## Most Recent Check Details\n\n"
            report += f"**Date:** {most_recent.get('datetime', 'Unknown')}\n\n"
            report += f"**Total Violations:** {most_recent.get('total_violations', 0)}\n\n"
            
            categories = most_recent.get("violation_categories", {})
            if categories:
                report += "**Violation Categories:**\n\n"
                for category, count in categories.items():
                    report += f"- {category}: {count}\n"
            
            # Add comparison with first run if available
            if len(history_entries) > 1:
                first_run = history_entries[-1]
                first_violations = first_run.get("total_violations", 0)
                current_violations = most_recent.get("total_violations", 0)
                
                report += "\n## Progress Since First Check\n\n"
                report += f"**First Check Date:** {first_run.get('datetime', 'Unknown')}\n"
                report += f"**First Check Violations:** {first_violations}\n"
                report += f"**Current Violations:** {current_violations}\n"
                
                if first_violations > current_violations:
                    fixed = first_violations - current_violations
                    report += f"**Progress:** You've fixed {fixed} violations! 🎉\n"
                elif first_violations < current_violations:
                    added = current_violations - first_violations
                    report += f"**Alert:** {added} new violations have been introduced since the first check.\n"
                else:
                    report += "**Status:** The number of violations has remained the same since the first check.\n"
        
        return report
    
    @mcp.resource("kicad://drc/{project_path}")
    def get_drc_report(project_path: str) -> str:
        """Get a formatted DRC report for a KiCad project.
        
        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            
        Returns:
            Markdown-formatted DRC report
        """
        print(f"Generating DRC report for project: {project_path}")
        
        if not os.path.exists(project_path):
            return f"Project not found: {project_path}"
        
        # Get PCB file from project
        files = get_project_files(project_path)
        if "pcb" not in files:
            return "PCB file not found in project"
        
        pcb_file = files["pcb"]
        print(f"Found PCB file: {pcb_file}")
        
        # Try to run DRC via command line
        drc_results = run_drc_via_cli(pcb_file)
        
        if not drc_results["success"]:
            error_message = drc_results.get("error", "Unknown error")
            return f"# DRC Check Failed\n\nError: {error_message}"
        
        # Format results as Markdown
        project_name = os.path.basename(project_path)[:-10]  # Remove .kicad_pro
        pcb_name = os.path.basename(pcb_file)
        
        report = f"# Design Rule Check Report for {project_name}\n\n"
        report += f"PCB file: `{pcb_name}`\n\n"
        
        # Add summary
        total_violations = drc_results.get("total_violations", 0)
        report += f"## Summary\n\n"
        
        if total_violations == 0:
            report += "✅ **No DRC violations found**\n\n"
        else:
            report += f"❌ **{total_violations} DRC violations found**\n\n"
        
        # Add violation categories
        categories = drc_results.get("violation_categories", {})
        if categories:
            report += "## Violation Categories\n\n"
            for category, count in categories.items():
                report += f"- **{category}**: {count} violations\n"
            report += "\n"
        
        # Add detailed violations
        violations = drc_results.get("violations", [])
        if violations:
            report += "## Detailed Violations\n\n"
            
            # Limit to first 50 violations to keep the report manageable
            displayed_violations = violations[:50]
            
            for i, violation in enumerate(displayed_violations, 1):
                message = violation.get("message", "Unknown error")
                severity = violation.get("severity", "error")
                
                # Extract location information if available
                location = violation.get("location", {})
                x = location.get("x", 0)
                y = location.get("y", 0)
                
                report += f"### Violation {i}\n\n"
                report += f"- **Type**: {message}\n"
                report += f"- **Severity**: {severity}\n"
                
                if x != 0 or y != 0:
                    report += f"- **Location**: X={x:.2f}mm, Y={y:.2f}mm\n"
                
                report += "\n"
            
            if len(violations) > 50:
                report += f"*...and {len(violations) - 50} more violations (use the `run_drc_check` tool for complete results)*\n\n"
        
        # Add recommendations
        report += "## Recommendations\n\n"
        
        if total_violations == 0:
            report += "Your PCB design passes all design rule checks. It's ready for manufacturing!\n\n"
        else:
            report += "To fix these violations:\n\n"
            report += "1. Open your PCB in KiCad's PCB Editor\n"
            report += "2. Run the DRC by clicking the 'Inspect → Design Rules Checker' menu item\n"
            report += "3. Click on each error in the DRC window to locate it on the PCB\n"
            report += "4. Fix the issue according to the error message\n"
            report += "5. Re-run DRC to verify your fixes\n\n"
            
            # Add common solutions for frequent error types
            if categories:
                most_common_error = max(categories.items(), key=lambda x: x[1])[0]
                report += "### Common Solutions\n\n"
                
                if "clearance" in most_common_error.lower():
                    report += "**For clearance violations:**\n"
                    report += "- Reroute traces to maintain minimum clearance requirements\n"
                    report += "- Check layer stackup and adjust clearance rules if necessary\n"
                    report += "- Consider adjusting trace widths\n\n"
                    
                elif "width" in most_common_error.lower():
                    report += "**For width violations:**\n"
                    report += "- Increase trace widths to meet minimum requirements\n"
                    report += "- Check current requirements for your traces\n\n"
                    
                elif "drill" in most_common_error.lower():
                    report += "**For drill violations:**\n"
                    report += "- Adjust hole sizes to meet manufacturing constraints\n"
                    report += "- Check via settings\n\n"
        
        return report
