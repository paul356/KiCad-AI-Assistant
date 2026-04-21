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
