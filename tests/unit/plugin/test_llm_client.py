"""Tests for LLMClient history trimming."""
import json
import types
import pytest
from unittest.mock import MagicMock, patch

from kicad_plugin.llm_client import LLMClient, MAX_HISTORY_MESSAGES


def _make_client(max_history=10):
    settings = types.SimpleNamespace(
        llm_provider="openai",
        llm_api_key="sk-test",
        llm_model="gpt-4o",
        llm_base_url="",
    )
    return LLMClient(settings, mcp_base_url="http://127.0.0.1:9999", max_history=max_history)


def _user(content="hello"):
    return {"role": "user", "content": content}


def _assistant(content="ok", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool(tool_call_id="tc1", content="result"):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ---------------------------------------------------------------------------
# _trim_history unit tests
# ---------------------------------------------------------------------------

class TestTrimHistory:
    def test_no_trim_when_under_limit(self):
        client = _make_client(max_history=10)
        client._history = [_user(), _assistant()]
        client._trim_history()
        assert len(client._history) == 2

    def test_no_trim_when_exactly_at_limit(self):
        client = _make_client(max_history=2)
        client._history = [_user(), _assistant()]
        client._trim_history()
        assert len(client._history) == 2

    def test_drops_oldest_assistant_turn(self):
        client = _make_client(max_history=3)
        # 4 messages: user, asst, user, asst  — need to drop 1 turn
        client._history = [
            _user("q1"), _assistant("a1"),
            _user("q2"), _assistant("a2"),
        ]
        client._trim_history()
        assert len(client._history) <= 3
        # The oldest assistant turn (a1) should be gone
        contents = [m.get("content") for m in client._history]
        assert "a1" not in contents

    def test_drops_complete_assistant_tool_pair(self):
        client = _make_client(max_history=4)
        # 5 messages: user, asst+tool_call, tool_result, user, asst
        client._history = [
            _user("q1"),
            _assistant("thinking", tool_calls=[{"id": "tc1"}]),
            _tool("tc1", "tool result"),
            _user("q2"),
            _assistant("final"),
        ]
        client._trim_history()
        # Should have dropped the assistant+tool pair together (2 messages gone → 3 left)
        assert len(client._history) <= 4
        roles = [m["role"] for m in client._history]
        # No orphaned tool message without preceding assistant
        tool_indices = [i for i, m in enumerate(client._history) if m["role"] == "tool"]
        for ti in tool_indices:
            assert client._history[ti - 1]["role"] == "assistant"

    def test_trim_multiple_rounds(self):
        client = _make_client(max_history=2)
        # Build a long history with many turns
        client._history = []
        for i in range(10):
            client._history.append(_user(f"q{i}"))
            client._history.append(_assistant(f"a{i}"))
        client._trim_history()
        assert len(client._history) <= 2

    def test_fallback_drops_oldest_when_no_assistant(self):
        client = _make_client(max_history=1)
        client._history = [_user("q1"), _user("q2")]
        client._trim_history()
        assert len(client._history) <= 1

    def test_max_history_default_is_constant(self):
        settings = types.SimpleNamespace(
            llm_provider="openai", llm_api_key="", llm_model="gpt-4o", llm_base_url=""
        )
        client = LLMClient(settings, "http://localhost:9999")
        assert client._max_history == MAX_HISTORY_MESSAGES


# ---------------------------------------------------------------------------
# run() integration: trim called before LLM
# ---------------------------------------------------------------------------

class TestRunTrims:
    def test_trim_called_before_llm_call(self):
        client = _make_client(max_history=3)
        # Pre-fill history with 4 messages so trim will fire
        client._history = [
            _user("old1"), _assistant("r1"),
            _user("old2"), _assistant("r2"),
        ]

        # Mock out _call_llm to return a simple final response
        final_response = {
            "finish_reason": "stop",
            "message": {"content": "done", "tool_calls": []},
        }
        client._call_llm = MagicMock(return_value=final_response)
        client._fetch_tool_definitions = MagicMock(return_value=[])

        result = client.run("new question", context_block="")
        assert result == "done"
        # After run(), history should not have ballooned past max_history + 2
        # (the new user message + final assistant message are added during run)
        assert len(client._history) <= client._max_history + 2


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------

class TestStreaming:
    def _make_sse_response(self, lines):
        """Return a mock response object that yields SSE lines via readline()."""
        data = [line.encode("utf-8") + b"\n" for line in lines] + [b""]
        obj = MagicMock()
        obj.readline.side_effect = data
        obj.__enter__ = lambda s: s
        obj.__exit__ = MagicMock(return_value=False)
        return obj

    def test_stream_openai_text_only(self):
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_text_delta=chunks.append)

        assert chunks == ["Hello", " world"]
        assert result["message"]["content"] == "Hello world"
        assert result["finish_reason"] == "stop"

    def test_stream_openai_tool_calls(self):
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1","function":{"name":"add_wire","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"x\\":"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_text_delta=chunks.append)

        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tc1"
        assert tc[0]["function"]["name"] == "add_wire"
        assert tc[0]["function"]["arguments"] == '{"x":1}'
        assert result["finish_reason"] == "tool_calls"

    def test_stream_anthropic_text_only(self):
        client = _make_client()
        # Set provider to anthropic
        client._settings.llm_provider = "anthropic"
        sse_lines = [
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" AI"}}',
            "",
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
            "",
            "event: message_stop",
            'data: {"type":"message_stop"}',
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_anthropic("sys", [], on_text_delta=chunks.append)

        assert chunks == ["Hello", " AI"]
        assert result["message"]["content"] == "Hello AI"
        assert result["finish_reason"] == "stop"

    def test_stream_anthropic_tool_use(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        sse_lines = [
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu1","name":"get_netlist","input":{}}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"sch.kicad_sch\\"}"}}',
            "",
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_anthropic("sys", [], on_text_delta=chunks.append)

        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tu1"
        assert tc[0]["function"]["name"] == "get_netlist"
        args = json.loads(tc[0]["function"]["arguments"])
        assert args == {"path": "sch.kicad_sch"}
        assert result["finish_reason"] == "tool_calls"

    def test_stream_openai_ssl_fallback(self):
        import urllib.error
        client = _make_client()
        chunks = []

        fallback_result = {
            "finish_reason": "stop",
            "message": {"content": "Fallback text", "tool_calls": []},
        }

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("unknown url type: https")), \
             patch.object(client, "_call_openai", return_value=fallback_result) as mock_call:
            result = client._stream_openai("sys", [], on_text_delta=chunks.append)

        mock_call.assert_called_once()
        assert chunks == ["Fallback text"]
        assert result["message"]["content"] == "Fallback text"

    def test_run_passes_on_text_delta_to_call_llm(self):
        client = _make_client()
        final_response = {
            "finish_reason": "stop",
            "message": {"content": "done", "tool_calls": []},
        }
        on_delta = MagicMock()
        client._call_llm = MagicMock(return_value=final_response)
        client._fetch_tool_definitions = MagicMock(return_value=[])

        result = client.run("hello", context_block="", on_text_delta=on_delta)

        assert result == "done"
        # _call_llm must have been called with on_text_delta=on_delta
        call_kwargs = client._call_llm.call_args
        assert call_kwargs.kwargs.get("on_text_delta") is on_delta

