"""Tests for LLMClient history management (dedup + compaction)."""

import json
import types
from unittest.mock import MagicMock, patch

from kicad_plugin.llm_client import LLMClient
from kicad_plugin.tool_registry import TOOL_POLICIES


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

    def test_dedup_always_called(self):
        client = _make_client()
        client._history = [_user("hi"), _assistant("hello")]
        with (
            patch.object(client, "_dedup_tool_calls") as mock_dedup,
            patch.object(client, "_compact_history"),
        ):
            client._maybe_compact("system")
        mock_dedup.assert_called_once()


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

        with patch.object(client, "_maybe_compact") as mock_compact:
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
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0}
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._fetch_tool_definitions = MagicMock(
            return_value=[{"function": {"name": "set_footprint_position"}}]
        )
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
            ((client._mcp_base_url, "save_file_version", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0},
                ),
            ),
            ((client._mcp_base_url, "reload_kicad", {"paths": ["/tmp/board.kicad_pcb"]}),),
        ]
        assert [call.args[0] for call in on_tool_call.call_args_list] == [
            "save_document",
            "save_file_version",
            "set_footprint_position",
            "reload_kicad",
        ]

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
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0}
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
        client._fetch_tool_definitions = MagicMock(
            return_value=[
                {"function": {"name": "set_footprint_position"}},
                {"function": {"name": "flip_footprint"}},
            ]
        )

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
            ((client._mcp_base_url, "save_file_version", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0},
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

    def test_explicit_save_file_version_is_reused_by_later_mutation(self):
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
                            "name": "save_file_version",
                            "arguments": json.dumps({"file_path": "/tmp/board.kicad_pcb"}),
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
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0}
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[save_response, mutate_response, final_response])
        client._fetch_tool_definitions = MagicMock(
            return_value=[
                {"function": {"name": "save_file_version"}},
                {"function": {"name": "set_footprint_position"}},
            ]
        )

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.side_effect = [
                {"success": True, "version_id": "v1"},
                {"success": True, "pcb_path": "/tmp/board.kicad_pcb"},
                {"success": True, "reloaded": ["/tmp/board.kicad_pcb"], "failed": []},
            ]
            result = client.run("edit board", context_block="")

        assert result == "done"
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_file_version", {"file_path": "/tmp/board.kicad_pcb"}),),
            (
                (
                    client._mcp_base_url,
                    "set_footprint_position",
                    {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0},
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
                                {"pcb_path": "/tmp/board.kicad_pcb", "reference": "R1", "x": 1.0}
                            ),
                        },
                    }
                ],
            },
        }
        final_response = {"finish_reason": "stop", "message": {"content": "done"}}
        client._call_llm = MagicMock(side_effect=[tool_response, final_response])
        client._fetch_tool_definitions = MagicMock(
            return_value=[{"function": {"name": "set_footprint_position"}}]
        )

        with patch("kicad_plugin.llm_client.call_mcp_tool") as mock_call_tool:
            mock_call_tool.return_value = {"success": False, "error": "disk full"}
            result = client.run("move R1", context_block="")

        assert result == "done"
        assert mock_call_tool.call_args_list == [
            ((client._mcp_base_url, "save_document", {"file_path": "/tmp/board.kicad_pcb"}),),
            ((client._mcp_base_url, "save_file_version", {"file_path": "/tmp/board.kicad_pcb"}),),
        ]
        tool_result = json.loads(client._history[-2]["content"])
        assert tool_result["success"] is False
        assert "Failed to save file version before set_footprint_position" in tool_result["error"]

    def test_run_reports_missing_tool_policy_before_calling_llm(self):
        client = _make_client()
        client._fetch_tool_definitions = MagicMock(
            return_value=[{"function": {"name": "unknown_tool"}}]
        )
        client._call_llm = MagicMock()

        result = client.run("hello", context_block="")

        assert (
            result == "[Framework error] Tool policy registry is missing entries for: unknown_tool"
        )
        client._call_llm.assert_not_called()


class TestToolPolicyRegistry:
    def test_registry_covers_plugin_tool_surface(self):
        expected_tools = {
            "extract_project_netlist",
            "extract_schematic_netlist",
            "find_component_connections",
            "sync_symbol_index",
            "get_symbol_sync_status",
            "search_symbols",
            "get_symbol",
            "list_symbol_libraries",
            "get_library_symbols",
            "get_symbol_index_stats",
            "get_symbol_pins",
            "add_symbol_to_schematic",
            "place_symbol_relative",
            "remove_symbol_from_schematic",
            "set_component_property",
            "list_component_properties",
            "delete_component_property",
            "move_component",
            "add_label_to_schematic",
            "list_labels_in_schematic",
            "delete_label_from_schematic",
            "connect_points_with_wire",
            "connect_pins_with_wire",
            "delete_wire_from_schematic",
            "sync_footprint_index",
            "get_footprint_sync_status",
            "list_footprint_libraries",
            "search_footprints",
            "get_footprint_details",
            "get_board_info",
            "list_footprints",
            "get_footprint",
            "list_nets",
            "get_ratsnest",
            "score_placement",
            "suggest_placement_order",
            "set_footprint_position",
            "flip_footprint",
            "set_footprint_property",
            "get_board_outline",
            "clear_board_outline",
            "add_board_outline_segment",
            "add_board_outline_arc",
            "set_board_outline_rect",
            "get_footprint_bbox",
            "get_board_bounding_box",
            "align_footprints",
            "distribute_footprints",
            "move_footprints_by_delta",
            "find_free_pcb_area",
            "get_schematic_sheet_info",
            "find_free_area",
            "assign_to_group",
            "list_groups",
            "get_group",
            "score_group",
            "place_component_group",
            "move_group",
            "rotate_group",
            "list_zones",
            "add_zone",
            "delete_zone",
            "update_pcb_from_schematic",
            "reload_kicad",
            "save_file_version",
            "list_file_versions",
            "restore_file_version",
            "save_document",
            "check_kicad_ipc_connection",
            "refill_zones",
        }

        assert set(TOOL_POLICIES) == expected_tools


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
            "data: [DONE]",
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
            "data: [DONE]",
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

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("unknown url type: https"),
            ),
            patch.object(client, "_call_openai", return_value=fallback_result) as mock_call,
        ):
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
