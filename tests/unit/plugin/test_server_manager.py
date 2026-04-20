"""Tests for ServerManager: port selection, env building, health check."""
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest

from kicad_plugin.server_manager import (
    ServerManager,
    _find_free_port,
    _is_port_open,
)


class TestFindFreePort:
    def test_returns_valid_port(self):
        port = _find_free_port()
        assert 1024 <= port <= 65535

    def test_port_is_available(self):
        port = _find_free_port()
        # Should be able to bind to it (it was just released)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))


class TestIsPortOpen:
    def test_closed_port_returns_false(self):
        port = _find_free_port()
        assert _is_port_open(port) is False

    def test_open_port_returns_true(self):
        port = _find_free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            assert _is_port_open(port) is True
        finally:
            srv.close()


class TestServerManager:
    def _make_manager(self, port=0, log_dir=""):
        settings = MagicMock()
        settings.server_port = port
        settings.server_log_dir = log_dir
        settings.llm_api_key = "sk-test"
        return ServerManager(settings)

    def test_initial_state(self):
        mgr = self._make_manager()
        assert mgr.port is None
        assert mgr.base_url is None
        assert mgr.is_running is False

    def test_base_url_format(self):
        mgr = self._make_manager()
        mgr._port = 12345
        assert mgr.base_url == "http://127.0.0.1:12345"

    def test_build_env_sets_profile(self):
        mgr = self._make_manager(port=9999)
        env = mgr._build_env(9999)
        assert env["KICAD_MCP_PROFILE"] == "plugin"
        assert env["MCP_TRANSPORT"] == "streamable-http"
        assert env["MCP_PORT"] == "9999"
        assert env["MCP_HOST"] == "127.0.0.1"

    def test_build_env_localhost_only(self):
        mgr = self._make_manager()
        env = mgr._build_env(1234)
        assert env["MCP_HOST"] == "127.0.0.1"
        assert "0.0.0.0" not in env.values()

    def test_build_env_includes_log_dir(self):
        mgr = self._make_manager(log_dir="/tmp/logs")
        env = mgr._build_env(1234)
        assert env["KICAD_MCP_LOG_DIR"] == "/tmp/logs"

    def test_build_command_uses_python_module(self):
        mgr = self._make_manager()
        cmd = mgr._build_command()
        assert "-m" in cmd
        assert "kicad_mcp.server" in cmd

    def test_stop_when_not_running(self):
        mgr = self._make_manager()
        # Should not raise
        mgr.stop()
        assert mgr.port is None

    def test_check_returns_false_when_not_running(self):
        mgr = self._make_manager()
        assert mgr.check() is False

    def test_start_uses_fixed_port_from_settings(self):
        """If settings.server_port > 0, that port is used."""
        mgr = self._make_manager(port=19999)
        with patch.object(mgr, "_wait_for_ready", return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            ok = mgr.start()
        assert ok is True
        assert mgr.port == 19999

    def test_start_auto_selects_port_when_zero(self):
        """If settings.server_port == 0, an OS-assigned port is used."""
        mgr = self._make_manager(port=0)
        with patch.object(mgr, "_wait_for_ready", return_value=True), \
             patch("subprocess.Popen") as mock_popen, \
             patch("kicad_plugin.server_manager._find_free_port", return_value=54321):
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            ok = mgr.start()
        assert ok is True
        assert mgr.port == 54321

    def test_start_returns_false_on_popen_failure(self):
        mgr = self._make_manager()
        with patch("subprocess.Popen", side_effect=FileNotFoundError("not found")):
            ok = mgr.start()
        assert ok is False
        assert not mgr.is_running

    def test_start_returns_false_on_timeout(self):
        mgr = self._make_manager()
        with patch("subprocess.Popen") as mock_popen, \
             patch.object(mgr, "_wait_for_ready", return_value=False):
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            ok = mgr.start()
        assert ok is False
