"""Unit tests for the wx-free auto-scroll decision helper.

The panel only pins the conversation to the newest entry when a turn is
producing output AND the user's "Auto-scroll" checkbox is checked.
"""

import pytest

from kicad_plugin.ui.panel import _should_follow_output


@pytest.mark.parametrize(
    ("auto_scroll", "follow", "expected"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
        (False, False, False),
    ],
)
def test_should_follow_output(auto_scroll: bool, follow: bool, expected: bool) -> None:
    assert _should_follow_output(auto_scroll, follow) is expected
