"""
LLM client: drives the agentic tool-call loop between the engineer's message
and the MCP server.

Supports OpenAI-compatible and Anthropic APIs. The client is intentionally
thin — it delegates all KiCad knowledge to the MCP tool surface.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Callable, Generator, Optional

log = logging.getLogger(__name__)


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


def _resolve_plugin_python() -> Optional[str]:
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
    import urllib.request
    import urllib.error

    if _in_process_ssl is not False:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            "Python was found. Run 'kicad_plugin/setup_plugin.sh <repo>' to create one."
        )

    # Build a clean env using an explicit allowlist (mirrors ServerManager).
    # KiCad's AppImage sets PYTHONHOME/PYTHONPATH/LD_LIBRARY_PATH for its own
    # embedded interpreter; inheriting them would break the venv Python.
    _ENV_ALLOWLIST = (
        "PATH", "HOME", "USER", "LOGNAME",
        "LANG", "LC_ALL", "LC_CTYPE",
        "TMPDIR", "TEMP", "TMP",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    )
    clean_env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}

    try:
        proc = subprocess.run(
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

MAX_HISTORY_MESSAGES = 100  # default sliding-window cap for conversation history

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PROMPT_HEADER = """\
You are an expert KiCad assistant embedded in the KiCad EDA tool.
You help engineers edit schematics and PCB layouts by calling the available MCP tools.\
"""

_PROMPT_SCHEMATIC = """\

# Schematic coordinate system
- All coordinates are in **millimetres** with **+X right, +Y DOWN** (KiCad
  schematic screen convention).
- Library symbols (.kicad_sym) internally use Y-UP, but every tool that
  takes or returns placed-symbol coordinates uses Y-DOWN. You only ever
  reason in Y-DOWN.
- The schematic grid is **1.27 mm (50 mil)**. add_symbol_to_schematic and
  move_component snap automatically; rotation is restricted to 0/90/180/270.
- Default sheet is **A4 (297 x 210 mm)**. Always call
  get_schematic_sheet_info to confirm the actual paper size and to learn
  the recommended drawing area (it accounts for the title block).

# Geometry you get for free
- get_symbol and get_symbol_pins return body_bbox + per-unit bboxes in
  library coordinates so you can size new placements before inserting.
- extract_schematic_netlist returns body_bbox per placed component in
  schematic (Y-down) world coordinates. Use it as your occupancy map.
- add_symbol_to_schematic, place_symbol_relative and move_component all
  return the resulting body_bbox so you usually do NOT need to re-extract
  the netlist after a successful placement.

# Recommended placement workflow
1. Call extract_schematic_netlist to learn what already exists and where.
2. To add a new symbol:
   a. Look it up with search_symbols / get_symbol (note its body_bbox).
   b. PREFER place_symbol_relative when you can describe the position
      relative to an existing reference (e.g. "right of U1, gap 2.54").
   c. Otherwise call find_free_area(for_library=..., for_symbol=...,
      prefer_near=...) and pass the returned candidate's **placement.x /
      placement.y** straight to add_symbol_to_schematic. (The candidate's
      ``origin`` is the bbox top-left, NOT the symbol anchor — always
      prefer ``placement`` when present.)
   d. Only fall back to absolute add_symbol_to_schematic(x, y, ...) if the
      helpers above cannot satisfy the request.
3. After moves/inserts, prefer using the returned body_bbox; only call
   extract_schematic_netlist again if you need to refresh net info.
4. Wire pins with **connect_pins_with_wire(from_ref, from_pin, to_ref,
   to_pin)** — it resolves world coordinates from the placement automatically,
   routes orthogonally, and inserts junctions when a pin already has a wire.
   Fall back to **add_wire_to_schematic(start_x, start_y, end_x, end_y)** only
   when you need a wire between bare points; use **add_junction_to_schematic**
   for explicit T/X joints.

# Wiring strategy
- The required pin on each component is dictated by the **circuit's
  electrical intent** — that comes first, ALWAYS. Pin choice is
  flexible only when both candidate pins are electrically interchangeable
  ("isotropic"): e.g. the two leads of a non-polarised resistor or
  ceramic capacitor, the two ends of an inductor, or two pins on the
  same internal net. It is NEVER flexible for polarised parts (diodes
  anode/cathode, electrolytic/tantalum capacitor +/-, LEDs, BJT/MOSFET
  terminals), ICs, connectors, or any pin whose name carries meaning
  (VCC, GND, EN, CLK, D+, etc.). Use ``get_symbol_pins`` /
  ``components[ref].pins[*].num`` together with the symbol's datasheet
  semantics to pick the correct pin first; only then optimise geometry.
- **When (and only when) the choice is between electrically equivalent
  pins**, pick the pair whose schematic coordinates are closest
  (minimum Manhattan distance). Read each candidate pin's world
  ``x``/``y`` from extract_schematic_netlist
  (``components[ref].pins[*]`` has ``num``, ``x``, ``y``, ``direction``).
  Shorter wires mean fewer bends and fewer crossings.
- For pins on the same net (e.g. all GND, all VCC), wire each new pin
  to the *closest already-wired pin on that net* rather than always
  going back to the same anchor — this keeps the net visually local.
- Prefer connect_pins_with_wire over manual coordinate routing whenever
  both endpoints are pins; it handles rotation and junction insertion for
  you.
- If the electrically-correct pin pair would produce a long or cluttered
  wire, consider **rotating or moving one of the components** instead of
  picking a different (wrong) pin.

# Spacing & layout rules
- Keep at least one grid step (1.27 mm) of clearance between symbol body
  bboxes; 2.54 mm or more is preferred so Reference/Value labels do not
  collide with neighbours.
- Align rows of similar components on the same Y; align columns on the
  same X. Use multiples of 1.27 mm for spacing so wires stay orthogonal.
- Stay inside the recommended_area returned by get_schematic_sheet_info;
  never place inside title_block_default.

# Hard rules (schematic)
- Always call extract_schematic_netlist (or get_schematic_sheet_info) for
  state before making spatial changes. Never invent coordinates.
- Use the active_schematic path from the context block below as the
  schematic_path argument for every editing tool.
- Every mutation tool automatically writes a .kicad_sch.bak backup, saves
  to disk, and attempts to reload the Schematic Editor via the KiCad IPC
  API. On KiCad 11+ this reloads automatically; on KiCad 10 or if the IPC
  API is unavailable, the engineer must use **File → Revert** in the
  Schematic Editor to load the updated file.
- Report errors clearly. Do not silently retry failed tool calls.
- If find_free_area returns no candidates and no relative placement is
  appropriate, ASK the engineer instead of guessing a position.\
"""

_PROMPT_PCB = """\

# PCB coordinate system
- All PCB coordinates are **millimetres**, **+X right, +Y DOWN** (same screen
  convention as schematics).
- PCB rotation is **clockwise-positive** (opposite to the CCW / Y-up
  convention used by .kicad_sym library data).  0° is unrotated; 90° tilts
  the footprint 90° clockwise on screen.
- There is no auto-snap for PCB tools.  Pass coordinates already aligned to
  your board grid.  Typical grids: **0.1 mm or 0.05 mm** for SMD work,
  **1.27 mm (50 mil)** for through-hole.
- PCB layers of interest: ``F.Cu`` / ``B.Cu`` (copper), ``F.SilkS`` /
  ``B.SilkS`` (silkscreen), ``F.Courtyard`` / ``B.Courtyard`` (keep-out),
  ``Edge.Cuts`` (board outline).

# PCB query workflow
1. Call **get_board_info** to learn layer stack, copper layer count, footprint
   count, net count, and board generator.
2. Call **list_footprints** to get every footprint's reference, value, x/y
   (world mm), rotation (CW+), and layer.
3. Call **get_footprint** for detailed info on a specific footprint: pad
   numbers/types/nets, all properties, and local pad coordinates.
   Note: pad coordinates from get_footprint are *local* (footprint-relative).
   Use **get_ratsnest** when you need world-coordinate pad positions.
4. Call **list_nets** to enumerate nets with their pad counts.
5. Call **get_ratsnest** to identify unconnected pad pairs.  An empty result
   means the board is fully routed.  Pad x/y in the ratsnest response are
   already in world coordinates (rotation applied).

# PCB placement workflow
- Before placing, call **get_board_info** + **list_footprints** to understand
  the current layout.
- Use **get_footprint_bbox** to get a footprint's courtyard bounding box in
  world coordinates.  Use this to check for overlaps before positioning.
- Use **get_board_bounding_box** to get the union bbox of all footprint
  courtyards — useful for sizing the board outline around all components.
- Move or rotate a single footprint: **set_footprint_position(pcb_path,
  reference, x, y, rotation)**.  Any argument may be ``null`` to leave it
  unchanged; at least one must be provided.
- Flip a footprint between F.Cu and B.Cu: **flip_footprint(pcb_path,
  reference)**.  All child layer items are updated automatically.
- Update a footprint property (Reference, Value, Datasheet, or custom field):
  **set_footprint_property(pcb_path, reference, property_name, value)**.

# PCB group operations
- **align_footprints(pcb_path, references, axis, coordinate)** — align all
  listed footprints to the same X or Y.  ``coordinate=null`` uses the mean.
- **distribute_footprints(pcb_path, references, axis)** — evenly space ≥3
  footprints along X or Y; outermost positions are fixed.
- **move_footprints_by_delta(pcb_path, references, dx, dy)** — shift a group
  by the same offset without changing their relative positions.

# Board outline workflow
- Query current Edge.Cuts geometry: **get_board_outline**.
- Replace the entire outline with a rectangle: **set_board_outline_rect(
  pcb_path, x, y, width, height, line_width, corner_radius)**.
  ``corner_radius=0`` emits a single gr_rect; positive value draws four
  lines + four 90° arcs.  Edge.Cuts line width is typically 0.05 mm.
- Add individual segments or arcs: **add_board_outline_segment** /
  **add_board_outline_arc**.  Wipe first with **clear_board_outline**.
- Arc angles: 0° is +X, angles increase clockwise.

# Footprint library workflow
- Build/refresh the footprint index (background):
  **sync_footprint_index(project_path, force)**.  Check progress with
  **get_footprint_sync_status**.
- List libraries: **list_footprint_libraries(project_path)**.
- Search footprints: **search_footprints(query, project_path, limit)**.
- Inspect pads/courtyard of a specific footprint:
  **get_footprint_details(library, name)**.

# Hard rules (PCB)
- Always call get_board_info + list_footprints before making spatial changes.
  Never invent footprint coordinates.
- Use the active_pcb path from the context block below as the pcb_path
  argument for every PCB editing tool.
- Every mutation tool automatically writes a .kicad_pcb.bak backup and
  **reloads the PCB editor in KiCad** — no manual reload is needed.
- Report errors clearly. Do not silently retry failed tool calls.
- Do not overlap footprint courtyards.  Verify clearances with
  get_footprint_bbox / get_board_bounding_box before committing a move.\
"""


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
    last_data: Optional[str] = None
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
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()

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
        with urllib.request.urlopen(req, timeout=30) as resp:
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

    def __init__(self, settings, mcp_base_url: str, max_history: int = MAX_HISTORY_MESSAGES) -> None:
        self._settings = settings
        self._mcp_base_url = mcp_base_url
        self._max_history = max_history
        self._history: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history."""
        self._history = []

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

    def run(
        self,
        user_message: str,
        context_block: str,
        on_tool_call: Optional[Callable[[str, dict, Any], None]] = None,
        on_text_delta: Optional[Callable[[str], None]] = None,
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

        Returns:
            The final assistant text message for display.
        """
        system = build_system_prompt(context_block)
        self._history.append({"role": "user", "content": user_message})
        self._trim_history()

        tools = self._fetch_tool_definitions()

        for _ in range(20):  # max 20 iterations (guard against infinite loops)
            response = self._call_llm(system, tools, on_text_delta=on_text_delta)

            if response.get("error"):
                return f"[LLM error] {response['error']}"

            finish_reason = response.get("finish_reason", "stop")
            message = response.get("message", {})
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # Final text response
                text = message.get("content") or ""
                self._history.append({"role": "assistant", "content": text})
                return text

            # Execute tool calls
            self._history.append({"role": "assistant", **message})
            tool_results = []
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                result = call_mcp_tool(self._mcp_base_url, name, args)

                if on_tool_call:
                    try:
                        on_tool_call(name, args, result)
                    except Exception:
                        pass  # UI callback errors must not break the loop

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })

            self._history.extend(tool_results)

        return "[Error] Maximum tool-call iterations reached. Please try a simpler request."

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _fetch_tool_definitions(self) -> list[dict[str, Any]]:
        """Fetch available tools from the MCP server and convert to LLM format."""
        import urllib.request, urllib.error
        url = f"{self._mcp_base_url}/mcp"
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
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
        if on_text_delta is not None:
            if provider == "anthropic":
                return self._stream_anthropic(system, tools, on_text_delta)
            return self._stream_openai(system, tools, on_text_delta)
        if provider == "anthropic":
            return self._call_anthropic(system, tools)
        return self._call_openai(system, tools)

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
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": t.get("tool_call_id"),
                        "content": t.get("content"),
                    })
                    i += 1
                messages.append({"role": "user", "content": tool_results})
                continue
            elif role == "assistant" and m.get("tool_calls"):
                # Include any assistant text alongside tool_use blocks
                content_blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                content_blocks.extend([
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"].get("arguments", "{}")),
                    }
                    for tc in m["tool_calls"]
                ])
                messages.append({"role": "assistant", "content": content_blocks})
            else:
                messages.append({"role": role, "content": m.get("content", "")})
            i += 1
        return messages

    def _stream_openai(self, system: str, tools: list[dict], on_text_delta) -> dict[str, Any]:
        """Call OpenAI-compatible API with streaming enabled.

        Uses in-process urllib for true SSE streaming when SSL is available.
        Falls back to non-streaming via _call_openai (subprocess) otherwise.
        """
        global _in_process_ssl
        import urllib.request
        import urllib.error

        base = (self._settings.llm_base_url or "https://api.openai.com").rstrip("/")
        if "/chat/completions" in base:
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        messages = [{"role": "system", "content": system}] + self._history
        payload = json.dumps({
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
            "stream": True,
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.llm_api_key}",
        }

        if _in_process_ssl is not False:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
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
                            except Exception:
                                pass

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
            except Exception:
                pass
        return result

    def _stream_anthropic(self, system: str, tools: list[dict], on_text_delta) -> dict[str, Any]:
        """Call Anthropic API with streaming enabled.

        Uses in-process urllib for true SSE streaming when SSL is available.
        Falls back to non-streaming via _call_anthropic (subprocess) otherwise.
        """
        global _in_process_ssl
        import urllib.request
        import urllib.error

        url = "https://api.anthropic.com/v1/messages"
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
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
            "stream": True,
        }
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
                with urllib.request.urlopen(req, timeout=300) as resp:
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
                                    except Exception:
                                        pass
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
                            return {"error": f"Anthropic stream error: {err.get('message', str(err))}"}

                    full_text = "\n".join(text_blocks[k] for k in sorted(text_blocks))
                    tool_calls = []
                    for k in sorted(tool_blocks):
                        tb = tool_blocks[k]
                        try:
                            inp = json.loads(tb["input_json"]) if tb["input_json"] else {}
                        except json.JSONDecodeError:
                            inp = {}
                        tool_calls.append({
                            "id": tb["id"],
                            "type": "function",
                            "function": {
                                "name": tb["name"],
                                "arguments": json.dumps(inp),
                            },
                        })
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
            except Exception:
                pass
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
        payload = json.dumps({
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
        }).encode()
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

    def _call_anthropic(self, system: str, tools: list[dict]) -> dict[str, Any]:
        url = "https://api.anthropic.com/v1/messages"
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

        payload = {"model": self._settings.llm_model, "max_tokens": 4096,
                   "system": system, "messages": messages}
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

        stop_reason = body.get("stop_reason", "end_turn")
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
