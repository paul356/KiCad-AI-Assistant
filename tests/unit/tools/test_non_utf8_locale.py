"""Tool-level UTF-8 regressions for non-UTF-8 locales (issue #327).

The tools under test serialise schematics through ``skip``, which opens
its output file with a bare ``open(fpath, "w")``.  Without an explicit
UTF-8 context that write follows the process locale: on Chinese Windows
(GBK) it either raises ``UnicodeEncodeError`` for µ or silently writes
GBK bytes that later fail to decode as UTF-8.

Each test runs the real tool in a subprocess whose preferred encoding is
forced away from UTF-8, then inspects the bytes that landed on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pin_edit.kicad_sch"
REPO_ROOT = Path(__file__).resolve().parents[3]
# Root sheet UUID of the fixture, as recorded in its instance paths.
ROOT_UUID = "b1f7a3c2-5d4e-4a91-8c6f-2e7d0b9a4c11"

# UTF-8 and GBK encodings of U+00B5 MICRO SIGN.
UTF8_MICRO = b"\xc2\xb5"
GBK_MICRO = b"\xa1\xc0"

# Executed in a subprocess whose preferred encoding is forced away from
# UTF-8.  Everything here stays ASCII so the script survives argv decoding
# under an ASCII locale; the micro sign is produced from an escape.
_CHILD_SCRIPT = """
import asyncio
import json
import locale
import os
import sys
import traceback

from kcaa.tools.sheet_tools import register_sheet_tools
from kcaa.tools.symbol_edit_tools import register_symbol_edit_tools


class _MockMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def main():
    encoding_path, schematic_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(encoding_path, "w", encoding="ascii") as fh:
        fh.write(locale.getpreferredencoding(False))

    mcp = _MockMCP()
    register_symbol_edit_tools(mcp)
    register_sheet_tools(mcp)

    if mode == "property":
        result = asyncio.run(
            mcp.tools["set_symbol_property"](
                schematic_path=schematic_path,
                items=[
                    {
                        "reference": "R1",
                        "property_name": "Value",
                        "property_value": "10\\u00b5F",
                    }
                ],
            )
        )
        if not result.get("success"):
            raise SystemExit("set_symbol_property failed: %s" % (result,))
        return

    result = asyncio.run(
        mcp.tools["add_sheet_symbol"](
            schematic_path=schematic_path,
            sheet_name="Power_Sub",
            sheet_file="power_sub.kicad_sch",
            x=30.48,
            y=121.92,
            width=40.64,
            height=33.02,
            create_child=True,
            child_paper="A4",
            child_title="Power Sub Sheet",
        )
    )
    if not result.get("success"):
        raise SystemExit("add_sheet_symbol failed: %s" % (result,))
    child_path = result.get("child_path")
    if not child_path or not os.path.isfile(child_path):
        raise SystemExit("child schematic not created: %r" % (child_path,))
    print(json.dumps({"child_path": child_path}))


try:
    main()
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    raise SystemExit(1)
"""


def _stage_schematic(tmp_path: Path) -> Path:
    """Copy the R1/R2 fixture and give R2 a non-ASCII value.

    The micro sign must already be in the file so that every tool which
    re-serialises the schematic has to write it back.
    """
    path = tmp_path / "pin_edit.kicad_sch"
    shutil.copy(FIXTURE, path)
    text = path.read_text(encoding="utf-8")
    target = '(property "Value" "Plain" (at 122.54 101.27 0)'
    assert target in text
    text = text.replace(target, '(property "Value" "10\u00b5F" (at 122.54 101.27 0)', 1)
    path.write_text(text, encoding="utf-8")

    # The sheet tools resolve the root sheet UUID through an owning project
    # file.  Without this, the upward search can adopt an unrelated project
    # from a shared temp directory.
    project = {"schematic": {"top_level_sheets": [{"filename": path.name, "uuid": ROOT_UUID}]}}
    (tmp_path / "pin_edit.kicad_pro").write_text(json.dumps(project), encoding="utf-8")
    return path


def _run_tool(tmp_path: Path, schematic_path: Path, mode: str) -> str:
    """Run one tool under a forced non-UTF-8 locale; return its encoding."""
    encoding_file = tmp_path / "encoding.txt"

    env = dict(os.environ)
    env.update({"PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "PYTHONUTF8": "0"})
    env.setdefault("KICAD_VERSION", "9.0")

    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(encoding_file), str(schematic_path), mode],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{mode} failed under non-UTF-8 locale:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    encoding = encoding_file.read_text(encoding="ascii")
    if encoding.lower().replace("-", "").replace("_", "") == "utf8":
        pytest.skip(f"could not force a non-UTF-8 locale (got {encoding!r})")
    return encoding


def _assert_utf8(path: Path) -> str:
    """Decode *path* as UTF-8, rejecting a GBK-encoded micro sign."""
    raw = path.read_bytes()
    assert GBK_MICRO not in raw, f"{path} contains GBK-encoded µ"
    return raw.decode("utf-8")


def test_set_symbol_property_writes_utf8_under_non_utf8_locale(tmp_path: Path) -> None:
    """set_symbol_property must save UTF-8 under a GBK-like locale.

    Simulates Chinese Windows, where ``open(fpath, "w")`` defaults to GBK.
    """
    path = _stage_schematic(tmp_path)
    _run_tool(tmp_path, path, "property")

    text = _assert_utf8(path)
    assert "10\u00b5F" in text
    assert UTF8_MICRO in path.read_bytes()
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "pin_edit.kicad_sch.bak").exists()


def test_add_sheet_symbol_writes_utf8_under_non_utf8_locale(tmp_path: Path) -> None:
    """add_sheet_symbol must save UTF-8 under a GBK-like locale.

    Simulates Chinese Windows, where re-serialising a schematic that
    already contains µ raised ``'gbk' codec can't encode``.
    """
    path = _stage_schematic(tmp_path)
    _run_tool(tmp_path, path, "sheet")

    parent_text = _assert_utf8(path)
    assert "10\u00b5F" in parent_text
    assert "Power_Sub" in parent_text

    child = tmp_path / "power_sub.kicad_sch"
    assert child.exists()
    _assert_utf8(child)

    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "pin_edit.kicad_sch.bak").exists()
