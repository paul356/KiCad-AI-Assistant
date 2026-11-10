"""Shell syntax checks for GitHub workflow ``run:`` blocks.

PyYAML is a runtime dependency but not a declared dev dependency, and this
repository does not want to add one just for a test. The workflows are
therefore scanned with a targeted regex for ``run: |`` blocks (plus
single-line ``run:`` commands), dedented, and checked with ``bash -n``.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = [
    _REPO_ROOT / ".github" / "workflows" / "ci.yml",
    _REPO_ROOT / ".github" / "workflows" / "publish.yml",
]
_BLOCK_START = re.compile(r"^(?P<indent>\s*)run:\s*\|\s*$")
_INLINE_RUN = re.compile(r"^\s*run:\s+(?P<command>\S.*)$")


def _extract_run_blocks(workflow: Path) -> list[str]:
    """Return every shell body declared by a ``run:`` key in *workflow*."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        block_start = _BLOCK_START.match(line)
        if block_start:
            indent = len(block_start.group("indent"))
            index += 1
            body: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                    break
                body.append(candidate)
                index += 1
            nonempty = [candidate for candidate in body if candidate.strip()]
            common_indent = min(len(candidate) - len(candidate.lstrip()) for candidate in nonempty)
            blocks.append(
                "\n".join(
                    candidate[common_indent:] if candidate.strip() else "" for candidate in body
                )
                + "\n"
            )
            continue

        inline = _INLINE_RUN.match(line)
        if inline:
            blocks.append(inline.group("command"))
        index += 1

    return blocks


@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda path: path.name)
def test_workflow_run_blocks_parse_with_bash(workflow: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this platform")

    blocks = _extract_run_blocks(workflow)
    assert blocks, f"no run blocks found in {workflow.name}"

    for index, body in enumerate(blocks):
        result = subprocess.run(
            [bash, "-n"], input=body, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, (
            f"{workflow.name} run block {index} failed bash -n:\n{result.stderr}\n{body}"
        )
