"""
LLM client: drives the agentic tool-call loop between the engineer's message
and the MCP server.

Supports OpenAI-compatible and Anthropic APIs. The client is intentionally
thin — it delegates all KiCad knowledge to the MCP tool surface.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Generator, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert KiCad schematic assistant embedded in the KiCad EDA tool.
You help engineers edit schematics by calling the available MCP tools.

Rules:
- Always call extract_schematic_netlist first to understand the current state before making changes.
- When adding a new component, call search_symbols to find the correct library and symbol name.
- Use the active_schematic path from the context block below as the schematic_path for all editing tools.
- After making changes, call extract_schematic_netlist again to confirm the result.
- Report errors clearly. Do not silently retry failed tool calls.
- If you are unsure about coordinates, ask the engineer rather than guessing.

{context_block}
"""


def build_system_prompt(context_block: str) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(context_block=context_block)


# ---------------------------------------------------------------------------
# MCP HTTP tool caller
# ---------------------------------------------------------------------------

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
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
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

    def __init__(self, settings, mcp_base_url: str) -> None:
        self._settings = settings
        self._mcp_base_url = mcp_base_url
        self._history: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history."""
        self._history = []

    def run(
        self,
        user_message: str,
        context_block: str,
        on_tool_call: Optional[Callable[[str, dict, Any], None]] = None,
    ) -> str:
        """
        Run one engineer request through the agentic loop.

        Args:
            user_message:  The engineer's chat message.
            context_block: Rendered KiCad context from context_bridge.
            on_tool_call:  Optional callback(tool_name, arguments, result) fired
                           after each tool execution — use this to update the UI.

        Returns:
            The final assistant text message for display.
        """
        system = build_system_prompt(context_block)
        self._history.append({"role": "user", "content": user_message})

        tools = self._fetch_tool_definitions()

        for _ in range(20):  # max 20 iterations (guard against infinite loops)
            response = self._call_llm(system, tools)

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
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
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

    def _call_llm(self, system: str, tools: list[dict]) -> dict[str, Any]:
        """Dispatch to the configured LLM provider."""
        provider = self._settings.llm_provider
        if provider == "anthropic":
            return self._call_anthropic(system, tools)
        return self._call_openai(system, tools)

    def _call_openai(self, system: str, tools: list[dict]) -> dict[str, Any]:
        import urllib.request, urllib.error
        base = self._settings.llm_base_url or "https://api.openai.com"
        url = f"{base.rstrip('/')}/v1/chat/completions"
        messages = [{"role": "system", "content": system}] + self._history
        payload = json.dumps({
            "model": self._settings.llm_model,
            "messages": messages,
            "tools": tools or None,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._settings.llm_api_key}",
            }, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": str(e)}

        choice = body.get("choices", [{}])[0]
        return {
            "finish_reason": choice.get("finish_reason", "stop"),
            "message": choice.get("message", {}),
        }

    def _call_anthropic(self, system: str, tools: list[dict]) -> dict[str, Any]:
        import urllib.request, urllib.error
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
        # Convert history (OpenAI format → Anthropic format).
        # Consecutive role="tool" messages must be batched into a single
        # role="user" message with multiple tool_result content blocks —
        # Anthropic requires strictly alternating user/assistant roles.
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

        payload = {"model": self._settings.llm_model, "max_tokens": 4096,
                   "system": system, "messages": messages}
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        encoded = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=encoded,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._settings.llm_api_key,
                "anthropic-version": "2023-06-01",
            }, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": str(e)}

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
