"""Best-effort helpers that trigger KiCad GUI reload after the MCP server
writes a file to disk.

Both functions are **fire-and-forget**: all errors are logged at DEBUG level
and never re-raised so that the calling tool can still return a success
result even when KiCad is not running or the IPC API is unavailable.
"""
import logging
import os

log = logging.getLogger(__name__)


def try_reload_pcb_in_kicad(pcb_path: str) -> None:
    """Silently revert the active board in KiCad to the version just saved.

    Calls ``kipy.board.Board.revert()`` via the IPC API so the PCB editor
    immediately shows the file the MCP tool just wrote.  If the active board's
    path does not match *pcb_path*, or if KiCad is not running, the call is a
    silent no-op.
    """
    try:
        import kipy  # lazy import — kipy is optional at server start-up

        kicad = kipy.KiCad()
        board = kicad.get_board()
        if board is None:
            return

        # Build the full path from the board's DocumentSpecifier.
        doc = board._doc
        board_full = (
            os.path.join(doc.project.path, doc.board_filename)
            if doc.project.path
            else doc.board_filename
        )
        try:
            if not os.path.samefile(pcb_path, board_full):
                log.debug(
                    "PCB in KiCad (%s) differs from saved file (%s); skipping reload",
                    board_full,
                    pcb_path,
                )
                return
        except OSError:
            pass  # samefile fails if a path does not yet exist; proceed anyway

        board.revert()
        log.info("Board %r reverted in KiCad GUI", doc.board_filename)
    except Exception as exc:
        log.debug("Could not reload PCB in KiCad: %s", exc)


def try_reload_schematic_in_kicad(sch_path: str) -> bool:
    """Silently revert the active schematic in KiCad to the version just saved.

    Uses ``kipy.schematic.Schematic.revert()`` via the IPC API (KiCad 11+).
    On KiCad 10 the schematic IPC revert is not supported; in that case the
    function returns ``False`` so the caller can surface a manual-reload hint.

    Returns:
        True  — the schematic was reverted in the KiCad GUI automatically.
        False — the revert could not be performed (KiCad not running, IPC not
                supported, or the schematic path did not match any open doc).
    """
    try:
        import kipy  # lazy import — kipy is optional at server start-up
        from kipy.proto.common.types import DocumentType  # noqa: PLC0415
        from kipy.schematic import Schematic  # noqa: PLC0415

        kicad = kipy.KiCad()
        docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        if not docs:
            log.debug("No open schematic documents in KiCad; skipping reload")
            return False

        for doc in docs:
            # Build the full path from the DocumentSpecifier.
            sch_full = (
                os.path.join(doc.project.path, doc.project.name + ".kicad_sch")
                if doc.project.path
                else doc.project.name + ".kicad_sch"
            )
            try:
                if not os.path.samefile(sch_path, sch_full):
                    log.debug(
                        "Schematic in KiCad (%s) differs from saved file (%s); skipping",
                        sch_full,
                        sch_path,
                    )
                    continue
            except OSError:
                pass  # samefile fails if a path does not yet exist; proceed anyway

            schematic = Schematic(kicad._client, doc)
            schematic.revert()
            log.info("Schematic %r reverted in KiCad GUI", sch_full)
            return True

        log.debug("No open schematic in KiCad matched %s; skipping reload", sch_path)
        return False
    except Exception as exc:
        log.debug("Could not reload schematic in KiCad: %s", exc)
        return False
