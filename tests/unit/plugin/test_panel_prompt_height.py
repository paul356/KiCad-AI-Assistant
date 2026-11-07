"""Unit tests for the pure prompt-field height arithmetic (issue #252).

Only ``_prompt_height_for_drag`` is covered here.  The wx binding (grip panel,
mouse capture, double-click reset) needs a live GUI and is untested by design:
the test suite runs without wx installed.
"""

from kicad_plugin.ui.panel import _PROMPT_MIN_HEIGHT, _prompt_height_for_drag


def test_grow_adds_pointer_delta() -> None:
    assert _prompt_height_for_drag(60, 25, 700) == 85


def test_shrink_subtracts_pointer_delta() -> None:
    assert _prompt_height_for_drag(60, -20, 700) == 40


def test_zero_delta_keeps_start_height() -> None:
    assert _prompt_height_for_drag(60, 0, 700) == 60


def test_min_height_clamp() -> None:
    assert _prompt_height_for_drag(40, -100, 700) == _PROMPT_MIN_HEIGHT


def test_max_height_leaves_room_for_panel() -> None:
    # 300 px panel -> at most 300 - 160 = 140 px for the prompt field.
    assert _prompt_height_for_drag(60, 500, 300) == 140


def test_tiny_panel_falls_back_to_min_height() -> None:
    # 100 - 160 is negative, so the minimum wins for any delta.
    assert _prompt_height_for_drag(60, 500, 100) == _PROMPT_MIN_HEIGHT
    assert _prompt_height_for_drag(60, -500, 100) == _PROMPT_MIN_HEIGHT


def test_custom_min_height_is_respected() -> None:
    assert _prompt_height_for_drag(50, -100, 700, min_height=44) == 44
