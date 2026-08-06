"""
LLM client: drives the agentic tool-call loop between the engineer's message
and the MCP server.

Supports OpenAI-compatible and Anthropic APIs. The client is intentionally
thin — it delegates all KiCad knowledge to the MCP tool surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import platform
import random
import subprocess  # nosec B404 -- controlled subprocess execution, no user input
import time
from typing import Any

from .tool_registry import get_missing_tool_policies, get_tool_policy

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# P1  stale-query detection: category mappings
# ------------------------------------------------------------------

# Categories assigned to *query* tools based on what they return.
# Library/lookup-only queries (search_symbols, get_symbol, …) are excluded
# because they are never invalidated by a file mutation.
QUERY_CATEGORY: dict[str, str] = {
    # Schematic queries
    "list_symbol_properties": "symbol_properties",
    "get_symbol_pins": "symbol_pins",
    "check_reference_conflicts": "symbol_inventory",
    "list_labels_in_schematic": "labels",
    "extract_project_netlist": "netlist",
    "extract_schematic_netlist": "netlist",
    "find_component_connections": "netlist",
    "get_schematic_sheet_info": "sheet_meta",
    "list_sheet_symbols": "sheet_inventory",
    "get_sheet_hierarchy": "sheet_structure",
    # PCB queries
    "get_board_info": "pcb_meta",
    "list_footprints": "pcb_inventory",
    "get_footprint": "pcb_properties",
    "list_nets": "pcb_nets",
    "get_ratsnest": "pcb_nets",
    "score_placement": "pcb_placement",
    "suggest_placement_order": "pcb_placement",
    "get_board_outline": "pcb_outline",
    "get_footprint_bbox": "pcb_bbox",
    "get_board_bounding_box": "pcb_bbox",
    "find_free_pcb_area": "pcb_placement",
    "list_footprint_groups": "pcb_groups",
    "get_footprint_group": "pcb_groups",
    "score_footprint_group": "pcb_groups",
    "list_symbol_groups": "symbol_groups",
    "get_symbol_group": "symbol_groups",
    "score_symbol_group": "symbol_groups",
    "list_zones": "pcb_zones",
}

# Per-mutation categories that become stale.
MUTATION_RIPPLES: dict[str, set[str]] = {
    # Schematic mutations
    "add_symbol_to_schematic": {
        "symbol_inventory",
        "symbol_properties",
        "symbol_pins",
        "netlist",
        "placement",
    },
    "remove_symbol_from_schematic": {
        "symbol_inventory",
        "symbol_properties",
        "symbol_pins",
        "netlist",
        "placement",
    },
    "set_symbol_property": {"symbol_properties"},
    "rename_symbol": {"symbol_inventory"},
    "delete_symbol_property": {"symbol_properties"},
    "move_component": {"symbol_inventory", "placement"},
    "place_symbol_relative": {"symbol_inventory", "placement"},
    "add_label_to_schematic": {"labels"},
    "delete_label_from_schematic": {"labels"},
    "connect_points_with_wire": {"netlist"},
    "connect_pins_with_wire": {"netlist"},
    "delete_wire_from_schematic": {"netlist"},
    "add_sheet_symbol": {"sheet_inventory", "sheet_structure", "placement"},
    "remove_sheet_symbol": {"sheet_inventory", "sheet_structure", "placement"},
    "update_sheet_symbol": {"sheet_inventory", "sheet_structure"},
    "add_sheet_pin": {"sheet_structure"},
    "remove_sheet_pin": {"sheet_structure"},
    # PCB mutations
    "set_footprint_position": {"pcb_inventory", "pcb_placement"},
    "flip_footprint": {"pcb_inventory", "pcb_placement"},
    "set_footprint_property": {"pcb_properties"},
    "clear_board_outline": {"pcb_outline"},
    "add_board_outline_segment": {"pcb_outline"},
    "add_board_outline_arc": {"pcb_outline"},
    "set_board_outline_rect": {"pcb_outline"},
    "align_footprints": {"pcb_inventory", "pcb_placement"},
    "distribute_footprints": {"pcb_inventory", "pcb_placement"},
    "move_footprints_by_delta": {"pcb_inventory", "pcb_placement"},
    "assign_footprints_to_group": {"pcb_groups", "pcb_inventory"},
    "place_footprint_group": {"pcb_inventory", "pcb_groups", "pcb_placement"},
    "move_footprint_group": {"pcb_inventory", "pcb_groups", "pcb_placement"},
    "rotate_footprint_group": {"pcb_inventory", "pcb_groups", "pcb_placement"},
    "assign_symbols_to_group": {"symbol_groups", "symbol_inventory"},
    "place_symbol_group": {"symbol_inventory", "symbol_groups", "placement"},
    "move_symbol_group": {"symbol_inventory", "symbol_groups", "placement"},
    "rotate_symbol_group": {"symbol_inventory", "symbol_groups", "placement"},
    "add_zone": {"pcb_zones"},
    "delete_zone": {"pcb_zones"},
}

# Token-efficient annotation prefix.
_STALE_PREFIX = (
    "⚠️ STALE — file was modified after this query. Re-query if the data may have changed.\n"
)

_STALE_PREFIX_LEN = len(_STALE_PREFIX)


def _extract_file_path(args: dict[str, Any]) -> str:
    """Return the first file-path value found in *args*, or ``""``."""
    for key in ("file_path", "schematic_path", "pcb_path", "project_path"):
        v = args.get(key, "")
        if v:
            return v
    return ""


_SKILLS_DIR = Path(
    os.environ.get("KCAA_SKILLS_DIR", str(Path(__file__).resolve().parent / "skills"))
)


def _parse_skill_front_matter(text: str) -> dict[str, str]:
    """Return flat front-matter metadata from a skill Markdown file."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def _load_skill_catalog_entries() -> list[dict[str, str]]:
    """Load skill metadata for prompt-time catalog generation."""
    if not _SKILLS_DIR.exists():
        return []

    skills: list[dict[str, str]] = []
    for path in _SKILLS_DIR.glob("*.md"):
        try:
            meta = _parse_skill_front_matter(path.read_text(encoding="utf-8"))
            name = meta.get("name") or path.stem.replace("_", "-")
            skills.append(
                {
                    "name": name,
                    "description": meta.get("description", ""),
                    "priority": meta.get("priority", "50"),
                }
            )
        except Exception:
            log.warning("Failed to read skill file %s", path)

    return sorted(skills, key=lambda s: (-int(s["priority"]), s["name"]))


def _build_skill_catalog_block() -> str:
    """Render a compact catalog of available on-demand workflow skills."""
    skills = _load_skill_catalog_entries()
    if not skills:
        return ""
    names = ", ".join(skill["name"] for skill in skills)
    return "# Skills\n" + names


# ---------------------------------------------------------------------------
# HTTPS shim
#
# KiCad's embedded interpreter on some platforms (notably the Linux AppImage)
# ships without a working ``_ssl`` extension, so an in-process
# ``urllib.request.urlopen("https://…")`` raises
# ``URLError("unknown url type: https")``.  When that happens we shell out to
# the plugin's own venv Python (the same interpreter that runs the MCP server)
# which has full SSL support.
# ---------------------------------------------------------------------------

# Marker substring used to detect the missing-ssl failure mode.
_NO_HTTPS_MARKER = "unknown url type: https"

# Cached result of whether in-process urllib can reach HTTPS.
# None = unknown, True = works, False = SSL unavailable (use subprocess).
_in_process_ssl: bool | None = None

# Storage for reasoning_content (DeepSeek thinking mode)
_current_reasoning: list[str] = []


def _resolve_plugin_python() -> str | None:
    """Return the path to the plugin venv's Python, or None if absent."""
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if platform.system() == "Windows":
        candidate = os.path.join(plugin_dir, ".venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(plugin_dir, ".venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else None


# Subprocess script: read raw body bytes from stdin, perform the POST, emit
# a single-line JSON object on stdout describing the outcome.
# Always exits with code 0 and communicates errors via the JSON payload:
#   success/HTTP error -> {"status": <http_code>, "body": "<response_text>"}
#   network/other error -> {"status": 0, "error": "<description>"}
_SUBPROCESS_SCRIPT = r"""
import json, sys, urllib.request, urllib.error
try:
    url = sys.argv[1]
    headers = json.loads(sys.argv[2])
    timeout = float(sys.argv[3])
    body = sys.stdin.buffer.read()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = {"status": resp.status, "body": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        result = {"status": e.code, "body": e.read().decode("utf-8", "replace")}
    except Exception as e:
        result = {"status": 0, "error": f"{type(e).__name__}: {e}"}
except Exception as e:
    result = {"status": 0, "error": f"subprocess setup error: {type(e).__name__}: {e}"}
sys.stdout.write(json.dumps(result))
sys.stdout.flush()
"""


def _https_post_json(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, str]:
    """POST ``body`` to ``url`` and return ``(status_code, response_text)``.

    Tries in-process ``urllib`` first; on the missing-https-handler failure
    mode, falls back to invoking the plugin venv Python as a one-shot proxy.
    Raises ``RuntimeError`` with a clear message if both paths fail.
    """
    global _in_process_ssl
    import urllib.error
    import urllib.request

    if _in_process_ssl is not False:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 -- MCP client, localhost only
                _in_process_ssl = True
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _in_process_ssl = True
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            if _NO_HTTPS_MARKER not in str(e.reason):
                raise RuntimeError(f"HTTPS request failed: {e}") from e
            _in_process_ssl = False
            # Fall through to subprocess fallback below.
        except Exception as e:  # noqa: BLE001 — surface unexpected errors verbatim
            raise RuntimeError(f"HTTPS request failed: {e}") from e

    # Subprocess fallback: embedded Python lacks working ssl.
    venv_python = _resolve_plugin_python()
    if not venv_python:
        raise RuntimeError(
            "KiCad's embedded Python lacks SSL support and no plugin venv "
            "Python was found. Run 'kicad_plugin/setup_plugin.sh' to create one."
        )

    # Build a clean env using an explicit allowlist (mirrors ServerManager).
    # KiCad's AppImage sets PYTHONHOME/PYTHONPATH/LD_LIBRARY_PATH for its own
    # embedded interpreter; inheriting them would break the venv Python.
    _ENV_ALLOWLIST = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    )
    clean_env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}

    try:
        proc = subprocess.run(  # nosec B603 -- input is validated
            [venv_python, "-I", "-c", _SUBPROCESS_SCRIPT, url, json.dumps(headers), str(timeout)],
            input=body,
            capture_output=True,
            timeout=timeout + 5,
            check=False,
            env=clean_env,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"HTTPS subprocess timed out after {timeout}s") from e

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTPS subprocess crashed (exit {proc.returncode}): {stderr}")

    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"HTTPS subprocess returned invalid JSON: {e}") from e

    status = out.get("status", 0)
    if status == 0:
        raise RuntimeError(f"HTTPS request failed: {out.get('error', 'unknown error')}")
    return int(status), str(out.get("body", ""))


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PROMPT_HEADER = """\
You are a KiCad assistant — modest, cautious, and proactive.
- Before every task, briefly share your intent and plan with the user.
- When a tool returns unexpected results or errors, pause and ask the user
  for guidance. The user is more experienced at solving circuit design
  problems — defer to their judgment.
- Edit schematics/PCBs via MCP tools.
- Unless asked, never call `save_file_version`, `reload_kicad`,
  `check_kicad_ipc_connection`, or `save_document`.
- The framework handles snapshots/reloads. Use ``list_skills()`` /
  ``get_skill(name)`` for guides.\
"""

_PROMPT_SCHEMATIC = """\

# Schematic coordinate system
- All coordinates are in **millimetres** with **+X right, +Y DOWN** (KiCad
  schematic screen convention).
- Schematic symbol local coordinates (in .kicad_sym files) use **Y-UP**.
  However, every tool that takes or returns placed-symbol coordinates uses
  Y-DOWN. You only ever reason in Y-DOWN.
- Rotation in symbol (.kicad_sym) files is **counterclockwise (CCW)**.
  0°=unrotated, 90°=tilt left, 180°=flipped, 270°=tilt right.
- The schematic grid is **1.27 mm (50 mil)**. add_symbol_to_schematic and
  move_component snap automatically; rotation is restricted to 0/90/180/270.
- Default sheet is **A4 (297 x 210 mm)**. At the start of each request
  that involves spatial changes, call get_schematic_sheet_info once to
  confirm the actual paper size and to learn the recommended drawing area
  (it accounts for the title block).

# Hard rules (schematic)
- Always call extract_schematic_netlist (or get_schematic_sheet_info) for
  state before making spatial changes. Never invent coordinates.
- Use the active_schematic path from the context block below as the
  schematic_path argument for every editing tool.
- Every mutation tool automatically writes a .kicad_sch.bak backup and saves
  to disk. The plugin framework handles the version snapshot and final
  `reload_kicad(...)` call automatically for successful file mutations.
- Report errors clearly. Do not silently retry failed tool calls.
- If find_free_area returns no candidates and no relative placement is
  appropriate, ASK the engineer instead of guessing a position.\
"""

_PROMPT_PCB = """\

# PCB coordinate system
- All PCB coordinates are **millimetres**, **+X right, +Y DOWN** (same screen
  convention as schematics).
- Footprint (.kicad_mod) files internally use **Y-DOWN** coordinates.
- Rotation in footprint (.kicad_mod) files is **counterclockwise (CCW)**.
  0°=unrotated, 90°=tilt left, 180°=flipped, 270°=tilt right.
- There is no auto-snap for PCB tools.  Pass coordinates already aligned to
  your board grid.  Typical grids: **0.1 mm or 0.05 mm** for SMD work,
  **1.27 mm (50 mil)** for through-hole.
- PCB layers of interest: ``F.Cu`` / ``B.Cu`` (copper), ``F.SilkS`` /
  ``B.SilkS`` (silkscreen), ``F.Courtyard`` / ``B.Courtyard`` (keep-out),
  ``Edge.Cuts`` (board outline).

# Hard rules (PCB)
- Always call get_board_info + list_footprints before making spatial changes.
  Never invent footprint coordinates.
- Use the active_pcb path from the context block below as the pcb_path
  argument for every PCB editing tool.
- Every mutation tool automatically writes a .kicad_pcb.bak backup and saves
  to disk. The plugin framework handles the version snapshot and final
  `reload_kicad(...)` call automatically for successful file mutations.
- Report errors clearly. Do not silently retry failed tool calls.
- Do not overlap footprint courtyards.  Verify clearances with
  get_footprint_bbox / get_board_bounding_box before committing a move.\
"""

_PROMPT_SKILL_CATALOG = _build_skill_catalog_block()
if _PROMPT_SKILL_CATALOG:
    _PROMPT_PCB = _PROMPT_PCB + "\n\n" + _PROMPT_SKILL_CATALOG


def build_system_prompt(context_block: str) -> str:
    """Assemble the system prompt with both schematic and PCB sections."""
    return _PROMPT_HEADER + _PROMPT_SCHEMATIC + "\n" + _PROMPT_PCB + "\n\n" + context_block


# ---------------------------------------------------------------------------
# MCP HTTP tool caller
# ---------------------------------------------------------------------------


def _parse_mcp_response_text(text: str) -> dict[str, Any]:
    """Parse a FastMCP streamable-http response body.

    The server may reply with either plain JSON (``json_response=True``) or an
    SSE event stream containing ``data: {...}`` lines. Handle both shapes.
    """
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    # SSE framing: collect the last `data:` payload (the JSON-RPC reply).
    last_data: str | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            last_data = line[5:].strip()
    if last_data is None:
        raise json.JSONDecodeError("No JSON or SSE 'data:' frame in response", text, 0)
    return json.loads(last_data)


def call_mcp_tool(base_url: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Call one MCP tool over HTTP and return the result dict.

    Uses urllib (stdlib only) so there is no extra dependency beyond what
    KiCad's bundled Python provides.
    """
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
    ).encode()

    url = f"{base_url}/mcp"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # FastMCP's streamable-http transport requires the client to
            # advertise both possible reply types or it returns 406.
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 -- MCP client, localhost only
            body = _parse_mcp_response_text(resp.read().decode())
    except urllib.error.URLError as e:
        return {"success": False, "error": f"MCP server unreachable: {e}"}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"Invalid JSON from MCP server: {e}"}

    if "error" in body:
        return {"success": False, "error": body["error"].get("message", str(body["error"]))}

    result = body.get("result", {})
    # FastMCP returns content as a list of {type, text} blocks
    content = result.get("content", [])
    if content and isinstance(content, list) and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except json.JSONDecodeError:
            return {"success": True, "text": content[0]["text"]}
    return result


def _is_retryable_llm_error(error: str) -> bool:
    """Return True if *error* indicates a transient server-side issue worth retrying."""
    lower = error.lower()
    return any(
        kw in lower
        for kw in (
            "503",
            "429",
            "service_unavailable",
            "service is too busy",
            "rate limit",
        )
    )


@dataclass
class _ToolExecutionState:
    """Per-request framework state for snapshot and reload orchestration."""

    snapshotted_paths: set[str] = field(default_factory=set)
    dirty_paths: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# OpenAI-compatible agentic loop
# ---------------------------------------------------------------------------


class LLMClient:
    """
    Drives an agentic conversation loop with tool calls.

    Supports:
      - provider="openai"    → OpenAI chat completions API (and compatible endpoints)
      - provider="anthropic" → Anthropic messages API
    """

    def __init__(self, settings, mcp_base_url: str) -> None:
        self._settings = settings
        self._mcp_base_url = mcp_base_url
        self._context_tokens: int = getattr(settings, "llm_context_tokens", 128_000)
        self._compact_threshold: float = getattr(settings, "llm_compact_threshold", 0.70)
        self._compact_target_threshold: float = getattr(
            settings, "llm_compact_target_threshold", 0.49
        )
        self._keep_recent_turns: int = getattr(settings, "llm_keep_recent_turns", 4)
        self._max_tokens: int = getattr(settings, "llm_max_tokens", 0)
        self._history: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history."""
        self._history = []

    def get_history(self) -> list[dict[str, Any]]:
        """Return a copy of the conversation history for session persistence."""
        return list(self._history)

    def set_history(self, history: list[dict[str, Any]]) -> None:
        """Replace conversation history when restoring a saved session."""
        self._history = list(history)

    def _trim_history(self) -> None:
        """Drop oldest complete turns until history is within the cap.

        A "turn" is one assistant message optionally followed by consecutive
        tool-result messages.  We never split a turn in half, and we never
        drop user messages ahead of assistant turns.
        """
        while len(self._history) > self._max_history:
            # Find the first assistant message and drop it together with any
            # immediately following tool-result messages.
            dropped = False
            for i, msg in enumerate(self._history):
                if msg.get("role") == "assistant":
                    # Count how many consecutive tool messages follow
                    j = i + 1
                    while j < len(self._history) and self._history[j].get("role") == "tool":
                        j += 1
                    del self._history[i:j]
                    dropped = True
                    break
            if not dropped:
                # Fallback: drop the oldest message of any role
                self._history.pop(0)

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate token count as character count divided by 4 (no external tokenizer)."""
        return sum(len(json.dumps(m)) for m in messages) // 4

    def _dedup_tool_calls(self) -> None:
        """Remove stale duplicate tool calls, keeping only the latest call to each tool.

        Walks history from tail to head. A "tool turn" is one assistant message
        that contains tool_calls plus all immediately-following role=="tool" messages.
        If every tool name in a turn has already been seen in a more-recent turn,
        the entire turn (assistant + its tool results) is deleted.
        """
        seen_tools: set[str] = set()
        i = len(self._history) - 1
        while i >= 0:
            msg = self._history[i]
            if msg.get("role") == "tool":
                i -= 1
                continue
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Find the extent of this tool turn: assistant msg at i,
                # followed by consecutive tool messages at i+1 …
                j = i + 1
                while j < len(self._history) and self._history[j].get("role") == "tool":
                    j += 1
                tool_names = {tc["function"]["name"] for tc in msg["tool_calls"]}
                if tool_names.issubset(seen_tools):
                    # All tools in this turn are superseded — drop the whole turn
                    del self._history[i:j]
                    i -= 1
                    continue
                # Keep this turn; mark its tools as seen
                seen_tools.update(tool_names)
            i -= 1

    def _compact_history(self, system_prompt: str, target_summary_chars: int) -> bool:
        """Summarise the oldest part of history into a single compact message.

        Identifies the last ``self._keep_recent_turns`` complete assistant turns
        as the "recent" block that is always preserved verbatim.  Everything before
        those turns is the compactable prefix.

        Builds a text-only transcript of the prefix (user/assistant text only;
        tool names noted inline; raw tool results omitted) and asks the LLM to
        summarise it with user intentions and agreements listed first.

        The returned summary is hard-clipped to ``target_summary_chars`` at a
        word boundary before storing.

        Returns True on success, False if nothing was compacted or an error occurred.
        """
        # ---- Identify the split point (oldest of the recent turns) ----------
        turns_found = 0
        split_idx = len(self._history)  # index of first message in "recent" block
        i = len(self._history) - 1
        while i >= 0 and turns_found < self._keep_recent_turns:
            if self._history[i].get("role") == "assistant":
                split_idx = i
                turns_found += 1
            i -= 1

        # Walk back to include the user message that opened this oldest recent turn
        j = split_idx - 1
        while j >= 0 and self._history[j].get("role") != "user":
            j -= 1
        if j >= 0:
            split_idx = j

        prefix = self._history[:split_idx]
        if len(prefix) < 4:
            return False  # nothing worth compacting yet

        # ---- Build text-only transcript of the prefix -----------------------
        lines: list[str] = []
        for msg in prefix:
            role = msg.get("role", "")
            if role == "user":
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                lines.append(f"User: {content}")
            elif role == "assistant":
                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                if content:
                    lines.append(f"Assistant: {content}")
                if tool_calls:
                    names = ", ".join(tc["function"]["name"] for tc in tool_calls)
                    lines.append(f"Assistant called tools: {names}")
            # role=="tool" messages are omitted entirely

        transcript = "\n".join(lines)
        target_words = max(50, target_summary_chars // 4)
        compact_prompt = (
            f"Summarize the following KiCad assistant session excerpt in approximately "
            f"{target_words} words.\n"
            "Structure your summary as follows:\n"
            "1. User intentions and agreed proposals (list these first)\n"
            "2. Any other relevant context\n\n"
            "Drop intermediate tool roundtrips, failed attempts, superseded proposals, "
            "and tool-returned data (placements, net lists, positions — those will be "
            "re-fetched from tools when needed).\n\n"
            f"<session>\n{transcript}\n</session>"
        )

        # ---- Call LLM for the summary (no tools, short timeout) -------------
        try:
            provider = self._settings.llm_provider
            compaction_history = [{"role": "user", "content": compact_prompt}]
            # Temporarily swap history for the compaction call
            original_history = self._history
            self._history = compaction_history
            if provider == "anthropic":
                resp = self._call_anthropic(
                    "You are a helpful assistant that summarizes conversations concisely.",
                    [],
                )
            else:
                resp = self._call_openai(
                    "You are a helpful assistant that summarizes conversations concisely.",
                    [],
                )
            self._history = original_history
        except Exception as e:
            self._history = original_history  # type: ignore[possibly-undefined]
            log.warning("History compaction failed: %s", e)
            return False

        if resp.get("error"):
            log.warning("History compaction LLM error: %s", resp["error"])
            return False

        summary = resp.get("message", {}).get("content") or ""
        if not summary:
            return False

        # ---- Hard-clip summary to target_summary_chars at a word boundary ---
        if len(summary) > target_summary_chars:
            clipped = summary[:target_summary_chars]
            # Walk back to last space to avoid cutting mid-word
            last_space = clipped.rfind(" ")
            if last_space > target_summary_chars // 2:
                clipped = clipped[:last_space]
            summary = clipped

        # ---- Replace the compactable prefix with the summary message --------
        summary_msg = {
            "role": "user",
            "content": f"[Session summary – earlier context]: {summary}",
        }
        self._history = [summary_msg] + self._history[split_idx:]
        log.debug(
            "History compacted: prefix of %d messages → 1 summary message (%d chars)",
            len(prefix),
            len(summary),
        )
        return True

    def _prune_rollback_history(self) -> None:
        """Prune tool-call turns invalidated by restore_file_version.

        When the LLM restores a file to an earlier version, every tool call that
        mutated or queried that file between the save point and the restore is
        now based on stale state — prune those turns.

        Handles nested restores: starts from the most recent restore and skips
        any restore whose messages fall inside an already-pruned range.
        """
        # ---- Build save-point lookup ------------------------------------------
        # For each save_file_version tool result, record (file_path, version_id) → index.
        save_points: dict[tuple[str, str], int] = {}
        for i, msg in enumerate(self._history):
            if msg.get("role") != "tool":
                continue
            p_idx = self._find_parent_assistant(i)
            if p_idx is None:
                continue
            parent = self._history[p_idx]
            tc_id = msg.get("tool_call_id")
            for tc in parent.get("tool_calls") or []:
                if tc.get("id") != tc_id:
                    continue
                if tc.get("function", {}).get("name") != "save_file_version":
                    continue
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                    result = json.loads(msg.get("content", "{}"))
                    fp = args.get("file_path", "")
                    vid = result.get("version_id", "")
                    if fp and vid:
                        save_points[(fp, vid)] = i
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

        # ---- Scan from tail for restore_file_version -------------------------
        marked: set[int] = set()  # indices to remove  (tool results)
        tool_call_removals: dict[int, list[str]] = {}  # assistant_idx → [tc_id, ...]
        i = len(self._history) - 1
        while i >= 0:
            if i in marked:
                i -= 1
                continue
            msg = self._history[i]
            if msg.get("role") != "tool":
                i -= 1
                continue
            p_idx = self._find_parent_assistant(i)
            if p_idx is None or p_idx in marked:
                i -= 1
                continue
            parent = self._history[p_idx]
            tc_id = msg.get("tool_call_id")
            for tc in parent.get("tool_calls") or []:
                if tc.get("id") != tc_id:
                    continue
                if tc.get("function", {}).get("name") != "restore_file_version":
                    continue
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                    file_path = args.get("file_path", "")
                    version_id = args.get("version_id", "")
                except (json.JSONDecodeError, KeyError, TypeError):
                    break

                save_idx = save_points.get((file_path, version_id))
                if save_idx is None or save_idx >= i:
                    break  # Can't locate save point

                # Mark file-touching tool turns inside (save_idx, i]
                for j in range(save_idx + 1, i):
                    if j in marked:
                        continue
                    mj = self._history[j]
                    if mj.get("role") != "tool":
                        continue
                    pj = self._find_parent_assistant(j)
                    if pj is None:
                        continue
                    # Check the specific tool_call (not whole turn)
                    tcj_id = mj.get("tool_call_id")
                    parent_j = self._history[pj]
                    for tcj in parent_j.get("tool_calls") or []:
                        if tcj.get("id") != tcj_id:
                            continue
                        try:
                            tcj_args = json.loads(tcj["function"].get("arguments", "{}"))
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
                        if self._tool_touches_file(tcj_args, file_path):
                            marked.add(j)
                            tool_call_removals.setdefault(pj, []).append(tcj_id)
                break
            i -= 1

        if not marked:
            return

        # ---- Rebuild history --------------------------------------------------
        # 1. Strip removed tool_calls from assistant messages.
        # 2. Remove assistant messages with no remaining content or tool_calls.
        # 3. Remove marked tool_result messages.
        new_history: list[dict[str, Any]] = []
        for idx, msg in enumerate(self._history):
            if idx in marked:
                continue  # drop tool_result
            if msg.get("role") == "assistant" and idx in tool_call_removals:
                removed_ids = set(tool_call_removals[idx])
                new_tcs = [
                    tc for tc in (msg.get("tool_calls") or []) if tc.get("id") not in removed_ids
                ]
                if not new_tcs and not msg.get("content"):
                    continue  # drop empty assistant message
                msg = dict(msg)  # shallow copy before mutating
                msg["tool_calls"] = new_tcs
            new_history.append(msg)

        pruned = len(self._history) - len(new_history)
        if pruned:
            log.info("_prune_rollback_history: pruned %d stale messages", pruned)
        self._history = new_history

    def _find_parent_assistant(self, tool_result_index: int) -> int | None:
        """Return the index of the assistant message that issued the tool call
        whose result is at *tool_result_index*."""
        for j in range(tool_result_index - 1, -1, -1):
            if self._history[j].get("role") == "assistant":
                return j
        return None

    def _annotate_stale_queries(self) -> None:
        """Annotate query results that became stale due to a later file mutation.

        For each ``file_mutation`` tool result we record the (file_path, categories)
        pairs it invalidates. Then we walk forward and prepend a stale-warning to
        any earlier query result whose category + file_path matches a later mutation.

        This is non-destructive — it only adds a warning prefix, never removes data.
        """
        # ---- 1. Collect (file_path, rippled_categories) for every mutation ----
        mutation_ripples: dict[int, tuple[str, set[str]]] = {}
        for i, msg in enumerate(self._history):
            if msg.get("role") != "tool":
                continue
            p_idx = self._find_parent_assistant(i)
            if p_idx is None:
                continue
            parent = self._history[p_idx]
            tc_id = msg.get("tool_call_id")
            for tc in parent.get("tool_calls") or []:
                if tc.get("id") != tc_id:
                    continue
                name = tc.get("function", {}).get("name", "")
                ripples = MUTATION_RIPPLES.get(name)
                if not ripples:
                    continue
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                fp = _extract_file_path(args)
                if fp:
                    mutation_ripples[i] = (fp, ripples)

        if not mutation_ripples:
            return

        # ---- 2. For each query result, check if a later mutation invalidates it
        annotated = 0
        for i, msg in enumerate(self._history):
            if msg.get("role") != "tool":
                continue
            p_idx = self._find_parent_assistant(i)
            if p_idx is None:
                continue
            parent = self._history[p_idx]
            tc_id = msg.get("tool_call_id")
            for tc in parent.get("tool_calls") or []:
                if tc.get("id") != tc_id:
                    continue
                name = tc.get("function", {}).get("name", "")
                category = QUERY_CATEGORY.get(name)
                if category is None:
                    continue  # not tracked (library query, etc.)
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                q_fp = _extract_file_path(args)
                if not q_fp:
                    continue

                # Check if any LATER mutation on same file invalidates this category
                for m_idx, (m_fp, m_cats) in mutation_ripples.items():
                    if m_idx <= i:
                        continue  # only later mutations matter
                    if m_fp != q_fp:
                        continue
                    if category not in m_cats:
                        continue
                    # Found a matching invalidation
                    content = msg.get("content", "")
                    if not content.startswith(_STALE_PREFIX):
                        msg["content"] = _STALE_PREFIX + content
                        annotated += 1
                    break  # one annotation per query is enough
        if annotated:
            log.info("_annotate_stale_queries: tagged %d stale query result(s)", annotated)

    @staticmethod
    def _tool_touches_file(args: dict[str, Any], file_path: str) -> bool:
        """Return True if *args* reference *file_path* via any known file arg name."""
        for key in ("file_path", "schematic_path", "pcb_path", "project_path"):
            if args.get(key) == file_path:
                return True
        return False

    def _validate_history(self) -> None:
        """Repair orphaned tool_calls in self._history.

        An assistant message that contains ``tool_calls`` must be immediately
        followed by role="tool" messages with matching ``tool_call_id`` values.
        If any are missing (e.g. due to a truncated stream), the API will
        reject the request with HTTP 400.  This method detects and repairs
        such inconsistencies before they reach the LLM.

        Repair strategy for each malformed assistant message:
        * If ALL tool_calls are orphaned → drop the entire assistant message.
        * If SOME are orphaned → strip only the orphaned entries from
          ``tool_calls``, keeping the valid ones and any text content.
        """
        i = 0
        removed_count = 0
        while i < len(self._history):
            msg = self._history[i]
            tool_calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
            if not tool_calls:
                i += 1
                continue

            # Collect all tool_call_ids declared by this assistant message
            declared_ids: set[str] = set()
            tc_names: dict[str, str] = {}
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                if tc_id:
                    declared_ids.add(tc_id)
                    tc_names[tc_id] = tc.get("function", {}).get("name", "?")

            if not declared_ids:
                i += 1
                continue

            # Collect tool_call_ids from immediately-following tool messages
            satisfied_ids: set[str] = set()
            j = i + 1
            while j < len(self._history) and self._history[j].get("role") == "tool":
                tc_id = self._history[j].get("tool_call_id", "")
                if tc_id:
                    satisfied_ids.add(tc_id)
                j += 1

            orphaned = declared_ids - satisfied_ids
            if not orphaned:
                i = j  # skip past the tool-results we just scanned
                continue

            # Log the corruption
            orphaned_info = ", ".join(f"{tc_names.get(oid, '?')}({oid[:8]}…)" for oid in orphaned)
            log.warning(
                "_validate_history: %d orphaned tool_call(s) at history[%d]: %s",
                len(orphaned),
                i,
                orphaned_info,
            )

            if orphaned == declared_ids:
                # All tool_calls are orphaned — drop the entire assistant message
                log.warning(
                    "_validate_history: dropping assistant message at history[%d] "
                    "(all %d tool_calls orphaned)",
                    i,
                    len(orphaned),
                )
                del self._history[i]
                removed_count += 1
                # Don't advance i — next message slides into this position
                continue

            # Partial corruption — strip only orphaned tool_calls
            surviving = [tc for tc in tool_calls if tc.get("id") not in orphaned]
            log.warning(
                "_validate_history: stripping %d orphaned tool_call(s) from "
                "assistant at history[%d] (%d surviving)",
                len(orphaned),
                i,
                len(surviving),
            )
            msg = dict(msg)  # shallow copy before mutating
            msg["tool_calls"] = surviving
            self._history[i] = msg
            removed_count += 1
            i = j  # skip past the tool-results we scanned

        if removed_count:
            log.warning(
                "_validate_history: repaired %d corrupted assistant turn(s)",
                removed_count,
            )

    def _maybe_compact(self, system_prompt: str) -> None:
        """Dedup tool calls then, if the token budget is exceeded, compact history.

        This is the sole history-management entry point; called once per user turn
        before the LLM is invoked.
        """
        self._prune_rollback_history()
        self._annotate_stale_queries()
        self._dedup_tool_calls()

        system_tokens = len(system_prompt) // 4
        history_tokens = self._estimate_tokens(self._history)
        used = system_tokens + history_tokens
        budget = self._context_tokens * self._compact_threshold

        if used <= budget:
            return  # well within limits, nothing to do

        # Estimate the cost of the recent turns we will always keep
        i = len(self._history) - 1
        turns_found = 0
        split_idx = len(self._history)
        while i >= 0 and turns_found < self._keep_recent_turns:
            if self._history[i].get("role") == "assistant":
                split_idx = i
                turns_found += 1
            i -= 1
        # Walk back to include the user message that opened the oldest recent turn
        j = split_idx - 1
        while j >= 0 and self._history[j].get("role") != "user":
            j -= 1
        if j >= 0:
            split_idx = j
        recent_tokens = self._estimate_tokens(self._history[split_idx:])

        target_post_compact = self._context_tokens * self._compact_target_threshold
        target_summary_chars = max(
            200, int((target_post_compact - system_tokens - recent_tokens) * 4)
        )

        self._compact_history(system_prompt, target_summary_chars)
        self._validate_history()  # compaction rebuilds history; verify integrity

    @staticmethod
    def _tool_result_succeeded(result: Any) -> bool:
        """Return True when *result* represents a successful tool execution."""

        if not isinstance(result, dict):
            return True
        if result.get("success") is False:
            return False
        return "error" not in result

    @staticmethod
    def _get_required_path(
        args: dict[str, Any], tool_name: str, arg_name: str | None
    ) -> str | None:
        """Extract a non-empty path argument required by the tool policy."""

        if not arg_name:
            return None
        value = args.get(arg_name)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        log.warning("Tool %s is missing required %s argument", tool_name, arg_name)
        return None

    @staticmethod
    def _format_reload_failure(result: dict[str, Any]) -> str:
        """Render a concise user-facing warning for a failed auto reload."""

        errors = result.get("errors")
        if isinstance(errors, dict) and errors:
            parts = [f"{path}: {message}" for path, message in sorted(errors.items())]
            return "KiCad reload failed: " + "; ".join(parts)
        failed = result.get("failed")
        if isinstance(failed, list) and failed:
            return "KiCad reload failed for: " + ", ".join(str(path) for path in failed)
        return str(result.get("error") or "KiCad reload failed.")

    @staticmethod
    def _emit_tool_callback(
        on_tool_call: Callable[[str, dict, Any], None] | None,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        """Safely notify the UI about a tool execution."""

        if not on_tool_call:
            return
        try:
            on_tool_call(tool_name, args, result)
        except Exception as e:
            log.debug("UI callback error in _emit_tool_callback: %s", e)  # must not break the loop

    def _execute_tool_with_policy(
        self,
        tool_name: str,
        args: dict[str, Any],
        state: _ToolExecutionState,
        on_tool_call: Callable[[str, dict, Any], None] | None,
    ) -> dict[str, Any]:
        """Execute one tool call with framework-managed snapshot tracking."""

        policy = get_tool_policy(tool_name)
        path = self._get_required_path(args, tool_name, policy.path_arg)

        if policy.auto_snapshot:
            if not path:
                return {
                    "success": False,
                    "error": (
                        f"Framework policy for {tool_name} requires a non-empty "
                        f"{policy.path_arg!r} argument."
                    ),
                }
            if path not in state.snapshotted_paths:
                # Save the document in KiCad first to sync in-memory changes
                # (e.g. from IPC operations) to disk before taking a snapshot.
                save_args = {"file_path": path}
                save_result = call_mcp_tool(self._mcp_base_url, "save_document", save_args)
                self._emit_tool_callback(on_tool_call, "save_document", save_args, save_result)
                if not self._tool_result_succeeded(save_result):
                    log.warning(
                        "save_document failed before %s: %s",
                        tool_name,
                        save_result.get("error", "unknown error"),
                    )

                snapshot_args = {"file_path": path}
                snapshot_result = call_mcp_tool(
                    self._mcp_base_url, "save_file_version", snapshot_args
                )
                self._emit_tool_callback(
                    on_tool_call, "save_file_version", snapshot_args, snapshot_result
                )
                if not self._tool_result_succeeded(snapshot_result):
                    error = snapshot_result.get("error", "unknown error")
                    return {
                        "success": False,
                        "error": f"Failed to save file version before {tool_name}: {error}",
                    }
                state.snapshotted_paths.add(path)

        result = call_mcp_tool(self._mcp_base_url, tool_name, args)

        # Run plugin-side post-process hook (e.g. DRC marker reading via pcbnew)
        if policy.post_process is not None:
            result = policy.post_process(result)

        self._emit_tool_callback(on_tool_call, tool_name, args, result)

        if not self._tool_result_succeeded(result):
            return result

        if policy.track_snapshot and path:
            state.snapshotted_paths.add(path)
        if policy.mark_dirty and path:
            state.dirty_paths.add(path)
        if policy.clear_dirty_paths_arg:
            reload_paths = args.get(policy.clear_dirty_paths_arg, [])
            if isinstance(reload_paths, list):
                for reload_path in reload_paths:
                    if isinstance(reload_path, str):
                        state.dirty_paths.discard(reload_path)
        return result

    def _auto_reload_modified_files(
        self,
        state: _ToolExecutionState,
        on_tool_call: Callable[[str, dict, Any], None] | None,
    ) -> str | None:
        """Reload dirty KiCad files after a successful turn, if needed."""

        if not state.dirty_paths:
            return None

        reload_args = {"paths": sorted(state.dirty_paths)}
        reload_result = call_mcp_tool(self._mcp_base_url, "reload_kicad", reload_args)
        self._emit_tool_callback(on_tool_call, "reload_kicad", reload_args, reload_result)

        if self._tool_result_succeeded(reload_result):
            state.dirty_paths.clear()
            return None
        return self._format_reload_failure(reload_result)

    def run(
        self,
        user_message: str,
        context_block: str,
        on_tool_call: Callable[[str, dict, Any], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Run one engineer request through the agentic loop.

        Args:
            user_message:  The engineer's chat message.
            context_block: Rendered KiCad context from context_bridge.
            on_tool_call:  Optional callback(tool_name, arguments, result) fired
                           after each tool execution — use this to update the UI.
            on_text_delta: Optional callback(chunk) fired for each text chunk
                           when streaming is active.
            images:        Optional list of dicts {"media_type": "image/png",
                           "data": "<base64>"} attached to this user message.

        Returns:
            The final assistant text message for display.
        """
        system = build_system_prompt(context_block)
        content = self._build_user_content(user_message, images)
        self._history.append({"role": "user", "content": content})
        self._maybe_compact(system)

        tools = self._fetch_tool_definitions()
        missing_policies = get_missing_tool_policies(
            [tool["function"]["name"] for tool in tools if tool.get("function", {}).get("name")]
        )
        if missing_policies:
            return "[Framework error] Tool policy registry is missing entries for: " + ", ".join(
                missing_policies
            )

        state = _ToolExecutionState()

        for _ in range(20):  # max 20 iterations (guard against infinite loops)
            response = self._call_llm(system, tools, on_text_delta=on_text_delta)

            if response.get("error"):
                return f"[LLM error] {response['error']}"

            message = response.get("message", {})
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # Final text response
                text = message.get("content") or ""
                reload_warning = self._auto_reload_modified_files(state, on_tool_call)
                if reload_warning:
                    text = (
                        f"{text}\n\n[Framework warning] {reload_warning}"
                        if text
                        else f"[Framework warning] {reload_warning}"
                    )
                self._history.append({"role": "assistant", "content": text})
                return text

            # Execute tool calls
            # Save message, preserving thinking content for DeepSeek models
            msg_to_save = {"role": "assistant"}
            if message.get("content"):
                msg_to_save["content"] = message["content"]
            if message.get("reasoning_content"):
                msg_to_save["reasoning_content"] = message["reasoning_content"]
            if message.get("tool_calls"):
                msg_to_save["tool_calls"] = message["tool_calls"]
            self._history.append(msg_to_save)
            tool_results = []
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                result = self._execute_tool_with_policy(name, args, state, on_tool_call)

                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result),
                    }
                )

            self._history.extend(tool_results)

        return "[Error] Maximum tool-call iterations reached. Please try a simpler request."

    @staticmethod
    def _build_user_content(
        user_message: str, images: list[dict[str, Any]] | None
    ) -> str | list[dict[str, Any]]:
        """Build the user message content (plain string or multimodal array).

        The canonical format stored in history is OpenAI-style content blocks;
        provider-specific formats (Anthropic / Ollama) are converted at request
        time by _anthropic_content / _ollama_messages.
        """
        if not images:
            return user_message
        blocks: list[dict[str, Any]] = [{"type": "text", "text": user_message}]
        for img in images:
            media = img.get("media_type", "image/png")
            data = img.get("data", "")
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{data}"},
                }
            )
        return blocks

    def _fetch_tool_definitions(self) -> list[dict[str, Any]]:
        """Fetch available tools from the MCP server and convert to LLM format."""
        import urllib.error
        import urllib.request

        url = f"{self._mcp_base_url}/mcp"
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- MCP client, localhost only
                body = _parse_mcp_response_text(resp.read().decode())
        except Exception as e:
            log.warning(f"Could not fetch tool list: {e}")
            return []

        tools_raw = body.get("result", {}).get("tools", [])
        # Convert MCP tool schema to OpenAI function-call format
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools_raw
        ]

    def _call_llm(self, system: str, tools: list[dict], on_text_delta=None) -> dict[str, Any]:
        """Dispatch to the configured LLM provider."""
        provider = self._settings.llm_provider
        tool_names = [t.get("function", {}).get("name", "?") for t in tools]
        total_payload = len(system) + len(json.dumps(self._history)) + len(json.dumps(tools))
        log.info(
            "LLM request — provider=%s model=%s tools=%d (%s) history=%d system=%dB total≈%dB",
            provider,
            self._settings.llm_model,
            len(tools),
            ", ".join(tool_names[:10]) + ("…" if len(tool_names) > 10 else ""),
            len(self._history),
            len(system),
            total_payload,
        )
        self._validate_history()  # guard against corrupted history before every API call

        max_retries = 5
        for attempt in range(max_retries):
            if provider == "ollama":
                if on_text_delta is not None:
                    response = self._stream_ollama(system, tools, on_text_delta)
                else:
                    response = self._call_ollama(system, tools)
            elif on_text_delta is not None:
                if provider == "anthropic":
                    response = self._stream_anthropic(system, tools, on_text_delta)
                else:
                    response = self._stream_openai(system, tools, on_text_delta)
            elif provider == "anthropic":
                response = self._call_anthropic(system, tools)
            else:
                response = self._call_openai(system, tools)

            error = response.get("error") if isinstance(response, dict) else None
            if error and _is_retryable_llm_error(error):
                if attempt < max_retries - 1:
                    delay = 1.0 + random.uniform(-0.5, 0.5)  # nosec B311 -- retry jitter, not cryptographic
                    delay = max(0.1, delay)
                    log.warning(
                        "LLM retry %d/%d — waiting %.1fs: %s",
                        attempt + 1,
                        max_retries - 1,
                        delay,
                        error[:120],
                    )
                    time.sleep(delay)
                    continue
            return response

        return response  # unreachable; placate static analysis

    def _build_anthropic_messages(self) -> list[dict]:
        """Convert self._history from OpenAI format to Anthropic message format.

        Consecutive role="tool" messages are batched into a single role="user"
        message with multiple tool_result content blocks — Anthropic requires
        strictly alternating user/assistant roles.
        """
        messages = []
        i = 0
        while i < len(self._history):
            m = self._history[i]
            role = m.get("role")
            if role == "tool":
                # Batch all consecutive tool results into one user message
                tool_results = []
                while i < len(self._history) and self._history[i].get("role") == "tool":
                    t = self._history[i]
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": t.get("tool_call_id"),
                            "content": t.get("content"),
                        }
                    )
                    i += 1
                messages.append({"role": "user", "content": tool_results})
                continue
            elif role == "assistant" and m.get("tool_calls"):
                # Include any assistant text alongside tool_use blocks
                content_blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                content_blocks.extend(
                    [
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"].get("arguments", "{}")),
                        }
                        for tc in m["tool_calls"]
                    ]
                )
                messages.append({"role": "assistant", "content": content_blocks})
            else:
                messages.append(
                    {"role": role, "content": self._anthropic_content(m.get("content", ""))}
                )
            i += 1
        return messages

    @staticmethod
    def _anthropic_content(content) -> str | list[dict[str, Any]]:
        """Convert OpenAI-style content blocks to Anthropic message format."""
        if isinstance(content, str):
            return content
        blocks: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media_type = header[5:].split(";")[0]
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        }
                    )
        return blocks

    def _stream_openai(self, system: str, tools: list[dict], on_text_delta) -> dict[str, Any]:
        """Call OpenAI-compatible API with streaming enabled.

        Uses in-process urllib for true SSE streaming when SSL is available.
        Falls back to non-streaming via _call_openai (subprocess) otherwise.
        """
        global _current_reasoning
        _current_reasoning = []
        global _in_process_ssl
        import urllib.error
        import urllib.request

        base = (self._settings.llm_base_url or "https://api.openai.com").rstrip("/")
        if "/chat/completions" in base:
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        messages = [{"role": "system", "content": system}] + self._history
        payload = json.dumps(
            {
                "model": self._settings.llm_model,
                "messages": messages,
                "tools": tools or None,
                "stream": True,
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.llm_api_key}",
        }

        if _in_process_ssl is not False:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 -- MCP client, localhost only
                    _in_process_ssl = True
                    text_parts = []
                    tool_calls_by_index: dict[int, dict] = {}
                    finish_reason = "stop"

                    while True:
                        raw = resp.readline()
                        if raw == b"":
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choice = chunk.get("choices", [{}])[0]
                        fr = choice.get("finish_reason")
                        if fr is not None:
                            finish_reason = fr

                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            try:
                                on_text_delta(content)
                            except Exception as e:
                                log.debug("Text delta callback error in streaming: %s", e)

                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            _current_reasoning.append(reasoning)

                        for tc_delta in delta.get("tool_calls") or []:
                            idx = tc_delta["index"]
                            if idx not in tool_calls_by_index:
                                tool_calls_by_index[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = tool_calls_by_index[idx]
                            if tc_delta.get("id"):
                                tc["id"] += tc_delta["id"]
                            fn = tc_delta.get("function", {})
                            if fn.get("name"):
                                tc["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                tc["function"]["arguments"] += fn["arguments"]

                    tool_calls = [tool_calls_by_index[k] for k in sorted(tool_calls_by_index)]
                    message: dict[str, Any] = {"content": "".join(text_parts)}
                    if tool_calls:
                        message["tool_calls"] = tool_calls
                    if _current_reasoning:
                        message["reasoning_content"] = "".join(_current_reasoning)
                    return {"finish_reason": finish_reason, "message": message}

            except urllib.error.URLError as e:
                if _NO_HTTPS_MARKER not in str(e.reason):
                    return {"error": f"HTTPS request failed: {e}"}
                _in_process_ssl = False
                # Fall through to non-streaming fallback below.
            except Exception as e:
                return {"error": f"Streaming request failed: {e}"}

        # In-process SSL unavailable: fall back to non-streaming via subprocess.
        result = self._call_openai(system, tools)
        content = result.get("message", {}).get("content", "")
        if content:
            try:
                on_text_delta(content)
            except Exception as e:
                log.debug("Non-streaming callback error: %s", e)
        return result

    def _ollama_messages(self) -> list[dict[str, Any]]:
        """Convert self._history to Ollama /api/chat message format.

        Multimodal history (OpenAI-style image_url blocks) is flattened: text
        becomes a plain string and base64 images are attached via the
        per-message ``images`` field (supported by all Ollama versions).
        """
        out: list[dict[str, Any]] = []
        for msg in self._history:
            content = msg.get("content", "")
            images: list[str] = []
            if isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "image_url":
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            _, _, data = url.partition(",")
                        else:
                            data = url
                        images.append(data)
                content = "\n".join(p for p in text_parts if p)
            converted = {**msg, "content": content}
            if images:
                converted["images"] = images
            out.append(converted)
        return out

    def _stream_ollama(self, system: str, tools: list[dict], on_text_delta) -> dict[str, Any]:
        """Call Ollama native API with streaming enabled.

        Uses the Ollama /api/chat endpoint with ``stream: true``.
        Ollama returns newline-delimited JSON (NDJSON), not SSE.
        Since Ollama runs on localhost, no SSL fallback is needed.
        """
        base = (self._settings.llm_base_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/chat"

        messages = [{"role": "system", "content": system}] + self._ollama_messages()
        payload_dict: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
            "stream": True,
            "options": {"num_ctx": self._context_tokens},
        }
        if self._max_tokens > 0:
            payload_dict["options"]["num_predict"] = self._max_tokens
        payload = json.dumps(payload_dict).encode()
        headers = {"Content-Type": "application/json"}

        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 -- localhost only
                text_parts = []
                tool_calls_by_index: dict[int, dict] = {}
                finish_reason = "stop"

                while True:
                    raw = resp.readline()
                    if raw == b"":
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("done"):
                        finish_reason = chunk.get("done_reason", "stop")
                        break

                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    if content:
                        text_parts.append(content)
                        try:
                            on_text_delta(content)
                        except Exception as e:
                            log.debug("Text delta callback error in Ollama streaming: %s", e)

                    for tc_delta in msg.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        tc = tool_calls_by_index[idx]
                        if tc_delta.get("id"):
                            tc["id"] += tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tc["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tc["function"]["arguments"] += fn["arguments"]

                tool_calls = [tool_calls_by_index[k] for k in sorted(tool_calls_by_index)]
                message: dict[str, Any] = {"content": "".join(text_parts)}
                if tool_calls:
                    message["tool_calls"] = tool_calls
                return {"finish_reason": finish_reason, "message": message}

        except urllib.error.URLError as e:
            return {"error": f"Ollama request failed: {e}"}
        except Exception as e:
            return {"error": f"Ollama streaming request failed: {e}"}

    def _stream_anthropic(self, system: str, tools: list[dict], on_text_delta) -> dict[str, Any]:
        """Call Anthropic API with streaming enabled.

        Uses in-process urllib for true SSE streaming when SSL is available.
        Falls back to non-streaming via _call_anthropic (subprocess) otherwise.
        """
        global _in_process_ssl
        import urllib.error
        import urllib.request

        base = (self._settings.llm_base_url or "https://api.anthropic.com").rstrip("/")
        # Accept full endpoint URL or bare hostname — only append the default
        # path when the user hasn't already specified one.
        if "/v1/messages" in base:
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/messages"
        else:
            url = f"{base}/v1/messages"
        anthropic_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {}),
            }
            for t in tools
        ]
        messages = self._build_anthropic_messages()
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if self._max_tokens > 0:
            payload["max_tokens"] = self._max_tokens
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        encoded = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._settings.llm_api_key,
            "anthropic-version": "2023-06-01",
        }

        if _in_process_ssl is not False:
            req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 -- MCP client, localhost only
                    _in_process_ssl = True
                    text_blocks: dict[int, str] = {}
                    tool_blocks: dict[int, dict] = {}
                    stop_reason = "end_turn"
                    current_event = ""

                    while True:
                        raw = resp.readline()
                        if raw == b"":
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line[6:].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        try:
                            event_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        etype = event_data.get("type", current_event)

                        if etype == "content_block_start":
                            idx = event_data.get("index", 0)
                            block = event_data.get("content_block", {})
                            btype = block.get("type")
                            if btype == "text":
                                text_blocks[idx] = block.get("text", "")
                            elif btype == "tool_use":
                                tool_blocks[idx] = {
                                    "id": block["id"],
                                    "name": block["name"],
                                    "input_json": "",
                                }

                        elif etype == "content_block_delta":
                            idx = event_data.get("index", 0)
                            delta = event_data.get("delta", {})
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                chunk = delta.get("text", "")
                                text_blocks[idx] = text_blocks.get(idx, "") + chunk
                                if chunk:
                                    try:
                                        on_text_delta(chunk)
                                    except Exception as e:
                                        log.debug("Anthropic text delta callback error: %s", e)
                            elif dtype == "input_json_delta":
                                partial = delta.get("partial_json", "")
                                if idx in tool_blocks:
                                    tool_blocks[idx]["input_json"] += partial

                        elif etype == "message_delta":
                            delta = event_data.get("delta", {})
                            sr = delta.get("stop_reason")
                            if sr:
                                stop_reason = sr

                        elif etype == "error":
                            err = event_data.get("error", {})
                            return {
                                "error": f"Anthropic stream error: {err.get('message', str(err))}"
                            }

                    full_text = "\n".join(text_blocks[k] for k in sorted(text_blocks))
                    tool_calls = []
                    for k in sorted(tool_blocks):
                        tb = tool_blocks[k]
                        try:
                            inp = json.loads(tb["input_json"]) if tb["input_json"] else {}
                        except json.JSONDecodeError:
                            inp = {}
                        tool_calls.append(
                            {
                                "id": tb["id"],
                                "type": "function",
                                "function": {
                                    "name": tb["name"],
                                    "arguments": json.dumps(inp),
                                },
                            }
                        )
                    message_out: dict[str, Any] = {"content": full_text}
                    if tool_calls:
                        message_out["tool_calls"] = tool_calls
                    finish = "tool_calls" if tool_calls else "stop"
                    if stop_reason == "max_tokens":
                        finish = "stop"
                    return {"finish_reason": finish, "message": message_out}

            except urllib.error.URLError as e:
                if _NO_HTTPS_MARKER not in str(e.reason):
                    return {"error": f"HTTPS request failed: {e}"}
                _in_process_ssl = False
                # Fall through to non-streaming fallback below.
            except Exception as e:
                return {"error": f"Streaming request failed: {e}"}

        # In-process SSL unavailable: fall back to non-streaming via subprocess.
        result = self._call_anthropic(system, tools)
        content = result.get("message", {}).get("content", "")
        if content:
            try:
                on_text_delta(content)
            except Exception as e:
                log.debug("Non-streaming callback error: %s", e)
        return result

    def _call_openai(self, system: str, tools: list[dict]) -> dict[str, Any]:
        base = (self._settings.llm_base_url or "https://api.openai.com").rstrip("/")
        # Accept either a server root (e.g. "https://api.openai.com") or a
        # full endpoint URL (e.g. ".../v1/chat/completions").  Only append the
        # default path when the user hasn't already specified one.
        if "/chat/completions" in base:
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        messages = [{"role": "system", "content": system}] + self._history
        payload_dict: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
        }
        if self._max_tokens > 0:
            payload_dict["max_tokens"] = self._max_tokens
        payload = json.dumps(payload_dict).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.llm_api_key}",
        }
        try:
            status, text = _https_post_json(url, headers, payload, timeout=60)
        except RuntimeError as e:
            return {"error": str(e)}

        if status >= 400:
            return {"error": f"HTTP {status}: {text[:200]}"}
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON from OpenAI: {e}"}

        if not isinstance(body, dict):
            return {"error": f"Unexpected response from OpenAI: {text[:200]}"}

        choice = body.get("choices", [{}])[0]
        return {
            "finish_reason": choice.get("finish_reason", "stop"),
            "message": choice.get("message", {}),
        }

    def _call_ollama(self, system: str, tools: list[dict]) -> dict[str, Any]:
        """Call Ollama native API (non-streaming).

        Uses the Ollama /api/chat endpoint.  No API key is required.
        Tool format is the same as OpenAI-compatible (Ollama supports it natively).
        """
        base = (self._settings.llm_base_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/chat"

        messages = [{"role": "system", "content": system}] + self._ollama_messages()
        payload_dict: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
            "stream": False,
            "options": {"num_ctx": self._context_tokens},
        }
        if self._max_tokens > 0:
            payload_dict["options"]["num_predict"] = self._max_tokens
        payload = json.dumps(payload_dict).encode()
        headers = {"Content-Type": "application/json"}

        try:
            status, text = _https_post_json(url, headers, payload, timeout=60)
        except RuntimeError as e:
            return {"error": str(e)}

        if status >= 400:
            return {"error": f"HTTP {status}: {text[:200]}"}
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON from Ollama: {e}"}

        if not isinstance(body, dict):
            return {"error": f"Unexpected response from Ollama: {text[:200]}"}

        msg = body.get("message", {})
        finish_reason = "stop"
        if body.get("done_reason") == "tool_calls":
            finish_reason = "tool_calls"
        return {
            "finish_reason": finish_reason,
            "message": msg,
        }

    def _call_anthropic(self, system: str, tools: list[dict]) -> dict[str, Any]:
        base = (self._settings.llm_base_url or "https://api.anthropic.com").rstrip("/")
        # Accept full endpoint URL or bare hostname — only append the default
        # path when the user hasn't already specified one.
        if "/v1/messages" in base:
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/messages"
        else:
            url = f"{base}/v1/messages"
        # Convert OpenAI tool format to Anthropic format
        anthropic_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {}),
            }
            for t in tools
        ]
        messages = self._build_anthropic_messages()

        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "system": system,
            "messages": messages,
        }
        if self._max_tokens > 0:
            payload["max_tokens"] = self._max_tokens
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        encoded = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._settings.llm_api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            status, text = _https_post_json(url, headers, encoded, timeout=60)
        except RuntimeError as e:
            return {"error": str(e)}

        if status >= 400:
            return {"error": f"HTTP {status}: {text[:200]}"}
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON from Anthropic: {e}"}

        if not isinstance(body, dict):
            return {"error": f"Unexpected response from Anthropic: {text[:200]}"}

        content_blocks_resp = body.get("content", [])
        text_blocks = [b["text"] for b in content_blocks_resp if b.get("type") == "text"]
        tool_use_blocks = [b for b in content_blocks_resp if b.get("type") == "tool_use"]
        message: dict[str, Any] = {"content": "\n".join(text_blocks)}
        if tool_use_blocks:
            message["tool_calls"] = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                }
                for b in tool_use_blocks
            ]
        return {
            "finish_reason": "tool_calls" if tool_use_blocks else "stop",
            "message": message,
        }
