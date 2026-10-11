"""Unit tests for the ``KICAD_MCP_RENDER_ROUTE_PNG`` kill-switch.

The per-route render is the slowest part of a routing call, so it is
*off by default*: a vision-capable model must opt in with an explicit
``=1``.  ``KICAD_MCP_SUPPORTS_VISION`` stays the base capability gate —
a text-only model never renders regardless of the switch.  The toggle
affects only whether PNG bytes are produced; the JSON envelope carries
no ``route_png`` key either way (covered by the routing-tool tests).
"""

import pytest

from kcaa.utils.config import render_route_png_enabled


@pytest.mark.parametrize(
    "value",
    ["0", "false", "no", "off", "", "2", "banana"],
)
def test_toggle_off_values_disable_render(monkeypatch, value):
    """Anything that is not an explicit on-value disables rendering —
    including an unset variable (the default)."""
    monkeypatch.setenv("KICAD_MCP_RENDER_ROUTE_PNG", value)
    assert render_route_png_enabled() is False


def test_toggle_defaults_to_off(monkeypatch):
    """Unset KICAD_MCP_RENDER_ROUTE_PNG: rendering is off."""
    monkeypatch.delenv("KICAD_MCP_RENDER_ROUTE_PNG", raising=False)
    assert render_route_png_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_toggle_on_values_enable_render(monkeypatch, value):
    monkeypatch.setenv("KICAD_MCP_RENDER_ROUTE_PNG", value)
    assert render_route_png_enabled() is True


def test_toggle_on_but_text_only_model_stays_off(monkeypatch):
    """The base vision capability gates the switch: a text-only model
    (KICAD_MCP_SUPPORTS_VISION=0) never renders even with the toggle
    set to on."""
    monkeypatch.setenv("KICAD_MCP_RENDER_ROUTE_PNG", "1")
    monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", "0")
    assert render_route_png_enabled() is False


def test_toggle_on_with_vision_enabled(monkeypatch):
    monkeypatch.setenv("KICAD_MCP_RENDER_ROUTE_PNG", "1")
    monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", "1")
    assert render_route_png_enabled() is True
