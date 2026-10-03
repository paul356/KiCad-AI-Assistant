"""export_pcb_layer_image: the image block is gated on the model's vision
capability (KICAD_MCP_SUPPORTS_VISION), set by the plugin from its
llm_supports_vision setting."""

from __future__ import annotations

import asyncio
import os

import pytest

from kcaa.tools.render_board_tools import register_render_board_tools

FIXTURE = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "integration",
        "fixtures",
        "test_routing_board.kicad_pcb",
    )
)


class TestExportPcbLayerImageVisionGate:
    def _make_mcp(self):
        pytest.importorskip("fastmcp")
        from fastmcp import FastMCP

        mcp = FastMCP(name="test-render")
        register_render_board_tools(mcp)
        return mcp

    def _call(self, pcb_path: str):
        mcp = self._make_mcp()
        tool = asyncio.run(mcp.get_tool("export_pcb_layer_image"))
        return asyncio.run(tool.fn(pcb_path=pcb_path))

    def test_omits_image_for_text_only_model(self, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_SUPPORTS_VISION", "0")
        raw = self._call(FIXTURE)
        assert isinstance(raw, str), f"expected a bare text report, got {type(raw).__name__}"
        assert "report=" in raw

    def test_returns_image_by_default(self):
        # Absent flag (standalone MCP clients): keep the image — the
        # plugin is the one that knows the model and always sets it.
        raw = self._call(FIXTURE)
        assert isinstance(raw, tuple) and len(raw) == 2
        from fastmcp.utilities.types import Image

        assert isinstance(raw[1], Image)
        assert raw[1].data[:8] == b"\x89PNG\r\n\x1a\n"
