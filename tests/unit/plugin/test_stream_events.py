"""Unit tests for the wx-free stream-lifecycle event state machine."""

from kicad_plugin.ui.stream_events import apply_stream_event, make_ai_entry


def _ts() -> str:
    return "00:00:00"


def _state(**overrides):
    state = {
        "pending": "",
        "entries": [],
        "tool_seq": 0,
        "tool_calls_made": False,
        "turn_had_text": False,
        "delta_chars": 0,
        "cancelled": False,
    }
    state.update(overrides)
    return state


def _apply(state, evt):
    """Apply one event against the current scalar state (mimics the panel consumer)."""
    st = apply_stream_event(
        pending=state["pending"],
        entries=state["entries"],
        tool_seq=state["tool_seq"],
        tool_calls_made=state["tool_calls_made"],
        turn_had_text=state["turn_had_text"],
        delta_chars=state["delta_chars"],
        cancelled=state["cancelled"],
        evt=evt,
        timestamp=_ts,
    )
    state.update(
        pending=st.pending,
        tool_seq=st.tool_seq,
        tool_calls_made=st.tool_calls_made,
        turn_had_text=st.turn_had_text,
        delta_chars=st.delta_chars,
    )
    return st


def test_text_chunks_accumulate_into_draft():
    s = _state()
    st = _apply(s, {"type": "text_start"})
    assert st.draft_changed is False  # ordering marker only

    st = _apply(s, {"type": "text_chunk", "content": "Hello"})
    assert s["pending"] == "Hello"
    assert st.draft_changed is True
    assert st.entries_changed is False
    assert s["turn_had_text"] is True
    assert s["delta_chars"] == 5

    _apply(s, {"type": "text_chunk", "content": " world"})
    assert s["pending"] == "Hello world"
    assert s["entries"] == []


def test_text_end_finalises_draft_as_ai_entry():
    s = _state()
    _apply(s, {"type": "text_chunk", "content": "full answer"})
    st = _apply(s, {"type": "text_end"})

    assert st.entries_changed is True
    assert s["pending"] == ""
    assert s["entries"] == [{"type": "ai", "text": "full answer", "timestamp": "00:00:00"}]


def test_text_end_with_empty_draft_is_noop():
    s = _state()
    st = _apply(s, {"type": "text_end"})
    assert st.entries_changed is False
    assert s["entries"] == []


def test_bug_b_reload_card_no_longer_truncates_final_answer():
    """End-of-turn ``reload_kicad`` is enqueued strictly after ``text_end``, so
    the FULL answer is archived before the card.

    Regression test for the real incident: answers of 1105/1611/1009/844 chars
    ended up as final entries of only 14/1/6/0 chars because the reload
    notification finalised a draft whose last ~50 ms were still in transit.
    With the FIFO queue that tail cannot exist here: text_end is applied
    before the card, so the final AI entry must equal the full reply.
    """
    answer = "x" * 1105  # same size as the documented Bug B turn
    s = _state()
    for i in range(0, len(answer), 10):  # chunk the stream like an SSE pump
        _apply(s, {"type": "text_chunk", "content": answer[i : i + 10]})
    _apply(s, {"type": "text_end"})
    _apply(
        s,
        {
            "type": "tool_call",
            "name": "reload_kicad",
            "args": {"paths": ["/tmp/board.kicad_pcb"]},
            "result": {"success": True},
        },
    )
    _apply(s, {"type": "turn_end", "reply": answer})

    assert [e["type"] for e in s["entries"]] == ["ai", "tool_call"]
    text_entry = s["entries"][0]
    assert text_entry["text"] == answer  # complete — not a 14-char tail
    assert len(text_entry["text"]) == 1105
    # Nothing is archived after the reload card (no second/late AI entry).
    assert [e for e in s["entries"] if e["type"] == "ai"] == [text_entry]
    assert s["pending"] == ""  # draft fully consumed


def test_cancel_drops_later_text_but_keeps_tool_cards():
    s = _state()
    _apply(s, {"type": "text_chunk", "content": "partial answer"})

    s["cancelled"] = True
    st = _apply(s, {"type": "text_chunk", "content": " more"})
    assert s["pending"] == "partial answer"  # dropped, not appended
    assert st.draft_changed is False

    _apply(s, {"type": "text_end"})  # skipped while cancelled
    assert s["entries"] == []

    # Tool cards still land after cancel.
    _apply(s, {"type": "tool_call", "name": "get_net", "args": {}, "result": {"success": True}})
    assert s["entries"][-1]["type"] == "tool_call"

    # The partial draft is still pending (text_end was skipped), so turn_end
    # finalises it defensively and does NOT append the full reply.
    _apply(s, {"type": "turn_end", "reply": "full reply"})
    assert [e["type"] for e in s["entries"]] == ["tool_call", "ai"]
    assert s["entries"][-1]["text"] == "partial answer"


def test_turn_end_reply_fallback_when_never_streamed():
    """An error turn streams no text; the reply becomes the entry (the old
    ``was_streamed=False`` path)."""
    s = _state()
    st = _apply(s, {"type": "turn_end", "reply": "[LLM error] API down"})

    assert st.entries_changed is True
    assert s["entries"] == [{"type": "ai", "text": "[LLM error] API down", "timestamp": "00:00:00"}]


def test_turn_end_defensive_finalise_of_leftover_draft():
    """turn_end finalises a draft that somehow survived (no text_end seen)."""
    s = _state()
    _apply(s, {"type": "text_chunk", "content": "before"})
    st = _apply(s, {"type": "turn_end", "reply": "before"})

    assert st.entries_changed is True
    assert s["pending"] == ""
    assert s["entries"] == [{"type": "ai", "text": "before", "timestamp": "00:00:00"}]


def test_tool_call_entries_carry_seq_and_mark_tool_use():
    s = _state()
    _apply(
        s,
        {
            "type": "tool_call",
            "name": "edit_track",
            "args": {"x": 1},
            "result": {"success": True},
        },
    )
    _apply(
        s,
        {
            "type": "tool_call",
            "name": "reload_kicad",
            "args": {"paths": []},
            "result": {"success": True},
        },
    )

    assert s["tool_calls_made"] is True
    assert [e["_seq"] for e in s["entries"]] == [1, 2]
    assert [e["name"] for e in s["entries"]] == ["edit_track", "reload_kicad"]


def test_make_ai_entry_shape():
    entry = make_ai_entry("answer text", "12:34:56")
    assert entry == {"type": "ai", "text": "answer text", "timestamp": "12:34:56"}
