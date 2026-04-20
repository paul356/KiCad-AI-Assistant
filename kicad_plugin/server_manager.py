"""
ServerManager: starts, monitors, and stops the kicad-mcp MCP server subprocess.

The server is launched in the 'plugin' profile via streamable-http transport
on a dynamically chosen localhost port.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_STARTUP_TIMEOUT_S = 15
_HEALTH_POLL_INTERVAL_S = 0.3
_HEALTH_PATH = "/mcp"  # FastMCP's default health-check endpoint


def _find_free_port() -> int:
    """Bind to port 0 and immediately release to get an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_port_open(port: int) -> bool:
    """Return True if something is accepting connections on localhost:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class ServerManager:
    """Manages the lifecycle of the kicad-mcp MCP server subprocess."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self._process: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def port(self) -> Optional[int]:
        """The port the server is listening on, or None if not started."""
        return self._port

    @property
    def base_url(self) -> Optional[str]:
        """HTTP base URL for the running server, or None."""
        if self._port is None:
            return None
        return f"http://127.0.0.1:{self._port}"

    @property
    def is_running(self) -> bool:
        """True if the server process is alive."""
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        """Start the MCP server. Returns True on success, False on timeout/error."""
        with self._lock:
            if self.is_running:
                log.debug("Server already running")
                return True

            port = self._settings.server_port or _find_free_port()
            log.info("Starting kicad-mcp server on port %d", port)

            env = self._build_env(port)
            cmd = self._build_command()

            log.debug("Server command: %s", cmd)
            try:
                kwargs: dict = {}
                if platform.system() == "Windows":
                    # Windows-specific: CREATE_NEW_PROCESS_GROUP isolates signals
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True

                log_dir = self._settings.resolved_log_dir
                log_path = os.path.join(log_dir, "kicad_mcp_server.log") if log_dir else None
                if log_path:
                    os.makedirs(log_dir, exist_ok=True)
                    log_file = open(log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime
                    kwargs["stdout"] = log_file
                    kwargs["stderr"] = log_file
                else:
                    kwargs["stdout"] = subprocess.DEVNULL
                    kwargs["stderr"] = subprocess.DEVNULL

                self._process = subprocess.Popen(cmd, env=env, **kwargs)
            except (FileNotFoundError, PermissionError) as e:
                log.error("Failed to launch server: %s", e)
                return False

        if not self._wait_for_ready(port):
            log.error("Server did not become ready in time")
            self.stop()
            return False

        with self._lock:
            self._port = port
        log.info("kicad-mcp server ready on port %d", port)
        return True

    def stop(self) -> None:
        """Terminate the server process gracefully."""
        with self._lock:
            if self._process is None:
                return
            process = self._process
            self._process = None
            self._port = None

        log.info("Stopping kicad-mcp server")
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except OSError as e:
            log.warning("Error stopping server: %s", e)

    def restart(self) -> bool:
        """Stop then start the server. Returns True on success."""
        self.stop()
        return self.start()

    def check(self) -> bool:
        """Return True if the server is alive, attempt restart if it crashed."""
        if self.is_running:
            return True
        log.warning("Server process has exited unexpectedly")
        return False

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_command(self) -> list[str]:
        """Build the command to launch the MCP server."""
        python = sys.executable or shutil.which("python3") or shutil.which("python") or "python3"
        return [python, "-m", "kicad_mcp.server"]

    def _build_env(self, port: int) -> dict[str, str]:
        """Build environment variables for the server subprocess."""
        env = os.environ.copy()
        env["MCP_TRANSPORT"] = "streamable-http"
        env["MCP_PORT"] = str(port)
        env["MCP_HOST"] = "127.0.0.1"  # never listen on 0.0.0.0
        env["KICAD_MCP_PROFILE"] = "plugin"

        if self._settings.server_log_dir:
            env["KICAD_MCP_LOG_DIR"] = self._settings.server_log_dir
        return env

    def _wait_for_ready(self, port: int, timeout: float = _STARTUP_TIMEOUT_S) -> bool:
        """Poll until the server accepts connections or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                proc = self._process
            if proc is not None and proc.poll() is not None:
                log.error("Server process exited during startup")
                return False
            if _is_port_open(port):
                return True
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        return False
