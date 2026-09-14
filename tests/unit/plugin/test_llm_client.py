"""Tests for LLMClient history management (dedup + compaction)."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import re
import ssl
import sys
import threading
import types
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from kicad_plugin import llm_client
from kicad_plugin.llm_client import LLMClient, _subprocess_sse_stream
from kicad_plugin.tool_registry import TOOL_POLICIES


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
        llm_model="gpt-4o",
        llm_base_url="",
        llm_context_tokens=context_tokens,
        llm_compact_threshold=compact_threshold,
        llm_compact_target_threshold=compact_target,
        llm_keep_recent_turns=keep_recent_turns,
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
        # Budget admits exactly the newest recent turns (each ~25 tokens): they
        # stay verbatim, the older ones are folded into the prefix.
        client = _make_client(keep_recent_turns=4)
        client._history = []
        for i in range(4):
            client._history.append(_user(f"small q {i}"))
            client._assistant_append = None
            client._history.append(_assistant(f"small a {i}"))
        for i in range(4):
            client._history.append(_user(f"recent q {i}"))
            client._history.append(_assistant("recent a " + "x" * 80))  # ~25 tok each
        original_rows = [m["content"] for m in client._history]

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        # recent 4 turns ≈ 164 tokens + summary ceiling 50 -> budget 220 admits all
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=220
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
            client._history.append(_assistant("recent a " + "x" * 80))  # ~25 tok each
        original_rows = [m["content"] for m in client._history]

        summary_response = {
            "finish_reason": "stop",
            "message": {"content": "Summary of older context."},
        }
        # budget 140: summary ceiling 50 -> split_tokens 90 admits exactly the
        # newest 2 turns (2 x 41), a 3rd (123) would overrun
        with patch.object(client, "_call_openai", return_value=summary_response):
            result = client._compact_history(
                "system", target_summary_chars=200, target_history_tokens=140
            )

        assert result is True
        # summary + last 2 recent turns (2 x 2 rows = 4 messages)
        assert len(client._history) == 1 + 4
        preserved_rows = [m["content"] for m in client._history[1:]]
        assert preserved_rows == original_rows[-4:]

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
        # Verify target_summary_chars was passed as a positive int
        _, kwargs = (
            mock_compact.call_args
            if mock_compact.call_args.kwargs
            else (mock_compact.call_args.args, {})
        )
        called_args = mock_compact.call_args.args
        assert called_args[1] >= 200  # at least the minimum floor

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
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "save_project_version",
                    {"project_file": "/tmp/board.kicad_pro"},
                ),
            ),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
                ),
            ),
            ((client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),),
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
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "save_project_version",
                    {"project_file": "/tmp/board.kicad_pro"},
                ),
            ),
            (
                (
                    client._mcp_base_url,
                    "pcb_route_pad_to_pad",
                    {"pcb_path": "/tmp/board.kicad_pcb", "pad1": "A1", "pad2": "B2"},
                ),
            ),
            ((client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),),
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

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("move R1", context_block="")

        assert result == "[LLM error] API down"
        assert mock_call_tool.call_args_list[-1] == (
            (client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),
        )

    def test_max_iterations_exit_still_reloads_dirty_path(self):
        """Hitting the iteration cap still flushes dirty paths via the shared
        end-of-turn reload."""
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

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = lambda base, name, args: (
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []}
                if name == "reload_kicad"
                else {"success": True}
            )
            result = client.run("move R1", context_block="")

        assert (
            result == "[Error] Maximum tool-call iterations reached. Please try a simpler request."
        )
        assert mock_call_tool.call_args_list[-1][0][1] == "reload_kicad"
        assert mock_call_tool.call_args_list[-1][0][2] == {"paths": ["/tmp/board.kicad_pcb"]}

    def test_tool_exception_still_reloads_dirty_path(self):
        """An exception escaping run() still flushes dirty paths from earlier
        successful mutations in the same turn."""
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
        client._call_llm = MagicMock(side_effect=[tool_response])
        client._enabled_tools = {"set_footprint_position", "flip_footprint"}

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True},  # save_document (snapshot for set_footprint_position)
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                # flip_footprint reuses the same-turn snapshot (no save calls)
                RuntimeError("boom"),  # flip_footprint raises
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            with pytest.raises(RuntimeError, match="boom"):
                client.run("edit board", context_block="")

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
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "save_project_version",
                    {"project_file": "/tmp/board.kicad_pro"},
                ),
            ),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
                ),
            ),
            (
                (
                    client._mcp_base_url,
                    "flip_footprint",
                    {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R2"},
                ),
            ),
            ((client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),),
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
        assert mock_call_tool.call_args_list == [
            (
                (
                    client._mcp_base_url,
                    "save_project_version",
                    {"project_file": "/tmp/board.kicad_pro"},
                ),
            ),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "items": [{"reference": "R1", "x": 1.0}]},
                ),
            ),
            ((client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),),
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
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "save_project_version",
                    {"project_file": "/tmp/board.kicad_pro"},
                ),
            ),
        ]
        tool_result = json.loads(client._history[-2]["content"])
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
        }

    def test_empty_api_key_omits_authorization(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        assert client._openai_headers() == {"Content-Type": "application/json"}

    def test_api_key_adds_x_api_key(self):
        client = _make_client()

        assert client._anthropic_headers() == {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "sk-test",
        }

    def test_empty_api_key_omits_x_api_key(self):
        client = _make_client()
        client._settings.llm_api_key = ""

        assert client._anthropic_headers() == {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }


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
        client._call_llm = MagicMock(return_value=final_response)
        client._fetch_tool_definitions = MagicMock(return_value=[])

        result = client.run("hello", context_block="", on_stream_event=on_event)

        assert result == "done"
        # _call_llm must have been called with on_stream_event=on_event
        call_kwargs = client._call_llm.call_args
        assert call_kwargs.kwargs.get("on_stream_event") is on_event


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
        """Library queries (search_symbols etc.) have no QUERY_CATEGORY entry."""
        history = self._build_history(
            ("query", "search_symbols", "f.sch", "tc1", '["opamp"]'),
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

        def _boom(base_url, tool_name, args):
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

    def test_unenabled_real_tool_rejected_without_network(self):
        client = _make_client()
        client._tool_registry = {"extract_netlist": _fake_tool_def("extract_netlist")}
        state = llm_client._ToolExecutionState()
        result = client._execute_or_reject_tool("extract_netlist", {}, state, None)
        assert result["success"] is False
        assert "not enabled" in result["error"]

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
        trimmed = [n for n in notices if "Tool set trimmed" in n]
        assert len(trimmed) == 1
        assert "tool_a" in trimmed[0] and "tool_b" in trimmed[0] and "tool_c" in trimmed[0]
        # notice reports the post-eviction used token estimate
        assert re.search(r"used ≈\d+ tokens", trimmed[0]) is not None
        assert not any("History compacted" in n for n in notices)

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
