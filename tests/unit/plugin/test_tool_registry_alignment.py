"""
Verify that every @mcp.tool() in kcaa/tools/ has a corresponding entry
in the plugin-side tool_registry.py TOOL_POLICIES dict.

This prevents the "Tool policy registry is missing entries" error
that occurs when the LLM client calls a tool not covered by the
plugin's explicit policy registry.
"""

from pathlib import Path
import re

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # kcaa project root

# Plugin tool_registry.py — co-located with the plugin source inside the project.
_PLUGIN_REGISTRY = _REPO_ROOT / "kicad_plugin" / "tool_registry.py"

_TOOLS_DIR = _REPO_ROOT / "kcaa" / "tools"


def _collect_kcaa_tools() -> set[str]:
    """Scan all ``@mcp.tool()`` decorated functions under kcaa/tools/.

    Also recognises the ``mcp.tool()(registry["name"])`` loop idiom used by
    ``register_project_tools`` to register a selectable tool subset.
    """
    tools: set[str] = set()
    for py_file in sorted(_TOOLS_DIR.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        for m in re.finditer(r'@mcp\.tool\(\s*name\s*=\s*"([^"]+)"\s*\)', text):
            tools.add(m.group(1))
        for m in re.finditer(
            r"@mcp\.tool\(([^)]*)\)\s*\n\s*(?:async\s+)?def\s+(\w+)\s*\(",
            text,
        ):
            if re.search(r"\bname\s*=", m.group(1)):
                continue
            tools.add(m.group(2))
        for m in re.finditer(r'registry\["([^"]+)"\]\s*=', text):
            tools.add(m.group(1))
    return tools


# Source files whose tools MUST have a TOOL_POLICIES entry.
# Other files (BOM, thumbnail, analysis, validation, project) contain
# pre-existing gaps that are not enforced here.
_MANDATORY_SOURCES: frozenset[str] = frozenset(
    {
        "pcb_routing_tools.py",
        "pcb_query_tools.py",
        "pcb_edit_tools.py",
        "pcb_placement_tools.py",
        "pcb_placement_helpers.py",
        "pcb_group_tools.py",
        "pcb_library_tools.py",
        "pcb_zone_tools.py",
        "drc_tools.py",
    }
)


def _collect_mandatory_tools() -> set[str]:
    """Return tools from source files that must have registry entries."""
    tools: set[str] = set()
    for py_file in _TOOLS_DIR.iterdir():
        if py_file.name not in _MANDATORY_SOURCES:
            continue
        text = py_file.read_text(encoding="utf-8")
        for m in re.finditer(r'@mcp\.tool\(\s*name\s*=\s*"([^"]+)"\s*\)', text):
            tools.add(m.group(1))
        for m in re.finditer(
            r"@mcp\.tool\(([^)]*)\)\s*\n\s*(?:async\s+)?def\s+(\w+)\s*\(",
            text,
        ):
            if re.search(r"\bname\s*=", m.group(1)):
                continue
            tools.add(m.group(2))
    return tools


def _collect_registry_tools() -> set[str]:
    """Parse the plugin-side tool_registry.py and return all TOOL_POLICIES keys."""
    if not _PLUGIN_REGISTRY.exists():
        pytest.skip(f"Plugin registry not found: {_PLUGIN_REGISTRY}")

    text = _PLUGIN_REGISTRY.read_text(encoding="utf-8")

    # Find the TOOL_POLICIES dict and parse all string keys
    tools: set[str] = set()
    in_dict = False
    for line in text.splitlines():
        stripped = line.strip()
        # Detect start of TOOL_POLICIES dict
        if stripped.startswith("TOOL_POLICIES:"):
            in_dict = True
            continue
        if in_dict:
            # Stop at the closing brace at the top level
            if stripped == "}":
                break
            # Match lines like:    "tool_name": ToolPolicy(...)
            m = re.match(r'^\s*"([^"]+)":\s*ToolPolicy\(', stripped)
            if m:
                tools.add(m.group(1))
    return tools


def test_mandatory_tools_have_registry_entries() -> None:
    """PCB/routing/DRC/placement tools must all be in the plugin's TOOL_POLICIES.

    A mismatch here causes the LLM client to raise "Tool policy registry
    is missing entries" when it tries to call the tool.
    """
    mandatory = _collect_mandatory_tools()
    registered = _collect_registry_tools()

    missing = sorted(mandatory - registered)
    assert not missing, (
        f"{len(missing)} mandatory tool(s) missing from "
        f"tool_registry.py TOOL_POLICIES.\n"
        f"Add entries to the TOOL_POLICIES dict at:\n"
        f"  {_PLUGIN_REGISTRY}\n\n"
        f"Missing:\n  " + "\n  ".join(missing)
    )


def test_registry_has_no_stale_entries() -> None:
    """Every entry in TOOL_POLICIES should correspond to an existing tool."""
    kcaa_tools = _collect_kcaa_tools()
    registry_tools = _collect_registry_tools()

    stale = sorted(registry_tools - kcaa_tools)
    assert not stale, (
        f"{len(stale)} tool(s) in tool_registry.py TOOL_POLICIES have "
        f"no corresponding @mcp.tool() in kcaa/tools/:\n  " + "\n  ".join(stale)
    )
