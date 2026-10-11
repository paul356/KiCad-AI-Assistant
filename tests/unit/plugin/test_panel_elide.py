"""Unit tests for the wx-free UI elision of tool-result image payloads.

``_elide_tool_result_image`` contracts:
- tool results with a ``_image`` dict get a shallow copy whose image data is
  truncated to a short preview + byte-length marker (a full base64 PNG would
  flood the tool card and the persisted session);
- the caller's original dict is NEVER mutated (the LLM history still
  consumes the full payload afterwards);
- results without an image (or with a tiny one) pass through as-is.
"""

from kicad_plugin.ui.panel import _elide_tool_result_image


def test_big_image_data_is_elided_into_preview() -> None:
    result = {
        "success": True,
        "text": "Rendered board",
        "_image": {"media_type": "image/png", "data": "A" * 1000},
    }
    out = _elide_tool_result_image(result)
    assert out is not result  # display copy, never the original
    assert out["text"] == "Rendered board"
    data = out["_image"]["data"]
    assert data.startswith("A" * 16)
    assert "[1000 bytes]" in data
    assert len(data) < 64


def test_original_dict_untouched() -> None:
    result = {
        "success": True,
        "_image": {"media_type": "image/png", "data": "B" * 5000},
    }
    _elide_tool_result_image(result)
    assert result["_image"]["data"] == "B" * 5000  # full payload preserved


def test_result_without_image_passes_through() -> None:
    result = {"success": True, "text": "plain result"}
    out = _elide_tool_result_image(result)
    assert out is result


def test_tiny_image_passes_through() -> None:
    result = {"success": True, "_image": {"media_type": "image/png", "data": "short"}}
    out = _elide_tool_result_image(result)
    assert out is result


def test_non_dict_result_passes_through() -> None:
    assert _elide_tool_result_image("just a string") == "just a string"
