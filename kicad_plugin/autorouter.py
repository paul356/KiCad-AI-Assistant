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

from collections.abc import Callable
import glob
import logging
import os
import subprocess
import threading

log = logging.getLogger(__name__)

# Timeout (seconds) for the freerouting subprocess.
_SUBPROCESS_TIMEOUT = 600


def _strip_nets_from_dsn(dsn_path: str, net_names: list[str]) -> None:
    """Remove ``(net NAME ...)`` blocks whose name is in *net_names* from the DSN.

    Operates on the temp copy in-place.  Net names may be quoted
    (``"Net-(U1-PROG)"``) or bare (``GND``, ``+3.3V``).  Quoted strings
    inside the S-expression are handled correctly so that parentheses inside
    net names don't confuse the depth counter.
    """
    if not net_names:
        return

    skip_set = {name.strip() for name in net_names}

    with open(dsn_path, encoding="utf-8") as f:
        text = f.read()

    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] != "(":
            out.append(text[i])
            i += 1
            continue

        # Skip whitespace after '(' to read the keyword.
        j = i + 1
        while j < n and text[j] in " \t":
            j += 1

        k = j
        while k < n and text[k] not in " \t\n\r()":
            k += 1
        keyword = text[j:k]

        if keyword != "net":
            out.append(text[i])
            i += 1
            continue

        # Skip whitespace before net name.
        m = k
        while m < n and text[m] in " \t":
            m += 1

        # Read quoted or unquoted net name.
        if m < n and text[m] == '"':
            try:
                end_q = text.index('"', m + 1)
            except ValueError:
                # Malformed DSN: no closing quote — leave block untouched.
                log.warning(
                    "autoroute: malformed quoted net name in DSN at pos %d; leaving block", m
                )
                out.append(text[i])
                i += 1
                continue
            net_name = text[m + 1 : end_q]
        else:
            end_u = m
            while end_u < n and text[end_u] not in " \t\n\r()":
                end_u += 1
            net_name = text[m:end_u]
            if not net_name:
                # Empty or missing net name — leave block untouched.
                out.append(text[i])
                i += 1
                continue

        if net_name not in skip_set:
            out.append(text[i])
            i += 1
            continue

        # Skip the entire (net ...) block, respecting quoted strings.
        depth = 1
        pos = i + 1
        in_string = False
        while pos < n and depth > 0:
            c = text[pos]
            if in_string:
                if c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
            pos += 1
        i = pos
        # Drop the trailing newline(s) to avoid leaving blank lines.
        while i < n and text[i] in ("\n", "\r"):
            i += 1

    with open(dsn_path, "w", encoding="utf-8") as f:
        f.write("".join(out))


def find_freerouting_jar() -> str | None:
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
        os.path.join(
            home, ".local", "share", "kicad", "*", "scripting", "plugins", "*", "freerouting*.jar"
        ),
        os.path.join(
            home,
            "Library",
            "Preferences",
            "kicad",
            "*",
            "scripting",
            "plugins",
            "*",
            "freerouting*.jar",
        ),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    return None


def _run_subprocess(
    dsn_path: str,
    ses_path: str,
    on_progress: Callable[[str], None] | None,
    ignore_nets: list[str] | None,
    max_passes: int,
) -> tuple[bool, str, str, str]:
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
            "",
            "",
        )

    log.info("freerouting: using jar at %s", jar_path)

    cmd: list[str] = [
        "java",
        "-jar",
        jar_path,
        "-de",
        dsn_path,
        "-do",
        ses_path,
        "--gui.enabled=false",
        "-mp",
        str(max_passes),
    ]

    cmd_str = " ".join(cmd)
    _progress(f"Running FreeRouting (max {max_passes} passes)…")
    log.info("freerouting: %s", cmd_str)

    debug_prefix = f"Command: {cmd_str}\n"
    if ignore_nets:
        debug_prefix += f"Ignoring nets (removed from DSN): {', '.join(ignore_nets)}\n"
        _strip_nets_from_dsn(dsn_path, ignore_nets)
    debug_prefix += "---\n"

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

    stdout = debug_prefix + (result.stdout or "")
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
    on_progress: Callable[[str], None] | None = None,
    ignore_nets: list[str] | None = None,
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
        Net *names* FreeRouting should skip (e.g. ``["GND", "+3.3V"]``).
        These are removed from the DSN ``(network ...)`` section before
        FreeRouting runs, so they stay as ratsnest and are not routed.
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
