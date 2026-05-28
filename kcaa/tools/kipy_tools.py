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
from typing import Any

from fastmcp import Context, FastMCP

log = logging.getLogger(__name__)


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


def _connect(timeout_ms: int = 5000) -> Any:
    """Return a connected kipy.KiCad instance, or raise ConnectionError.

    Args:
        timeout_ms: Maximum time to wait for KiCad responses (default 5000ms).
    """
    try:
        import kipy  # imported lazily so the module loads even without kipy

        return kipy.KiCad(socket_path=_find_kicad_socket(), timeout_ms=timeout_ms)
    except ImportError as exc:
        raise RuntimeError(
            "kicad-python is not installed. Run: uv pip install kicad-python"
        ) from exc
    except Exception as exc:
        # kipy raises kipy.errors.ConnectionError (subclass of OSError) when
        # KiCad is not running or the socket is unavailable.
        raise RuntimeError(
            f"Not connected to KiCad: {exc}. Make sure KiCad is running and the IPC API is enabled."
        ) from exc


def register_kipy_tools(mcp: FastMCP) -> None:
    """Register KiCad IPC API tools with the MCP server."""

    # ------------------------------------------------------------------
    # check_kicad_ipc_connection
    # ------------------------------------------------------------------

    @mcp.tool()
    async def check_kicad_ipc_connection(ctx: Context | None = None) -> dict[str, Any]:
        """Check if the KiCad IPC socket is open and responsive.

        Uses kipy's ping() command to actively test the connection to the
        running KiCad instance. Works on both Linux (Unix domain sockets)
        and Windows (named pipes).

        Returns:
            dict with:
                success (bool): True if the tool executed without errors.
                connected (bool): True if KiCad is reachable via IPC.
                socket_path (str): The socket path that was tested.
                error (str): Present only if connection failed.
        """
        try:
            import kipy.errors  # noqa: PLC0415

            # Use longer timeout for ping check since KiCad may be slow to respond
            kicad = _connect(timeout_ms=10000)
            socket_path = _find_kicad_socket()

            try:
                kicad.ping()
                return {
                    "success": True,
                    "connected": True,
                    "socket_path": socket_path,
                }
            except kipy.errors.ConnectionError as exc:
                # Timeout means socket exists but KiCad is slow/busy
                # Connection refused/missing means socket doesn't exist
                error_msg = str(exc).lower()
                if "timed out" in error_msg or "timeout" in error_msg:
                    # Socket exists, just slow response - consider it connected
                    log.debug("IPC ping timed out, but socket appears available: %s", exc)
                    return {
                        "success": True,
                        "connected": True,
                        "socket_path": socket_path,
                        "warning": "Ping timed out but socket is available",
                    }
                else:
                    # Real connection error - socket doesn't exist or permission denied
                    return {
                        "success": True,
                        "connected": False,
                        "error": str(exc),
                    }
        except RuntimeError as exc:
            return {
                "success": True,
                "connected": False,
                "error": str(exc),
            }
        except Exception as exc:
            log.exception("Unexpected error in check_kicad_ipc_connection")
            return {
                "success": True,
                "connected": False,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # save_document
    # ------------------------------------------------------------------

    @mcp.tool()
    async def save_document(file_path: str, ctx: Context | None = None) -> dict:
        """Save the currently open document (PCB or schematic) in KiCad.

        Uses the IPC API to trigger KiCad's save operation, which writes
        the document to disk and clears the modified (dirty) flag in the UI.
        The document type is determined by the file extension.

        Args:
            file_path: Path to the document file (.kicad_pcb or .kicad_sch).

        Returns:
            dict with keys:
                success (bool): True if the save succeeded.
                document_type (str): Type of document saved ("pcb" or "schematic").
                error (str): Present only if the save failed.
        """
        try:
            kicad = _connect(timeout_ms=10000)

            if file_path.endswith(".kicad_pcb"):
                from kipy.board import Board  # noqa: PLC0415
                from kipy.proto.common.types import DocumentType  # noqa: PLC0415

                docs = kicad.get_open_documents(DocumentType.DOCTYPE_PCB)
                if not docs:
                    return {"success": False, "error": "No PCB is currently open in KiCad"}
                board = Board(kicad._client, docs[0])
                board.save()
                return {"success": True, "document_type": "pcb"}

            elif file_path.endswith(".kicad_sch"):
                from kipy.proto.common.types import DocumentType  # noqa: PLC0415
                from kipy.schematic import Schematic  # noqa: PLC0415

                docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
                if not docs:
                    return {"success": False, "error": "No schematic is currently open in KiCad"}
                schematic = Schematic(kicad._client, docs[0])
                schematic.save()
                return {"success": True, "document_type": "schematic"}

            else:
                return {"success": False, "error": f"Unsupported file type: {file_path}"}

        except RuntimeError as exc:
            return {"success": False, "error": str(exc)}
        except ConnectionError as exc:
            # kipy raises ConnectionError when IPC API is not available
            return {
                "success": False,
                "error": (
                    f"Cannot connect to KiCad IPC API: {exc}. "
                    "Please enable IPC in KiCad: Preferences → Preferences → Plugins → 'Enable KiCad API' (KiCad 9+)."
                ),
            }
        except Exception as exc:
            log.exception("Unexpected error in save_document")
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
        from kcaa.utils.kipy_reload import (  # noqa: PLC0415
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
