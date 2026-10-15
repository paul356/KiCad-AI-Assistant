"""Unified stream-lifecycle event queue core (wx-free).

The background LLM thread is the *sole producer* of turn-internal events
(text_start / text_chunk / text_end / tool_call / turn_end) and enqueues
them in occurrence order into a FIFO deque owned by the panel; the panel's
50 ms timer is the *sole consumer*, which drains the deque and applies each
event through :func:`apply_stream_event`, then renders once.

Because the FIFO order is the only source of truth, a ``text_end`` is always
applied before the ``tool_call`` / ``turn_end`` events that follow it.  The
race where an end-of-turn notification (``reload_kicad``) finalised a draft
that was still being filled by the 50 ms text pipeline is therefore
structurally impossible, and draft finalisation is driven by the explicit
``text_end`` marker instead of by heuristics about pending text.

This module intentionally imports nothing from wx / webview / IO so the
state machine can be unit-tested without a display.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

log = logging.getLogger(__name__)

# Event payloads, produced in this order by the background thread:
#   {"type": "text_start"}                              ordering marker (no-op)
#   {"type": "text_chunk", "content": str}              streamed content
#   {"type": "text_end"}                                response text complete -> finalise draft
#   {"type": "tool_call", "name": str, "args": dict, "result": Any}
#   {"type": "turn_end", "reply": str, "failure": str | None}   turn complete (takes over _on_reply)
#   {"type": "status", "text": str, "color_hex": str}   transient system notice (e.g. compacted history)
#
# The panel tags every event with the generation of the turn it belongs to
# (``_gen``) and drops events of older turns before calling apply_stream_event.

AI_ENTRY_TYPES = ("user", "ai")

# Colour for a turn-end error reply surfaced as a status notice — mirrors the
# panel's _C_ERR_HEX.  Kept here (instead of importing panel, which is wx
# bound) so the state machine stays importable without a display.
STATUS_ERR_HEX = "#BE1E1E"


def make_ai_entry(text: str, timestamp: str) -> dict[str, Any]:
    """Build a permanent AI conversation entry (rendering format unchanged)."""
    return {"type": "ai", "text": text, "timestamp": timestamp}


@dataclass
class TurnState:
    """Scalar turn state after applying one event."""

    pending: str = ""
    tool_calls_made: bool = False
    turn_had_text: bool = False
    delta_chars: int = 0
    # Mutation flags for the caller's single merged render pass.
    draft_changed: bool = False
    entries_changed: bool = False


def apply_stream_event(
    *,
    pending: str,
    entries: list,
    tool_calls_made: bool,
    turn_had_text: bool,
    delta_chars: int,
    cancelled: bool,
    evt: dict[str, Any],
    timestamp: Callable[[], str],
) -> TurnState:
    """Apply one lifecycle event to the turn state (pure; ``entries`` mutated).

    Cancellation: text events are dropped once ``cancelled`` is set (the
    draft keeps whatever was already consumed, the turn-end finalises it);
    ``tool_call`` cards are still inserted so the user sees what the agent
    was doing; ``turn_end`` still closes the turn out.

    Returns the full updated scalar state plus mutation flags.
    """
    base = TurnState(
        pending=pending,
        tool_calls_made=tool_calls_made,
        turn_had_text=turn_had_text,
        delta_chars=delta_chars,
    )

    etype = evt.get("type")
    if etype == "text_start":
        return base  # ordering marker only

    if etype == "text_chunk":
        if cancelled:
            return base
        content = evt.get("content") or ""
        base.pending = pending + content
        base.turn_had_text = True
        base.delta_chars = delta_chars + len(content)
        base.draft_changed = True
        return base

    if etype == "text_end":
        if cancelled or not pending:
            return base
        entries.append(make_ai_entry(pending, timestamp()))
        base.pending = ""
        base.entries_changed = True
        return base

    if etype == "tool_call":
        _res = evt.get("result")
        log.info(
            "tool_call_flow stream_append: name=%s result_keys=%s _image_present=%s",
            evt.get("name", "?"),
            sorted(_res.keys()) if isinstance(_res, dict) else type(_res).__name__,
            "_image" in _res if isinstance(_res, dict) else False,
        )
        entries.append(
            {
                "type": "tool_call",
                "name": evt.get("name", "?"),
                "args": evt.get("args"),
                "result": evt.get("result"),
            }
        )
        base.tool_calls_made = True
        base.entries_changed = True
        return base

    if etype == "turn_end":
        if pending:  # defensive: text_end normally finalises the draft first
            entries.append(make_ai_entry(pending, timestamp()))
            base.pending = ""
            base.entries_changed = True
        reply = evt.get("reply") or ""
        failure = evt.get("failure")
        if reply:
            if not turn_had_text:
                # Non-streamed turn (LLM/framework error, iteration cap, or a
                # cancelled turn whose text never reached the draft): the reply
                # is the only answer text this turn produced.  Mirrors the old
                # ``was_streamed=False`` branch of _on_reply.
                entries.append(make_ai_entry(reply, timestamp()))
                base.entries_changed = True
            elif failure is not None:
                # The draft above (or the ai entry archived at text_end) is
                # the visible answer, so the reply is NOT appended as a second
                # AI entry; a failed turn must still surface or it looks like
                # the model silently stopped (issue #191).  Only the
                # structured ``failure`` category can mark a turn failed —
                # reply text is never inspected for failure detection, so an
                # explicit failure=None or a keyless event is a normal turn
                # even when the text happens to open with "[Error]".
                entries.append({"type": "status", "text": reply, "color_hex": STATUS_ERR_HEX})
                base.entries_changed = True
        return base

    if etype == "status":
        entries.append(
            {
                "type": "status",
                "text": evt.get("text", ""),
                "color_hex": evt.get("color_hex", "#1E1E1E"),
            }
        )
        base.entries_changed = True
        return base

    return base  # unknown event type — ignore
