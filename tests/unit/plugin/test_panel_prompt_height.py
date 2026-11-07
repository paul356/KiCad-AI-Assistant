"""Unit tests for the pure prompt-field height arithmetic (issue #252).

Only ``_prompt_height_for_drag`` is covered here.  The wx binding (grip panel,
mouse capture, double-click reset) needs a live GUI and is untested by design:
the test suite runs without wx installed.
"""

from kicad_plugin.ui.panel import _PROMPT_MIN_HEIGHT, _prompt_height_for_drag


def test_grow_adds_pointer_delta() -> None:
    assert _prompt_height_for_drag(60, 25, 400) == 85


def test_shrink_subtracts_pointer_delta() -> None:
    assert _prompt_height_for_drag(60, -20, 400) == 40


def test_zero_delta_keeps_start_height() -> None:
    assert _prompt_height_for_drag(60, 0, 400) == 60


def test_min_height_clamp() -> None:
    assert _prompt_height_for_drag(40, -100, 400) == _PROMPT_MIN_HEIGHT


def test_cap_from_conversation_budget() -> None:
    # A drag start recorded cap of 60 + 200 available px from the
    # conversation view: growing 500 px stops at 260.
    assert _prompt_height_for_drag(60, 500, 260) == 260


def test_cap_below_min_falls_back_to_min_height() -> None:
    # No conversation space to give up: the cap (20) is under the minimum,
    # so both grow and shrink clamp to the minimum.
    assert _prompt_height_for_drag(60, 500, 20) == _PROMPT_MIN_HEIGHT
    assert _prompt_height_for_drag(60, -500, 20) == _PROMPT_MIN_HEIGHT


def test_custom_min_height_is_respected() -> None:
    assert _prompt_height_for_drag(50, -100, 400, min_height=44) == 44
