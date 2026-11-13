"""Tests for LLMClient history management (dedup + compaction)."""

import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
import threading
import time
import types
from unittest.mock import ANY, MagicMock, patch
import urllib.error
import urllib.request

import pytest

from kicad_plugin import llm_client
from kicad_plugin import session_store as sstore
from kicad_plugin.llm_client import LLMClient, LLMTurnFailure, _subprocess_sse_stream
from kicad_plugin.settings import DEFAULT_LLM_USER_AGENT
from kicad_plugin.tool_registry import TOOL_POLICIES


@pytest.fixture(autouse=True)
def _neutralize_system_proxy(monkeypatch):
    """Make these unit tests independent of the host's proxy configuration.

    Most tests here patch ``urllib.request.urlopen`` and drive loopback MCP
    URLs.  With a real system proxy configured, production code correctly
    takes the direct-opener path for those URLs and the patch would never be
    reached.  Proxy handling itself is covered by ``TestLoopbackProxyBypass``
    and ``TestProxyEndToEnd``.
    """
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})


def _on_event_collect(chunks):
    """Build an on_stream_event callback that records text chunks in order."""

    def _on_stream_event(evt):
        if evt.get("type") == "text_chunk":
            chunks.append(evt["content"])

    return _on_stream_event


class _SSETestServer(ThreadingHTTPServer):
    """Threaded localhost server that answers POST with an SSE body."""

    def __init__(self, lines, status=200, body=b""):
        self._lines = lines
        self._status = status
        self._body = body
        super().__init__(("127.0.0.1", 0), self._make_handler(), bind_and_activate=True)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(server._status)
                if server._status == 200:
                    payload = "".join(line + "\n" for line in server._lines)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload.encode())))
                    self.end_headers()
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                else:
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(server._body)))
                    self.end_headers()
                    self.wfile.write(server._body)

            def log_message(self, *args):  # silence test-server noise
                pass

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


def _make_client(
    context_tokens=10_000, compact_threshold=0.70, compact_target=0.49, keep_recent_turns=4
):
    settings = types.SimpleNamespace(
        llm_provider="openai",
        llm_api_key="sk-test",
        llm_user_agent=DEFAULT_LLM_USER_AGENT,
        llm_model="gpt-4o",
        llm_base_url="",
        llm_context_tokens=context_tokens,
        llm_compact_threshold=compact_threshold,
        llm_compact_target_threshold=compact_target,
        llm_keep_recent_turns=keep_recent_turns,
        llm_supports_vision=False,
    )
    return LLMClient(settings, mcp_base_url="http://127.0.0.1:9999")


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
# _dedup_tool_calls unit tests
# ---------------------------------------------------------------------------


class TestSetBaseUrl:
    def test_updates_url_after_construction(self):
        client = _make_client()
        assert client._mcp_base_url == "http://127.0.0.1:9999"
        client.set_base_url("http://127.0.0.1:5555")
        assert client._mcp_base_url == "http://127.0.0.1:5555"

    def test_none_then_real_url(self):
        client = _make_client()
        # Client constructed before the backend is up sees base URL None.
        client.set_base_url(None)
        client.set_base_url("http://127.0.0.1:1234")
        assert client._mcp_base_url == "http://127.0.0.1:1234"

    def test_url_change_invalidates_cached_registry(self):
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        client._enabled_tools = {"get_board_info"}
        client.set_base_url("http://127.0.0.1:7777")
        assert client._tool_registry is None
        # The enabled set is session state and survives a URL change; stale
        # names are pruned lazily by _build_request_tools once the catalog
        # is refetched.
        assert client._enabled_tools == {"get_board_info"}

    def test_build_request_tools_prunes_stale_enabled(self):
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        client._enabled_tools = {"get_board_info", "ghost_tool"}
        tools = client._build_request_tools()
        names = [t["function"]["name"] for t in tools]
        assert "ghost_tool" not in names
        assert "get_board_info" in names
        assert client._enabled_tools == {"get_board_info"}

    def test_build_request_tools_keeps_enabled_when_catalog_unknown(self):
        client = _make_client()
        client._tool_registry = None
        client._enabled_tools = {"get_board_info", "ghost_tool"}
        tools = client._build_request_tools()
        # Registry not yet fetched: no pruning, lazy adoption holds.
        assert client._enabled_tools == {"get_board_info", "ghost_tool"}
        assert all(t["function"]["name"] not in ("get_board_info", "ghost_tool") for t in tools)

    def test_same_url_keeps_cached_registry(self):
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        client._enabled_tools = {"get_board_info"}
        client.set_base_url("http://127.0.0.1:9999")
        assert client._tool_registry is not None
        assert client._enabled_tools == {"get_board_info"}


class TestDedupToolCalls:
    def test_no_change_when_no_tool_calls(self):
        client = _make_client()
        client._history = [_user(), _assistant()]
        client._dedup_tool_calls()
        assert len(client._history) == 2

    def test_single_tool_call_kept(self):
        client = _make_client()
        client._history = [
            _user("q"),
            _assistant(
                "thinking",
                tool_calls=[{"id": "tc1", "function": {"name": "get_netlist", "arguments": "{}"}}],
            ),
            _tool("tc1", "result"),
        ]
        client._dedup_tool_calls()
        assert len(client._history) == 3

    def test_duplicate_tool_call_oldest_dropped(self):
        """Three calls to same tool — only the latest survives."""
        client = _make_client()
        client._history = [
            _user("q1"),
            _assistant(
                "t1",
                tool_calls=[
                    {"id": "tc1", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc1", "r1"),
            _user("q2"),
            _assistant(
                "t2",
                tool_calls=[
                    {"id": "tc2", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc2", "r2"),
            _user("q3"),
            _assistant(
                "t3",
                tool_calls=[
                    {"id": "tc3", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc3", "r3"),
        ]
        client._dedup_tool_calls()
        # Only the latest tool turn (tc3) should survive; tc1 and tc2 turns dropped
        tool_ids_in_history = [
            tc["id"] for m in client._history if m.get("tool_calls") for tc in m["tool_calls"]
        ]
        assert tool_ids_in_history == ["tc3"]

    def test_two_different_tools_both_latest_survive(self):
        """Two different tools each called twice — both latest survive."""
        client = _make_client()
        client._history = [
            _user("q1"),
            _assistant(
                "t1", tool_calls=[{"id": "tc1", "function": {"name": "tool_A", "arguments": "{}"}}]
            ),
            _tool("tc1", "r1"),
            _user("q2"),
            _assistant(
                "t2", tool_calls=[{"id": "tc2", "function": {"name": "tool_B", "arguments": "{}"}}]
            ),
            _tool("tc2", "r2"),
            _user("q3"),
            _assistant(
                "t3", tool_calls=[{"id": "tc3", "function": {"name": "tool_A", "arguments": "{}"}}]
            ),
            _tool("tc3", "r3"),
            _user("q4"),
            _assistant(
                "t4", tool_calls=[{"id": "tc4", "function": {"name": "tool_B", "arguments": "{}"}}]
            ),
            _tool("tc4", "r4"),
        ]
        client._dedup_tool_calls()
        tool_ids = [
            tc["id"] for m in client._history if m.get("tool_calls") for tc in m["tool_calls"]
        ]
        # Only the latest call to each tool survives: tc3 (tool_A) and tc4 (tool_B)
        assert sorted(tool_ids) == ["tc3", "tc4"]

    def test_partial_overlap_turn_kept(self):
        """A turn calling tool_A+tool_B is kept if tool_B has not been seen yet."""
        client = _make_client()
        client._history = [
            _user("q1"),
            _assistant(
                "t1", tool_calls=[{"id": "tc1", "function": {"name": "tool_A", "arguments": "{}"}}]
            ),
            _tool("tc1", "r1"),
            _user("q2"),
            _assistant(
                "t2",
                tool_calls=[
                    {"id": "tc2a", "function": {"name": "tool_A", "arguments": "{}"}},
                    {"id": "tc2b", "function": {"name": "tool_B", "arguments": "{}"}},
                ],
            ),
            _tool("tc2a", "r2a"),
            _tool("tc2b", "r2b"),
        ]
        client._dedup_tool_calls()
        # tc2 turn calls tool_B for the first time → must be kept
        tool_ids = [
            tc["id"] for m in client._history if m.get("tool_calls") for tc in m["tool_calls"]
        ]
        assert "tc2a" in tool_ids or "tc2b" in tool_ids


# ---------------------------------------------------------------------------
# _compact_history unit tests
# ---------------------------------------------------------------------------


class TestCompactHistory:
    def _make_full_history(self, n_turns=6):
        """Build a history with n_turns complete user+assistant turns."""
        h = []
        for i in range(n_turns):
            h.append(_user(f"question {i}"))
            h.append(_assistant(f"answer {i}"))
        return h

    def test_returns_false_when_prefix_too_short(self):
        client = _make_client(keep_recent_turns=4)
        # Only 4 turns total → after reserving 4 recent turns the prefix is empty
        client._history = self._make_full_history(n_turns=4)
        result = client._compact_history("system", target_summary_chars=500)
        assert result is False
        assert len(client._history) == 8  # unchanged

    def test_compacts_prefix_and_preserves_recent_turns(self):
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)
        original_recent = client._history[-8:]  # last 4 complete turns

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User wants to place R1 and connect to GND."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history("system", target_summary_chars=1000)

        assert result is True
        # History should be: 1 summary message + last 4 turns (8 messages)
        assert len(client._history) == 9
        assert client._history[0]["role"] == "user"
        assert "[Session summary" in client._history[0]["content"]
        # Recent turns preserved verbatim
        assert client._history[1:] == original_recent

    def test_hard_clips_oversized_summary(self):
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)

        long_summary = "word " * 2000  # ~10 000 chars
        summary_response = {
            "finish_reason": "stop",
            "message": {"content": long_summary},
        }
        target = 100
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history("system", target_summary_chars=target)

        assert result is True
        stored = client._history[0]["content"]
        # The stored summary (inside the wrapper prefix) must not exceed target
        summary_part = stored.replace("[Session summary – earlier context]: ", "")
        assert len(summary_part) <= target

    def test_returns_false_on_llm_error(self):
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)
        original = list(client._history)

        error_response = {"error": "API error", "message": {}}
        with patch.object(client, "_call_openai", return_value=error_response):
            result = client._compact_history("system", target_summary_chars=500)

        assert result is False
        assert client._history == original  # unchanged

    def test_returns_false_on_exception(self):
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)
        original = list(client._history)

        with patch.object(client, "_call_openai", side_effect=RuntimeError("network")):
            result = client._compact_history("system", target_summary_chars=500)

        assert result is False
        assert client._history == original

    def test_dedup_runs_as_compaction_substep(self):
        """Dedup is folded into _compact_history, not a _maybe_compact lever."""
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)
        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User wants to place R1."},
        }
        with (
            patch.object(client, "_dedup_tool_calls") as mock_dedup,
            patch.object(client, "_call_openai", return_value=summary_response),
        ):
            result = client._compact_history("system", target_summary_chars=500)
        assert result is True
        mock_dedup.assert_called_once()

    def test_chunks_oversized_transcript_across_multiple_calls(self):
        # context 10k -> dedupe gate: no single compaction call may send more
        # than half the window (10_000 * 0.5 * 4 = 20_000 chars).
        client = _make_client(context_tokens=10_000, keep_recent_turns=4)
        # 8 turns, ~8k chars per message -> prefix of 4 turns ~= 64k chars of
        # transcript, well over the 20k per-call budget.
        client._history = []
        for i in range(8):
            client._history.append(_user("q" + "x" * 8_000))
            client._history.append(_assistant("a" + "y" * 8_000))
        original_recent = client._history[-8:]  # last 4 turns

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User intends to place R1, C2 and connect GND."},
        }
        prompts = []
        max_chunk_chars = int(10_000 * 0.5 * 4)

        def _capture(system_prompt, tools):
            # During the compaction call self._history holds the chunk prompt
            prompts.append(client._history[0]["content"])
            return summary_response

        with patch.object(client, "_call_openai", side_effect=_capture) as mock_call:
            result = client._compact_history("system", target_summary_chars=1000)

        assert result is True
        assert mock_call.call_count > 1, "oversized transcript must be chunked"
        assert mock_call.call_count == 4  # 64k chars / 20k per chunk, line-aligned
        for p in prompts:
            chunk = p.split("<session>\n", 1)[1].rsplit("\n</session>", 1)[0]
            assert len(chunk) <= max_chunk_chars  # per-call input stays within the gate
        assert client._history[0]["role"] == "user"
        assert "[Session summary" in client._history[0]["content"]
        assert client._history[1:] == original_recent

    def test_hard_splits_single_turn_larger_than_budget(self):
        # One message alone exceeds the per-call budget: it must be hard-split
        # so the compaction call still fits half the window.
        client = _make_client(context_tokens=10_000, keep_recent_turns=2)
        client._history = []
        for i in range(3):
            client._history.append(_user(f"pre q {i}"))
            client._history.append(_assistant(f"pre a {i}"))
        client._history.append(_user("QUESTION " + "x" * 45_000))  # > 20k gate
        client._history.append(_assistant("mid a"))
        for i in range(2):
            client._history.append(_user(f"post q {i}"))
            client._history.append(_assistant(f"post a {i}"))
        # 10 messages: prefix = 6 messages (3 pre turns + QUESTION + mid a) >= 4,
        # recent = last 2 turns preserved.
        max_chunk_chars = int(10_000 * 0.5 * 4)
        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User asked a long question."},
        }
        prompts = []

        def _capture(system_prompt, tools):
            prompts.append(client._history[0]["content"])
            return summary_response

        with patch.object(client, "_call_openai", side_effect=_capture) as mock_call:
            result = client._compact_history("system", target_summary_chars=1000)

        assert result is True
        # 45k chars -> three hard-split segments (20k + 20k + 5k), each its own
        # call, plus one call for the small pre-turn lines and one for the
        # trailing "mid a" line.
        assert mock_call.call_count == 5
        for p in prompts:
            chunk = p.split("<session>\n", 1)[1].rsplit("\n</session>", 1)[0]
            assert len(chunk) <= max_chunk_chars

    def test_absorbs_recent_turns_when_preserved_block_over_budget(self):
        # Recent turns alone (~800 est tokens) exceed target_history_tokens:
        # _compact_history must fold preserved turns into the prefix until the
        # remaining block fits the joint budget.  Each recent turn is ~200
        # tokens (heavy assistant answer) and the summary ceiling is 50 tokens,
        # so a 60-token budget accepts no turn at all — even the newest turn is
        # folded in and only the summary remains (issue #140: no "keep the
        # final turn" floor).
        client = _make_client(keep_recent_turns=4)
        # prefix: 4 small turns; recent: 4 turns with heavy assistant answers
        client._history = []
        for i in range(4):
            client._history.append(_user(f"small q {i}"))
            client._history.append(_assistant(f"small a {i}"))
        for i in range(4):
            client._history.append(_user(f"recent q {i}"))
            client._history.append(_assistant("recent big a " + "x" * 800))  # ~200 tok

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User worked through recent items."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=60
            )

        assert result is True
        # entire history folded into the summary — nothing preserved verbatim
        assert len(client._history) == 1
        assert client._history[0]["role"] == "user"
        assert "[Session summary" in client._history[0]["content"]

    def test_absorption_keeps_turns_that_fit(self):
        # Budget admits exactly the newest recent turns (each ~41 tokens:
        # user+assistant pair incl. JSON overhead): they stay verbatim, the
        # older ones are folded into the prefix.
        client = _make_client(keep_recent_turns=4)
        client._history = []
        for i in range(4):
            client._history.append(_user(f"small q {i}"))
            client._assistant_append = None
            client._history.append(_assistant(f"small a {i}"))
        for i in range(4):
            client._history.append(_user(f"recent q {i}"))
            client._history.append(_assistant("recent a " + "x" * 80))  # ~41 tok/turn
        original_rows = [m["content"] for m in client._history]

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        # recent 4 turns ≈ 164 tokens + summary ceiling 50 -> budget 600 admits all: floor 200 + turns 164
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=600
            )

        assert result is True
        assert len(client._history) == 1 + 8  # summary + 4 recent turns
        preserved_rows = [m["content"] for m in client._history[1:]]
        assert preserved_rows == original_rows[-8:]

    def test_absorption_partial_keeps_only_newest(self):
        # Budget admits ~2 recent turns; older preserved turns fold into prefix.
        client = _make_client(keep_recent_turns=4)
        client._history = []
        for i in range(4):
            client._history.append(_user(f"small q {i}"))
            client._history.append(_assistant(f"small a {i}"))
        for i in range(4):
            client._history.append(_user(f"recent q {i}"))
            client._history.append(_assistant("recent a " + "x" * 80))  # ~41 tok/turn
        original_rows = [m["content"] for m in client._history]

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        # budget 140: summary ceiling 50 -> split_tokens 90 admits exactly the
        # newest 2 turns (2 x 41), a 3rd (123) would overrun
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=300
            )

        assert result is True
        # summary + last 2 recent turns (2 x 2 rows = 4 messages)
        assert len(client._history) == 1 + 4
        preserved_rows = [m["content"] for m in client._history[1:]]
        assert preserved_rows == original_rows[-4:]

    def test_pending_question_preserved_when_preceding_turn_exceeds_budget(self):
        # Regression for issue #140: the live user question at the tail of
        # history must never be folded into the summary, even when the
        # preceding turn alone exceeds the entire history budget (the 16:31
        # session shape: a giant tool-result turn followed by a fresh user
        # question). Old code merged the question into the preceding turn on a
        # user→assistant boundary and broke on the first turn, dropping the
        # question into an 800-char-clipped summary.
        client = _make_client(keep_recent_turns=4)
        client._history = []
        # 4 small prefix turns
        for i in range(4):
            client._history.append(_user(f"old q {i}"))
            client._history.append(_assistant(f"old a {i}"))
        # One giant turn: user + assistant + huge tool result (~500 tok)
        tc = [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "list_tracks", "arguments": "{}"},
            }
        ]
        client._history.append(_user("Please help understand this project."))
        client._history.append(_assistant("pulling state", tool_calls=tc))
        client._history.append(_tool("tc1", "x" * 2000))  # ~500 tok
        # The live question — must be preserved verbatim
        live_q = "How files in this project?"
        client._history.append(_user(live_q))

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of earlier context."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=100
            )

        assert result is True
        # The live question must be in the preserved tail, not folded.
        contents = [m.get("content") for m in client._history]
        assert live_q in contents, f"live question lost: {contents}"
        assert client._history[-1]["content"] == live_q

    def test_absorption_counts_tool_messages_in_turn(self):
        # A turn ends at its assistant's next sibling, not at the assistant
        # itself: trailing tool-result messages must be counted in the turn's
        # size.  Here the final turn is user+asst(tool_call)+tool(Drc result);
        # tool alone is ~514 tok, so with the tool counted the turn (~566 tok)
        # exceeds split_tokens (308) and the whole history folds into the
        # summary; without counting the tool the turn (~51 tok) would "fit" and
        # stay verbatim, understating the post-compaction size (issue #140).
        client = _make_client(keep_recent_turns=1)
        client._history = []
        for i in range(3):
            client._history.append(_user(f"small q {i}"))
            client._history.append(_assistant(f"small a {i}"))
        tc = [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "route_pcb", "arguments": '{"net": "GND"}'},
            }
        ]
        client._history.append(_user("fold q"))
        client._history.append(_assistant("need routing", tool_calls=tc))
        client._history.append(_tool("tc1", "DRC " + "x" * 2000))

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=358
            )

        assert result is True
        # the 566-token turn does not fit split_tokens=308 -> everything folds
        assert len(client._history) == 1
        assert "[Session summary" in client._history[0]["content"]

    def test_absorption_tool_turn_fits_when_budget_allows(self):
        # Same turn shape, but a generous budget: the tool-carrying turn fits,
        # so it stays verbatim after the summary (fix guard: counting tool
        # messages must not over-reject turns that do fit).
        client = _make_client(keep_recent_turns=1)
        client._history = []
        for i in range(3):
            client._history.append(_user(f"small q {i}"))
            client._history.append(_assistant(f"small a {i}"))
        tc = [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "route_pcb", "arguments": '{"net": "GND"}'},
            }
        ]
        client._history.append(_user("fold q"))
        client._history.append(_assistant("need routing", tool_calls=tc))
        client._history.append(_tool("tc1", "DRC " + "x" * 2000))
        original = [m["content"] for m in client._history]

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=2_000
            )

        assert result is True
        assert len(client._history) == 1 + 3  # summary + full final turn
        assert [m["content"] for m in client._history[1:]] == original[-3:]

    def test_preserves_all_recent_when_budget_covers_them(self):
        # Generous target_history_tokens: the preserved block already fits, so
        # no absorption — identical shape to the no-absorption test.
        client = _make_client(keep_recent_turns=4)
        client._history = self._make_full_history(n_turns=8)
        original_recent = client._history[-8:]
        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "User wants R1 placed."},
        }
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=500, target_history_tokens=10_000
            )
        assert result is True
        assert client._history[1:] == original_recent  # all 4 recent turns preserved

    def test_large_running_summary_stays_within_half_window(self):
        # A compaction call sends preamble + running summary + chunk.  Even
        # when the LLM returns a near-maximum summary and the transcript is
        # huge, every all call must stay within half the window.
        client = _make_client(context_tokens=10_000, keep_recent_turns=4)
        client._history = []
        for i in range(8):
            client._history.append(_user("q" + "x" * 8_000))
            client._history.append(_assistant("a" + "y" * 8_000))

        # summary near the target cap (context*0.25*4 chars = 10k chars)
        big_summary = ("word " * 2_500)[:9_900]
        summary_response = {
            "finish_reason": "stop",
            "message": {"content": big_summary},
        }
        prompts = []

        def _capture(system_prompt, tools):
            prompts.append(client._history[0]["content"])
            return summary_response

        with patch.object(client, "_call_openai", side_effect=_capture) as mock_call:
            result = client._compact_history("system", target_summary_chars=10_000)

        assert result is True
        assert mock_call.call_count > 1
        half_window_chars = int(10_000 * 0.5 * 4)  # 20 000
        for p in prompts:
            assert len(p) <= half_window_chars, (
                f"compaction call exceeded half window: {len(p)} > {half_window_chars}"
            )

    def test_dedup_prunes_superseded_turns_inside_compaction(self):
        # Real dedup (not mocked) must run as a compaction sub-step: the
        # superseded tool turn is dropped before summarising, so its content
        # never reaches the summarisation prompt nor the stored history.
        client = _make_client(keep_recent_turns=4)
        # 8 turns: tc1 (superseded) in the compactable prefix; tc3 (latest
        # call to the same tool) inside the preserved recent block.  After
        # dedup drops the tc1 turn, 7 turns remain — enough that the prefix
        # is non-empty while the recent block still holds tc3.
        client._history = [
            _user("q1"),
            _assistant(
                "t1",
                tool_calls=[
                    {"id": "tc1", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc1", "SUPERSEDED-NETLIST-CONTENT"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
            _user("q4"),
            _assistant("a4"),
            _user("q5"),
            _assistant(
                "t5",
                tool_calls=[
                    {"id": "tc3", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc3", "latest result"),
            _user("q6"),
            _assistant("a6"),
            _user("q7"),
            _assistant("a7"),
        ]
        prompts = []

        def _capture(system_prompt, tools):
            prompts.append(client._history[0]["content"])
            return {"finish_reason": "stop", "message": {"content": "Session summary."}}

        with patch.object(client, "_call_openai", side_effect=_capture):
            result = client._compact_history("system", target_summary_chars=500)

        assert result is True
        # superseded turn (tc1) removed from stored history; the latest call
        # (tc3, inside the preserved recent block) survives
        tool_ids = [
            tc["id"] for m in client._history if m.get("tool_calls") for tc in m["tool_calls"]
        ]
        assert tool_ids == ["tc3"]
        assert client._history[0]["role"] == "user"  # summary message stored
        assert "[Session summary" in client._history[0]["content"]
        # superseded content must not have reached the summarisation prompt
        for p in prompts:
            assert "SUPERSEDED-NETLIST-CONTENT" not in p


# ---------------------------------------------------------------------------
# _maybe_compact unit tests
# ---------------------------------------------------------------------------


class TestMaybeCompact:
    def test_no_compaction_below_threshold(self):
        # Large context window, small history → should not trigger
        client = _make_client(context_tokens=128_000, compact_threshold=0.70)
        client._history = [_user("hi"), _assistant("hello")]
        with patch.object(client, "_compact_history") as mock_compact:
            client._maybe_compact("short system prompt")
        mock_compact.assert_not_called()

    def test_compaction_triggered_above_threshold(self):
        # Tiny context window so that a modest history exceeds the threshold
        client = _make_client(
            context_tokens=100, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        # Fill history with large messages that exceed 70 tokens (70% of 100)
        big_content = "x" * 400  # ~100 tokens each
        client._history = [
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
        ]
        with patch.object(client, "_compact_history", return_value=True) as mock_compact:
            client._maybe_compact("system")
        mock_compact.assert_called_once()
        # target_summary_chars is no longer pre-deducted in _maybe_compact
        # (it is computed inside _compact_history after absorption); the
        # joint history budget is the value _maybe_compact passes.
        called_args = mock_compact.call_args.args
        assert called_args[2] > 0  # target_history_tokens is a positive int

    def test_history_unmodified_under_budget(self):
        """Under budget: history is append-only — no dedup, no annotation, no compaction."""
        client = _make_client(context_tokens=128_000, compact_threshold=0.70)
        client._history = [_user("hi"), _assistant("hello")]
        with (
            patch.object(client, "_dedup_tool_calls") as mock_dedup,
            patch.object(client, "_annotate_stale_queries") as mock_annotate,
            patch.object(client, "_compact_history") as mock_compact,
        ):
            client._maybe_compact("short system prompt")
        mock_dedup.assert_not_called()
        mock_annotate.assert_not_called()
        mock_compact.assert_not_called()

    def test_dedup_not_called_at_budget_check(self):
        """Dedup moved inside _compact_history; _maybe_compact never calls it directly."""
        client = _make_client(
            context_tokens=100, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        big_content = "x" * 400  # ~100 tokens each
        client._history = [
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
        ]
        with (
            patch.object(client, "_dedup_tool_calls") as mock_dedup,
            patch.object(client, "_compact_history", return_value=True),
            patch.object(client, "_annotate_stale_queries"),
        ):
            client._maybe_compact("system")
        mock_dedup.assert_not_called()

    def test_dedup_can_avoid_compaction(self):
        """Dedup no longer a standalone budget lever: it runs inside _compact_history.

        A superseded duplicate tool turn is therefore not pruned by
        _maybe_compact itself; compaction is what handles over-budget.
        """
        client = _make_client(
            context_tokens=200, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        client._history = [
            _user("q1"),
            _assistant(
                "t1",
                tool_calls=[
                    {"id": "tc1", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc1", "x" * 400),
            _user("q2"),
            _assistant(
                "t2",
                tool_calls=[
                    {"id": "tc2", "function": {"name": "extract_netlist", "arguments": "{}"}}
                ],
            ),
            _tool("tc2", "y" * 200),
        ]
        with (
            patch.object(client, "_compact_history", return_value=True) as mock_compact,
            patch.object(client, "_annotate_stale_queries") as mock_annotate,
        ):
            client._maybe_compact("system")
        # Over budget with no registered tools to evict → compaction runs and
        # dedup happens inside it; the duplicate turn is still present here.
        mock_compact.assert_called_once()
        assert any(m.get("tool_call_id") == "tc1" for m in client._history)
        mock_annotate.assert_called_once()

    def test_annotate_runs_after_compaction(self):
        """Stale annotation runs after compaction, on the preserved turns only."""
        client = _make_client(
            context_tokens=100, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        big_content = "x" * 400
        client._history = [
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
        ]
        call_order: list[str] = []
        with (
            patch.object(client, "_compact_history", return_value=True) as mock_compact,
            patch.object(client, "_annotate_stale_queries") as mock_annotate,
        ):
            mock_compact.side_effect = lambda *a, **k: call_order.append("compact")
            mock_annotate.side_effect = lambda: call_order.append("annotate")
            client._maybe_compact("system")
        assert call_order == ["compact", "annotate"]

    def test_budget_parameters_stay_integer(self):
        """context_tokens * float threshold must not leak a float into the
        compaction budget: `summary[:target_summary_chars]` with a float
        slice crashes with "slice indices must be integers" (regression:
        the pre-fix target_post_compact was float, and the min() against
        the int window cap sometimes picked the float side)."""
        client = _make_client(
            context_tokens=100, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        big_content = "x" * 400  # ~100 tokens each
        client._history = [
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
        ]
        with patch.object(client, "_compact_history", return_value=True) as mock_compact:
            client._maybe_compact("system")
        called_args = mock_compact.call_args.args
        assert isinstance(called_args[2], int), called_args[2]

    def test_compaction_real_path_never_float_slices(self):
        """The full _maybe_compact -> _compact_history path survives a
        history that overflows the budget: the live LLM call we patch is
        the compaction summariser, so the real _compact_history code runs
        (float-free slices) instead of a mocked return."""
        client = _make_client(
            context_tokens=100, compact_threshold=0.70, compact_target=0.40, keep_recent_turns=2
        )
        big_content = "x" * 4000  # far over the 70-token threshold
        client._history = [
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
            _user(big_content),
            _assistant(big_content),
        ]
        # Compaction's own LLM call must succeed (short fake response).
        with patch.object(client, "_call_openai") as mock_openai:
            mock_openai.return_value = {"message": {"content": "summarised context"}}
            # Tools estimation walks the registry; a bare client is fine —
            # registry is None so zero tools.
            err = client._maybe_compact("system")
        assert err is None or err.startswith("[Error]")
        # History collapsed to summary + preserved recent turns (compaction
        # actually ran, reached the slice site).
        assert client._history[0]["role"] == "user"
        assert "[Session summary" in client._history[0]["content"]


# ---------------------------------------------------------------------------
# run() integration
# ---------------------------------------------------------------------------


class TestRunIntegration:
    def test_maybe_compact_called_before_llm(self):
        client = _make_client()
        final_response = {
            "finish_reason": "stop",
            "message": {"content": "done", "tool_calls": []},
        }
        client._call_llm = MagicMock(return_value=final_response)
        client._fetch_tool_definitions = MagicMock(return_value=[])

        with patch.object(client, "_maybe_compact", return_value=None) as mock_compact:
            result = client.run("new question", context_block="")

        assert result == "done"
        mock_compact.assert_called_once()

    def test_framework_auto_snapshots_and_reloads_mutations(self):
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._enabled_tools = {"set_footprint_position"}
        on_tool_call = MagicMock()

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("move R1", context_block="", on_tool_call=on_tool_call)

        assert result == "done"
        assert [c.args for c in mock_call_tool.call_args_list] == [
            (client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),
            (
                client._mcp_base_url,
                "save_project_version",
                {"project_file": "/tmp/board.kicad_pro"},
            ),
            (
                client._mcp_base_url,
                "set_footprint_position",
                {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
            ),
            (client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),
        ]
        assert [call.args[0] for call in on_tool_call.call_args_list] == [
            "save_document",
            "save_project_version",
            "set_footprint_position",
            "reload_kicad",
        ]

    def test_failed_mutation_still_reloads_dirty_path_at_turn_end(self):
        """A failed PCB mutation still marks the path dirty so reload_kicad
        runs at turn end, keeping the UI refresh consistent with the action."""
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "pcb_route_pad_to_pad",
                            "arguments": json.dumps(
                                {"pcb_path": "/tmp/board.kicad_pcb", "pad1": "A1", "pad2": "B2"}
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._enabled_tools = {"pcb_route_pad_to_pad"}
        on_tool_call = MagicMock()

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document
                {"success": True, "version_id": "v1"},
                {"success": False, "error": "routing failed"},  # pcb_route_pad_to_pad fails
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("route A1 to B2", context_block="", on_tool_call=on_tool_call)

        assert result == "done"
        assert [c.args for c in mock_call_tool.call_args_list] == [
            (client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),
            (
                client._mcp_base_url,
                "save_project_version",
                {"project_file": "/tmp/board.kicad_pro"},
            ),
            (
                client._mcp_base_url,
                "pcb_route_pad_to_pad",
                {"pcb_path": "/tmp/board.kicad_pcb", "pad1": "A1", "pad2": "B2"},
            ),
            (client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),
        ]
        assert [call.args[0] for call in on_tool_call.call_args_list] == [
            "save_document",
            "save_project_version",
            "pcb_route_pad_to_pad",
            "reload_kicad",
        ]

    def test_llm_error_after_mutation_still_reloads_dirty_path(self):
        """An [LLM error] exit still flushes dirty paths via the shared
        end-of-turn reload, matching the UI refresh display."""
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    }
                ],
            },
        }
        client._call_llm = MagicMock(side_effect=[tool_response, {"error": "API down"}])
        client._enabled_tools = {"set_footprint_position"}

        failures = []
        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("move R1", context_block="", on_turn_failure=failures.append)

        assert result == "[LLM error] API down"
        assert failures == [LLMTurnFailure.LLM_ERROR]
        assert mock_call_tool.call_args_list[-1].args == (
            client._mcp_base_url,
            "reload_kicad",
            {"paths": ["/tmp/board.kicad_pcb"]},
        )

    def test_max_iterations_exit_still_reloads_dirty_path(self, caplog):
        """Hitting the iteration cap still flushes dirty paths via the shared
        end-of-turn reload, logs a WARNING with the reply so the failure is
        greppable, and reports the structured ITERATION_CAP category
        (issue #191)."""
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-loop",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    }
                ],
            },
        }
        client._call_llm = MagicMock(return_value=tool_response)  # never stops calling tools
        client._enabled_tools = {"set_footprint_position"}

        failures = []
        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = lambda base, name, args, timeout=None: (
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []}
                if name == "reload_kicad"
                else {"success": True}
            )
            # The plugin logger sets propagate=False (kicad_plugin/__init__),
            # so pytest's root-level capture never sees the record; attach the
            # capture handler to the client logger itself, as the plugin's own
            # FileHandler would receive it.
            logger = logging.getLogger("kicad_plugin.llm_client")
            logger.addHandler(caplog.handler)
            try:
                with caplog.at_level("WARNING", logger="kicad_plugin.llm_client"):
                    result = client.run(
                        "move R1", context_block="", on_turn_failure=failures.append
                    )
            finally:
                logger.removeHandler(caplog.handler)

        assert failures == [LLMTurnFailure.ITERATION_CAP]

        cap_reply = "[Error] Maximum tool-call iterations reached. Please try a simpler request."
        assert result == cap_reply
        assert cap_reply in caplog.text  # greppable failure in the plugin log
        assert mock_call_tool.call_args_list[-1][0][1] == "reload_kicad"
        assert mock_call_tool.call_args_list[-1][0][2] == {"paths": ["/tmp/board.kicad_pcb"]}

    def test_compaction_error_fires_failure_callback(self):
        """A compaction failure reports COMPACTION_ERROR through
        on_turn_failure before any LLM call (issue #191, Plan A)."""
        client = _make_client()
        client._call_llm = MagicMock()
        failures = []
        with patch.object(client, "_maybe_compact", return_value="context window overflow"):
            result = client.run("hi", context_block="", on_turn_failure=failures.append)

        assert result == "[Error] context window overflow"
        assert failures == [LLMTurnFailure.COMPACTION_ERROR]
        client._call_llm.assert_not_called()

    def test_framework_error_fires_failure_callback(self):
        """A missing tool policy reports FRAMEWORK_ERROR through
        on_turn_failure before any LLM call (issue #191, Plan A)."""
        client = _make_client()
        client._call_llm = MagicMock()
        client._fetch_tool_definitions = MagicMock(return_value=[])
        failures = []
        with (
            patch.object(client, "_maybe_compact", return_value=None),
            patch(
                "kicad_plugin.llm_client.get_missing_tool_policies",
                return_value=["mystery_tool"],
            ),
        ):
            result = client.run("hi", context_block="", on_turn_failure=failures.append)

        assert "mystery_tool" in result
        assert failures == [LLMTurnFailure.FRAMEWORK_ERROR]
        client._call_llm.assert_not_called()

    def test_tool_exception_recorded_and_still_reloads_dirty_path(self):
        """A tool raising mid-turn no longer escapes run() (issue #176): the
        exception is captured as a recorded tool result so the failed call
        keeps its history pair, and the end-of-turn reload still flushes dirty
        paths from the earlier successful mutation."""
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    },
                    {
                        "id": "tc2",
                        "type": "function",
                        "function": {
                            "name": "flip_footprint",
                            "arguments": json.dumps(
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R2"}
                            ),
                        },
                    },
                ],
            },
        }
        client._call_llm = MagicMock(
            side_effect=[
                tool_response,
                {"finish_reason": "stop", "message": {"content": "done"}},
            ]
        )
        client._enabled_tools = {"set_footprint_position", "flip_footprint"}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document (snapshot for set_footprint_position)
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                # flip_footprint reuses the same-turn snapshot (no save calls)
                RuntimeError("boom"),  # flip_footprint raises → captured as result
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("edit board", context_block="")

        assert result == "done"
        # The failing call has a recorded tool result carrying the exception.
        failed = [
            json.loads(m["content"])
            for m in client._history
            if m["role"] == "tool" and "boom" in m["content"]
        ]
        assert len(failed) == 1
        assert failed[0]["success"] is False
        assert mock_call_tool.call_args_list[-1][0][1] == "reload_kicad"
        assert mock_call_tool.call_args_list[-1][0][2] == {"paths": ["/tmp/board.kicad_pcb"]}

    def test_framework_snapshots_each_file_once_per_turn(self):
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    },
                    {
                        "id": "tc2",
                        "type": "function",
                        "function": {
                            "name": "flip_footprint",
                            "arguments": json.dumps(
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R2"}
                            ),
                        },
                    },
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._enabled_tools = {"set_footprint_position", "flip_footprint"}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("edit board", context_block="")

        assert result == "done"
        assert [c.args for c in mock_call_tool.call_args_list] == [
            (client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),
            (
                client._mcp_base_url,
                "save_project_version",
                {"project_file": "/tmp/board.kicad_pro"},
            ),
            (
                client._mcp_base_url,
                "set_footprint_position",
                {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
            ),
            (
                client._mcp_base_url,
                "flip_footprint",
                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R2"},
            ),
            (client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),
        ]

    def test_explicit_save_project_version_is_reused_by_later_mutation(self):
        client = _make_client()
        save_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "save_project_version",
                            "arguments": json.dumps({"project_file": "/tmp/board.kicad_pro"}),
                        },
                    }
                ],
            },
        }
        mutate_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc2",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[save_response, mutate_response, final_response])
        client._enabled_tools = {"save_project_version", "set_footprint_position"}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("edit board", context_block="")

        assert result == "done"
        assert [c.args for c in mock_call_tool.call_args_list] == [
            (
                client._mcp_base_url,
                "save_project_version",
                {"project_file": "/tmp/board.kicad_pro"},
            ),
            (
                client._mcp_base_url,
                "set_footprint_position",
                {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
            ),
            (client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),
        ]

    def test_mutation_returns_error_when_auto_snapshot_fails(self):
        client = _make_client()
        tool_response = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "set_footprint_position",
                            "arguments": json.dumps(
                                {
                                    "pcb_path": "/tmp/board.kicad_pcb",
                                    "items": [{"reference": "R1", "x": 1.0}],
                                }
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._enabled_tools = {"set_footprint_position"}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.return_value = {"success": False, "error": "disk full"}
            result = client.run("move R1", context_block="")

        assert result == "done"
        assert [c.args for c in mock_call_tool.call_args_list] == [
            (client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),
            (
                client._mcp_base_url,
                "save_project_version",
                {"project_file": "/tmp/board.kicad_pro"},
            ),
        ]
        tool_results = [m for m in client._history if m["role"] == "tool"]
        tool_result = json.loads(tool_results[0]["content"])
        assert tool_result["success"] is False
        assert (
            "Failed to save project version before set_footprint_position" in tool_result["error"]
        )

    def test_enable_tool_refuses_tools_without_policy(self):
        """A tool with no execution policy must never become callable (issue #129)."""
        client = _make_client()
        client._tool_registry = {"unknown_tool": _fake_tool_def("unknown_tool")}
        result = client._execute_meta_tool("enable_tool", {"tools": ["unknown_tool"]})
        assert result["success"] is False
        assert "no execution policy" in result["error"]
        assert "unknown_tool" not in client._enabled_tools

    def test_tool_returned_image_injected_only_when_vision_enabled(self):
        """A tool result with an MCP image block becomes an image_url user
        message only when the model is configured to accept images; otherwise
        the text report alone is kept (matches the UI attachment gate)."""
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}

        def _image_result(*_args, **_kwargs) -> dict:
            return {
                "success": True,
                "text": "board rendered",
                "_image": {"media_type": "image/png", "data": "QUJD"},
            }

        # vision disabled (default) — image must NOT reach history
        client = _make_client()
        client._call_llm = MagicMock(
            side_effect=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {
                                    "name": "export_pcb_layer_image",
                                    "arguments": json.dumps({"pcb_path": "/tmp/board.kicad_pcb"}),
                                },
                            }
                        ],
                    },
                },
                final_response,
            ]
        )
        client._enabled_tools = {"export_pcb_layer_image"}
        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_image_result) as mock_tool:
            mock_tool.side_effect = _image_result
            result = client.run("render the board", context_block="")

        assert result == "done"
        image_msgs = [m for m in client._history if isinstance(m.get("content"), list)]
        assert image_msgs == [], "image must not be injected when vision is disabled"

        # vision enabled — image_url user message appended after tool results
        client = _make_client()
        client._settings.llm_supports_vision = True
        client._call_llm = MagicMock(
            side_effect=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {
                                    "name": "export_pcb_layer_image",
                                    "arguments": json.dumps({"pcb_path": "/tmp/board.kicad_pcb"}),
                                },
                            }
                        ],
                    },
                },
                final_response,
            ]
        )
        client._enabled_tools = {"export_pcb_layer_image"}
        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_image_result) as mock_tool:
            mock_tool.side_effect = _image_result
            result = client.run("render the board", context_block="")

        assert result == "done"
        image_msgs = [m for m in client._history if isinstance(m.get("content"), list)]
        assert len(image_msgs) == 1
        blocks = image_msgs[0]["content"]
        assert blocks[0]["type"] == "image_url"
        assert blocks[0]["image_url"]["url"] == "data:image/png;base64,QUJD"


class TestMCPToolTimeoutAndInterrupt:
    """Issue #176: long MCP tool calls must not be silently dropped.  A call
    that exceeds the (configurable) threshold or is interrupted by Stop ends
    the turn with an explicit error, and the failure is recorded in
    llm_history (and therefore the session payload)."""

    def _tool_response(self, name="get_board_info", args=None, call_id="tc1"):
        return {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args or {}),
                        },
                    }
                ],
            },
        }

    def test_call_mcp_tool_returns_timeout_dict_when_transport_blocks(self):
        """call_mcp_tool itself must return a distinguishable timeout error
        instead of hanging when the server keeps the stream alive past the
        per-call deadline."""

        def _blocking_urlopen(request, timeout=None):
            time.sleep(10)  # server is streaming, no data comes
            raise AssertionError("urlopen must be abandoned at the deadline")

        with patch("urllib.request.urlopen", side_effect=_blocking_urlopen):
            out = llm_client.call_mcp_tool("http://127.0.0.1:9", "some_tool", {}, timeout=0.05)

        assert out["success"] is False
        assert out["timed_out"] is True
        assert "did not complete within" in out["error"]
        assert "0.05" in out["error"]

    def test_tool_call_over_threshold_times_out_and_ends_turn(self):
        """A call longer than the configured threshold ends the turn with an
        explicit timeout error; the tool result entry (with the reason) is
        written into llm_history and the LLM is not re-invoked."""
        client = _make_client()
        client._mcp_tool_timeout = 0.05
        client._call_llm = MagicMock(return_value=self._tool_response())

        def _block(base_url, tool_name, args, timeout=None):
            time.sleep(10)  # longer than the configured threshold
            raise AssertionError("blocking call must be abandoned on timeout")

        failures = []
        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_block):
            result = client.run("do it", context_block="", on_turn_failure=failures.append)

        assert "[Error]" in result
        assert "did not complete within" in result
        assert failures == [LLMTurnFailure.TIMED_OUT]
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        assert recorded["success"] is False
        assert recorded["timed_out"] is True
        assert "did not complete within" in recorded["error"]
        # The timeout must end the turn: no retry, no second LLM call.
        client._call_llm.assert_called_once()

    def test_cancel_event_interrupts_inflight_tool_call(self):
        """Pressing Stop while a call is in flight records an interruption
        error entry and ends the turn instead of dropping the call."""
        client = _make_client()
        client._mcp_tool_timeout = 30  # longer than the test window
        client._call_llm = MagicMock(return_value=self._tool_response())
        cancel = threading.Event()

        def _block(base_url, tool_name, args, timeout=None):
            time.sleep(10)
            return {"success": True}

        def _press_stop():
            time.sleep(0.1)
            cancel.set()

        failures = []
        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_block):
            stopper = threading.Thread(target=_press_stop)
            stopper.start()
            try:
                result = client.run(
                    "do it",
                    context_block="",
                    cancel_event=cancel,
                    on_turn_failure=failures.append,
                )
            finally:
                stopper.join(timeout=2)

        assert "[Interrupted]" in result
        assert "interrupted by user" in result
        assert failures == [LLMTurnFailure.INTERRUPTED]
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        assert recorded["success"] is False
        assert recorded["interrupted"] is True
        assert "interrupted by user" in recorded["error"]
        client._call_llm.assert_called_once()

    def test_cancel_before_execution_skips_tool(self):
        """A cancel set before the tool runs records the interruption without
        executing (or waiting on) the tool at all."""
        client = _make_client()
        client._call_llm = MagicMock(return_value=self._tool_response())
        cancel = threading.Event()
        cancel.set()

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            result = client.run("do it", context_block="", cancel_event=cancel)

        assert "[Interrupted]" in result
        mock_call_tool.assert_not_called()
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        assert json.loads(tool_results[0]["content"])["interrupted"] is True

    def test_failed_call_result_written_into_session_payload(self):
        """A failed tool call must write a result entry carrying the
        server-side error into the session payload (llm_history), so the next
        turn can see why the call failed."""
        client = _make_client()
        route_args = {
            "pcb_path": "/tmp/board.kicad_pcb",
            "ref_a": "U11",
            "pad_a": "3v3",
            "ref_b": "J2",
            "pad_b": "3",
            "net": "3v3",
        }
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(name="pcb_route_pad_to_pad", args=route_args),
                {"finish_reason": "stop", "message": {"content": "routing rejected"}},
            ]
        )
        client._enabled_tools = {"pcb_route_pad_to_pad"}

        def _server(base_url, name, args, timeout=None):
            if name == "pcb_route_pad_to_pad":
                return {"success": False, "error": "cannot shove track through U11"}
            return {"success": True}

        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_server):
            result = client.run("route 3v3", context_block="")

        assert result == "routing rejected"  # LLM saw the error and wrapped up
        payload = sstore.make_payload([], client.get_history(), None, client.get_enabled_tools())
        tool_msgs = [m for m in payload["llm_history"] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        recorded = json.loads(tool_msgs[0]["content"])
        assert recorded["success"] is False
        assert "cannot shove track through U11" in recorded["error"]

    def _two_call_response(self):
        """A batch of two tool calls (client-side our tool ids)."""
        return {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "get_board_info", "arguments": "{}"},
                    },
                    {
                        "id": "tc2",
                        "type": "function",
                        "function": {"name": "get_schematic_components", "arguments": "{}"},
                    },
                ],
            },
        }

    def _assert_all_tool_call_ids_answered(self, client):
        """Every tool_call_id in the saved assistant message must have a
        matching tool result entry (Anthropic pairing contract)."""
        assistant_msgs = [
            m for m in client._history if m["role"] == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_msgs) == 1
        assistant_ids = {tc["id"] for tc in assistant_msgs[0]["tool_calls"]}
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert assistant_ids == {t["tool_call_id"] for t in tool_results}
        return tool_results

    def test_interrupted_batch_records_results_for_every_tool_call(self):
        """Stop mid-batch: the unexecuted calls still get recorded results,
        so no tool_call_id is left orphaned in llm_history."""
        client = _make_client()
        client._call_llm = MagicMock(return_value=self._two_call_response())
        cancel = threading.Event()
        cancel.set()

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            result = client.run("do it", context_block="", cancel_event=cancel)

        assert "[Interrupted]" in result
        mock_call_tool.assert_not_called()  # nothing in the batch was executed
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 2
        for m in tool_results:
            recorded = json.loads(m["content"])
            assert recorded["success"] is False
            assert recorded["interrupted"] is True
        client._call_llm.assert_called_once()

    def test_timeout_batch_records_skip_results_for_remaining_calls(self):
        """A timeout abort leaves no orphaned tool_call_id either: the calls
        after the failing one are recorded as not executed, and the
        remaining tool is never invoked."""
        client = _make_client()
        client._mcp_tool_timeout = 0.05
        client._call_llm = MagicMock(return_value=self._two_call_response())

        def _block(base_url, tool_name, args, timeout=None):
            time.sleep(10)
            raise AssertionError("blocking call must be abandoned on timeout")

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = _block
            result = client.run("do it", context_block="")

        assert "[Error]" in result
        assert mock_call_tool.call_count == 1  # the 2nd call was never executed
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 2
        timed_out = json.loads(tool_results[0]["content"])
        assert timed_out["timed_out"] is True
        skipped = json.loads(tool_results[1]["content"])
        assert skipped["success"] is False
        assert "not executed" in skipped["error"]
        assert "interrupted" not in skipped

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError("peer closed"),
            ssl.SSLError(1, "tls fail"),
            http.client.RemoteDisconnected("gone"),
        ],
    )
    def test_transport_oserror_returns_failure_not_raise(self, exc):
        """The worker's blanket exception handler must capture every
        OSError-family transport failure — ConnectionResetError,
        ssl.SSLError, RemoteDisconnected — into a structured failure instead
        of letting it escape the turn (issue #176)."""
        with patch("urllib.request.urlopen", side_effect=exc):
            out = llm_client.call_mcp_tool("http://127.0.0.1:9", "pcb_route_pad_to_pad", {})
        assert out["success"] is False
        assert type(exc).__name__ in out["error"]

    def test_mixed_batch_timeout_records_all_and_skips_tail(self):
        """[executed-ok, blocking timeout, tail]: the executed call is
        recorded normally, the timeout aborts with [Error], and the tail
        gets a synthesized 'not executed' result — no orphaned id."""
        client = _make_client()
        client._mcp_tool_timeout = 0.05
        client._call_llm = MagicMock(
            return_value={
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                        {
                            "id": "tc2",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                        {
                            "id": "tc3",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                    ],
                },
            }
        )

        def _transport(base_url, tool_name, args, timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                return {"success": True}
            time.sleep(10)  # second call blocks past the deadline
            raise AssertionError("blocking call must be abandoned on timeout")

        calls = [0]
        with patch(
            "kicad_plugin.llm_client.call_mcp_tool", side_effect=_transport
        ) as mock_call_tool:
            result = client.run("do it", context_block="")

        assert "[Error]" in result
        assert mock_call_tool.call_count == 2  # tc1 ok, tc2 blocked; tc3 never executed
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 3
        assert json.loads(tool_results[0]["content"])["success"] is True
        assert json.loads(tool_results[1]["content"])["timed_out"] is True
        skipped = json.loads(tool_results[2]["content"])
        assert skipped["success"] is False
        assert "not executed" in skipped["error"]

    def test_image_result_relayed_when_aborting_call_times_out(self):
        """An image-bearing tool result that lands before the aborting call
        is still peeled and relayed (vision enabled), the timeout aborts,
        and the interrupted tail still gets its synthesized result."""
        client = _make_client()
        client._mcp_tool_timeout = 0.05
        client._settings.llm_supports_vision = True
        client._call_llm = MagicMock(
            return_value={
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                        {
                            "id": "tc2",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                        {
                            "id": "tc3",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": "{}"},
                        },
                    ],
                },
            }
        )

        def _transport(base_url, tool_name, args, timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                return {
                    "success": True,
                    "text": "board rendered",
                    "_image": {"media_type": "image/png", "data": "QUJD"},
                }
            time.sleep(10)  # second call blocks past the deadline
            raise AssertionError("blocking call must be abandoned on timeout")

        calls = [0]
        with patch(
            "kicad_plugin.llm_client.call_mcp_tool", side_effect=_transport
        ) as mock_call_tool:
            result = client.run("do it", context_block="")

        assert "[Error]" in result
        assert mock_call_tool.call_count == 2
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 3
        # Image peeled off the executed call's recorded result...
        first = json.loads(tool_results[0]["content"])
        assert first["success"] is True
        assert "_image" not in first
        # ...and relayed as an image_url user message despite the aborted turn.
        image_msgs = [m for m in client._history if isinstance(m.get("content"), list)]
        assert len(image_msgs) == 1
        assert image_msgs[0]["content"][0]["image_url"]["url"] == "data:image/png;base64,QUJD"
        assert json.loads(tool_results[1]["content"])["timed_out"] is True
        assert "not executed" in json.loads(tool_results[2]["content"])["error"]

    def test_arguments_null_tail_does_not_typeerror(self):
        """A provider sending 'arguments': null (both on the aborting call
        and on the unexecuted tail) must not raise TypeError from
        json.loads(None) in either the main loop or the skip helper."""
        client = _make_client()
        client._call_llm = MagicMock(
            return_value={
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": None},
                        },
                        {
                            "id": "tc2",
                            "type": "function",
                            "function": {"name": "get_board_info", "arguments": None},
                        },
                    ],
                },
            }
        )
        cancel = threading.Event()
        cancel.set()

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            result = client.run("do it", context_block="", cancel_event=cancel)

        assert "[Interrupted]" in result
        mock_call_tool.assert_not_called()
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 2
        for m in tool_results:
            assert json.loads(m["content"])["interrupted"] is True

    def test_snapshot_phase_timeout_aborts_turn_with_marker(self):
        """A Stop/deadline landing during the pre-tool snapshot phase must
        carry the marker: the turn aborts with [Error] and records
        timed_out, and the main tool never runs with a fresh deadline."""
        client = _make_client()
        client._mcp_tool_timeout = 0.05
        client._call_llm = MagicMock(
            return_value=self._tool_response(
                name="pcb_route_pad_to_pad",
                args={"pcb_path": "/tmp/board.kicad_pcb", "pad1": "A1", "pad2": "B2"},
            )
        )
        client._enabled_tools = {"pcb_route_pad_to_pad"}

        def _block(base_url, tool_name, args, timeout=None):
            time.sleep(10)
            raise AssertionError("blocking call must be abandoned on timeout")

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = _block  # save_document blocks
            result = client.run("route A1 to B2", context_block="")

        assert "[Error]" in result
        assert "did not complete within" in result
        assert mock_call_tool.call_count == 1  # save_document only; snapshot+tool skipped
        tool_results = self._assert_all_tool_call_ids_answered(client)
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        assert recorded["success"] is False
        assert recorded["timed_out"] is True
        client._call_llm.assert_called_once()


class TestStatusPollBlocking:
    """Issue #190: status polls for background operations must not burn the
    turn's iteration cap.  When get_symbol_sync_status /
    get_footprint_sync_status report the operation still running, the client
    re-queries them client-side (bounded by the per-call timeout) and hands
    the model the final snapshot, so the whole wait costs one poll round."""

    def _tool_response(self, name="get_symbol_sync_status", args=None, call_id="tc1"):
        return {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args or {}),
                        },
                    }
                ],
            },
        }

    def test_status_poll_blocks_until_op_finishes_without_extra_rounds(self):
        """A background op that takes several poll snapshots completes inside
        the model's single status call: no extra LLM rounds are consumed and
        the recorded tool result is the final snapshot."""
        client = _make_client()
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),  # model polls the sync status
                {"finish_reason": "stop", "message": {"content": "sync done"}},
            ]
        )
        status_calls = {"n": 0}

        def _status(base_url, tool_name, args, timeout=None):
            if tool_name != "get_symbol_sync_status":
                return {"success": True}
            status_calls["n"] += 1
            if status_calls["n"] < 3:
                return {"running": True, "current": status_calls["n"], "total": 10}
            return {
                "running": False,
                "current": 10,
                "total": 10,
                "last_result": {"symbols": 1234},
            }

        with (
            patch("kicad_plugin.llm_client._STATUS_POLL_INTERVAL_SECONDS", 0.01),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_status),
        ):
            result = client.run("sync symbols", context_block="")

        assert result == "sync done"
        # The whole wait happened inside one model poll round: round 1 asks
        # for the status, round 2 sees the finished sync and answers.
        assert client._call_llm.call_count == 2
        # Intermediate snapshots were polled client-side, not round-tripped
        # through the model.
        assert status_calls["n"] == 3
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        assert recorded["running"] is False
        assert recorded["last_result"] == {"symbols": 1234}

    def test_status_poll_when_op_already_done_does_not_block(self):
        """A status call that reports the op finished is a plain single MCP
        call: no client-side re-polling happens."""
        client = _make_client()
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),
                {"finish_reason": "stop", "message": {"content": "ok"}},
            ]
        )

        def _status(base_url, tool_name, args, timeout=None):
            return {"running": False, "last_result": {"symbols": 5}}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = _status
            result = client.run("sync symbols", context_block="")

        assert result == "ok"
        assert mock_call_tool.call_count == 1

    def test_status_poll_budget_exhaustion_returns_last_snapshot(self):
        """When the sync outlasts the wait budget the last snapshot is
        returned (no error, no timeout marker) and the turn continues; the
        model may poll again with a fresh budget.  Budget exhaustion is not
        a fatal exit, so the on_turn_failure callback must NOT fire (issue
        #191: only the iteration-cap else-branch — reached when the model
        re-polls past _MAX_ITERATIONS — reports a failure)."""
        client = _make_client()
        client._mcp_tool_timeout = 0.1
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),
                {"finish_reason": "stop", "message": {"content": "still syncing, try later"}},
            ]
        )
        failures: list = []

        def _status(base_url, tool_name, args, timeout=None):
            return {"running": True, "current": 5, "total": 10}

        with (
            patch("kicad_plugin.llm_client._STATUS_POLL_INTERVAL_SECONDS", 0.02),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_status),
        ):
            result = client.run("sync symbols", context_block="", on_turn_failure=failures.append)

        assert result == "still syncing, try later"
        assert failures == []  # budget exhaustion is not a fatal exit
        assert client._call_llm.call_count == 2
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        # Last snapshot, not a synthesized error.
        assert recorded["running"] is True
        assert recorded["current"] == 5
        assert "timed_out" not in recorded
        assert recorded.get("interrupted") is not True

    def test_cancel_during_status_wait_aborts_turn(self):
        """Pressing Stop while the client blocks on a running sync aborts the
        turn immediately with the interruption recorded (issue #176
        semantics), without waiting out another LLM round."""
        client = _make_client()
        client._call_llm = MagicMock(return_value=self._tool_response())
        cancel = threading.Event()

        def _status(base_url, tool_name, args, timeout=None):
            return {"running": True, "current": 1, "total": 10}

        def _press_stop():
            time.sleep(0.05)
            cancel.set()

        with (
            patch("kicad_plugin.llm_client._STATUS_POLL_INTERVAL_SECONDS", 0.02),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_status),
        ):
            stopper = threading.Thread(target=_press_stop)
            stopper.start()
            try:
                result = client.run("sync symbols", context_block="", cancel_event=cancel)
            finally:
                stopper.join(timeout=2)

        assert "[Interrupted]" in result
        assert "get_symbol_sync_status interrupted by user" in result
        tool_results = [m for m in client._history if m["role"] == "tool"]
        assert len(tool_results) == 1
        recorded = json.loads(tool_results[0]["content"])
        assert recorded["interrupted"] is True
        client._call_llm.assert_called_once()

    def test_status_poll_emits_progress_notice_while_waiting(self):
        """A blocking status-poll wait surfaces a transient progress notice
        ("Symbol sync: 1/10 — Device/R") on the status channel (issue #190):
        the user sees real sync progress instead of a silent block.  A short
        wait emits exactly one notice — the immediate one; the 5 s rate
        window is never crossed."""
        client = _make_client()
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),  # model polls the sync status
                {"finish_reason": "stop", "message": {"content": "sync done"}},
            ]
        )
        events: list[dict] = []
        status_calls = {"n": 0}

        def _status(base_url, tool_name, args, timeout=None):
            status_calls["n"] += 1
            if status_calls["n"] < 3:
                return {
                    "running": True,
                    "current": status_calls["n"],
                    "total": 10,
                    "current_library": "Device/R",
                }
            return {"running": False, "current": 10, "total": 10, "last_result": {"symbols": 1}}

        with (
            patch("kicad_plugin.llm_client._STATUS_POLL_INTERVAL_SECONDS", 0.01),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_status),
        ):
            result = client.run("sync symbols", context_block="", on_stream_event=events.append)

        assert result == "sync done"
        progress = [
            e for e in events if e.get("type") == "status" and "Symbol sync:" in e.get("text", "")
        ]
        assert len(progress) == 1
        # The first notice surfaces the initial status snapshot.
        assert progress[0]["text"] == "Symbol sync: 1/10 — Device/R"
        assert progress[0]["color_hex"] == "#BE6400"
        # The snapshot returned to the model is still the final one.
        recorded = json.loads([m for m in client._history if m["role"] == "tool"][0]["content"])
        assert recorded["running"] is False

    def test_status_poll_emits_no_wait_notice_when_not_blocking(self):
        """An op that is already done never emits a wait notice: the status
        channel stays silent for a non-blocking poll."""
        client = _make_client()
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),
                {"finish_reason": "stop", "message": {"content": "ok"}},
            ]
        )
        events: list[dict] = []

        def _status(base_url, tool_name, args, timeout=None):
            return {"running": False, "last_result": {"symbols": 5}}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = _status
            result = client.run("sync symbols", context_block="", on_stream_event=events.append)

        assert result == "ok"
        assert mock_call_tool.call_count == 1
        assert [e for e in events if e.get("type") == "status"] == []

    def test_status_poll_progress_notices_rate_limited_to_5s(self):
        """Progress notices are rate-limited to one per 5 s of elapsed wait
        time (plus the immediate first one) — the UI is not flooded once per
        poll — and each notice surfaces the freshest progress seen.  Uses a
        fake clock so the cadence is deterministic."""
        client = _make_client()
        client._mcp_tool_timeout = 60
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(),
                {"finish_reason": "stop", "message": {"content": "ok"}},
            ]
        )
        events: list[tuple[float, dict]] = []
        status_calls = {"n": 0}
        fake_now = {"t": 0.0}

        def _status(base_url, tool_name, args, timeout=None):
            status_calls["n"] += 1
            return {
                "running": True,
                "current": status_calls["n"],
                "total": 100,
                "current_library": "Device",
            }

        def _fake_sleep(seconds):
            fake_now["t"] += seconds

        def _fake_monotonic():
            return fake_now["t"]

        def _collect(evt):
            events.append((fake_now["t"], evt))

        with (
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_status),
            patch("kicad_plugin.llm_client.time.sleep", side_effect=_fake_sleep),
            patch("kicad_plugin.llm_client.time.monotonic", side_effect=_fake_monotonic),
        ):
            result = client.run("sync symbols", context_block="", on_stream_event=_collect)

        assert result == "ok"
        progress = [
            (t, e)
            for (t, e) in events
            if e.get("type") == "status" and "Symbol sync:" in e.get("text", "")
        ]
        # Wait runs the full 60 s budget; notices at t=0 then every 5 s.
        assert [t for t, _ in progress] == [float(k) for k in range(0, 60, 5)]
        # Each notice carries the freshest snapshot: the emit at t=5*k saw
        # the poll right before it (current = 5*k + 1).
        currents = [
            int(re.search(r"Symbol sync: (\d+)/100 — Device", e["text"]).group(1))
            for _, e in progress
        ]
        assert currents == [5 * k + 1 for k in range(len(progress))]
        assert all(e["color_hex"] == "#BE6400" for _, e in progress)

    def test_wait_status_text_surfaces_progress_guards(self):
        """The progress notice format guards total==0 ("preparing…") and a
        missing/empty current_library (no trailing dash segment)."""
        status = llm_client._wait_status_text
        assert (
            status(
                "get_symbol_sync_status",
                {"current": 342, "total": 1250, "current_library": "Device/R"},
            )
            == "Symbol sync: 342/1250 — Device/R"
        )
        assert (
            status("get_symbol_sync_status", {"current": 342, "total": 1250, "current_library": ""})
            == "Symbol sync: 342/1250"
        )
        assert (
            status("get_symbol_sync_status", {"current": 0, "total": 0, "current_library": None})
            == "Symbol sync: preparing…"
        )
        assert (
            status(
                "get_symbol_sync_status", {"current": 0, "total": 0, "current_library": "Device"}
            )
            == "Symbol sync: preparing… — Device"
        )
        assert (
            status("get_footprint_sync_status", {"current": 0, "total": 0, "current_library": ""})
            == "Footprint sync: preparing…"
        )
        # Unmapped name falls back to the raw tool name (defensive).
        assert (
            status("get_board_info", {"current": 1, "total": 2, "current_library": ""})
            == "get_board_info: 1/2"
        )


class TestToolPolicyRegistry:
    def test_registry_covers_plugin_tool_surface(self):
        """All TOOL_POLICIES entries must have valid ToolPolicy fields.

        Note: exact set cross-validation is done by
        ``tests/unit/plugin/test_tool_registry_alignment.py``.
        """
        assert len(TOOL_POLICIES) > 0
        for name, policy in TOOL_POLICIES.items():
            assert policy.kind in (
                "query",
                "file_mutation",
                "versioning",
                "ui_refresh",
                "ipc_action",
                "indexing",
            ), f"{name}: invalid kind {policy.kind}"
            if policy.auto_snapshot:
                assert policy.path_arg is not None, f"{name}: auto_snapshot requires path_arg"


# ---------------------------------------------------------------------------
# OpenAI-compatible endpoint and HTTPS fallback tests
# ---------------------------------------------------------------------------


class TestOpenAICompatibleRequests:
    def test_custom_base_url_remains_available_on_openai_provider(self):
        client = _make_client()
        client._settings.llm_base_url = "https://gateway.example/v1"
        response = json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        )

        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post:
            client._call_openai("system", [])

        assert post.call_args.args[0] == "https://gateway.example/v1/chat/completions"

    def test_custom_base_url_remains_available_on_anthropic_provider(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        client._settings.llm_base_url = "https://gateway.example/anthropic"
        response = json.dumps(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post:
            client._call_anthropic("system", [])

        assert post.call_args.args[0] == "https://gateway.example/anthropic/v1/messages"

    def test_call_anthropic_always_sends_max_tokens(self):
        """Anthropic API requires max_tokens; compatible gateways reject
        requests without it (400 InvalidParameter)."""
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        response = json.dumps(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post:
            client._call_anthropic("system", [])

        payload = json.loads(post.call_args.args[2])
        assert payload["max_tokens"] == llm_client._ANTHROPIC_DEFAULT_MAX_TOKENS

    def test_call_anthropic_honors_configured_max_tokens(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        client._max_tokens = 128
        response = json.dumps(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post:
            client._call_anthropic("system", [])

        payload = json.loads(post.call_args.args[2])
        assert payload["max_tokens"] == 128

    def test_api_key_adds_bearer_authorization(self):
        client = _make_client()

        assert client._openai_headers() == {
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-test",
            "User-Agent": DEFAULT_LLM_USER_AGENT,
        }

    def test_empty_api_key_omits_authorization(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        assert client._openai_headers() == {
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_LLM_USER_AGENT,
        }

    def test_api_key_adds_x_api_key(self):
        client = _make_client()

        assert client._anthropic_headers() == {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "sk-test",
            "User-Agent": DEFAULT_LLM_USER_AGENT,
        }

    def test_empty_api_key_omits_x_api_key(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        assert client._anthropic_headers() == {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": DEFAULT_LLM_USER_AGENT,
        }

    def test_default_user_agent_is_proven_python_requests(self):
        """The default UA must be a proven value, not a plugin-branded one."""
        client = _make_client()

        for headers in (client._openai_headers(), client._anthropic_headers()):
            assert headers["User-Agent"] == "python-requests/2.32.3"
            assert not headers["User-Agent"].startswith("KiCad-AI-Assistant")

    def test_default_user_agent_with_empty_api_key(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        for headers in (client._openai_headers(), client._anthropic_headers()):
            assert headers["User-Agent"] == "python-requests/2.32.3"
            assert not headers["User-Agent"].startswith("KiCad-AI-Assistant")

    def test_user_agent_never_starts_with_python_urllib(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        for headers in (client._openai_headers(), client._anthropic_headers()):
            assert not headers["User-Agent"].startswith("Python-urllib")

    def test_both_builders_send_identical_user_agent(self):
        client = _make_client()

        openai_ua = client._openai_headers()["User-Agent"]
        anthropic_ua = client._anthropic_headers()["User-Agent"]
        assert openai_ua == anthropic_ua
        assert anthropic_ua == DEFAULT_LLM_USER_AGENT

    def test_custom_user_agent_used_by_openai_headers(self):
        client = _make_client()
        client._settings.llm_user_agent = "axios/1.7.0"

        assert client._openai_headers()["User-Agent"] == "axios/1.7.0"

    def test_custom_user_agent_used_by_anthropic_headers(self):
        client = _make_client()
        client._settings.llm_user_agent = "axios/1.7.0"

        assert client._anthropic_headers()["User-Agent"] == "axios/1.7.0"

    def test_custom_user_agent_not_confused_with_urllib_default(self):
        client = _make_client()
        client._settings.llm_user_agent = "axios/1.7.0"

        for headers in (client._openai_headers(), client._anthropic_headers()):
            assert headers["User-Agent"] == "axios/1.7.0"
            assert "Python-urllib" not in headers["User-Agent"]

    def test_empty_user_agent_falls_back_to_plugin_constant(self):
        """Empty setting (legacy in-memory objects) keeps the plugin constant
        as a safety fallback; the proven default covers real configs."""
        client = _make_client()
        client._settings.llm_user_agent = ""

        assert client._user_agent() == llm_client._LLM_USER_AGENT

    def test_fallback_user_agent_version_matches_pyproject(self):
        """The fallback UA embeds the release version, so it must track
        pyproject.toml (the plugin package has no __version__)."""
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10 has no stdlib TOML parser
            match = re.search(
                r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8")
            )
            assert match is not None, "[project].version not found in pyproject.toml"
            version = match.group(1)
        else:
            with pyproject.open("rb") as fh:
                version = tomllib.load(fh)["project"]["version"]
        expected = f"KiCad-AI-Assistant/{version} (+https://github.com/paul356/KiCad-AI-Assistant)"

        assert expected == llm_client._LLM_USER_AGENT


# ---------------------------------------------------------------------------
# Google OpenAI-compatible endpoint (issue #145): detected by BASE URL, not by
# a provider tag. The plugin skips its SSE streaming path and uses
# non-streaming calls + emulated text lifecycle events, because Google's
# stream omits tool_call index AND id, breaking multi-tool aggregation and the
# next-turn history (HTTP 400); the non-streaming response carries both.
# ---------------------------------------------------------------------------


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


class TestGeminiEndpointDetection:
    def test_is_gemini_endpoint_true_for_google_url(self):
        client = _make_client()
        client._settings.llm_base_url = GEMINI_BASE
        assert client._is_gemini_endpoint() is True

    def test_is_gemini_endpoint_false_for_other_urls(self):
        client = _make_client()
        for url in ("", "https://api.openai.com", "https://my-proxy.local/v1"):
            client._settings.llm_base_url = url
            assert client._is_gemini_endpoint() is False, url


class TestGeminiEndpointNonStreaming:
    def test_call_llm_gemini_url_non_streaming_emits_events(self):
        client = _make_client()
        client._settings.llm_provider = "openai"  # provider tag is irrelevant
        client._settings.llm_base_url = GEMINI_BASE
        response = {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hello gemini"},
        }
        events = []
        with (
            patch.object(client, "_call_openai", return_value=response) as mock_openai,
            patch.object(client, "_stream_openai") as mock_stream,
        ):
            result = client._call_llm("sys", [], on_stream_event=events.append)

        mock_openai.assert_called_once_with("sys", [])
        mock_stream.assert_not_called()
        assert events == [
            {"type": "text_start"},
            {"type": "text_chunk", "content": "hello gemini"},
            {"type": "text_end"},
        ]
        assert result["message"]["content"] == "hello gemini"

    def test_call_llm_gemini_tool_calls_kept_intact(self):
        client = _make_client()
        client._settings.llm_base_url = GEMINI_BASE
        tool_calls = [
            {
                "index": 0,
                "id": "tc-gemini-1",
                "type": "function",
                "function": {"name": "extract_schematic_netlist", "arguments": "{}"},
            },
            {
                "index": 1,
                "id": "tc-gemini-2",
                "type": "function",
                "function": {"name": "list_footprints", "arguments": "{}"},
            },
        ]
        response = {
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
        }

        with patch.object(client, "_call_openai", return_value=response):
            result = client._call_llm("sys", [])

        # Non-streaming tool_calls arrive whole: no name concatenation, ids intact.
        assert result["message"]["tool_calls"] == tool_calls
        assert len(result["message"]["tool_calls"]) == 2
        names = [tc["function"]["name"] for tc in result["message"]["tool_calls"]]
        assert names == ["extract_schematic_netlist", "list_footprints"]
        ids = [tc["id"] for tc in result["message"]["tool_calls"]]
        assert all(ids)
        assert len(set(ids)) == 2

        # Emulated event surface with no text content: lifecycle only, no
        # empty text_chunk.
        events = []
        with patch.object(client, "_call_openai", return_value=response):
            client._call_llm("sys", [], on_stream_event=events.append)

        assert events == [{"type": "text_start"}, {"type": "text_end"}]

    def test_non_gemini_url_still_streams(self):
        # URL detection must not change behavior for other OpenAI-compatible
        # endpoints: they keep the real streaming path.
        client = _make_client()
        client._settings.llm_base_url = "https://my-proxy.local/v1"
        with (
            patch.object(client, "_call_openai") as mock_openai,
            patch.object(client, "_stream_openai") as mock_stream,
        ):
            client._call_llm("sys", [], on_stream_event=lambda e: None)

        mock_openai.assert_not_called()
        mock_stream.assert_called_once_with("sys", [], ANY)

    def test_call_openai_uses_configured_google_url(self):
        # No implicit provider-based defaults anymore: the configured base URL
        # is used as-is (chat/completions appended only when path is missing).
        client = _make_client()
        client._settings.llm_base_url = GEMINI_BASE
        client._settings.llm_model = "gemini-2.5-flash"
        response = json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]}
        )

        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post:
            client._call_openai("sys", [])

        url = post.call_args.args[0]
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        payload = json.loads(post.call_args.args[2])
        assert payload["model"] == "gemini-2.5-flash"

        # openai provider without a custom base URL is unchanged.
        client2 = _make_client()
        with patch(
            "kicad_plugin.llm_client._https_post_json", return_value=(200, response)
        ) as post2:
            client2._call_openai("sys", [])

        assert post2.call_args.args[0] == "https://api.openai.com/v1/chat/completions"
        assert json.loads(post2.call_args.args[2])["model"] == "gpt-4o"


class TestHttpsFallback:
    def test_certificate_error_retries_with_plugin_ca_bundle(self):
        certificate_error = ssl.SSLCertVerificationError(
            1, "unable to get local issuer certificate"
        )
        response = MagicMock(status=200)
        response.read.return_value = b"ok"
        response.__enter__.return_value = response
        plugin_context = object()

        with (
            patch.object(llm_client, "_in_process_ssl", None),
            patch(
                "urllib.request.urlopen",
                side_effect=[urllib.error.URLError(certificate_error), response],
            ) as urlopen,
            patch.object(llm_client, "_plugin_ssl_context", return_value=plugin_context),
        ):
            result = llm_client._https_post_json(
                "https://gateway.example/v1/chat/completions",
                {"Content-Type": "application/json"},
                b"{}",
                timeout=30,
            )

        assert result == (200, "ok")
        assert urlopen.call_count == 2
        assert urlopen.call_args_list[1].kwargs["context"] is plugin_context

    def test_certificate_error_without_ca_bundle_uses_plugin_venv(self):
        certificate_error = ssl.SSLCertVerificationError(
            1, "unable to get local issuer certificate"
        )
        process = types.SimpleNamespace(
            returncode=0,
            stdout=b'{"status": 200, "body": "ok"}',
            stderr=b"",
        )

        with (
            patch.object(llm_client, "_in_process_ssl", None),
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError(certificate_error),
            ),
            patch.object(llm_client, "_plugin_ssl_context", return_value=None),
            patch.object(llm_client, "_resolve_plugin_python", return_value="/tmp/python"),
            patch.object(llm_client.subprocess, "run", return_value=process) as run,
        ):
            result = llm_client._https_post_json(
                "https://gateway.example/v1/chat/completions",
                {"Content-Type": "application/json"},
                b"{}",
                timeout=30,
            )

        assert result == (200, "ok")
        run.assert_called_once()

    def test_certificate_error_matches_reason_text(self):
        error = urllib.error.URLError(
            OSError(
                "CERTIFICATE_VERIFY_FAILED certificate verify failed: "
                "unable to get local issuer certificate"
            )
        )

        assert llm_client._is_certificate_verification_error(error) is True


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

    def test_stream_anthropic_always_sends_max_tokens(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        sse_lines = [
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        ]
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp) as m:
            client._stream_anthropic("sys", [], on_stream_event=lambda evt: None)

        payload = json.loads(m.call_args[0][0].data)
        assert payload["max_tokens"] == llm_client._ANTHROPIC_DEFAULT_MAX_TOKENS

    def test_stream_openai_text_only(self):
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=_on_event_collect(chunks))

        assert chunks == ["Hello", " world"]
        assert result["message"]["content"] == "Hello world"
        assert result["finish_reason"] == "stop"

    def test_stream_openai_emits_text_boundary_events(self):
        """text_start precedes the chunks and text_end follows the last one —
        the ordering guarantee the panel consumer relies on (Bug B)."""
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=events.append)

        assert result["message"]["content"] == "Hello world"
        assert [e["type"] for e in events] == [
            "text_start",
            "text_chunk",
            "text_chunk",
            "text_end",
        ]
        assert events[-1]["type"] == "text_end"

    def test_stream_openai_skips_empty_choices_chunk(self):
        """Mid-stream empty choices chunks (usage/keepalive) must not abort
        the parse — tool-call deltas that follow must still be captured."""
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Checking"},"finish_reason":null}]}',
            'data: {"choices":[]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1","function":{"name":"list_tracks","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=_on_event_collect(chunks))

        assert chunks == ["Checking"]
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["function"]["name"] == "list_tracks"
        assert result["finish_reason"] == "tool_calls"

    def test_relay_script_guards_empty_choices_and_tool_use_keys(self):
        """The venv subprocess relay must contain the same guards (it is the
        path used by KiCad's SSL-less embedded Python)."""
        script = llm_client._SUBPROCESS_SSE_SCRIPT
        assert "if not _choices:" in script
        assert 'block.get("id", "")' in script
        assert 'block.get("name", "")' in script

    def test_stream_openai_http_error_includes_body(self):
        """In-process streaming must surface HTTP 4xx with the response body
        (HTTPError is a URLError subclass; it must not be swallowed by the
        HTTPS-fallback branch)."""
        client = _make_client()
        err = urllib.error.HTTPError(
            "http://x",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"code":"InvalidParameter","message":"Request body format invalid"}'),
        )
        with (
            patch.object(llm_client, "_in_process_ssl", None),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            result = client._stream_openai("sys", [], on_stream_event=lambda evt: None)

        assert result["error"] == (
            'HTTP 400: {"code":"InvalidParameter","message":"Request body format invalid"}'
        )

    def test_stream_openai_tool_calls(self):
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1","function":{"name":"add_wire","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"x\\":"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=_on_event_collect(chunks))

        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tc1"
        assert tc[0]["function"]["name"] == "add_wire"
        assert tc[0]["function"]["arguments"] == '{"x":1}'
        assert result["finish_reason"] == "tool_calls"

    def test_stream_openai_tool_calls_without_index(self):
        """Gemini's OpenAI-compatible shim omits the per-delta tool-call
        ``index`` field; the parser must default it instead of KeyError."""
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"tc1","function":{"name":"add_wire","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"x\\":"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"1}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        chunks = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=_on_event_collect(chunks))

        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tc1"
        assert tc[0]["function"]["name"] == "add_wire"
        assert tc[0]["function"]["arguments"] == '{"x":1}'
        assert result["finish_reason"] == "tool_calls"

    def test_stream_openai_gemini_shim_url(self):
        """A base URL that already ends in /openai (Gemini's shim) must
        not gain a spurious /v1/ path segment: .../openai/chat/completions,
        not .../openai/v1/chat/completions."""
        client = _make_client()
        client._settings.llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp) as m:
            client._stream_openai("sys", [], on_stream_event=lambda evt: None)

        url = m.call_args[0][0].full_url
        assert url == ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    def test_call_openai_gemini_shim_url(self):
        """Same endpoint fix for the non-streaming path (_call_openai),
        used by history compaction / non-stream callers."""
        client = _make_client()
        client._settings.llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        with patch(
            "kicad_plugin.llm_client._https_post_json",
            return_value=(200, '{"choices":[{"message":{"content":"hi"}}]}'),
        ) as m:
            result = client._call_openai("sys", [])

        assert result["message"]["content"] == "hi"
        url = m.call_args[0][0]
        assert url == ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    def test_stream_anthropic_http_error_includes_body(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        err = urllib.error.HTTPError(
            "http://x",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"code":"InvalidParameter","message":"Request body format invalid"}'),
        )
        with (
            patch.object(llm_client, "_in_process_ssl", None),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            result = client._stream_anthropic("sys", [], on_stream_event=lambda evt: None)

        assert result["error"] == (
            'HTTP 400: {"code":"InvalidParameter","message":"Request body format invalid"}'
        )

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
            result = client._stream_anthropic("sys", [], on_stream_event=_on_event_collect(chunks))

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
            result = client._stream_anthropic("sys", [], on_stream_event=_on_event_collect(chunks))

        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tu1"
        assert tc[0]["function"]["name"] == "get_netlist"
        args = json.loads(tc[0]["function"]["arguments"])
        assert args == {"path": "sch.kicad_sch"}
        assert result["finish_reason"] == "tool_calls"

    @pytest.mark.parametrize(
        "reason",
        [
            "unknown url type: https",
            ssl.SSLCertVerificationError(1, "unable to get local issuer certificate"),
        ],
    )
    def test_stream_openai_ssl_fallback_streams_via_subprocess(self, reason):
        client = _make_client()
        chunks = []

        subprocess_result = {
            "finish_reason": "stop",
            "message": {"content": "Relayed text", "tool_calls": []},
        }

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError(reason),
            ),
            patch("kicad_plugin.llm_client._in_process_ssl", None),
            patch("kicad_plugin.llm_client._plugin_ssl_context", return_value=None),
            patch(
                "kicad_plugin.llm_client._subprocess_sse_stream",
                return_value=subprocess_result,
            ) as mock_stream,
        ):
            result = client._stream_openai("sys", [], on_stream_event=_on_event_collect(chunks))

        mock_stream.assert_called_once()
        call_kwargs = mock_stream.call_args.kwargs
        assert call_kwargs["fmt"] == "openai"
        assert call_kwargs["timeout"] == 300
        assert result["message"]["content"] == "Relayed text"

    @pytest.mark.parametrize(
        "reason",
        [
            "unknown url type: https",
            ssl.SSLCertVerificationError(1, "unable to get local issuer certificate"),
        ],
    )
    def test_stream_anthropic_ssl_fallback_streams_via_subprocess(self, reason):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        chunks = []

        subprocess_result = {
            "finish_reason": "tool_calls",
            "message": {"content": "", "tool_calls": [{"id": "tu1"}]},
        }

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError(reason),
            ),
            patch("kicad_plugin.llm_client._in_process_ssl", None),
            patch("kicad_plugin.llm_client._plugin_ssl_context", return_value=None),
            patch(
                "kicad_plugin.llm_client._subprocess_sse_stream",
                return_value=subprocess_result,
            ) as mock_stream,
        ):
            result = client._stream_anthropic("sys", [], on_stream_event=_on_event_collect(chunks))

        mock_stream.assert_called_once()
        call_kwargs = mock_stream.call_args.kwargs
        assert call_kwargs["fmt"] == "anthropic"
        assert call_kwargs["timeout"] == 300
        assert result["message"]["tool_calls"] == [{"id": "tu1"}]

    def test_stream_anthropic_emits_text_boundary_events(self):
        client = _make_client()
        client._settings.llm_provider = "anthropic"
        sse_lines = [
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" AI"}}',
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        ]
        events = []
        mock_resp = self._make_sse_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_anthropic("sys", [], on_stream_event=events.append)

        assert result["message"]["content"] == "Hello AI"
        assert [e["type"] for e in events] == [
            "text_start",
            "text_chunk",
            "text_chunk",
            "text_end",
        ]

    def test_run_passes_on_stream_event_to_call_llm(self):
        client = _make_client()
        final_response = {
            "finish_reason": "stop",
            "message": {"content": "done", "tool_calls": []},
        }
        on_event = MagicMock()
        on_failure = MagicMock()
        client._call_llm = MagicMock(return_value=final_response)
        client._fetch_tool_definitions = MagicMock(return_value=[])

        result = client.run(
            "hello", context_block="", on_stream_event=on_event, on_turn_failure=on_failure
        )
        on_failure.assert_not_called()  # a normal turn reports no failure

        assert result == "done"
        # _call_llm must have been called with on_stream_event=on_event
        call_kwargs = client._call_llm.call_args
        assert call_kwargs.kwargs.get("on_stream_event") is on_event


class TestToolCallArgumentsNormalization:
    """Tool-call ``arguments`` must reach the client as JSON strings.

    Ollama's native /api/chat encodes them as JSON objects while OpenAI
    encodes them as serialized strings; both shapes must normalise so the
    downstream ``json.loads(arguments or "{}")`` never receives a dict (#232).
    """

    def _make_line_response(self, lines):
        """Return a mock response object that yields one line per readline()."""
        data = [line.encode("utf-8") + b"\n" for line in lines] + [b""]
        obj = MagicMock()
        obj.readline.side_effect = data
        obj.__enter__ = lambda s: s
        obj.__exit__ = MagicMock(return_value=False)
        return obj

    def _stream_ollama(self, client, lines):
        mock_resp = self._make_line_response(lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            return client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

    def _run_stream_ollama(self, client, lines):
        """Run the streaming path with a fake transport, returning events/body."""
        captured: dict[str, object] = {}
        events: list[dict] = []
        mock_resp = self._make_line_response(lines)

        def _fake_urlopen(req, timeout, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return mock_resp

        with patch("kicad_plugin.llm_client._urlopen", side_effect=_fake_urlopen):
            result = client._stream_ollama("sys", [], on_stream_event=events.append)
        return result, captured["body"], events

    def _run_call_ollama(self, client, body):
        """Run the non-streaming path with a fake transport, returning body."""
        captured: dict[str, object] = {}

        def _fake_post(url, headers, payload, timeout=300):
            captured["body"] = json.loads(payload.decode())
            return 200, json.dumps(body)

        with patch("kicad_plugin.llm_client._https_post_json", side_effect=_fake_post):
            result = client._call_ollama("sys", [])
        return result, captured["body"]

    def test_stream_ollama_object_arguments_become_json_string(self):
        """Issue #232: the tool call that ends an Ollama stream carries a dict."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"name":"get_board_info",'
            '"arguments":{"detail":"full"}}}]},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tc = result["message"]["tool_calls"][0]
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"detail": "full"}
        assert tc["id"] == ""
        # #272: an accumulated tool call reports "tool_calls" even when the
        # provider's done_reason says "stop".  The value coincides with what
        # OpenAI-compatible providers report for tool calls; that path echoes
        # the provider's finish_reason rather than inferring it.
        assert result["finish_reason"] == "tool_calls"

    def test_stream_ollama_string_argument_fragments_still_concatenate(self):
        """The string-delta path (OpenAI-style) must keep concatenating."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"name":"add_wire","arguments":"{\\"a\\":"}}]},"done":false}',
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"arguments":"1}"}}]},"done":false}',
            '{"done":true,"done_reason":"tool_calls"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["message"]["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
        assert result["finish_reason"] == "tool_calls"

    def test_call_ollama_normalizes_object_arguments(self):
        """The non-streaming path returns the raw Ollama message; its tool
        calls must be normalised before the caller parses them."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_board_info", "arguments": {"detail": "full"}}}
                ],
            },
            "done_reason": "stop",
        }
        with patch(
            "kicad_plugin.llm_client._https_post_json",
            return_value=(200, json.dumps(body)),
        ):
            result = client._call_ollama("sys", [])

        assert "error" not in result
        assert result["finish_reason"] == "tool_calls"
        arguments = result["message"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"detail": "full"}

    def test_call_ollama_text_response_keeps_done_reason(self):
        """Without tool calls the provider's reason (e.g. a num_predict
        truncation) must reach the caller unchanged."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {"role": "assistant", "content": "Partial answer"},
            "done_reason": "length",
        }
        with patch(
            "kicad_plugin.llm_client._https_post_json",
            return_value=(200, json.dumps(body)),
        ):
            result = client._call_ollama("sys", [])

        assert "error" not in result
        assert result["finish_reason"] == "length"
        assert result["message"]["content"] == "Partial answer"
        assert "tool_calls" not in result["message"]

    def test_call_openai_normalizes_object_arguments(self):
        """Ollama's OpenAI-compatible endpoint (/v1/chat/completions) also
        returns arguments as an object on the non-streaming path."""
        client = _make_client()
        client._settings.llm_base_url = "http://127.0.0.1:11434/v1"
        body = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {
                                    "name": "get_board_info",
                                    "arguments": {"detail": "full"},
                                },
                            }
                        ],
                    },
                }
            ]
        }
        with patch(
            "kicad_plugin.llm_client._https_post_json",
            return_value=(200, json.dumps(body)),
        ):
            result = client._call_openai("sys", [])

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        arguments = result["message"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"detail": "full"}

    def test_stream_openai_object_arguments_become_json_string(self):
        """Local OpenAI-compatible endpoints (Ollama /v1, LM Studio,
        llama.cpp) can also send arguments as an object."""
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1",'
            '"function":{"name":"get_board_info","arguments":{"detail":"full"}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        mock_resp = self._make_line_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=lambda evt: None)

        assert "error" not in result, result.get("error")
        tc = result["message"]["tool_calls"][0]
        assert tc["id"] == "tc1"
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"detail": "full"}

    def test_stream_ollama_ignores_non_string_content(self):
        """Content blocks (list/dict) from compatible endpoints must not break
        ``"".join`` nor be JSON-pasted into the chat text."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":[{"type":"text","text":"hi"}]},"done":false}',
            '{"message":{"role":"assistant","content":{"text":"x"}},"done":false}',
            '{"message":{"role":"assistant","content":"Plan: check the board."},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]

        # kicad_plugin loggers do not propagate to the root (plugin logging
        # sets propagate=False), so caplog never sees them — attach a capture
        # handler straight to the module logger instead.
        warnings: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: warnings.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger("kicad_plugin.llm_client")
        logger.addHandler(handler)
        try:
            result = self._stream_ollama(client, lines)
        finally:
            logger.removeHandler(handler)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "Plan: check the board."
        assert result["finish_reason"] == "stop"
        assert sum("non-string content" in w for w in warnings) == 2

    def test_stream_ollama_skips_non_dict_message(self):
        """An /api/generate-style or truncated chunk must be skipped."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message": "oops", "done": false}',
            '{"done": true, "done_reason": "stop"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == ""
        assert result["finish_reason"] == "stop"

    # ---- Ollama outbound arguments must be objects -------------------------

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("[1,2]", {}),
            ([1, 2], {}),
            ('"s"', {}),
            ("5", {}),
            ("", {}),
            (None, {}),
            ("{not json", {}),
            ('{"a": 1}', {"a": 1}),
            ({"a": 1}, {"a": 1}),
        ],
    )
    def test_to_ollama_tool_arguments_accepts_only_objects(self, value, expected):
        """Ollama's native API only accepts JSON objects; a JSON array,
        scalar, or junk must collapse to ``{}`` instead of being forwarded."""
        assert llm_client._to_ollama_tool_arguments(value) == expected

    def test_ollama_outbound_list_arguments_become_empty_object(self):
        """A history entry holding ``[1,2]`` must not reach Ollama as a list
        (its API rejects it with the same 400 as a string)."""
        client = _make_client()
        client._history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "list_tracks", "arguments": "[1,2]"},
                    }
                ],
            }
        ]

        outbound = client._ollama_messages()[0]

        assert outbound["tool_calls"][0]["function"]["arguments"] == {}
        assert client._history[0]["tool_calls"][0]["function"]["arguments"] == "[1,2]"

    def test_build_anthropic_messages_list_arguments_become_empty_object(self):
        """Anthropic requires ``input`` to be an object; a JSON array in
        history must fall back to ``{}``."""
        client = _make_client()
        client._history = [
            {"role": "user", "content": "which tracks?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "list_tracks", "arguments": "[1,2]"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "[]"},
        ]

        messages = client._build_anthropic_messages()

        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["content"][0]["input"] == {}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"a": 1}, {"a": 1}),
            ([1, 2], {}),
            (5, {}),
            ("", {}),
            ("{not json", {}),
            ('{"a":1}', {"a": 1}),
            ("[1,2]", {}),
        ],
    )
    def test_anthropic_tool_input_accepts_any_arguments_shape(self, value, expected):
        """Restored session history can hold a non-string ``arguments``
        (``_validate_history`` does not check it); ``input`` must degrade to
        ``{}`` for anything that is not a JSON object, never raise the
        ``TypeError`` json.loads raises for a dict/list/int."""
        assert llm_client._anthropic_tool_input(value) == expected

    # ---- Ollama native API round trip (P1) ---------------------------------

    def test_ollama_outbound_history_arguments_are_objects(self):
        """P1: Ollama's native /api/chat rejects string arguments with
        ``400 Value looks like object, but can't find closing '}' symbol``,
        so a normalized history entry must be decoded back to an object while
        history itself keeps the internal string form."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"name":"get_board_info",'
            '"arguments":{"detail":"full"}}}]},"done":false}',
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        streamed = self._stream_ollama(client, lines)
        assert "error" not in streamed, streamed.get("error")

        history_message = dict(streamed["message"], role="assistant")
        client._history = [{"role": "user", "content": "board?"}, history_message]

        outbound = client._ollama_messages()[-1]
        arguments = outbound["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, dict)
        assert arguments == {"detail": "full"}
        assert outbound["tool_calls"][0]["function"]["name"] == "get_board_info"

        # The internal representation and the caller's object stay untouched.
        assert isinstance(history_message["tool_calls"][0]["function"]["arguments"], str)
        assert client._history[-1] is history_message
        assert isinstance(
            client._history[-1]["tool_calls"][0]["function"]["arguments"],
            str,
        )

    def test_ollama_outbound_empty_or_broken_arguments_become_objects(self):
        """``""`` (no-argument tool call), ``None`` and unparseable strings
        must reach Ollama as ``{}`` instead of raising."""
        client = _make_client()
        client._history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "t1", "arguments": ""}},
                    {"id": "b", "type": "function", "function": {"name": "t2", "arguments": None}},
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "t3", "arguments": "{not json"},
                    },
                ],
            }
        ]

        outbound = client._ollama_messages()[0]

        assert [tc["function"]["arguments"] for tc in outbound["tool_calls"]] == [{}, {}, {}]
        assert client._history[0]["tool_calls"][2]["function"]["arguments"] == "{not json"

    def test_ollama_second_round_request_body_uses_object_arguments(self):
        """P1 end-to-end: the JSON actually POSTed on the follow-up turn must
        carry ``arguments`` as an object."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        client._history = [
            {"role": "user", "content": "board?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "",
                        "type": "function",
                        "function": {
                            "name": "get_board_info",
                            "arguments": '{"detail": "full"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "", "content": "{}"},
        ]
        captured: dict[str, object] = {}
        mock_resp = self._make_line_response(['{"done":true,"done_reason":"stop"}'])

        def _fake_urlopen(req, timeout, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return mock_resp

        with patch("kicad_plugin.llm_client._urlopen", side_effect=_fake_urlopen):
            result = client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

        assert "error" not in result, result.get("error")
        sent = next(m for m in captured["body"]["messages"] if m.get("role") == "assistant")
        assert sent["tool_calls"][0]["function"]["arguments"] == {"detail": "full"}

    def test_stream_ollama_request_leaves_thinking_to_the_server(self):
        """Issue #273: the request must not set ``think``.  Forcing it false
        makes models that support the channel natively fold their reasoning
        into ``content`` (including literal ``<think`` markers)."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        captured: dict[str, object] = {}
        mock_resp = self._make_line_response(['{"done":true,"done_reason":"stop"}'])

        def _fake_urlopen(req, timeout, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return mock_resp

        with patch("kicad_plugin.llm_client._urlopen", side_effect=_fake_urlopen):
            client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

        body = captured["body"]
        assert body["stream"] is True
        assert "think" not in body

    def test_call_ollama_request_leaves_thinking_to_the_server(self):
        """Issue #273: same for the non-streaming request path."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        captured: dict[str, object] = {}

        def _fake_post(url, headers, payload, timeout=300):
            captured["body"] = json.loads(payload.decode())
            return 200, '{"message":{"role":"assistant","content":"hi"}}'

        with patch("kicad_plugin.llm_client._https_post_json", side_effect=_fake_post):
            result = client._call_ollama("sys", [])

        assert "error" not in result, result.get("error")
        body = captured["body"]
        assert body["stream"] is False
        assert "think" not in body

    def test_stream_ollama_uses_thinking_when_content_empty(self):
        """Issue #273: a model that answers only in ``message.thinking`` must
        still produce the reply instead of a blank message."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","thinking":"The board "},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"has 4 layers."},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "The board has 4 layers."
        assert result["finish_reason"] == "stop"

    def test_stream_ollama_prefers_content_over_thinking(self):
        """Issue #273: thinking is a fallback only; real content always wins."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        captured: dict[str, object] = {}
        lines = [
            '{"message":{"role":"assistant","content":"","thinking":"reasoning"},"done":false}',
            '{"message":{"role":"assistant","content":"4","thinking":""},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        mock_resp = self._make_line_response(lines)

        def _fake_urlopen(req, timeout, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return mock_resp

        with patch("kicad_plugin.llm_client._urlopen", side_effect=_fake_urlopen):
            result = client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "4"
        assert "think" not in captured["body"]

    @pytest.mark.parametrize("content", ["", "\n", " \r\n\t"])
    def test_stream_ollama_tool_call_with_whitespace_content_stays_empty(self, content):
        """A tool-call turn normalises whitespace-only content to "" and never
        falls back to the reasoning text."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "thinking": "I should call get_board_info.",
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "get_board_info",
                                    "arguments": {"detail": "full"},
                                },
                            }
                        ],
                    },
                    "done": False,
                }
            ),
            '{"message":{"role":"assistant","content":"","thinking":"get_board_info."},'
            '"done":false}',
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        result, body, events = self._run_stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        assert result["message"]["tool_calls"][0]["function"]["name"] == "get_board_info"
        assert [e for e in events if e["type"] == "text_chunk"] == []
        assert "think" not in body

    def test_call_ollama_uses_thinking_when_content_empty(self):
        """Issue #273: non-streaming reply falls back to ``message.thinking``
        when ``content`` is empty."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "Layer count is 4.",
            },
            "done_reason": "stop",
        }
        with patch(
            "kicad_plugin.llm_client._https_post_json",
            return_value=(200, json.dumps(body)),
        ):
            result = client._call_ollama("sys", [])

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "Layer count is 4."

    def test_call_ollama_prefers_content_over_thinking(self):
        """Issue #273: thinking is ignored when the model produced content."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        captured: dict[str, object] = {}
        body = {
            "message": {
                "role": "assistant",
                "content": "4",
                "thinking": "reasoning",
            },
            "done_reason": "stop",
        }

        def _fake_post(url, headers, payload, timeout=300):
            captured["body"] = json.loads(payload.decode())
            return 200, json.dumps(body)

        with patch("kicad_plugin.llm_client._https_post_json", side_effect=_fake_post):
            result = client._call_ollama("sys", [])

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "4"
        assert "think" not in captured["body"]

    @pytest.mark.parametrize("content", ["", "\n", " \r\n\t"])
    def test_call_ollama_tool_call_keeps_whitespace_content_empty(self, content):
        """Issue #273 P1: a non-streaming tool-call turn must not adopt the
        reasoning text and normalises whitespace-only content to ""."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": content,
                "thinking": "I should call get_board_info.",
                "tool_calls": [
                    {"function": {"name": "get_board_info", "arguments": {"detail": "full"}}}
                ],
            },
            "done_reason": "tool_calls",
        }
        result, sent = self._run_call_ollama(client, body)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        assert result["message"]["tool_calls"][0]["function"]["name"] == "get_board_info"
        assert "think" not in sent

    def test_stream_ollama_thinking_fallback_emits_single_chunk(self):
        """Issue #273 P3: thinking split across chunks (after leading
        whitespace) is emitted as exactly one fallback chunk."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"\\n"},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"The "},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"board "},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"has 4 layers."},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        result, body, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == "The board has 4 layers."
        assert [e for e in events if e["type"] == "text_chunk"] == [
            {"type": "text_chunk", "content": "The board has 4 layers."}
        ]
        assert "think" not in body

    def test_stream_ollama_thinking_only_event_sequence(self):
        """Issue #273 P3: the fallback replaces whitespace-only content and
        yields text_start -> one text_chunk -> text_end."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"\\n"},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"answer"},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        _, body, events = self._run_stream_ollama(client, lines)

        assert events == [
            {"type": "text_start"},
            {"type": "text_chunk", "content": "answer"},
            {"type": "text_end"},
        ]
        assert "think" not in body

    def test_stream_ollama_ignores_non_string_thinking(self):
        """Issue #273 P3: non-string thinking never becomes content; the
        whitespace-only content is still flushed as a single chunk."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"\\n","thinking":null},"done":false}',
            '{"message":{"role":"assistant","content":"  ","thinking":{"text":"no"}},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        result, body, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == "\n  "
        assert [e for e in events if e["type"] == "text_chunk"] == [
            {"type": "text_chunk", "content": "\n  "}
        ]
        assert "think" not in body

    def test_stream_ollama_drops_whitespace_before_thinking_fallback(self):
        """Issue #273 P2: leading whitespace chunks are buffered and dropped
        when the fallback replaces the content."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"\\n"},"done":false}',
            '{"message":{"role":"assistant","content":"  "},"done":false}',
            '{"message":{"role":"assistant","content":"","thinking":"answer"},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        result, body, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == "answer"
        assert [e for e in events if e["type"] == "text_chunk"] == [
            {"type": "text_chunk", "content": "answer"}
        ]
        assert "think" not in body

    def test_stream_ollama_flushes_whitespace_without_fallback(self):
        """Issue #273 P2: whitespace-only content is still streamed when no
        thinking fallback applies, matching the returned content."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"\\n"},"done":false}',
            '{"message":{"role":"assistant","content":"  "},"done":false}',
            '{"done":true,"done_reason":"stop"}',
        ]
        result, body, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == "\n  "
        assert [e for e in events if e["type"] == "text_chunk"] == [
            {"type": "text_chunk", "content": "\n  "}
        ]
        assert "think" not in body

    def test_stream_ollama_whitespace_then_content_matches_message(self):
        """Leading whitespace is folded into the first real chunk and the
        streamed text equals the returned content byte-for-byte."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "\n"}, "done": False}),
            json.dumps({"message": {"role": "assistant", "content": "  "}, "done": False}),
            json.dumps({"message": {"role": "assistant", "content": "answer"}, "done": False}),
            json.dumps({"message": {"role": "assistant", "content": "\nmore"}, "done": False}),
            '{"done":true,"done_reason":"stop"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        chunks = [e["content"] for e in events if e["type"] == "text_chunk"]
        assert chunks == ["\n  answer", "\nmore"]
        assert "".join(chunks) == result["message"]["content"] == "\n  answer\nmore"

    def test_stream_ollama_whitespace_then_tool_call_no_reasoning_events(self):
        """Whitespace before a tool call is dropped and reasoning never reaches
        any event or the content field."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "\n",
                        "thinking": "reason",
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "get_board_info",
                                    "arguments": {"detail": "full"},
                                },
                            }
                        ],
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == ""
        assert [e for e in events if e["type"] == "text_chunk"] == []
        assert all("reason" not in (e.get("content") or "") for e in events)

    def test_stream_ollama_thinking_only_without_callback_no_crash(self):
        """``on_stream_event=None`` returns the same gated content as the
        callback path when ``finish_reason`` is a call-less ``tool_calls``."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "reason",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        with_events, _, _ = self._run_stream_ollama(client, lines)
        mock_resp = self._make_line_response(lines)
        with patch("kicad_plugin.llm_client._urlopen", return_value=mock_resp):
            without_events = client._stream_ollama("sys", [], on_stream_event=None)

        assert with_events["message"]["content"] == ""
        assert without_events["message"]["content"] == ""
        assert without_events["finish_reason"] == "tool_calls"

    def test_stream_ollama_thinking_only_without_callback_returns_thinking(self):
        """``on_stream_event=None`` still applies the thinking fallback on a
        normal (``done_reason="stop"``) reply."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "answer",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"stop"}',
        ]
        mock_resp = self._make_line_response(lines)
        with patch("kicad_plugin.llm_client._urlopen", return_value=mock_resp):
            result = client._stream_ollama("sys", [], on_stream_event=None)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "stop"
        assert result["message"]["content"] == "answer"

    def test_stream_ollama_preserves_thinking_crlf(self):
        """CRLF sequences and blank-line gaps in thinking survive verbatim."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "\n"}, "done": False}),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "line1\r\n",
                    },
                    "done": False,
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "\r\n",
                    },
                    "done": False,
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "line2",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"stop"}',
        ]
        expected = "line1\r\n\r\nline2"
        result, _, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == expected
        assert [e for e in events if e["type"] == "text_chunk"] == [
            {"type": "text_chunk", "content": expected}
        ]

    def test_stream_ollama_non_string_thinking_with_empty_content(self):
        """Non-string thinking is ignored and a call-less ``tool_calls``
        finish reason still blocks the fallback."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {"message": {"role": "assistant", "content": "", "thinking": None}, "done": False}
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": {"text": "no"},
                    },
                    "done": False,
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "actual",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert result["message"]["content"] == ""
        assert [e for e in events if e["type"] == "text_chunk"] == []

    def test_stream_ollama_non_string_thinking_without_tool_gate(self):
        """On a normal reply, non-string thinking is ignored and content stays
        empty without a fallback or crash."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {"message": {"role": "assistant", "content": "", "thinking": None}, "done": False}
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": {"text": "no"},
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"stop"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "stop"
        assert result["message"]["content"] == ""
        assert [e for e in events if e["type"] == "text_chunk"] == []

    def test_stream_ollama_malformed_tool_calls_reason_blocks_fallback(self):
        """A ``tool_calls`` finish reason without any parsed call must not turn
        reasoning into content."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "thinking": "reason",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        assert [e for e in events if e["type"] == "text_chunk"] == []

    def test_call_ollama_malformed_tool_calls_reason_blocks_fallback(self):
        """Non-streaming: same gate when ``done_reason`` claims tool calls with
        no parsable call."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "reason",
            },
            "done_reason": "tool_calls",
        }
        result, _ = self._run_call_ollama(client, body)

        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""

    def test_stream_ollama_malformed_tool_calls_normalises_whitespace(self):
        """A call-less ``tool_calls`` reason still normalises whitespace-only
        content to "" and emits no chunk."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "\n",
                        "thinking": "reason",
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        assert [e for e in events if e["type"] == "text_chunk"] == []

    def test_call_ollama_malformed_tool_calls_normalises_whitespace(self):
        """Non-streaming: a call-less ``tool_calls`` reason normalises
        whitespace-only content to ""."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "\n",
                "thinking": "reason",
            },
            "done_reason": "tool_calls",
        }
        result, _ = self._run_call_ollama(client, body)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""

    @pytest.mark.parametrize("message", [None, "oops"])
    def test_call_ollama_malformed_tool_calls_keeps_non_dict_message(self, message):
        """A non-dict ``message`` must pass through untouched instead of
        raising when ``done_reason`` claims tool calls."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {"message": message, "done_reason": "tool_calls"}
        result, _ = self._run_call_ollama(client, body)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"] == message

    def test_call_ollama_malformed_tool_calls_normalises_dict_message(self):
        """The non-dict guard must not skip whitespace normalization for a
        dict message."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "\n",
                "thinking": "reason",
            },
            "done_reason": "tool_calls",
        }
        result, _ = self._run_call_ollama(client, body)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""

    def test_stream_ollama_tool_call_with_length_reason_stays_empty(self):
        """A parsed call reports ``tool_calls`` even when the server's done
        reason is ``length``; whitespace content is still dropped."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "\n",
                        "thinking": "reason",
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "get_board_info",
                                    "arguments": {"detail": "full"},
                                },
                            }
                        ],
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"length"}',
        ]
        result, _, events = self._run_stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == ""
        assert result["message"]["tool_calls"][0]["function"]["name"] == "get_board_info"
        assert [e for e in events if e["type"] == "text_chunk"] == []
        assert all("reason" not in (e.get("content") or "") for e in events)

    def test_call_ollama_tool_call_with_content_preserves_it(self):
        """Non-streaming tool calls keep real content verbatim while arguments
        are normalised to JSON strings."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        body = {
            "message": {
                "role": "assistant",
                "content": "Board info follows.",
                "thinking": "reason",
                "tool_calls": [
                    {"function": {"name": "get_board_info", "arguments": {"detail": "full"}}}
                ],
            },
            "done_reason": "stop",
        }
        result, _ = self._run_call_ollama(client, body)

        assert "error" not in result, result.get("error")
        assert result["finish_reason"] == "tool_calls"
        assert result["message"]["content"] == "Board info follows."
        arguments = result["message"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"detail": "full"}

    def test_stream_ollama_http_error_includes_body(self):
        """A rejected second-round payload (HTTP 400) must show the provider's
        body, not a bare status code."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        err = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"error":"Value looks like object, but can\'t find closing \'}\' symbol"}'
            ),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

        assert result["error"].startswith("Ollama request failed: HTTP 400:")
        assert "Value looks like object" in result["error"]

    # ---- Remaining covered contracts (P2) ----------------------------------

    def test_stream_openai_relay_serializes_object_arguments(self):
        """P2-1: ``_in_process_ssl`` is sticky, so a loopback Ollama /v1 URL is
        relayed through the subprocess script once SSL failed earlier.  That
        script has its own accumulator and must serialize objects too."""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1",'
            '"function":{"name":"get_board_info","arguments":{"detail":"full"}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        server = _SSETestServer(lines)
        client = _make_client()
        client._settings.llm_base_url = server.url
        try:
            with (
                patch.object(llm_client, "_in_process_ssl", False),
                patch(
                    "kicad_plugin.llm_client._resolve_plugin_python",
                    return_value=sys.executable,
                ),
                patch(
                    "kicad_plugin.llm_client._subprocess_env",
                    return_value={"PATH": os.environ.get("PATH", "")},
                ),
            ):
                result = client._stream_openai("sys", [], on_stream_event=lambda evt: None)
        finally:
            server.shutdown()

        assert "error" not in result, result.get("error")
        tc = result["message"]["tool_calls"][0]
        assert tc["id"] == "tc1"
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"detail": "full"}

    def test_build_anthropic_messages_empty_arguments(self):
        """P2-2: a no-argument tool call normalizes to ``""``; the Anthropic
        history builder must fall back to ``{}`` instead of raising."""
        client = _make_client()
        client._history = [
            {"role": "user", "content": "which tracks?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "list_tracks", "arguments": ""},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "[]"},
        ]

        messages = client._build_anthropic_messages()

        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["content"][0]["type"] == "tool_use"
        assert assistant["content"][0]["input"] == {}

    def test_normalize_tool_calls_copies_instead_of_mutating(self):
        """P2-3: the helper must not rewrite the caller's response body."""
        original = [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "get_board_info", "arguments": {"detail": "full"}},
            }
        ]

        normalized = llm_client._normalize_tool_calls(original)

        assert normalized[0]["function"]["arguments"] == '{"detail": "full"}'
        assert isinstance(original[0]["function"]["arguments"], dict)
        assert original[0]["function"]["arguments"] == {"detail": "full"}
        assert normalized[0] is not original[0]
        assert normalized[0]["function"] is not original[0]["function"]


class TestOutboundReasoningHygiene:
    """Issue #283: stored reasoning text must not reach provider requests."""

    @staticmethod
    def _history():
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is on this board?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJD"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden chain",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "get_board_info", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "{}"},
        ]

    def test_history_round_trip_keeps_reasoning_content(self):
        client = _make_client()
        history = self._history()
        client.set_history(history)

        restored = client.get_history()

        assert restored == history
        assert restored[1]["reasoning_content"] == "hidden chain"

    def test_openai_payload_omits_reasoning_content(self):
        client = _make_client()
        client._history = self._history()

        payload = json.loads(client._openai_payload("sys", [], False))

        assert all("reasoning_content" not in m for m in payload["messages"])
        assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"]["name"] == "get_board_info"
        tool = next(m for m in payload["messages"] if m["role"] == "tool")
        assert tool["tool_call_id"] == "tc1"

    def test_ollama_messages_omit_reasoning_content(self):
        client = _make_client()
        client._history = self._history()

        messages = client._ollama_messages()

        assert all("reasoning_content" not in m for m in messages)
        assert messages[0]["content"] == "What is on this board?"
        assert messages[0]["images"] == ["QUJD"]
        assert messages[1]["tool_calls"][0]["function"]["arguments"] == {}
        assert messages[2]["tool_call_id"] == "tc1"

    def test_call_ollama_body_omits_reasoning_content(self):
        client = _make_client()
        client._settings.llm_provider = "ollama"
        client._history = self._history()
        captured: dict[str, object] = {}

        def _fake_post(url, headers, payload, timeout=300):
            captured["body"] = json.loads(payload.decode())
            return 200, '{"message":{"role":"assistant","content":"ok"}}'

        with patch("kicad_plugin.llm_client._https_post_json", side_effect=_fake_post):
            result = client._call_ollama("sys", [])

        assert "error" not in result, result.get("error")
        messages = captured["body"]["messages"]  # type: ignore[index]
        assert all("reasoning_content" not in m for m in messages)
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"]["arguments"] == {}


class TestOllamaFinalChunk:
    """Issue #272: Ollama may attach the last assistant message to the chunk
    that also carries ``done: true``.  That message must be consumed before
    the read loop exits instead of being dropped along with the chunk."""

    def _make_line_response(self, lines):
        data = [line.encode("utf-8") + b"\n" for line in lines] + [b""]
        obj = MagicMock()
        obj.readline.side_effect = data
        obj.__enter__ = lambda s: s
        obj.__exit__ = MagicMock(return_value=False)
        return obj

    def _stream_ollama(self, client, lines, events=None):
        mock_resp = self._make_line_response(lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            return client._stream_ollama(
                "sys", [], on_stream_event=(events.append if events is not None else None)
            )

    def test_stream_ollama_final_chunk_tool_call_is_kept(self):
        """A complete call in the ``done: true`` chunk must reach the caller."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"id":"call_1","function":{"name":"get_board_info",'
            '"arguments":"{\\"detail\\":\\"full\\"}"}}]},"done":true,'
            '"done_reason":"stop"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["id"] == "call_1"
        assert tcs[0]["function"]["name"] == "get_board_info"
        assert isinstance(tcs[0]["function"]["arguments"], str)
        assert json.loads(tcs[0]["function"]["arguments"]) == {"detail": "full"}
        assert result["finish_reason"] == "tool_calls"

    def test_stream_ollama_final_chunk_object_arguments_are_normalized(self):
        """The #232 object-arguments rule also applies to the final chunk."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        final = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_footprints", "arguments": {"layer": "F.Cu"}}}
                ],
            },
            "done": True,
            "done_reason": "stop",
        }

        result = self._stream_ollama(client, [json.dumps(final)])

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert isinstance(tcs[0]["function"]["arguments"], str)
        assert json.loads(tcs[0]["function"]["arguments"]) == {"layer": "F.Cu"}
        assert result["finish_reason"] == "tool_calls"

    def test_stream_ollama_tool_call_split_across_final_chunk_is_merged(self):
        """Name and arguments arriving in different chunks share one slot."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"name":"add_wire"}}]},"done":false}',
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"function":{"arguments":"{\\"a\\":1}"}}]},"done":true,'
            '"done_reason":"tool_calls"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "add_wire"
        assert tcs[0]["function"]["arguments"] == '{"a":1}'

    def test_stream_ollama_final_chunk_content_and_tool_call(self):
        """Both the text and the call in the final chunk must be kept, and the
        text emitted exactly once."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        events = []
        final = {
            "message": {
                "role": "assistant",
                "content": "Checking the board.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "index": 0,
                            "name": "get_board_info",
                            "arguments": {"detail": "full"},
                        },
                    }
                ],
            },
            "done": True,
            "done_reason": "stop",
        }

        result = self._stream_ollama(client, [json.dumps(final)], events=events)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "Checking the board."
        assert events == [
            {"type": "text_start"},
            {"type": "text_chunk", "content": "Checking the board."},
            {"type": "text_end"},
        ]
        tcs = result["message"]["tool_calls"]
        assert tcs[0]["function"]["name"] == "get_board_info"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"detail": "full"}
        assert result["finish_reason"] == "tool_calls"

    def test_stream_ollama_final_chunk_content_only_survives(self):
        """A final chunk carrying only text must not be lost at the break."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        events = []
        final = {
            "message": {"role": "assistant", "content": "Done: all nets routed."},
            "done": True,
            "done_reason": "stop",
        }

        result = self._stream_ollama(client, [json.dumps(final)], events=events)

        assert "error" not in result, result.get("error")
        assert result["message"]["content"] == "Done: all nets routed."
        assert "tool_calls" not in result["message"]
        assert events == [
            {"type": "text_start"},
            {"type": "text_chunk", "content": "Done: all nets routed."},
            {"type": "text_end"},
        ]
        assert result["finish_reason"] == "stop"

    def test_stream_ollama_eof_without_done_chunk_keeps_calls(self):
        """A truncated stream with no ``done`` chunk still returns the calls
        accumulated so far and reports them."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            '{"message":{"role":"assistant","content":"","tool_calls":'
            '[{"index":0,"id":"call_1","function":{"name":"get_board_info",'
            '"arguments":{"detail":"full"}}}]},"done":false}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "get_board_info"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"detail": "full"}
        assert result["finish_reason"] == "tool_calls"

    @pytest.mark.parametrize("tool_calls", [{}, {"function": {"name": "get_board_info"}}])
    def test_stream_ollama_final_chunk_non_list_tool_calls_is_ignored(self, tool_calls):
        """A malformed ``tool_calls`` object must not raise; the content the
        chunk carried still reaches the caller."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        final = {
            "message": {
                "role": "assistant",
                "content": "Board checked.",
                "tool_calls": tool_calls,
            },
            "done": True,
            "done_reason": "stop",
        }

        result = self._stream_ollama(client, [json.dumps(final)])

        assert "error" not in result, result.get("error")
        assert result["message"] == {"content": "Board checked."}
        assert result["finish_reason"] == "stop"

    @pytest.mark.parametrize(
        "final_chunk",
        [
            {"done": True},
            {"done": True, "message": {}},
            {"done": True, "message": None},
            {"done": True, "message": "oops"},
        ],
    )
    def test_stream_ollama_final_chunk_without_message_is_unchanged(self, final_chunk):
        """Missing, empty, or non-dict messages on the final chunk behave as
        before: empty content, no tool calls, ``stop``."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        events = []

        result = self._stream_ollama(client, [json.dumps(final_chunk)], events=events)

        assert "error" not in result, result.get("error")
        assert result == {"finish_reason": "stop", "message": {"content": ""}}
        assert events == [{"type": "text_start"}, {"type": "text_end"}]


class TestToolCallSlotRouting:
    """Issue #234: a provider may return several tool calls in one message
    with no ``index`` on any of them.  An index is only a merge key; a
    nameless index-less delta is an argument fragment of the previous call."""

    def _make_line_response(self, lines):
        data = [line.encode("utf-8") + b"\n" for line in lines] + [b""]
        obj = MagicMock()
        obj.readline.side_effect = data
        obj.__enter__ = lambda s: s
        obj.__exit__ = MagicMock(return_value=False)
        return obj

    def _stream_ollama(self, client, lines):
        mock_resp = self._make_line_response(lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            return client._stream_ollama("sys", [], on_stream_event=lambda evt: None)

    def test_stream_ollama_indexless_parallel_calls_get_separate_slots(self):
        """Ollama returns the whole tool_calls array in one message and omits
        ``index`` entirely: four calls must not collapse into slot 0."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        chunk = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_board_info", "arguments": {"detail": "full"}}},
                        {"function": {"name": "list_footprints", "arguments": {"layer": "F.Cu"}}},
                        {"function": {"name": "list_tracks", "arguments": {"net": "GND"}}},
                        {
                            "function": {
                                "name": "extract_schematic_netlist",
                                "arguments": {"format": "json"},
                            }
                        },
                    ],
                },
                "done": False,
            }
        )
        lines = [chunk, '{"done":true,"done_reason":"tool_calls"}']

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["function"]["name"] for tc in tcs] == [
            "get_board_info",
            "list_footprints",
            "list_tracks",
            "extract_schematic_netlist",
        ]
        assert [json.loads(tc["function"]["arguments"]) for tc in tcs] == [
            {"detail": "full"},
            {"layer": "F.Cu"},
            {"net": "GND"},
            {"format": "json"},
        ]

    def test_stream_ollama_real_wire_shape_keeps_parallel_calls_apart(self):
        """Replay of an Ollama 0.30.5 capture: one complete call per chunk,
        ``index`` nested in ``function``, ``arguments`` an object."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        chunk_a = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "function": {
                            "index": 0,
                            "name": "get_board_info",
                            "arguments": {"detail": "full"},
                        },
                    }
                ],
            },
            "done": False,
        }
        chunk_b = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_b",
                        "function": {
                            "index": 1,
                            "name": "list_footprints",
                            "arguments": {},
                        },
                    }
                ],
            },
            "done": False,
        }
        lines = [json.dumps(chunk_a), json.dumps(chunk_b), '{"done":true}']

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["id"] for tc in tcs] == ["call_a", "call_b"]
        assert [tc["function"]["name"] for tc in tcs] == ["get_board_info", "list_footprints"]
        assert [json.loads(tc["function"]["arguments"] or "{}") for tc in tcs] == [
            {"detail": "full"},
            {},
        ]

    def test_tool_call_slot_prefers_nested_function_index(self):
        """Ollama nests the provider index in ``function``; a nameless delta
        carrying it must join that call instead of the previously routed
        slot, while a top-level ``index`` keeps its OpenAI behaviour."""
        slots = {0: {"function": {"name": "get_board_info", "arguments": ""}}}

        nested = llm_client._tool_call_slot(
            slots, {"function": {"index": 1, "arguments": '{"a":'}}, 1, 0
        )
        assert nested == (1, 2, 1)

        top_level = llm_client._tool_call_slot(
            slots, {"index": 0, "function": {"arguments": "{}"}}, 1, 1
        )
        assert top_level == (0, 1, 0)

    @pytest.mark.parametrize("provider_index", [1, 5])
    def test_stream_ollama_mixed_indices_keep_wire_order(self, provider_index):
        """A derived slot must be allocated after the highest provider index;
        otherwise ``sorted()`` replays a mixed stream out of order."""
        first_name = "second_on_wire"
        client = _make_client()
        client._settings.llm_provider = "ollama"
        indexed = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "index": provider_index,
                                "name": first_name,
                                "arguments": {"x": 1},
                            }
                        }
                    ],
                },
                "done": False,
            }
        )
        indexless = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "third_on_wire", "arguments": {"x": 2}}}],
                },
                "done": False,
            }
        )
        lines = [indexed, indexless, '{"done":true,"done_reason":"tool_calls"}']

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert [tc["function"]["name"] for tc in result["message"]["tool_calls"]] == [
            first_name,
            "third_on_wire",
        ]

    def test_stream_ollama_derived_slot_stays_above_highest_index(self):
        """``_tool_call_slot`` must advance to the highest index seen, not the
        last one: slots 5 then 1 leave the index-less call in 6, not 2."""
        client = _make_client()
        client._settings.llm_provider = "ollama"

        def _chunk(name, index=None, call_id=""):
            function: dict[str, object] = {"name": name, "arguments": {"x": 1}}
            if index is not None:
                function["index"] = index
            return json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": call_id, "function": function}],
                    },
                    "done": False,
                }
            )

        lines = [
            _chunk("first@5", 5, "a"),
            _chunk("first@1", 1, "b"),
            _chunk("derived@6", None, "c"),
            '{"done":true,"done_reason":"tool_calls"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        assert [tc["function"]["name"] for tc in result["message"]["tool_calls"]] == [
            "first@1",
            "first@5",
            "derived@6",
        ]

    def test_stream_ollama_reversed_nested_indices_keep_provider_order(self):
        """The provider index, not the arrival order, defines the call order —
        a name-based fallback would emit these two chunks reversed."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        chunk_second = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_second",
                            "function": {
                                "index": 1,
                                "name": "second_on_index",
                                "arguments": {"y": 2},
                            },
                        }
                    ],
                },
                "done": False,
            }
        )
        chunk_first = json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_first",
                            "function": {
                                "index": 0,
                                "name": "first_on_index",
                                "arguments": {"x": 1},
                            },
                        }
                    ],
                },
                "done": False,
            }
        )
        lines = [chunk_second, chunk_first, '{"done":true}']

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["id"] for tc in tcs] == ["call_first", "call_second"]
        assert [tc["function"]["name"] for tc in tcs] == [
            "first_on_index",
            "second_on_index",
        ]

    def test_stream_ollama_indexless_argument_fragment_continues_last_slot(self):
        """A nameless index-less delta is a fragment of the call opened by the
        previous chunk — it must not start a new slot."""
        client = _make_client()
        client._settings.llm_provider = "ollama"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "add_wire", "arguments": '{"a":'}}],
                    },
                    "done": False,
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"arguments": "1}"}}],
                    },
                    "done": False,
                }
            ),
            '{"done":true,"done_reason":"tool_calls"}',
        ]

        result = self._stream_ollama(client, lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "add_wire"
        assert tcs[0]["function"]["arguments"] == '{"a":1}'

    def test_stream_openai_indexless_parallel_calls_get_separate_slots(self):
        """The in-process OpenAI-compatible parser has the same contract."""
        client = _make_client()
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"id":"a","function":{"name":"get_board_info","arguments":{}}},'
            '{"id":"b","function":{"name":"list_footprints","arguments":{}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        mock_resp = self._make_line_response(sse_lines)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._stream_openai("sys", [], on_stream_event=lambda evt: None)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["function"]["name"] for tc in tcs] == [
            "get_board_info",
            "list_footprints",
        ]
        assert [tc["id"] for tc in tcs] == ["a", "b"]

    def _stream_openai_via_relay(self, sse_lines):
        server = _SSETestServer(sse_lines)
        client = _make_client()
        client._settings.llm_base_url = server.url
        try:
            with (
                patch.object(llm_client, "_in_process_ssl", False),
                patch(
                    "kicad_plugin.llm_client._resolve_plugin_python",
                    return_value=sys.executable,
                ),
                patch(
                    "kicad_plugin.llm_client._subprocess_env",
                    return_value={"PATH": os.environ.get("PATH", "")},
                ),
            ):
                return client._stream_openai("sys", [], on_stream_event=lambda evt: None)
        finally:
            server.shutdown()

    def test_relay_indexless_parallel_calls_get_separate_slots(self):
        """The subprocess relay script keeps its own accumulator and must
        route index-less calls the same way."""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"id":"a","function":{"name":"get_board_info","arguments":{}}},'
            '{"id":"b","function":{"name":"list_footprints","arguments":{}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]

        result = self._stream_openai_via_relay(sse_lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["function"]["name"] for tc in tcs] == [
            "get_board_info",
            "list_footprints",
        ]

    def test_relay_honours_nested_function_index(self):
        """The relay copy must read Ollama's nested ``function.index`` exactly
        like the in-process parser; the name fallback would emit these two
        chunks in arrival order instead of provider order."""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"id":"second","function":{"index":1,"name":"second_on_index",'
            '"arguments":{"y":2}}},'
            '{"id":"first","function":{"index":0,"name":"first_on_index",'
            '"arguments":{"x":1}}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]

        result = self._stream_openai_via_relay(sse_lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["id"] for tc in tcs] == ["first", "second"]
        assert [tc["function"]["name"] for tc in tcs] == [
            "first_on_index",
            "second_on_index",
        ]

    def test_relay_mixed_indices_keep_wire_order(self):
        """The relay must derive slots above a provider's explicit index;
        otherwise a mixed stream is replayed out of wire order once
        ``sorted()`` orders slots instead of arrival."""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":5,"id":"a",'
            '"function":{"name":"first_wire","arguments":{"x":1}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"b",'
            '"function":{"name":"second_wire","arguments":{"y":2}}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]

        result = self._stream_openai_via_relay(sse_lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert [tc["function"]["name"] for tc in tcs] == ["first_wire", "second_wire"]

    def test_relay_derived_slot_stays_above_highest_index(self):
        """The derived slot must clear the *highest* index seen, not the last
        one: with slots 5 then 1, the index-less call belongs in 6, not 2."""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":5,"id":"a",'
            '"function":{"name":"first@5","arguments":{}}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b",'
            '"function":{"name":"first@1","arguments":{}}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"c",'
            '"function":{"name":"derived@6","arguments":{}}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]

        result = self._stream_openai_via_relay(sse_lines)

        assert "error" not in result, result.get("error")
        assert [tc["function"]["name"] for tc in result["message"]["tool_calls"]] == [
            "first@1",
            "first@5",
            "derived@6",
        ]

    def test_relay_nameless_fragment_follows_negative_index(self):
        """Parity with ``_tool_call_slot``: a negative provider index is a real
        slot, not the relay's old "no slot yet" sentinel, so the fragment
        joins it instead of opening slot 0."""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":-1,"function":'
            '{"name":"neg","arguments":"{\\"x\\":"}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"function":'
            '{"arguments":"1}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]

        result = self._stream_openai_via_relay(sse_lines)

        assert "error" not in result, result.get("error")
        tcs = result["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "neg"
        assert tcs[0]["function"]["arguments"] == '{"x":1}'


class TestSubprocessSSEStream:
    """End-to-end relay: _subprocess_sse_stream runs a real subprocess Python
    against a local HTTP server speaking SSE, verifying deltas, aggregation,
    and error surfacing across the pipe protocol."""

    def _relay(self, lines, status=200, body=b"", fmt="openai"):
        server = _SSETestServer(lines, status=status, body=body)
        chunks = []
        try:
            with (
                patch(
                    "kicad_plugin.llm_client._resolve_plugin_python",
                    return_value=sys.executable,
                ),
                # Keep proxy env vars out of the subprocess so it reaches 127.0.0.1 directly.
                patch(
                    "kicad_plugin.llm_client._subprocess_env",
                    return_value={"PATH": os.environ.get("PATH", "")},
                ),
            ):
                result = _subprocess_sse_stream(
                    url=server.url,
                    headers={"Content-Type": "application/json"},
                    payload=b'{"model":"t","stream":true}',
                    timeout=30,
                    fmt=fmt,
                    on_stream_event=_on_event_collect(chunks),
                )
        finally:
            server.shutdown()
        return result, chunks

    def test_openai_text_deltas_and_aggregation(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        result, chunks = self._relay(lines, fmt="openai")

        assert "error" not in result
        assert chunks == ["Hello", " world"]
        assert result["message"]["content"] == "Hello world"
        assert result["finish_reason"] == "stop"

    def test_openai_tool_calls_and_reasoning_aggregated(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"think "},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"hard"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc1","function":{"name":"add_wire","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"x\\":1}"}}]},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        result, chunks = self._relay(lines, fmt="openai")

        assert "error" not in result
        assert chunks == []
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tc1"
        assert tc[0]["function"]["name"] == "add_wire"
        assert tc[0]["function"]["arguments"] == '{"x":1}'
        assert result["message"]["reasoning_content"] == "think hard"
        assert result["finish_reason"] == "tool_calls"

    def test_anthropic_text_and_tool_use(self):
        lines = [
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            "",
            "event: content_block_start",
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu1","name":"get_netlist","input":{}}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"sch.kicad_sch\\"}"}}',
            "",
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        result, chunks = self._relay(lines, fmt="anthropic")

        assert "error" not in result
        assert chunks == ["Hello"]
        assert result["message"]["content"] == "Hello"
        tc = result["message"]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "tu1"
        assert tc[0]["function"]["name"] == "get_netlist"
        args = json.loads(tc[0]["function"]["arguments"])
        assert args == {"path": "sch.kicad_sch"}
        assert result["finish_reason"] == "tool_calls"

    def test_http_error_surfaces_status_and_body(self):
        result, chunks = self._relay([], status=429, body=b"rate limited", fmt="openai")

        assert chunks == []
        assert result["error"] == "HTTP 429: rate limited"

    def test_delta_callback_error_does_not_abort_stream(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        server = _SSETestServer(lines)
        seen = []

        def boom(evt):
            content = evt.get("content")
            if content:
                seen.append(content)
            raise ValueError("ui hiccup")

        try:
            with (
                patch(
                    "kicad_plugin.llm_client._resolve_plugin_python",
                    return_value=sys.executable,
                ),
                patch(
                    "kicad_plugin.llm_client._subprocess_env",
                    return_value={"PATH": os.environ.get("PATH", "")},
                ),
            ):
                result = _subprocess_sse_stream(
                    url=server.url,
                    headers={"Content-Type": "application/json"},
                    payload=b"{}",
                    timeout=30,
                    fmt="openai",
                    on_stream_event=boom,
                )
        finally:
            server.shutdown()

        assert seen == ["Hello", " world"]
        assert result["message"]["content"] == "Hello world"


# ---------------------------------------------------------------------------
# _prune_rollback_history unit tests
# ---------------------------------------------------------------------------


def _tool_call(id_, name, args):
    return {
        "id": id_,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _assistant_tc(content, tool_calls):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool_result(tcid, content):
    return {"role": "tool", "tool_call_id": tcid, "content": json.dumps(content)}


class TestPruneRollbackHistory:
    """Unit tests for rollback-aware history pruning."""

    # ---- Helpers ---------------------------------------------------------------

    @staticmethod
    def _make_history(blocks):
        """Expand a list of (role, payload) tuples into full messages.

        Tuples:
          ("user", "text or dict")
          ("assistant", text)
          ("assistant+tc", [tool_call, ...])
          ("tool+save", file_path, version_id, tool_call_id)  → save_project_version result
          ("tool+restore", file_path, version_id, tool_call_id) → restore result
          ("tool+misc", tool_call_id, content_dict) → generic tool result
          ("tool", tool_call_id, "content_string")
        """
        history = []
        for b in blocks:
            role = b[0]
            if role == "user":
                history.append({"role": "user", "content": b[1]})
            elif role == "assistant":
                history.append({"role": "assistant", "content": b[1]})
            elif role == "assistant+tc":
                history.append({"role": "assistant", "content": "", "tool_calls": b[1]})
            elif role == "tool+save":
                _, fp, vid, tcid = b
                history.append(
                    _tool_result(tcid, {"version_id": vid, "snapshot_path": f"/tmp/{vid}"})
                )
            elif role == "tool+restore":
                _, fp, vid, tcid = b
                history.append(_tool_result(tcid, {"restored_from": vid}))
            elif role == "tool+misc":
                _, tcid, content = b
                history.append(_tool_result(tcid, content))
            elif role == "tool":
                history.append({"role": "tool", "tool_call_id": b[1], "content": b[2]})
        return history

    # ---- No-op cases ----------------------------------------------------------

    def test_no_restore_no_pruning(self):
        history = self._make_history(
            [
                ("user", "add resistor"),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "tc1", "add_symbol_to_schematic", {"file_path": "proj/main.kicad_sch"}
                        )
                    ],
                ),
                ("tool", "tc1", '{"uuid": "r1"}'),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        assert len(client._history) == 3

    def test_save_not_found_no_pruning(self):
        history = self._make_history(
            [
                ("user", "restore to version we never saved"),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "tc1",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v999"},
                        )
                    ],
                ),
                ("tool+restore", "f.sch", "v999", "tc1"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        assert len(client._history) == 3

    # ---- Basic restore --------------------------------------------------------

    def test_simple_restore_prunes_intermediate_turns(self):
        history = self._make_history(
            [
                ("user", "add resistor"),
                (
                    "assistant+tc",
                    [_tool_call("s1", "save_project_version", {"project_file": "f.kicad_pro"})],
                ),
                ("tool+save", "f.sch", "v1", "s1"),
                # Turn A – touches file
                (
                    "assistant+tc",
                    [_tool_call("tcA", "add_symbol_to_schematic", {"file_path": "f.sch"})],
                ),
                ("tool", "tcA", '{"uuid": "A"}'),
                # Turn B – also touches file
                ("assistant+tc", [_tool_call("tcB", "move_component", {"file_path": "f.sch"})]),
                ("tool", "tcB", '{"ok": true}'),
                # Turn C – restore
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v1"},
                        )
                    ],
                ),
                ("tool+restore", "f.sch", "v1", "rst"),
                ("user", "now do something else"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        roles = [m["role"] for m in client._history]
        assert roles == ["user", "assistant", "tool", "assistant", "tool", "user"]
        # The pruned history should contain: save user+assistant+save_result, restore user+restore_result, final user
        # (empty assistant removed since all its tool_calls pruned)

    def test_non_file_touching_turns_preserved(self):
        history = self._make_history(
            [
                ("user", "start"),
                (
                    "assistant+tc",
                    [_tool_call("s1", "save_project_version", {"project_file": "f.kicad_pro"})],
                ),
                ("tool+save", "f.sch", "v1", "s1"),
                # Turn touches f.sch
                (
                    "assistant+tc",
                    [_tool_call("tcA", "add_symbol_to_schematic", {"file_path": "f.sch"})],
                ),
                ("tool", "tcA", '{"ok": true}'),
                # Turn touches OTHER file
                (
                    "assistant+tc",
                    [_tool_call("tcB", "add_symbol_to_schematic", {"file_path": "other.sch"})],
                ),
                ("tool", "tcB", '{"ok": true}'),
                # Restore f.sch
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v1"},
                        )
                    ],
                ),
                ("tool+restore", "f.sch", "v1", "rst"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # other.sch turn should survive; f.sch turn should be pruned
        # Find the surviving add_symbol_to_schematic assistant
        surviving = [
            m
            for m in client._history
            if m["role"] == "assistant"
            and m.get("tool_calls")
            and any(tc["function"]["name"] == "add_symbol_to_schematic" for tc in m["tool_calls"])
        ]
        assert len(surviving) == 1
        for tc in surviving[0]["tool_calls"]:
            args = json.loads(tc["function"]["arguments"])
            if tc["function"]["name"] == "add_symbol_to_schematic":
                assert args["file_path"] == "other.sch"

    # ---- Partial pruning within a turn ---------------------------------------

    def test_partial_turn_pruning(self):
        history = self._make_history(
            [
                ("user", "start"),
                (
                    "assistant+tc",
                    [_tool_call("s1", "save_project_version", {"project_file": "f.kicad_pro"})],
                ),
                ("tool+save", "f.sch", "v1", "s1"),
                # One turn with two tool_calls: only one touches f.sch
                (
                    "assistant+tc",
                    [
                        _tool_call("tcSchema", "add_symbol_to_schematic", {"file_path": "f.sch"}),
                        _tool_call(
                            "tcOther", "add_symbol_to_schematic", {"file_path": "other.sch"}
                        ),
                    ],
                ),
                ("tool", "tcSchema", '{"uuid": "A"}'),
                ("tool", "tcOther", '{"uuid": "B"}'),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v1"},
                        ),
                    ],
                ),
                ("tool+restore", "f.sch", "v1", "rst"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # The pruned assistant should still have tcOther but NOT tcSchema
        assistants = [
            m for m in client._history if m["role"] == "assistant" and m.get("tool_calls")
        ]
        for a in assistants:
            names = [tc["function"]["name"] for tc in a["tool_calls"]]
            if "add_symbol_to_schematic" in names:
                assert len(a["tool_calls"]) == 1  # only tcOther remains
                assert a["tool_calls"][0]["id"] == "tcOther"

    # ---- Nested restore ------------------------------------------------------

    def test_nested_restore_skipped(self):
        history = self._make_history(
            [
                ("user", "start"),
                # save v1
                (
                    "assistant+tc",
                    [_tool_call("s1", "save_project_version", {"project_file": "f.kicad_pro"})],
                ),
                ("tool+save", "f.sch", "v1", "s1"),
                # Turn T1
                (
                    "assistant+tc",
                    [_tool_call("t1", "add_symbol_to_schematic", {"file_path": "f.sch"})],
                ),
                ("tool", "t1", '{"uuid": "T1"}'),
                # save v2 (after T1)
                (
                    "assistant+tc",
                    [_tool_call("s2", "save_project_version", {"project_file": "f.kicad_pro"})],
                ),
                ("tool+save", "f.sch", "v2", "s2"),
                # Turn T2
                (
                    "assistant+tc",
                    [_tool_call("t2", "add_symbol_to_schematic", {"file_path": "f.sch"})],
                ),
                ("tool", "t2", '{"uuid": "T2"}'),
                # restore v2 (inner)
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst2",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v2"},
                        )
                    ],
                ),
                ("tool+restore", "f.sch", "v2", "rst2"),
                # Turn T3
                (
                    "assistant+tc",
                    [_tool_call("t3", "add_symbol_to_schematic", {"file_path": "f.sch"})],
                ),
                ("tool", "t3", '{"uuid": "T3"}'),
                # restore v1 (outer)
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst1",
                            "restore_project_version",
                            {"project_file": "f.kicad_pro", "version_id": "v1"},
                        )
                    ],
                ),
                ("tool+restore", "f.sch", "v1", "rst1"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # T1, T2, T3 and both restore results should all be pruned
        # Only save results and user messages survive
        for m in client._history:
            if m["role"] == "tool":
                content = json.loads(m["content"])
                assert "version_id" in content or "restored_from" in content
            if m["role"] == "assistant" and m.get("tool_calls"):
                names = [tc["function"]["name"] for tc in m["tool_calls"]]
                assert all(n in ("save_project_version", "restore_project_version") for n in names)

    # ---- Multiple file paths -------------------------------------------------

    def test_restore_only_affects_matching_file(self):
        history = self._make_history(
            [
                ("user", "start"),
                (
                    "assistant+tc",
                    [_tool_call("s1", "save_project_version", {"project_file": "a.kicad_pro"})],
                ),
                ("tool+save", "a.sch", "vA", "s1"),
                (
                    "assistant+tc",
                    [_tool_call("s2", "save_project_version", {"project_file": "b.kicad_pro"})],
                ),
                ("tool+save", "b.sch", "vB", "s2"),
                (
                    "assistant+tc",
                    [_tool_call("tcA", "add_symbol_to_schematic", {"file_path": "a.sch"})],
                ),
                ("tool", "tcA", '{"ok": true}'),
                (
                    "assistant+tc",
                    [_tool_call("tcB", "add_symbol_to_schematic", {"file_path": "b.sch"})],
                ),
                ("tool", "tcB", '{"ok": true}'),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "a.kicad_pro", "version_id": "vA"},
                        )
                    ],
                ),
                ("tool+restore", "a.sch", "vA", "rst"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # b.sch turn should survive
        for m in client._history:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    args = json.loads(tc["function"]["arguments"])
                    if tc["function"]["name"] == "add_symbol_to_schematic":
                        assert args["file_path"] == "b.sch"

    def test_restore_project_prunes_all_project_files(self):
        """Restoring a project prunes turns touching ANY bundled file (sch or PCB).

        The archive bundles the same-stem schematic + PCB + project file, so
        a project restore invalidates tool calls against either file.
        """
        history = self._make_history(
            [
                ("user", "start"),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "s1", "save_project_version", {"project_file": "proj/main.kicad_pro"}
                        )
                    ],
                ),
                ("tool+save", "proj/main.kicad_sch", "v1", "s1"),
                # Schematic change
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "tcS", "add_symbol_to_schematic", {"file_path": "proj/main.kicad_sch"}
                        )
                    ],
                ),
                ("tool", "tcS", '{"uuid": "R1"}'),
                # PCB change – different file
                (
                    "assistant+tc",
                    [_tool_call("tcP", "add_footprint", {"file_path": "proj/main.kicad_pcb"})],
                ),
                ("tool", "tcP", '{"ok": true}'),
                # Restore schematic
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "proj/main.kicad_pro", "version_id": "v1"},
                        )
                    ],
                ),
                ("tool+restore", "proj/main.kicad_sch", "v1", "rst"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # Both the schematic and the PCB turns should be pruned
        for m in client._history:
            if m["role"] == "assistant" and m.get("tool_calls"):
                names = [tc["function"]["name"] for tc in m["tool_calls"]]
                assert "add_symbol_to_schematic" not in names
                assert "add_footprint" not in names

    def test_restore_shares_only_same_full_path(self):
        """Files with same stem but different paths are treated independently."""
        history = self._make_history(
            [
                ("user", "start"),
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "s1",
                            "save_project_version",
                            {"project_file": "proj/sub/leaf.kicad_pro"},
                        )
                    ],
                ),
                ("tool+save", "proj/sub/leaf.sch", "v1", "s1"),
                # Change to leaf.sch in a different directory
                (
                    "assistant+tc",
                    [_tool_call("tc1", "add_symbol_to_schematic", {"file_path": "other/leaf.sch"})],
                ),
                ("tool", "tc1", '{"ok": true}'),
                # Restore proj/sub/leaf.sch
                (
                    "assistant+tc",
                    [
                        _tool_call(
                            "rst",
                            "restore_project_version",
                            {"project_file": "proj/sub/leaf.kicad_pro", "version_id": "v1"},
                        )
                    ],
                ),
                ("tool+restore", "proj/sub/leaf.sch", "v1", "rst"),
            ]
        )
        client = _make_client()
        client._history = history
        client._prune_rollback_history()
        # other/leaf.sch survives – only same full path is pruned
        for m in client._history:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc["function"]["name"] == "add_symbol_to_schematic":
                        args = json.loads(tc["function"]["arguments"])
                        assert args["file_path"] == "other/leaf.sch"


# ---------------------------------------------------------------------------
# _annotate_stale_queries unit tests
# ---------------------------------------------------------------------------

_STALE_MARKER = "⚠️ STALE"


class TestAnnotateStaleQueries:
    """Unit tests for category-aware stale-query annotation."""

    def _build_history(self, *entries):
        """Each entry is (role, tool_call_id?, content, tool_calls_or_args?).

        Shorthands for common patterns:
          ("user", text)
          ("assistant", text)
          ("query", tool_name, file_path, tool_call_id, result_text)
          ("mutation", tool_name, file_path, tool_call_id)
          ("tool", tool_call_id, content)
        """
        history = []
        for e in entries:
            role = e[0]
            if role == "user":
                history.append({"role": "user", "content": e[1]})
            elif role == "assistant":
                history.append({"role": "assistant", "content": e[1]})
            elif role == "query":
                _, name, fp, tcid, result = e
                history.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_tool_call(tcid, name, {"schematic_path": fp})],
                    }
                )
                history.append({"role": "tool", "tool_call_id": tcid, "content": result})
            elif role == "mutation":
                _, name, fp, tcid = e
                history.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_tool_call(tcid, name, {"schematic_path": fp})],
                    }
                )
                history.append({"role": "tool", "tool_call_id": tcid, "content": "{}"})
            elif role == "tool":
                history.append({"role": "tool", "tool_call_id": e[1], "content": e[2]})
        return history

    # ---- No-op ---------------------------------------------------------------

    def test_no_mutation_no_annotation(self):
        history = self._build_history(
            ("query", "check_reference_conflicts", "f.sch", "tc1", '["R1"]'),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        assert _STALE_MARKER not in str(client._history)

    # ---- Basic invalidation --------------------------------------------------

    def test_query_then_mutation_gets_annotated(self):
        history = self._build_history(
            ("query", "check_reference_conflicts", "f.sch", "tc1", '{"conflicts":[]}'),
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc2"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"]
        assert len(tc1_result) == 1
        assert tc1_result[0]["content"].startswith(_STALE_MARKER)

    def test_mutation_then_query_not_annotated(self):
        """Query AFTER mutation is fresh — no annotation."""
        history = self._build_history(
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc1"),
            ("query", "check_reference_conflicts", "f.sch", "tc2", '["R1","R2","R3"]'),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc2_result = [m for m in client._history if m.get("tool_call_id") == "tc2"]
        assert len(tc2_result) == 1
        assert not tc2_result[0]["content"].startswith(_STALE_MARKER)

    # ---- Category-aware ------------------------------------------------------

    def test_non_matching_category_not_annotated(self):
        """add_symbol invalidates 'labels' category, but list_labels is not in that."""
        history = self._build_history(
            ("query", "get_schematic_sheet_info", "f.sch", "tc1", '{"paper":"A4"}'),
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc2"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"]
        assert len(tc1_result) == 1
        assert not tc1_result[0]["content"].startswith(_STALE_MARKER)
        # add_symbol → {symbol_inventory, symbol_properties, symbol_pins, netlist, placement}
        # sheet_meta is NOT in that set → not annotated

    def test_different_file_not_annotated(self):
        history = self._build_history(
            ("query", "check_reference_conflicts", "a.sch", "tc1", '["R1"]'),
            ("mutation", "add_symbol_to_schematic", "b.sch", "tc2"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"]
        assert len(tc1_result) == 1
        assert not tc1_result[0]["content"].startswith(_STALE_MARKER)

    # ---- Mixed scenarios -----------------------------------------------------

    def test_only_matching_queries_annotated(self):
        history = self._build_history(
            ("query", "check_reference_conflicts", "f.sch", "tc1", '["R1"]'),
            ("query", "get_schematic_sheet_info", "f.sch", "tc2", '{"paper":"A4"}'),
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc3"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1 = [m for m in client._history if m.get("tool_call_id") == "tc1"][0]
        tc2_r = [m for m in client._history if m.get("tool_call_id") == "tc2"][0]
        assert tc1["content"].startswith(_STALE_MARKER)  # symbol_inventory
        assert not tc2_r["content"].startswith(_STALE_MARKER)  # sheet_meta

    def test_library_query_never_annotated(self):
        """Library queries (search_library_symbols etc.) have no QUERY_CATEGORY entry."""
        history = self._build_history(
            ("query", "search_library_symbols", "f.sch", "tc1", '["opamp"]'),
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc2"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"]
        assert len(tc1_result) == 1
        assert not tc1_result[0]["content"].startswith(_STALE_MARKER)

    def test_no_double_prefix(self):
        history = self._build_history(
            ("query", "check_reference_conflicts", "f.sch", "tc1", '["R1"]'),
            ("mutation", "add_symbol_to_schematic", "f.sch", "tc2"),
            ("mutation", "remove_symbol_from_schematic", "f.sch", "tc3"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"][0]
        content = tc1_result["content"]
        # Should have exactly one prefix
        assert content.startswith(_STALE_MARKER)
        assert content.count(_STALE_MARKER) == 1

    def test_pcb_query_vs_schematic_mutation_independent(self):
        history = self._build_history(
            ("query", "list_footprints", "proj/pcb.kicad_pcb", "tc1", '["U1"]'),
            ("mutation", "add_symbol_to_schematic", "proj/pcb.kicad_sch", "tc2"),
        )
        client = _make_client()
        client._history = history
        client._annotate_stale_queries()
        tc1_result = [m for m in client._history if m.get("tool_call_id") == "tc1"][0]
        assert not tc1_result["content"].startswith(_STALE_MARKER)


class TestToolDirectRequest:
    """Framework tool_direct requests route through run() but never reach the
    LLM: executed directly, assistant+tool pair recorded in history, request
    itself not stored."""

    REQUEST = {
        "kind": "tool_direct",
        "name": "sync_footprint_index",
        "arguments": {"project_path": "/p"},
    }

    def test_direct_executes_tool_and_records_pair(self):
        client = _make_client()
        calls = []
        with patch(
            "kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}
        ) as mock_call:
            result = client.run(
                self.REQUEST,
                "",
                on_tool_call=lambda name, args, res: calls.append((name, args, res)),
            )
        assert result == {"status": "started"}
        mock_call.assert_called_once()
        # UI callback fired with the tool name / args / result
        assert calls == [("sync_footprint_index", {"project_path": "/p"}, {"status": "started"})]
        # History: synthetic user preamble + assistant/tool pair — no stale
        # user residue for the request itself beyond the preamble.
        assert len(client._history) == 3
        assert client._history[0]["role"] == "user"
        assistant, tool = client._history[1], client._history[2]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["function"]["name"] == "sync_footprint_index"
        assert assistant["tool_calls"][0]["function"]["arguments"] == '{"project_path": "/p"}'
        assert tool["role"] == "tool"
        assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
        assert tool["content"] == '{"status": "started"}'

    def test_direct_appends_to_existing_user_history_without_preamble(self):
        client = _make_client()
        client._history.append({"role": "user", "content": "hi"})
        with patch("kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}):
            client.run(self.REQUEST, "")
        roles = [m["role"] for m in client._history]
        # No extra preamble: existing user message already opens the history
        assert roles == ["user", "assistant", "tool"]

    def test_direct_survives_validate_history(self):
        client = _make_client()
        with patch("kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}):
            client.run(self.REQUEST, "")
        client._validate_history()
        assert len(client._history) == 3

    def test_direct_persisted_via_get_history(self):
        client = _make_client()
        with patch("kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}):
            client.run(self.REQUEST, "")
        snapshot = client.get_history()
        assert len(snapshot) == 3
        assert snapshot[0]["role"] == "user"
        assert snapshot[1]["role"] == "assistant"
        assert snapshot[2]["role"] == "tool"

    def test_direct_failure_records_error_pair_and_fires_callback(self):
        client = _make_client()
        calls = []

        def _boom(base_url, tool_name, args, timeout=None):
            raise RuntimeError("backend down")

        with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=_boom):
            result = client.run(
                self.REQUEST,
                "",
                on_tool_call=lambda name, args, res: calls.append((name, args, res)),
            )
        # Exception captured as a failed result — no propagation
        assert result["success"] is False
        assert "backend down" in result["error"]
        assert calls[0][2]["success"] is False
        # History pair still complete so _validate_history does not strip it
        assert len(client._history) == 3
        client._validate_history()
        assert len(client._history) == 3

    def test_direct_anthropic_conversion_opens_with_user_and_alternates(self):
        client = _make_client()
        with patch("kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}):
            client.run(self.REQUEST, "")
            # next chat request appends a real user message after the pair
            client._history.append({"role": "user", "content": "thanks"})
        messages = client._build_anthropic_messages()
        assert messages[0]["role"] == "user"
        assert [m["role"] for m in messages].count("user") == 2
        assert [m["role"] for m in messages].count("assistant") == 1
        # tool result + following user text merged into one user message
        last = messages[-1]
        assert last["role"] == "user"
        merged_text = "".join(b.get("text", "") for b in last["content"] if b.get("type") == "text")
        assert "thanks" in merged_text

    def test_direct_unknown_kind_still_treated_as_chat(self):
        # A dict without kind == "tool_direct" is not a framework request;
        # run() treats it as ordinary user text content rendering (dict str).
        # The chat path must never hit a real LLM endpoint in tests: stub it.
        client = _make_client()
        client._call_llm = MagicMock(
            return_value={"finish_reason": "stop", "message": {"content": "done"}}
        )
        with patch("kicad_plugin.llm_client.call_mcp_tool", return_value={"status": "started"}):
            reply = client.run({"not": "a request"}, "")
        assert isinstance(reply, str)
        client._call_llm.assert_called_once()


# ---------------------------------------------------------------------------
# On-demand tool loading (issue #129)
# ---------------------------------------------------------------------------


def _fake_tool_def(name, description="does something", schema=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


class TestToolLoading:
    def test_request_tools_meta_only_by_default(self):
        client = _make_client()
        tools = client._build_request_tools()
        assert [t["function"]["name"] for t in tools] == [
            "enable_tool",
            "disable_tool",
            "get_tool_schema",
        ]

    def test_catalog_block_renders_registered_tools(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def(
                "extract_schematic_netlist", "Extract the schematic netlist."
            ),
            "get_board_info": _fake_tool_def("get_board_info", "General board information."),
            "add_zone": _fake_tool_def(
                "add_zone",
                "Add a copper zone to the PCB. Supports thermal relief, clearance tuning.",
            ),
        }
        block = client._build_tool_catalog_block()
        assert "- extract_schematic_netlist: Extract the schematic netlist." in block
        assert "- get_board_info: General board information." in block
        # only the first sentence of the registered description is rendered
        assert "- add_zone: Add a copper zone to the PCB." in block
        assert "thermal relief" not in block
        assert client._build_tool_catalog_block() == block  # deterministic per session

    def test_catalog_uses_first_non_empty_docstring_line(self):
        client = _make_client()
        client._tool_registry = {
            "sync_symbol_index": {
                "type": "function",
                "function": {
                    "name": "sync_symbol_index",
                    "description": (
                        "\nStart building or refreshing the symbol library index. "
                        "The sync runs in a background thread; poll "
                        "get_symbol_sync_status to track progress."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        }
        block = client._build_tool_catalog_block()
        # the blank first line is skipped; the first non-empty line renders
        assert (
            "- sync_symbol_index: Start building or refreshing the symbol library index." in block
        )
        assert "background thread" not in block

    def test_enable_tool_activates_batch_in_registration_order(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist"),
            "get_board_info": _fake_tool_def("get_board_info"),
            "add_zone": _fake_tool_def("add_zone"),
        }
        result = client._execute_meta_tool("enable_tool", {"tools": ["add_zone", "get_board_info"]})
        assert result["success"] is True
        names = [t["function"]["name"] for t in client._build_request_tools()]
        # registration order wins over enable order
        assert names == [
            "enable_tool",
            "disable_tool",
            "get_tool_schema",
            "get_board_info",
            "add_zone",
        ]

    def test_disable_tool_removes_from_request(self):
        client = _make_client()
        client._tool_registry = {
            "get_board_info": _fake_tool_def("get_board_info"),
            "add_zone": _fake_tool_def("add_zone"),
        }
        client._enabled_tools = {"get_board_info", "add_zone"}
        client._execute_meta_tool("disable_tool", {"tools": ["get_board_info"]})
        names = [t["function"]["name"] for t in client._build_request_tools()]
        assert names == ["enable_tool", "disable_tool", "get_tool_schema", "add_zone"]

    def test_enable_tool_unknown_name_atomically_rejected(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist")
        }
        result = client._execute_meta_tool(
            "enable_tool", {"tools": ["extract_schematic_nettlist", "get_board_info"]}
        )
        assert result["success"] is False
        assert "extract_schematic_netlist" in result["suggestions"]["extract_schematic_nettlist"]
        assert client._enabled_tools == set()  # atomic: nothing partially enabled

    def test_enable_tool_batch_without_policy_atomically_rejected(self):
        client = _make_client()
        client._tool_registry = {
            "get_board_info": _fake_tool_def("get_board_info"),
            "unknown_tool": _fake_tool_def("unknown_tool"),
        }
        result = client._execute_meta_tool(
            "enable_tool", {"tools": ["get_board_info", "unknown_tool"]}
        )
        assert result["success"] is False
        assert "no execution policy" in result["error"]
        assert client._enabled_tools == set()

    def test_get_tool_schema_previews_disabled_tool_only(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist")
        }
        preview = client._execute_meta_tool(
            "get_tool_schema", {"tool_name": "extract_schematic_netlist"}
        )
        assert preview["success"] is True
        assert preview["enabled"] is False
        assert client._enabled_tools == set()  # preview does not enable
        client._enabled_tools = {"extract_schematic_netlist"}
        blocked = client._execute_meta_tool(
            "get_tool_schema", {"tool_name": "extract_schematic_netlist"}
        )
        assert blocked["success"] is False
        assert "already enabled" in blocked["error"]

    def test_unknown_schema_suggests_close_matches(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist")
        }
        result = client._execute_meta_tool(
            "get_tool_schema", {"tool_name": "extract_schematic_nettlist"}
        )
        assert result["success"] is False
        assert "extract_schematic_netlist" in result["suggestions"]

    def test_get_tool_schema_batch_previews_multiple(self):
        client = _make_client()
        client._tool_registry = {
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist"),
            "get_board_info": _fake_tool_def("get_board_info"),
        }
        preview = client._execute_meta_tool(
            "get_tool_schema", {"tools": ["extract_schematic_netlist", "get_board_info"]}
        )
        assert preview["success"] is True
        assert set(preview["tools"]) == {"extract_schematic_netlist", "get_board_info"}
        for entry in preview["tools"].values():
            assert entry["enabled"] is False
            assert "schema" in entry and "description" in entry
        assert client._enabled_tools == set()  # preview never enables

    def test_get_tool_schema_batch_skips_already_enabled(self):
        client = _make_client()
        client._tool_registry = {
            "get_board_info": _fake_tool_def("get_board_info"),
            "add_zone": _fake_tool_def("add_zone", description="add a zone"),
        }
        client._enabled_tools = {"add_zone"}
        preview = client._execute_meta_tool(
            "get_tool_schema", {"tools": ["get_board_info", "add_zone"]}
        )
        assert preview["success"] is True
        assert preview["tools"]["get_board_info"]["enabled"] is False
        assert preview["tools"]["add_zone"]["enabled"] is True
        assert "already enabled" in preview["tools"]["add_zone"]["note"].lower()
        assert "skipped" in preview["note"]

    def test_get_tool_schema_batch_unknown_atomically_rejected(self):
        client = _make_client()
        client._tool_registry = {
            "get_board_info": _fake_tool_def("get_board_info"),
            "extract_schematic_netlist": _fake_tool_def("extract_schematic_netlist"),
        }
        result = client._execute_meta_tool(
            "get_tool_schema", {"tools": ["get_board_info", "extract_schematic_nettlist"]}
        )
        assert result["success"] is False  # atomic: no partial preview
        assert result["suggestions"]["extract_schematic_nettlist"] == ["extract_schematic_netlist"]

    def test_get_tool_schema_batch_rejects_bad_args(self):
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        empty = client._execute_meta_tool("get_tool_schema", {"tools": []})
        assert empty["success"] is False
        assert "non-empty" in empty["error"]
        neither = client._execute_meta_tool("get_tool_schema", {})
        assert neither["success"] is False
        assert "tool_name" in neither["error"]

    def test_get_tool_schema_def_describes_batch_preview(self):
        defs = {d["function"]["name"]: d["function"] for d in llm_client._META_TOOL_DEFS}
        gts = defs["get_tool_schema"]
        assert "tools" in gts["parameters"]["properties"]
        assert "NOT enabled" in gts["description"]
        assert "tools" in gts["description"]

    def test_unenabled_catalog_tool_executes_via_policy(self):
        """PR #173 review: enable is not a call gate — unenabled tools run."""
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        state = llm_client._ToolExecutionState()
        with patch(
            "kicad_plugin.llm_client.call_mcp_tool",
            return_value={"success": True, "info": "board data"},
        ) as mock_call:
            result = client._execute_or_reject_tool("get_board_info", {}, state, None)
        assert result["success"] is True
        assert mock_call.call_args.args[1] == "get_board_info"
        assert client._enabled_tools == set()  # ran without ever being enabled

    def test_enabled_tool_still_executes(self):
        """Enabling stays supported and keeps the same execution path."""
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        client._enabled_tools = {"get_board_info"}
        state = llm_client._ToolExecutionState()
        with patch(
            "kicad_plugin.llm_client.call_mcp_tool",
            return_value={"success": True, "info": "board data"},
        ) as mock_call:
            result = client._execute_or_reject_tool("get_board_info", {}, state, None)
        assert result["success"] is True
        assert mock_call.call_args.args[1] == "get_board_info"

    def test_catalogued_tool_without_policy_rejected(self):
        """Safety: a catalogued tool with no execution policy must not run."""
        client = _make_client()
        client._tool_registry = {"mystery_tool": _fake_tool_def("mystery_tool")}
        state = llm_client._ToolExecutionState()
        result = client._execute_or_reject_tool("mystery_tool", {}, state, None)
        assert result["success"] is False
        assert "no execution policy" in result["error"]

    def test_unknown_tool_rejected(self):
        """Hallucinated tool names outside the catalog are refused."""
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        state = llm_client._ToolExecutionState()
        result = client._execute_or_reject_tool("ghost_tool", {}, state, None)
        assert result["success"] is False
        assert "not in the catalog" in result["error"]

    def test_meta_tool_executed_locally(self):
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        state = llm_client._ToolExecutionState()
        result = client._execute_or_reject_tool(
            "enable_tool", {"tools": ["get_board_info"]}, state, None
        )
        assert result["success"] is True

    def test_mid_turn_enable_schema_in_next_request(self):
        """P1: a tool enabled mid-turn must ship its schema in the NEXT request."""
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        enable_resp = {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "enable_tool",
                            "arguments": json.dumps({"tools": ["get_board_info"]}),
                        },
                    }
                ],
            },
        }
        final_resp = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[enable_resp, final_resp])
        with patch("kicad_plugin.llm_client.call_mcp_tool"):
            result = client.run("use get_board_info", context_block="")
        assert result == "done"
        second_tools = client._call_llm.call_args_list[1].args[1]
        assert [t["function"]["name"] for t in second_tools] == [
            "enable_tool",
            "disable_tool",
            "get_tool_schema",
            "get_board_info",
        ]

    def test_catalog_notice_when_registry_unavailable(self):
        """P2: failed tools/list must surface, not run a catalog-less turn."""
        client = _make_client()
        client._tool_registry = None
        client._fetch_tool_definitions = MagicMock(return_value=[])
        block = client._build_tool_catalog_block()
        assert "tool catalog unavailable" in block
        assert "retry next turn" in block

    def test_enable_tool_reports_unavailable_catalog(self):
        client = _make_client()
        client._tool_registry = None
        client._fetch_tool_definitions = MagicMock(return_value=[])
        result = client._execute_meta_tool("enable_tool", {"tools": ["get_board_info"]})
        assert result["success"] is False
        assert "unavailable" in result["error"]

    def test_get_tool_schema_reports_unavailable_catalog(self):
        client = _make_client()
        client._tool_registry = None
        client._fetch_tool_definitions = MagicMock(return_value=[])
        result = client._execute_meta_tool("get_tool_schema", {"tool_name": "get_board_info"})
        assert result["success"] is False
        assert "unavailable" in result["error"]


# ---------------------------------------------------------------------------
# System-prompt wiring for on-demand skill discovery (list_skills/get_skill)
# ---------------------------------------------------------------------------


def _write_skill_file(skills_dir, name, description, priority="50"):
    """Write a skill markdown file with front matter into *skills_dir*."""
    (skills_dir / f"{name}.md").write_text(
        f'---\nname: {name}\npriority: {priority}\ndescription: "{description}"\n---\n'
        f"# {name}\nBody of {name}.\n",
        encoding="utf-8",
    )


class TestSkillPrompt:
    """Prompt must surface the skill category mapping and teach skill tool loading."""

    def test_catalog_block_surfaces_skill_names(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir, "pcb-routing", "Routing workflow guide", "95")
        _write_skill_file(skills_dir, "drc-rules", "DRC rules reference", "45")
        with patch.object(llm_client, "_SKILLS_DIR", skills_dir):
            block = llm_client._build_skill_catalog_block()
        assert "# Skills" in block
        # The per-skill "- name: description" list is part of the prompt again
        # (2026-10: budget relaxed so the prompt is not over-compressed); the
        # category guide and the list must surface the skill names together.
        assert "pcb-routing" in block
        assert "drc-rules" in block
        # Regression guard: at least one full "- name: description" line must
        # render, so the list cannot silently shrink back to the bare mapping.
        assert "- pcb-routing: Routing workflow guide" in block
        assert "- drc-rules: DRC rules reference" in block

    def test_catalog_block_points_to_get_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir, "pcb-routing", "Routing workflow guide")
        with patch.object(llm_client, "_SKILLS_DIR", skills_dir):
            block = llm_client._build_skill_catalog_block()
        assert "list_skills()" in block
        assert "get_skill(name)" in block
        # Review: the catalog says list_skills lists how-to guides for MCP tools.
        assert "how-to guides" in block

    def test_skill_catalog_block_empty_when_no_skills(self, tmp_path):
        with patch.object(llm_client, "_SKILLS_DIR", tmp_path / "missing"):
            assert llm_client._build_skill_catalog_block() == ""

    def test_tool_loading_prompt_covers_skill_tools(self):
        prompt = llm_client._TOOL_LOADING_PROMPT
        assert "list_skills()" in prompt
        assert "get_skill(name)" in prompt
        # Review: enable is optional — all catalog tools are callable as-is.
        assert "callable immediately" in prompt
        assert "optional" in prompt

    def test_catalog_block_maps_task_categories(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir, "pcb-routing", "Routing workflow guide", "95")
        with patch.object(llm_client, "_SKILLS_DIR", skills_dir):
            block = llm_client._build_skill_catalog_block()
        # The block tells the model skills are organized by task category and
        # gives the category -> skill mapping for the common cases.
        assert "organized by task category" in block
        for needle in ("routing→pcb-routing", "placement→pcb-placement", "DRC→drc-rules"):
            assert needle in block, f"catalog missing category mapping {needle!r}"

    def test_tool_loading_prompt_loads_guide_per_category(self):
        prompt = llm_client._TOOL_LOADING_PROMPT
        assert "task's category" in prompt
        assert "get_skill(name)" in prompt
        # Review: tell the model WHY skills matter — workflows/pitfalls that
        # bare parameter schemas cannot convey.
        assert "pitfalls" in prompt
        assert "get_tool_schema" in prompt

    def test_tool_loading_prompt_prefers_schema_preview_over_enable(self):
        prompt = llm_client._TOOL_LOADING_PROMPT
        assert "get_tool_schema(tools=[" in prompt
        assert "WITHOUT enabling" in prompt
        assert "provider cache" in prompt


class TestContextBudgetIncludesTools:
    def test_tools_est_push_over_budget_triggers_compaction(self):
        client = _make_client(context_tokens=10_000)  # budget = 7000
        client._history = [_user("q1"), _assistant("a1"), _user("q2")]
        calls = []
        client._compact_history = lambda _system, _target, _target_history: calls.append(1) or True
        # Without tools this request would be well under budget (~1K tokens).
        client._maybe_compact("x" * 4_000, tools_est_tokens=6_000)
        assert calls

    def test_tools_est_excluded_does_not_compact(self):
        client = _make_client(context_tokens=10_000)
        client._history = [_user("q1"), _assistant("a1"), _user("q2")]
        calls = []
        client._compact_history = lambda _system, _target, _target_history: calls.append(1) or True
        client._maybe_compact("x" * 4_000)
        assert not calls

    def test_fixed_overhead_warning_fires_once(self):
        client = _make_client(context_tokens=2_000)  # budget 1400 < system+meta
        warned = []
        client._maybe_compact("x" * 8_000, on_warning=warned.append)
        client._maybe_compact("x" * 8_000, on_warning=warned.append)
        client._maybe_compact("x" * 8_000, on_warning=warned.append)
        assert len(warned) == 1

    def test_budget_noop_warning_when_nothing_to_compact(self):
        client = _make_client(context_tokens=100_000)  # budget 70_000
        client._history = [_user("q1"), _assistant("a1"), _user("q2")]
        warned = []
        client._maybe_compact("x" * 4_000, on_warning=warned.append, tools_est_tokens=69_000)
        assert len(warned) == 1
        assert "too short to compact" in warned[0]


# ---------------------------------------------------------------------------
# Tool eviction after failed compaction + window-overflow hard fail (#133)
# ---------------------------------------------------------------------------


class TestToolEviction:
    def test_evicts_tail_tools_until_target_reached(self):
        client = _make_client()
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 2_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 2_000),
            "tool_c": _fake_tool_def("tool_c", description="x" * 2_000),
        }
        client._enabled_tools = {"tool_a", "tool_b", "tool_c"}
        # 3 tools x ~500 tokens each; start at 1200 -> evict c (700), then fit.
        evicted = client._evict_tools_to_target(target_tokens=1_000, current_used=1_200)
        assert evicted == ["tool_c"]  # tail of catalog order first
        assert client._enabled_tools == {"tool_a", "tool_b"}

    def test_evicts_all_when_target_unreachable(self):
        client = _make_client()
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 2_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 2_000),
        }
        client._enabled_tools = {"tool_a", "tool_b"}
        evicted = client._evict_tools_to_target(target_tokens=0, current_used=1_200)
        assert evicted == ["tool_b", "tool_a"]  # tail-first order preserved
        assert client._enabled_tools == set()

    def test_meta_tools_never_evicted(self):
        client = _make_client()
        client._tool_registry = {"real_a": _fake_tool_def("real_a", description="x" * 16_000)}
        client._enabled_tools = {"real_a", *llm_client._META_TOOL_NAMES}
        evicted = client._evict_tools_to_target(target_tokens=0, current_used=5_000_000)
        assert evicted == ["real_a"]
        assert client._enabled_tools == set(llm_client._META_TOOL_NAMES)
        names = [t["function"]["name"] for t in client._build_request_tools()]
        assert names == ["enable_tool", "disable_tool", "get_tool_schema"]

    def test_maybe_compact_evicts_first_reaches_target_skips_compaction(self):
        # context 10k -> budget 7k, compaction target 4.9k. History ~1.6k,
        # system 1k, tools 3 x ~2k -> used ~8.9k. Eviction runs FIRST (issue
        # #133) — dropping all three tools lands at ~2.8k, inside the target,
        # so compaction is never attempted.
        client = _make_client(context_tokens=10_000)
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 8_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 8_000),
            "tool_c": _fake_tool_def("tool_c", description="x" * 8_000),
        }
        client._enabled_tools = {"tool_a", "tool_b", "tool_c"}
        client._history = [
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
        ]
        notices = []
        with patch.object(client, "_compact_history") as mock_compact:
            err = client._maybe_compact(
                "x" * 4_000, on_compacted=notices.append, tools_est_tokens=6_337
            )
        assert err is None  # under the real window after eviction
        assert client._enabled_tools == set()  # all 3 evicted
        mock_compact.assert_not_called()  # eviction already reached the target
        # one consolidated notice: action + per-segment sizes + before -> after
        assert len(notices) == 1
        assert "Context compressed for budget" in notices[0]
        assert "tool_a" in notices[0] and "tool_b" in notices[0] and "tool_c" in notices[0]
        assert (
            re.search(
                r"≈\d+ \(system \d+ \+ tools \d+ \+ history \d+\) => ≈\d+ "
                r"\(system \d+ \+ tools \d+ \+ history \d+\) tokens",
                notices[0],
            )
            is not None
        )
        assert "History compacted" not in notices[0]

    def test_maybe_compact_compacts_when_eviction_insufficient(self):
        # History dominates: even with every enabled tool evicted the request
        # stays above the 4.9k target, so compaction runs after eviction.
        client = _make_client(context_tokens=10_000)
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 8_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 8_000),
            "tool_c": _fake_tool_def("tool_c", description="x" * 8_000),
        }
        client._enabled_tools = {"tool_a", "tool_b", "tool_c"}
        client._history = [
            _user("x" * 8_000),
            _assistant("x" * 8_000),
            _user("x" * 8_000),
            _assistant("x" * 8_000),
            _user("x" * 8_000),
            _assistant("x" * 8_000),
            _user("x" * 8_000),
            _assistant("x" * 8_000),
        ]
        with patch.object(client, "_compact_history", return_value=True) as mock_compact:
            err = client._maybe_compact("x" * 4_000, tools_est_tokens=6_337)
        assert err is not None  # mocked compact does not shrink → still over window
        assert client._enabled_tools == set()
        mock_compact.assert_called_once()
        # joint budget: target_history_tokens = target - system - surviving tools
        # (no enabled tools remain, so only the fixed meta-tool overhead counts)
        args = mock_compact.call_args.args
        meta_tokens = len(json.dumps(llm_client._META_TOOL_DEFS)) // 4
        assert args[2] == max(0, int(10_000 * 0.49) - 1_000 - meta_tokens)

    def test_window_overflow_returns_error_message(self):
        # context 2k -> budget 1.4k, target 980. History+system+meta alone
        # (~2.9k) exceed the real window even with every tool evicted.
        client = _make_client(context_tokens=2_000)
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 8_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 8_000),
            "tool_c": _fake_tool_def("tool_c", description="x" * 8_000),
        }
        client._enabled_tools = {"tool_a", "tool_b", "tool_c"}
        client._history = [
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
        ]
        warned = []
        with patch.object(client, "_compact_history", return_value=True):
            err = client._maybe_compact(
                "x" * 4_000, on_warning=warned.append, tools_est_tokens=6_337
            )
        assert err is not None
        assert "Context window overflow" in err
        assert "Not sent" in err
        assert len(warned) == 1
        assert client._enabled_tools == set()

    def test_run_aborts_without_sending_on_overflow(self):
        client = _make_client(context_tokens=2_000)
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a", description="x" * 8_000),
            "tool_b": _fake_tool_def("tool_b", description="x" * 8_000),
            "tool_c": _fake_tool_def("tool_c", description="x" * 8_000),
        }
        client._enabled_tools = {"tool_a", "tool_b", "tool_c"}
        client._history = [
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
            _user("x" * 800),
            _assistant("x" * 800),
        ]
        client._call_llm = MagicMock()
        with patch.object(client, "_compact_history", return_value=True):
            result = client.run("new question", context_block="")
        assert "Context window overflow" in result
        client._call_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Enabled-tool persistence API (issue #136)
# ---------------------------------------------------------------------------


class TestEnabledToolsPersistence:
    def test_get_returns_sorted_stable(self):
        client = _make_client()
        client._enabled_tools = {"zebra", "alpha", "middle"}
        assert client.get_enabled_tools() == ["alpha", "middle", "zebra"]

    def test_set_replaces_enabled_set(self):
        client = _make_client()
        client._enabled_tools = {"old_a", "old_b"}
        client.set_enabled_tools(["new_x", "new_y"])
        assert client._enabled_tools == {"new_x", "new_y"}
        assert client.get_enabled_tools() == ["new_x", "new_y"]

    def test_set_empty_clears(self):
        client = _make_client()
        client._enabled_tools = {"a", "b"}
        client.set_enabled_tools([])
        assert client._enabled_tools == set()

    def test_names_inert_until_registry_loaded(self):
        # Restoring a session may precede the catalog fetch: enabled names for
        # unloaded tools must not appear in the request (or crash) until the
        # registry has them — same semantics as a fresh session.
        client = _make_client()
        client.set_enabled_tools(["future_tool"])
        assert client._build_request_tools() == list(llm_client._META_TOOL_DEFS)
        client._tool_registry = {"future_tool": _fake_tool_def("future_tool")}
        names = [t["function"]["name"] for t in client._build_request_tools()]
        assert names == ["enable_tool", "disable_tool", "get_tool_schema", "future_tool"]

    def test_restored_set_participates_in_budget(self):
        client = _make_client()
        client._tool_registry = {
            "big_tool": _fake_tool_def("big_tool", description="x" * 4_000),
        }
        client.set_enabled_tools(["big_tool"])
        assert client._enabled_tools_est_tokens() > 0

    def test_evicted_tool_absent_from_get(self):
        # #133 eviction is persistent within the session; the persisted set
        # must reflect it so a later save does not resurrect evicted schemas.
        client = _make_client()
        client._tool_registry = {
            "tool_a": _fake_tool_def("tool_a"),
            "tool_b": _fake_tool_def("tool_b"),
        }
        client._enabled_tools = {"tool_a", "tool_b"}
        client._evict_tools_to_target(target_tokens=0, current_used=1_000)
        assert client.get_enabled_tools() == []


# ---------------------------------------------------------------------------
# Regression: set_history must survive (issue #138 — deleted in #137, restore
# paths in panel.py call it; no test covered it, so the break went unnoticed).
# ---------------------------------------------------------------------------


class TestSetHistoryRegression:
    def test_set_history_restores_conversation(self):
        client = _make_client()
        client._history = [{"role": "user", "content": "old"}]
        restored = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        client.set_history(restored)
        assert client._history == restored
        assert client._history is not restored  # defensive copy

    def test_set_history_empty_clears(self):
        client = _make_client()
        client._history = [{"role": "user", "content": "old"}]
        client.set_history([])
        assert client._history == []

    def test_set_history_then_set_enabled_tools_sequence(self):
        # The restore path order: set_history first, then the enabled set.
        # Both must be independently replaceable without cross-talk.
        client = _make_client()
        client.set_history([{"role": "user", "content": "q"}])
        client.set_enabled_tools(["alpha"])
        assert client._history == [{"role": "user", "content": "q"}]
        assert client.get_enabled_tools() == ["alpha"]


# ---------------------------------------------------------------------------
# call_mcp_tool: _image key contract
# ---------------------------------------------------------------------------


class TestCallMcpToolImageKey:
    """call_mcp_tool must carry the PNG only when the server actually
    returned an image block: a result that has no image must not get a
    distracting ``_image: null`` key (the old unconditional injection)."""

    def _mock_call(self, content_blocks):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"result": {"content": content_blocks}}).encode()
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            return llm_client.call_mcp_tool("http://127.0.0.1:9", "some_tool", {})

    def test_text_only_result_has_no_image_key(self):
        out = self._mock_call([{"type": "text", "text": json.dumps({"ok": 1})}])
        assert out == {"ok": 1}
        assert "_image" not in out

    def test_image_block_result_carries_image_key(self):
        out = self._mock_call(
            [
                {"type": "text", "text": json.dumps({"ok": 1})},
                {"type": "image", "mimeType": "image/png", "data": "QUJD"},
            ]
        )
        assert out["ok"] == 1
        assert out["_image"] == {"media_type": "image/png", "data": "QUJD"}

    def test_socket_timeout_returns_failure_not_raise(self):
        """#176: a slow MCP tool call makes ``urlopen`` raise
        ``TimeoutError`` (a.k.a. socket.timeout).  That used to escape
        call_mcp_tool -- the LLM turn was killed mid-reply with no tool
        result recorded.  It must come back as a structured failure so
        the model sees the timeout and keeps the turn alive."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            out = llm_client.call_mcp_tool("http://127.0.0.1:9", "pcb_route_pad_to_pad", {})
        assert out["success"] is False
        assert "timed out" in out["error"]

    def test_unreachable_server_still_returns_failure(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            out = llm_client.call_mcp_tool("http://127.0.0.1:9", "pcb_route_pad_to_pad", {})
        assert out["success"] is False
        assert "unreachable" in out["error"]


# ---------------------------------------------------------------------------
# call_mcp_tool: isError normalization (issue #195)
# ---------------------------------------------------------------------------


class TestCallMcpToolIsError:
    """A FastMCP result with isError: true (e.g. a pydantic validation
    failure) must normalize to success: False with a readable error —
    never the old behavior of wrapping the error prose in success: True."""

    def _mock_call(self, body):
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode()
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            return llm_client.call_mcp_tool("http://127.0.0.1:9", "list_symbol_libraries", {})

    def test_validation_error_is_failure_not_success(self):
        """The observed FastMCP validation-failure shape: isError with a
        prose text block ("Unexpected keyword argument: 'query'")."""
        out = self._mock_call(
            {
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "1 validation error for call[list_symbol_libraries]\n"
                                "query\n  Unexpected keyword argument "
                                "[type=unexpected_keyword_argument, "
                                "input_value='Switch', input_type=str]"
                            ),
                        }
                    ],
                }
            }
        )
        assert out["success"] is False
        assert "Unexpected keyword argument" in out["error"]

    def test_multiple_text_blocks_are_joined(self):
        out = self._mock_call(
            {
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "first failure"},
                        {"type": "text", "text": "second failure"},
                    ],
                }
            }
        )
        assert out["success"] is False
        assert out["error"] == "first failure\nsecond failure"

    def test_structured_content_error_preferred(self):
        out = self._mock_call(
            {
                "result": {
                    "isError": True,
                    "structuredContent": {"error": "boom: bad argument"},
                    "content": [{"type": "text", "text": "ignored prose"}],
                }
            }
        )
        assert out["success"] is False
        assert out["error"] == "boom: bad argument"

    def test_iserror_without_content_has_fallback_error(self):
        out = self._mock_call({"result": {"isError": True}})
        assert out["success"] is False
        assert out["error"]

    def test_success_result_with_iserror_false_unchanged(self):
        out = self._mock_call(
            {
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps({"ok": 1})}],
                }
            }
        )
        assert out == {"ok": 1}

    def test_iserror_wins_over_decodable_json_text(self):
        """A text block that parses as JSON must NOT be returned as
        success when the server flagged isError — the failure wins over
        the success-path json.loads (precedence pin, issue #195)."""
        out = self._mock_call(
            {
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"ok": 1})}],
                }
            }
        )
        assert out["success"] is False
        assert out["error"] == json.dumps({"ok": 1})


# Amazon Bedrock provider: OpenAI-compatible endpoint with bearer (Bedrock
# API key) or AWS SigV4 auth.
# ---------------------------------------------------------------------------


def _make_bedrock_client(**overrides):
    client = _make_client()
    client._settings.llm_provider = "bedrock"
    client._settings.llm_model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    client._settings.llm_api_key = ""
    client._settings.llm_aws_region = "us-east-1"
    client._settings.llm_aws_access_key_id = ""
    client._settings.llm_aws_secret_access_key = ""
    client._settings.llm_aws_session_token = ""
    for k, v in overrides.items():
        setattr(client._settings, k, v)
    return client


class TestAwsSigV4Headers:
    _NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)

    def _sign(self, payload=b'{"a":1}', token="", now=None):
        return llm_client._aws_sigv4_headers(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-opus-5-5/invoke",
            "us-east-1",
            "bedrock",
            "AKIDEXAMPLE",
            "SECRETKEY",
            token,
            payload,
            {"Content-Type": "application/json"},
            now=now or self._NOW,
        )

    def test_authorization_structure(self):
        h = self._sign()
        assert h["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert (
            "Credential=AKIDEXAMPLE/20260102/us-east-1/bedrock/aws4_request" in h["Authorization"]
        )
        # host, content-type and x-amz-date are signed (sorted order)
        assert "SignedHeaders=content-type;host;x-amz-date" in h["Authorization"]
        assert h["X-Amz-Date"] == "20260102T030405Z"
        assert h["Host"] == "bedrock-runtime.us-east-1.amazonaws.com"
        assert "Signature=" in h["Authorization"]

    def test_signing_is_deterministic(self):
        assert self._sign()["Authorization"] == self._sign()["Authorization"]

    def test_different_payload_changes_signature(self):
        assert self._sign(b'{"a":1}')["Authorization"] != self._sign(b'{"a":2}')["Authorization"]

    def test_session_token_signed_and_sent(self):
        h = self._sign(token="SESSION123")
        assert h["X-Amz-Security-Token"] == "SESSION123"
        assert "x-amz-security-token" in h["Authorization"]


class TestBedrockHeaders:
    def test_base_url_from_region(self):
        client = _make_bedrock_client(llm_aws_region="eu-west-1")
        assert client._bedrock_base_url() == "https://bedrock-runtime.eu-west-1.amazonaws.com"

    def test_base_url_defaults_region(self):
        client = _make_bedrock_client(llm_aws_region="")
        assert "us-east-1" in client._bedrock_base_url()

    def test_bearer_when_no_aws_creds(self):
        client = _make_bedrock_client(llm_api_key="bdrk-key")
        headers = client._bedrock_headers("https://x/y", b"{}")
        assert headers["Authorization"] == "Bearer bdrk-key"
        assert "X-Amz-Date" not in headers

    def test_sigv4_when_aws_creds_present(self):
        client = _make_bedrock_client(
            llm_api_key="ignored-when-sigv4",
            llm_aws_access_key_id="AKID",
            llm_aws_secret_access_key="SECRET",
        )
        url = (
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-opus-5-5/invoke"
        )
        headers = client._bedrock_headers(url, b"{}")
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "X-Amz-Date" in headers

    def test_no_auth_header_when_nothing_configured(self):
        client = _make_bedrock_client()
        headers = client._bedrock_headers("https://x/y", b"{}")
        assert "Authorization" not in headers


class TestBedrockDispatch:
    def test_non_streaming_routes_to_call_bedrock(self):
        client = _make_bedrock_client()
        with patch.object(
            client, "_call_bedrock", return_value={"finish_reason": "stop", "message": {}}
        ) as m:
            client._call_llm("system", [])
        m.assert_called_once()

    def test_streaming_routes_to_stream_bedrock(self):
        client = _make_bedrock_client()
        with patch.object(
            client, "_stream_bedrock", return_value={"finish_reason": "stop", "message": {}}
        ) as m:
            client._call_llm("system", [], on_stream_event=lambda e: None)
        m.assert_called_once()

    def test_call_bedrock_posts_to_native_invoke_endpoint(self):
        # Model id with a ':' must be percent-encoded in the URL path.
        client = _make_bedrock_client(
            llm_api_key="bdrk-key",
            llm_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        )
        captured = {}

        def fake_post(url, headers, body, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body)
            # Bedrock returns the Anthropic Messages response shape.
            return 200, json.dumps(
                {"content": [{"type": "text", "text": "hi there"}], "stop_reason": "end_turn"}
            )

        with patch.object(llm_client, "_https_post_json", side_effect=fake_post):
            out = client._call_bedrock("sys", [])

        assert captured["url"] == (
            "https://bedrock-runtime.us-east-1.amazonaws.com"
            "/model/anthropic.claude-3-5-sonnet-20241022-v2%3A0/invoke"
        )
        # Anthropic-style body: anthropic_version present, no top-level model.
        assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"
        assert "model" not in captured["body"]
        assert captured["headers"]["Authorization"] == "Bearer bdrk-key"
        assert out["finish_reason"] == "stop"
        assert out["message"]["content"] == "hi there"

    def test_stream_bedrock_replays_result_through_callback(self):
        client = _make_bedrock_client(llm_api_key="bdrk-key")
        events = []
        with patch.object(
            client,
            "_call_bedrock",
            return_value={"finish_reason": "stop", "message": {"content": "hello world"}},
        ):
            out = client._stream_bedrock("sys", [], events.append)
        types = [e["type"] for e in events]
        assert types == ["text_start", "text_chunk", "text_end"]
        assert events[1]["content"] == "hello world"
        assert out["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Issue #208: harness-side skill auto-injection via frontmatter tools:
# ---------------------------------------------------------------------------


def _write_declared_skill(skills_dir, name, tools, body="Body of the skill."):
    """Write a skill file with a frontmatter ``tools:`` declaration."""
    tools_line = f"tools: [{', '.join(tools)}]\n" if tools else ""
    (skills_dir / f"{name}.md").write_text(
        f'---\nname: {name}\npriority: "50"\ndescription: "{name} guide"\n'
        f"{tools_line}---\n# {name}\n{body}\n",
        encoding="utf-8",
    )


def _guide_messages(history):
    """Return the user-role skill-guide messages injected into *history*."""
    return [
        m
        for m in history
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[Skill guide: ")
    ]


class TestSkillAutoInject:
    """Frontmatter-declared skills must auto-inject into history after the
    first call to one of their tools, exactly once per session (issue #208)."""

    def _tool_response(self, name, tc_id="tc1", args=None):
        return {
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args or {})},
                    }
                ],
            },
        }

    def _client_with_placement_tool(self, tmp_path):
        """Client whose registry exposes set_footprint_position and whose
        skills dir declares it for the pcb-placement skill.

        Returns ``(client, skills_dir)``; the caller wraps its run in
        ``with patch.object(llm_client, "_SKILLS_DIR", skills_dir):``.
        """
        client = _make_client()
        client._tool_registry = {
            "set_footprint_position": _fake_tool_def("set_footprint_position"),
            "align_footprints": _fake_tool_def("align_footprints"),
        }
        client._enabled_tools = {"set_footprint_position"}
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_declared_skill(
            skills_dir,
            "pcb-placement",
            ["set_footprint_position", "align_footprints"],
            body="Place bodies must not overlap.",
        )
        return client, skills_dir

    def _ok_server(self, *_args, **_kwargs):
        return {"success": True}

    def test_first_declared_tool_call_injects_skill_body(self, tmp_path):
        """Red-green core: after a call to a declared tool, the skill body is
        present in history as a user-role message positioned AFTER that
        batch's tool results."""
        client, skills_dir = self._client_with_placement_tool(tmp_path)
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response(
                    "set_footprint_position",
                    args={
                        "pcb_path": "/tmp/board.kicad_pcb",
                        "items": [{"reference": "R1", "x": 1.0}],
                    },
                ),
                {"finish_reason": "stop", "message": {"content": "done"}},
            ]
        )
        with (
            patch.object(llm_client, "_SKILLS_DIR", skills_dir),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=self._ok_server),
        ):
            result = client.run("move R1", context_block="")

        assert result == "done"
        history = client.get_history()
        guides = _guide_messages(history)
        assert len(guides) == 1
        assert "[Skill guide: pcb-placement]" in guides[0]["content"]
        assert "Place bodies must not overlap." in guides[0]["content"]
        # Positioned after the batch's tool-results message, not between the
        # assistant tool_calls message and its results.
        tool_idx = next(i for i, m in enumerate(history) if m["role"] == "tool")
        guide_idx = next(i for i, m in enumerate(history) if m is guides[0])
        assert guide_idx > tool_idx
        assert client._injected_skills == {"pcb-placement"}

    def test_repeat_trigger_injects_nothing(self, tmp_path):
        """A second call to a declared tool in the same session is a pure
        no-op: no body, no 'already loaded' note, no extra mutation."""
        client, skills_dir = self._client_with_placement_tool(tmp_path)
        args = {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]}
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response("set_footprint_position", tc_id="tc1", args=args),
                self._tool_response("set_footprint_position", tc_id="tc2", args=args),
                {"finish_reason": "stop", "message": {"content": "done"}},
            ]
        )
        with (
            patch.object(llm_client, "_SKILLS_DIR", skills_dir),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=self._ok_server),
        ):
            result = client.run("move R1 again", context_block="")

        assert result == "done"
        assert len(_guide_messages(client.get_history())) == 1
        assert client._injected_skills == {"pcb-placement"}

    def test_skill_without_tools_never_injected(self, tmp_path):
        """Skills without a frontmatter ``tools:`` field are never
        auto-injected; get_skill remains their only load path."""
        client = _make_client()
        client._tool_registry = {"get_board_info": _fake_tool_def("get_board_info")}
        client._enabled_tools = {"get_board_info"}
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_skill_file(skills_dir, "plain-skill", "Plain skill without tools")
        with patch.object(llm_client, "_SKILLS_DIR", skills_dir):
            client._call_llm = MagicMock(
                side_effect=[
                    self._tool_response("get_board_info", args={"pcb_path": "/tmp/b.kicad_pcb"}),
                    {"finish_reason": "stop", "message": {"content": "ok"}},
                ]
            )
            with patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=self._ok_server):
                result = client.run("inspect board", context_block="")

        assert result == "ok"
        assert _guide_messages(client.get_history()) == []
        assert client._injected_skills == set()

    def test_declared_tool_missing_from_registry_warns_not_crashes(self, tmp_path):
        """A frontmatter-declared tool absent from the MCP registry yields a
        warning at map load and is tolerated — no crash, no dropped entries."""
        client = _make_client()
        client._tool_registry = {"real_tool": _fake_tool_def("real_tool")}
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_declared_skill(skills_dir, "ghost-skill", ["real_tool", "ghost_tool"])

        # kicad_plugin loggers do not propagate to the root (plugin logging
        # sets propagate=False), so caplog never sees them — attach a capture
        # handler straight to the module logger instead.
        warnings: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: warnings.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger("kicad_plugin.llm_client")
        logger.addHandler(handler)
        try:
            with patch.object(llm_client, "_SKILLS_DIR", skills_dir):
                mapping = client._get_skill_tool_map()
        finally:
            logger.removeHandler(handler)

        assert any("ghost_tool" in w and "ghost-skill" in w for w in warnings)
        assert mapping["ghost_tool"].name == "ghost-skill"

    def test_system_prompt_byte_identical_before_after_injection(self, tmp_path):
        """Injection must never touch the system prompt (cache stability)."""
        client, skills_dir = self._client_with_placement_tool(tmp_path)
        args = {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]}
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response("set_footprint_position", tc_id="tc1", args=args),
                self._tool_response("set_footprint_position", tc_id="tc2", args=args),
                {"finish_reason": "stop", "message": {"content": "done"}},
            ]
        )
        with (
            patch.object(llm_client, "_SKILLS_DIR", skills_dir),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=self._ok_server),
        ):
            client.run("move R1", context_block="")

        systems = [call.args[0] for call in client._call_llm.call_args_list]
        assert len(systems) == 3
        assert systems[0] == systems[1] == systems[2]

    def test_no_user_message_between_assistant_and_tool_results(self, tmp_path):
        """Provider pairing stays intact: every assistant tool_calls message
        is immediately followed by its role:'tool' results, even when a skill
        guide was injected after the previous batch."""
        client, skills_dir = self._client_with_placement_tool(tmp_path)
        args = {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]}
        client._call_llm = MagicMock(
            side_effect=[
                self._tool_response("set_footprint_position", tc_id="tc1", args=args),
                self._tool_response("set_footprint_position", tc_id="tc2", args=args),
                {"finish_reason": "stop", "message": {"content": "done"}},
            ]
        )
        with (
            patch.object(llm_client, "_SKILLS_DIR", skills_dir),
            patch("kicad_plugin.llm_client.call_mcp_tool", side_effect=self._ok_server),
        ):
            result = client.run("move R1", context_block="")

        assert result == "done"
        history = client.get_history()
        for i, msg in enumerate(history):
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            # The very next message must be a tool result (no user message
            # may sit between the assistant tool_calls and its results).
            assert history[i + 1].get("role") == "tool"
            expected = {tc["id"] for tc in msg["tool_calls"]}
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                expected.discard(history[j].get("tool_call_id"))
                j += 1
            assert not expected, f"orphaned tool_call_ids in history: {expected}"


class TestSkillFrontMatterToolsParsing:
    """Frontmatter ``tools:`` list syntax parsing (issue #208)."""

    def test_bracket_list(self):
        text = "---\nname: x\ntools: [set_footprint_position, align_footprints]\n---\nbody"
        meta = llm_client._parse_skill_front_matter(text)
        assert meta["tools"] == ["set_footprint_position", "align_footprints"]

    def test_comma_separated(self):
        text = "---\nname: x\ntools: a, b, c\n---\nbody"
        meta = llm_client._parse_skill_front_matter(text)
        assert meta["tools"] == ["a", "b", "c"]

    def test_bracket_no_spaces(self):
        text = "---\nname: x\ntools: [a,b,c]\n---\nbody"
        meta = llm_client._parse_skill_front_matter(text)
        assert meta["tools"] == ["a", "b", "c"]

    def test_empty_tools_field(self):
        text = "---\nname: x\ntools:\n---\nbody"
        meta = llm_client._parse_skill_front_matter(text)
        assert meta["tools"] == []

    def test_missing_tools_field(self):
        text = '---\nname: x\npriority: 50\ndescription: "d"\n---\nbody'
        meta = llm_client._parse_skill_front_matter(text)
        assert "tools" not in meta


# ---------------------------------------------------------------------------
# Loopback proxy bypass (issue #230, defect A)
# ---------------------------------------------------------------------------


class TestLoopbackProxyBypass:
    """``_urlopen`` must route loopback destinations direct only when the
    system proxy would otherwise intercept them, and must leave every other
    path byte-for-byte unchanged."""

    @pytest.fixture(autouse=True)
    def _reset_loopback_opener(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_loopback_opener", None)

    @staticmethod
    def _request(url: str):
        return urllib.request.Request(url, data=b"{}", method="POST")

    def _use_fake_loopback_opener(self, monkeypatch):
        """Install a fake cached opener and return ``(opener, calls)``."""
        calls: list[tuple[object, float | None]] = []

        class _Opener:
            def open(self, request, timeout=None):
                calls.append((request, timeout))
                resp = MagicMock(status=200)
                resp.read.return_value = b'{"ok": true}'
                resp.__enter__.return_value = resp
                return resp

        opener = _Opener()
        monkeypatch.setattr(llm_client, "_loopback_opener", opener)
        return opener, calls

    @pytest.mark.parametrize(
        "proxies",
        [
            {"http": "http://proxy.example:8118"},
            {"https": "http://proxy.example:8118"},
            {"http": "http://proxy.example:8118", "https": "http://proxy.example:8118"},
        ],
    )
    def test_loopback_with_proxy_uses_direct_opener(self, monkeypatch, proxies):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
        opener, calls = self._use_fake_loopback_opener(monkeypatch)
        req = self._request("http://127.0.0.1:8043/mcp")

        with patch("urllib.request.urlopen", side_effect=AssertionError("system opener used")):
            out = llm_client._urlopen(req, 7)

        assert out.read() == b'{"ok": true}'
        assert calls == [(req, 7)]

    def test_external_with_proxy_uses_default_opener(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        _, loopback_calls = self._use_fake_loopback_opener(monkeypatch)
        req = self._request("https://api.deepseek.com/v1/chat/completions")
        sentinel = object()

        with patch("urllib.request.urlopen", return_value=sentinel) as system_urlopen:
            out = llm_client._urlopen(req, 9)

        assert out is sentinel
        system_urlopen.assert_called_once_with(req, timeout=9)
        # The loopback opener must never be used for outbound traffic.
        assert loopback_calls == []

    def test_loopback_without_proxy_keeps_default_opener(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
        _, loopback_calls = self._use_fake_loopback_opener(monkeypatch)
        req = self._request("http://127.0.0.1:8043/mcp")
        sentinel = object()

        with patch("urllib.request.urlopen", return_value=sentinel) as system_urlopen:
            out = llm_client._urlopen(req, 5)

        assert out is sentinel
        system_urlopen.assert_called_once_with(req, timeout=5)
        assert loopback_calls == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/mcp",
            "http://localhost:11434/api/chat",
            "https://LOCALHOST:443/mcp",
            "http://127.0.0.1:8043/mcp",
            "http://127.1.2.3:99/mcp",
            "http://[::1]:8080/mcp",
            "http://0.0.0.0:1234/mcp",
            "http://localhost./mcp",
            "https://LOCALHOST.:443/mcp",
            "http://127.1:8043/mcp",
            "http://127.0.1:8043/mcp",
            "http://[::ffff:127.0.0.1]:8043/mcp",
        ],
    )
    def test_is_loopback_url_accepts_loopback_hosts(self, url):
        assert llm_client._is_loopback_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.evil.com/mcp",
            "http://127.0.0.1.evil.com/mcp",
            "http://127.1.evil.com/mcp",
            "http://localhost.evil.com/mcp",
            "http://2130706433/mcp",
            "http://1.2.3/mcp",
            "http://10.0.0.1/mcp",
            "http://192.168.71.5:8118/",
            "https://example.com/mcp",
            "https://api.deepseek.com/v1/chat/completions",
            "not a url",
            "",
        ],
    )
    def test_is_loopback_url_rejects_non_loopback_hosts(self, url):
        assert llm_client._is_loopback_url(url) is False

    def test_host_with_embedded_nul_is_rejected_without_raising(self):
        """``inet_aton`` raises ValueError (not OSError) on a NUL byte."""
        assert llm_client._is_loopback_url("http://127.0.0.1\x00evil.com/") is False

    def test_proxy_would_intercept_requires_both_conditions(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy"})
        assert llm_client._proxy_would_intercept("http://127.0.0.1:1/mcp") is True
        assert llm_client._proxy_would_intercept("https://example.com/") is False

        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
        assert llm_client._proxy_would_intercept("http://127.0.0.1:1/mcp") is False

    @pytest.mark.parametrize(
        "proxies",
        [
            {"HTTP": "http://proxy.example:8118"},
            {"HTTPS": "http://proxy.example:8118"},
            {"Http": "http://proxy.example:8118", "hTTps": "http://proxy.example:8118"},
        ],
    )
    def test_proxy_keys_are_matched_case_insensitively(self, monkeypatch, proxies):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
        assert llm_client._proxy_would_intercept("http://127.0.0.1:1/mcp") is True

    def test_loopback_opener_is_built_once_and_cached(self, monkeypatch):
        built: list[tuple[object, ...]] = []
        sentinel = object()

        def _build(*handlers):
            built.append(handlers)
            return sentinel

        monkeypatch.setattr(urllib.request, "build_opener", _build)

        assert llm_client._get_loopback_opener() is sentinel
        assert llm_client._get_loopback_opener() is sentinel
        assert len(built) == 1
        assert isinstance(built[0][0], urllib.request.ProxyHandler)
        assert built[0][0].proxies == {}

    def test_loopback_opener_build_is_thread_safe(self, monkeypatch):
        import concurrent.futures

        builds = 0
        sentinel = object()

        def _slow_build(*handlers):
            nonlocal builds
            builds += 1
            time.sleep(0.01)
            return sentinel

        monkeypatch.setattr(urllib.request, "build_opener", _slow_build)

        def _get():
            return llm_client._get_loopback_opener()

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: _get(), range(64)))

        assert builds == 1
        assert all(result is sentinel for result in results)

    def test_non_loopback_ca_retry_path_is_still_used(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        self._use_fake_loopback_opener(monkeypatch)
        req = self._request("https://gateway.example/v1/chat/completions")
        sentinel = object()

        with (
            patch.object(
                llm_client, "_urlopen_with_plugin_ca_retry", return_value=sentinel
            ) as ca_retry,
            patch("urllib.request.urlopen", side_effect=AssertionError("default opener used")),
        ):
            out = llm_client._urlopen(req, 30, ca_retry=True)

        assert out is sentinel
        ca_retry.assert_called_once_with(req, 30)

    def test_loopback_with_proxy_skips_ca_retry(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        _, calls = self._use_fake_loopback_opener(monkeypatch)
        req = self._request("http://127.0.0.1:8043/mcp")

        with (
            patch.object(
                llm_client,
                "_urlopen_with_plugin_ca_retry",
                side_effect=AssertionError("CA retry must not run for loopback"),
            ),
            patch("urllib.request.urlopen", side_effect=AssertionError("system opener used")),
        ):
            llm_client._urlopen(req, 10, ca_retry=True)

        assert [call[0] for call in calls] == [req]

    def test_loopback_without_proxy_keeps_ca_retry_path(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
        self._use_fake_loopback_opener(monkeypatch)
        req = self._request("http://127.0.0.1:8043/mcp")
        sentinel = object()

        with (
            patch.object(
                llm_client, "_urlopen_with_plugin_ca_retry", return_value=sentinel
            ) as ca_retry,
            patch("urllib.request.urlopen", side_effect=AssertionError("default opener used")),
        ):
            out = llm_client._urlopen(req, 10, ca_retry=True)

        assert out is sentinel
        ca_retry.assert_called_once_with(req, 10)

    def test_https_post_json_loopback_bypasses_proxy(self, monkeypatch):
        """A local OpenAI-compatible endpoint goes direct, not via the proxy."""
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        _, calls = self._use_fake_loopback_opener(monkeypatch)
        monkeypatch.setattr(llm_client, "_in_process_ssl", None)

        with patch("urllib.request.urlopen", side_effect=AssertionError("system opener used")):
            status, text = llm_client._https_post_json(
                "http://127.0.0.1:1234/v1/chat/completions",
                {"Content-Type": "application/json"},
                b'{"model": "local"}',
                timeout=30,
            )

        assert (status, text) == (200, '{"ok": true}')
        assert [call[0].full_url for call in calls] == ["http://127.0.0.1:1234/v1/chat/completions"]

    def test_stream_openai_request_loopback_bypasses_proxy(self, monkeypatch):
        """The streaming path must also reach a local endpoint directly."""
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        opened: list[str] = []

        class _StreamingOpener:
            def open(self, request, timeout=None):
                opened.append(request.full_url)
                resp = MagicMock()
                resp.readline.side_effect = [
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
                    b"data: [DONE]\n",
                ]
                resp.__enter__.return_value = resp
                return resp

        monkeypatch.setattr(llm_client, "_loopback_opener", _StreamingOpener())
        monkeypatch.setattr(llm_client, "_in_process_ssl", None)
        events: list[dict] = []

        with patch("urllib.request.urlopen", side_effect=AssertionError("system opener used")):
            out = LLMClient._stream_openai_request(
                None,
                "http://127.0.0.1:1234/v1/chat/completions",
                {"Content-Type": "application/json"},
                b"{}",
                events.append,
            )

        assert out["finish_reason"] == "stop"
        assert out["message"]["content"] == "hi"
        assert {"type": "text_chunk", "content": "hi"} in events
        assert opened == ["http://127.0.0.1:1234/v1/chat/completions"]

    def test_stream_anthropic_loopback_bypasses_proxy(self, monkeypatch):
        """The Anthropic streaming path routes loopback endpoints directly."""
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        opened: list[str] = []

        class _StreamingOpener:
            def open(self, request, timeout=None):
                opened.append(request.full_url)
                resp = MagicMock()
                resp.readline.side_effect = [b""]
                resp.__enter__.return_value = resp
                return resp

        monkeypatch.setattr(llm_client, "_loopback_opener", _StreamingOpener())
        monkeypatch.setattr(llm_client, "_in_process_ssl", None)

        calls: list[tuple[str, float, bool]] = []
        real_urlopen = llm_client._urlopen

        def _spy_urlopen(request, timeout, *, ca_retry=False):
            calls.append((request.full_url, timeout, ca_retry))
            return real_urlopen(request, timeout, ca_retry=ca_retry)

        monkeypatch.setattr(llm_client, "_urlopen", _spy_urlopen)
        client = _make_client()
        client._settings.llm_base_url = "http://127.0.0.1:1234"

        with patch("urllib.request.urlopen", side_effect=AssertionError("system opener used")):
            out = client._stream_anthropic("sys", [], None)

        assert "error" not in out
        assert out["finish_reason"] == "stop"
        assert calls == [("http://127.0.0.1:1234/v1/messages", 300, True)]
        assert opened == ["http://127.0.0.1:1234/v1/messages"]

    def test_https_post_json_non_loopback_keeps_ca_retry(self, monkeypatch):
        """Outbound LLM calls keep the existing plugin-CA retry path."""
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:8118"})
        _, loopback_calls = self._use_fake_loopback_opener(monkeypatch)
        monkeypatch.setattr(llm_client, "_in_process_ssl", None)
        response = MagicMock(status=200)
        response.read.return_value = b"ok"
        response.__enter__.return_value = response

        with patch.object(
            llm_client, "_urlopen_with_plugin_ca_retry", return_value=response
        ) as ca_retry:
            status, text = llm_client._https_post_json(
                "https://gateway.example/v1/chat/completions",
                {"Content-Type": "application/json"},
                b"{}",
                timeout=30,
            )

        assert (status, text) == (200, "ok")
        ca_retry.assert_called_once()
        assert loopback_calls == []


class _RecordingProxyServer(ThreadingHTTPServer):
    """A local HTTP proxy stub that records absolute-URI and CONNECT requests.

    ``do_CONNECT`` is implemented deliberately: HTTPS through a proxy needs
    the CONNECT tunnel, and without it the client only sees
    ``501 Unsupported method ('CONNECT')`` — a gap in the stub, not a
    statement about the caller.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self._records_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), self._make_handler(), bind_and_activate=True)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def record(self, method: str, path: str) -> None:
        with self._records_lock:
            self.requests.append((method, path))

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, body: bytes = b"proxied") -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.record("GET", self.path)
                self._answer()

            def do_POST(self):
                server.record("POST", self.path)
                self._answer()

            def do_CONNECT(self):
                server.record("CONNECT", self.path)
                self.send_response(200)
                self.end_headers()

        return Handler


class _LoopbackTargetServer(ThreadingHTTPServer):
    """A direct-only local endpoint that answers MCP-shaped POSTs."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), self._make_handler(), bind_and_activate=True)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def _make_handler(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


class TestProxyEndToEnd:
    """Acceptance check for issue #230: a real fake proxy observes zero
    loopback requests and exactly one external request."""

    @pytest.fixture()
    def proxy_and_target(self, monkeypatch):
        proxy = _RecordingProxyServer()
        target = _LoopbackTargetServer()
        monkeypatch.setattr(llm_client, "_loopback_opener", None)
        # urllib caches its default opener process-wide; earlier tests run
        # with getproxies() stubbed to {}, so force a rebuild under this
        # test's proxy environment.
        monkeypatch.setattr(urllib.request, "_opener", None)
        monkeypatch.setenv("http_proxy", proxy.url)
        monkeypatch.setenv("https_proxy", proxy.url)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # The autouse fixture stubs getproxies out; use the real env reader
        # so this test exercises the production decision end to end.
        monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
        try:
            yield proxy, target
        finally:
            proxy.shutdown()
            proxy.server_close()
            target.shutdown()
            target.server_close()

    def test_loopback_bypasses_proxy_and_external_still_uses_it(self, proxy_and_target):
        proxy, target = proxy_and_target

        req = urllib.request.Request(
            f"http://127.0.0.1:{target.port}/mcp",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with llm_client._urlopen(req, 5) as resp:
            assert resp.read() == b'{"ok": true}'
        assert proxy.requests == []  # loopback never reached the proxy

        external = urllib.request.Request("http://external.invalid/", method="GET")
        with llm_client._urlopen(external, 5) as resp:
            assert resp.read() == b"proxied"
        assert proxy.requests == [("GET", "http://external.invalid/")]

        https = urllib.request.Request("https://external.invalid/", method="GET")
        with pytest.raises(urllib.error.URLError):
            llm_client._urlopen(https, 5)
        assert ("CONNECT", "external.invalid:443") in proxy.requests

        loopback_hits = [r for r in proxy.requests if "127.0.0.1" in r[1]]
        assert loopback_hits == []

    def test_loopback_llm_endpoint_never_reaches_proxy(self, proxy_and_target, monkeypatch):
        proxy, target = proxy_and_target
        monkeypatch.setattr(llm_client, "_in_process_ssl", None)

        status, text = llm_client._https_post_json(
            f"http://127.0.0.1:{target.port}/v1/chat/completions",
            {"Content-Type": "application/json"},
            b'{"model": "local"}',
            timeout=5,
        )

        assert status == 200
        assert text == '{"ok": true}'
        assert proxy.requests == []
