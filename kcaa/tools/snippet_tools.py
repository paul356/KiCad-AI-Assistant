"""KiCad snippet save/load tools.

Implements programmatic export of selected schematic regions to
``.kicad_snippet`` files — the same format KiCad's *File → Export →
Snippet* command writes, and which *Place → Reusable Design Blocks →
Add Library Snippet* can read.

Why this lives in kcaa:

The snippet format is documented but the only tooling KiCad ships for
creating snippets is the *Save Selection as Snippet* menu command.
There is no IPC API for it and no CLI flag.  This module builds the
``.kicad_snippet`` S-expression by hand from a parent schematic so the
LLM can produce reusable blocks without round-tripping through the GUI.

Selection model: a rectangular bbox in the parent's mm coordinate system.
All wires, junctions, labels, and symbols whose geometry falls inside
the bbox are exported.  Bbox-local origin is normalised so the snippet
sits at (0, 0) when pasted.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import uuid as uuidlib
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.skip_compat import safe_schematic

log = logging.getLogger(__name__)


# Bbox tolerance (mm) — elements whose geometry crosses the boundary by
# less than this are kept, so a pin exactly on the bbox edge is not lost.
# Matches KiCad's own clipboard snap tolerance (one grid step = 1.27 mm).
_BBOX_TOL = 1.27


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _point_in_bbox(
    px: float,
    py: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
    tol: float = _BBOX_TOL,
) -> bool:
    """True if (px, py) lies inside (or on the edge of) the bbox."""
    return (
        px >= bx - tol
        and px <= bx + bw + tol
        and py >= by - tol
        and py <= by + bh + tol
    )


def _rect_overlaps_bbox(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    bbox_x: float,
    bbox_y: float,
    bbox_w: float,
    bbox_h: float,
) -> bool:
    """True if axis-aligned rect (ax,ay)-(bx,by) intersects the selection bbox."""
    lo_x, hi_x = min(ax, bx), max(ax, bx)
    lo_y, hi_y = min(ay, by), max(ay, by)
    bbox_hi_x = bbox_x + bbox_w
    bbox_hi_y = bbox_y + bbox_h
    return not (
        hi_x < bbox_x - _BBOX_TOL
        or lo_x > bbox_hi_x + _BBOX_TOL
        or hi_y < bbox_y - _BBOX_TOL
        or lo_y > bbox_hi_y + _BBOX_TOL
    )


# ---------------------------------------------------------------------------
# Element collectors
# ---------------------------------------------------------------------------


def _collect_selected_wires(
    sch: Any, bx: float, by: float, bw: float, bh: float, dx: float, dy: float
) -> list[dict[str, Any]]:
    """Return wires that overlap the selection bbox, translated by (dx, dy)."""
    out: list[dict[str, Any]] = []
    try:
        wires = list(sch.wire)
    except AttributeError:
        return out
    for w in wires:
        try:
            ax = float(w.start.value[0])
            ay = float(w.start.value[1])
            ex = float(w.end.value[0])
            ey = float(w.end.value[1])
            uuid = w.uuid if hasattr(w, "uuid") else None
        except (AttributeError, ValueError, TypeError):
            continue
        if not _rect_overlaps_bbox(ax, ay, ex, ey, bx, by, bw, bh):
            continue
        out.append(
            {
                "start": [ax - dx, ay - dy],
                "end": [ex - dx, ey - dy],
                "uuid": uuid,
            }
        )
    return out


def _collect_selected_junctions(
    sch: Any, bx: float, by: float, bw: float, bh: float, dx: float, dy: float
) -> list[dict[str, Any]]:
    """Return junctions inside the selection bbox, translated by (dx, dy)."""
    out: list[dict[str, Any]] = []
    try:
        junctions = list(sch.junction)
    except AttributeError:
        return out
    for j in junctions:
        try:
            jx = float(j.at.value[0])
            jy = float(j.at.value[1])
            uuid = j.uuid if hasattr(j, "uuid") else None
        except (AttributeError, ValueError, TypeError):
            continue
        if not _point_in_bbox(jx, jy, bx, by, bw, bh):
            continue
        out.append({"at": [jx - dx, jy - dy], "uuid": uuid})
    return out


def _collect_selected_labels(
    sch: Any, bx: float, by: float, bw: float, bh: float, dx: float, dy: float
) -> list[dict[str, Any]]:
    """Return labels inside the selection bbox, translated by (dx, dy)."""
    out: list[dict[str, Any]] = []
    try:
        labels = list(sch.label)
    except AttributeError:
        return out
    for lbl in labels:
        try:
            lx = float(lbl.at.value[0])
            ly = float(lbl.at.value[1])
            text = str(lbl.value)
            uuid = lbl.uuid if hasattr(lbl, "uuid") else None
        except (AttributeError, ValueError, TypeError):
            continue
        if not _point_in_bbox(lx, ly, bx, by, bw, bh):
            continue
        out.append(
            {
                "at": [lx - dx, ly - dy],
                "value": text,
                "uuid": uuid,
            }
        )
    return out


def _collect_selected_symbols(
    sch: Any, bx: float, by: float, bw: float, bh: float, dx: float, dy: float
) -> list[dict[str, Any]]:
    """Return symbols whose placement is inside the bbox, translated by (dx, dy).

    A symbol is included if its anchor point falls inside the bbox.  Any
    pin that lands just outside (e.g. a wide part on the bbox edge) keeps
    the symbol selected; that way a wire connecting two symbols near the
    edge doesn't lose its endpoint pin in transit.
    """
    out: list[dict[str, Any]] = []
    try:
        symbols = list(sch.symbol)
    except AttributeError:
        return out
    for sym in symbols:
        try:
            at = _parsed_value_list(getattr(sym, "at", None))
            sx = float(at[0])
            sy = float(at[1])
            rot = int(at[2]) if len(at) >= 3 else 0
            lib_id_raw = _parsed_value_str(getattr(sym, "lib_id", None))
            ref = None
            value = None
            uuid = _parsed_value_str(getattr(sym, "uuid", None))
            for prop in sym.property:
                if prop.name == "Reference":
                    ref = prop.value
                elif prop.name == "Value":
                    value = prop.value
        except (AttributeError, ValueError, TypeError, IndexError):
            continue
        if not _point_in_bbox(sx, sy, bx, by, bw, bh):
            continue
        out.append(
            {
                "at": [sx - dx, sy - dy, rot],
                "lib_id": lib_id_raw,  # raw resolved form (e.g. "Device:R_Small") for lib lookup
                "lib_id_symdir": _normalise_lib_id(lib_id_raw),
                "reference": ref or "",
                "value": value or "",
                "uuid": uuid,
            }
        )
    return out


def _normalise_lib_id(lib_id: str) -> str:
    """Return the symdir-style library id for use inside .kicad_snippet.

    KiCad stores lib_id in two forms:
      - Resolved:  ``Device:R_Potentiometer``
      - Library:   ``R_Potentiometer``  (legacy / symdir-style)

    Snippets only carry a single lib_symbols section, so all references must
    resolve inside the snippet.  The symdir form is portable; the resolved
    form would force the host project to have the same library alias.
    """
    if not lib_id:
        return lib_id
    if ":" in lib_id:
        return lib_id.split(":", 1)[1]
    return lib_id


def _parsed_value_str(value: Any) -> str:
    """Return the actual string value held by a skip ``ParsedValue``.

    skip wraps leaf tokens in a ``ParsedValue`` whose ``__str__`` is the
    debug-style ``"name = value"`` form (because the wrapper carries the
    source-tree name alongside the value).  Callers that want to embed the
    raw string in a new S-expression need ``.value`` — which the wrapper
    exposes as a plain attribute — and a ``str()`` fallback for already-plain
    values.
    """
    if value is None:
        return ""
    value_attr = getattr(value, "value", None)
    if isinstance(value_attr, (str, int, float)):
        return str(value_attr)
    s = str(value)
    if " = " in s:
        return s.split(" = ", 1)[1].strip()
    return s


def _parsed_value_list(value: Any) -> list:
    """Return the actual list value held by a skip ``ParsedValue``.

    Like ``_parsed_value_str`` but for compound values such as
    ``(at x y rot)``.  Returns an empty list on failure so callers can
    skip the symbol cleanly.
    """
    if value is None:
        return []
    value_attr = getattr(value, "value", None)
    if isinstance(value_attr, list):
        return list(value_attr)
    # Try to coerce a sexpdata list/tuple into a plain list.
    try:
        return list(value)
    except TypeError:
        return []


def _extract_lib_symbols(sch: Any, lib_ids: set[str]) -> list[str]:
    """Serialise every parent lib_symbol whose lib_id is in *lib_ids*.

    Returns one S-expression string per match, including all child unit
    definitions, so the snippet is fully self-contained when pasted.

    skip internals:
      - ``sch.lib_symbols._libsyms_by_id`` is a dict mapping the *resolved*
        lib_id (``"Device:R_Small"``) → LibSymbol.
      - ``sym.raw`` returns a ``sexpdata`` list (parent + children)
      - ``sym.sexp`` / ``sym.name`` are ``None`` — do not call them

    The caller's ``lib_ids`` may be in symdir form (``"R_Small"``) because
    that is what we write into the snippet.  We match either form against
    the library's resolved-form keys.

    ``sexpdata.dumps(sym.raw)`` produces the KiCad-format string.
    """
    if not lib_ids:
        return []
    import sexpdata

    try:
        ls = sch.lib_symbols
    except AttributeError:
        return []
    by_id = getattr(ls, "_libsyms_by_id", None) or {}

    # Build a set of every form (resolved, symdir, bare) we might match.
    wanted: set[str] = set(lib_ids)
    for lib_id in list(lib_ids):
        wanted.add(lib_id)
        if ":" in lib_id:
            wanted.add(lib_id.split(":", 1)[1])

    blocks: list[str] = []
    for lib_id, sym in by_id.items():
        if lib_id not in wanted:
            continue
        try:
            raw = sym.raw
        except Exception:
            continue
        if raw is None:
            continue
        try:
            text = sexpdata.dumps(raw)
        except Exception:
            continue
        # Rewrite the resolved-form lib_id (``"Device:R_Small"``) into the
        # symdir form (``"R_Small"``) so the snippet does not depend on the
        # host project's library-alias table.  Only rewrite at the symbol's
        # own name token; child unit names (``"R_Small_0_1"`` etc.) are
        # unaffected because they don't carry a library prefix.
        if ":" in lib_id:
            symdir = lib_id.split(":", 1)[1]
            # Match the resolved form as the first quoted token after
            # ``(symbol `` — anything else (child unit names, etc.) keeps
            # its original text.
            text = re.sub(
                r'(\(symbol ")' + re.escape(lib_id) + r'(")',
                r"\1" + symdir + r"\2",
                text,
                count=1,
            )
        blocks.append(text)
    return blocks


# ---------------------------------------------------------------------------
# .kicad_snippet S-expression serialiser
# ---------------------------------------------------------------------------


def _sexp_escape(value: str) -> str:
    """Escape a string for use inside a double-quoted S-expression token."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _format_wire(w: dict[str, Any]) -> str:
    pts = " ".join(
        f"(xy {p[0]:.4f} {p[1]:.4f})" for p in (w["start"], w["end"])
    )
    uuid_str = f' (uuid "{_sexp_escape(w["uuid"])}")' if w.get("uuid") else ""
    return f"(wire (pts {pts}) (stroke (width 0) (type default)){uuid_str})"


def _format_junction(j: dict[str, Any]) -> str:
    uuid_str = f' (uuid "{_sexp_escape(j["uuid"])}")' if j.get("uuid") else ""
    return (
        f'(junction (at {j["at"][0]:.4f} {j["at"][1]:.4f}) '
        f"(color 0 0 0 0) (diameter 0){uuid_str})"
    )


def _format_label(lbl: dict[str, Any]) -> str:
    val = _sexp_escape(lbl["value"])
    uuid_str = f' (uuid "{_sexp_escape(lbl["uuid"])}")' if lbl.get("uuid") else ""
    return (
        f'(label "{val}" (at {lbl["at"][0]:.4f} {lbl["at"][1]:.4f} 0) '
        f"(effects (font (size 1.27 1.27)) (justify left bottom)){uuid_str})"
    )


def _format_symbol(s: dict[str, Any]) -> str:
    ax, ay, rot = s["at"]
    # Prefer the symdir form for portability across host projects; fall back
    # to the raw (resolved) form if the symdir key wasn't precomputed.
    lib_id = _sexp_escape(s.get("lib_id_symdir") or s.get("lib_id") or "")
    ref = _sexp_escape(s["reference"] or "")
    value = _sexp_escape(s["value"] or "")
    uuid_raw = s.get("uuid") or ""
    uuid_str = f' (uuid "{_sexp_escape(uuid_raw)}")' if uuid_raw else ""
    return (
        f'(symbol (lib_id "{lib_id}") (at {ax:.4f} {ay:.4f} {rot}) (unit 1) '
        f"(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) "
        f"(fields_autoplaced yes) "
        f'(property "Reference" "{ref}" (at 0 0 0) '
        f'(effects (font (size 1.27 1.27)) (justify left)) '
        f"(do_not_autoplace yes)) "
        f'(property "Value" "{value}" (at 0 0 0) '
        f'(effects (font (size 1.27 1.27)) (justify left)) '
        f"(do_not_autoplace yes)) "
        f"(instances{uuid_str}))"
    )


def _build_snippet_sexp(
    name: str,
    lib_symbols: list[str],
    junctions: list[dict[str, Any]],
    wires: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    snippet_uuid: str,
) -> str:
    """Serialise a ``(kicad_snippet ...)`` S-expression."""
    name_esc = _sexp_escape(name)
    lib_block = "\n".join(f"      {block}" for block in lib_symbols) if lib_symbols else ""
    lib_section = (
        f"    (lib_symbols\n{lib_block}\n    )" if lib_symbols else "    (lib_symbols)"
    )

    body_lines: list[str] = []
    for s in symbols:
        body_lines.append(_format_symbol(s))
    for w in wires:
        body_lines.append(_format_wire(w))
    for j in junctions:
        body_lines.append(_format_junction(j))
    for lbl in labels:
        body_lines.append(_format_label(lbl))
    body = "\n".join("    " + line for line in body_lines)

    return (
        f'(kicad_snippet (version 20231120) (generator "kcaa")\n'
        f'  (uuid "{_sexp_escape(snippet_uuid)}")\n'
        f'  (name "{name_esc}")\n'
        f"  (embedded_fonts no)\n"
        f"  (data\n"
        f"{lib_section}\n"
        f"{body}\n"
        f"  )\n"
        f")\n"
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_snippet_tools(mcp: FastMCP) -> None:
    """Register snippet save / load tools with the MCP server."""

    @mcp.tool()
    async def save_selection_as_snippet(
        schematic_path: str,
        output_path: str,
        bbox_x: float,
        bbox_y: float,
        bbox_width: float,
        bbox_height: float,
        snippet_name: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Save a rectangular region of a schematic as a KiCad ``.kicad_snippet``.

        All wires, junctions, labels, and placed symbols whose geometry
        overlaps the selection bbox are exported.  The snippet's local
        coordinate origin is normalised to ``(0, 0)`` so the block sits
        flush against the top-left of the destination sheet when pasted.

        Required ``lib_symbols`` for the exported symbols are bundled inside
        the snippet, so the file is portable across KiCad projects
        regardless of the host's library table.

        Snippet format notes:

        * The file follows the ``(kicad_snippet ...)`` S-expression layout
          KiCad itself uses (version ``20231120``).  Pasting into KiCad
          honours all bundled lib_symbols.
        * Symbol references are normalised to symdir form (``R_Potentiometer``
          rather than ``Device:R_Potentiometer``) so the snippet does not
          depend on the host project's library aliases.
        * Pin geometry comes from the bundled lib_symbols definitions; the
          snippet itself carries no ``(pin ...)`` overrides.

        Args:
            schematic_path: Absolute path to the parent ``.kicad_sch`` file.
            output_path: Absolute path for the new ``.kicad_snippet`` file.
                ``.kicad_snippet`` extension is appended if missing.
            bbox_x: Selection bbox left edge in mm (parent coordinates).
            bbox_y: Selection bbox top edge in mm (parent coordinates).
            bbox_width: Selection bbox width in mm.
            bbox_height: Selection bbox height in mm.
            snippet_name: Display name written into the snippet's
                ``(name ...)``.  Defaults to the output filename if empty.

        Returns:
            dict with keys:
                success (bool),
                output_path (str),
                snippet_uuid (str),
                counts (dict with ``wires``, ``junctions``, ``labels``,
                    ``symbols``, ``lib_symbols``),
                notes (list[str]).
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"error": f"Schematic file not found: {schematic_path!r}"}
        for name, val in [
            ("bbox_x", bbox_x),
            ("bbox_y", bbox_y),
            ("bbox_width", bbox_width),
            ("bbox_height", bbox_height),
        ]:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                return {"error": f"'{name}' must be a finite number (got {val})"}
        if bbox_width <= 0 or bbox_height <= 0:
            return {"error": "bbox_width and bbox_height must be > 0"}

        if not output_path.endswith(".kicad_snippet"):
            output_path = output_path + ".kicad_snippet"

        try:
            sch = safe_schematic(schematic_path)
        except Exception as exc:
            return {"error": f"Failed to open schematic: {exc}"}

        try:
            dx, dy = bbox_x, bbox_y

            symbols = _collect_selected_symbols(
                sch, bbox_x, bbox_y, bbox_width, bbox_height, dx, dy
            )
            wires = _collect_selected_wires(
                sch, bbox_x, bbox_y, bbox_width, bbox_height, dx, dy
            )
            junctions = _collect_selected_junctions(
                sch, bbox_x, bbox_y, bbox_width, bbox_height, dx, dy
            )
            labels = _collect_selected_labels(
                sch, bbox_x, bbox_y, bbox_width, bbox_height, dx, dy
            )

            # Collect the set of lib_ids we need to embed.  Use the raw
            # (resolved) form here so the lookup against the library's
            # _libsyms_by_id dict works; _extract_lib_symbols also accepts
            # symdir-form as a fallback.
            lib_ids = {s["lib_id"] for s in symbols if s.get("lib_id")}
            lib_blocks = _extract_lib_symbols(sch, lib_ids)

            notes: list[str] = []
            if symbols and not lib_blocks:
                notes.append(
                    f"Found {len(symbols)} symbol(s) but no matching lib_symbols entries; "
                    "the snippet may not paste correctly until the host project has the same libraries."
                )

            snippet_uuid = str(uuidlib.uuid4())
            sexp = _build_snippet_sexp(
                name=snippet_name or os.path.splitext(os.path.basename(output_path))[0],
                lib_symbols=lib_blocks,
                junctions=junctions,
                wires=wires,
                labels=labels,
                symbols=symbols,
                snippet_uuid=snippet_uuid,
            )

            # Atomic replace (write to .tmp, rename) so a crash never leaves
            # a partial snippet file on disk.
            tmp_path = output_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(sexp)
            if os.path.exists(output_path):
                bak_path = output_path + ".bak"
                shutil.copy(output_path, bak_path)
            os.replace(tmp_path, output_path)
        except Exception as exc:
            return {"error": f"Failed to write snippet: {exc}"}

        return {
            "success": True,
            "output_path": output_path,
            "snippet_uuid": snippet_uuid,
            "counts": {
                "wires": len(wires),
                "junctions": len(junctions),
                "labels": len(labels),
                "symbols": len(symbols),
                "lib_symbols": len(lib_blocks),
            },
            "notes": notes,
        }

    @mcp.tool()
    async def read_snippet(
        snippet_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Inspect a ``.kicad_snippet`` file and return a structured summary.

        Parses the snippet's ``(data ...)`` block to count wires, junctions,
        labels, and placed symbols.  This is a regex-based scan of the source
        text (skip doesn't model ``.kicad_snippet``), so returned counts
        are a rough sanity check rather than a strict inventory.

        Args:
            snippet_path: Absolute path to the ``.kicad_snippet`` file.

        Returns:
            dict with keys:
                success (bool),
                snippet_path (str),
                name (str),
                uuid (str),
                counts (dict),
                raw_size_bytes (int),
                note (str).
        """
        if not snippet_path.endswith(".kicad_snippet"):
            return {"error": f"Not a .kicad_snippet file: {snippet_path!r}"}
        if not os.path.isfile(snippet_path):
            return {"error": f"Snippet file not found: {snippet_path!r}"}

        with open(snippet_path, encoding="utf-8") as f:
            raw = f.read()

        counts = {
            "wires": len(re.findall(r"\(wire\b", raw)),
            "junctions": len(re.findall(r"\(junction\b", raw)),
            "labels": len(re.findall(r"\(label\b", raw)),
            # Match `(symbol` at start of a line (lib_symbols section) OR
            # `(symbol (lib_id ...)` (placed symbol with leading indent).
            "symbols": len(
                re.findall(
                    r"(?:^|\s)\(symbol\b", raw, flags=re.MULTILINE
                )
            ),
            "lib_symbols": len(
                re.findall(r'\(symbol "([^"]+)"', raw)
            ),
        }

        uuid_m = re.search(r'\(uuid "([^"]+)"\)', raw)
        name_m = re.search(r'\(name "([^"]*)"\)', raw)

        return {
            "success": True,
            "snippet_path": snippet_path,
            "name": name_m.group(1) if name_m else "",
            "uuid": uuid_m.group(1) if uuid_m else "",
            "counts": counts,
            "raw_size_bytes": len(raw),
            "note": (
                "Counts are regex-based on the source text; some lines may be counted "
                "inside (lib_symbols ...) too.  Use the returned counts as a rough "
                "sanity check, not a strict inventory."
            ),
        }