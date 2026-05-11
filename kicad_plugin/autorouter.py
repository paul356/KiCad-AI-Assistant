"""
Plugin-side autorouter: run FreeRouting subprocess in a background thread.

IMPORTANT — thread safety
--------------------------
KiCad's pcbnew API is NOT thread-safe.  All pcbnew calls (ExportSpecctraDSN,
ImportSpecctraSES, Refresh) MUST be made from the wx main thread.

This module therefore contains NO pcbnew calls.  The caller is responsible
for:
  1. Calling ``pcbnew.ExportSpecctraDSN(board, dsn_path)`` on the main thread
     before starting the thread.
  2. Calling ``pcbnew.ImportSpecctraSES(board, ses_path)`` and
     ``pcbnew.Refresh()`` on the main thread inside the ``on_done`` callback
     (e.g. via ``wx.CallAfter``).

Public API
----------
find_freerouting_jar() -> Optional[str]
    Locate freerouting.jar on disk.

start_freerouting_thread(dsn_path, ses_path, on_done, ...) -> threading.Thread
    Run FreeRouting in a background thread; call on_done when finished.
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

# Timeout (seconds) for the freerouting subprocess.
_SUBPROCESS_TIMEOUT = 600


def find_freerouting_jar() -> Optional[str]:
    """Return the absolute path to freerouting*.jar, or None if not found.

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
                     "scripting", "plugins", "*", "freerouting*.jar"),
        os.path.join(home, "Library", "Preferences", "kicad", "*",
                     "scripting", "plugins", "*", "freerouting*.jar"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    return None


def _run_subprocess(
    dsn_path: str,
    ses_path: str,
    on_progress: Optional[Callable[[str], None]],
    ignore_nets: Optional[List[str]],
    max_passes: int,
) -> Tuple[bool, str, str, str]:
    """Run FreeRouting synchronously and return (success, message, stdout, stderr).

    This function has NO pcbnew calls and is safe to run in a background thread.
    The DSN file must already exist at *dsn_path* before this is called.
    """
    def _progress(msg: str) -> None:
        log.info("freerouting: %s", msg)
        if on_progress:
            on_progress(msg)

    # Locate jar
    jar_path = find_freerouting_jar()
    if jar_path is None:
        return (
            False,
            "freerouting.jar not found. Place it in the plugin directory.",
            "", "",
        )

    log.info("freerouting: using jar at %s", jar_path)

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
    log.info("freerouting: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "'java' command not found. Install a JRE and ensure it is on PATH.", "", ""
    except subprocess.TimeoutExpired:
        return False, f"FreeRouting timed out after {_SUBPROCESS_TIMEOUT}s.", "", ""

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if not os.path.isfile(ses_path):
        return (
            False,
            "FreeRouting did not produce a .ses file. Check the log output for details.",
            stdout,
            stderr,
        )

    return True, "FreeRouting completed.", stdout, stderr


def start_freerouting_thread(
    dsn_path: str,
    ses_path: str,
    on_done: Callable[[bool, str, str, str], None],
    on_progress: Optional[Callable[[str], None]] = None,
    ignore_nets: Optional[List[str]] = None,
    max_passes: int = 50,
) -> threading.Thread:
    """Start FreeRouting in a background daemon thread.

    Parameters
    ----------
    dsn_path:
        Path to the already-exported Specctra DSN file.
    ses_path:
        Desired output path for the routed SES file.
    on_done:
        Callback ``(success, message, stdout, stderr) -> None`` invoked from
        the worker thread when routing finishes.  Use ``wx.CallAfter`` to
        marshal this to the wx main thread before touching pcbnew or wx.
    on_progress:
        Optional callback ``(message: str) -> None`` for intermediate updates.
        Also invoked from the worker thread — use ``wx.CallAfter``.
    ignore_nets:
        Net names FreeRouting should skip (e.g. ``["GND", "VCC"]``).
    max_passes:
        Maximum routing passes (``-mp`` flag).
    """
    def _worker() -> None:
        success, message, stdout, stderr = _run_subprocess(
            dsn_path, ses_path, on_progress, ignore_nets, max_passes
        )
        on_done(success, message, stdout, stderr)

    t = threading.Thread(target=_worker, daemon=True, name="freerouting-worker")
    t.start()
    return t
