"""
Plugin-side autorouter: export DSN → FreeRouting → import SES.

All pcbnew calls happen here, inside the KiCad plugin process where the
pcbnew module is available.  This module is intentionally independent of
the MCP server so it can be used from the plugin UI without any network
round-trips.

Public API
----------
find_freerouting_jar() -> Optional[str]
    Locate freerouting.jar on disk.

run_autoroute(board, on_progress, on_done, ignore_nets, max_passes)
    Synchronous; intended to be called from a background thread.

start_autoroute_thread(board, on_progress, on_done, **kwargs) -> threading.Thread
    Convenience wrapper that creates and starts a daemon thread.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# Timeout (seconds) for the freerouting subprocess.
_SUBPROCESS_TIMEOUT = 600


def find_freerouting_jar() -> Optional[str]:
    """Return the absolute path to freerouting.jar, or None if not found.

    Search order:
    1. Same directory as this file (deployed plugin location).
    2. Glob over all KiCad user plugin directories (~/.local/share/kicad/…).
    """
    # 1. Plugin dir (most likely location when deployed) — match any version
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in glob.glob(os.path.join(plugin_dir, "freerouting*.jar")):
        if os.path.isfile(candidate):
            return candidate

    # 2. Glob fallback across all KiCad user scripting plugin dirs
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".local", "share", "kicad", "*",
                     "scripting", "plugins", "*", "freerouting.jar"),
        os.path.join(home, "Library", "Preferences", "kicad", "*",
                     "scripting", "plugins", "*", "freerouting.jar"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    return None


def run_autoroute(
    board,
    on_progress: Optional[Callable[[str], None]] = None,
    on_done: Optional[Callable[[bool, str, str, str], None]] = None,
    ignore_nets: Optional[List[str]] = None,
    max_passes: int = 50,
) -> None:
    """Run FreeRouting on *board* synchronously.

    Parameters
    ----------
    board:
        A ``pcbnew.BOARD`` instance (the object returned by
        ``pcbnew.GetBoard()``).
    on_progress:
        Optional callback ``(message: str) -> None`` for intermediate status
        updates.  Safe to call from a non-wx thread via ``wx.CallAfter``.
    on_done:
        Callback ``(success, message, stdout, stderr) -> None`` called once
        when autorouting finishes (regardless of success/failure).
    ignore_nets:
        List of net names that FreeRouting should not route (e.g.
        ``["GND", "VCC"]``).  Passed via ``-inc`` flag.
    max_passes:
        Maximum routing passes (``-mp`` flag).  Default 50.
    """
    def _progress(msg: str) -> None:
        log.info("autoroute: %s", msg)
        if on_progress:
            on_progress(msg)

    def _done(success: bool, message: str, stdout: str = "", stderr: str = "") -> None:
        log.info("autoroute done: success=%s  %s", success, message)
        if on_done:
            on_done(success, message, stdout, stderr)

    # ------------------------------------------------------------------ #
    # 1. Locate freerouting.jar
    # ------------------------------------------------------------------ #
    jar_path = find_freerouting_jar()
    if jar_path is None:
        _done(
            False,
            "freerouting.jar not found. Place it in the plugin directory "
            "alongside autorouter.py.",
        )
        return

    log.info("autoroute: using jar at %s", jar_path)

    # ------------------------------------------------------------------ #
    # 2. Export Specctra DSN to a temp directory
    # ------------------------------------------------------------------ #
    tmp_dir = tempfile.mkdtemp(prefix="kicad_autoroute_")
    dsn_path = os.path.join(tmp_dir, "board.dsn")
    ses_path = os.path.join(tmp_dir, "board.ses")

    try:
        try:
            import pcbnew
        except ImportError:
            _done(False, "pcbnew module not available — must run inside KiCad.")
            return

        _progress("Exporting Specctra DSN…")
        try:
            pcbnew.ExportSpecctraDSN(board, dsn_path)
        except Exception as exc:
            _done(False, f"DSN export failed: {exc}")
            return

        if not os.path.isfile(dsn_path):
            _done(False, "DSN file was not created — ExportSpecctraDSN may have failed silently.")
            return

        # ------------------------------------------------------------------ #
        # 3. Run FreeRouting
        # ------------------------------------------------------------------ #
        cmd: List[str] = [
            "java", "-jar", jar_path,
            "-de", dsn_path,
            "-do", ses_path,
            "--gui.enabled=false",
            "-mp", str(max_passes),
        ]
        if ignore_nets:
            cmd += ["-inc", ",".join(ignore_nets)]

        _progress(f"Running FreeRouting (max {max_passes} passes)…")
        log.info("autoroute: running %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except FileNotFoundError:
            _done(False, "'java' command not found. Install a JRE and ensure it is on PATH.")
            return
        except subprocess.TimeoutExpired:
            _done(False, f"FreeRouting timed out after {_SUBPROCESS_TIMEOUT}s.")
            return

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # ------------------------------------------------------------------ #
        # 4. Import routed SES back into KiCad
        # ------------------------------------------------------------------ #
        if not os.path.isfile(ses_path):
            _done(
                False,
                "FreeRouting did not produce a .ses file. "
                "Check the log output below for details.",
                stdout,
                stderr,
            )
            return

        _progress("Importing routed SES…")
        try:
            pcbnew.ImportSpecctraSES(board, ses_path)
        except Exception as exc:
            _done(False, f"SES import failed: {exc}", stdout, stderr)
            return

        try:
            pcbnew.Refresh()
        except Exception:
            pass  # Non-critical; board is already updated

        # FreeRouting has a known SMD padstack bug — warn the user.
        warning = (
            " Note: if this is a pure-SMD board and traces look sparse, "
            "FreeRouting may have skipped SMD pads with '(attach off)' padstacks."
        )
        _done(
            True,
            "Auto-routing complete — save the board to persist changes." + warning,
            stdout,
            stderr,
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def start_autoroute_thread(
    board,
    on_progress: Optional[Callable[[str], None]] = None,
    on_done: Optional[Callable[[bool, str, str, str], None]] = None,
    ignore_nets: Optional[List[str]] = None,
    max_passes: int = 50,
) -> threading.Thread:
    """Create, start, and return a daemon thread that calls ``run_autoroute``.

    All parameters are forwarded to :func:`run_autoroute`.
    """
    t = threading.Thread(
        target=run_autoroute,
        args=(board,),
        kwargs={
            "on_progress": on_progress,
            "on_done": on_done,
            "ignore_nets": ignore_nets,
            "max_passes": max_passes,
        },
        daemon=True,
        name="freerouting-worker",
    )
    t.start()
    return t
