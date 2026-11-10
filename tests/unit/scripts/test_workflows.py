"""Shell syntax checks for GitHub workflow ``run:`` blocks.

PyYAML is a runtime dependency but not a declared dev dependency, and this
repository does not want to add one just for a test. The workflows are
therefore scanned with a targeted regex for YAML block scalar ``run:``
forms (``|``/``>`` with optional chomping or indentation indicators, including
a trailing comment) plus single-line ``run:`` commands, dedented, and checked
with ``bash -n``.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
_RUN_KEY = r"^(?P<indent>\s*)(?:-\s+)?run:"
_BLOCK_START = re.compile(_RUN_KEY + r"\s*[|>](?:[+-]?\d*|\d*[+-]?)?\s*(?:#.*)?$")
_INLINE_RUN = re.compile(_RUN_KEY + r"\s+(?P<command>\S.*)$")


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


def test_workflow_discovery_covers_every_workflow_file() -> None:
    assert len(_WORKFLOWS) == 4
    assert {path.name for path in _WORKFLOWS} == {
        "build-plugin.yml",
        "ci.yml",
        "enforce-develop-pr.yml",
        "publish.yml",
    }


def test_extractor_supports_yaml_block_scalar_forms(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "steps:\n"
        "  - run: |\n"
        "      echo pipe\n"
        "  - run: >\n"
        "      echo folded\n"
        "  - run: |-\n"
        "      echo strip\n"
        "  - run: |2\n"
        "        echo indented\n"
        "  - run: | # comment\n"
        "      echo commented\n"
        "  - run: echo inline\n",
        encoding="utf-8",
    )

    blocks = _extract_run_blocks(workflow)
    assert len(blocks) == 6
    assert "echo pipe" in blocks[0]
    assert "echo folded" in blocks[1]
    assert "echo strip" in blocks[2]
    assert "echo indented" in blocks[3]
    assert "echo commented" in blocks[4]
    assert "echo inline" in blocks[5]


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
