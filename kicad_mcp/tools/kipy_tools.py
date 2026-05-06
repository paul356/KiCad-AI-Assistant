"""
KiCad IPC API tools using kicad-python (kipy).

Requires KiCad 9+ to be running with the IPC API enabled (KICAD_IPC_API=ON,
which is the default in KiCad 9/10).  All tools in this module communicate
with the live KiCad GUI process via a local socket; they do NOT modify files
directly.

WARNING: run_action uses an unstable KiCad internal API.  Action names may
change between KiCad versions.
"""
import glob
import logging
import os
import platform
from typing import Any, Dict

from fastmcp import Context, FastMCP

log = logging.getLogger(__name__)

# The TOOL_ACTION name used to synchronise the board from the schematic.
# Verified by live testing against KiCad 10: this action opens the
# "Update PCB from Schematic" dialog.  The action is registered under the
# common tool namespace, not eeschema, so it works regardless of which
# editor window currently has focus.
_ACTION_UPDATE_PCB = "common.Control.updatePcbFromSchematic"


def _find_kicad_socket() -> str:
    """Return the IPC socket URL for the running KiCad instance.

    Preference order:
    1. ``KICAD_API_SOCKET`` environment variable (set by KiCad for plugins).
    2. Newest ``api*.sock`` file inside the KiCad temp directory (handles
       PID-stamped names like ``api-465732.sock`` created by KiCad 9+).
    3. Hard-coded default ``ipc:///tmp/kicad/api.sock``.
    """
    env = os.environ.get("KICAD_API_SOCKET")
    if env:
        return env

    if platform.system() == "Windows":
        from tempfile import gettempdir
        return f"ipc://{gettempdir()}\\kicad\\api.sock"

    # Candidate directories (Flatpak first, then standard)
    home = os.environ.get("HOME", "")
    candidate_dirs = []
    if home:
        candidate_dirs.append(f"{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad")
    candidate_dirs.append("/tmp/kicad")

    for sock_dir in candidate_dirs:
        matches = glob.glob(os.path.join(sock_dir, "api*.sock"))
        if matches:
            # Pick the most-recently modified socket (newest KiCad instance)
            newest = max(matches, key=os.path.getmtime)
            return f"ipc://{newest}"

    return "ipc:///tmp/kicad/api.sock"


def _connect() -> Any:
    """Return a connected kipy.KiCad instance, or raise ConnectionError."""
    try:
        import kipy  # imported lazily so the module loads even without kipy
        return kipy.KiCad(socket_path=_find_kicad_socket())
    except ImportError as exc:
        raise RuntimeError(
            "kicad-python is not installed. "
            "Run: uv pip install kicad-python"
        ) from exc
    except Exception as exc:
        # kipy raises kipy.errors.ConnectionError (subclass of OSError) when
        # KiCad is not running or the socket is unavailable.
        raise RuntimeError(
            f"Not connected to KiCad: {exc}. "
            "Make sure KiCad is running and the IPC API is enabled."
        ) from exc


def register_kipy_tools(mcp: FastMCP) -> None:
    """Register KiCad IPC API tools with the MCP server."""

    # ------------------------------------------------------------------
    # update_pcb_from_schematic
    # ------------------------------------------------------------------

    @mcp.tool()
    async def update_pcb_from_schematic(ctx: Context | None) -> Dict[str, Any]:
        """Trigger "Update PCB from Schematic" in the running KiCad instance.

        Calls ``kicad.run_action("eeschema.EditorControl.updatePCBFromSchematic")``
        via the KiCad IPC API.  This is equivalent to using
        **Tools → Update PCB from Schematic** inside KiCad, opening the
        interactive update dialog in the KiCad GUI.

        Because this action opens a modal dialog, KiCad does not send a reply
        back over the IPC socket until the user closes the dialog.  The tool
        therefore treats a connection timeout as a successful dispatch
        (the dialog is open and waiting for the user).

        Requires KiCad 9+ to be running with the IPC API enabled.

        WARNING: The underlying action name is part of an unstable KiCad
        internal API and may change in future KiCad versions.

        Returns:
            dict with:
                success (bool): True if the dialog was opened (or action dispatched).
                action (str): The action name that was called.
                status (str): Status description.
                error (str): Present only on failure.
        """
        try:
            from kipy.proto.common import commands as _commands  # noqa: PLC0415
            import kipy.errors  # noqa: PLC0415

            kicad = _connect()
            try:
                response = kicad.run_action(_ACTION_UPDATE_PCB)
                status_code = response.status
                log.info("run_action(%r) status: %r", _ACTION_UPDATE_PCB, status_code)
            except kipy.errors.ConnectionError as timeout_exc:
                # A timeout means KiCad opened a modal dialog and is waiting
                # for the user — this is the expected success path.
                if "timed out" in str(timeout_exc).lower():
                    log.info(
                        "run_action(%r) timed out — dialog is open in KiCad",
                        _ACTION_UPDATE_PCB,
                    )
                    return {
                        "success": True,
                        "action": _ACTION_UPDATE_PCB,
                        "status": "dialog_opened",
                    }
                raise

            if status_code == _commands.RAS_OK:
                return {
                    "success": True,
                    "action": _ACTION_UPDATE_PCB,
                    "status": "RAS_OK",
                }
            # Map known failure codes to human-readable messages.
            _status_messages = {
                _commands.RAS_UNKNOWN: "RAS_UNKNOWN — KiCad returned an unknown status",
                _commands.RAS_INVALID: (
                    "RAS_INVALID — action not found; "
                    "make sure a PCB or schematic is open in KiCad"
                ),
                _commands.RAS_FRAME_NOT_OPEN: (
                    "RAS_FRAME_NOT_OPEN — the required editor frame is not open; "
                    "open the PCB editor in KiCad and try again"
                ),
            }
            msg = _status_messages.get(
                status_code,
                f"Unexpected status code {status_code!r}",
            )
            return {
                "success": False,
                "action": _ACTION_UPDATE_PCB,
                "status": status_code,
                "error": msg,
            }
        except RuntimeError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log.exception("Unexpected error in update_pcb_from_schematic")
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # reload_kicad
    # ------------------------------------------------------------------

    @mcp.tool()
    async def reload_kicad(
        paths: list[str],
        ctx: Context | None = None,
    ) -> dict:
        """Reload one or more KiCad documents in the running KiCad GUI.

        Call this tool once after all edits are complete to reload the
        modified files in KiCad.  Pass every file that was modified during
        the current editing session; the tool dispatches to the appropriate
        revert call based on file extension.

        Supported extensions:
            .kicad_sch  — reverts the schematic via the IPC API
            .kicad_pcb  — reverts the board via the IPC API

        Args:
            paths: List of absolute file paths to reload (e.g.
                   ["/path/to/design.kicad_sch", "/path/to/design.kicad_pcb"]).

        Returns:
            dict with keys:
                success (bool): True if all requested reloads succeeded.
                reloaded (list[str]): Paths that were successfully reloaded.
                failed (list[str]): Paths that could not be reloaded (KiCad
                    not running, IPC unavailable, or path mismatch).
                errors (dict[str, str]): Per-path error messages for failures.
        """
        from kicad_mcp.utils.kipy_reload import (  # noqa: PLC0415
            try_reload_pcb_in_kicad,
            try_reload_schematic_in_kicad,
        )

        reloaded: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}

        for path in paths:
            if path.endswith(".kicad_sch"):
                ok = try_reload_schematic_in_kicad(path)
                if ok:
                    reloaded.append(path)
                else:
                    failed.append(path)
                    errors[path] = (
                        "Schematic could not be reloaded automatically. "
                        "Use File → Revert in the KiCad Schematic Editor."
                    )
            elif path.endswith(".kicad_pcb"):
                try:
                    try_reload_pcb_in_kicad(path)
                    reloaded.append(path)
                except Exception as exc:
                    failed.append(path)
                    errors[path] = str(exc)
            else:
                failed.append(path)
                errors[path] = f"Unsupported file extension: {path!r}"

        return {
            "success": len(failed) == 0,
            "reloaded": reloaded,
            "failed": failed,
            **({"errors": errors} if errors else {}),
        }
