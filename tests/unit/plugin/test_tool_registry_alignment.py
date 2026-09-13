"""
Verify that every @mcp.tool() in kcaa/tools/ has a corresponding entry
in the plugin-side tool_registry.py TOOL_POLICIES dict.

This prevents the "Tool policy registry is missing entries" error
that occurs when the LLM client calls a tool not covered by the
plugin's explicit policy registry.
"""

import ast
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


def _decorated_tool_functions() -> dict[str, str]:
    """Map every tool name to the first non-empty line of its docstring.

    Covers both @mcp.tool()-decorated functions and the
    ``registry["name"] = fn`` loop idiom used by ``register_project_tools``
    (the tool name is the registry key; its summary comes from the assigned
    function's docstring). Uses ast so indentation/annotations are handled
    reliably.
    """
    summary_lines: dict[str, str] = {}
    for py_file in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))

        def first_line(node: ast.AST) -> str:
            doc = ast.get_docstring(node, clean=False) or ""
            return next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")

        fn_docs: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            fn_docs[node.name] = first_line(node)
            decorated = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "mcp"
                and d.func.attr == "tool"
                for d in node.decorator_list
            )
            if decorated:
                summary_lines[node.name] = fn_docs[node.name]

        # Loop-idioom registrations: registry["<tool>"] = <function>
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "registry"
                and isinstance(target.slice, ast.Constant)
                and isinstance(node.value, ast.Name)
            ):
                continue
            summary_lines[target.slice.value] = fn_docs.get(node.value.id, "")
    return summary_lines


def _collect_plugin_profile_tools() -> set[str]:
    """Tools the plugin profile actually registers (kcaa/server.py).

    Mirrors ``_register_plugin_profile``: every ``register_*_tools(mcp)`` call
    maps through the server's own imports to its tools module; each tool the
    module exposes becomes part of the plugin surface. A ``tools=(...)``
    keyword (project tools) restricts the surface to the listed subset.
    """
    server_path = _REPO_ROOT / "kcaa" / "server.py"
    server = ast.parse(server_path.read_text(encoding="utf-8"))

    # import fn name -> kcaa/tools/<module>.py via server.py imports
    reg_to_module: dict[str, str] = {}
    for node in ast.walk(server):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("kcaa.tools."):
            continue
        module = node.module.removeprefix("kcaa.tools.")
        for alias in node.names:
            reg_to_module[alias.asname or alias.name] = module

    profile_fn = next(
        n
        for n in ast.walk(server)
        if isinstance(n, ast.FunctionDef) and n.name == "_register_plugin_profile"
    )
    tools: set[str] = set()
    for stmt in profile_fn.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        fn = call.func
        reg_name = fn.attr if isinstance(fn, ast.Attribute) else fn.id
        if reg_name not in reg_to_module:
            continue
        tools_kw = next((k.value for k in call.keywords if k.arg == "tools"), None)
        if isinstance(tools_kw, ast.Tuple | ast.List):
            for elt in tools_kw.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    tools.add(elt.value)
            continue
        module_tools = _decorated_tool_functions_in(reg_to_module[reg_name])
        tools |= module_tools
    return tools


def _decorated_tool_functions_in(module: str) -> set[str]:
    """All tool names exposed by one kcaa/tools/<module>.py."""
    tree = ast.parse((_TOOLS_DIR / f"{module}.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorated = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "mcp"
            and d.func.attr == "tool"
            for d in node.decorator_list
        )
        if decorated:
            names.add(node.name)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "registry"
            and isinstance(node.targets[0].slice, ast.Constant)
        ):
            names.add(node.targets[0].slice.value)
    return names


def test_plugin_profile_tools_have_registry_entries() -> None:
    """Every tool the plugin profile exposes must be in TOOL_POLICIES.

    The mandatory-source check above is one-directional over a fixed file
    list; a tool added to an already-covered plugin module would show in the
    catalog but be permanently refused by enable_tool. This derives the real
    plugin surface from server.py so the two can never drift.
    """
    plugin_tools = _collect_plugin_profile_tools()
    registered = _collect_registry_tools()

    missing = sorted(plugin_tools - registered)
    assert not missing, (
        f"{len(missing)} plugin-profile tool(s) missing from "
        f"tool_registry.py TOOL_POLICIES.\n"
        f"Add entries to the TOOL_POLICIES dict at:\n"
        f"  {_PLUGIN_REGISTRY}\n\n"
        f"Missing:\n  " + "\n  ".join(missing)
    )


def test_tools_have_valid_docstring_summary_line() -> None:
    """Every @mcp.tool() tool must have a non-empty, <= 100 char docstring.

    The prompt catalog renders the first non-empty docstring line as the
    tool summary (issue #129); a tool without any docstring text would
    produce "- name: " and an oversized first line would be truncated
    mid-word in the catalog block.
    """
    summary_lines = _decorated_tool_functions()
    assert summary_lines, "no @mcp.tool() tools found - scan is broken"

    problems: list[str] = []
    for name, first in sorted(summary_lines.items()):
        if not first:
            problems.append(f"{name}: no non-empty docstring line")
        elif len(first) > 100:
            problems.append(f"{name}: first docstring line too long ({len(first)} chars)")
    assert not problems, (
        "Every tool's first non-empty docstring line must be <= 100 chars "
        "(it is rendered as the tool summary in the prompt catalog):\n  " + "\n  ".join(problems)
    )
