"""No-connect flag tools for the KiCad MCP server.

Provides tools to add, list, and remove no-connect (``no_connect``) flags in
KiCad schematics using the skip library. A no-connect flag marks a pin as
intentionally unconnected so ERC does not report it as an error; in the
``.kicad_sch`` s-expression it is a top-level ``(no_connect (at X Y) (uuid …))``
element placed exactly on the pin endpoint.
"""

import logging
import math
import os
from typing import Any
import uuid

from fastmcp import Context, FastMCP
import sexpdata

from kcaa.utils.schematic_sexp_utils import save_schematic
from kcaa.utils.skip_compat import safe_schematic

log = logging.getLogger(__name__)


def _iter_no_connects(sch: Any) -> list[Any]:
    """Return the schematic's no_connect elements as a plain list.

    Handles the skip quirk where the ``no_connect`` attribute is a collection
    (``_elements``) when several exist, a single element when exactly one
    exists, and raises AttributeError when none exist. Mirrors
    ``_iter_schematic_labels`` in symbol_edit_tools.
    """
    try:
        coll = sch.no_connect
    except AttributeError:
        return []
    if coll is None:
        return []
    elements = getattr(coll, "_elements", None)
    if elements is not None:
        return list(elements)
    return [coll]


def register_no_connect_tools(mcp: FastMCP) -> None:
    """Register no-connect flag tools against *mcp*."""

    @mcp.tool()
    async def add_no_connect(
        schematic_path: str,
        x: float,
        y: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a no-connect flag at a coordinate in a KiCad schematic.

        A no-connect flag marks a pin as intentionally unconnected so ERC does
        not flag it. The flag must sit **exactly on a pin endpoint** to apply —
        coordinates are mm in screen convention (**+Y is down**). This tool does
        NOT auto-snap; pass the pin's x/y from ``extract_schematic_netlist``.

        A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            x: X coordinate of the pin to mark, in mm.
            y: Y coordinate of the pin to mark, in mm.

        Returns:
            dict with keys: success (bool), no_connect ({x, y}),
            file_modified, backup_path. On a pre-existing flag at the same
            spot: success with already_present=True and no file change.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        # Idempotent: don't stack a second flag on the same pin.
        for nc in _iter_no_connects(sch):
            try:
                at = nc.at.value
                if abs(float(at[0]) - x) <= 0.01 and abs(float(at[1]) - y) <= 0.01:
                    return {
                        "success": True,
                        "already_present": True,
                        "no_connect": {"x": x, "y": y},
                        "file_modified": None,
                    }
            except (AttributeError, IndexError, TypeError, ValueError):
                continue

        try:
            nc_tmpl = [
                sexpdata.Symbol("no_connect"),
                [sexpdata.Symbol("at"), x, y],
                [sexpdata.Symbol("uuid"), sexpdata.Symbol(str(uuid.uuid4()))],
            ]
            sch.new_from_list(nc_tmpl)
            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to add no-connect flag: {exc}"}

        return {
            "success": True,
            "no_connect": {"x": x, "y": y},
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }

    @mcp.tool()
    async def list_no_connects(
        schematic_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List all no-connect flags in a KiCad schematic.

        Use the returned coordinates with ``remove_no_connect`` to delete a
        specific flag.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.

        Returns:
            dict with keys: success (bool), no_connects (list of {x, y}),
            count (int).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        no_connects = []
        for nc in _iter_no_connects(sch):
            try:
                at = nc.at.value
                no_connects.append({"x": float(at[0]), "y": float(at[1])})
            except (AttributeError, IndexError, TypeError, ValueError):
                continue

        return {"success": True, "no_connects": no_connects, "count": len(no_connects)}

    @mcp.tool()
    async def remove_no_connect(
        schematic_path: str,
        x: float,
        y: float,
        tolerance: float = 0.01,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove no-connect flag(s) at a coordinate in a KiCad schematic.

        Deletes every no-connect flag whose position matches (x, y) within
        *tolerance*. Use ``list_no_connects`` first to obtain exact
        coordinates. A backup (.kicad_sch.bak) is written before saving.

        Args:
            schematic_path: Absolute path to the target .kicad_sch file.
            x: X coordinate of the flag in mm.
            y: Y coordinate of the flag in mm.
            tolerance: Maximum coordinate difference considered a match
                (default 0.01 mm).

        Returns:
            dict with keys: success (bool), deleted_count (int),
            file_modified, backup_path. deleted_count=0 (with success=True)
            when no flag matched; no file is written in that case.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        if not math.isfinite(x) or not math.isfinite(y):
            return {"error": f"Coordinates must be finite numbers (got x={x}, y={y})"}

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        matched = []
        for nc in _iter_no_connects(sch):
            try:
                at = nc.at.value
                if abs(float(at[0]) - x) <= tolerance and abs(float(at[1]) - y) <= tolerance:
                    matched.append(nc)
            except (AttributeError, IndexError, TypeError, ValueError):
                continue

        if not matched:
            return {
                "success": True,
                "deleted_count": 0,
                "file_modified": None,
            }

        try:
            for nc in matched:
                nc.delete()
            save_schematic(schematic_path, sch)
        except Exception as exc:
            return {"error": f"Failed to remove no-connect flag: {exc}"}

        return {
            "success": True,
            "deleted_count": len(matched),
            "file_modified": schematic_path,
            "backup_path": schematic_path + ".bak",
        }
